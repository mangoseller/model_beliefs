"""Curse-of-knowledge analysis: the estimators per model, dataset, method, setting and reference.  No model calls.

    python -m second_order.curse [--models qwen3_8b qwen3_14b] [--resamples 2000]

Writes ``outputs/curse/``:

* ``estimates.csv``: ``leakage.estimate_with_points`` per model, dataset (``ckd``: latent and
  direct beliefs; ``bespoke``: direct) and case set.  Reference ``two_record`` (CKD: C
  against M and D, the headline) and ``single_record`` (C against M_single and D_single, as in the
  Methods).  Case sets: ``all``; ``comprehension_pass`` (CKD: patients whose C checks were
  correct in both option orders; bespoke: C samples whose follow-up was correct); and,
  for CKD, ``order_person_first`` and ``order_own_first`` (the record order the patient
  was assigned).  Bespoke placebo items are excluded.
* ``direction.csv``: CKD false-record leakage for the randomized sicker and healthier
  edits on the 64 eligible patients, and their difference, bootstrapped within label x
  direction.
* ``checks.csv``: CKD comprehension checks by setting, relation and record order, and
  the share of patients right in both option orders.
* ``role_swap.csv``: per patient and setting, the signed gaps C - M (original) and
  C_swap - D (ownership headers swapped), their references, and the paired role probes.
* ``placebo.csv``, ``report_control.csv``, ``bayes.csv``, ``bespoke_points.csv``: bespoke
  placebo gaps; C_ctrl_F - D_F (the friend read PERSON the true record); answers against
  the Bayes answer of the believer's evidence; per-item points for the figures.
* ``comprehension.csv``, ``retention.csv``, ``scatter.csv``, ``thresholds.csv``,
  ``decision_curves.csv``: follow-up accuracy, retention, the per-case gaps behind the
  regressions, the decision cutoffs implied by each fit, and the screening decision
  against the latent belief (after the paper's Figures 5 and 3).
* ``RESULTS.md``: the tables, the Camerer et al. reference w, and anomalies.

Figures: ``python -m second_order.figures``.
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd

from second_order import beliefs as BL
from second_order import config as C
from second_order import datasets as DS
from second_order import leakage as L
from second_order.conditions import CONDITIONS, SETTINGS, references
from second_order.report import display, md_table
from second_order.responses import accuracy_summary, responses_path

OUT = C.RESULTS / "curse"
METHODS = ("latent", "direct")
HEADLINE = ["lambda", "lambda_fieller", "tau", "tau_shared", "rho", "beta_C", "beta_D", "phi",
            "manipulation_median_abs_gap"]
SHARED = ["phi", "rho_pooled", "beta_D_pooled", "diff_lambda", "diff_tau", "diff_rho", "diff_phi_setting"]
AFFINE = ["intercept_C", "intercept_D", "phi_setting", "phi_D_on_A", "intercept_D_on_A", "phi_M_on_B", "intercept_M_on_B", "curvature_D", "curvature_C",
          "lambda_x_positive", "lambda_x_negative", "lambda_third_low", "lambda_third_mid", "lambda_third_high",
          "mean_position", "anchor_mean_M_minus_B"]
GROUP = ["model", "dataset", "method", "reference", "setting"]


def models_default() -> list[str]:
    present = sorted(p.name for p in C.BELIEFS.iterdir() if p.is_dir()) if C.BELIEFS.exists() else []
    wanted = list(C.load()["experiment"]["models"])
    return [m for m in wanted if m in present] or present


def read_csv(path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() and path.stat().st_size > 1 else pd.DataFrame()


# Case selection
def comprehension_pass(model: str, dataset: str, b: pd.DataFrame) -> pd.DataFrame | None:
    """Beliefs with each C condition restricted to answers whose comprehension check was right.

    CKD: patients right on both option orders of the setting's check.  Bespoke: the C means
    recomputed from the samples whose follow-up was right.  None if nothing was checked.
    """
    if dataset == "ckd":
        checks = read_csv(C.RESULTS / "checks" / f"{model}.csv")
        if checks.empty:
            return None
        checks = checks[checks.output.str.startswith("check/")]
        orders = checks.pivot(index=["case_id", "condition"], columns="output", values="correct")
        passed = set(orders.reindex(columns=["check/normal", "check/flip"]).eq(1).all(axis=1).loc[lambda x: x].index)
        checked = {"C", "C_F"}
        keep = ~b.condition.isin(checked) | pd.Series(list(zip(b.case_id, b.condition)), index=b.index).isin(passed)
        return b[keep]
    comp = read_csv(C.RESULTS / "comprehension" / f"{model}.csv")
    if comp.empty:
        return None
    good, checked, rows = comp[comp.correct == 1], comp.condition.unique(), []
    for condition in checked:
        path = responses_path(model, dataset, condition)
        if path.exists():
            rows.append(pd.read_parquet(path).merge(good[good.condition == condition][["case_id", "replicate", "sample"]],
                                                     on=["case_id", "replicate", "sample"]))
    if not rows:
        return None
    kept = BL.direct_rows(pd.concat(rows, ignore_index=True))
    kept = kept[kept.case_id.isin(set(b.case_id))]
    kept["dataset"] = dataset
    return pd.concat([b[~b.condition.isin(checked)], kept[b.columns]], ignore_index=True)


def case_sets(model: str, dataset: str, b: pd.DataFrame) -> dict[str, pd.DataFrame]:
    sets = {"all": b}
    passed = comprehension_pass(model, dataset, b)
    if passed is not None:
        sets["comprehension_pass"] = passed
    if dataset == "ckd":
        order = DS.patients().block_order
        for name in ("person_first", "own_first"):
            sets[f"order_{name}"] = b[b.case_id.map(order) == name]
    return sets


# Estimates
def beliefs_for(model: str, dataset: str, method: str) -> pd.DataFrame:
    b = BL.read(model, method, dataset)
    if dataset == "ckd" and "M2" in set(b.condition):
        raise ValueError("These CKD beliefs use the archived protocol. Generate the amended prompts in a new run.")
    return b[~b.case_id.isin(DS.placebo_ids())] if dataset == "bespoke" else b


def estimates(model: str, resamples: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    tables, scatter = [], []
    for dataset in C.datasets_for(model):
        for method in [m for m in METHODS if m in BL.methods_for(model, dataset)]:
            b = beliefs_for(model, dataset, method)
            for name, part in case_sets(model, dataset, b).items():
                t, pts = L.estimate_with_points(part, dataset, resamples=resamples, seed=seed)
                tables.append(t.assign(model=model, dataset=dataset, method=method, case_set=name))
                if name == "all":
                    scatter.append(pts.assign(model=model, dataset=dataset, method=method, cohort="headline"))
            if dataset == "ckd":
                _, pts = L.estimate_with_points(b, dataset, resamples=resamples, seed=seed, match_references=True, points_only=True)
                scatter.append(pts.assign(model=model, dataset=dataset, method=method, cohort="paired_layouts"))
    return concat(tables), concat(scatter)


def direction_contrast(model: str, resamples: int, seed: int) -> pd.DataFrame:
    """False-record lambda for the randomized sicker and healthier edits, stratified bootstrap."""
    patients = DS.patients()
    eligible = patients[patients.direction_contrast_eligible == "1"]
    rows = []
    for method in [m for m in METHODS if m in BL.methods_for(model, "ckd")]:
        W = L.wide(beliefs_for(model, "ckd", method))
        for reference in references("ckd"):
            k = L.roles("ckd", "false", reference)
            cols = [k[r] for r in "ABCMD"]
            part = W.loc[W.index.isin(eligible.index)].reindex(columns=cols).dropna()
            strata = eligible.loc[part.index, ["label", "false_direction"]].astype(str)
            strata = strata.label + "|" + strata.false_direction
            direction = eligible.loc[part.index, "false_direction"].to_numpy()

            def stat(arrays: dict, direction: np.ndarray) -> dict[str, float]:
                out = {}
                for name in ("sicker", "healthier"):
                    sel = {c: v[direction == name] for c, v in arrays.items()}
                    stats = L.setting_statistics(sel, k) if len(sel[k["A"]]) > 2 else {}
                    for key in ("lambda", "beta_D"):
                        out[f"{key}_{name}"] = stats.get(key, np.nan)
                out["diff_lambda_sicker_minus_healthier"] = out["lambda_sicker"] - out["lambda_healthier"]
                return out

            arrays = {c: part[c].to_numpy(float) for c in cols}
            point = stat(arrays, direction)
            rng = np.random.default_rng(seed)
            groups = [np.flatnonzero(strata.to_numpy() == s) for s in sorted(strata.unique())]
            draws = []
            for _ in range(int(resamples)):
                idx = np.concatenate([rng.choice(g, size=len(g)) for g in groups]) if groups else np.array([], dtype=int)
                draws.append(stat({c: v[idx] for c, v in arrays.items()}, direction[idx]))
            for key, value in point.items():
                d = np.array([x[key] for x in draws], dtype=float)
                d = d[np.isfinite(d)]
                lo, hi = np.percentile(d, [2.5, 97.5]) if len(d) else (np.nan, np.nan)
                reasons = []
                for group in ("sicker", "healthier"):
                    if key.startswith("beta_D") or group not in key:
                        continue
                    n = (direction == group).sum()
                    dens = np.array([x[f"beta_D_{group}"] for x in draws])
                    dens = dens[np.isfinite(dens)]
                    if n == 0:
                        reasons.append(f"empty subgroup ({group})")
                    elif n < 3 or not len(dens):
                        reasons.append(f"insufficient cases or variation ({group})")
                    elif np.percentile(dens, 2.5) <= 0 <= np.percentile(dens, 97.5):
                        reasons.append(f"beta_D_{group} interval includes 0")
                if reasons:
                    value = lo = hi = np.nan
                rows.append({"model": model, "method": method, "reference": reference, "statistic": key,
                             "blank_reason": ", ".join(reasons), "n_healthier": int((direction == "healthier").sum()),
                             "estimate": value, "ci_lower": lo, "ci_upper": hi, "n_cases": len(part),
                             "n_sicker": int((direction == "sicker").sum()), "resamples": int(resamples)})
    return pd.DataFrame(rows)


# CKD diagnostics
def check_tables(model: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(check accuracy by setting, relation and record order; role-swap gaps and probe pairs)."""
    scored = read_csv(C.RESULTS / "checks" / f"{model}.csv")
    if scored.empty:
        return pd.DataFrame(), pd.DataFrame()
    scored["question"] = scored.output.str.split("/").str[0]
    checks = scored[scored.question == "check"]
    acc = accuracy_summary(checks, ["model", "setting", "check_relation", "block_order"])
    keys = ["model", "setting", "check_relation", "block_order"]
    if not checks.empty:
        pairs = checks.pivot(index=keys + ["case_id"], columns="order", values="correct").reindex(columns=["normal", "flip"])
        paired = pairs.notna().all(axis=1)
        right = pairs.eq(1).all(axis=1)
        summary = pd.DataFrame({"patients": 1, "had_both": paired, "both_correct": right}).groupby(level=keys).sum()
        summary["both_orders_right"] = summary.both_correct / summary.had_both.replace(0, np.nan)
        acc = acc.merge(summary.reset_index(), on=keys)
    probes = scored[scored.question == "role_check"]
    probes = probes.assign(swapped=probes.condition.str.startswith("probe_swap"))
    pairs = probes.pivot(index=["model", "setting", "case_id", "output"], columns="swapped", values="correct")
    rows = []
    if {True, False} <= set(pairs.columns):
        for (m, setting), g in pairs.groupby(level=[0, 1]):
            answered = g.dropna(subset=[True, False])
            rows.append({"model": m, "setting": setting, "kind": "probe_pairs", "n": len(g), "had_both": len(answered),
                         "accuracy_original": g[False].mean(), "accuracy_swapped": g[True].mean(),
                         "both_right": (answered[False].eq(1) & answered[True].eq(1)).mean()})
    if "direct" in BL.methods_for(model, "ckd"):
        b = BL.read(model, "direct", "ckd")
        W = b[b.replicate == 0].pivot_table(index="case_id", columns="condition", values="value", aggfunc="first")
        for setting in SETTINGS:
            f = "_F" if setting == "false" else ""
            need = [f"C{f}", f"M{f}", f"D{f}", f"C_swap{f}"]
            if any(c not in W for c in need):
                continue
            w = W[need].dropna()
            gaps = {"C_minus_M": w[f"C{f}"] - w[f"M{f}"], "C_swap_minus_D": w[f"C_swap{f}"] - w[f"D{f}"],
                    "D_minus_M": w[f"D{f}"] - w[f"M{f}"], "C_swap_minus_C": w[f"C_swap{f}"] - w[f"C{f}"]}
            for name, gap in gaps.items():
                rows.append({"model": model, "setting": setting, "kind": f"gap_{name}", "n": len(gap),
                             "mean": gap.mean(), "se": gap.std(ddof=1) / np.sqrt(len(gap)),
                             "mean_abs": gap.abs().mean()})
    return acc, pd.DataFrame(rows)


