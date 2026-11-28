"""Phase 2 of the binding experiment: beliefs, lambda per cell, contrasts and the decision rules.

    python -m belief_leakage.binding_analysis --run runs/binding-pilot --model qwen3_8b [--resamples 2000]

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
* ``lambda_cells_rules.csv``: the cells the decision rules read, from their own, larger bootstrap;
* ``decisions.json`` and ``RESULTS.md``: the pre-registered decision rules (H1-H3), anomalies, and
  for the pilot profile the decisions it exists to make (retention, check accuracy, precision).

The partial-setting shown/withheld checks are scored from the reasoning where it reaches a
conclusion (``check_scoring``): for yes/no fields the letter is confounded with the field's value.
The release's letter scoring is kept beside it (``check_passed_letter``, ``accuracy_letter``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from belief_leakage import binding as B
from belief_leakage import config as C
from belief_leakage import leakage as L
from belief_leakage import run_plan
from belief_leakage.beliefs import clip_logit

ROLES = ("A", "B", "C", "M", "D")
# lambda intervals in the rules are 95% Fieller sets. Each rule is supported, refuted (its interval excludes the
# claim) or inconclusive (too imprecise to tell); undetermined when a Fieller set is unbounded (weak beta_D).
RULES = {
    "H1_false_misbinding": "false_labelled: lambda inside [-0.1, 0.1] in both orders, the person_first - own_first "
                           "difference inside (-0.2, 0.2), and labelling lowers the person_first leak "
                           "(false_labelled - false_neutral, person_first, CI upper < 0; false_labelled - "
                           "false_verified, which also removes the cue and changes A/B, is reported beside it; "
                           "beta_D of both unlabelled person_first cells excluding 0)",
    "H2_curse_of_knowledge": "strictest cell of the variant (instructed+explicit if run, else labelled+explicit), "
                             "check passed (shown/withheld checks scored from the reasoning), orders pooled: "
                             "lambda lower bound > 0.15",
    "H3_absence_not_ignorance": "the variant's labelled arms, orders pooled: lambda(implicit) - lambda(explicit) > 0.3 "
                                "with CI lower > 0 (both cells' beta_D intervals excluding 0), and the strictest "
                                "cell's lambda <= 0.1",
}


# Loading and beliefs
def load(root: Path, model: str) -> tuple[pd.DataFrame, dict]:
    plan = B.read_plan(root)
    long = pd.read_parquet(root / "outputs" / f"{model}_responses.parquet")
    return long, plan


def load_order_followup(root: Path, model: str) -> tuple[pd.DataFrame, dict]:
    """The order follow-up's export in this module's schema, so it gets the same estimators."""
    run_plan.read_plan(root)
    long = pd.read_parquet(root / "outputs" / f"{model}_responses.parquet")
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
    from belief_leakage import fit_pooled as FP
    from belief_leakage.responses import configured_specs, split as patient_split

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
        slope, intercept = FP.calibration(fit, seed=fseed)
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


def load_texts(root: Path, model: str) -> pd.Series | None:
    """item_key -> generated text, for scoring the partial checks from the reasoning (``check_scoring``)."""
    path = root / "outputs" / f"{model}_texts.parquet"
    if not path.exists():
        print(f"  {path.name} not found: partial checks are scored by the letter only", flush=True)
        return None
    t = pd.read_parquet(path, columns=["item_key", "text"])
    return t.drop_duplicates("item_key").set_index("item_key").text


def score_checks(long: pd.DataFrame, texts: pd.Series | None) -> pd.DataFrame:
    """Every check row with ``correct_letter`` (the release's letter scoring) and ``correct`` (the shown/withheld
    checks scored from the reasoning where it reaches a conclusion, else the letter)."""
    from belief_leakage.check_scoring import reasoning_correct
    chk = long[long.output.str.startswith("check/") & (long.arm != "drift")].copy()
    missing = chk.status.isin(["not_generated", "call_error"])
    chk["correct_letter"] = chk.value.where(chk.status == "parsed", 0.0).where(~missing)
    chk["correct"] = chk["correct_letter"]
    chk["scored_by"] = np.where(missing, "", "letter")
    if texts is not None and len(chk):
        rel = chk.check_relation.astype(str)
        use = ~missing & rel.isin(["shown", "withheld"]) & chk.status.isin(["parsed", "parse_failure"])
        got = [reasoning_correct(texts.get(k), f, r) for k, f, r in
               zip(chk.item_key[use], chk.check_fact[use].astype(str), rel[use])]
        got = pd.Series(got, index=chk.index[use], dtype=object)
        have = got.notna()
        chk.loc[have[have].index, "correct"] = got[have].astype(float)
        chk.loc[have[have].index, "scored_by"] = "reasoning"
    return chk


def check_pass(long: pd.DataFrame, texts: pd.Series | None = None) -> pd.DataFrame:
    """case_id x 'pass:<arm>|<order>' -> 1.0 if the cell's check is right in both option orders (``correct`` of
    ``score_checks``), and 'passletter:<arm>|<order>' with the release's letter scoring."""
    chk = score_checks(long, texts)
    out = []
    for column, prefix in (("correct", "pass:"), ("correct_letter", "passletter:")):
        g = chk.groupby(["case_id", "arm", "block_order"])[column].agg(["min", "count"]).reset_index()
        g["passed"] = np.where(g["count"] == 2, (g["min"] == 1).astype(float), np.nan)
        g["col"] = prefix + g.arm + "|" + g.block_order
        out.append(g.pivot(index="case_id", columns="col", values="passed"))
    return pd.concat(out, axis=1)


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
# check-passed subsets: shown/withheld checks scored from the reasoning, and by the release's letter
CHECK_SUBSETS = {"check_passed": "pass:", "check_passed_letter": "passletter:"}


