"""Phase 2 of the binding experiment: beliefs, lambda per cell, contrasts and the decision rules.

    python -m second_order.binding_analysis --run runs/binding-pilot --model qwen3_8b [--resamples 2000]

Reads the session-1 export (``outputs/<model>_responses.parquet``) and writes
``outputs/analysis/<model>/``:

* ``beliefs_direct.parquet`` (mean clipped logit of the stated probability per case, arm,
  order, role and replicate) and, for the latent arms, ``beliefs_latent.parquet`` (one pooled
  refit across every cell: an own-belief family, A and B, and a PERSON family, C, M and D;
  item parameters on train-split patients; posterior means de-shrunk by the fit's
  calibration line, as in ``fit_pooled``);
* ``lambda_cells.csv``: the Methods statistics (``leakage.setting_statistics``) per method,
  arm and order, plus both orders stacked ("pooled") and the patients right on both option
  orders of the cell's check ("check_passed"), with paired patient-bootstrap intervals;
* ``contrasts.csv``: order (person_first - own_first), binding and absence contrasts, every
  one from the same bootstrap draws;
* ``checks.csv``, ``retention.csv``, ``drift.csv``;
* ``decisions.json`` and ``RESULTS.md``: the pre-registered rules of the plan, and for the
  pilot the decisions it exists to make.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import binding as B
from second_order import config as C
from second_order import leakage as L
from second_order.beliefs import clip_logit

ROLES = ("A", "B", "C", "M", "D")
# lambda intervals in the rules are 95% Fieller sets. Each rule is supported, refuted (its interval excludes the
# claim) or inconclusive (too imprecise to tell); undetermined when a Fieller set is unbounded (weak beta_D).
RULES = {
    "H1_false_misbinding": "false_labelled: lambda inside [-0.1, 0.1] in both orders, the person_first - own_first "
                           "difference inside (-0.2, 0.2), and labelling lowers the person_first leak "
                           "(false_labelled - false_verified, person_first, CI upper < 0)",
    "H2_curse_of_knowledge": "partial, strictest cell (instructed+explicit if run, else labelled+explicit), check "
                             "passed, orders pooled: lambda lower bound > 0.15",
    "H3_absence_not_ignorance": "partial labelled, orders pooled: lambda(implicit) - lambda(explicit) > 0.3 with CI "
                                "lower > 0 (both cells' beta_D intervals excluding 0), and the strictest cell's "
                                "lambda <= 0.1",
}


# Loading and beliefs
def load(root: Path, model: str) -> tuple[pd.DataFrame, dict]:
    plan = B.read_plan(root)
    long = pd.read_parquet(root / "outputs" / f"{model}_responses.parquet")
    return long, plan


def load_order_followup(root: Path, model: str) -> tuple[pd.DataFrame, dict]:
    """Astra's order follow-up export in this module's schema, so it gets the same estimators."""
    from second_order import order_followup as F
    F.read_plan(root)
    raw = pd.read_parquet(root / "outputs" / f"{model}_responses.parquet")
    long = raw.copy()
    long["arm"] = [B.FOLLOWUP_ARM[(s, w)] for s, w in zip(long.setting, long.wording)]
    long["variant"], long["binding"], long["absence"] = long.setting, long.wording, "implicit"
    ab = long.role.isin(["A", "B"])
    long.loc[ab, "block_order"] = "shared"
    long["ab_version"] = [B.own_belief_version(a, r) if r in "AB" else "" for a, r in zip(long.arm, long.role)]
    long["drift_of"] = ""
    long = long.drop_duplicates(["case_id", "arm", "block_order", "role", "output", "replicate", "sample"])
    split = B.patient_split(int(C.load()["seed"]))
    long["split"] = long.case_id.map(split)
    arms = list(B.ORDER_FOLLOWUP_ARMS)
    plan = dict(spec=dict(profile="order_followup", arms=arms, latent_arms=arms), cases=sorted(long.case_id.unique()))
    return long, plan


def direct_beliefs(long: pd.DataFrame) -> pd.DataFrame:
    part = long[(long.output == "probability/normal") & (long.status == "parsed") & (long.arm != "drift")].copy()
    part["logit"] = clip_logit(part.value.astype(float).to_numpy())
    keys = ["case_id", "arm", "block_order", "role", "replicate"]
    g = part.groupby(keys)
    out = pd.DataFrame({"value": g.logit.mean(), "n_samples": g.size(),
                        "sd": g.logit.std(ddof=1) / np.sqrt(g.size())}).reset_index()
    out["value_calibrated"] = out["value"]
    out["method"] = "direct"
    return out


def latent_beliefs(long: pd.DataFrame, plan: dict, model: str) -> pd.DataFrame:
    """One pooled refit across all latent-arm cells (self: A, B; person: C, M, D)."""
    from second_order import fit_pooled as FP
    from second_order.measurement import calibration
    from second_order.responses import configured_specs, split as patient_split

    arms = plan["spec"]["latent_arms"]
    if not arms:
        return pd.DataFrame()
    names = [s.name for s in configured_specs()]
    part = long[long.arm.isin(arms) & long.output.isin(names) & (long["sample"] == 0)]
    keys = ["case_id", "arm", "block_order", "role", "replicate"]
    # A vector is identified by its probability item (all its outputs share the records).
    vid = part[part.output == "probability/normal"].set_index(keys).item_key.rename("vector_id")
    wide = part.pivot_table(index=keys, columns="output", values="value", aggfunc="first").reindex(columns=names)
    wide = wide.join(vid).reset_index()
    vectors = wide.drop_duplicates("vector_id").copy()
    vectors["n_outputs"] = vectors[names].notna().sum(axis=1)
    share = float(C.load()["latent"].get("min_output_share", 1.0))
    vectors["usable"] = vectors.n_outputs >= share * len(names)
    vectors["split"] = vectors.case_id.map(patient_split(model)).fillna("none")
    vectors["condition"] = np.where(vectors.role.isin(["A", "B"]), "self", "person")
    seed = int(C.load()["seed"])
    fitted = []
    for family in ("self", "person"):
        rows = vectors[(vectors.condition == family) & vectors.usable]
        if not {"train", "val"} <= set(rows.split):
            raise ValueError(f"latent family {family}: no train or validation patients")
        fseed = FP.stable_seed(seed, model, "binding", family)
        fit, info = FP.fit_group(vectors, [family], fseed)
        post = FP.posteriors(fit, rows)
        slope, intercept = calibration(fit, seed=fseed)
        post["cal"] = (post["ell"] - intercept) / slope
        fitted.append(pd.concat([rows[["vector_id"]], post[["ell", "sd", "cal"]]], axis=1).assign(family=family))
        print(f"  latent {model}/{family}: epochs={info['epochs_ran']} val_nll={info['best_validation_mean_nll']:.4f} "
              f"calibration slope={slope:.3f}", flush=True)
    post = pd.concat(fitted).set_index("vector_id")
    out = wide[keys + ["vector_id"]].join(post, on="vector_id").dropna(subset=["ell"])
    out = out.rename(columns={"ell": "value", "cal": "value_calibrated"})
    out["n_samples"], out["method"] = 1, "latent"
    return out[keys + ["value", "sd", "value_calibrated", "n_samples", "method"]]


# Lambda per cell, joint bootstrap
def column(arm, order, role, ab_version=None):
    return f"{ab_version}|shared|{role}" if role in ("A", "B") else f"{arm}|{order}|{role}"


def roles_for(arm: str, order: str) -> dict[str, str]:
    return {r: column(arm, order, r, B.own_belief_version(arm, r) if r in "AB" else None) for r in ROLES}


def wide_frame(beliefs: pd.DataFrame) -> pd.DataFrame:
    """case_id x '<cell>|<role>[@r1]' (value) and ':cal' columns; A and B by their shared version."""
    b = beliefs.copy()
    b["col"] = [column(a, o, r, B.own_belief_version(a, r) if r in "AB" else None) + (L.SECOND_RUN if rep else "")
                for a, o, r, rep in zip(b.arm, b.block_order, b.role, b.replicate)]
    b = b.drop_duplicates(["case_id", "col"])
    W = b.pivot(index="case_id", columns="col", values="value")
    Wc = b.pivot(index="case_id", columns="col", values="value_calibrated")
    Wc.columns = [f"{c}:cal" for c in Wc.columns]
    return W.join(Wc)


def check_pass(long: pd.DataFrame) -> pd.DataFrame:
    """case_id x '<arm>|<order>' -> 1.0 if the cell's check is right in both option orders."""
    chk = long[long.output.str.startswith("check/") & (long.arm != "drift")].copy()
    chk["correct"] = chk.value.where(chk.status == "parsed", 0.0).where(~chk.status.isin(["not_generated", "call_error"]))
    g = chk.groupby(["case_id", "arm", "block_order"]).correct.agg(["min", "count"]).reset_index()
    g["passed"] = np.where(g["count"] == 2, (g["min"] == 1).astype(float), np.nan)
    g["col"] = "pass:" + g.arm + "|" + g.block_order
    return g.pivot(index="case_id", columns="col", values="passed")


