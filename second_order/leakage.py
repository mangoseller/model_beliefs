"""Leakage estimators and the paired case bootstrap (plan, Phase 5).  Method-agnostic.

Inputs are rows of the shared belief format.  Everything is computed per
(framing, pair), where a pair names how the agent's copy differs from the
full profile (``conditions.pair_conditions`` gives the A, B, C, M, D keys).

Matched-control estimator (headline; RQ1, RQ2 ladder, RQ3)
----------------------------------------------------------
With x = l_A - l_B (the model's own gap, raw, all outputs):

    T_C = slope of (l_D - l_C) on x
    T_M = slope of (l_D - l_M) on x
    lambda = 1 - T_C / T_M          (0 = no leakage, 1 = reports its own belief)

C, M and D are answers about the agent from one fit, so attribution effects
and shrinkage cancel; x comes from different model calls, so its noise is
independent of the outcomes and scales T_C and T_M equally.  ``mean_position``
= mean(l_C - l_M) / mean(l_D - l_M) is the level counterpart (a uniform shift
towards the model's own view shows there, not in lambda).

For probes, pass ``regressor=`` latent beliefs: the outcomes are probe
readings and x is the behavioural gap.

Tracking and compression (latent method only; needs independent halves)
-----------------------------------------------------------------------
With x_p = l_A^prob - l_B^prob and calibrated decision-only latents:

    R     = slope of (l_A^dec - l_B^dec) on x_p     (what a full response looks like)
    tau   = [slope of (l_D^dec - l_C^dec) on x_p] / R      = kappa (1 - lambda)
    kappa = [slope of (l_D^dec - l_M^dec) on x_p] / R      (needs M)

Pairs without a matched control report tau, and lambda with kappa borrowed
from S4 (``contrasts``).

Also reported: the original cross-framing estimator (``*_original``; biased
when kappa != 1, kept as a secondary number), a noise check ``lambda_0``, the
share of gap variance captured by the posterior means, the anchor check
mean(l_M - l_B), and descriptive two-weight regressions.

Ratio rule: a ratio is blanked when its denominator's interval includes 0.
Uncertainty: paired bootstrap over cases, 95% percentile intervals; item
parameters are fixed, so intervals are conditional on the fit.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from second_order.conditions import framing_pairs, pair_conditions

# Which half plays which role, per method.  Only the latent method has
# independent halves (reports vs decisions, and two decision halves).
HALVES = {
    "latent": {"full": "all", "prob": "prob", "dec": "dec", "null1": "dec_h1", "null2": "dec_h2"},
    "direct": {"full": "all"},
    "probe": {"full": "all"},
}
# Ratio statistic -> the denominator whose interval must exclude 0.
DENOMINATOR = {
    "lambda": "T_M", "mean_position": "mean_D_minus_M", "tau": "R", "kappa": "R",
    "normalised_lambda_original": "lambda_D_original", "lambda_borrowed_kappa": "R",
    "lambda_proportionality_low": "T_M_low", "lambda_proportionality_mid": "T_M_mid",
    "lambda_proportionality_high": "T_M_high",
}
X = "x|"  # prefix of regressor columns supplied from another method


def wide(beliefs: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """case_id x '{condition}:{half}' (value), plus ':sd' and ':cal' (value_calibrated)."""
    b = beliefs.copy()
    b["col"] = prefix + b["condition"] + ":" + b["half"]
    parts = [b.pivot_table(index="case_id", columns="col", values="value", aggfunc="first")]
    for column, suffix in (("sd", "sd"), ("value_calibrated", "cal")):
        extra = b.pivot_table(index="case_id", columns="col", values=column, aggfunc="first",
                              dropna=False)
        extra.columns = [f"{c}:{suffix}" for c in extra.columns]
        parts.append(extra)
    return parts[0].join(parts[1:])


def ols(y: np.ndarray, *xs: np.ndarray) -> np.ndarray:
    """Intercept first, then one weight per regressor."""
    design = np.column_stack([np.ones_like(y), *xs])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    return coef


def _ratio(num: float, den: float) -> float:
    return num / den if den != 0 else np.nan


def needed_columns(method: str, framing: str, pair: str, has_regressor: bool,
                   available: set[str]) -> list[str]:
    """Columns a case must have to enter this pair's analysis set.

    The matched control is required only if it was run (its column exists).
    """
    k = dict(pair_conditions(framing, pair))
    h = HALVES[method]
    if k["M"] and f"{k['M']}:{h['full']}" not in available:
        k["M"] = None
    cols = [f"{k[r]}:{h['full']}" for r in ("C", "D")] + ([f"{k['M']}:{h['full']}"] if k["M"] else [])
    own = [f"{k[r]}:{h['full']}" for r in ("A", "B")]
    cols += [X + c for c in own] if has_regressor else own
    if "dec" in h:
        cols += [f"{k[r]}:{half}" for r in ("A", "B") for half in (h["prob"], h["dec"])]
        cols += [f"{k[r]}:{h['dec']}" for r in ("C", "D")] + ([f"{k['M']}:{h['dec']}"] if k["M"] else [])
        cols += [f"{k['B']}:{h['null1']}", f"{k['B']}:{h['null2']}"]
    return cols


def pair_statistics(W: dict[str, np.ndarray], method: str, framing: str, pair: str,
                    calibrated: bool = False) -> dict[str, float]:
    """All statistics for one framing and pair on the cases in ``W``.

    ``calibrated`` reads the C, M, D outcomes from ``value_calibrated``: a required
    check on the pooled fit, and the only valid choice for per-condition fits,
    where each condition has its own prior and raw posterior means do not share shrinkage.
    """
    k = pair_conditions(framing, pair)
    h = HALVES[method]
    full = h["full"]
    has_m = k["M"] is not None and f"{k['M']}:{full}" in W
    px = X if X + f"A:{full}" in W else ""
    a, b = W[f"{px}A:{full}"], W[f"{px}{k['B']}:{full}"]
    x = a - b
    oc = ":cal" if calibrated else ""
    c, d = W[f"{k['C']}:{full}{oc}"], W[f"{k['D']}:{full}{oc}"]
    out: dict[str, float] = {
        "manipulation_median_abs_gap": float(np.median(np.abs(x))),
        "gap_sd": float(np.std(x, ddof=1)),
    }
    sd_a, sd_b = W.get(f"{px}A:{full}:sd"), W.get(f"{px}{k['B']}:{full}:sd")
    if sd_a is not None and sd_b is not None and np.isfinite(sd_a).all() and np.isfinite(sd_b).all():
        noise = float(np.mean(sd_a**2 + sd_b**2))
        out["gap_variance_share"] = float(np.var(x, ddof=1) / (np.var(x, ddof=1) + noise))

    # ---- matched-control estimator
    out["intercept_C"], out["T_C"] = ols(d - c, x)
    if has_m:
        m = W[f"{k['M']}:{full}{oc}"]
        out["intercept_M"], out["T_M"] = ols(d - m, x)
        out["lambda"] = 1 - _ratio(out["T_C"], out["T_M"])
        out["mean_D_minus_M"] = float(np.mean(d - m))
        out["mean_position"] = _ratio(float(np.mean(c - m)), out["mean_D_minus_M"])
        cal = ":cal" if f"{k['M']}:{full}:cal" in W else ""
        own_b = W[f"{k['B']}:{full}{cal}"] if f"{k['B']}:{full}{cal}" in W else b
        out["anchor_mean_M_minus_B"] = float(np.mean(W[f"{k['M']}:{full}{cal}"] - own_b))
        out["sd_C"], out["sd_M"], out["sd_D"] = (float(np.std(v, ddof=1)) for v in (c, m, d))
        # Proportionality: lambda within thirds of |x| (same cases in T_C and T_M).
        edges = np.quantile(np.abs(x), [1 / 3, 2 / 3])
        bins = np.digitize(np.abs(x), edges)
        for i, name in enumerate(("low", "mid", "high")):
            sel = bins == i
            if sel.sum() >= 50:
                t_c, t_m = ols((d - c)[sel], x[sel])[1], ols((d - m)[sel], x[sel])[1]
                out[f"T_M_{name}"] = t_m
                out[f"lambda_proportionality_{name}"] = 1 - _ratio(t_c, t_m)

    # ---- descriptive: C on the model's own two beliefs (attenuated; not a test)
    _, out["two_weight_wA"], out["two_weight_wB"] = ols(c, a, b)

    # ---- tracking, compression, noise check, original estimator (independent halves)
    if "dec" in h and not px:
        def cal(cond: str, half: str) -> np.ndarray:
            return W[f"{cond}:{half}:cal"]

        xp = W[f"A:{h['prob']}"] - W[f"{k['B']}:{h['prob']}"]
        r = ols(cal("A", h["dec"]) - cal(k["B"], h["dec"]), xp)[1]
        out["R"] = r
        out["tau"] = _ratio(ols(cal(k["D"], h["dec"]) - cal(k["C"], h["dec"]), xp)[1], r)
        if has_m:
            out["kappa"] = _ratio(ols(cal(k["D"], h["dec"]) - cal(k["M"], h["dec"]), xp)[1], r)
        out["lambda_0"] = ols(cal(k["B"], h["null1"]) - cal(k["B"], h["null2"]), xp)[1]
        x_orig = a - W[f"{k['B']}:{h['prob']}"]
        b_dec = cal(k["B"], h["dec"])
        out["lambda_C_original"] = ols(cal(k["C"], full) - b_dec, x_orig)[1]
        out["lambda_D_original"] = ols(cal(k["D"], full) - b_dec, x_orig)[1]
        out["normalised_lambda_original"] = _ratio(out["lambda_C_original"], out["lambda_D_original"])
    return out


# Cross-pair contrasts, each on the cases complete for both sides:
# (name, framing_1, pair_1, framing_2, pair_2, research question)
CONTRASTS = (
    ("F_minus_H", "john", "F", "john", "H", "RQ1c"),
    ("user_minus_john", "user", "S4", "john", "S4", "RQ1d"),
    ("S1_vs_S4", "john", "S1", "john", "S4", "RQ2"),
    ("H_vs_S4", "john", "H", "john", "S4", "RQ2"),
)


def contrast_statistics(W: dict[str, np.ndarray], method: str, name: str, f1: str, p1: str,
                        f2: str, p2: str, calibrated: bool = False) -> dict[str, float]:
    s1 = pair_statistics(W, method, f1, p1, calibrated)
    s2 = pair_statistics(W, method, f2, p2, calibrated)
    out: dict[str, float] = {}
    for stat in ("lambda", "tau", "T_C", "mean_position"):
        if stat in s1 and stat in s2:
            out[f"diff_{stat}"] = s1[stat] - s2[stat]
    # A pair without its own matched control borrows kappa from S4 (John only).
    if p2 == "S4" and f1 == f2 == "john" and "tau" in s1 and "kappa" in s2:
        out["R"] = s1["R"]
        out["lambda_borrowed_kappa"] = 1 - _ratio(s1["tau"], s2["kappa"])
    return out


def bootstrap(frame: pd.DataFrame, fn: Callable[[dict[str, np.ndarray]], dict[str, float]], *,
              resamples: int, seed: int) -> pd.DataFrame:
    """Point estimates, bootstrap medians and 95% percentile intervals (paired over cases)."""
    arrays = {c: frame[c].to_numpy(dtype=float) for c in frame.columns}
    point = fn(arrays)
    rng = np.random.default_rng(seed)
    n = len(frame)
    draws: dict[str, list[float]] = {key: [] for key in point}
    for _ in range(int(resamples)):
        idx = rng.integers(0, n, n)
        stats = fn({c: v[idx] for c, v in arrays.items()})
        for key in point:
            draws[key].append(stats.get(key, np.nan))
    rows = []
    for key, value in point.items():
        d = np.asarray(draws[key], dtype=float)
        d = d[np.isfinite(d)]
        lo, med, hi = np.percentile(d, [2.5, 50, 97.5]) if len(d) else (np.nan, np.nan, np.nan)
        rows.append({"statistic": key, "estimate": float(value), "boot_median": float(med),
                     "ci_lower": float(lo), "ci_upper": float(hi), "n_cases": n,
                     "resamples": int(resamples)})
    table = pd.DataFrame(rows)
    return apply_ratio_rule(table)


def apply_ratio_rule(table: pd.DataFrame) -> pd.DataFrame:
    """Blank a ratio whose denominator's interval includes 0 (fixed in advance)."""
    table = table.copy()
    table["blank_reason"] = ""
    by_stat = table.set_index("statistic")
    for i, row in table.iterrows():
        den = DENOMINATOR.get(row.statistic)
        if den is None or den not in by_stat.index:
            continue
        d = by_stat.loc[den]
        if not (d.ci_lower > 0 or d.ci_upper < 0):
            table.loc[i, ["estimate", "boot_median", "ci_lower", "ci_upper"]] = np.nan
            table.loc[i, "blank_reason"] = f"{den} interval includes 0"
    return table


