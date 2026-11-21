"""Curse-of-knowledge analysis: the Methods estimators per model, dataset, method and setting.  No model calls.

    python -m second_order.curse [--models qwen3_8b qwen3_14b] [--resamples 2000]

For each model, dataset (``ckd``, ``bespoke``) and belief method (``latent``
and ``direct`` for CKD, ``direct`` for bespoke), on the main analysis set:

* ``estimates.csv``: the leakage table (``leakage.leakage_table``) for the
  partial setting (pair S4) and the false-belief setting (pair F): lambda =
  beta_C / beta_D, rho, phi, tau, the affine checks, and the contrast
  ``F_minus_S4`` (differences and phi pooled across settings).  Case sets:
  ``all`` and ``comprehension_pass`` (CKD: cases whose comprehension answers
  are all correct; bespoke: C answers averaged over the samples whose
  follow-up was correct).  Bespoke placebo items are excluded here.
* ``placebo.csv``: bespoke placebo items: the gaps l_A - l_B, l_D - l_M and
  l_C - l_M per setting, with normal intervals from the sample standard errors.
* ``bayes.csv``: bespoke items: how the model's answers follow the Bayes
  answer (slope and intercept in logits, mean absolute error), for own
  beliefs and for beliefs about the agent (whose Bayes answer uses the
  agent's evidence; a perfect perspective-taker has lambda 0 and phi 1).
* ``comprehension.csv``: follow-up accuracy per model, dataset and condition.
* ``scatter.csv``: per case, x = l_A - l_B, l_C - l_M and l_D - l_M (figures).
* ``thresholds.csv``: CKD decision thresholds implied by each pooled fit (where
  P(decide) = 1/2, i.e. z = sigmoid(-a/b)) against the instructed ones, for the
  model's own family and PERSON's (the paper's Figure 5, per fit family).
* ``decision_curves.csv``: the plain screening decision smoothed against the
  all-output posterior mean z per condition (Nadaraya-Watson, Gaussian kernel,
  bandwidth 0.10, all complete cases, pointwise 95% case-bootstrap bands; the
  paper's Figure 3, per condition).
* ``bespoke_points.csv``: per item and condition, the mean stated logit, its
  standard error and the Bayes logit of the believer's evidence.
* ``RESULTS.md``: the tables, the Camerer et al. reference w, and anomalies.

Figures: ``python -m second_order.figures`` (``curse_*``).
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd

from second_order import beliefs as BL
from second_order import config as C
from second_order.report import display, md_table
from second_order.bespoke import bayes_answer, load as load_items, logit
from second_order.conditions import CONDITIONS, SETTING_OF, SETTINGS, pair_conditions
from second_order.leakage import leakage_table, pair_keys
from second_order.responses import comprehension_summary, responses_path, scored

OUT = C.RESULTS / "curse"
METHODS = ("latent", "direct")
HEADLINE = ["lambda", "beta_C", "beta_D", "rho", "phi", "tau", "T_C", "manipulation_median_abs_gap", "gap_sd"]
AFFINE = ["phi_D_on_A", "intercept_D_on_A", "phi_M_on_B", "intercept_M_on_B", "curvature_D", "curvature_C",
          "lambda_x_positive", "lambda_x_negative", "lambda_proportionality_low", "lambda_proportionality_mid",
          "lambda_proportionality_high", "anchor_mean_M_minus_B", "mean_position"]
CONTRAST = ["diff_lambda", "diff_phi", "diff_tau", "diff_rho", "phi_pooled", "rho_pooled", "beta_D_pooled"]


def models_default() -> list[str]:
    present = sorted(p.name for p in C.BELIEFS.iterdir() if p.is_dir()) if C.BELIEFS.exists() else []
    wanted = list(C.load()["experiment"]["models"])
    return [m for m in wanted if m in present] or present


def placebo_ids() -> set[str]:
    try:
        return {i.case_id for i in load_items() if i.placebo}
    except FileNotFoundError:
        return set()


def beliefs_for(model: str, dataset: str, method: str) -> pd.DataFrame:
    b = BL.read(model, method, dataset)
    if method == "latent":
        b = b[b.fit == "pooled"]
    return b[b.condition.isin(CONDITIONS)]


def comprehension_rows(model: str, dataset: str) -> pd.DataFrame:
    path = C.RESULTS / "comprehension" / f"{model}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    return frame[frame.dataset == dataset]


def comprehension_pass(model: str, dataset: str, b: pd.DataFrame) -> pd.DataFrame | None:
    """Beliefs with each C condition restricted to answers whose follow-up was answered correctly.

    Filtering is per C condition, so each setting loses only the cases (CKD) or samples
    (bespoke) that failed its own follow-up.  None if no follow-up has been answered.
    """
    comp = comprehension_rows(model, dataset)
    if comp.empty:
        return None
    comp = comp.assign(correct=scored(comp))
    if comp.correct.notna().sum() == 0:
        return None
    good = comp[comp.correct == 1]
    checked = comp.condition.unique()
    if dataset == "ckd":
        every = comp.dropna(subset=["correct"]).groupby(["case_id", "condition"]).correct.min()
        passed = set(every[every == 1].index)
        keep = ~b.condition.isin(checked) | pd.Series(list(zip(b.case_id, b.condition)), index=b.index).isin(passed)
        return b[keep]
    # bespoke: recompute the C means from the samples whose follow-up was correct
    rows = []
    for condition in checked:
        path = responses_path(model, dataset, condition)
        if not path.exists():
            continue
        long = pd.read_parquet(path)
        rows.append(long.merge(good[good.condition == condition][["case_id", "replicate", "sample"]],
                               on=["case_id", "replicate", "sample"]))
    if not rows:
        return None
    kept = BL.sample_mean_rows(pd.concat(rows, ignore_index=True))
    kept = kept[kept.case_id.isin(set(b.case_id))]
    kept["dataset"] = dataset
    return pd.concat([b[~b.condition.isin(checked)], kept[b.columns]], ignore_index=True)


def estimates(model: str, resamples: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    tables, scatter = [], []
    placebo = placebo_ids()
    for dataset in C.datasets_for(model):
        for method in [m for m in METHODS if m in BL.methods_for(model, dataset)]:
            b = beliefs_for(model, dataset, method)
            b = b[~b.case_id.isin(placebo)]
            sets = {"all": b}
            passed = comprehension_pass(model, dataset, b)
            if passed is not None:
                sets["comprehension_pass"] = passed
            for name, part in sets.items():
                t = leakage_table(part, method, resamples=resamples, seed=seed)
                if t.empty:
                    continue
                t = t[((t.framing == "john") & t.pair.isin(SETTINGS.values()))
                      | ((t.framing == "contrast") & (t.pair == "F_minus_S4"))].copy()
                t.insert(0, "model", model)
                t.insert(1, "dataset", dataset)
                t.insert(2, "method", method)
                t.insert(3, "case_set", name)
                t.insert(4, "setting", t.pair.map(lambda p: SETTING_OF.get(p, "false_minus_partial")))
                tables.append(t)
            main = b[(b.replicate == 0) & (b.half == "all")]
            W = main.pivot_table(index="case_id", columns="condition", values="value", aggfunc="first")
            for setting, pair in SETTINGS.items():
                k = pair_keys("john", pair, [f"{c}:" for c in W.columns])
                if any(k[r] not in W for r in "ABCMD"):
                    continue
                scatter.append(pd.DataFrame({
                    "model": model, "dataset": dataset, "method": method, "setting": setting, "case_id": W.index,
                    "x_A_minus_B": W[k["A"]] - W[k["B"]], "y_C_minus_M": W[k["C"]] - W[k["M"]],
                    "y_D_minus_M": W[k["D"]] - W[k["M"]]}).dropna())
    return (pd.concat(tables, ignore_index=True) if tables else pd.DataFrame(),
            pd.concat(scatter, ignore_index=True) if scatter else pd.DataFrame())


def placebo_table(model: str) -> pd.DataFrame:
    if "direct" not in BL.methods_for(model, "bespoke"):
        return pd.DataFrame()
    b = beliefs_for(model, "bespoke", "direct")
    b = b[b.case_id.isin(placebo_ids()) & (b.replicate == 0)].set_index(["case_id", "condition"])
    rows = []
    for case_id in sorted({c for c, _ in b.index}):
        for setting, pair in SETTINGS.items():
            k = pair_conditions("john", pair)
            for name, (first, second) in {"A_minus_B": ("A", k["B"]), "D_minus_M": (k["D"], k["M"]),
                                          "C_minus_M": (k["C"], k["M"])}.items():
                if (case_id, first) not in b.index or (case_id, second) not in b.index:
                    continue
                r1, r2 = b.loc[(case_id, first)], b.loc[(case_id, second)]
                gap, se = r1.value - r2.value, float(np.hypot(r1.sd, r2.sd))
                rows.append({"model": model, "case_id": case_id, "setting": setting, "gap": name,
                             "estimate": gap, "se": se, "ci_lower": gap - 1.96 * se, "ci_upper": gap + 1.96 * se,
                             "n_samples": int(min(r1.n_samples, r2.n_samples))})
    return pd.DataFrame(rows)


def bayes_table(model: str) -> pd.DataFrame:
    if "direct" not in BL.methods_for(model, "bespoke"):
        return pd.DataFrame()
    b = beliefs_for(model, "bespoke", "direct")
    b = b[(b.replicate == 0) & ~b.case_id.isin(placebo_ids())].copy()
    b["bayes_logit"] = [logit(bayes_answer(c, k)) for c, k in zip(b.case_id, b.condition)]
    b["about"] = b.condition.map(lambda c: CONDITIONS[c].about)
    groups = [("own", "all", b[b.about == "self"]), ("agent", "all", b[b.about == "agent"])]
    groups += [("own" if CONDITIONS[c].about == "self" else "agent", c, b[b.condition == c])
               for c in sorted(b.condition.unique())]
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


DECISION_CURVE_CONDITIONS = ("A", "D_john", "M_S4", "C_john", "B", "D_F", "M_F", "C_F", "B_F")


def decision_kind(output: str) -> str:
    if "no_threshold" in output:
        return "plain"
    if "bet_payoff" in output:
        return "bet"
    return "cost" if "cost_fn" in output else "threshold"


def threshold_table(model: str) -> pd.DataFrame:
    """Implied vs instructed decision thresholds per pooled fit family (CKD)."""
    path = C.LATENTS / f"{model}_parameters.parquet"
    if not path.exists():
        return pd.DataFrame()
    p = pd.read_parquet(path)
    p = p[(p.fit == "pooled") & p.parameter.isin(["nominal_threshold", "implied_threshold"])]
    w = p.pivot_table(index=["fit_group", "output_name"], columns="parameter", values="value",
                      aggfunc="first").reset_index()
    w["family"] = w.fit_group.map(lambda g: CONDITIONS[g.split("+")[0]].framing)
    w["kind"] = w.output_name.map(decision_kind)
    w.insert(0, "model", model)
    return w


def decision_curves(model: str, *, bandwidth: float = 0.10, points: int = 40, resamples: int = 200,
                    seed: int = 0) -> pd.DataFrame:
    """Smoothed P(plain screening decision) against posterior mean z, per condition (CKD)."""
    from second_order.responses import DATASET_COLUMN

    lat_path, mat_path = C.LATENTS / f"{model}.parquet", C.LATENTS / f"{model}_matrix.parquet"
    if not lat_path.exists() or not mat_path.exists():
        return pd.DataFrame()
    lat = pd.read_parquet(lat_path)
    lat = lat[(lat.fit == "pooled") & (lat.replicate == 0)][["case_id", "condition", "z_mean_all"]]
    mat = pd.read_parquet(mat_path)
    column = DATASET_COLUMN["decision_no_threshold"]
    mat = mat[mat.complete & (mat.replicate == 0)][["case_id", "condition", column]]
    both = lat.merge(mat, on=["case_id", "condition"])
    both = both[both.condition.isin(DECISION_CURVE_CONDITIONS)]
    if both.empty:
        return pd.DataFrame()
    grid = np.linspace(*np.quantile(both.z_mean_all, [0.05, 0.95]), points)
    rng = np.random.default_rng(seed)

    def smooth(z: np.ndarray, y: np.ndarray) -> np.ndarray:
        k = np.exp(-0.5 * ((grid[:, None] - z[None, :]) / bandwidth) ** 2)
        return (k @ y) / k.sum(axis=1)

    rows = []
    for condition, g in both.groupby("condition"):
        z, y = g.z_mean_all.to_numpy(float), g[column].to_numpy(float)
        draws = np.array([smooth(z[i], y[i]) for i in (rng.integers(0, len(z), len(z)) for _ in range(resamples))])
        lo, hi = np.nanpercentile(draws, [2.5, 97.5], axis=0)
        rows.append(pd.DataFrame({"model": model, "condition": condition, "z": grid, "p_decide": smooth(z, y),
                                  "ci_lower": lo, "ci_upper": hi, "n_cases": len(z)}))
    return pd.concat(rows, ignore_index=True)


def bespoke_points(model: str) -> pd.DataFrame:
    """Per bespoke item and condition: mean stated logit, its standard error and the Bayes logit."""
    if "direct" not in BL.methods_for(model, "bespoke"):
        return pd.DataFrame()
    b = beliefs_for(model, "bespoke", "direct")
    b = b[b.replicate == 0].copy()
    items = {i.case_id: i for i in load_items()}
    b["bayes_logit"] = [logit(bayes_answer(c, k)) for c, k in zip(b.case_id, b.condition)]
    b["family"] = b.case_id.map(lambda c: items[c].family)
    b["placebo"] = b.case_id.map(lambda c: items[c].placebo)
    b["role"] = b.condition.map(lambda c: CONDITIONS[c].role)
    b["setting"] = b.condition.map(lambda c: SETTING_OF.get(CONDITIONS[c].pair, "both"))
    return b[["model", "case_id", "family", "placebo", "condition", "role", "setting", "value", "sd", "n_samples",
              "bayes_logit"]]


# ---------------------------------------------------------------- report
def blank(r) -> str:
    return r.blank_reason if isinstance(r.blank_reason, str) else ""


def fmt(r) -> str:
    if r is None or not np.isfinite(r.estimate):
        return "" if r is None else f"blank ({blank(r)})" if blank(r) else ""
    return f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}]"


def headline_md(est: pd.DataFrame, stats: list[str]) -> str:
    rows = []
    for (model, dataset, method, setting), g in est[est.case_set == "all"].groupby(
            ["model", "dataset", "method", "setting"], sort=False):
        g = g.set_index("statistic")
        row = {"model": display(model), "dataset": dataset, "method": method, "setting": setting,
               "n": int(g.n_cases.iloc[0])}
        for s in stats:
            row[s] = fmt(g.loc[s]) if s in g.index else ""
        rows.append(row)
    return md_table(pd.DataFrame(rows), ["model", "dataset", "method", "setting", "n"] + stats) if rows \
        else "_(no rows)_\n"


def anomalies(est: pd.DataFrame, placebo: pd.DataFrame, comp: pd.DataFrame, retention: pd.DataFrame) -> list[str]:
    out = []
    main = est[est.case_set == "all"] if not est.empty else est
    for _, r in main.iterrows():
        label = f"{r.model} {r.dataset} {r.method} {r.setting}"
        if r.statistic == "rho" and np.isfinite(r.estimate) and r.ci_lower <= 0:
            out.append(f"rho interval includes 0: {label}: {fmt(r)}; phi and tau are blank.")
        if r.statistic in ("curvature_D", "curvature_C") and np.isfinite(r.estimate) and (r.ci_lower > 0 or r.ci_upper < 0):
            out.append(f"Affine check: {r.statistic} interval excludes 0: {label}: {fmt(r)}.")
        if r.statistic == "diff_phi" and np.isfinite(r.estimate) and (r.ci_lower > 0 or r.ci_upper < 0):
            out.append(f"phi differs between settings: {label}: {fmt(r)} (the shared-phi assumption).")
        if r.statistic == "manipulation_median_abs_gap" and r.estimate < 0.5:
            out.append(f"Manipulation check below 0.5 logits: {label}: median |l_A - l_B| = {r.estimate:.3f}.")
        if blank(r) and r.statistic in HEADLINE:
            out.append(f"Ratio rule: {label}: {r.statistic} left blank ({blank(r)}).")
    by = main.set_index(["model", "dataset", "method", "setting", "statistic"]) if not main.empty else None
    if by is not None:
        for (model, dataset, method, setting), _ in main.groupby(["model", "dataset", "method", "setting"]):
            key = (model, dataset, method, setting)
            if key + ("phi",) in by.index and key + ("phi_D_on_A",) in by.index:
                phi, check = by.loc[key + ("phi",)], by.loc[key + ("phi_D_on_A",)]
                if np.isfinite(phi.estimate) and np.isfinite(check.estimate) and not (
                        phi.ci_lower <= check.estimate <= phi.ci_upper):
                    out.append(f"Affine check: phi from the gap ({fmt(phi)}) and from l_D on l_A "
                               f"({check.estimate:.3f}) disagree: {model} {dataset} {method} {setting}.")
    for _, r in placebo.iterrows():
        if r.ci_lower > 0 or r.ci_upper < 0:
            out.append(f"Placebo gap away from 0: {r.model} {r.case_id} {r.setting} {r.gap}: "
                       f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}].")
    for _, r in comp.iterrows():
        if r.accuracy < 0.9:
            out.append(f"Comprehension accuracy below 90%: {r.model} {r.dataset} {r.condition}: {r.accuracy:.1%}.")
    for _, r in retention.iterrows():
        if r.retention < 0.9:
            out.append(f"Retention below 90%: {r.model} {r.get('dataset', 'ckd')} {r.condition}: {r.retention:.1%}.")
    return out


def write_results_md(models, est, placebo, bayes, comp, retention) -> None:
    w = float(C.load()["experiment"]["camerer_w"])
    parts = [f"# Curse of knowledge: results\n\nGenerated {dt.date.today().isoformat()} by "
             "`python -m second_order.curse`; figures by `python -m second_order.figures`.  Numbers only; no "
             "interpretation.  Estimates with 95% paired case-bootstrap percentile intervals; a blank ratio means "
             "its denominator's interval includes 0.  Bespoke placebo items are excluded from the estimates.\n\n"
             f"Models: {', '.join(display(m) for m in models)}.  Reference: Camerer, Loewenstein and Weber (1989) "
             f"find w ≈ {w} for people (a weight on dollar values, not on log-odds).\n\n"]
    if est.empty:
        parts.append("_(no estimates: run generation and `second_order.phase3` first)_\n")
    else:
        parts.append("## Leakage, responsiveness and theory of mind\n\nlambda = beta_C / beta_D (0: no leakage, 1: "
                     "the agent is given the model's own belief); rho = Cov(x, x') / Var(x) from the second run; "
                     "phi = beta_D / rho; tau = (beta_D - beta_C) / rho = phi (1 - lambda).  rho, phi and tau use "
                     "calibrated values (for latent beliefs, the posterior means de-shrunk per fit; the slopes "
                     "they use are beta_D_calibrated and beta_C_calibrated), so for latent rows phi is not "
                     "beta_D / rho with the raw beta_D shown; lambda is the same either way.\n\n"
                     + headline_md(est, ["lambda", "phi", "rho", "tau", "beta_C", "beta_D", "beta_C_calibrated",
                                         "beta_D_calibrated"]) + "\n")
        passed = est[est.case_set == "comprehension_pass"]
        if not passed.empty:
            parts.append("### Answers whose comprehension follow-up was correct\n\n"
                         + headline_md(passed.assign(case_set="all"), ["lambda", "phi", "tau"]) + "\n")
        contrast = est[(est.setting == "false_minus_partial") & (est.case_set == "all")]
        parts.append("### False belief against partial evidence\n\n"
                     + headline_md(contrast, [s for s in CONTRAST if s in set(contrast.statistic)]) + "\n")
        parts.append("## Checks of the modelling assumptions\n\nAffine form: phi_D_on_A and phi_M_on_B estimate "
                     "phi from levels (instrumented by the second run); curvature_* are quadratic terms of the gap "
                     "regressions.  Partial linearity: lambda by the sign of the gap and by thirds of |x|.\n\n"
                     + headline_md(est[est.setting != "false_minus_partial"],
                                   [s for s in AFFINE if s in set(est.statistic)]) + "\n")
    if not placebo.empty:
        parts.append("## Bespoke placebo items\n\nAll three gaps should be near 0.\n\n"
                     + md_table(placebo.assign(model=placebo.model.map(display)),
                                ["model", "case_id", "setting", "gap", "estimate", "ci_lower", "ci_upper",
                                 "n_samples"]) + "\n")
    if not bayes.empty:
        parts.append("## Bespoke items against the Bayes answer\n\nSlope and intercept of the model's mean logit on "
                     "the Bayes logit of the believer's evidence.\n\n"
                     + md_table(bayes.assign(model=bayes.model.map(display)),
                                ["model", "beliefs", "condition", "n", "slope_on_bayes", "intercept",
                                 "mean_abs_error_logit", "corr"]) + "\n")
    if not comp.empty:
        parts.append("## Comprehension follow-ups\n\n" + md_table(comp, list(comp.columns)) + "\n")
    if not retention.empty:
        parts.append("## Retention\n\n" + md_table(retention, [c for c in ["model", "dataset", "condition",
                     "cases_generated", "cases_complete", "retention", "parse_failures", "call_errors",
                     "refusal_like_responses"] if c in retention]) + "\n")
    parts.append("## Anomalies\n\n" + "".join(f"- {a}\n" for a in anomalies(est, placebo, comp, retention)) + "\n")
    (OUT / "RESULTS.md").write_text("".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--resamples", type=int, default=int(C.load()["analysis"]["bootstrap_resamples"]))
    args = parser.parse_args()
    seed = int(C.load()["seed"])
    models = args.models or models_default()
    OUT.mkdir(parents=True, exist_ok=True)
    est, scat, plac, bay, comp, ret, thr, curves, points = [], [], [], [], [], [], [], [], []
    for model in models:
        print(f"curse analysis: {model}", flush=True)
        e, s = estimates(model, args.resamples, seed)
        est.append(e), scat.append(s), plac.append(placebo_table(model)), bay.append(bayes_table(model))
        thr.append(threshold_table(model)), points.append(bespoke_points(model))
        curves.append(decision_curves(model, resamples=min(args.resamples, 200), seed=seed))
        for dataset in C.datasets_for(model):
            rows = comprehension_rows(model, dataset)
            if not rows.empty:
                comp.append(comprehension_summary(rows))
        path = C.RESULTS / "retention" / f"{model}.csv"
        if path.exists():
            ret.append(pd.read_csv(path))
    cat = lambda fs: pd.concat([f for f in fs if not f.empty], ignore_index=True) if any(  # noqa: E731
        not f.empty for f in fs) else pd.DataFrame()
    est, scat, plac, bay, comp, ret, thr, curves, points = map(
        cat, (est, scat, plac, bay, comp, ret, thr, curves, points))
    for name, frame in (("estimates", est), ("scatter", scat), ("placebo", plac), ("bayes", bay),
                        ("comprehension", comp), ("retention", ret), ("thresholds", thr),
                        ("decision_curves", curves), ("bespoke_points", points)):
        frame.to_csv(OUT / f"{name}.csv", index=False)
    write_results_md(models, est, plac, bay, comp, ret)
    print(f"wrote {OUT / 'RESULTS.md'}")


if __name__ == "__main__":
    main()