def cell_statistics(W: dict, arms, checks: bool) -> dict:
    """Every cell's statistics on one (resampled) set of patients."""
    out = {}
    for arm in arms:
        ks = [roles_for(arm, o) for o in B.ORDERS]
        for order, k in zip(B.ORDERS, ks):
            sub, n = _subset(W, k)
            if n >= MIN_CASES:
                out[(arm, order, "all")] = L.setting_statistics(sub, k) | {"n": n}
            for subset, prefix in CHECK_SUBSETS.items():
                if checks and f"{prefix}{arm}|{order}" in W:
                    sub, n = _subset(W, k, W[f"{prefix}{arm}|{order}"])
                    if n >= MIN_CASES:
                        out[(arm, order, subset)] = L.setting_statistics(sub, k) | {"n": n}
        sub, n = _stacked(W, ks)
        if n >= MIN_CASES:
            out[(arm, "pooled", "all")] = L.setting_statistics(sub, ks[0]) | {"n": n}
        for subset, prefix in CHECK_SUBSETS.items():
            if checks and all(f"{prefix}{arm}|{o}" in W for o in B.ORDERS):
                sub, n = _stacked(W, ks, [W[f"{prefix}{arm}|{o}"] for o in B.ORDERS])
                if n >= MIN_CASES:
                    out[(arm, "pooled", subset)] = L.setting_statistics(sub, ks[0]) | {"n": n}
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


def bootstrap_draws(frame: pd.DataFrame, arms, resamples: int, seed: int, checks=True):
    """Point estimates and the paired patient-bootstrap draws of every cell statistic and contrast."""
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
    draws = {key: {s: np.asarray(v, float) for s, v in stats.items()} for key, stats in draws.items()}
    cdraws = {name: np.asarray(v, float) for name, v in cdraws.items()}
    return points, cpoints, draws, cdraws


def summarise(points, cpoints, draws, cdraws, take=None):
    """(cells, contrasts) tables from ``bootstrap_draws``; ``take`` (an index array) summarises a subset of the
    draws, which is how the decision rules estimate their Monte Carlo error."""
    rows = []
    for (arm, order, subset), point in points.items():
        point = {s: v for s, v in point.items() if s != "n"}
        d = {s: (v if take is None else v[take]) for s, v in draws[(arm, order, subset)].items() if s != "n"}
        resamples = len(next(iter(d.values()))) if d else 0
        t = L._bootstrap_summary(point, d, points[(arm, order, subset)]["n"], resamples)
        rows.append(t.assign(arm=arm, order=order, subset=subset))
    cells = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    crow = []
    for name, value in cpoints.items():
        d = cdraws[name] if take is None else cdraws[name][take]
        total = len(d)
        d = d[np.isfinite(d)]
        lo, hi = np.percentile(d, [2.5, 97.5]) if len(d) else (np.nan, np.nan)
        crow.append(dict(contrast=name, estimate=value, ci_lower=lo, ci_upper=hi, usable_draws=len(d),
                         resamples=total))
    return cells, pd.DataFrame(crow)