def threshold_table(model: str) -> pd.DataFrame:
    """Implied against stated decision cutoffs per pooled fit family (CKD)."""
    path = C.LATENTS / f"{model}_parameters.parquet"
    if not path.exists():
        return pd.DataFrame()
    p = pd.read_parquet(path)
    if p.empty:
        return pd.DataFrame()
    p = p[p.parameter.isin(["nominal_threshold", "implied_threshold", "printed_threshold", "payoff_threshold"])]
    w = p.pivot_table(index=["fit_group", "output_name"], columns="parameter", values="value",
                      aggfunc="first").reset_index()
    w["family"] = w.fit_group.map(lambda g: CONDITIONS[g.split("+")[0]].about)
    w["kind"] = w.output_name.str.split("_").str[0].str.split("/").str[0]
    w["order"] = w.output_name.str.split("/").str[1]
    w.insert(0, "model", model)
    return w


def decision_curves(model: str, *, bandwidth: float = 0.10, points: int = 40, resamples: int = 200,
                    seed: int = 0) -> pd.DataFrame:
    """Smoothed P(recommend screening) against posterior mean z, per condition (CKD, both option orders)."""
    lat_path, mat_path = C.LATENTS / f"{model}.parquet", C.LATENTS / f"{model}_matrix.parquet"
    if not lat_path.exists() or not mat_path.exists():
        return pd.DataFrame()
    lat = pd.read_parquet(lat_path)
    lat = lat[(lat.replicate == 0) & (lat.split == "test")][["case_id", "condition", "z_without_decision"]].rename(columns={"z_without_decision": "z_mean"})
    mat = pd.read_parquet(mat_path)
    scheduled = sorted(mat.loc[mat.replicate == 0, "condition"].unique())
    mat = mat[mat.usable & (mat.replicate == 0)]
    mat = mat.assign(decide=mat[["decision/normal", "decision/flip"]].mean(axis=1))[["case_id", "condition", "decide"]]
    both = lat.merge(mat, on=["case_id", "condition"])
    observed = both.dropna(subset=["z_mean", "decide"])
    grid = np.linspace(*np.quantile(observed.z_mean, [0.05, 0.95]), points) if len(observed) else np.array([])
    rng = np.random.default_rng(seed)

    def smooth(z: np.ndarray, y: np.ndarray) -> np.ndarray:
        log_k = -0.5 * ((grid[:, None] - z[None, :]) / bandwidth) ** 2
        k = np.exp(log_k - log_k.max(axis=1, keepdims=True))
        return (k @ y) / k.sum(axis=1)

    rows = []
    for condition in scheduled:
        g = observed[observed.condition == condition]
        if len(g) < 3:
            rows.append(pd.DataFrame([dict(model=model, condition=condition, n_cases=len(g), status="insufficient cases", z=np.nan, p_decide=np.nan, ci_lower=np.nan, ci_upper=np.nan)]))
            continue
        z, y = g.z_mean.to_numpy(float), g.decide.to_numpy(float)
        draws = np.array([smooth(z[i], y[i]) for i in (rng.integers(0, len(z), len(z)) for _ in range(resamples))])
        lo, hi = np.nanpercentile(draws, [2.5, 97.5], axis=0)
        rows.append(pd.DataFrame({"model": model, "condition": condition, "z": grid, "p_decide": smooth(z, y),
                                  "ci_lower": lo, "ci_upper": hi, "n_cases": len(z), "status": "ok"}))
    return concat(rows)


