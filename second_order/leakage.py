"""The Methods estimators and the paired case bootstrap.

Inputs are rows of the shared belief format (``beliefs.py``).  For each setting
(partial, false) and each no-leakage reference of C (``conditions.references``: CKD's
two-record M2 and D2, the headline, and the single-record M and D of the Methods), with
per case x = l_A - l_B and x' the same gap from the second run (``replicate`` 1):

    beta_C = slope of (l_C - l_M) on x      beta_D = slope of (l_D - l_M) on x
    (M and D standing for the reference's two conditions)
    lambda = beta_C / beta_D                (Wald estimator; 0 = no leakage, 1 = full leakage)
    rho    = Cov(x, x') / Var(x)            (reliability of x)
    tau    = (beta_D - beta_C) / rho        (theory-of-mind score, = phi (1 - lambda))

phi, the responsiveness, is shared by the two settings (Methods): ``phi`` =
pooled beta_D / pooled rho, from within-setting slopes and covariances (so the
settings' intercepts may differ).  Each setting's own beta_D / rho is reported
as ``phi_setting``, a check of that assumption, with the difference
``diff_phi_setting`` and the differences in lambda, tau and rho.

rho, phi and tau are computed on calibrated values (``value_calibrated``):
for latent beliefs the posterior means shrink by different amounts in the
model's own fit and in PERSON's, which would otherwise scale phi.  lambda uses
the values as measured; C, M and D come from one fit, so their shrinkage cancels.

Checks of the modelling assumptions (none enters the headline numbers):

* affine form: reliability-corrected slopes of l_D on l_A and of l_M on l_B
  (instrumented by the second run; both estimate phi), and quadratic terms of
  the two gap regressions (zero under the model);
* partial linearity: lambda for x > 0 and x < 0 separately and within thirds
  of |x|, and the level ratio mean(l_C - l_M) / mean(l_D - l_M);
* anchoring: mean(l_M - l_B); manipulation: the median |x|.

Without a second run, only lambda and the checks that need no rho are reported.

Uncertainty: paired bootstrap over cases (patients or items), 95% percentile intervals;
latent item parameters are fixed, so intervals are conditional on the fit.  lambda also
gets a Fieller confidence set from the bootstrap covariance of beta_C and beta_D
(``lambda_fieller``), which is unbounded when beta_D is not clearly away from 0.  Ratio
rule: any other ratio is blanked when its denominator's interval includes 0.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from second_order.conditions import SETTINGS, references, setting_conditions

# Ratio statistic -> the denominator whose interval must exclude 0.
DENOMINATOR = {
    "mean_position": "mean_D_minus_M",
    "tau": "rho", "phi_setting": "rho", "phi_lambda": "rho",
    "phi_D_on_A": "cov_A_A1", "phi_M_on_B": "cov_B_B1", "phi": "rho_pooled",
    "lambda_x_positive": "beta_D_positive", "lambda_x_negative": "beta_D_negative",
    "lambda_third_low": "beta_D_low", "lambda_third_mid": "beta_D_mid", "lambda_third_high": "beta_D_high",
}
SECOND_RUN = "@r1"  # suffix of the second run's columns (replicate 1)
MIN_SIGN_CASES = 8  # cases needed on each side of 0 for lambda by the sign of the gap
MIN_THIRD_CASES = 50  # cases needed in each third of |x|
FIELLER_Z = 1.959964  # 95%


def wide(beliefs: pd.DataFrame) -> pd.DataFrame:
    """case_id x '{condition}[@r1]' (value), plus ':sd' and ':cal' (value_calibrated)."""
    b = beliefs.copy()
    run = pd.to_numeric(b["replicate"], errors="coerce").fillna(0).astype(int)
    b["col"] = b["condition"] + run.map(lambda r: f"@r{r}" if r else "")
    parts = [b.pivot_table(index="case_id", columns="col", values="value", aggfunc="first")]
    for column, suffix in (("sd", "sd"), ("value_calibrated", "cal")):
        extra = b.pivot_table(index="case_id", columns="col", values=column, aggfunc="first", dropna=False)
        extra.columns = [f"{c}:{suffix}" for c in extra.columns]
        parts.append(extra)
    return parts[0].join(parts[1:])


def ols(y: np.ndarray, *xs: np.ndarray) -> np.ndarray:
    """Intercept first, then one weight per regressor."""
    design = np.column_stack([np.ones_like(y), *xs])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    return coef


def slope(y: np.ndarray, x: np.ndarray) -> float:
    return float(ols(y, x)[1])


def _ratio(num: float, den: float) -> float:
    return num / den if den != 0 else np.nan


def _cov(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.cov(a, b, ddof=1)[0, 1])


def cal(W: dict[str, np.ndarray], key: str) -> np.ndarray:
    """A column's calibrated values if stored, else its values."""
    return W.get(f"{key}:cal", W[key])