def bootstrap(frame: pd.DataFrame, arms, resamples: int, seed: int, checks=True):
    return summarise(*bootstrap_draws(frame, arms, resamples, seed, checks))


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


def _beta_d_reason(cells, arm, order, subset="all"):
    r = _get(cells, arm, order, "beta_D", subset)
    return "" if r is not None and not (r.ci_lower <= 0 <= r.ci_upper) else \
        f"{arm}|{order}|{subset}: beta_D interval includes 0"


MAX_UNUSABLE_SHARE = 0.01  # a contrast whose draws are inestimable more often than this cannot decide a rule
RULE_RESAMPLES = 10_000  # the rules' own bootstrap; the tables use analysis.bootstrap_resamples
MC_BATCHES = 10  # Monte Carlo error of each deciding quantity, from this many disjoint batches of those draws


def _contrast_reason(c, name):
    if c is None:
        return f"no {name} contrast"
    if c.usable_draws < (1 - MAX_UNUSABLE_SHARE) * c.resamples:
        return f"{name}: {1 - c.usable_draws / c.resamples:.1%} of bootstrap draws inestimable"
    return ""


def _interval(r):
    return None if r is None else [float(r.estimate), float(r.ci_lower), float(r.ci_upper)]


def _triple(q, name):
    """[estimate, lower, upper] of quantity ``name`` (None where missing), for the evidence."""
    return [q.get(f"{name}.{s}") for s in ("est", "lo", "hi")]


def _lam(q, cells, name, arm, order, subset="all"):
    """Puts a cell's Fieller lambda into ``q`` (estimate, lower, upper); returns the reason it can't, or ''."""
    iv, why = lambda_interval(cells, arm, order, subset)
    if iv is not None:
        q[f"{name}.est"], q[f"{name}.lo"], q[f"{name}.hi"] = iv
    return f"{arm}|{order}|{subset}: {why}" if why else ""


def _con(q, contrasts, name, contrast):
    c = _contrast(contrasts, contrast)
    why = _contrast_reason(c, contrast)
    if c is not None:
        q[f"{name}.est"], q[f"{name}.lo"], q[f"{name}.hi"] = _interval(c)
    return why


# Each rule: (cells, contrasts) -> (quantities, reasons it is undetermined, evidence); its decide(q) gives
# (supported, refuted) from the quantities alone, so the Monte Carlo check can perturb them.
def _h1(cells, contrasts, arms):
    """The reduction clause is decided on false_labelled - false_neutral, which isolates labelling (both arms drop
    the cue and share A and B_F_neutral); false_labelled - false_verified also changes the cue and the own-belief
    pair, and is reported beside it."""
    q, reasons = {}, []
    for o in B.ORDERS:
        reasons.append(_lam(q, cells, o, "false_labelled", o))
    reasons.append(_con(q, contrasts, "order", "order:false_labelled"))
    reasons.append(_con(q, contrasts, "reduction", "false_labelled-false_neutral:person_first"))
    reasons += [_beta_d_reason(cells, a, "person_first") for a in ("false_neutral", "false_verified")]
    prereg = _contrast(contrasts, "false_labelled-false_verified:person_first")
    evidence = {o: _triple(q, o) for o in B.ORDERS} | {
        "order_difference": _triple(q, "order"),
        "labelling_reduction_person_first": _triple(q, "reduction"),
        "reduction_contrast": "false_labelled-false_neutral:person_first",
        "plan_reduction_false_labelled_minus_false_verified": _interval(prereg)}
    return q, [r for r in reasons if r], evidence


def _h1_decide(q):
    supported = all(q[f"{o}.lo"] >= -0.1 and q[f"{o}.hi"] <= 0.1 for o in B.ORDERS) \
        and q["order.lo"] > -0.2 and q["order.hi"] < 0.2 and q["reduction.hi"] < 0
    refuted = any(q[f"{o}.lo"] > 0.1 or q[f"{o}.hi"] < -0.1 for o in B.ORDERS) \
        or q["order.lo"] >= 0.2 or q["order.hi"] <= -0.2 or q["reduction.lo"] >= 0
    return supported, refuted


def _h2(variant, subset="check_passed"):
    def rule(cells, contrasts, arms):
        q = {}
        arm = strictest(arms, variant)
        reasons = [_lam(q, cells, "lam", arm, "pooled", subset)]
        evidence = {"cell": arm, "subset": subset,
                    f"lambda_{subset}_pooled": _triple(q, "lam")}
        return q, [r for r in reasons if r], evidence
    return rule