# Bespoke diagnostics
def bespoke_points(model: str) -> pd.DataFrame:
    """Per bespoke item and condition: mean stated logit, its standard error and the Bayes logit."""
    if "direct" not in BL.methods_for(model, "bespoke"):
        return pd.DataFrame()
    b = BL.read(model, "direct", "bespoke")
    b = b[b.replicate == 0].copy()
    meta = [DS.row("bespoke", c, k, "probability") for c, k in zip(b.case_id, b.condition)]
    b["bayes_logit"] = [float(m["normative_logodds"]) for m in meta]
    b["family"] = [m["family"] for m in meta]
    b["placebo"] = [bool(m["placebo"]) for m in meta]
    b["role"] = b.condition.map(lambda c: CONDITIONS[c].role)
    b["setting"] = b.condition.map(lambda c: CONDITIONS[c].setting or "both")
    return b[["model", "case_id", "family", "placebo", "condition", "role", "setting", "value", "sd", "n_samples", "mean_probability", "logit_of_mean_probability",
              "bayes_logit"]]


def gap_rows(model: str, points: pd.DataFrame, items, pairs: dict[str, tuple[str, str]], setting: str) -> list[dict]:
    p = points.set_index(["case_id", "condition"])
    rows = []
    for case_id in items:
        for name, (first, second) in pairs.items():
            if (case_id, first) not in p.index or (case_id, second) not in p.index:
                continue
            r1, r2 = p.loc[(case_id, first)], p.loc[(case_id, second)]
            gap, se = r1.value - r2.value, float(np.hypot(r1.sd, r2.sd))
            rows.append({"model": model, "case_id": case_id, "setting": setting, "gap": name, "estimate": gap,
                         "se": se, "ci_lower": gap - 1.96 * se, "ci_upper": gap + 1.96 * se,
                         "n_samples": int(min(r1.n_samples, r2.n_samples))})
    return rows


