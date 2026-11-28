"""Leave-one-output-out prediction from a fitted latent model.

For every test case and every output ``j``: infer ``p(z | y_-j)`` from the
other fifteen outputs under the fixed fitted model, then predict
``E[y_j] = integral E[y_j | z] p(z | y_-j) dz``.  No parameters are refit.

Supports Figure 4 and the probability-sufficiency appendix figure (main fit)
and the latent-sensitivity appendix figure (main and monotone-spline fits).

Usage::

    python -m archive.smolin_wilder_replication.evaluation.leave_one_out               # fits/main -> evaluation/leave_one_out
    python -m archive.smolin_wilder_replication.evaluation.leave_one_out --fit spline  # fits/spline -> evaluation/leave_one_out_spline

Outputs: ``leave_one_output_out_predictions.csv`` (one row per test case x
output), ``metrics_by_output.csv`` (MSE, relative MSE vs the training mean,
c-index, ...), ``metrics_summary.csv`` (macro means per domain x model).
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
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.model import (
    OutputSpec,
    ProbabilisticLatentModel,
    concordance_index,
    load_fit,
    restore_cell,
)
from archive.smolin_wilder_replication.models import PIPELINE_POSITION, pipeline_order

OUTPUT_NAMES = {"main": "leave_one_out", "spline": "leave_one_out_spline"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fit", choices=tuple(OUTPUT_NAMES), default="main")
    parser.add_argument("--fit-dir", type=Path, default=None, help="Default: <fits>/<fit>.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--models", nargs="+", choices=pipeline_order(), default=list(pipeline_order()))
    return parser.parse_args()


def leave_one_out_predictions(model: ProbabilisticLatentModel, values: np.ndarray) -> pd.DataFrame:
    """Exact quadrature predictions after excluding each target column."""

    observations = torch.as_tensor(values, dtype=model.dtype, device=model.z_grid.device)
    if observations.ndim != 2 or observations.shape[1] != len(model.specs):
        raise ValueError("values must have one column per output specification")
    parts: list[pd.DataFrame] = []
    model.eval()
    with torch.no_grad():
        log_by_output = model.log_likelihood_by_output(observations)
        for output_index, spec in enumerate(model.specs):
            included = [index for index in range(len(model.specs)) if index != output_index]
            loo_log_joint = log_by_output[:, included, :].sum(dim=1) + model.prior_log_weights()[None, :]
            loo_log_weights = loo_log_joint - torch.logsumexp(loo_log_joint, dim=1, keepdim=True)
            weights = torch.exp(loo_log_weights)
            prediction = weights @ model.conditional_mean(spec)
            log_score = torch.logsumexp(loo_log_weights + log_by_output[:, output_index, :], dim=1)
            z_mean = weights @ model.z_grid
            z_variance = torch.sum(weights * (model.z_grid[None, :] - z_mean[:, None]).square(), dim=1)
            part: dict[str, Any] = {
                "row_index": np.arange(len(observations)),
                "held_out_output_index": output_index,
                "output_name": spec.name,
                "output_kind": spec.kind,
                "actual_likelihood_scale": observations[:, output_index].cpu().numpy(),
                "prediction_likelihood_scale": prediction.cpu().numpy(),
                "conditional_log_score_likelihood_scale": log_score.cpu().numpy(),
                "posterior_z_mean": z_mean.cpu().numpy(),
                "posterior_z_sd": torch.sqrt(z_variance.clamp_min(0.0)).cpu().numpy(),
            }
            if spec.kind == "ordinal":
                probabilities = weights @ model.ordinal_probabilities(spec)
                for category_index in range(probabilities.shape[1]):
                    part[f"prob_class_{category_index + 1}"] = probabilities[:, category_index].cpu().numpy()
            parts.append(pd.DataFrame(part))
    return pd.concat(parts, ignore_index=True)


def attach_raw_scale_values(
    predictions: pd.DataFrame, test: pd.DataFrame, specs: Sequence[OutputSpec]
) -> pd.DataFrame:
    """Add ``actual``/``prediction`` columns on the reported scale."""

    result = predictions.copy()
    result["actual"] = np.nan
    result["prediction"] = np.nan
    for spec in specs:
        selected = result["output_name"].eq(spec.name)
        actual = test[spec.name].to_numpy(dtype=float)
        prediction = result.loc[selected, "prediction_likelihood_scale"].to_numpy(dtype=float)
        result.loc[selected, "actual"] = actual
        result.loc[selected, "prediction"] = prediction
    return result


def output_metrics(predictions: pd.DataFrame, train: pd.DataFrame) -> pd.DataFrame:
    """Per-output accuracy of ``prediction`` against ``actual``."""

    rows: list[dict[str, Any]] = []
    for (output_name, output_kind), group in predictions.groupby(["output_name", "output_kind"], sort=False):
        finite = group[np.isfinite(group["actual"]) & np.isfinite(group["prediction"])]
        actual = finite["actual"].to_numpy(dtype=float)
        predicted = finite["prediction"].to_numpy(dtype=float)
        train_values = train[output_name].to_numpy(dtype=float)
        train_values = train_values[np.isfinite(train_values)]
        baseline_mean = float(np.mean(train_values))
        mse = float(np.mean((actual - predicted) ** 2))
        baseline_mse = float(np.mean((actual - baseline_mean) ** 2))
        c_index, n_pairs = concordance_index(actual, predicted)
        row = {
            "output_name": output_name,
            "output_kind": output_kind,
            "n_test": int(len(finite)),
            "mse": mse,
            "rmse": math.sqrt(mse),
            "mae": float(np.mean(np.abs(actual - predicted))),
            "baseline_train_mean": baseline_mean,
            "baseline_test_mse": baseline_mse,
            "relative_mse": mse / baseline_mse if baseline_mse > 0.0 else math.nan,
            "variance_explained_vs_train_mean": (1.0 - mse / baseline_mse if baseline_mse > 0.0 else math.nan),
            "c_index": float(c_index),
            "n_comparable_pairs": int(n_pairs),
            "mean_conditional_log_score_likelihood_scale": float(
                finite["conditional_log_score_likelihood_scale"].mean()
            ),
            "brier_score": math.nan,
            "classification_accuracy": math.nan,
        }
        if output_kind == "binary":
            row["brier_score"] = mse
            row["classification_accuracy"] = float(np.mean((predicted >= 0.5) == actual.astype(bool)))
        elif output_kind == "ordinal":
            row["classification_accuracy"] = float(np.mean(np.rint(predicted) == actual))
        rows.append(row)
    return pd.DataFrame(rows)


def summary_metrics(metrics: pd.DataFrame) -> pd.DataFrame:
    keys = ["domain", "domain_display", "model_key", "model_display", "model_position"]
    rows: list[dict[str, Any]] = []
    for key, group in metrics.groupby(keys, sort=False, dropna=False):
        rows.append(
            {
                **dict(zip(keys, key, strict=True)),
                "n_outputs": int(len(group)),
                "n_outputs_with_c_index": int(np.isfinite(group["c_index"]).sum()),
                "mean_c_index": float(group["c_index"].mean()),
                "mean_relative_mse": float(group["relative_mse"].mean()),
                "median_relative_mse": float(group["relative_mse"].median()),
                "mean_variance_explained_vs_train_mean": float(group["variance_explained_vs_train_mean"].mean()),
                "conditioning": "all available outputs except prediction target",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    fit_dir = Path(args.fit_dir or FITS_DIR / args.fit)
    output_dir = ensure(Path(args.output_dir or EVALUATION_DIR / OUTPUT_NAMES[args.fit]))
    parameters, metadata = load_fit(fit_dir)

    prediction_parts: list[pd.DataFrame] = []
    metric_parts: list[pd.DataFrame] = []
    for domain in args.domains:
        cfg = D.DOMAIN_CONFIGS[domain]
        dataset = D.load_dataset(domain)
        for model_key in args.models:
            group, train, _, test = D.model_splits(dataset, model_key)
            if train.empty or test.empty:
                raise ValueError(f"{domain}/{model_key} lacks a required train or test split")
            model = restore_cell(parameters, metadata, domain, model_key, cfg.output_specs)
            print(f"Evaluating held-out outputs for {domain}/{model_key}", flush=True)
            predictions = leave_one_out_predictions(model, D.likelihood_values(test, cfg.output_specs))
            predictions = attach_raw_scale_values(predictions, test, cfg.output_specs)
            for column in [column for column in D.ID_COLUMNS if column in test]:
                predictions[column] = predictions["row_index"].map(test[column])
            base = {
                "domain": domain,
                "domain_display": cfg.display,
                "model_key": model_key,
                "model_display": str(group["model_display"].iloc[0]),
                "model_position": PIPELINE_POSITION[model_key],
            }
            for column, value in reversed(list(base.items())):
                predictions.insert(0, column, value)
            prediction_parts.append(predictions)
            metrics = output_metrics(predictions, train)
            for column, value in reversed(list(base.items())):
                metrics.insert(0, column, value)
            metric_parts.append(metrics)

    predictions = pd.concat(prediction_parts, ignore_index=True)
    metrics = pd.concat(metric_parts, ignore_index=True)
    sort_columns = ["domain", "model_position", "output_name"]
    predictions.sort_values([*sort_columns, "row_index"]).to_csv(
        output_dir / "leave_one_output_out_predictions.csv", index=False
    )
    metrics.sort_values(sort_columns).to_csv(output_dir / "metrics_by_output.csv", index=False)
    summary_metrics(metrics).to_csv(output_dir / "metrics_summary.csv", index=False)
    (output_dir / "run_metadata.json").write_text(
        json.dumps(
            {
                "fit_directory": str(fit_dir),
                "fit_response_curve": metadata["response_curve"],
                "domains": list(args.domains),
                "models": list(args.models),
                "test_conditioning": "for target j, p(z | all available test outputs except j)",
                "prediction": "posterior integral of E[y_j | z] under p(z | y_-j)",
                "relative_mse_baseline": "test MSE of the constant training-set mean, separately by output",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote results to {output_dir}", flush=True)


if __name__ == "__main__":
    main()
