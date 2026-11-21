"""Phase 5 analysis from the shared belief format, organised by research question.  No model calls.

    python -m second_order.deprecated.analyze [--models ...] [--resamples 2000]

RQ1 (separation): ``results/leakage.csv``: lambda, kappa, tau and their checks per
  model, method, framing and pair, plus the contrasts F - H (RQ1c) and user - John (RQ1d).
RQ2 (coherence and the ladder): ``results/coherence.csv``; ladder rows of ``leakage.csv``.
RQ3 (internal): the ``probe`` rows of ``leakage.csv`` (outcomes are probe readings, the
  regressor is the behavioural latent gap).
Also ``results/{agreement,retention,scatter_points}.csv``, ``results/disagreements/{model}.md``
and ``results/RESULTS.md`` (tables, figure references, anomalies; no interpretation).
Figures: ``python -m second_order.deprecated.rq_figures``.
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from second_order import beliefs as BL
from second_order import config as C
from second_order.report import display, md_table
from second_order.deprecated.coherence import coherence_table, latent_predictions, link_predictions
from second_order.conditions import CONDITIONS, framing_pairs, pair_conditions
from second_order.leakage import leakage_table
from second_order.prompts_second_order import prompt_for
from second_order.run_generation import pilot_cases

GEMMA_PARAMS = {"gemma3_4b": 4, "gemma3_12b": 12, "gemma3_27b": 27, "gemma3_4b_mlx": 4, "gemma3_12b_mlx": 12}
OPEN_WEIGHT = {"gemma3_4b", "gemma3_12b", "gemma3_27b", "llama31_70b", "qwen3_32b", "deepseek_v32",
               "glm5", "gemma3_4b_mlx", "gemma3_12b_mlx"}


def selected_mask(frame: pd.DataFrame) -> pd.Series:
    if "selected_layer" not in frame:
        return pd.Series(True, index=frame.index)
    return frame["selected_layer"].map(lambda v: True if pd.isna(v) else bool(v))


def research_question(framing: str, pair: str) -> str:
    """Which RQ a (framing, pair) row of the leakage table serves first."""
    if framing == "contrast":
        return {"F_minus_H": "RQ1c", "user_minus_john": "RQ1d", "F_minus_S4": "curse"}.get(pair, "RQ2")
    if framing == "user":
        return "RQ1d"
    return {"S4": "RQ1a/b", "F": "RQ1c", "H": "RQ1c", "S1": "RQ2"}[pair]


def models_with_beliefs() -> list[str]:
    return sorted(p.name for p in C.BELIEFS.iterdir() if p.is_dir()) if C.BELIEFS.exists() else []


def wide_for(model: str) -> pd.DataFrame:
    path = C.LATENTS / f"{model}_matrix.parquet"
    if path.exists():
        frame = pd.read_parquet(path)
        return frame[frame.replicate == 0] if "replicate" in frame else frame  # main run only
    first = pd.read_csv(C.FIRST_ORDER_DATASET)
    first = first[first.model_key == C.split_source(model)].rename(columns={"source_prompt_id": "case_id"})
    first["condition"], first["complete"], first["framing"], first["model"] = "A", True, "self", model
    return first


def capability(model: str, coherence: pd.DataFrame) -> float:
    base = pd.read_csv(C.DOCS / "baseline_ckd.csv")
    row = base[(base.model_key == model) & (base.condition == "reports_to_decisions")]
    if len(row):
        return float(row.mean_c_index.iloc[0])
    own = coherence[(coherence.model == model) & (coherence.method == "latent") & (coherence.condition == "A")
                    & (coherence.direction == "reports_to_decisions") & (coherence.statistic == "mean_c_index")]
    return float(own.estimate.iloc[0]) if len(own) else np.nan


def analysis_set(model: str, b: pd.DataFrame, conditions) -> list[str]:
    lat = C.LATENTS / f"{model}.parquet"
    present = [c for c in conditions if c in set(b.condition)]
    if lat.exists():
        l = pd.read_parquet(lat)
        l = l[(l.fit == "pooled") & (l.replicate == 0 if "replicate" in l else True)]  # main run only
        counts = l[l.condition.isin(present)].groupby("case_id").condition.nunique()
        return sorted(counts[counts == len(present)].index)
    counts = b[b.condition.isin(present)].groupby("case_id").condition.nunique()
    return sorted(counts[counts == len(present)].index)


def selected_layers(model: str) -> pd.DataFrame:
    path = C.RESULTS / "probes" / model / "selected_layers.csv"
    return pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["position", "probe_type", "layer"])


def probe_variants(model: str, b: pd.DataFrame, all_layers: bool):
    sel = selected_layers(model)
    for _, s in sel.iterrows():
        if s.position == "P1":
            continue
        yield (s.probe_type, s.position, int(s.layer), True,
               b[(b.probe_type == s.probe_type) & (b.position == s.position) & (b.layer == s.layer)])
    if all_layers:
        for (ptype, pos, layer), part in b[b.probe_type == "truth"].groupby(["probe_type", "position", "layer"]):
            yield ptype, pos, int(layer), False, part


def run_leakage(model: str, resamples: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Leakage table for every method, and scatter points for the latent method."""
    rows, scatter = [], []
    pilots = set(pilot_cases())
    methods = [m for m in BL.methods_for(model) if m in ("latent", "direct", "probe")]
    latent = BL.read(model, "latent") if "latent" in methods else None
    behavioural = latent[latent.fit == "pooled"] if latent is not None else None
    for method in methods:
        b = BL.read(model, method)
        b = b[b.condition.isin(CONDITIONS)]
        variants = []  # (metadata, beliefs, regressor, calibrated outcomes)
        if method == "latent":
            variants.append(({"fit": "pooled", "outcome_values": "raw"}, b[b.fit == "pooled"], None, False))
            # Required checks (stop point 5): calibrated outcomes, and per-condition fits.
            variants.append(({"fit": "pooled", "outcome_values": "calibrated"}, b[b.fit == "pooled"], None, True))
            variants.append(({"fit": "separate", "outcome_values": "calibrated"}, b[b.fit == "separate"], None, True))
        elif method == "direct":
            variants.append(({"fit": "none", "outcome_values": "raw"}, b, None, False))
        elif behavioural is not None:
            for ptype, pos, layer, is_sel, part in probe_variants(model, b, all_layers=True):
                variants.append(({"fit": "frozen", "outcome_values": "raw", "probe_type": ptype, "position": pos,
                                  "layer": layer, "selected_layer": is_sel}, part, behavioural, False))
        for meta, part, regressor, calibrated in variants:
            if part.empty:
                continue
            headline = meta.get("selected_layer", True) and meta["fit"] != "separate" and not calibrated
            split = part.drop_duplicates("case_id").set_index("case_id").split
            ids = set(part.case_id)
            sets = {"all_complete": ids}
            if headline:
                sets["test_only"] = {c for c in ids if split.get(c) == "test"}
                sets["pilot_excluded"] = ids - pilots
            for set_name, keep in sets.items():
                n_res = resamples if meta.get("selected_layer", True) else min(resamples, 500)
                t = leakage_table(part[part.case_id.isin(keep)], method, resamples=n_res, seed=seed,
                                  regressor=regressor, calibrated=calibrated)
                if t.empty:
                    continue
                t.insert(0, "model", model)
                t.insert(1, "method", method)
                t.insert(2, "rq", [research_question(f, p) for f, p in zip(t.framing, t.pair)])
                t.insert(3, "case_set", set_name)
                for k, v in meta.items():
                    t[k] = v
                rows.append(t)
            if method == "latent" and headline:
                W = part[part.replicate == 0].pivot_table(index="case_id", columns=["condition", "half"],
                                                          values="value")
                for framing, pair in framing_pairs():
                    k = pair_conditions(framing, pair)
                    if (k["D"], "all") not in W:  # runs made before the pair's own D existed
                        k = pair_conditions(framing, pair, shared_d=True)
                    need = [k["A"], k["B"], k["C"], k["D"]]
                    if any((c, "all") not in W for c in need):
                        continue
                    frame = pd.DataFrame({
                        "model": model, "framing": framing, "pair": pair, "case_id": W.index,
                        "x_A_minus_B": W[(k["A"], "all")] - W[(k["B"], "all")],
                        "y_D_minus_C": W[(k["D"], "all")] - W[(k["C"], "all")],
                        "y_D_minus_M": (W[(k["D"], "all")] - W[(k["M"], "all")]
                                        if k["M"] and (k["M"], "all") in W else np.nan)})
                    scatter.append(frame.dropna(subset=["x_A_minus_B", "y_D_minus_C"]))
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(),
            pd.concat(scatter, ignore_index=True) if scatter else pd.DataFrame())