def placebo_table(model: str, points: pd.DataFrame) -> pd.DataFrame:
    if points.empty:
        return pd.DataFrame()
    pairs = {"A_minus_B": ("A", "B"), "D_minus_M": ("D", "M"), "C_minus_M": ("C", "M")}
    return pd.DataFrame(gap_rows(model, points, sorted(DS.placebo_ids()), pairs, "partial"))


def report_control_table(model: str, points: pd.DataFrame) -> pd.DataFrame:
    """C_ctrl_F - D_F per item (0 if the model tracks which record was read), and C_F - D_F beside it."""
    if points.empty:
        return pd.DataFrame()
    items = sorted(set(points.case_id[points.condition == "C_ctrl_F"]))
    return pd.DataFrame(gap_rows(model, points, items, {"C_ctrl_minus_D": ("C_ctrl_F", "D_F"),
                                                         "C_minus_D": ("C_F", "D_F")}, "false"))


def bayes_table(model: str, points: pd.DataFrame) -> pd.DataFrame:
    if points.empty:
        return pd.DataFrame()
    b = points[~points.placebo]
    b = b.assign(beliefs=b.role.map(lambda r: "own" if r in ("A", "B") else "PERSON"))
    groups = [(about, "all", g) for about, g in b.groupby("beliefs")]
    groups += [(g.beliefs.iloc[0], c, g) for c, g in b.groupby("condition")]
    rows = []
    for about, condition, g in groups:
        if len(g) < 3:
            continue
        slope, intercept = np.polyfit(g.bayes_logit, g.value, 1)
        rows.append({"model": model, "beliefs": about, "condition": condition, "n": len(g),
                     "slope_on_bayes": slope, "intercept": intercept,
                     "mean_abs_error_logit": float(np.mean(np.abs(g.value - g.bayes_logit))),
                     "corr": float(np.corrcoef(g.bayes_logit, g.value)[0, 1])})
    return pd.DataFrame(rows)