def _subset(W, k, mask=None):
    need = [k[r] for r in ROLES] + [k[r] + L.SECOND_RUN for r in ("A", "B") if k[r] + L.SECOND_RUN in W]
    if any(c not in W for c in need):
        return {c: v[:0] for c, v in W.items()}, 0
    ok = np.all([np.isfinite(W[c]) for c in need], axis=0)
    if mask is not None:
        ok &= mask == 1
    return {c: v[ok] for c, v in W.items()}, int(ok.sum())


def _stacked(W, ks, masks=None):
    """Both orders stacked: A and B repeat, C, M and D come from each order."""
    parts = [_subset(W, k, None if masks is None else masks[i])[0] for i, k in enumerate(ks)]
    base = ks[0]
    out = {}
    for r in ROLES:
        out[base[r]] = np.concatenate([p[k[r]] for p, k in zip(parts, ks)])
        out[f"{base[r]}:cal"] = np.concatenate([p.get(f"{k[r]}:cal", p[k[r]]) for p, k in zip(parts, ks)])
    for r in ("A", "B"):
        if all(k[r] + L.SECOND_RUN in p for p, k in zip(parts, ks)):
            out[base[r] + L.SECOND_RUN] = np.concatenate([p[k[r] + L.SECOND_RUN] for p, k in zip(parts, ks)])
            out[base[r] + L.SECOND_RUN + ":cal"] = np.concatenate(
                [p.get(k[r] + L.SECOND_RUN + ":cal", p[k[r] + L.SECOND_RUN]) for p, k in zip(parts, ks)])
    return out, len(out[base["C"]])