def _h2_decide(q):
    return q["lam.lo"] > 0.15, q["lam.hi"] <= 0.15


def _h3(variant):
    def rule(cells, contrasts, arms):
        q = {}
        arm = strictest(arms, variant)
        reasons = [_con(q, contrasts, "drop", f"{variant}_labelled-{variant}_labelled_explicit:pooled"),
                   _lam(q, cells, "strict", arm, "pooled")]
        reasons += [_beta_d_reason(cells, a, "pooled") for a in (f"{variant}_labelled", f"{variant}_labelled_explicit")]
        evidence = {"drop": _triple(q, "drop"), "strictest": arm, "strictest_lambda": _triple(q, "strict")}
        return q, [r for r in reasons if r], evidence
    return rule


def _h3_decide(q):
    supported = q["drop.est"] > 0.3 and q["drop.lo"] > 0 and q["strict.est"] <= 0.1
    refuted = q["drop.hi"] < 0.3 or q["strict.lo"] > 0.1
    return supported, refuted


def _status(decide, q):
    supported, refuted = decide(q)
    return "supported" if supported else "refuted" if refuted else "inconclusive"


def run_rule(rule, decide, cells, contrasts, arms, batches=()) -> dict:
    """The rule's verdict; with ``batches`` (``summarise`` on disjoint subsets of the same draws), the Monte Carlo
    s.e. of every deciding quantity and ``near_threshold``: the quantities whose move by 2 s.e. changes the
    verdict (the verdict then rests on bootstrap noise, and is reported as not robust)."""
    q, reasons, evidence = rule(cells, contrasts, arms)
    if reasons:
        return _verdict(False, False, reasons) | evidence
    out = _verdict(*decide(q), []) | evidence
    if batches:
        values = {k: [] for k in q}
        for bc, bk in batches:
            qb, rb, _ = rule(bc, bk, arms)
            for k in q:
                values[k].append(qb.get(k, np.nan))
        mcse = {}
        for k, v in values.items():
            v = np.asarray(v, float)
            v = v[np.isfinite(v)]
            mcse[k] = float(np.std(v, ddof=1) / np.sqrt(len(v))) if len(v) > 1 else np.nan
        near = []
        for k, se in mcse.items():
            if np.isfinite(se) and any(_status(decide, q | {k: q[k] + s * se}) != out["status"] for s in (-2, 2)):
                near.append(k)
        out |= {"monte_carlo_se": {k: round(v, 5) for k, v in mcse.items()}, "near_threshold": near,
                "robust": not near}
    return out


def decision_rules(cells, contrasts, arms, batches=(), exploratory=None) -> dict:
    """The pre-registered rules on (cells, contrasts), with the Monte Carlo check where ``batches`` are given.
    ``exploratory`` (cells, contrasts): the strong variant's H2 and H3, which are not pre-registered and are
    reported as exploratory."""
    out = {}
    if {"false_labelled", "false_verified", "false_neutral"} <= set(arms):
        out["H1_false_misbinding"] = run_rule(_h1, _h1_decide, cells, contrasts, arms, batches)
    if strictest(arms, "partial"):
        h2 = run_rule(_h2("partial"), _h2_decide, cells, contrasts, arms, batches)
        # the same rule on every patient and on the release's letter-scored check, for comparison
        others = {s: run_rule(_h2("partial", s), _h2_decide, cells, contrasts, arms)
                  for s in ("all", "check_passed_letter")}
        h2["other_subsets"] = {s: {"status": r["status"], "lambda": r.get(f"lambda_{s}_pooled")}
                               for s, r in others.items()}
        h2["subset_dependent"] = any(r["status"] != h2["status"] for r in others.values())
        out["H2_curse_of_knowledge[partial]"] = h2
    if {"partial_labelled", "partial_labelled_explicit"} <= set(arms) and strictest(arms, "partial"):
        out["H3_absence_not_ignorance[partial]"] = run_rule(_h3("partial"), _h3_decide, cells, contrasts, arms,
                                                            batches)
    if exploratory is not None:
        ec, ek = exploratory
        if strictest(arms, "strong"):
            out["H2_curse_of_knowledge[strong, exploratory]"] = run_rule(
                _h2("strong"), _h2_decide, ec, ek, arms) | {"exploratory": True}
        if {"strong_labelled", "strong_labelled_explicit"} <= set(arms) and strictest(arms, "strong"):
            out["H3_absence_not_ignorance[strong, exploratory]"] = run_rule(
                _h3("strong"), _h3_decide, ec, ek, arms) | {"exploratory": True}
    return out


