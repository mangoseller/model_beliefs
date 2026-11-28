"""Two-dimensional latent-belief sensitivity model (Appendix: latent sensitivity).

Extends the main model to ``z = (z1, z2)`` in (0, 1)^2 with a fitted joint
prior whose log density is bilinearly interpolated in logit coordinates on a
7 x 7 knot grid.  With ``x_d = logit(z_d)``::

    probability   y ~ Beta(mean = z1, precision phi)          (anchors z1)
    binary j      logit P(y_j = 1) = a_j + b_j x1 + c_j x2    (b_j > 0)
    ordinal j     ordered logit with score b_j x1 + c_j x2

The loadings ``c_j`` start at random values (``c = 0`` is a stationary point),
each cell keeps the best of three restarts by validation NLL, and the sign of
``z2`` is then fixed so the mean loading is positive.  Integration uses a
128 x 32 tensor Gauss-Legendre grid.

After fitting, each cell's leave-one-output-out predictions are computed exactly
as for the main model (:mod:`archive.smolin_wilder_replication.evaluation.leave_one_out`).  The
results bundle ships these held-out outputs in ``fits/two_d``; refits match
them approximately.

Usage::

    python -m archive.smolin_wilder_replication.latent.two_d                       # -> build/fits/two_d
    python -m archive.smolin_wilder_replication.latent.two_d --shard 0 --shards 9

Outputs: ``leave_one_output_out_predictions.csv``,
``leave_one_output_out_metrics.csv``, ``fit_summary.csv``, and ``parameters.csv``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from itertools import product
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

from archive.smolin_wilder_replication.config import DOMAINS, REFIT_DIR
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.fit import resolve_device
from archive.smolin_wilder_replication.latent.model import (
    LATENT_PRIOR_ROW_NAME,
    PRIOR_KNOTS,
    FitConfig,
    OutputSpec,
    concordance_index,
    fit_model,
    gauss_legendre_grid,
    inverse_softplus,
    logit,
)
from archive.smolin_wilder_replication.models import PIPELINE_POSITION

LATENT_DIM = 2
QUADRATURE_POINTS = (128, 32)
LOADING_INIT_SCALE = 0.25
RESTARTS = 3
RESULT_TABLES = (
    "fit_summary.csv",
    "parameters.csv",
    "leave_one_output_out_predictions.csv",
    "leave_one_output_out_metrics.csv",
)
# Column order of the held-out prediction table.
PREDICTION_COLUMNS = (
    "domain", "domain_display", "model_key", "model_display", "model_position", "latent_dim",
    "row_index", "held_out_output_index", "output_name", "output_kind",
    "actual_likelihood_scale", "prediction_likelihood_scale",
    "conditional_log_score_likelihood_scale", "posterior_z1_mean", "posterior_z_mean",
    "prob_class_1", "prob_class_2", "prob_class_3", "prob_class_4", "prob_class_5",
    "actual", "prediction", "posterior_z2_mean",
)  # fmt: skip


class LatentTraitModel(nn.Module):
    """Mixed-output latent-trait model with a fitted joint prior on D axes."""

    min_slope = 1e-3
    min_gap = 1e-3

    def __init__(
        self,
        specs: Sequence[OutputSpec],
        *,
        latent_dim: int = LATENT_DIM,
        quadrature_points: Sequence[int] = QUADRATURE_POINTS,
        probability_epsilon: float = 1e-4,
        loading_init_scale: float = LOADING_INIT_SCALE,
        prior_knots: int = PRIOR_KNOTS,
        dtype: torch.dtype = torch.float64,
    ) -> None:
        super().__init__()
        self.specs = tuple(specs)
        if int(latent_dim) < 1:
            raise ValueError("latent_dim must be at least one")
        self.latent_dim = int(latent_dim)
        self.probability_epsilon = float(probability_epsilon)
        self.loading_init_scale = float(loading_init_scale)
        self.prior_knots = int(prior_knots)
        self.dtype = dtype
        per_dimension = [int(value) for value in quadrature_points][: self.latent_dim]
        if len(per_dimension) < self.latent_dim:
            raise ValueError("quadrature_points must give at least latent_dim node counts")
        self.quadrature_points = tuple(per_dimension)

        axis_nodes: list[torch.Tensor] = []
        axis_log_weights: list[torch.Tensor] = []
        for n_points in self.quadrature_points:
            nodes, weights = gauss_legendre_grid(n_points, dtype=dtype)
            axis_nodes.append(nodes)
            axis_log_weights.append(torch.log(weights))

        mesh_nodes = torch.meshgrid(*axis_nodes, indexing="ij")
        mesh_log_weights = torch.meshgrid(*axis_log_weights, indexing="ij")
        z_grid = torch.stack([axis.reshape(-1) for axis in mesh_nodes], dim=0)
        log_prior = torch.stack([axis.reshape(-1) for axis in mesh_log_weights], dim=0).sum(dim=0)

        # z_grid_all has shape [latent_dim, n_nodes]; row 0 is the anchored axis.
        self.register_buffer("z_grid_all", z_grid)
        self.register_buffer("z_grid", z_grid[0].clone())
        self.register_buffer("logit_z_grid_all", logit(z_grid))
        self.register_buffer("logit_z_grid", logit(z_grid[0].clone()))
        self.register_buffer("log_prior_weights", log_prior)
        self.register_buffer(
            "prior_knot_logits_all",
            torch.stack(
                [
                    torch.linspace(
                        float(logit(nodes[0])), float(logit(nodes[-1])), self.prior_knots, dtype=dtype
                    )
                    for nodes in axis_nodes
                ]
            ),
        )
        self.prior_knot_values = nn.Parameter(torch.zeros((self.prior_knots,) * self.latent_dim, dtype=dtype))
        self.raw_probability_precision = nn.Parameter(torch.tensor(inverse_softplus(18.0), dtype=dtype))

        self.binary_intercepts = nn.ParameterDict()
        self.binary_raw_slopes = nn.ParameterDict()
        self.ordinal_raw_slopes = nn.ParameterDict()
        self.ordinal_first_cutpoints = nn.ParameterDict()
        self.ordinal_raw_gaps = nn.ParameterDict()
        self.secondary_loadings = nn.ParameterDict()

        slope_start = inverse_softplus(1.0 - self.min_slope)
        gap_start = inverse_softplus(1.0 - self.min_gap)
        n_extra = self.latent_dim - 1
        for spec in self.specs:
            if spec.kind == "probability":
                continue
            if n_extra > 0:
                self.secondary_loadings[spec.name] = nn.Parameter(
                    self.loading_init_scale * torch.randn(n_extra, dtype=dtype)
                )
            if spec.kind == "binary":
                self.binary_raw_slopes[spec.name] = nn.Parameter(torch.tensor(slope_start, dtype=dtype))
                self.binary_intercepts[spec.name] = nn.Parameter(torch.tensor(0.0, dtype=dtype))
                continue
            if spec.kind == "ordinal":
                n_categories = int(spec.n_categories or 0)
                self.ordinal_raw_slopes[spec.name] = nn.Parameter(torch.tensor(slope_start, dtype=dtype))
                self.ordinal_first_cutpoints[spec.name] = nn.Parameter(torch.tensor(-1.5, dtype=dtype))
                self.ordinal_raw_gaps[spec.name] = nn.Parameter(
                    torch.full((n_categories - 2,), gap_start, dtype=dtype)
                )
                continue
            raise ValueError(f"unknown output kind {spec.kind!r} for {spec.name!r}")

    @property
    def n_nodes(self) -> int:
        return int(self.log_prior_weights.numel())

    def prior_log_weights(self) -> torch.Tensor:
        """Normalized quadrature log weights under the fitted joint prior."""

        intervals: list[torch.Tensor] = []
        fractions: list[torch.Tensor] = []
        for axis in range(self.latent_dim):
            knots = self.prior_knot_logits_all[axis]
            coordinates = self.logit_z_grid_all[axis]
            interval = torch.bucketize(coordinates.contiguous(), knots[1:-1])
            lower = knots[interval]
            upper = knots[interval + 1]
            intervals.append(interval)
            fractions.append(((coordinates - lower) / (upper - lower)).clamp(0.0, 1.0))

        log_density = torch.zeros_like(self.log_prior_weights)
        for corner in product((0, 1), repeat=self.latent_dim):
            indices = tuple(intervals[axis] + int(corner[axis]) for axis in range(self.latent_dim))
            corner_weight = torch.ones_like(log_density)
            for axis, upper_corner in enumerate(corner):
                fraction = fractions[axis]
                corner_weight = corner_weight * (fraction if upper_corner else (1.0 - fraction))
            log_density = log_density + corner_weight * self.prior_knot_values[indices]

        unnormalized = self.log_prior_weights + log_density
        return unnormalized - torch.logsumexp(unnormalized, dim=0)

    @property
    def probability_precision(self) -> torch.Tensor:
        return 2.0 + F.softplus(self.raw_probability_precision)

    def binary_slope(self, name: str) -> torch.Tensor:
        return self.min_slope + F.softplus(self.binary_raw_slopes[name])

    def ordinal_slope(self, name: str) -> torch.Tensor:
        return self.min_slope + F.softplus(self.ordinal_raw_slopes[name])

    def secondary_contribution(self, name: str) -> torch.Tensor:
        """Return sum_{d>=2} c_jd logit(z_d) on the quadrature grid."""

        if self.latent_dim == 1:
            return torch.zeros((), dtype=self.dtype, device=self.z_grid.device)
        return self.secondary_loadings[name] @ self.logit_z_grid_all[1:]

    def binary_logits(self, spec: OutputSpec) -> torch.Tensor:
        return (
            self.binary_intercepts[spec.name]
            + self.binary_slope(spec.name) * self.logit_z_grid_all[0]
            + self.secondary_contribution(spec.name)
        )

    def binary_implied_threshold(self, spec: OutputSpec) -> torch.Tensor:
        """First-axis crossing of probability one half at logit(z_d) = 0 for d >= 2."""

        return torch.sigmoid(-self.binary_intercepts[spec.name] / self.binary_slope(spec.name))

    def ordinal_cutpoints(self, spec: OutputSpec) -> torch.Tensor:
        first = self.ordinal_first_cutpoints[spec.name].reshape(1)
        raw_gaps = self.ordinal_raw_gaps[spec.name]
        if raw_gaps.numel() == 0:
            return first
        gaps = self.min_gap + F.softplus(raw_gaps)
        return torch.cat((first, first + torch.cumsum(gaps, dim=0)))

    def ordinal_score(self, spec: OutputSpec) -> torch.Tensor:
        return self.ordinal_slope(spec.name) * self.logit_z_grid_all[0] + self.secondary_contribution(spec.name)

    def ordinal_probabilities(self, spec: OutputSpec) -> torch.Tensor:
        """Return P(y = c | z_q) with shape [node, category]."""

        score = self.ordinal_score(spec)
        cutpoints = self.ordinal_cutpoints(spec)
        cumulative = torch.sigmoid(cutpoints[None, :] - score[:, None])
        pieces = [cumulative[:, :1]]
        if cumulative.shape[1] > 1:
            pieces.append(cumulative[:, 1:] - cumulative[:, :-1])
        pieces.append(1.0 - cumulative[:, -1:])
        probabilities = torch.cat(pieces, dim=1)
        return probabilities.clamp_min(torch.finfo(self.dtype).tiny)

    def probability_mean(self) -> torch.Tensor:
        return self.z_grid_all[0]

    def conditional_mean(self, spec: OutputSpec) -> torch.Tensor:
        """E[y_j | z_q] on the likelihood scale, for every quadrature node."""

        if spec.kind == "probability":
            return self.probability_mean()
        if spec.kind == "binary":
            return torch.sigmoid(self.binary_logits(spec))
        if spec.kind == "ordinal":
            probabilities = self.ordinal_probabilities(spec)
            categories = torch.arange(1, probabilities.shape[1] + 1, dtype=self.dtype, device=self.z_grid.device)
            return probabilities @ categories
        raise ValueError(spec.kind)

    def output_log_prob(self, output_index: int, observations: torch.Tensor) -> torch.Tensor:
        """Return log p(y_i | z_q) with shape [case, node]."""

        spec = self.specs[int(output_index)]
        values = observations.to(dtype=self.dtype, device=self.z_grid.device).reshape(-1)
        finite = torch.isfinite(values)
        safe_values = torch.where(finite, values, torch.zeros_like(values))

        if spec.kind == "probability":
            y = safe_values.clamp(self.probability_epsilon, 1.0 - self.probability_epsilon)
            mean = self.probability_mean()
            precision = self.probability_precision
            alpha = mean * precision
            beta = (1.0 - mean) * precision
            log_normalizer = torch.lgamma(alpha + beta) - torch.lgamma(alpha) - torch.lgamma(beta)
            result = (
                log_normalizer[None, :]
                + (alpha[None, :] - 1.0) * torch.log(y[:, None])
                + (beta[None, :] - 1.0) * torch.log1p(-y[:, None])
            )
        elif spec.kind == "binary":
            logits = self.binary_logits(spec)
            # y log s(l) + (1 - y) log s(-l) == log s(-l) + y l
            result = F.logsigmoid(-logits)[None, :] + safe_values[:, None] * logits[None, :]
        elif spec.kind == "ordinal":
            probabilities = self.ordinal_probabilities(spec)
            categories = safe_values.to(torch.long) - 1
            categories = categories.clamp(0, probabilities.shape[1] - 1)
            result = torch.log(probabilities[:, categories].T)
        else:  # pragma: no cover - constructor rejects unknown kinds
            raise ValueError(spec.kind)

        return torch.where(finite[:, None], result, torch.zeros_like(result))

    def validate_observations(self, observations: np.ndarray | torch.Tensor) -> None:
        values = torch.as_tensor(observations, dtype=self.dtype)
        if values.ndim != 2 or values.shape[1] != len(self.specs):
            raise ValueError(f"expected observations with shape [n, {len(self.specs)}], got {tuple(values.shape)}")
        for output_index, spec in enumerate(self.specs):
            column = values[:, output_index]
            column = column[torch.isfinite(column)]
            if column.numel() == 0:
                continue
            if spec.kind in {"probability", "binary"} and (torch.any(column < 0.0) or torch.any(column > 1.0)):
                raise ValueError(f"{spec.name} contains values outside [0, 1]")
            if spec.kind == "binary" and torch.any((column != 0.0) & (column != 1.0)):
                raise ValueError(f"{spec.name} contains non-binary values")
            if spec.kind == "ordinal":
                n_categories = int(spec.n_categories or 0)
                if (
                    torch.any(column != torch.round(column))
                    or torch.any(column < 1)
                    or torch.any(column > n_categories)
                ):
                    raise ValueError(f"{spec.name} must contain integer categories 1..{n_categories}")

    def log_likelihood_by_output(self, observations: torch.Tensor) -> torch.Tensor:
        """Return shape [case, output, node]."""

        return torch.stack(
            [self.output_log_prob(index, observations[:, index]) for index in range(len(self.specs))], dim=1
        )

    def joint_log_likelihood(self, observations: torch.Tensor) -> torch.Tensor:
        """Return sum_j log p(y_ij | z_q) with shape [case, node]."""

        total = self.output_log_prob(0, observations[:, 0])
        for index in range(1, len(self.specs)):
            total = total + self.output_log_prob(index, observations[:, index])
        return total

    def marginal_log_prob(self, observations: torch.Tensor) -> torch.Tensor:
        """Evaluate log integral p(z) prod_j p(y_j | z) dz per case."""

        joint = self.joint_log_likelihood(observations)
        return torch.logsumexp(joint + self.prior_log_weights()[None, :], dim=1)

    def regularization(self) -> torch.Tensor:
        terms = [parameter.square().mean() for parameter in self.parameters()]
        return torch.stack(terms).mean() if terms else torch.zeros((), dtype=self.dtype)

    @torch.no_grad()
    def canonicalize(self) -> list[bool]:
        """Make the mean loading on each extra coordinate positive (reflecting the prior)."""

        flipped: list[bool] = []
        if self.latent_dim == 1:
            return flipped
        stacked = torch.stack([value for value in self.secondary_loadings.values()])
        for axis in range(self.latent_dim - 1):
            flip = bool(stacked[:, axis].mean() < 0.0)
            flipped.append(flip)
            if flip:
                for parameter in self.secondary_loadings.values():
                    parameter[axis] = -parameter[axis]
                self.prior_knot_values.copy_(self.prior_knot_values.flip(dims=(axis + 1,)))
        return flipped

    def parameter_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in self.specs:
            base = {"output_name": spec.name, "output_kind": spec.kind}
            if spec.kind == "probability":
                rows.append({**base, "parameter": "precision", "value": float(self.probability_precision)})
                continue
            if spec.kind == "binary":
                rows.append({**base, "parameter": "nominal_threshold", "value": spec.nominal_threshold})
                rows.append(
                    {**base, "parameter": "implied_threshold", "value": float(self.binary_implied_threshold(spec))}
                )
                rows.append({**base, "parameter": "slope", "value": float(self.binary_slope(spec.name))})
                rows.append({**base, "parameter": "intercept", "value": float(self.binary_intercepts[spec.name])})
            elif spec.kind == "ordinal":
                rows.append({**base, "parameter": "slope", "value": float(self.ordinal_slope(spec.name))})
                for index, value in enumerate(self.ordinal_cutpoints(spec), start=1):
                    rows.append({**base, "parameter": f"cutpoint_{index}", "value": float(value)})
            if self.latent_dim > 1:
                for axis, value in enumerate(self.secondary_loadings[spec.name], start=2):
                    rows.append({**base, "parameter": f"loading_dim{axis}", "value": float(value)})
        base = {"output_name": LATENT_PRIOR_ROW_NAME, "output_kind": "latent_prior"}
        for index in np.ndindex(self.prior_knot_values.shape):
            suffix = "_".join(str(axis_index + 1) for axis_index in index)
            rows.append(
                {**base, "parameter": f"prior_knot_value_{suffix}", "value": float(self.prior_knot_values[index])}
            )
        return rows


def fit_with_restarts(
    build_model: Callable[[], LatentTraitModel],
    train_values: np.ndarray,
    validation_values: np.ndarray,
    config: FitConfig,
    *,
    restarts: int,
    seed: int,
    progress_label: str,
) -> tuple[LatentTraitModel, dict[str, Any]]:
    """Fit several random initializations and keep the best validation NLL."""

    best_model: LatentTraitModel | None = None
    best_info: dict[str, Any] | None = None
    restart_scores: list[float] = []
    for restart in range(max(1, int(restarts))):
        torch.manual_seed(int(seed) + 1000 * restart)
        np.random.seed(int(seed) + 1000 * restart)
        model = build_model()
        label = progress_label if restarts <= 1 else f"{progress_label}/restart{restart + 1}"
        info = fit_model(model, train_values, validation_values, config, progress_label=label)
        restart_scores.append(float(info["best_validation_mean_nll"]))
        if best_info is None or info["best_validation_mean_nll"] < best_info["best_validation_mean_nll"]:
            best_model = model
            best_info = dict(info)
    assert best_model is not None and best_info is not None
    best_info["n_restarts"] = int(max(1, int(restarts)))
    best_info["restart_validation_nlls"] = ";".join(f"{score:.6f}" for score in restart_scores)
    best_info["restart_validation_nll_spread"] = float(max(restart_scores) - min(restart_scores))
    best_info["selected_restart"] = int(int(np.argmin(restart_scores)) + 1)
    return best_model, best_info


def leave_one_out_predictions(model: LatentTraitModel, values: np.ndarray) -> pd.DataFrame:
    """Predict each output from all the others (exact quadrature)."""

    observations = torch.as_tensor(values, dtype=model.dtype, device=model.z_grid.device)
    parts: list[pd.DataFrame] = []
    model.eval()
    with torch.no_grad():
        log_by_output = model.log_likelihood_by_output(observations)
        all_log_joint = log_by_output.sum(dim=1) + model.prior_log_weights()[None, :]
        for output_index, spec in enumerate(model.specs):
            loo_log_joint = all_log_joint - log_by_output[:, output_index, :]
            loo_log_weights = loo_log_joint - torch.logsumexp(loo_log_joint, dim=1, keepdim=True)
            weights = torch.exp(loo_log_weights)
            prediction = weights @ model.conditional_mean(spec)
            log_score = torch.logsumexp(loo_log_weights + log_by_output[:, output_index, :], dim=1)
            posterior_means = weights @ model.z_grid_all.T
            part: dict[str, Any] = {
                "row_index": np.arange(len(observations)),
                "held_out_output_index": output_index,
                "output_name": spec.name,
                "output_kind": spec.kind,
                "actual_likelihood_scale": observations[:, output_index].cpu().numpy(),
                "prediction_likelihood_scale": prediction.cpu().numpy(),
                "conditional_log_score_likelihood_scale": log_score.cpu().numpy(),
            }
            for axis in range(model.latent_dim):
                part[f"posterior_z{axis + 1}_mean"] = posterior_means[:, axis].cpu().numpy()
            part["posterior_z_mean"] = part["posterior_z1_mean"]
            if spec.kind == "ordinal":
                probabilities = weights @ model.ordinal_probabilities(spec)
                for category_index in range(probabilities.shape[1]):
                    part[f"prob_class_{category_index + 1}"] = probabilities[:, category_index].cpu().numpy()
            parts.append(pd.DataFrame(part))
    return pd.concat(parts, ignore_index=True)


def attach_raw_scale_values(predictions: pd.DataFrame, test: pd.DataFrame, specs: Sequence[OutputSpec]) -> pd.DataFrame:
    result = predictions.copy()
    result["actual"] = np.nan
    result["prediction"] = np.nan
    for spec in specs:
        selected = result["output_name"].eq(spec.name)
        prediction = result.loc[selected, "prediction_likelihood_scale"].to_numpy(dtype=float)
        result.loc[selected, "actual"] = test[spec.name].to_numpy(dtype=float)
        result.loc[selected, "prediction"] = prediction
    return result


def leave_one_out_metrics(predictions: pd.DataFrame, train: pd.DataFrame) -> pd.DataFrame:
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
            "classification_accuracy": math.nan,
        }
        if output_kind == "binary":
            row["classification_accuracy"] = float(np.mean((predicted >= 0.5) == actual.astype(bool)))
        elif output_kind == "ordinal":
            row["classification_accuracy"] = float(np.mean(np.rint(predicted) == actual))
        rows.append(row)
    return pd.DataFrame(rows)


# Command-line entry point
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    # Cells are merged in alphabetical model order.
    parser.add_argument("--models", nargs="+", choices=sorted(PIPELINE_POSITION), default=sorted(PIPELINE_POSITION))
    parser.add_argument("--output-dir", type=Path, default=REFIT_DIR / "two_d")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--epochs", type=int, default=30000)
    parser.add_argument("--patience", type=int, default=500)
    parser.add_argument("--min-delta", type=float, default=1e-5)
    parser.add_argument("--lr", type=float, default=2e-2)
    parser.add_argument("--restarts", type=int, default=RESTARTS)
    parser.add_argument("--probability-epsilon", type=float, default=1e-4)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--progress-every", type=int, default=2000)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="cpu")
    return parser.parse_args()


def cell_seed(base_seed: int, domain: str, model_key: str) -> int:
    """Restart-0 seed of one cell (positions include the excluded model; see models.py)."""

    cell_index = list(D.DOMAIN_CONFIGS).index(domain) * 10 + PIPELINE_POSITION[model_key]
    return int(base_seed) + 101 * cell_index + LATENT_DIM


def fit_cell(domain: str, model_key: str, dataset: pd.DataFrame, args: argparse.Namespace, device: torch.device):
    cfg = D.DOMAIN_CONFIGS[domain]
    specs = cfg.output_specs
    group, train, validation, test = D.model_splits(dataset, model_key)
    config = FitConfig(
        epochs=int(args.epochs),
        patience=int(args.patience),
        min_delta=float(args.min_delta),
        learning_rate=float(args.lr),
        l2=0.0,
        progress_every=int(args.progress_every),
    )
    label = f"{domain}/{model_key}/d{LATENT_DIM}"
    print(f"Fitting {label}", flush=True)

    def build_model() -> LatentTraitModel:
        return LatentTraitModel(
            specs,
            latent_dim=LATENT_DIM,
            quadrature_points=QUADRATURE_POINTS,
            probability_epsilon=float(args.probability_epsilon),
            loading_init_scale=LOADING_INIT_SCALE,
        ).to(device)

    model, fit_info = fit_with_restarts(
        build_model,
        D.likelihood_values(train, specs),
        D.likelihood_values(validation, specs),
        config,
        restarts=int(args.restarts),
        seed=cell_seed(int(args.seed), domain, model_key),
        progress_label=label,
    )
    model.canonicalize()

    predictions = leave_one_out_predictions(model, D.likelihood_values(test, specs))
    predictions = attach_raw_scale_values(predictions, test, specs)
    metrics = leave_one_out_metrics(predictions, train)
    base = {
        "domain": domain,
        "domain_display": cfg.display,
        "model_key": model_key,
        "model_display": str(group["model_display"].iloc[0]),
        "model_position": PIPELINE_POSITION[model_key],
        "latent_dim": LATENT_DIM,
    }
    parameters = pd.DataFrame(model.parameter_rows())
    for frame in (metrics, predictions, parameters):
        for column, value in reversed(list(base.items())):
            frame.insert(0, column, value)
    fit_summary = pd.DataFrame(
        [
            {
                **base,
                "n_train": int(len(train)),
                "n_validation": int(len(validation)),
                "n_test": int(len(test)),
                "quadrature_nodes": int(model.n_nodes),
                **{key: value for key, value in fit_info.items() if key != "fit_success"},
            }
        ]
    )
    return {
        "fit_summary.csv": fit_summary,
        "parameters.csv": parameters,
        "leave_one_output_out_predictions.csv": predictions[list(PREDICTION_COLUMNS)],
        "leave_one_output_out_metrics.csv": metrics,
    }


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    device = resolve_device(str(args.device))
    # Row order of the merged tables: models alphabetically, then domains.
    cells = [(domain, model) for model in args.models for domain in args.domains]
    datasets: dict[str, pd.DataFrame] = {}
    for index, (domain, model_key) in enumerate(cells):
        if index % int(args.shards) != int(args.shard):
            continue
        leaf = output_dir / "cells" / domain / model_key
        if (leaf / "complete.json").exists():
            print(f"Complete: {domain}/{model_key}", flush=True)
            continue
        if domain not in datasets:
            datasets[domain] = D.load_dataset(domain)
        tables = fit_cell(domain, model_key, datasets[domain], args, device)
        leaf.parent.mkdir(parents=True, exist_ok=True)
        attempt = Path(tempfile.mkdtemp(prefix=f"{model_key}.attempt-", dir=leaf.parent))
        for filename, frame in tables.items():
            frame.to_csv(attempt / filename, index=False)
        (attempt / "complete.json").write_text(json.dumps({"domain": domain, "model": model_key}) + "\n")
        os.rename(attempt, leaf)
    leaves = [output_dir / "cells" / domain / model for domain, model in cells]
    if not all((leaf / "complete.json").exists() for leaf in leaves):
        print("Not all cells complete; skipping merge", flush=True)
        return
    for filename in RESULT_TABLES:
        frame = pd.concat([pd.read_csv(leaf / filename) for leaf in leaves], ignore_index=True)
        frame.to_csv(output_dir / filename, index=False)
    print(f"Merged {len(leaves)} cells into {output_dir}", flush=True)


if __name__ == "__main__":
    main()
