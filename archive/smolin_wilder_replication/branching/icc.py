"""Share of within-case probability variance explained by the reasoning prefix (Figure 6b).

For every case, a one-way random-effects ICC over its five prefixes with five
probability answers each (negative prefix-variance estimates are set to zero);
the 30 case ICCs of each model and domain are averaged, with a 95% percentile
bootstrap interval over cases.

Usage::

    python -m archive.smolin_wilder_replication.branching.icc

Input:   ``PARSED_DIR/branching/probability/probability_responses.csv``.
Outputs (``EVALUATION_DIR/branching_icc``): ``icc_within_instance.csv`` (one row
per model x case), ``icc_within_instance_summary.csv`` (one row per model x
domain), ``icc_within_instance_run_summary.json``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from archive.smolin_wilder_replication.branching.design import BRANCHING_PARSED_DIR
from archive.smolin_wilder_replication.config import EVALUATION_DIR

BOOTSTRAP_REPETITIONS = 10_000
SEED = 20260811


def instance_prefix_icc(frame: pd.DataFrame) -> dict[str, float]:
    """One-way random-effects ICC for five prefixes with five responses each."""
    required = ["prefix_id", "probability_replicate", "probability"]
    if frame[required].isna().any().any():
        raise ValueError("Instance ICC input contains missing values")
    responses_per_prefix = frame.groupby("prefix_id")["probability_replicate"].nunique()
    if responses_per_prefix.nunique() != 1:
        raise ValueError("Instance ICC requires equal responses per prefix")
    n_prefixes = int(frame["prefix_id"].nunique())
    n_responses = int(responses_per_prefix.iloc[0])
    if len(frame) != n_prefixes * n_responses:
        raise ValueError("Instance ICC input is not balanced")

    grand_mean = float(frame["probability"].mean())
    prefix_means = frame.groupby("prefix_id")["probability"].mean()
    row_means = frame["prefix_id"].map(prefix_means).to_numpy(float)
    values = frame["probability"].to_numpy(float)
    ss_between = n_responses * float(np.sum((prefix_means.to_numpy(float) - grand_mean) ** 2))
    ss_within = float(np.sum((values - row_means) ** 2))
    ms_between = ss_between / (n_prefixes - 1)
    ms_within = ss_within / (n_prefixes * (n_responses - 1))
    raw_prefix_variance = (ms_between - ms_within) / n_responses
    prefix_variance = max(raw_prefix_variance, 0.0)
    residual_variance = max(ms_within, 0.0)
    raw_total = raw_prefix_variance + residual_variance
    constrained_total = prefix_variance + residual_variance
    return {
        "n_prefixes": n_prefixes,
        "responses_per_prefix": n_responses,
        "n_responses": int(len(frame)),
        "mean_probability": grand_mean,
        "ms_between_prefixes": float(ms_between),
        "ms_within_prefixes": float(ms_within),
        "raw_prefix_variance": float(raw_prefix_variance),
        "prefix_variance": float(prefix_variance),
        "residual_variance": float(residual_variance),
        "raw_prefix_icc": float(raw_prefix_variance / raw_total) if raw_total > 0 else np.nan,
        "prefix_icc": (
            float(prefix_variance / constrained_total) if constrained_total > 0 else np.nan
        ),
        "prefix_variance_constrained_to_zero": int(raw_prefix_variance < 0),
    }


def bootstrap_instance_mean(values: np.ndarray, repetitions: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    values = np.asarray(values, dtype=float)
    draws = rng.choice(values, size=(repetitions, len(values)), replace=True)
    return np.mean(draws, axis=1)


def analyze(
    responses: pd.DataFrame, bootstrap_repetitions: int, seed: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    instance_rows = []
    for (model_alias, domain, instance_id), group in responses.groupby(
        ["model_alias", "domain", "instance_id"], sort=True
    ):
        if not group["parse_ok"].eq(1).all():
            raise ValueError(f"Incomplete parses for {model_alias}/{domain}/{instance_id}")
        estimate = instance_prefix_icc(group)
        estimate.update({"model_alias": model_alias, "domain": domain, "instance_id": instance_id})
        instance_rows.append(estimate)
    instances = pd.DataFrame(instance_rows)

    summaries = []
    for group_index, ((model_alias, domain), group) in enumerate(
        instances.groupby(["model_alias", "domain"], sort=True)
    ):
        # Seed per (model, domain) cell in sorted order.
        bootstrap = pd.Series(
            bootstrap_instance_mean(
                group["prefix_icc"].to_numpy(float),
                bootstrap_repetitions,
                seed + 100_003 * (group_index + 1),
            )
        )
        summaries.append(
            {
                "model_alias": model_alias,
                "domain": domain,
                "n_instances": int(len(group)),
                "mean_instance_prefix_icc": float(group["prefix_icc"].mean()),
                "mean_instance_prefix_icc_ci95_low": float(bootstrap.quantile(0.025)),
                "mean_instance_prefix_icc_ci95_high": float(bootstrap.quantile(0.975)),
                "median_instance_prefix_icc": float(group["prefix_icc"].median()),
                "sd_instance_prefix_icc": float(group["prefix_icc"].std(ddof=1)),
                "mean_raw_instance_prefix_icc": float(group["raw_prefix_icc"].mean()),
                "instances_constrained_to_zero": int(
                    group["prefix_variance_constrained_to_zero"].sum()
                ),
            }
        )
    return instances, pd.DataFrame(summaries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--responses",
        type=Path,
        default=BRANCHING_PARSED_DIR / "probability" / "probability_responses.csv",
    )
    parser.add_argument("--out-dir", type=Path, default=EVALUATION_DIR / "branching_icc")
    parser.add_argument("--bootstrap-repetitions", type=int, default=BOOTSTRAP_REPETITIONS)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    responses = pd.read_csv(args.responses)
    instances, summary = analyze(responses, args.bootstrap_repetitions, args.seed)
    instances = instances.sort_values(["domain", "model_alias", "instance_id"])
    summary = summary.sort_values(["domain", "model_alias"])
    args.out_dir.mkdir(parents=True, exist_ok=True)
    instances.to_csv(args.out_dir / "icc_within_instance.csv", index=False)
    summary.to_csv(args.out_dir / "icc_within_instance_summary.csv", index=False)
    run_summary = {
        "estimand": "for each instance, fraction of probability variance attributable to its five sampled prefixes; then arithmetic mean over 30 instances",
        "instance_estimator": "one-way random-effects ICC with five responses per prefix",
        "negative_prefix_variance": "constrained to zero for the variance-fraction estimate; raw ICC retained in instance table",
        "confidence_intervals": "95% percentile bootstrap over the 30 instance-level ICCs",
        "bootstrap_repetitions": args.bootstrap_repetitions,
        "seed": args.seed,
    }
    (args.out_dir / "icc_within_instance_run_summary.json").write_text(
        json.dumps(run_summary, indent=2) + "\n", encoding="utf-8"
    )
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