# The cells the pre-registered rules read; they get their own, larger bootstrap.
RULE_ARMS = ("false_labelled", "false_verified", "false_neutral", "partial_labelled", "partial_labelled_explicit",
             "partial_instructed_explicit")


def rule_tables(frame, arms, resamples, seed):
    """(cells, contrasts, batches) for the rule arms: one bootstrap of ``resamples`` draws, summarised whole and
    in ``MC_BATCHES`` disjoint batches."""
    rule_arms = [a for a in RULE_ARMS if a in arms]
    if not rule_arms:
        return None
    draws = bootstrap_draws(frame, rule_arms, resamples, seed)
    cells, contrasts = summarise(*draws)
    batches = [summarise(*draws, take=idx) for idx in np.array_split(np.arange(int(resamples)), MC_BATCHES)]
    return cells, contrasts, batches


def pilot_decisions(cells, retention, checks_table, n_patients, n_full=150) -> dict:
    """What the pilot reports before the full run: retention under the new prompts, check accuracy per arm, the
    manipulation size of each variant, and the lambda interval widths projected to 150 patients."""
    out = {}
    if not retention.empty:
        low = retention[retention.retention < 0.9]
        out["prompt_retention_ok"] = dict(decision=bool(low.empty), rule="every arm x role keeps >= 90% parsed",
                                          below=low.to_dict("records"))
    if not checks_table.empty:  # item-weighted over the order x relation strata
        g = checks_table.assign(w=checks_table.accuracy * checks_table.n,
                                wl=checks_table.accuracy_letter * checks_table.n).groupby("arm")
        out["check_accuracy_by_arm"] = (g.w.sum() / g.n.sum()).round(3).to_dict()
        out["check_accuracy_by_arm_letter"] = (g.wl.sum() / g.n.sum()).round(3).to_dict()
    gaps = cells[(cells.statistic == "manipulation_median_abs_gap") & (cells.order == "pooled") & (cells.subset == "all")]
    out["median_abs_gap_by_arm"] = {r.arm: round(float(r.estimate), 3) for r in gaps.itertuples()}
    scale = float(np.sqrt(n_patients / n_full))
    # the rules read Fieller sets, so those are what is projected (bounded sets only)
    half = cells[(cells.statistic == "lambda_fieller") & (cells.subset == "all") & (cells.interval == "bounded")].assign(
        half_width=lambda t: (t.ci_upper - t.ci_lower) / 2)
    out["projected_lambda_fieller_half_width_at_150"] = {
        f"{r.arm}|{r.order}": round(float(r.half_width * scale), 3) for r in half.itertuples() if np.isfinite(r.half_width)}
    return out


# Other tables
def retention_table(long):
    part = long[long.arm != "drift"]
    g = part.assign(parsed=part.status == "parsed", truncated=part.status == "truncated").groupby(["arm", "role"])
    return pd.DataFrame({"scheduled": g.size(), "parsed": g.parsed.sum(), "truncated": g.truncated.sum()}
                        ).assign(retention=lambda t: t.parsed / t.scheduled).reset_index()


def checks_table(long, texts=None):
    """Check accuracy per arm, order and relation: ``accuracy`` as scored for the check-passed subsets,
    ``accuracy_letter`` by the release's letter, and the share scored from the reasoning."""
    chk = score_checks(long, texts)
    chk = chk[chk.correct_letter.notna()]
    if chk.empty:
        return pd.DataFrame()
    chk = chk.assign(by_reasoning=(chk.scored_by == "reasoning").astype(float))
    return chk.groupby(["arm", "block_order", "check_relation"]).agg(
        accuracy=("correct", "mean"), accuracy_letter=("correct_letter", "mean"),
        scored_from_reasoning=("by_reasoning", "mean"), n=("correct", "size")).reset_index()