MIN_CASES = 8


def cell_statistics(W: dict, arms, checks: bool) -> dict:
    """Every cell's statistics on one (resampled) set of patients."""
    out = {}
    for arm in arms:
        ks = [roles_for(arm, o) for o in B.ORDERS]
        for order, k in zip(B.ORDERS, ks):
            sub, n = _subset(W, k)
            if n >= MIN_CASES:
                out[(arm, order, "all")] = L.setting_statistics(sub, k) | {"n": n}
            if checks and f"pass:{arm}|{order}" in W:
                sub, n = _subset(W, k, W[f"pass:{arm}|{order}"])
                if n >= MIN_CASES:
                    out[(arm, order, "check_passed")] = L.setting_statistics(sub, k) | {"n": n}
        sub, n = _stacked(W, ks)
        if n >= MIN_CASES:
            out[(arm, "pooled", "all")] = L.setting_statistics(sub, ks[0]) | {"n": n}
        if checks and all(f"pass:{arm}|{o}" in W for o in B.ORDERS):
            sub, n = _stacked(W, ks, [W[f"pass:{arm}|{o}"] for o in B.ORDERS])
            if n >= MIN_CASES:
                out[(arm, "pooled", "check_passed")] = L.setting_statistics(sub, ks[0]) | {"n": n}
    return out


def contrast_list(arms) -> list[tuple[str, tuple, tuple]]:
    """(name, minuend cell, subtrahend cell) for lambda differences."""
    have = set(arms)
    out = [(f"order:{a}", (a, "person_first", "all"), (a, "own_first", "all")) for a in arms]
    pairs = [("partial_labelled", "partial_original"), ("false_labelled", "false_verified"),
             ("false_neutral", "false_verified"), ("false_labelled", "false_neutral"),
             ("strong_labelled", "strong_original"), ("partial_instructed", "partial_labelled"),
             ("false_instructed", "false_labelled"), ("strong_instructed", "strong_labelled"),
             ("partial_labelled", "partial_labelled_explicit"), ("partial_instructed", "partial_instructed_explicit"),
             ("strong_labelled", "strong_labelled_explicit"), ("strong_instructed", "strong_instructed_explicit"),
             ("strong_original", "partial_original"), ("strong_labelled", "partial_labelled")]
    for a, b in pairs:
        if a in have and b in have:
            for order in (*B.ORDERS, "pooled"):
                out.append((f"{a}-{b}:{order}", (a, order, "all"), (b, order, "all")))
    return out


