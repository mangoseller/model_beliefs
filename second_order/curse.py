"""Curse-of-knowledge analysis: the estimators per model, dataset, method, setting and reference.  No model calls.

    python -m second_order.curse [--models qwen3_8b qwen3_14b] [--resamples 2000]

Writes ``outputs/curse/``:

* ``estimates.csv``: ``leakage.leakage_table`` per model, dataset (``ckd``: latent and
  direct beliefs; ``bespoke``: direct) and case set.  Reference ``two_record`` (CKD: C
  against M2 and D2, the headline) and ``single_record`` (C against M and D, as in the
  Methods).  Case sets: ``all``; ``comprehension_pass`` (CKD: patients whose C checks were
  correct in both option orders; bespoke: C samples whose follow-up was correct); and,
  for CKD, ``order_person_first`` and ``order_own_first`` (the record order the patient
  was assigned).  Bespoke placebo items are excluded.
* ``direction.csv``: CKD false-record leakage for the randomized sicker and healthier
  edits on the 64 eligible patients, and their difference, bootstrapped within label x
  direction.
* ``checks.csv``: CKD comprehension checks by setting, relation and record order, and
  the share of patients right in both option orders.
* ``role_swap.csv``: per patient and setting, the signed gaps C - M2 (original) and
  C_swap - D2 (ownership headers swapped), their references, and the paired role probes.
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
HEADLINE = ["lambda", "lambda_fieller", "tau", "rho", "beta_C", "beta_D", "phi_setting",
            "manipulation_median_abs_gap"]
SHARED = ["phi", "rho_pooled", "beta_D_pooled", "diff_lambda", "diff_tau", "diff_rho", "diff_phi_setting"]
AFFINE = ["phi_D_on_A", "intercept_D_on_A", "phi_M_on_B", "intercept_M_on_B", "curvature_D", "curvature_C",
          "lambda_x_positive", "lambda_x_negative", "lambda_third_low", "lambda_third_mid", "lambda_third_high",
          "mean_position", "anchor_mean_M_minus_B"]
GROUP = ["model", "dataset", "method", "reference", "setting"]


def models_default() -> list[str]:
    present = sorted(p.name for p in C.BELIEFS.iterdir() if p.is_dir()) if C.BELIEFS.exists() else []
    wanted = list(C.load()["experiment"]["models"])
    return [m for m in wanted if m in present] or present


def read_csv(path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


# ---------------------------------------------------------------- case sets
def comprehension_pass(model: str, dataset: str, b: pd.DataFrame) -> pd.DataFrame | None:
    """Beliefs with each C condition restricted to answers whose comprehension check was right.

    CKD: patients right on both option orders of the setting's check.  Bespoke: the C means
    recomputed from the samples whose follow-up was right.  None if nothing was checked.
    """
    if dataset == "ckd":
        checks = read_csv(C.RESULTS / "checks" / f"{model}.csv")
        if checks.empty:
            return None
        checks = checks[checks.output.str.startswith("check/")].dropna(subset=["correct"])
        both = checks.groupby(["case_id", "condition"]).correct.agg(["min", "size"])
        passed = set(both[(both["min"] == 1) & (both["size"] == 2)].index)
        checked = set(checks.condition)
        keep = ~b.condition.isin(checked) | pd.Series(list(zip(b.case_id, b.condition)), index=b.index).isin(passed)
        return b[keep]
    comp = read_csv(C.RESULTS / "comprehension" / f"{model}.csv")
    if comp.empty or comp.correct.notna().sum() == 0:
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


# ---------------------------------------------------------------- estimates
def beliefs_for(model: str, dataset: str, method: str) -> pd.DataFrame:
    b = BL.read(model, method, dataset)
    return b[~b.case_id.isin(DS.placebo_ids())] if dataset == "bespoke" else b


def estimates(model: str, resamples: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    tables, scatter = [], []
    for dataset in C.datasets_for(model):
        for method in [m for m in METHODS if m in BL.methods_for(model, dataset)]:
            b = beliefs_for(model, dataset, method)
            for name, part in case_sets(model, dataset, b).items():
                t = L.leakage_table(part, dataset, resamples=resamples, seed=seed)
                if not t.empty:
                    tables.append(t.assign(model=model, dataset=dataset, method=method, case_set=name))
            W = b[b.replicate == 0].pivot_table(index="case_id", columns="condition", values="value", aggfunc="first")
            for setting in SETTINGS:
                for reference in references(dataset):
                    k = L.roles(dataset, setting, reference)
                    if all(k[r] in W for r in "ABCMD"):
                        scatter.append(pd.DataFrame({
                            "model": model, "dataset": dataset, "method": method, "setting": setting,
                            "reference": reference, "case_id": W.index, "x_A_minus_B": W[k["A"]] - W[k["B"]],
                            "y_C_minus_M": W[k["C"]] - W[k["M"]], "y_D_minus_M": W[k["D"]] - W[k["M"]]}).dropna())
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
            if any(c not in W for c in cols):
                continue
            part = W.loc[W.index.isin(eligible.index), cols].dropna()
            strata = eligible.loc[part.index, ["label", "false_direction"]].astype(str).agg("|".join, axis=1)
            direction = eligible.loc[part.index, "false_direction"].to_numpy()

            def stat(arrays: dict, direction: np.ndarray) -> dict[str, float]:
                out = {}
                for name in ("sicker", "healthier"):
                    sel = {c: v[direction == name] for c, v in arrays.items()}
                    out[f"lambda_{name}"] = L.setting_statistics(sel, k)["lambda"] if len(sel[k["A"]]) > 2 else np.nan
                out["diff_lambda_sicker_minus_healthier"] = out["lambda_sicker"] - out["lambda_healthier"]
                return out

            arrays = {c: part[c].to_numpy(float) for c in cols}
            point = stat(arrays, direction)
            rng = np.random.default_rng(seed)
            groups = [np.flatnonzero(strata.to_numpy() == s) for s in sorted(strata.unique())]
            draws = []
            for _ in range(int(resamples)):
                idx = np.concatenate([rng.choice(g, size=len(g)) for g in groups])
                draws.append(stat({c: v[idx] for c, v in arrays.items()}, direction[idx]))
            for key, value in point.items():
                d = np.array([x[key] for x in draws], dtype=float)
                d = d[np.isfinite(d)]
                lo, hi = np.percentile(d, [2.5, 97.5]) if len(d) else (np.nan, np.nan)
                rows.append({"model": model, "method": method, "reference": reference, "statistic": key,
                             "estimate": value, "ci_lower": lo, "ci_upper": hi, "n_cases": len(part),
                             "n_sicker": int((direction == "sicker").sum()), "resamples": int(resamples)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- CKD diagnostics
def check_tables(model: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(check accuracy by setting, relation and record order; role-swap gaps and probe pairs)."""
    scored = read_csv(C.RESULTS / "checks" / f"{model}.csv")
    if scored.empty:
        return pd.DataFrame(), pd.DataFrame()
    scored["question"] = scored.output.str.split("/").str[0]
    checks = scored[scored.question == "check"]
    acc = accuracy_summary(checks, ["model", "setting", "check_relation", "block_order"])
    both = checks.dropna(subset=["correct"]).groupby(["model", "setting", "case_id"]).correct.agg(["min", "size"])
    both = both[both["size"] == 2].groupby(level=[0, 1])["min"].mean().rename("both_orders_right").reset_index()
    acc = acc.merge(both, on=["model", "setting"], how="left") if not acc.empty else acc

    probes = scored[scored.question == "role_check"].dropna(subset=["correct"])
    probes = probes.assign(swapped=probes.condition.str.startswith("probe_swap"))
    pairs = probes.pivot_table(index=["model", "setting", "case_id", "output"], columns="swapped", values="correct")
    rows = []
    if {True, False} <= set(pairs.columns):
        for (m, setting), g in pairs.groupby(level=[0, 1]):
            rows.append({"model": m, "setting": setting, "kind": "probe_pairs", "n": len(g),
                         "accuracy_original": g[False].mean(), "accuracy_swapped": g[True].mean(),
                         "both_right": (g[False].eq(1) & g[True].eq(1)).mean()})
    if "direct" in BL.methods_for(model, "ckd"):
        b = BL.read(model, "direct", "ckd")
        W = b[b.replicate == 0].pivot_table(index="case_id", columns="condition", values="value", aggfunc="first")
        for setting in SETTINGS:
            f = "_F" if setting == "false" else ""
            need = [f"C{f}", f"M2{f}", "D2", f"C_swap{f}"]
            if any(c not in W for c in need):
                continue
            w = W[need].dropna()
            gaps = {"C_minus_M2": w[f"C{f}"] - w[f"M2{f}"], "C_swap_minus_D2": w[f"C_swap{f}"] - w["D2"],
                    "D2_minus_M2": w["D2"] - w[f"M2{f}"], "C_swap_minus_C": w[f"C_swap{f}"] - w[f"C{f}"]}
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
    p = p[p.parameter.isin(["nominal_threshold", "implied_threshold"])]
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
    lat = lat[lat.replicate == 0][["case_id", "condition", "z_mean"]]
    mat = pd.read_parquet(mat_path)
    mat = mat[mat.usable & (mat.replicate == 0)]
    mat = mat.assign(decide=mat[["decision/normal", "decision/flip"]].mean(axis=1))[["case_id", "condition", "decide"]]
    both = lat.merge(mat, on=["case_id", "condition"])
    if both.empty:
        return pd.DataFrame()
    grid = np.linspace(*np.quantile(both.z_mean, [0.05, 0.95]), points)
    rng = np.random.default_rng(seed)

    def smooth(z: np.ndarray, y: np.ndarray) -> np.ndarray:
        k = np.exp(-0.5 * ((grid[:, None] - z[None, :]) / bandwidth) ** 2)
        return (k @ y) / k.sum(axis=1)

    rows = []
    for condition, g in both.groupby("condition"):
        z, y = g.z_mean.to_numpy(float), g.decide.to_numpy(float)
        draws = np.array([smooth(z[i], y[i]) for i in (rng.integers(0, len(z), len(z)) for _ in range(resamples))])
        lo, hi = np.nanpercentile(draws, [2.5, 97.5], axis=0)
        rows.append(pd.DataFrame({"model": model, "condition": condition, "z": grid, "p_decide": smooth(z, y),
                                  "ci_lower": lo, "ci_upper": hi, "n_cases": len(z)}))
    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------- bespoke
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
    return b[["model", "case_id", "family", "placebo", "condition", "role", "setting", "value", "sd", "n_samples",
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


# ---------------------------------------------------------------- report
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
        if r.statistic == "lambda_fieller" and r.interval != "bounded":
            out.append(f"Weak denominator: {label}: the lambda confidence set is {fmt(r)}.")
        if r.statistic in ("rho", "rho_pooled") and np.isfinite(r.estimate) and r.ci_lower <= 0:
            out.append(f"{r.statistic} interval includes 0: {label}: {fmt(r)}; the ratios over it are blank.")
        if r.statistic in ("curvature_D", "curvature_C") and sig:
            out.append(f"Affine check: {r.statistic} interval excludes 0: {label}: {fmt(r)}.")
        if r.statistic == "diff_phi_setting" and sig:
            out.append(f"phi differs between settings: {label}: {fmt(r)} (the shared-phi assumption).")
        if r.statistic == "manipulation_median_abs_gap" and r.estimate < 0.5:
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
             "items (bespoke); a blank ratio means its denominator's interval includes 0.  Bespoke placebo items "
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
                     "belief).  Reference `two_record` (CKD) uses C's matched layout, M2 and D2; `single_record` "
                     "uses M and D, as in the Methods.  lambda_fieller is the 95% Fieller confidence set.  rho = "
                     "Cov(x, x') / Var(x) from the second run; tau = (beta_D - beta_C) / rho; both use calibrated "
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
        ("direction", "CKD false records: sicker against healthier edits",
         "The 64 patients eligible for the randomized comparison; bootstrap within label x direction.",
         ["model", "method", "reference", "statistic", "estimate", "ci_lower", "ci_upper", "n_cases", "n_sicker"]),
        ("checks", "CKD comprehension checks", "Accuracy by setting, relation and record order; both_orders_right "
         "is the share of patients right in both option orders.", None),
        ("role_swap", "CKD role swaps", "Probe pairs: both_right is the share of pairs answered right in both "
         "ownership orientations (0 for a strategy that ignores ownership).  Gaps (direct beliefs, logits): "
         "C - M2 is the original leakage gap, C_swap - D2 the swapped one, D2 - M2 their reference.", None),
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
    names = ["estimates", "scatter", "direction", "checks", "role_swap", "placebo", "report_control", "bayes",
             "bespoke_points", "comprehension", "retention", "thresholds", "decision_curves"]
    collected: dict[str, list[pd.DataFrame]] = {n: [] for n in names}
    for model in models:
        print(f"curse analysis: {model}", flush=True)
        est, scat = estimates(model, args.resamples, seed)
        points = bespoke_points(model)
        acc, swap = check_tables(model) if "ckd" in C.datasets_for(model) else (pd.DataFrame(), pd.DataFrame())
        found = {"estimates": est, "scatter": scat, "checks": acc, "role_swap": swap, "bespoke_points": points,
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