def drift_table(long, resamples, seed):
    d = long[long.arm == "drift"]
    if d.empty:
        return pd.DataFrame()
    base = long[long.arm != "drift"].drop_duplicates("item_key").set_index("item_key")
    d = d.assign(base_value=d.drift_of.map(base.value), base_status=d.drift_of.map(base.status))
    d = d[(d.status == "parsed") & (d.base_status == "parsed")].copy()
    d["kind"] = np.where(d.output.str.split("/").str[0] == "probability", "probability", "choice")
    rows = []
    rng = np.random.default_rng(seed)
    for kind, part in d.groupby("kind"):
        if kind == "probability":
            diff = clip_logit(part.value.astype(float)) - clip_logit(part.base_value.astype(float))
        else:
            diff = part.value.astype(float).to_numpy() - part.base_value.astype(float).to_numpy()
        frame = pd.DataFrame({"case": part.case_id.to_numpy(), "diff": diff})
        per = frame.groupby("case")["diff"].mean().to_numpy()
        boots = rng.choice(per, (resamples, len(per))).mean(axis=1)
        rows.append(dict(kind=kind, n=len(part), patients=len(per), mean_difference=float(np.mean(diff)),
                         ci_lower=float(np.percentile(boots, 2.5)), ci_upper=float(np.percentile(boots, 97.5)),
                         scale="logit (new - baseline)" if kind == "probability" else "answer value (new - baseline)"))
    return pd.DataFrame(rows)


DRIFT_LOGIT = 0.1  # a stated-probability drift beyond this (CI excluding 0) is flagged as an anomaly


def anomalies(drift: pd.DataFrame, decisions: dict) -> list[str]:
    """What RESULTS.md lists under Anomalies: drift between the reused and the new answers, rules that rest on
    bootstrap noise or depend on how the check is scored, and H1 verdicts that change without reused answers."""
    out = []
    for r in drift.itertuples() if not drift.empty else ():
        if r.kind == "probability" and not (r.ci_lower <= 0 <= r.ci_upper) and abs(r.mean_difference) > DRIFT_LOGIT:
            out.append(f"drift: new - reused stated probability {r.mean_difference:+.3f} logit "
                       f"[{r.ci_lower:+.3f}, {r.ci_upper:+.3f}] ({r.patients} patients): the reused answers in the "
                       "follow-up arms (false_verified, partial_original) and in A/B are from another session")
    for method, rules in decisions.items():
        for name, r in rules.items():
            if r.get("near_threshold"):
                out.append(f"{method} / {name}: {r['status']}, but within 2 Monte Carlo s.e. of a threshold "
                           f"({', '.join(r['near_threshold'])})")
            if r.get("subset_dependent"):
                out.append(f"{method} / {name}: the verdict depends on the patient subset "
                           f"({r['status']} on check_passed; "
                           + ", ".join(f"{s}: {o['status']}" for s, o in r["other_subsets"].items()) + ")")
            sens = r.get("sensitivity_new_answers_only")
            if sens and sens.get("status") != r["status"]:
                out.append(f"{method} / {name}: {r['status']}, but {sens.get('status')} without the reused answers")
    return out


def gate(reasoning, no_reasoning, arms) -> pd.DataFrame:
    """Phase 3 gate on one set of patients (both arguments: ``bootstrap``'s (cells, contrasts)). A cell enters
    Phase 3 if its no-reasoning lambda is estimable (bounded Fieller set), its interval overlaps the reasoning
    one, and, where the reasoning order effect is clear (CI excluding 0), the no-reasoning one has its sign.
    Both lambda intervals are 95% Fieller sets."""
    (rc, rk), (nc, nk) = reasoning, no_reasoning
    rows = []
    for arm in arms:
        oa, ob = _contrast(rk, f"order:{arm}"), _contrast(nk, f"order:{arm}")
        clear = oa is not None and not (oa.ci_lower <= 0 <= oa.ci_upper)
        same = not clear or (ob is not None and np.sign(oa.estimate) == np.sign(ob.estimate))
        for order in B.ORDERS:
            if _get(rc, arm, order) is None:
                continue
            a, why_a = lambda_interval(rc, arm, order)
            b, why = lambda_interval(nc, arm, order)
            overlap = a is not None and b is not None and max(a[1], b[1]) <= min(a[2], b[2])
            reason = "; ".join(x for x in (f"reasoning: {why_a}" if why_a else "",
                                           f"no reasoning: {why}" if why else "") if x)
            rows.append(dict(arm=arm, order=order, lambda_reasoning=np.nan if a is None else a[0],
                             lambda_no_reasoning=np.nan if b is None else b[0], no_reasoning_estimable=b is not None,
                             intervals_overlap=bool(overlap), order_effect_consistent=bool(same),
                             enters_phase3=bool(a is not None and b is not None and overlap and same), reason=reason))
    return pd.DataFrame(rows)