def evaluate(W, arms, checks=True):
    cells = cell_statistics(W, arms, checks)
    contrasts = {}
    for name, a, b in contrast_list(arms):
        if a in cells and b in cells:
            contrasts[name] = cells[a]["lambda"] - cells[b]["lambda"]
    return cells, contrasts


def bootstrap(frame: pd.DataFrame, arms, resamples: int, seed: int, checks=True):
    arrays = {c: frame[c].to_numpy(dtype=float) for c in frame.columns}
    points, cpoints = evaluate(arrays, arms, checks)
    draws = {key: {s: [] for s in stats} for key, stats in points.items()}
    cdraws = {name: [] for name in cpoints}
    rng = np.random.default_rng(seed)
    n = len(frame)
    for _ in range(int(resamples)):
        idx = rng.integers(0, n, n)
        cells, contrasts = evaluate({c: v[idx] for c, v in arrays.items()}, arms, checks)
        for key, stats in draws.items():
            got = cells.get(key, {})
            for s, values in stats.items():
                values.append(got.get(s, np.nan))
        for name, values in cdraws.items():
            values.append(contrasts.get(name, np.nan))
    rows = []
    for (arm, order, subset), point in points.items():
        n_cases = point.pop("n")
        draws[(arm, order, subset)].pop("n")
        t = L._bootstrap_summary(point, draws[(arm, order, subset)], n_cases, resamples)
        rows.append(t.assign(arm=arm, order=order, subset=subset))
    cells = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    crow = []
    for name, value in cpoints.items():
        d = np.asarray(cdraws[name], float)
        d = d[np.isfinite(d)]
        lo, hi = np.percentile(d, [2.5, 97.5]) if len(d) else (np.nan, np.nan)
        crow.append(dict(contrast=name, estimate=value, ci_lower=lo, ci_upper=hi, usable_draws=len(d)))
    return cells, pd.DataFrame(crow)


# Decisions
def _get(cells, arm, order, stat="lambda", subset="all"):
    r = cells[(cells.arm == arm) & (cells.order == order) & (cells.statistic == stat) & (cells.subset == subset)]
    return r.iloc[0] if len(r) else None


def _contrast(contrasts, name):
    r = contrasts[contrasts.contrast == name]
    return r.iloc[0] if len(r) else None


def strictest(arms, variant="partial"):
    for arm in (f"{variant}_instructed_explicit", f"{variant}_labelled_explicit"):
        if arm in arms:
            return arm
    return None


def lambda_interval(cells, arm, order, subset="all"):
    """[lambda, Fieller lower, upper], or (None, reason) when the Fieller set is not a bounded interval."""
    lam, fieller = _get(cells, arm, order, "lambda", subset), _get(cells, arm, order, "lambda_fieller", subset)
    if lam is None or fieller is None:
        return None, "no estimate"
    if fieller.interval != "bounded":
        return None, f"Fieller set {fieller.interval} (weak beta_D)"
    return [float(lam.estimate), float(fieller.ci_lower), float(fieller.ci_upper)], ""


def _verdict(supported, refuted, reasons):
    if reasons:
        return {"status": "undetermined", "supported": False, "reason": "; ".join(reasons)}
    status = "supported" if supported else "refuted" if refuted else "inconclusive"
    return {"status": status, "supported": bool(supported)}


def _beta_d_reason(cells, arm, order):
    r = _get(cells, arm, order, "beta_D")
    return "" if r is not None and not (r.ci_lower <= 0 <= r.ci_upper) else f"{arm}|{order}: beta_D interval includes 0"