def comprehension_table(model: str) -> pd.DataFrame:
    comp = read_csv(C.RESULTS / "comprehension" / f"{model}.csv")
    return accuracy_summary(comp, ["model", "dataset", "condition", "check_relation"]) if not comp.empty else comp


# Reporting utilities
def fmt(r) -> str:
    if r is None:
        return ""
    if r.get("statistic", r.name) == "lambda_fieller":
        kind = r.interval if isinstance(r.interval, str) else ""
        if kind == "bounded":
            return f"[{r.ci_lower:.3f}, {r.ci_upper:.3f}]"
        if kind == "exclusive":
            return f"outside ({r.ci_lower:.3f}, {r.ci_upper:.3f})"
        return kind
    if not np.isfinite(r.estimate):
        reason = r.blank_reason if isinstance(getattr(r, "blank_reason", None), str) else ""
        return f"blank ({reason})" if reason else ""
    return f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}]"


def headline_md(est: pd.DataFrame, stats: list[str]) -> str:
    rows = []
    for key, g in est.groupby(GROUP, sort=False):
        g = g.set_index("statistic")
        row = dict(zip(GROUP, key)) | {"model": display(key[0]), "n": int(g.n_cases.iloc[0])}
        for s in stats:
            row[s] = fmt(g.loc[s]) if s in g.index else ""
        rows.append(row)
    return md_table(pd.DataFrame(rows), GROUP + ["n"] + stats) if rows else "_(no rows)_\n"


