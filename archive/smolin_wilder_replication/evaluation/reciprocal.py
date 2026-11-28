"""Reciprocal prediction between belief reports and decisions (Figure 1).

The main fitted model is restored unchanged.  For each test case the posterior
over ``z`` is inferred from one prompt group and used to predict the other:

``reports_to_decisions``  evidence: stated probability, probability quintile and
    Likert report; targets: the thirteen binary decisions.
``decisions_to_reports``  evidence: the thirteen decisions; targets: the three
    belief reports.

Target responses never enter their own posterior.  Model summaries carry 95%
percentile intervals from 10,000 bootstrap resamples of target prompts.

Usage::

    python -m archive.smolin_wilder_replication.evaluation.reciprocal    # fits/main -> evaluation/reciprocal

Outputs: ``predictions.csv`` (case level), ``metrics_by_target.csv``,
``model_summary.csv`` (mean c-index, pooled R^2 vs the best test constant, CIs).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import torch

from archive.smolin_wilder_replication.config import DOMAINS, EVALUATION_DIR, FITS_DIR, ensure
from archive.smolin_wilder_replication.evaluation.leave_one_out import output_metrics
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.model import OutputSpec, ProbabilisticLatentModel, load_fit, restore_cell
from archive.smolin_wilder_replication.models import RECIPROCAL_BOOTSTRAP_POSITION

CONDITION_ORDER = ("reports_to_decisions", "decisions_to_reports")
CONDITION_LABEL = {
    "reports_to_decisions": "Belief reports available; predict decisions",
    "decisions_to_reports": "Decisions available; predict belief reports",
}
BOOTSTRAP_REPETITIONS = 10_000
BOOTSTRAP_SEED = 20260825
# The ``model_position`` column and bootstrap seeds of this analysis use the
# model order of models.RECIPROCAL_BOOTSTRAP_POSITION.
MODEL_ORDER = tuple(sorted(RECIPROCAL_BOOTSTRAP_POSITION, key=RECIPROCAL_BOOTSTRAP_POSITION.__getitem__))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fit-dir", type=Path, default=FITS_DIR / "main")
    parser.add_argument("--output-dir", type=Path, default=EVALUATION_DIR / "reciprocal")
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--models", nargs="+", choices=MODEL_ORDER, default=list(MODEL_ORDER))
    return parser.parse_args()


def condition_indices(specs: Sequence[OutputSpec]) -> dict[str, dict[str, list[int]]]:
    """Evidence and target output indices of the two conditions."""

    decisions = [index for index, spec in enumerate(specs) if spec.kind == "binary"]
    reports = [index for index, spec in enumerate(specs) if spec.kind in {"probability", "ordinal"}]
    if len(decisions) != 13 or len(reports) != 3 or len(decisions) + len(reports) != len(specs):
        raise ValueError("expected 13 decision and 3 report prompts")
    return {
        "reports_to_decisions": {"evidence": reports, "targets": decisions},
        "decisions_to_reports": {"evidence": decisions, "targets": reports},
    }


def reciprocal_predictions(
    model: ProbabilisticLatentModel,
    likelihood_values: np.ndarray,
    raw_test: pd.DataFrame,
    *,
    condition: str,
    evidence_indices: Sequence[int],
    target_indices: Sequence[int],
) -> pd.DataFrame:
    observations = torch.as_tensor(likelihood_values, dtype=model.dtype, device=model.z_grid.device)
    if observations.ndim != 2 or observations.shape[1] != len(model.specs):
        raise ValueError("likelihood values must have one column per output")
    model.eval()
    rows: list[pd.DataFrame] = []
    with torch.no_grad():
        log_by_output = model.log_likelihood_by_output(observations)
        log_joint = log_by_output[:, list(evidence_indices), :].sum(dim=1) + model.prior_log_weights()[None, :]
        log_weights = log_joint - torch.logsumexp(log_joint, dim=1, keepdim=True)
        weights = torch.exp(log_weights)
        z_mean = weights @ model.z_grid
        z_variance = torch.sum(weights * (model.z_grid[None, :] - z_mean[:, None]).square(), dim=1)
        for target_index in target_indices:
            spec = model.specs[target_index]
            prediction_likelihood = weights @ model.conditional_mean(spec)
            part: dict[str, Any] = {
                "row_index": np.arange(len(observations)),
                "condition": condition,
                "condition_label": CONDITION_LABEL[condition],
                "evidence_output_count": len(evidence_indices),
                "target_output_count": len(target_indices),
                "target_output_index": target_index,
                "output_name": spec.name,
                "output_kind": spec.kind,
                "actual_likelihood_scale": observations[:, target_index].cpu().numpy(),
                "prediction_likelihood_scale": prediction_likelihood.cpu().numpy(),
                "actual": raw_test[spec.name].to_numpy(dtype=float),
                "prediction": prediction_likelihood.cpu().numpy(),
                "conditional_log_score_likelihood_scale": torch.logsumexp(
                    log_weights + log_by_output[:, target_index, :], dim=1
                ).cpu().numpy(),
                "posterior_z_mean": z_mean.cpu().numpy(),
                "posterior_z_sd": torch.sqrt(z_variance.clamp_min(0.0)).cpu().numpy(),
            }
            if spec.kind == "ordinal":
                probabilities = weights @ model.ordinal_probabilities(spec)
                for category_index in range(probabilities.shape[1]):
                    part[f"prob_class_{category_index + 1}"] = probabilities[:, category_index].cpu().numpy()
            rows.append(pd.DataFrame(part))
    return pd.concat(rows, ignore_index=True)


def target_metrics(predictions: pd.DataFrame, train: pd.DataFrame) -> pd.DataFrame:
    """Leave-one-out style metrics plus R^2 against the best test constant."""

    metrics = output_metrics(predictions, train)
    extra_rows: list[dict[str, Any]] = []
    for (condition, output_name), group in predictions.groupby(["condition", "output_name"], sort=False):
        finite = group[np.isfinite(group["actual"]) & np.isfinite(group["prediction"])]
        actual = finite["actual"].to_numpy(dtype=float)
        predicted = finite["prediction"].to_numpy(dtype=float)
        test_mean = float(np.mean(actual))
        sse = float(np.sum((actual - predicted) ** 2))
        sst = float(np.sum((actual - test_mean) ** 2))
        extra_rows.append(
            {
                "condition": condition,
                "output_name": output_name,
                "best_test_constant": test_mean,
                "sse": sse,
                "sst": sst,
                "r2_vs_best_test_constant": 1.0 - sse / sst if sst > 0.0 else math.nan,
            }
        )
    extras = pd.DataFrame(extra_rows)
    condition_by_output = predictions[["condition", "output_name"]].drop_duplicates()
    metrics = condition_by_output.merge(metrics, on="output_name", how="left", validate="one_to_one")
    return metrics.merge(extras, on=["condition", "output_name"], how="left", validate="one_to_one")


def summarize_metrics(
    metrics: pd.DataFrame,
    *,
    bootstrap_repetitions: int = BOOTSTRAP_REPETITIONS,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> pd.DataFrame:
    """Summarize targets and form percentile CIs by resampling target prompts."""

    keys = ["domain", "domain_display", "model_key", "model_display", "model_position", "condition", "condition_label"]
    rows: list[dict[str, Any]] = []
    for key, group in metrics.groupby(keys, sort=False, dropna=False):
        total_sst = float(group["sst"].sum())
        domain = str(group["domain"].iloc[0])
        model_position = int(group["model_position"].iloc[0])
        condition = str(group["condition"].iloc[0])
        cell_seed = (
            int(bootstrap_seed)
            + 1_000 * DOMAINS.index(domain)
            + 10 * model_position
            + CONDITION_ORDER.index(condition)
        )
        rng = np.random.default_rng(cell_seed)
        indices = rng.integers(0, len(group), size=(int(bootstrap_repetitions), len(group)))

        c_values = group["c_index"].to_numpy(dtype=float)
        sampled_c = c_values[indices]
        sampled_c_count = np.isfinite(sampled_c).sum(axis=1)
        c_draws = np.full(int(bootstrap_repetitions), np.nan, dtype=float)
        valid_c = sampled_c_count > 0
        c_draws[valid_c] = np.nansum(sampled_c[valid_c], axis=1) / sampled_c_count[valid_c]
        c_draws = c_draws[np.isfinite(c_draws)]
        c_lower, c_upper = np.percentile(c_draws, [2.5, 97.5]) if len(c_draws) else (math.nan, math.nan)

        sse_values = group["sse"].to_numpy(dtype=float)
        sst_values = group["sst"].to_numpy(dtype=float)
        draw_sse = sse_values[indices].sum(axis=1)
        draw_sst = sst_values[indices].sum(axis=1)
        r2_draws = np.full(int(bootstrap_repetitions), np.nan, dtype=float)
        valid_r2 = draw_sst > 0.0
        r2_draws[valid_r2] = 1.0 - draw_sse[valid_r2] / draw_sst[valid_r2]
        finite_r2_draws = r2_draws[np.isfinite(r2_draws)]
        r2_lower, r2_upper = (
            np.percentile(finite_r2_draws, [2.5, 97.5]) if len(finite_r2_draws) else (math.nan, math.nan)
        )
        rows.append(
            {
                **dict(zip(keys, key, strict=True)),
                "evidence_output_count": int(group["evidence_output_count"].iloc[0]),
                "target_output_count": int(len(group)),
                "targets_with_c_index": int(group["c_index"].notna().sum()),
                "targets_with_r2": int(group["r2_vs_best_test_constant"].notna().sum()),
                "mean_c_index": float(group["c_index"].mean()),
                "mean_c_index_ci_lower": float(c_lower),
                "mean_c_index_ci_upper": float(c_upper),
                "macro_mean_r2": float(group["r2_vs_best_test_constant"].mean()),
                "pooled_r2": (1.0 - float(group["sse"].sum()) / total_sst if total_sst > 0.0 else math.nan),
                "pooled_r2_ci_lower": float(r2_lower),
                "pooled_r2_ci_upper": float(r2_upper),
                "mean_relative_mse_vs_train_mean": float(group["relative_mse"].mean()),
                "bootstrap_repetitions": int(bootstrap_repetitions),
                "bootstrap_seed": int(cell_seed),
            }
        )
    return pd.DataFrame(rows).sort_values(["domain", "condition", "model_position"]).reset_index(drop=True)


def _add_base_columns(frame: pd.DataFrame, base: dict[str, Any]) -> pd.DataFrame:
    result = frame.copy()
    for column, value in reversed(list(base.items())):
        result.insert(0, column, value)
    return result


def main() -> None:
    args = parse_args()
    output_dir = ensure(Path(args.output_dir))
    parameters, metadata = load_fit(Path(args.fit_dir))

    prediction_parts: list[pd.DataFrame] = []
    metric_parts: list[pd.DataFrame] = []
    for domain in args.domains:
        cfg = D.DOMAIN_CONFIGS[domain]
        dataset = D.load_dataset(domain)
        groups = condition_indices(cfg.output_specs)
        for model_key in args.models:
            full_group, train, _, test = D.model_splits(dataset, model_key)
            if train.empty or test.empty:
                raise ValueError(f"{domain}/{model_key} lacks train or test rows")
            model = restore_cell(parameters, metadata, domain, model_key, cfg.output_specs)
            values = D.likelihood_values(test, cfg.output_specs)
            base = {
                "domain": domain,
                "domain_display": cfg.display,
                "model_key": model_key,
                "model_display": str(full_group["model_display"].iloc[0]),
                "model_position": RECIPROCAL_BOOTSTRAP_POSITION[model_key],
            }
            print(f"Evaluating reciprocal groups for {domain}/{model_key}", flush=True)
            for condition in CONDITION_ORDER:
                definition = groups[condition]
                predictions = reciprocal_predictions(
                    model,
                    values,
                    test,
                    condition=condition,
                    evidence_indices=definition["evidence"],
                    target_indices=definition["targets"],
                )
                evidence_names = "|".join(cfg.output_specs[index].name for index in definition["evidence"])
                target_names = "|".join(cfg.output_specs[index].name for index in definition["targets"])
                predictions["evidence_outputs"] = evidence_names
                predictions["target_outputs"] = target_names
                for id_column in D.ID_COLUMNS:
                    if id_column in test:
                        predictions[id_column] = predictions["row_index"].map(test[id_column])
                prediction_parts.append(_add_base_columns(predictions, base))

                metrics = target_metrics(predictions, train)
                metrics["condition_label"] = CONDITION_LABEL[condition]
                metrics["evidence_output_count"] = len(definition["evidence"])
                metrics["target_output_count"] = len(definition["targets"])
                metrics["evidence_outputs"] = evidence_names
                metrics["target_outputs"] = target_names
                metric_parts.append(_add_base_columns(metrics, base))

    predictions = pd.concat(prediction_parts, ignore_index=True)
    metrics = pd.concat(metric_parts, ignore_index=True)
    summary = summarize_metrics(metrics)
    predictions.sort_values(["domain", "model_position", "condition", "output_name", "row_index"]).to_csv(
        output_dir / "predictions.csv", index=False
    )
    metrics.sort_values(["domain", "model_position", "condition", "output_name"]).to_csv(
        output_dir / "metrics_by_target.csv", index=False
    )
    summary.to_csv(output_dir / "model_summary.csv", index=False)
    (output_dir / "run_metadata.json").write_text(
        json.dumps(
            {
                "fit_directory": str(args.fit_dir),
                "domains": list(args.domains),
                "models": list(args.models),
                "target_responses_used_for_posterior_inference": False,
                "bootstrap": {
                    "unit": "target prompt within model/domain/condition",
                    "repetitions": BOOTSTRAP_REPETITIONS,
                    "seed": BOOTSTRAP_SEED,
                    "cell_seed_rule": "seed + 1000 * domain_position + 10 * model_position + condition_position",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote results to {output_dir}", flush=True)


if __name__ == "__main__":
    main()