def decision_rules(cells, contrasts, arms) -> dict:
    out = {}

    def interval(r):
        return None if r is None else [float(r.estimate), float(r.ci_lower), float(r.ci_upper)]

    # H1: labels remove the misbinding leak (which must exist in the unlabelled person_first cell)
    if {"false_labelled", "false_verified"} <= set(arms):
        got = {o: lambda_interval(cells, "false_labelled", o) for o in B.ORDERS}
        order_diff = _contrast(contrasts, "order:false_labelled")
        reduction = _contrast(contrasts, "false_labelled-false_verified:person_first")
        reasons = [f"{o}: {why}" for o, (iv, why) in got.items() if iv is None]
        reasons += [f"no {n} contrast" for n, c in (("order", order_diff), ("reduction", reduction)) if c is None]
        ok = not reasons
        supported = ok and all(-0.1 <= iv[1] and iv[2] <= 0.1 for iv, _ in got.values()) \
            and -0.2 < order_diff.ci_lower and order_diff.ci_upper < 0.2 and reduction.ci_upper < 0
        refuted = ok and (any(iv[1] > 0.1 or iv[2] < -0.1 for iv, _ in got.values())
                          or order_diff.ci_lower >= 0.2 or order_diff.ci_upper <= -0.2 or reduction.ci_lower >= 0)
        out["H1_false_misbinding"] = _verdict(supported, refuted, reasons) | {o: iv for o, (iv, _) in got.items()} | \
            {"order_difference": interval(order_diff), "labelling_reduction_person_first": interval(reduction)}
    for variant in ("partial", "strong"):
        arm = strictest(arms, variant)
        if arm is None:
            continue
        iv, why = lambda_interval(cells, arm, "pooled", "check_passed")
        out[f"H2_curse_of_knowledge[{variant}]"] = _verdict(
            iv is not None and iv[1] > 0.15, iv is not None and iv[2] <= 0.15, [why] if why else []) | {
            "cell": arm, "lambda_check_passed_pooled": iv}
        drop = _contrast(contrasts, f"{variant}_labelled-{variant}_labelled_explicit:pooled")
        strict, why = lambda_interval(cells, arm, "pooled")
        reasons = ([] if drop is not None else ["no absence contrast"]) + ([why] if why else [])
        reasons += [r for a in (f"{variant}_labelled", f"{variant}_labelled_explicit")
                    if (r := _beta_d_reason(cells, a, "pooled"))]
        ok = not reasons
        supported = ok and drop.estimate > 0.3 and drop.ci_lower > 0 and strict[0] <= 0.1
        refuted = ok and (drop.ci_upper < 0.3 or strict[1] > 0.1)
        out[f"H3_absence_not_ignorance[{variant}]"] = _verdict(supported, refuted, reasons) | {
            "drop": interval(drop), "strictest": arm, "strictest_lambda": strict}
    return out


def pilot_decisions(cells, arms, retention, checks_table, n_patients, n_full=150) -> dict:
    """What the pilot reports before the full run: retention under the new prompts, check accuracy per arm, the
    manipulation size of each variant, and the lambda interval widths projected to 150 patients."""
    out = {}
    if not retention.empty:
        low = retention[retention.retention < 0.9]
        out["prompt_retention_ok"] = dict(decision=bool(low.empty), rule="every arm x role keeps >= 90% parsed",
                                          below=low.to_dict("records"))
    if not checks_table.empty:
        out["check_accuracy_by_arm"] = checks_table.groupby("arm").accuracy.mean().round(3).to_dict()
    gaps = cells[(cells.statistic == "manipulation_median_abs_gap") & (cells.order == "pooled") & (cells.subset == "all")]
    out["median_abs_gap_by_arm"] = {r.arm: round(float(r.estimate), 3) for r in gaps.itertuples()}
    scale = float(np.sqrt(n_patients / n_full))
    half = cells[(cells.statistic == "lambda") & (cells.subset == "all")].assign(
        half_width=lambda t: (t.ci_upper - t.ci_lower) / 2)
    out["projected_lambda_ci_half_width_at_150"] = {f"{r.arm}|{r.order}": round(float(r.half_width * scale), 3)
                                                     for r in half.itertuples() if np.isfinite(r.half_width)}
    return out