def run_coherence(model: str, resamples: int, seed: int) -> pd.DataFrame:
    wide = wide_for(model)
    out = []
    methods = BL.methods_for(model)
    conditions = [c for c in wide.condition.unique()]
    for method in [m for m in methods if m in ("latent", "direct", "probe")]:
        b = BL.read(model, method)
        b = b[b.replicate == 0]
        # The paired bootstrap resamples the same cases in every condition it predicts, so the
        # case set is the cases complete in all of them (not only the core conditions).
        cases_all = analysis_set(model, b, conditions)
        if method == "latent":
            params_path = C.LATENTS / f"{model}_parameters.parquet"
            if not params_path.exists():
                continue
            test = sorted(set(wide[(wide.split == "test")].case_id) & set(cases_all))
            preds = latent_predictions(wide[wide.case_id.isin(test)], pd.read_parquet(params_path), test)
            out.append(coherence_table(preds["predictions"], method="latent", model=model, n_cases=len(test),
                                       resamples=resamples, seed=seed))
            continue
        variants = [("direct", b)] if method == "direct" else [
            (f"probe_{t}_{p}", part) for t, p, _, _, part in probe_variants(model, b, all_layers=False)]
        for name, part in variants:
            part = part[(part.half == "all") & part.condition.isin(conditions)]
            ids = analysis_set(model, part, sorted(set(part.condition)))
            test = sorted(set(wide[(wide.split == "test")].case_id) & set(ids))
            preds = link_predictions(part, wide, test)
            t = coherence_table(preds["predictions"], method=method, model=model, n_cases=len(test),
                                resamples=resamples, seed=seed)
            t["variant"] = name
            out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def run_agreement(models: list[str], leakage: pd.DataFrame, coherence: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for model in models:
        methods = {}
        for method in BL.methods_for(model):
            if method not in ("latent", "direct", "probe"):
                continue
            b = BL.read(model, method)
            b = b[(b.half == "all") & (b.replicate == 0)]
            if method == "latent":
                methods["latent"] = b[b.fit == "pooled"]
            elif method == "direct":
                methods["direct"] = b
            else:
                for t, p, _, _, part in probe_variants(model, b, all_layers=False):
                    if t == "truth":
                        methods[f"probe_{p}"] = part
        names = sorted(methods)
        for i, m1 in enumerate(names):
            for m2 in names[i + 1:]:
                for cond in CONDITIONS:
                    a = methods[m1][methods[m1].condition == cond].set_index("case_id").value
                    c = methods[m2][methods[m2].condition == cond].set_index("case_id").value
                    ids = a.index.intersection(c.index)
                    if len(ids) < 5:
                        continue
                    rho = spearmanr(a.loc[ids], c.loc[ids]).statistic
                    rows.append({"model": model, "analysis": "spearman_between_methods", "method": f"{m1}~{m2}",
                                 "framing": CONDITIONS[cond].framing, "condition": cond,
                                 "estimate": rho, "n_cases": len(ids)})
        write_disagreements(model, methods)
    # sign agreement of contrasts across methods
    L = headline_rows(leakage)
    for model in models:
        signs = {}
        if not coherence.empty:
            co = coherence[(coherence.model == model) & (coherence.statistic == "mean_c_index")
                           & (coherence.direction == "reports_to_decisions")]
            for _, r in co[co.condition.isin(["A_minus_D", "D_minus_C", "M_minus_C"])].iterrows():
                key = f"{r.condition}_{r.framing}"
                signs.setdefault(key, {})[r.get("variant") if isinstance(r.get("variant"), str) else r.method] = np.sign(r.estimate)
        if not L.empty:
            lm = L[(L.model == model) & (L.framing == "contrast")
                   & L.statistic.isin(["diff_lambda", "diff_T_C"]) & L.pair.isin(["F_minus_H", "user_minus_john"])]
            for _, r in lm.iterrows():
                label = r.method if r.method != "probe" else f"probe_{r.probe_type}_{r.position}"
                if np.isfinite(r.estimate):
                    signs.setdefault(f"{r.pair}_{r.statistic}", {})[label] = np.sign(r.estimate)
        for contrast, by_method in signs.items():
            vals = set(by_method.values())
            rows.append({"model": model, "analysis": "sign_agreement", "method": "|".join(sorted(by_method)),
                         "framing": "", "condition": contrast, "estimate": float(len(vals) == 1),
                         "detail": "; ".join(f"{k}:{int(v)}" for k, v in sorted(by_method.items()))})
    # RQ1b: capability trend of lambda (S4, John) per method; descriptive, no significance claim
    if not L.empty:
        cap = {m: capability(m, coherence) for m in models}
        floor = float(C.load()["analysis"]["capability_floor"])
        g_all = L[(L.framing == "john") & (L.pair == "S4") & L.statistic.isin(["lambda", "T_C", "T_M"])]
        for (method, stat), g in g_all.groupby(["method", "statistic"]):
            g = g.drop_duplicates("model")
            g = g[g.model.map(lambda m: cap.get(m, np.nan) >= floor) & g.boot_median.notna()]
            if g.model.nunique() >= 3:
                rho = spearmanr(g.model.map(cap), g.boot_median).statistic
                rows.append({"model": "all", "analysis": "capability_spearman", "method": method,
                             "framing": "john", "condition": f"S4 {stat}", "estimate": rho,
                             "n_cases": int(g.model.nunique())})
    return pd.DataFrame(rows)


def headline_rows(leakage: pd.DataFrame) -> pd.DataFrame:
    """Primary rows: all complete cases, pooled fit with raw outcomes, selected probe layers."""
    if leakage.empty:
        return leakage
    L = leakage[(leakage.case_set == "all_complete") & selected_mask(leakage)]
    return L[(L.method != "latent") | ((L.fit == "pooled") & (L.outcome_values == "raw"))]


def write_disagreements(model: str, methods: dict[str, pd.DataFrame]) -> None:
    if len(methods) < 2:
        return
    n = int(C.load()["analysis"]["disagreements_per_model"])
    folder = C.RESULTS / "disagreements"
    folder.mkdir(parents=True, exist_ok=True)
    resp = {}
    c_conditions = [c for c in CONDITIONS if CONDITIONS[c].role == "C"]
    for cond in c_conditions:
        path = C.RESPONSES / model / f"{cond}.parquet"
        if path.exists():
            resp[cond] = pd.read_parquet(path)
    lines = [f"# {display(model)}: cases where the methods disagree most in condition C\n",
             "Disagreement = largest pairwise gap between methods after standardising each method's "
             "beliefs within the condition (z-scores).\n"]
    for cond in c_conditions:
        z = {}
        for name, part in methods.items():
            v = part[part.condition == cond].set_index("case_id").value
            if len(v) > 2:
                z[name] = (v - v.mean()) / v.std()
        if len(z) < 2:
            continue
        Z = pd.DataFrame(z).dropna()
        gap = Z.max(axis=1) - Z.min(axis=1)
        for case_id in gap.sort_values(ascending=False).index[:n]:
            lines.append(f"\n## {cond} / {case_id}: gap {gap[case_id]:.2f}\n")
            lines.append("| " + " | ".join(Z.columns) + " |\n|" + "---|" * len(Z.columns) + "\n| "
                         + " | ".join(f"{Z.loc[case_id, c]:.2f}" for c in Z.columns) + " |\n")
            lines.append(f"\nPrompt (numeric probability):\n\n```\n{prompt_for(model, case_id, cond, 'probability')}```\n")
            if cond in resp:
                r = resp[cond][resp[cond].case_id == case_id]
                for _, row in r.iterrows():
                    if row.output in ("probability", "decision_no_threshold"):
                        lines.append(f"\nResponse ({row.output}, parsed {row.value}):\n\n```\n{row.text}\n```\n")
                lines.append("\nParsed values: " + ", ".join(f"{o}={v}" for o, v in zip(r.output, r.value)) + "\n")
    (folder / f"{model}.md").write_text("".join(lines))


def anomalies(models, leakage, coherence, retention) -> list[str]:
    out = []
    for _, r in retention.iterrows():
        if r.retention < 0.9:
            out.append(f"Retention below 90%: {r.model} {r.condition}: {r.cases_complete}/{r.cases_generated} "
                       f"({r.retention:.1%}); parse failures {r.parse_failures}, call errors {r.call_errors}.")
        if r.refusal_like_responses:
            out.append(f"Refusal-like responses: {r.model} {r.condition}: {r.refusal_like_responses}.")
    for model in models:
        fs = C.RESULTS / "fits" / f"{model}_fit_summary.csv"
        if fs.exists():
            f = pd.read_csv(fs)
            for _, r in f[f.hit_epoch_limit].iterrows():
                out.append(f"Fit hit the epoch limit: {model} {r.fit} {r.conditions}.")
        ac = C.RESULTS / "fits" / f"{model}_anchor_check.csv"
        if ac.exists():
            a = pd.read_csv(ac)
            for _, r in a.iterrows():
                if abs(r.mean_numeric_probability - r.mean_posterior_z) > 0.05:
                    out.append(f"Anchor check: {model} {r.fit} {r.condition}: mean numeric report "
                               f"{r.mean_numeric_probability:.3f} vs mean posterior z {r.mean_posterior_z:.3f}.")
    L = headline_rows(leakage)
    if not L.empty:
        def label(r) -> str:
            return f"{r.model} {r.method} {r.framing}/{r.pair} {_variant(r)}"

        for _, r in L[(L.statistic == "manipulation_median_abs_gap") & (L.method == "latent")].iterrows():
            if r.estimate < 0.5:
                out.append(f"Manipulation check below 0.5 logits: {label(r)}: median |l_A - l_B| = {r.estimate:.3f}.")
        for _, r in L[L.blank_reason.fillna("") != ""].iterrows():
            out.append(f"Ratio rule: {label(r)}: {r.statistic} left blank ({r.blank_reason}).")
        for _, r in L[L.statistic == "kappa_halves"].iterrows():
            if np.isfinite(r.estimate) and not 0.7 <= r.estimate <= 1.3:
                out.append(f"kappa_halves outside [0.7, 1.3]: {label(r)}: {r.estimate:.3f} "
                           f"[{r.ci_lower:.3f}, {r.ci_upper:.3f}]. Answers about the agent follow evidence "
                           "differently from the model's own answers even with nothing hidden.")
        for _, r in L[L.statistic == "anchor_mean_M_minus_B"].iterrows():
            if abs(r.estimate) > 0.3 and r.method != "probe":
                out.append(f"Anchor check: {label(r)}: mean(l_M - l_B) = {r.estimate:.3f} logits (beyond 0.3).")
        for _, r in L[L.statistic == "lambda_0"].iterrows():
            if not (r.ci_lower <= 0 <= r.ci_upper):
                out.append(f"Noise check lambda_0 interval excludes 0: {label(r)}: {r.estimate:.3f} "
                           f"[{r.ci_lower:.3f}, {r.ci_upper:.3f}] (affects kappa and tau, not lambda).")
        # stop point 5, criterion 2: raw vs calibrated vs per-condition fits
        lat = leakage[(leakage.method == "latent") & (leakage.case_set == "all_complete")
                      & (leakage.statistic == "lambda")]
        for (model, framing, pair), g in lat.groupby(["model", "framing", "pair"]):
            head = g[(g.fit == "pooled") & (g.outcome_values == "raw")]
            if head.empty or not np.isfinite(head.estimate.iloc[0]):
                continue
            h = head.iloc[0]
            for _, r in g[g.outcome_values == "calibrated"].iterrows():
                if np.isfinite(r.estimate) and not (h.ci_lower <= r.estimate <= h.ci_upper):
                    out.append(f"lambda differs between fits: {model} {framing}/{pair}: pooled raw {h.estimate:.3f} "
                               f"[{h.ci_lower:.3f}, {h.ci_upper:.3f}], {r.fit} calibrated {r.estimate:.3f}.")
    return out


def _variant(r) -> str:
    if r.method == "probe":
        return f"(probe {r.probe_type} {r.position} L{int(r.layer)})"
    return f"(fit {r.fit})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--resamples", type=int, default=int(C.load()["analysis"]["bootstrap_resamples"]))
    args = parser.parse_args()
    seed = int(C.load()["seed"])
    models = args.models or models_with_beliefs()
    C.RESULTS.mkdir(parents=True, exist_ok=True)
    leak, scat, coh = [], [], []
    for model in models:
        print(f"analysis: {model}", flush=True)
        l, s = run_leakage(model, args.resamples, seed)
        leak.append(l), scat.append(s)
        coh.append(run_coherence(model, args.resamples, seed))
    leakage = pd.concat(leak, ignore_index=True) if leak else pd.DataFrame()
    coherence = pd.concat(coh, ignore_index=True) if coh else pd.DataFrame()
    if not coherence.empty:
        coherence["capability_condition_A_c_index"] = coherence.model.map(lambda m: capability(m, coherence))
    if not leakage.empty:
        leakage["capability_condition_A_c_index"] = leakage.model.map(lambda m: capability(m, coherence))
    leakage.to_csv(C.RESULTS / "leakage.csv", index=False)
    coherence.to_csv(C.RESULTS / "coherence.csv", index=False)
    pd.concat(scat, ignore_index=True).to_csv(C.RESULTS / "scatter_points.csv", index=False) if any(
        len(s) for s in scat) else None
    agreement = run_agreement(models, leakage, coherence)
    agreement.to_csv(C.RESULTS / "agreement.csv", index=False)
    rets = [pd.read_csv(p) for p in sorted((C.RESULTS / "retention").glob("*.csv"))] if (C.RESULTS / "retention").exists() else []
    retention = pd.concat(rets, ignore_index=True) if rets else pd.DataFrame(
        columns=["model", "condition", "retention", "cases_generated", "cases_complete", "parse_failures",
                 "call_errors", "refusal_like_responses"])
    retention.to_csv(C.RESULTS / "retention.csv", index=False)
    write_results_md(models, leakage, coherence, agreement, retention)


