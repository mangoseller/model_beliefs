"""Egocentric-leakage estimators and the paired case bootstrap (method-agnostic).

Inputs are rows of the shared belief format.  Per method, ``HALVES`` names
which halves play each role, so the estimator code does not depend on the
method:

* ``x``: the B belief on the regressor side; ``y``: the independent B belief
  on the outcome side (latent: prob / dec; probe: replay halves h1 / h2).
* ``null1``, ``null2``, ``null_x``: halves for the null regression.

Primary estimator (per framing f in {john, user}):

    ell_C* - ell_B[y]* = c + lambda * (ell_A - ell_B[x]) + e

where * marks the outcome-side beliefs read from ``value_calibrated`` (for
the latent method, posterior means de-shrunk with each fit's calibration
line; see ``measurement.py``; for other methods identical to ``value``).
Without it, unequal shrinkage of ell_C (16 outputs) and ell_B[dec] (13
decisions) biases lambda; the synthetic recovery test showed this.

plus the ceiling (ell_D in place of ell_C), the null
(ell_B[null1] - ell_B[null2] on ell_A - ell_B[null_x]), lambda_C / lambda_D,
the regressor reliability 1 - mean(sd_A^2 + sd_B[x]^2) / var(ell_A - ell_B[x]),
two-weight regressions (ell_C on ell_A and ell_B), the closer-to share, and
user - John differences.  ``direct`` has no halves: only two-weight fits.

Uncertainty: paired bootstrap over cases (same resampled cases for every
statistic), 95% percentile intervals.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

HALVES = {
    "latent": {"full": "all", "x": "prob", "y": "dec", "null1": "dec_h1", "null2": "dec_h2",
               "null_x": "prob"},
    "probe": {"full": "all", "x": "h1", "y": "h2", "null1": "h1", "null2": "h2", "null_x": "all"},
    "direct": {"full": "all"},
}
FRAMINGS = ("john", "user")


def wide(beliefs: pd.DataFrame, method: str) -> pd.DataFrame:
    """case_id x '{condition}:{half}' (value), ':sd' and ':cal' (value_calibrated)."""
    b = beliefs.copy()
    b["col"] = b["condition"] + ":" + b["half"]
    value = b.pivot_table(index="case_id", columns="col", values="value", aggfunc="first")
    parts = [value]
    for column, suffix in (("sd", "sd"), ("value_calibrated", "cal")):
        extra = b.pivot_table(index="case_id", columns="col", values=column, aggfunc="first",
                              dropna=False)
        extra.columns = [f"{c}:{suffix}" for c in extra.columns]
        parts.append(extra)
    return parts[0].join(parts[1:])


def ols(y: np.ndarray, *xs: np.ndarray) -> np.ndarray:
    """Intercept first, then one weight per regressor."""
    X = np.column_stack([np.ones_like(y), *xs])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return coef


def _col(W: dict[str, np.ndarray], key: str) -> np.ndarray:
    return W[key] if key in W else np.full(len(next(iter(W.values()))), np.nan)


def statistics(W: dict[str, np.ndarray], method: str) -> dict[str, float]:
    h = HALVES[method]
    full = h["full"]
    A, B = _col(W, f"A:{full}"), _col(W, f"B:{full}")
    out: dict[str, float] = {"manipulation_median_abs_A_minus_B": float(np.median(np.abs(A - B)))}
    if method == "direct":
        for f in FRAMINGS:
            for cond in ("C", "D"):
                y = _col(W, f"{cond}_{f}:{full}")
                if np.all(np.isnan(y)):
                    continue
                c, wa, wb = ols(y, A, B)
                out.update({f"{cond}_{f}_two_weight_intercept": c, f"{cond}_{f}_two_weight_wA": wa,
                            f"{cond}_{f}_two_weight_wB": wb})
        _person(out, W, full)
        return out
    Bx, By = _col(W, f"B:{h['x']}"), _col(W, f"B:{h['y']}:cal")
    x = A - Bx
    out["manipulation_median_abs_A_minus_Bx"] = float(np.median(np.abs(x)))
    sdA, sdBx = _col(W, f"A:{full}:sd"), _col(W, f"B:{h['x']}:sd")
    out["reliability"] = float(1 - np.mean(sdA**2 + sdBx**2) / np.var(x, ddof=1))
    y0 = _col(W, f"B:{h['null1']}:cal") - _col(W, f"B:{h['null2']}:cal")
    x0 = A - _col(W, f"B:{h['null_x']}")
    out["lambda_0_intercept"], out["lambda_0"] = ols(y0, x0)
    big = np.abs(x) > np.median(np.abs(x))
    for f in FRAMINGS:
        Cf, Df = _col(W, f"C_{f}:{full}"), _col(W, f"D_{f}:{full}")
        if np.all(np.isnan(Cf)):
            continue
        Cc, Dc = _col(W, f"C_{f}:{full}:cal"), _col(W, f"D_{f}:{full}:cal")
        out[f"c_C_{f}"], out[f"lambda_C_{f}"] = ols(Cc - By, x)
        out[f"c_D_{f}"], out[f"lambda_D_{f}"] = ols(Dc - By, x)
        out[f"normalised_lambda_{f}"] = out[f"lambda_C_{f}"] / out[f"lambda_D_{f}"]
        for cond, y in (("C", Cf), ("D", Df)):
            c, wa, wb = ols(y, A, B)
            out.update({f"{cond}_{f}_two_weight_intercept": c, f"{cond}_{f}_two_weight_wA": wa,
                        f"{cond}_{f}_two_weight_wB": wb})
        By_raw = _col(W, f"B:{h['y']}")
        out[f"closer_to_B_share_{f}"] = float(np.mean(np.abs(Cf - By_raw)[big] < np.abs(Cf - A)[big]))
    _person(out, W, full)
    return out


def _person(out: dict[str, float], W: dict[str, np.ndarray], full: str) -> None:
    for cond in ("C", "D"):
        u, j = _col(W, f"{cond}_user:{full}"), _col(W, f"{cond}_john:{full}")
        if np.all(np.isnan(u)) or np.all(np.isnan(j)):
            continue
        out[f"mean_shift_{cond}_user_minus_john"] = float(np.mean(u - j))
    for stat in ("lambda_C", "c_C", "lambda_D", "c_D", "normalised_lambda", "C_two_weight_wA",
                 "C_two_weight_intercept"):
        if stat.startswith("C_two"):
            a, b = f"C_user{stat[1:]}", f"C_john{stat[1:]}"
        else:
            a, b = f"{stat}_user", f"{stat}_john"
        if a in out and b in out:
            out[f"diff_user_minus_john_{stat}"] = out[a] - out[b]


def bootstrap(frame: pd.DataFrame, fn: Callable[[dict[str, np.ndarray]], dict[str, float]], *,
              resamples: int, seed: int) -> pd.DataFrame:
    """Point estimates and 95% percentile intervals from a paired case bootstrap."""
    arrays = {c: frame[c].to_numpy(dtype=float) for c in frame.columns}
    point = fn(arrays)
    rng = np.random.default_rng(seed)
    n = len(frame)
    draws: dict[str, list[float]] = {k: [] for k in point}
    for _ in range(int(resamples)):
        idx = rng.integers(0, n, n)
        stats = fn({c: v[idx] for c, v in arrays.items()})
        for k in point:
            draws[k].append(stats.get(k, np.nan))
    rows = []
    for k, v in point.items():
        d = np.asarray(draws[k], dtype=float)
        d = d[np.isfinite(d)]
        lo, hi = np.percentile(d, [2.5, 97.5]) if len(d) else (np.nan, np.nan)
        rows.append({"statistic": k, "estimate": float(v), "ci_lower": float(lo),
                     "ci_upper": float(hi), "n_cases": n, "resamples": int(resamples)})
    return pd.DataFrame(rows)


def leakage_table(beliefs: pd.DataFrame, method: str, *, resamples: int, seed: int,
                  required: list[str] | None = None) -> pd.DataFrame:
    """Bootstrap table on cases with every needed belief present."""
    W = wide(beliefs, method)
    h = HALVES[method]
    needed = required or [f"A:{h['full']}", f"B:{h['full']}"]
    W = W.dropna(subset=[c for c in needed if c in W.columns])
    value_cols = [c for c in W.columns if not c.endswith((":sd", ":cal"))]
    W = W.dropna(subset=value_cols)
    if len(W) < 5:
        return pd.DataFrame()
    return bootstrap(W, lambda a: statistics(a, method), resamples=resamples, seed=seed)