# Other tables
def retention_table(long):
    part = long[long.arm != "drift"]
    g = part.groupby(["arm", "role"])
    return pd.DataFrame({"scheduled": g.size(), "parsed": g.status.apply(lambda s: (s == "parsed").sum())}
                        ).assign(retention=lambda t: t.parsed / t.scheduled).reset_index()


def checks_table(long):
    chk = long[long.output.str.startswith("check/") & (long.arm != "drift")].copy()
    if chk.empty:
        return pd.DataFrame()
    chk["correct"] = chk.value.where(chk.status == "parsed", 0.0)
    return chk.groupby(["arm", "block_order", "check_relation"]).correct.agg(["mean", "size"]).rename(
        columns={"mean": "accuracy", "size": "n"}).reset_index()


def drift_table(long, resamples, seed):
    d = long[long.arm == "drift"]
    if d.empty:
        return pd.DataFrame()
    base = long[long.arm != "drift"].drop_duplicates("item_key").set_index("item_key")
    d = d.assign(base_value=d.drift_of.map(base.value), base_status=d.drift_of.map(base.status))
    d = d[(d.status == "parsed") & (d.base_status == "parsed")].copy()
    d["kind"] = d.output.str.split("/").str[0].map(lambda q: "probability" if q == "probability" else "choice")
    rows = []
    rng = np.random.default_rng(seed)
    for kind, part in d.groupby("kind"):
        if kind == "probability":
            diff = clip_logit(part.value.astype(float)) - clip_logit(part.base_value.astype(float))
        else:
            diff = part.value.astype(float).to_numpy() - part.base_value.astype(float).to_numpy()
        frame = pd.DataFrame({"case": part.case_id.to_numpy(), "diff": diff})
        per = frame.groupby("case")["diff"].mean().to_numpy()
        boots = [rng.choice(per, len(per)).mean() for _ in range(resamples)]
        rows.append(dict(kind=kind, n=len(part), patients=len(per), mean_difference=float(np.mean(diff)),
                         ci_lower=float(np.percentile(boots, 2.5)), ci_upper=float(np.percentile(boots, 97.5)),
                         scale="logit (new - baseline)" if kind == "probability" else "answer value (new - baseline)"))
    return pd.DataFrame(rows)


def gate(reasoning, no_reasoning, arms) -> pd.DataFrame:
    """Phase 3 gate on one set of patients (both arguments: ``bootstrap``'s (cells, contrasts)). A cell enters
    Phase 3 if its no-reasoning lambda is estimable (bounded Fieller set), its interval overlaps the reasoning
    one, and, where the reasoning order effect is clear (CI excluding 0), the no-reasoning one has its sign."""
    (rc, rk), (nc, nk) = reasoning, no_reasoning
    rows = []
    for arm in arms:
        oa, ob = _contrast(rk, f"order:{arm}"), _contrast(nk, f"order:{arm}")
        clear = oa is not None and not (oa.ci_lower <= 0 <= oa.ci_upper)
        same = not clear or (ob is not None and np.sign(oa.estimate) == np.sign(ob.estimate))
        for order in B.ORDERS:
            a = _get(rc, arm, order)
            b, why = lambda_interval(nc, arm, order)
            if a is None:
                continue
            overlap = b is not None and max(a.ci_lower, b[1]) <= min(a.ci_upper, b[2])
            rows.append(dict(arm=arm, order=order, lambda_reasoning=a.estimate,
                             lambda_no_reasoning=np.nan if b is None else b[0], no_reasoning_estimable=b is not None,
                             intervals_overlap=bool(overlap), order_effect_consistent=bool(same),
                             enters_phase3=bool(b is not None and overlap and same), reason=why))
    return pd.DataFrame(rows)