LEAK_COLS = ["model", "method", "framing", "pair", "statistic", "estimate", "boot_median", "ci_lower",
             "ci_upper", "n_cases", "blank_reason"]


def leak_section(L: pd.DataFrame, rq: str, stats: list[str]) -> str:
    part = L[(L.rq == rq) & L.statistic.isin(stats)] if not L.empty else L
    if part.empty:
        return "_(no rows yet)_\n\n"
    cols = list(LEAK_COLS)
    if "position" in part and (part.method == "probe").any():
        part = part.assign(probe=part.apply(lambda r: f"{r.probe_type} {r.position} L{int(r.layer)}"
                                            if r.method == "probe" else "", axis=1))
        cols.insert(2, "probe")
    order = {s: i for i, s in enumerate(stats)}
    part = part.assign(_o=part.statistic.map(order)).sort_values(["model", "method", "framing", "pair", "_o"])
    return md_table(part, cols) + "\n"


def write_results_md(models, leakage, coherence, agreement, retention) -> None:
    cfg = C.load()
    L = headline_rows(leakage)
    behavioural = L[L.method != "probe"] if not L.empty else L
    probe = L[L.method == "probe"] if not L.empty else L
    core = ["lambda", "T_C", "T_M", "mean_position", "kappa_halves", "tau_halves", "phi", "tau", "rho",
            "anchor_mean_M_minus_B",
            "manipulation_median_abs_gap", "gap_sd", "normalised_lambda_original"]
    parts = [f"# Second-order belief experiment: results\n\nGenerated {dt.date.today().isoformat()} by "
             "`python -m second_order.deprecated.analyze`; figures by `python -m second_order.deprecated.rq_figures`.  Numbers only; "
             "no interpretation.  Headline rows: all complete cases for the pair, pooled fit, raw outcomes.  "
             "`lambda` is the matched-control estimate (1 - T_C / T_M); a blank ratio means its denominator's "
             "interval includes 0.\n\n"]
    parts.append("## Settings\n\n"
                 f"- Core pair: {cfg['withheld_set']} = {', '.join(cfg['withheld_sets'][cfg['withheld_set']])}\n"
                 f"- False-belief field: {cfg['flip_field']}\n- Withheld fields rendered as: {cfg['missing_fields']}\n"
                 f"- C/D/M prompt layout: {cfg['layout']}\n- Capability floor: {cfg['analysis']['capability_floor']}\n"
                 f"- Bootstrap: paired over cases, {cfg['analysis']['bootstrap_resamples']} resamples "
                 "(layer sweeps: 500), 95% percentile\n"
                 f"- Models analysed here: {', '.join(display(m) for m in models)}\n\n")
    parts.append("## Retention\n\n" + md_table(retention, ["model", "condition", "cases_generated",
                 "cases_complete", "retention", "parse_failures", "call_errors", "refusal_like_responses"]) + "\n")

    parts.append("## RQ1: separation\n\nAssumptions that bear on these numbers: the matched control M is the "
                 "right zero-leakage anchor (RQ1 assumption 10); leakage is linear in the gap (2); condition A is "
                 "the same model as the new conditions (11).  kappa and tau also assume the two fit families share "
                 "a scale (6).\n\n### RQ1a/b: leakage for pair S4, John (figures rq1_lambda_vs_capability, "
                 "rq1_scatter)\n\n" + leak_section(behavioural, "RQ1a/b", core + [
                     "lambda_proportionality_low", "lambda_proportionality_mid", "lambda_proportionality_high"]))
    cap = agreement[agreement.analysis == "capability_spearman"] if not agreement.empty else agreement
    parts.append("Capability trend (Spearman across models above the floor; descriptive):\n\n"
                 + md_table(cap, ["method", "condition", "estimate", "n_cases"]) + "\n")
    parts.append("### RQ1c: false belief (F) against a missing field (H) (figure rq1c_pairs)\n\nC-F and C-H "
                 "differ in more than wrong versus missing (RQ1 assumption 12).\n\n"
                 + leak_section(behavioural, "RQ1c", core + ["diff_lambda", "diff_tau_halves", "diff_T_C",
                                                             "lambda_borrowed_kappa"]))
    parts.append("### RQ1d: user against John, in tracking units\n\nThe user framing has no matched control, "
                 "so no lambda.\n\n" + leak_section(behavioural, "RQ1d", ["tau_halves", "T_C", "diff_tau_halves", "diff_T_C",
                                                                         "normalised_lambda_original"]))

    parts.append("## RQ2: coherence and the evidence ladder\n\nCoherence depends on the spread of beliefs (RQ2 "
                 "assumption 1) and is not correctness (2).\n\n")
    if not coherence.empty:
        co = coherence[(coherence.statistic == "mean_c_index")]
        parts.append("### Coherence (figure rq2_coherence)\n\nMean c-index on the test split.  A_minus_D: cost "
                     "of attribution; D_minus_C and M_minus_C: cost of perspective-taking.\n\n" + md_table(
                         co, ["model", "method", "framing", "condition", "direction", "estimate", "ci_lower",
                              "ci_upper", "n_test_cases"] + (["variant"] if "variant" in co else [])) + "\n")
    parts.append("### Ladder rungs S1 and H against S4\n\n"
                 + leak_section(behavioural, "RQ2", ["tau_halves", "T_C", "lambda_borrowed_kappa", "diff_tau_halves", "diff_T_C",
                                                     "manipulation_median_abs_gap"]))

    parts.append("## RQ3: internal readout\n\nProbe lambda: outcomes are probe readings, the regressor is the "
                 "behavioural latent gap.  A probe lambda near the behavioural lambda is evidence of reuse; a probe "
                 "lambda near 1 does not separate a retained own belief from a probe that reads the chart (RQ3 "
                 "assumption 1).\n\n### Probe lambda (figures rq3_lambda_by_method, rq3_probe_layers)\n\n"
                 + leak_section(probe, "RQ1a/b", ["lambda", "T_C", "T_M"])
                 + leak_section(probe, "RQ1c", ["lambda", "T_C", "T_M"]))
    probe_val = sorted((C.RESULTS / "probes").glob("*/probe_validation.csv")) if (C.RESULTS / "probes").exists() else []
    if probe_val:
        pv = pd.concat([pd.read_csv(p) for p in probe_val], ignore_index=True)
        cols = [c for c in ["model", "position", "probe_type", "layer", "test_auroc_A", "shuffled_label_test_auroc_A",
                            "profile_baseline_all_fields_test_auroc", "profile_baseline_partial_fields_test_auroc",
                            "test_corr_with_ell_A", "profile_baseline_all_fields_test_corr_with_ell_A", "test_r2_ell_A",
                            "P1_max_abs_diff_across_C_D", "fidelity_mean_logprob_per_token_A"] if c in pv]
        parts.append("### Probe validation (Phase 4)\n\n" + md_table(pv, cols) + "\n")

    if not agreement.empty:
        ag = agreement[agreement.analysis != "capability_spearman"]
        parts.append("## Method agreement\n\n" + md_table(ag, [c for c in ["model", "analysis", "method",
                     "condition", "estimate", "n_cases", "detail"] if c in ag]) + "\n")
    parts.append("## Anomalies and stop-point checks\n\n"
                 + "".join(f"- {a}\n" for a in anomalies(models, leakage, coherence, retention)) + "\n")
    (C.RESULTS / "RESULTS.md").write_text("".join(parts))
    print(f"wrote {C.RESULTS / 'RESULTS.md'}")


if __name__ == "__main__":
    main()