def anomalies(est, placebo, control, comp, checks, retention) -> list[str]:
    out = []
    main = est[est.case_set == "all"] if not est.empty else est
    for _, r in main.iterrows():
        label = f"{r.model} {r.dataset} {r.method} {r.reference} {r.setting}"
        sig = np.isfinite(r.estimate) and (r.ci_lower > 0 or r.ci_upper < 0)
        match r.statistic:
            case "lambda_fieller" if r.interval != "bounded":
                out.append(f"Weak denominator: {label}: the lambda confidence set is {fmt(r)}.")
            case "rho" | "rho_pooled":
                if np.isfinite(r.estimate) and r.estimate < 0:
                    out.append(f"Negative reliability for {label} ({r.statistic} = {r.estimate:.3f}).")
                elif r.ci_lower <= 0 <= r.ci_upper:
                    out.append(f"{r.statistic} interval includes 0: {label}: {fmt(r)}; the ratios over it are blank.")
            case "curvature_D" | "curvature_C" if sig:
                out.append(f"Affine check: {r.statistic} interval excludes 0: {label}: {fmt(r)}.")
            case "diff_phi_setting" if sig:
                out.append(f"phi differs between settings: {label}: {fmt(r)} (the shared-phi assumption).")
            case "manipulation_median_abs_gap" if r.estimate < 0.5:
                out.append(f"Manipulation check below 0.5 logits: {label}: median |l_A - l_B| = {r.estimate:.3f}.")
    for frame, what in ((placebo, "Placebo gap"), (control, "Report-control gap")):
        for _, r in frame.iterrows():
            if r.get("gap") != "C_minus_D" and (r.ci_lower > 0 or r.ci_upper < 0):
                out.append(f"{what} away from 0: {r.model} {r.case_id} {r.gap}: "
                           f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}].")
    for frame, what in ((comp, "Comprehension"), (checks, "CKD check")):
        for _, r in frame.iterrows():
            if r.accuracy < 0.9:
                group = " ".join(str(r[c]) for c in frame.columns if c not in ("answered", "accuracy", "both_orders_right"))
                out.append(f"{what} accuracy below 90%: {group}: {r.accuracy:.1%}.")
    for _, r in retention.iterrows():
        if r.retention < 0.9:
            out.append(f"Retention below 90%: {r.model} {r.dataset} {r.condition}: {r.retention:.1%}.")
    return out