def roles(dataset: str, setting: str, reference: str) -> dict[str, str]:
    """Condition keys for A, B, C and the reference's M and D in one setting."""
    k = setting_conditions(dataset, setting)
    m, d = references(dataset)[reference]
    return {"A": k["A"], "B": k["B"], "C": k["C"], "M": k[m], "D": k[d], "setting": setting}


def needed_columns(k: dict[str, str], available) -> list[str]:
    """Columns a case must have to enter an analysis set (the second run, if there is one)."""
    second = [k[r] + SECOND_RUN for r in ("A", "B")]
    return [k[r] for r in ("A", "B", "C", "M", "D")] + (second if set(second) <= set(available) else [])


def has_second_run(W, k: dict[str, str]) -> bool:
    return all(k[r] + SECOND_RUN in W for r in ("A", "B"))


def setting_statistics(W: dict[str, np.ndarray], k: dict[str, str]) -> dict[str, float]:
    """All statistics for one setting and reference (``roles``) on the cases in ``W``."""
    a, b, c, m, d = (W[k[r]] for r in ("A", "B", "C", "M", "D"))
    x = a - b
    out: dict[str, float] = {"manipulation_median_abs_gap": float(np.median(np.abs(x))),
                             "gap_sd": float(np.std(x, ddof=1))}

    # ---- leakage (values as measured)
    out["beta_C"], out["beta_D"] = slope(c - m, x), slope(d - m, x)
    out["lambda"] = _ratio(out["beta_C"], out["beta_D"])
    out["curvature_D"], out["curvature_C"] = ols(d - m, x, x**2)[2], ols(c - m, x, x**2)[2]
    partial_linearity(out, x, c, m, d)
    if not has_second_run(W, k):
        return out

    # ---- reliability, responsiveness and theory of mind (calibrated values)
    ca, cb, cc, cm, cd = (cal(W, k[r]) for r in ("A", "B", "C", "M", "D"))
    ca1, cb1 = cal(W, k["A"] + SECOND_RUN), cal(W, k["B"] + SECOND_RUN)
    xc, x1c = ca - cb, ca1 - cb1
    out["var_x"] = float(np.var(xc, ddof=1))
    out["var_delta"] = _cov(xc, x1c)
    out["rho"] = _ratio(out["var_delta"], out["var_x"])
    out["beta_C_calibrated"], out["beta_D_calibrated"] = slope(cc - cm, xc), slope(cd - cm, xc)
    out["tau"] = _ratio(out["beta_D_calibrated"] - out["beta_C_calibrated"], out["rho"])
    out["phi_setting"] = _ratio(out["beta_D_calibrated"], out["rho"])
    out["phi_lambda"] = _ratio(out["beta_C_calibrated"], out["rho"])  # where C sits, in units of delta

    # ---- affine form: levels, instrumented by the second run; anchoring
    out["cov_A_A1"], out["cov_B_B1"] = _cov(ca, ca1), _cov(cb, cb1)
    out["phi_D_on_A"] = _ratio(_cov(cd, ca1), out["cov_A_A1"])
    out["intercept_D_on_A"] = float(np.mean(cd) - out["phi_D_on_A"] * np.mean(ca))
    out["phi_M_on_B"] = _ratio(_cov(cm, cb1), out["cov_B_B1"])
    out["intercept_M_on_B"] = float(np.mean(cm) - out["phi_M_on_B"] * np.mean(cb))
    out["anchor_mean_M_minus_B"] = float(np.mean(cm - cb))
    return out


def partial_linearity(out: dict[str, float], x, c, m, d) -> None:
    """lambda by the sign of x and within thirds of |x|, and the level ratio."""
    for name, sel in (("positive", x > 0), ("negative", x < 0)):
        if sel.sum() >= MIN_SIGN_CASES and (~sel).sum() >= MIN_SIGN_CASES:
            out[f"beta_D_{name}"] = slope((d - m)[sel], x[sel])
            out[f"lambda_x_{name}"] = _ratio(slope((c - m)[sel], x[sel]), out[f"beta_D_{name}"])
    bins = np.digitize(np.abs(x), np.quantile(np.abs(x), [1 / 3, 2 / 3]))
    for i, name in enumerate(("low", "mid", "high")):
        sel = bins == i
        if sel.sum() >= MIN_THIRD_CASES:
            out[f"beta_D_{name}"] = slope((d - m)[sel], x[sel])
            out[f"lambda_third_{name}"] = _ratio(slope((c - m)[sel], x[sel]), out[f"beta_D_{name}"])
    out["mean_D_minus_M"] = float(np.mean(d - m))
    out["mean_position"] = _ratio(float(np.mean(c - m)), out["mean_D_minus_M"])


