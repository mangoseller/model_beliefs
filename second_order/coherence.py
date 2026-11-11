"""Coherence per model, condition and method (plan, Phase 5).

* ``latent``: the first-order evaluation, unchanged: for each condition, the
  pooled fit containing it is restored and
  ``latent_beliefs.evaluation.reciprocal.reciprocal_predictions`` /
  ``target_metrics`` give reports -> decisions and decisions -> reports
  predictions on the test split (mean c-index over targets, pooled R^2 against
  the best test constant).
* ``direct`` and ``probe``: one belief value per case; for each of the 13
  decisions a logistic link (decision ~ belief) is fitted on the condition's
  train-split rows and evaluated on its test rows (reports -> decisions only).

Intervals: paired bootstrap over test cases (the same resampled cases for
every condition), 95% percentile.  Contrasts A - D (attribution cost) and
D - C (perspective-taking cost) per framing come from the same draws.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from latent_beliefs.evaluation.reciprocal import (
    CONDITION_ORDER,
    condition_indices,
    reciprocal_predictions,
    target_metrics,
)
from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values
from second_order.fit_pooled import SUBSETS, restore

DECISIONS = SUBSETS["dec"]


def cindex(y: np.ndarray, s: np.ndarray) -> float:
    """Same definition as ``latent.model.concordance_index`` (ties score 1/2)."""
    dy = y[:, None] - y[None, :]
    ds = s[:, None] - s[None, :]
    usable = np.triu(dy != 0, 1)
    n = usable.sum()
    if n == 0:
        return np.nan
    conc = ((dy * ds) > 0) & usable
    ties = (ds == 0) & usable
    return float((conc.sum() + 0.5 * ties.sum()) / n)


def _summaries(pred: pd.DataFrame, idx: np.ndarray | None) -> dict[str, float]:
    """pred: long (case_pos, target, actual, prediction) -> mean c-index and pooled R^2."""
    cs, sse, sst = [], 0.0, 0.0
    for _, g in pred.groupby("target", sort=False):
        a = g["actual"].to_numpy(float)
        p = g["prediction"].to_numpy(float)
        if idx is not None:
            a, p = a[idx], p[idx]
        cs.append(cindex(a, p))
        sse += float(np.sum((a - p) ** 2))
        sst += float(np.sum((a - a.mean()) ** 2))
    return {"mean_c_index": float(np.nanmean(cs)) if np.isfinite(cs).any() else np.nan,
            "pooled_r2": 1 - sse / sst if sst > 0 else np.nan}


def latent_predictions(wide: pd.DataFrame, params: pd.DataFrame, cases: list[str]) -> dict:
    """(condition, direction) -> long predictions on test cases, ordered by ``cases``."""
    out: dict[tuple[str, str], pd.DataFrame] = {}
    point: list[dict[str, Any]] = []
    groups = indices = condition_indices(CKD_SPECS)
    pooled = params[params.fit == "pooled"]
    for condition in wide.condition.unique():
        group = next(g for g in pooled.fit_group.unique() if condition in g.split("+"))
        model = restore(pooled[pooled.fit_group == group])
        rows = wide[(wide.condition == condition) & wide.complete]
        test = rows[rows.case_id.isin(cases) & (rows.split == "test")].set_index("case_id")
        test = test.loc[[c for c in cases if c in test.index]].reset_index()
        train = rows[rows.split == "train"]
        values = likelihood_values(test, CKD_SPECS)
        for direction in CONDITION_ORDER:
            pred = reciprocal_predictions(model, values, test, condition=direction,
                                          evidence_indices=groups[direction]["evidence"],
                                          target_indices=groups[direction]["targets"])
            metrics = target_metrics(pred, train)
            point.append({"condition": condition, "direction": direction,
                          "mean_c_index_first_order_code": float(metrics.c_index.mean()),
                          "pooled_r2_first_order_code": 1 - metrics.sse.sum() / metrics.sst.sum()})
            long = pred[["row_index", "output_name", "actual", "prediction"]].rename(
                columns={"output_name": "target"})
            out[(condition, direction)] = long.sort_values(["target", "row_index"])
    return {"predictions": out, "point": pd.DataFrame(point)}


def link_predictions(beliefs: pd.DataFrame, wide: pd.DataFrame, cases: list[str]) -> dict:
    """Logistic link from one belief value to each decision, fitted on train rows."""
    out = {}
    for condition in beliefs.condition.unique():
        b = beliefs[(beliefs.condition == condition)].set_index("case_id")["value"]
        rows = wide[(wide.condition == condition) & wide.complete].set_index("case_id")
        train = rows[(rows.split == "train") & rows.index.isin(b.index)]
        test_ids = [c for c in cases if c in rows.index and rows.loc[c, "split"] == "test" and c in b.index]
        parts = []
        for target in DECISIONS:
            y = train[target].to_numpy(float)
            x = b.loc[train.index].to_numpy(float)[:, None]
            xt = b.loc[test_ids].to_numpy(float)[:, None]
            if len(np.unique(y)) < 2:
                pred = np.full(len(test_ids), y.mean())
            else:
                pred = LogisticRegression(C=1e6, max_iter=1000).fit(x, y).predict_proba(xt)[:, 1]
            parts.append(pd.DataFrame({"row_index": range(len(test_ids)), "target": target,
                                       "actual": rows.loc[test_ids, target].to_numpy(float),
                                       "prediction": pred}))
        out[(condition, "reports_to_decisions")] = pd.concat(parts, ignore_index=True)
    return {"predictions": out, "point": pd.DataFrame()}


def coherence_table(preds: dict[tuple[str, str], pd.DataFrame], *, method: str, model: str,
                    n_cases: int, resamples: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    keys = list(preds)
    point = {k: _summaries(preds[k], None) for k in keys}
    draws = {k: [] for k in keys}
    for _ in range(int(resamples)):
        idx = rng.integers(0, n_cases, n_cases)
        for k in keys:
            draws[k].append(_summaries(preds[k], idx))
    rows = []

    def add(condition, direction, stat, est, values, framing):
        v = np.asarray(values, float)
        v = v[np.isfinite(v)]
        lo, hi = np.percentile(v, [2.5, 97.5]) if len(v) else (np.nan, np.nan)
        rows.append({"model": model, "method": method, "framing": framing, "condition": condition,
                     "direction": direction, "statistic": stat, "estimate": est,
                     "ci_lower": lo, "ci_upper": hi, "n_test_cases": n_cases, "resamples": resamples})

    for (condition, direction) in keys:
        framing = condition.split("_")[1] if "_" in condition else "self"
        for stat in ("mean_c_index", "pooled_r2"):
            add(condition, direction, stat, point[(condition, direction)][stat],
                [d[stat] for d in draws[(condition, direction)]], framing)
    for framing in ("john", "user"):
        for direction in sorted({d for _, d in keys}):
            for name, (first, second) in {"A_minus_D": ("A", f"D_{framing}"),
                                          "D_minus_C": (f"D_{framing}", f"C_{framing}")}.items():
                if (first, direction) not in preds or (second, direction) not in preds:
                    continue
                for stat in ("mean_c_index", "pooled_r2"):
                    est = point[(first, direction)][stat] - point[(second, direction)][stat]
                    vals = [a[stat] - b[stat] for a, b in zip(draws[(first, direction)],
                                                              draws[(second, direction)])]
                    add(name, direction, stat, est, vals, framing)
    return pd.DataFrame(rows)
