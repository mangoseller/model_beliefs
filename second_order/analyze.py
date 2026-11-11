"""Phase 5 (and Phase 6) analysis from the shared belief format.  No model calls.

    python -m second_order.analyze [--models ...] [--resamples 2000]

Writes ``results/{coherence,leakage,agreement,retention,scatter_points,phase6}.csv``,
``results/disagreements/{model}.md`` and ``RESULTS.md`` (tables, figure
references, anomalies; no interpretation).  Figures: ``python -m second_order.figures``.
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from latent_beliefs.models import MODEL_DISPLAY
from second_order import beliefs as BL
from second_order import config as C
from second_order.coherence import coherence_table, latent_predictions, link_predictions
from second_order.conditions import MAIN_CONDITIONS
from second_order.leakage import FRAMINGS, leakage_table
from second_order.prompts_second_order import build_prompt
from second_order.run_generation import pilot_cases

LOCAL_DISPLAY = {"gemma3_4b_mlx": "Gemma 3 4B (local 4-bit)", "gemma3_12b_mlx": "Gemma 3 12B (local 4-bit)"}
GEMMA_PARAMS = {"gemma3_4b": 4, "gemma3_12b": 12, "gemma3_27b": 27, "gemma3_4b_mlx": 4, "gemma3_12b_mlx": 12}
OPEN_WEIGHT = {"gemma3_4b", "gemma3_12b", "gemma3_27b", "llama31_70b", "qwen3_32b", "deepseek_v32",
               "glm5", "gemma3_4b_mlx", "gemma3_12b_mlx"}


def display(model: str) -> str:
    return MODEL_DISPLAY.get(model) or LOCAL_DISPLAY.get(model, model)


def selected_mask(frame: pd.DataFrame) -> pd.Series:
    if "selected_layer" not in frame:
        return pd.Series(True, index=frame.index)
    return frame["selected_layer"].map(lambda v: True if pd.isna(v) else bool(v))


def framing_of(stat: str) -> str:
    if "user_minus_john" in stat:
        return "user_minus_john"
    if "_john" in stat:
        return "john"
    if "_user" in stat:
        return "user"
    return "shared"


def models_with_beliefs() -> list[str]:
    return sorted(p.name for p in C.BELIEFS.iterdir() if p.is_dir()) if C.BELIEFS.exists() else []


def wide_for(model: str) -> pd.DataFrame:
    path = C.LATENTS / f"{model}_matrix.parquet"
    if path.exists():
        return pd.read_parquet(path)
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
        l = l[l.fit == "pooled"]
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
    rows, scatter = [], []
    pilots = set(pilot_cases())
    for method in [m for m in BL.methods_for(model) if m in ("latent", "direct", "probe")]:
        b = BL.read(model, method)
        b = b[b.condition.isin(MAIN_CONDITIONS)]
        if not {"A", "B"} <= set(b.condition) or not ({"C_john", "C_user"} & set(b.condition)):
            continue
        variants = []
        if method == "latent":
            for fit in ("pooled", "separate"):
                variants.append(({"fit": fit}, b[b.fit == fit]))
        elif method == "direct":
            variants.append(({"fit": "none"}, b))
        else:
            for ptype, pos, layer, is_sel, part in probe_variants(model, b, all_layers=True):
                variants.append(({"fit": "frozen", "probe_type": ptype, "position": pos, "layer": layer,
                                  "selected_layer": is_sel}, part))
        for meta, part in variants:
            cases_all = analysis_set(model, part, MAIN_CONDITIONS)
            split = part.drop_duplicates("case_id").set_index("case_id").split
            sets = {"all_complete": cases_all}
            if meta.get("selected_layer", True) and meta.get("fit") != "separate":
                sets["test_only"] = [c for c in cases_all if split.get(c) == "test"]
                sets["pilot_excluded"] = [c for c in cases_all if c not in pilots]
            for set_name, ids in sets.items():
                n_res = resamples if meta.get("selected_layer", True) else min(resamples, 500)
                t = leakage_table(part[part.case_id.isin(ids)], method, resamples=n_res, seed=seed)
                if t.empty:
                    continue
                t.insert(0, "model", model)
                t.insert(1, "method", method)
                t.insert(2, "framing", t.statistic.map(framing_of))
                t.insert(3, "case_set", set_name)
                for k, v in meta.items():
                    t[k] = v
                rows.append(t)
            if method == "latent" and meta["fit"] == "pooled":
                W = part[part.case_id.isin(cases_all)].pivot_table(index="case_id", columns=["condition", "half"],
                                                                   values="value_calibrated")
                Wr = part[part.case_id.isin(cases_all)].pivot_table(index="case_id", columns=["condition", "half"],
                                                                    values="value")
                for f in FRAMINGS:
                    if (f"C_{f}", "all") not in W:
                        continue
                    scatter.append(pd.DataFrame({
                        "model": model, "framing": f, "case_id": W.index,
                        "x_A_minus_Bprob": Wr[("A", "all")] - Wr[("B", "prob")],
                        "y_C_minus_Bdec": W[(f"C_{f}", "all")] - W[("B", "dec")],
                        "y_D_minus_Bdec": W[(f"D_{f}", "all")] - W[("B", "dec")]}))
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(),
            pd.concat(scatter, ignore_index=True) if scatter else pd.DataFrame())


def run_coherence(model: str, resamples: int, seed: int) -> pd.DataFrame:
    wide = wide_for(model)
    out = []
    methods = BL.methods_for(model)
    conditions = [c for c in wide.condition.unique()]
    probe_cases = None
    for method in [m for m in methods if m in ("latent", "direct", "probe")]:
        b = BL.read(model, method)
        cases_all = analysis_set(model, b[b.condition.isin(MAIN_CONDITIONS)], MAIN_CONDITIONS)
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
            (f"probe_{t}_{p}", part) for t, p, l, s, part in probe_variants(model, b, all_layers=False)]
        for name, part in variants:
            part = part[(part.half == "all") & part.condition.isin(conditions)]
            ids = analysis_set(model, part, [c for c in MAIN_CONDITIONS if c in conditions])
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
            b = b[b.half == "all"]
            if method == "latent":
                methods["latent"] = b[b.fit == "pooled"]
            elif method == "direct":
                methods["direct"] = b
            else:
                for t, p, l, s, part in probe_variants(model, b, all_layers=False):
                    if t == "truth":
                        methods[f"probe_{p}"] = part
        names = sorted(methods)
        for i, m1 in enumerate(names):
            for m2 in names[i + 1:]:
                for cond in MAIN_CONDITIONS:
                    a = methods[m1][methods[m1].condition == cond].set_index("case_id").value
                    c = methods[m2][methods[m2].condition == cond].set_index("case_id").value
                    ids = a.index.intersection(c.index)
                    if len(ids) < 5:
                        continue
                    rho = spearmanr(a.loc[ids], c.loc[ids]).statistic
                    rows.append({"model": model, "analysis": "spearman_between_methods", "method": f"{m1}~{m2}",
                                 "framing": cond.split("_")[1] if "_" in cond else "self", "condition": cond,
                                 "estimate": rho, "n_cases": len(ids)})
        write_disagreements(model, methods)
    # sign agreement of contrasts across methods
    L = leakage[(leakage.case_set == "all_complete") & selected_mask(leakage)] if not leakage.empty else leakage
    if not L.empty:
        L = L[(L.method != "latent") | (L.fit == "pooled")]
    for model in models:
        signs = {}
        if not coherence.empty:
            co = coherence[(coherence.model == model) & (coherence.statistic == "mean_c_index")
                           & (coherence.direction == "reports_to_decisions")]
            for _, r in co[co.condition.isin(["A_minus_D", "D_minus_C"])].iterrows():
                key = f"{r.condition}_{r.framing}"
                signs.setdefault(key, {})[r.get("variant") if isinstance(r.get("variant"), str) else r.method] = np.sign(r.estimate)
        if not L.empty:
            lm = L[(L.model == model) & (L.statistic == "diff_user_minus_john_lambda_C")]
            for _, r in lm.iterrows():
                label = r.method if r.method != "probe" else f"probe_{r.probe_type}_{r.position}"
                signs.setdefault("user_minus_john_lambda_C", {})[label] = np.sign(r.estimate)
        for contrast, by_method in signs.items():
            vals = set(by_method.values())
            rows.append({"model": model, "analysis": "sign_agreement", "method": "|".join(sorted(by_method)),
                         "framing": "", "condition": contrast, "estimate": float(len(vals) == 1),
                         "detail": "; ".join(f"{k}:{int(v)}" for k, v in sorted(by_method.items()))})
    # capability trend per method and framing
    if not L.empty:
        cap = {m: capability(m, coherence) for m in models}
        floor = float(C.load()["analysis"]["capability_floor"])
        for (method, stat), g in L[L.statistic.isin(["lambda_C_john", "lambda_C_user",
                                                     "normalised_lambda_john", "normalised_lambda_user"])
                                   ].groupby(["method", "statistic"]):
            g = g.drop_duplicates("model")
            g = g[g.model.map(lambda m: cap.get(m, np.nan) >= floor)]
            if g.model.nunique() >= 3:
                rho = spearmanr(g.model.map(cap), g.estimate).statistic
                rows.append({"model": "all", "analysis": "capability_spearman", "method": method,
                             "framing": framing_of(stat), "condition": stat, "estimate": rho,
                             "n_cases": int(g.model.nunique())})
    return pd.DataFrame(rows)


def write_disagreements(model: str, methods: dict[str, pd.DataFrame]) -> None:
    if len(methods) < 2:
        return
    n = int(C.load()["analysis"]["disagreements_per_model"])
    folder = C.RESULTS / "disagreements"
    folder.mkdir(parents=True, exist_ok=True)
    resp = {}
    for cond in ("C_john", "C_user"):
        path = C.RESPONSES / model / f"{cond}.parquet"
        if path.exists():
            resp[cond] = pd.read_parquet(path)
    lines = [f"# {display(model)}: cases where the methods disagree most in condition C\n",
             "Disagreement = largest pairwise gap between methods after standardising each method's "
             "beliefs within the condition (z-scores).\n"]
    for cond in ("C_john", "C_user"):
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
            lines.append(f"\nPrompt (numeric probability):\n\n```\n{build_prompt(case_id, cond, 'probability')}```\n")
            if cond in resp:
                r = resp[cond][resp[cond].case_id == case_id]
                for _, row in r.iterrows():
                    if row.output in ("probability", "decision_no_threshold"):
                        lines.append(f"\nResponse ({row.output}, parsed {row.value}):\n\n```\n{row.text}\n```\n")
                lines.append("\nParsed values: " + ", ".join(f"{o}={v}" for o, v in zip(r.output, r.value)) + "\n")
    (folder / f"{model}.md").write_text("".join(lines))


def run_phase6(models: list[str]) -> pd.DataFrame:
    from second_order.probes import latent_targets

    cfg = C.load()["phase6"]
    rows = []
    for model in models:
        lat_path = C.LATENTS / f"{model}.parquet"
        if not lat_path.exists():
            continue
        lat = pd.read_parquet(lat_path)
        lat = lat[lat.fit == "pooled"]
        E = lat[lat.condition.str.startswith("E_")]
        if E.empty:
            continue
        own = lat[lat.condition == "A"].set_index("case_id")
        targets = {"E_self": own.ell_all,
                   "E_weak": latent_targets(cfg["weak_model"]).set_index("case_id").ell_all,
                   "E_strong": latent_targets(cfg["strong_model"]).set_index("case_id").ell_all}
        for cond, tgt in targets.items():
            e = E[E.condition == cond].set_index("case_id").ell_all
            ids = e.index.intersection(tgt.index).intersection(own.index)
            if len(ids) < 5:
                continue
            x, y, a = tgt.loc[ids], e.loc[ids], own.ell_all.loc[ids]
            X = np.column_stack([np.ones(len(ids)), a, x])
            coef = np.linalg.lstsq(X, y.to_numpy(), rcond=None)[0]
            rows.append({"model": model, "method": "latent", "condition": cond, "n_cases": len(ids),
                         "corr_with_target_ell_A": float(np.corrcoef(x, y)[0, 1]),
                         "mae_vs_target_ell_A": float(np.mean(np.abs(x - y))),
                         "projection_weight_own_ell_A": coef[1], "projection_weight_target_ell_A": coef[2],
                         "projection_intercept": coef[0]})
    return pd.DataFrame(rows)


def md_table(frame: pd.DataFrame, cols: list[str], digits: int = 3) -> str:
    if frame.empty:
        return "_(no rows)_\n"
    f = frame[cols].copy()
    for c in f.columns:
        if f[c].dtype.kind == "f":
            f[c] = f[c].map(lambda v: "" if pd.isna(v) else f"{v:.{digits}f}")
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "".join("| " + " | ".join(str(v) for v in r) + " |\n" for r in f.to_numpy())


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
    if not leakage.empty:
        L = leakage[(leakage.case_set == "all_complete")]
        for _, r in L[L.statistic == "manipulation_median_abs_A_minus_B"].drop_duplicates(["model", "method"]).iterrows():
            if r.estimate < 0.5 and r.method == "latent":
                out.append(f"Manipulation check below 0.5 logits: {r.model} ({r.method}, fit {r.fit}): "
                           f"median |l_A - l_B| = {r.estimate:.3f}.")
        for _, r in L[L.statistic.str.startswith("lambda_D_")].iterrows():
            if not (r.ci_lower <= 1 <= r.ci_upper) and r.get("selected_layer", True) is not False:
                if r.method == "latent" and r.get("fit") == "separate":
                    continue
                out.append(f"lambda_D interval excludes 1: {r.model} {r.method} {r.framing} "
                           f"{_variant(r)}: {r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}].")
        for _, r in L[L.statistic == "lambda_0"].iterrows():
            if not (r.ci_lower <= 0 <= r.ci_upper) and r.get("selected_layer", True) is not False:
                out.append(f"lambda_0 interval excludes 0: {r.model} {r.method} {_variant(r)}: "
                           f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}].")
        # pooled vs separate
        lat = L[(L.method == "latent")]
        for (model, stat), g in lat[lat.statistic.str.startswith("normalised_lambda")].groupby(["model", "statistic"]):
            p, s = g[g.fit == "pooled"], g[g.fit == "separate"]
            if len(p) and len(s):
                p, s = p.iloc[0], s.iloc[0]
                if s.estimate < p.ci_lower or s.estimate > p.ci_upper:
                    out.append(f"Pooled vs separate fits differ: {model} {stat}: pooled {p.estimate:.3f} "
                               f"[{p.ci_lower:.3f}, {p.ci_upper:.3f}], separate {s.estimate:.3f}.")
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
    phase6 = run_phase6(models)
    phase6.to_csv(C.RESULTS / "phase6.csv", index=False)
    write_results_md(models, leakage, coherence, agreement, retention, phase6)


def write_results_md(models, leakage, coherence, agreement, retention, phase6) -> None:
    cfg = C.load()
    parts = [f"# Second-order belief experiment: results\n\nGenerated {dt.date.today().isoformat()} by "
             "`python -m second_order.analyze`; figures by `python -m second_order.figures`.  Numbers only; "
             "no interpretation.\n\n"]
    parts.append("## Settings used (decisions for Sam; defaults unless changed)\n\n"
                 f"- Withheld set: {cfg['withheld_set']} = {', '.join(cfg['withheld_sets'][cfg['withheld_set']])}\n"
                 f"- C/D prompt layout: {cfg['layout']}\n- Agent framings: John (third person) and user (first person)\n"
                 f"- Capability floor: {cfg['analysis']['capability_floor']}\n- Bootstrap: paired over cases, "
                 f"{cfg['analysis']['bootstrap_resamples']} resamples (layer sweeps: 500), 95% percentile\n"
                 f"- Models analysed here: {', '.join(display(m) for m in models)}\n- Second domain: none\n\n")
    parts.append("## Retention (figure 4)\n\n" + md_table(retention, ["model", "condition", "cases_generated",
                 "cases_complete", "retention", "parse_failures", "call_errors", "refusal_like_responses"]) + "\n")
    if not coherence.empty:
        co = coherence[(coherence.statistic == "mean_c_index")]
        parts.append("## Coherence (figure 1)\n\nMean c-index on the test split; reports -> decisions unless "
                     "stated.  A_minus_D and D_minus_C are paired contrasts.\n\n" + md_table(
                         co, ["model", "method", "framing", "condition", "direction", "estimate", "ci_lower",
                              "ci_upper", "n_test_cases"] + (["variant"] if "variant" in co else [])) + "\n")
    if not leakage.empty:
        keep = ["lambda_C_john", "lambda_D_john", "lambda_C_user", "lambda_D_user", "lambda_0",
                "normalised_lambda_john", "normalised_lambda_user", "reliability", "c_C_john", "c_C_user",
                "diff_user_minus_john_lambda_C", "diff_user_minus_john_c_C", "mean_shift_C_user_minus_john",
                "manipulation_median_abs_A_minus_B", "closer_to_B_share_john", "closer_to_B_share_user",
                "C_john_two_weight_wA", "C_john_two_weight_wB", "C_user_two_weight_wA", "C_user_two_weight_wB"]
        L = leakage[leakage.statistic.isin(keep)]
        L = L[selected_mask(L)]
        cols = ["model", "method", "case_set", "fit", "statistic", "estimate", "ci_lower", "ci_upper", "n_cases"]
        if "position" in L:
            L = L.assign(probe=L.apply(lambda r: f"{r.probe_type} {r.position} L{int(r.layer)}"
                                       if r.method == "probe" else "", axis=1))
            cols.insert(4, "probe")
        parts.append("## Leakage (figures 2, 3, 5, 6)\n\nPrimary case set first; `test_only` and "
                     "`pilot_excluded` are secondary; `separate` is the per-condition-fit robustness check.\n\n"
                     + md_table(L.sort_values(["model", "method", "case_set", "fit"]), cols) + "\n")
    if not agreement.empty:
        parts.append("## Method agreement\n\n" + md_table(agreement, [c for c in ["model", "analysis", "method",
                     "condition", "estimate", "n_cases", "detail"] if c in agreement]) + "\n")
    parts.append("## Phase 6: predicting other AI models\n\n" + md_table(phase6, list(phase6.columns)) + "\n"
                 if not phase6.empty else "## Phase 6: predicting other AI models\n\n_(no E-condition data yet)_\n\n")
    probe_val = sorted((C.RESULTS / "probes").glob("*/probe_validation.csv")) if (C.RESULTS / "probes").exists() else []
    if probe_val:
        pv = pd.concat([pd.read_csv(p) for p in probe_val], ignore_index=True)
        cols = [c for c in ["model", "position", "probe_type", "layer", "test_auroc_A", "shuffled_label_test_auroc_A",
                            "profile_baseline_all_fields_test_auroc", "profile_baseline_partial_fields_test_auroc",
                            "test_corr_with_ell_A", "profile_baseline_all_fields_test_corr_with_ell_A", "test_r2_ell_A",
                            "P1_max_abs_diff_across_C_D", "fidelity_mean_logprob_per_token_A"] if c in pv]
        parts.append("## Probe validation (Phase 4)\n\n" + md_table(pv, cols) + "\n")
    parts.append("## Figures\n\n" + "".join(f"{i}. `figures/fig{i}_{n}.pdf`\n" for i, n in enumerate(
        ["coherence", "lambda_vs_capability", "scatter", "retention", "lambda_by_method", "probe_layers"], 1)) + "\n")
    parts.append("## Anomalies\n\n" + "".join(f"- {a}\n" for a in anomalies(models, leakage, coherence, retention))
                 + "\n")
    (C.ROOT / "RESULTS.md").write_text("".join(parts))
    print("wrote RESULTS.md")


if __name__ == "__main__":
    main()