def shared_statistics(W: dict[str, np.ndarray], ks: dict[str, dict[str, str]]) -> dict[str, float]:
    """The shared phi and the differences between the settings, on cases complete for both."""
    per = {s: setting_statistics(W, k) for s, k in ks.items()}
    out = {f"diff_{stat}": per["false"][stat] - per["partial"][stat]
           for stat in ("lambda", "tau", "rho", "phi_setting") if stat in per["false"]}
    if not all(has_second_run(W, k) for k in ks.values()):
        return out
    sxy = sxx = cxx1 = 0.0
    for k in ks.values():
        x = cal(W, k["A"]) - cal(W, k["B"])
        x1 = cal(W, k["A"] + SECOND_RUN) - cal(W, k["B"] + SECOND_RUN)
        y = cal(W, k["D"]) - cal(W, k["M"])
        n1 = len(x) - 1
        sxy += _cov(y, x) * n1
        sxx += float(np.var(x, ddof=1)) * n1
        cxx1 += _cov(x, x1) * n1
    out["beta_D_pooled"], out["rho_pooled"] = _ratio(sxy, sxx), _ratio(cxx1, sxx)
    out["phi"] = _ratio(out["beta_D_pooled"], out["rho_pooled"])
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
    if "beta_C" in point and "beta_D" in point:
        rows.append(fieller(point["beta_C"], point["beta_D"], np.asarray(draws["beta_C"]),
                            np.asarray(draws["beta_D"])) | {"n_cases": n, "resamples": int(resamples)})
    return apply_ratio_rule(pd.DataFrame(rows))


def fieller(num: float, den: float, num_draws: np.ndarray, den_draws: np.ndarray) -> dict:
    """95% Fieller confidence set for num / den, with the bootstrap covariance of the two.

    ``interval``: ``bounded`` [lower, upper]; ``exclusive``, the whole line outside
    (lower, upper); or ``unbounded``, every value.
    """
    ok = np.isfinite(num_draws) & np.isfinite(den_draws)
    cov = np.cov(num_draws[ok], den_draws[ok]) if ok.sum() > 2 else np.full((2, 2), np.nan)
    q2 = FIELLER_Z**2
    a = den**2 - q2 * cov[1, 1]
    b = -2 * (num * den - q2 * cov[0, 1])
    c = num**2 - q2 * cov[0, 0]
    disc = b * b - 4 * a * c
    row = {"statistic": "lambda_fieller", "estimate": _ratio(num, den), "boot_median": np.nan}
    if not np.isfinite(disc):
        return row | {"ci_lower": np.nan, "ci_upper": np.nan, "interval": ""}
    if a > 0 and disc >= 0:
        roots = sorted(((-b - np.sqrt(disc)) / (2 * a), (-b + np.sqrt(disc)) / (2 * a)))
        return row | {"ci_lower": roots[0], "ci_upper": roots[1], "interval": "bounded"}
    if a < 0 and disc > 0:
        roots = sorted(((-b - np.sqrt(disc)) / (2 * a), (-b + np.sqrt(disc)) / (2 * a)))
        return row | {"ci_lower": roots[0], "ci_upper": roots[1], "interval": "exclusive"}
    return row | {"ci_lower": -np.inf, "ci_upper": np.inf, "interval": "unbounded"}


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


def leakage_table(beliefs: pd.DataFrame, dataset: str, *, resamples: int, seed: int,
                  min_cases: int = 5) -> pd.DataFrame:
    """Long table: one row per (setting, reference, statistic); setting ``both`` holds the shared phi.

    Each setting uses the cases complete for it; ``both`` uses the cases complete for the two.
    """
    W = wide(beliefs)
    tables = []

    def run(cols: list[str], fn, setting: str, reference: str) -> None:
        if any(c not in W.columns for c in cols):
            return
        part = W.dropna(subset=cols)
        if len(part) < min_cases:
            return
        t = bootstrap(part, fn, resamples=resamples, seed=seed)
        t.insert(0, "setting", setting)
        t.insert(1, "reference", reference)
        tables.append(t)

    for reference in references(dataset):
        ks = {s: roles(dataset, s, reference) for s in SETTINGS}
        for setting, k in ks.items():
            run(needed_columns(k, W.columns), lambda a, k=k: setting_statistics(a, k), setting, reference)
        run(sorted({c for k in ks.values() for c in needed_columns(k, W.columns)}),
            lambda a, ks=ks: shared_statistics(a, ks), "both", reference)
    return pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()