def leakage_table(beliefs: pd.DataFrame, method: str, *, resamples: int, seed: int,
                  regressor: pd.DataFrame | None = None, calibrated: bool = False,
                  min_cases: int = 5) -> pd.DataFrame:
    """Long table: one row per (framing, pair, statistic), each pair on its own complete cases.

    ``regressor``: beliefs of another method (latent) supplying x = l_A - l_B,
    for methods whose own A/B readings must not be the regressor (probes).
    """
    W = wide(beliefs)
    if regressor is not None:
        W = W.join(wide(regressor, prefix=X), how="inner")
    has_x = regressor is not None
    tables = []

    def run(cols: list[str], fn, framing: str, pair: str, question: str = "") -> None:
        if any(c not in W.columns for c in cols):
            return
        part = W.dropna(subset=cols)
        if len(part) < min_cases:
            return
        t = bootstrap(part, fn, resamples=resamples, seed=seed)
        t.insert(0, "framing", framing)
        t.insert(1, "pair", pair)
        t["contrast_for"] = question
        tables.append(t)

    for framing, pair in framing_pairs():
        run(needed_columns(method, framing, pair, has_x, set(W.columns)),
            lambda a, f=framing, p=pair: pair_statistics(a, method, f, p, calibrated), framing, pair)
    for name, f1, p1, f2, p2, question in CONTRASTS:
        cols = sorted(set(needed_columns(method, f1, p1, has_x, set(W.columns))
                          + needed_columns(method, f2, p2, has_x, set(W.columns))))
        run(cols, lambda a, args=(name, f1, p1, f2, p2): contrast_statistics(a, method, *args, calibrated),
            "contrast", name, question)
    return pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()