TRUNCATION_FLAG = 0.05  # an arm whose stated-probability answers are truncated more often than this is flagged


def truncation_anomalies(retention: pd.DataFrame) -> list[str]:
    """Truncated answers are never retried (every item is seeded from its key, so a retry repeats them); an arm
    that truncates more than the others loses its longest answers selectively."""
    if retention.empty or "truncated" not in retention:
        return []
    part = retention[retention.role.isin(["C", "M", "D"])]
    g = part.groupby("arm")[["truncated", "scheduled"]].sum()
    share = g.truncated / g.scheduled
    bad = share[share > TRUNCATION_FLAG]
    return [f"truncation: {arm} loses {s:.1%} of its C/M/D answers to the token limit (arms range "
            f"{share.min():.1%}-{share.max():.1%})" for arm, s in bad.items()]


def write_results(out: Path, model, plan, tables, decisions, pilot, flags=()) -> None:
    cells = tables["lambda_cells"]
    lines = [f"# Binding experiment: {model} ({plan['spec']['profile']} profile)", "",
             f"{len(plan['cases'])} patients; arms: {', '.join(plan['spec']['arms'])}.", "",
             "## lambda per cell (95% paired patient-bootstrap intervals)", "",
             "`lambda` has the percentile interval and `lambda Fieller` the 95% Fieller set, which is what the "
             "rules read. `check_passed`: right on both option orders of the cell's check, the shown/withheld checks "
             "scored from the reasoning; `check_passed_letter`: the release's letter scoring. `n` is "
             "patients per order and patient-orders when pooled.", "",
             "| method | arm | order | subset | n | lambda | lambda Fieller | beta_C | beta_D | median abs x |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for (method, arm, order, subset), part in cells.groupby(["method", "arm", "order", "subset"], sort=True):
        s = part.set_index("statistic")

        def fmt(stat):
            if stat not in s.index or not np.isfinite(s.loc[stat, "estimate"]):
                return s.loc[stat, "blank_reason"] if stat in s.index and s.loc[stat, "blank_reason"] else ""
            r = s.loc[stat]
            if stat == "lambda_fieller" and r.get("interval") != "bounded":
                return f"{r.get('interval') or 'not estimable'} (weak beta_D)"
            return f"{r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}]"
        n = int(s.loc["lambda", "n_cases"]) if "lambda" in s.index else ""
        lines.append(f"| {method} | {arm} | {order} | {subset} | {n} | {fmt('lambda')} | {fmt('lambda_fieller')} | "
                     f"{fmt('beta_C')} | {fmt('beta_D')} | {fmt('manipulation_median_abs_gap')} |")
    lines += ["", "## Contrasts of lambda (percentile intervals)", "", "| method | contrast | estimate |", "|---|---|---|"]
    for r in tables["contrasts"].itertuples():
        lines.append(f"| {r.method} | {r.contrast} | {r.estimate:.3f} [{r.ci_lower:.3f}, {r.ci_upper:.3f}] |")
    lines += ["", "## Decision rules (pre-registered)", "",
              f"The rules read a separate {RULE_RESAMPLES:,}-draw bootstrap of their cells (`lambda_cells_rules.csv`); "
              "`near_threshold` lists quantities whose move by 2 Monte Carlo s.e. would change the verdict.", ""]
    for method, rules in decisions.items():
        for name, result in rules.items():
            base = name.split("[")[0]
            evidence = {k: v for k, v in result.items() if k not in ("supported", "status")}
            tag = " (exploratory: not pre-registered)" if result.get("exploratory") else ""
            tag += "" if result.get("robust", True) else " (NOT ROBUST: near a threshold)"
            lines.append(f"- **{method} / {name}**: {result['status']}{tag}. Rule: {RULES.get(base, '')}. "
                         f"Evidence: `{json.dumps(evidence, default=float)}`")
    lines += ["", "## Anomalies", ""] + ([f"- {f}" for f in flags] or ["- none flagged"])
    if pilot:
        lines += ["", "## Pilot decisions", ""]
        for name, result in pilot.items():
            lines.append(f"- **{name}**: `{json.dumps(result, default=float)}`")
    (out / "RESULTS.md").write_text("\n".join(lines) + "\n")


def new_answers_only(long: pd.DataFrame) -> pd.DataFrame:
    """The responses without the C/M/D answers reused from the base run (another session); A and B, shared by
    every arm, are kept, since they enter both sides of every contrast."""
    if "source" not in long:
        return long
    return long[~((long.source == "baseline") & long.role.isin(["C", "M", "D"]))]


def analyse(root: Path, model: str, resamples: int = 2000, seed: int | None = None,
            order_followup: bool = False, rule_resamples: int = RULE_RESAMPLES) -> dict:
    long, plan = (load_order_followup if order_followup else load)(root, model)
    seed = int(C.load()["seed"]) if seed is None else seed
    arms = plan["spec"]["arms"]
    out = root / "outputs" / "analysis" / model
    out.mkdir(parents=True, exist_ok=True)
    beliefs = {"direct": direct_beliefs(long)}
    if plan["spec"]["latent_arms"]:
        beliefs["latent"] = latent_beliefs(long, plan, model)
    texts = load_texts(root, model)
    passes = check_pass(long, texts)
    all_cells, all_contrasts, rule_cells, decisions = [], [], [], {}
    for method, b in beliefs.items():
        b.to_parquet(out / f"beliefs_{method}.parquet", index=False)
        method_arms = arms if method == "direct" else plan["spec"]["latent_arms"]
        frame = wide_frame(b).join(passes, how="left")
        cells, contrasts = bootstrap(frame, method_arms, resamples, seed)
        cells["method"], contrasts["method"] = method, method
        all_cells.append(cells)
        all_contrasts.append(contrasts)
        ruled = rule_tables(frame, method_arms, rule_resamples, seed + 1)
        if ruled is None:
            decisions[method] = decision_rules(cells, contrasts, method_arms, exploratory=(cells, contrasts))
            continue
        rc, rk, batches = ruled
        rule_cells.append(rc.assign(method=method))
        decisions[method] = decision_rules(rc, rk, method_arms, batches, exploratory=(cells, contrasts))
        if method == "direct" and "H1_false_misbinding" in decisions[method]:
            # H1 without the C/M/D answers reused from the base run (a different session from the new arms')
            nb = direct_beliefs(new_answers_only(long))
            nc, nk = bootstrap(wide_frame(nb).join(passes, how="left"),
                               [a for a in ("false_labelled", "false_verified", "false_neutral") if a in method_arms],
                               resamples, seed + 2, checks=False)
            sens = run_rule(_h1, _h1_decide, nc, nk, method_arms)
            decisions[method]["H1_false_misbinding"]["sensitivity_new_answers_only"] = {
                k: sens.get(k) for k in ("status", "reason", "person_first", "own_first", "labelling_reduction_person_first")
                if k in sens}
    cells = pd.concat(all_cells, ignore_index=True)
    contrasts = pd.concat(all_contrasts, ignore_index=True)
    cells.to_csv(out / "lambda_cells.csv", index=False)
    contrasts.to_csv(out / "contrasts.csv", index=False)
    if rule_cells:
        pd.concat(rule_cells, ignore_index=True).to_csv(out / "lambda_cells_rules.csv", index=False)
    ret, chk = retention_table(long), checks_table(long, texts)
    ret.to_csv(out / "retention.csv", index=False)
    chk.to_csv(out / "checks.csv", index=False)
    drift = drift_table(long, resamples, seed)
    drift.to_csv(out / "drift.csv", index=False)
    flags = anomalies(drift, decisions) + truncation_anomalies(ret)
    pilot = (pilot_decisions(cells[cells.method == "direct"], ret, chk, len(plan["cases"]))
             if plan["spec"]["profile"] == "pilot" else {})
    result = {"decision_rules": decisions, "anomalies": flags, "pilot_decisions": pilot}
    (out / "decisions.json").write_text(json.dumps(result, indent=2, default=float))
    write_results(out, model, plan, {"lambda_cells": cells, "contrasts": contrasts}, decisions, pilot, flags)
    print(f"{model}: analysis written to {out}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--resamples", type=int, default=None)
    parser.add_argument("--rule-resamples", type=int, default=RULE_RESAMPLES,
                        help="draws of the decision rules' own bootstrap (CPU only)")
    parser.add_argument("--order-followup", action="store_true", help="the run is an order_followup plan")
    args = parser.parse_args()
    root = args.run.resolve()
    (run_plan.read_plan if args.order_followup else B.read_plan)(root)  # points the config at the run
    resamples = args.resamples or int(C.load()["analysis"]["bootstrap_resamples"])
    analyse(root, args.model, resamples, order_followup=args.order_followup, rule_resamples=args.rule_resamples)


if __name__ == "__main__":
    main()