def write_results(out: Path, model, plan, tables, decisions, pilot) -> None:
    cells = tables["lambda_cells"]
    lines = [f"# Binding experiment: {model} ({plan['spec']['profile']} profile)", "",
             f"{len(plan['cases'])} patients; arms: {', '.join(plan['spec']['arms'])}.", "",
             "## lambda per cell (95% paired patient-bootstrap intervals)", "",
             "| method | arm | order | subset | n | lambda | beta_C | beta_D | median abs x |", "|---|---|---|---|---|---|---|---|---|"]
    for (method, arm, order, subset), part in cells.groupby(["method", "arm", "order", "subset"], sort=True):
        s = part.set_index("statistic")

        def fmt(stat):
            if stat not in s.index or not np.isfinite(s.loc[stat, "estimate"]):
                return s.loc[stat, "blank_reason"] if stat in s.index and s.loc[stat, "blank_reason"] else ""
            r = s.loc[stat]
            return f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}]"
        n = int(s.loc["lambda", "n_cases"]) if "lambda" in s.index else ""
        lines.append(f"| {method} | {arm} | {order} | {subset} | {n} | {fmt('lambda')} | {fmt('beta_C')} | "
                     f"{fmt('beta_D')} | {fmt('manipulation_median_abs_gap')} |")
    lines += ["", "## Contrasts of lambda", "", "| method | contrast | estimate |", "|---|---|---|"]
    for r in tables["contrasts"].itertuples():
        lines.append(f"| {r.method} | {r.contrast} | {r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}] |")
    lines += ["", "## Pre-registered decision rules", ""]
    for method, rules in decisions.items():
        for name, result in rules.items():
            base = name.split("[")[0]
            evidence = {k: v for k, v in result.items() if k not in ("supported", "status")}
            lines.append(f"- **{method} / {name}**: {result['status']}. Rule: {RULES.get(base, '')}. "
                         f"Evidence: `{json.dumps(evidence)}`")
    if pilot:
        lines += ["", "## Pilot decisions", ""]
        for name, result in pilot.items():
            lines.append(f"- **{name}**: `{json.dumps(result, default=float)}`")
    (out / "RESULTS.md").write_text("\n".join(lines) + "\n")


def analyse(root: Path, model: str, resamples: int = 2000, seed: int | None = None,
            order_followup: bool = False) -> dict:
    long, plan = (load_order_followup if order_followup else load)(root, model)
    seed = int(C.load()["seed"]) if seed is None else seed
    arms = plan["spec"]["arms"]
    out = root / "outputs" / "analysis" / model
    out.mkdir(parents=True, exist_ok=True)
    beliefs = {"direct": direct_beliefs(long)}
    if plan["spec"]["latent_arms"]:
        beliefs["latent"] = latent_beliefs(long, plan, model)
    passes = check_pass(long)
    all_cells, all_contrasts, decisions = [], [], {}
    for method, b in beliefs.items():
        b.to_parquet(out / f"beliefs_{method}.parquet", index=False)
        method_arms = arms if method == "direct" else plan["spec"]["latent_arms"]
        frame = wide_frame(b).join(passes, how="left")
        cells, contrasts = bootstrap(frame, method_arms, resamples, seed)
        cells["method"], contrasts["method"] = method, method
        all_cells.append(cells)
        all_contrasts.append(contrasts)
        decisions[method] = decision_rules(cells, contrasts, method_arms)
    cells = pd.concat(all_cells, ignore_index=True)
    contrasts = pd.concat(all_contrasts, ignore_index=True)
    cells.to_csv(out / "lambda_cells.csv", index=False)
    contrasts.to_csv(out / "contrasts.csv", index=False)
    ret, chk = retention_table(long), checks_table(long)
    ret.to_csv(out / "retention.csv", index=False)
    chk.to_csv(out / "checks.csv", index=False)
    drift = drift_table(long, resamples, seed)
    drift.to_csv(out / "drift.csv", index=False)
    pilot = (pilot_decisions(cells[cells.method == "direct"], arms, ret, chk, len(plan["cases"]))
             if plan["spec"]["profile"] == "pilot" else {})
    result = {"decision_rules": decisions, "pilot_decisions": pilot}
    (out / "decisions.json").write_text(json.dumps(result, indent=2, default=float))
    write_results(out, model, plan, {"lambda_cells": cells, "contrasts": contrasts}, decisions, pilot)
    print(f"{model}: analysis written to {out}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--resamples", type=int, default=None)
    parser.add_argument("--order-followup", action="store_true", help="the run is Astra's order follow-up")
    args = parser.parse_args()
    root = args.run.resolve()
    if args.order_followup:
        from second_order import order_followup as F
        F.read_plan(root)
    else:
        B.read_plan(root)
    resamples = args.resamples or int(C.load()["analysis"]["bootstrap_resamples"])
    analyse(root, args.model, resamples, order_followup=args.order_followup)


if __name__ == "__main__":
    main()