def write_results_md(models, tables: dict[str, pd.DataFrame]) -> None:
    est = tables["estimates"]
    w = float(C.load()["experiment"]["camerer_w"])
    parts = [f"# Curse of knowledge: results\n\nGenerated {dt.date.today().isoformat()} by "
             "`python -m second_order.curse`; figures by `python -m second_order.figures`.  Numbers only; no "
             "interpretation.  Estimates with 95% paired bootstrap percentile intervals over patients (CKD) or "
             "items (bespoke); unavailable ratios carry a reason in estimates.csv.  Bespoke placebo items "
             "are excluded from the estimates.\n\n"
             f"Models: {', '.join(display(m) for m in models)}.  Reference: Camerer, Loewenstein and Weber (1989) "
             f"find w ≈ {w} for people (a weight on dollar values, not on log-odds).\n\n"]
    if est.empty:
        parts.append("_(no estimates: run generation and `second_order.phase3` first)_\n")
    else:
        main = est[est.case_set == "all"]
        per, both = main[main.setting.isin(SETTINGS)], main[main.setting == "both"]
        parts.append("## Leakage and theory of mind, per setting\n\nlambda = beta_C / beta_D, the slopes of "
                     "l_C - l_M and l_D - l_M on x = l_A - l_B (0: no leakage, 1: PERSON is given the model's own "
                     "belief).  Reference `two_record` (CKD) uses C's matched layout, M and D. The `single_record` comparison "
                     "uses M_single and D_single.  lambda_fieller is the 95% Fieller confidence set.  rho = "
                     "Cov(x, x') / Var(x) from the second run. tau = (beta_D - beta_C) / rho per setting (Methods); tau_shared = phi - beta_C / rho with the shared phi. Reliability uses calibrated "
                     "values (latent posterior means de-shrunk per fit).  phi_setting = beta_D / rho in that "
                     "setting alone.\n\n" + headline_md(per, HEADLINE) + "\n")
        parts.append("## Responsiveness phi, shared by the settings\n\nphi = pooled beta_D / pooled rho "
                     "(within-setting slopes and covariances, calibrated values), on the cases complete for both "
                     "settings; diff_* = false minus partial.\n\n"
                     + headline_md(both, [s for s in SHARED if s in set(both.statistic)]) + "\n")
        for case_set, title in (("comprehension_pass", "Answers whose comprehension check was right"),
                                ("order_person_first", "CKD patients shown PERSON's record first"),
                                ("order_own_first", "CKD patients shown the model's own record first")):
            part = est[est.case_set == case_set]
            if not part.empty:
                parts.append(f"### {title}\n\n" + headline_md(part, ["lambda", "lambda_fieller", "tau", "phi"]) + "\n")
        parts.append("## Checks of the modelling assumptions\n\nAffine form: phi_D_on_A and phi_M_on_B estimate "
                     "phi from levels (instrumented by the second run); curvature_* are quadratic terms of the gap "
                     "regressions.  Partial linearity: lambda by the sign of the gap and within thirds of |x|, and "
                     "the level ratio mean_position.\n\n"
                     + headline_md(per, [s for s in AFFINE if s in set(est.statistic)]) + "\n")
    sections = [
        ("layouts", "Effect of the CKD chart layout",
         "The difference is two-chart minus one-chart leakage. Both estimates use the same patients.",
         ["model", "method", "setting", "statistic", "estimate", "ci_lower", "ci_upper", "n_cases", "blank_reason"]),
        ("direction", "CKD false records: sicker against healthier edits",
         "The 64 patients eligible for the randomized comparison; bootstrap within label x direction.",
         ["model", "method", "reference", "statistic", "estimate", "ci_lower", "ci_upper", "n_cases", "n_sicker", "n_healthier", "blank_reason"]),
        ("checks", "CKD comprehension checks", "Accuracy by setting, relation and record order; both_orders_right "
         "is the fraction correct on both among patients with both answers. The table also counts scheduled patients.", None),
        ("role_swap", "CKD role swaps", "Probe pairs: both_right is the share of pairs answered right in both "
         "ownership orientations (0 for a strategy that ignores ownership).  Gaps (direct beliefs, logits): "
         "C - M is the original leakage gap, C_swap - D the swapped one, D - M their reference.", None),
        ("placebo", "Bespoke placebo items", "All three gaps should be near 0.",
         ["model", "case_id", "gap", "estimate", "ci_lower", "ci_upper", "n_samples"]),
        ("report_control", "Bespoke report-assignment control", "C_ctrl - D: the friend read PERSON the true "
         "record, so a model tracking who heard what gives 0; C - D (the friend read the false record) beside it.",
         ["model", "case_id", "gap", "estimate", "ci_lower", "ci_upper", "n_samples"]),
        ("bayes", "Bespoke items against the Bayes answer", "Slope and intercept of the model's mean logit on the "
         "Bayes logit of the believer's evidence.", None),
        ("comprehension", "Bespoke comprehension follow-ups", "", None),
        ("retention", "Retention", "", None),
    ]
    for name, title, note, columns in sections:
        frame = tables[name]
        if frame.empty:
            continue
        frame = frame.assign(model=frame.model.map(display)) if "model" in frame else frame
        parts.append(f"## {title}\n\n{note + chr(10) + chr(10) if note else ''}"
                     + md_table(frame, columns or list(frame.columns)) + "\n")
    parts.append("## Anomalies\n\n" + "".join(f"- {a}\n" for a in anomalies(
        est, tables["placebo"], tables["report_control"], tables["comprehension"], tables["checks"],
        tables["retention"])) + "\n")
    (OUT / "RESULTS.md").write_text("".join(parts))


def concat(frames) -> pd.DataFrame:
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--resamples", type=int, default=int(C.load()["analysis"]["bootstrap_resamples"]))
    args = parser.parse_args()
    seed = int(C.load()["seed"])
    models = args.models or models_default()
    OUT.mkdir(parents=True, exist_ok=True)
    names = ["estimates", "scatter", "layouts", "direction", "checks", "role_swap", "placebo", "report_control", "bayes",
             "bespoke_points", "comprehension", "retention", "thresholds", "decision_curves"]
    collected: dict[str, list[pd.DataFrame]] = {n: [] for n in names}
    for model in models:
        print(f"curse analysis: {model}", flush=True)
        est, scat = estimates(model, args.resamples, seed)
        points = bespoke_points(model)
        acc, swap = check_tables(model) if "ckd" in C.datasets_for(model) else (pd.DataFrame(), pd.DataFrame())
        paired = scat[scat.cohort == "paired_layouts"] if not scat.empty else scat
        layouts = L.layout_difference(paired, resamples=args.resamples, seed=seed) if not paired.empty else paired
        found = {"layouts": layouts, "estimates": est, "scatter": scat, "checks": acc, "role_swap": swap, "bespoke_points": points,
                 "direction": direction_contrast(model, args.resamples, seed) if "ckd" in C.datasets_for(model)
                 else pd.DataFrame(),
                 "placebo": placebo_table(model, points), "report_control": report_control_table(model, points),
                 "bayes": bayes_table(model, points), "comprehension": comprehension_table(model),
                 "retention": read_csv(C.RESULTS / "retention" / f"{model}.csv"),
                 "thresholds": threshold_table(model),
                 "decision_curves": decision_curves(model, resamples=min(args.resamples, 200), seed=seed)}
        for name, frame in found.items():
            collected[name].append(frame)
    tables = {name: concat(frames) for name, frames in collected.items()}
    for name, frame in tables.items():
        frame.to_csv(OUT / f"{name}.csv", index=False)
    write_results_md(models, tables)
    print(f"wrote {OUT / 'RESULTS.md'}")


if __name__ == "__main__":
    main()
