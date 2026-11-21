"""One-dimensional probabilistic latent-belief model (Methods, "Latent variable model"; all figures).

Each case has a scalar latent belief ``z`` in (0, 1) with a fitted prior; every
visible output is conditionally independent given ``z``::

    z_i ~ p(z)
    y_ij | z_i ~ p_j(y_ij | z_i, theta_j)

* Prior: Gauss-Legendre quadrature weights on (0, 1) times a free log-density
  that is piecewise linear in ``logit(z)`` between seven evenly spaced knots
  (renormalised over the quadrature grid).
* Stated probability: Beta likelihood with mean ``z`` (this anchors the scale)
  and a fitted precision.
* Binary decisions: ``logit P(y=1|z) = a_j + b_j logit(z)`` with ``b_j > 0``.
  Stated thresholds are metadata only; they do not enter the likelihood.
* Ordinal reports: ordered logit with score ``b_j logit(z)``.
* ``response_curve="monotone_spline"`` (Appendix sensitivity analysis) replaces
  each linear score in ``logit(z)`` by a monotone piecewise-linear curve with
  knots at fixed latent probabilities.

``z`` is integrated out on the quadrature grid; missing outputs are handled by
masking their likelihood. Parameters are fitted by maximum marginal likelihood
(full-batch Adam, early stopping on validation NLL).

Public API: :class:`OutputSpec`, :class:`ProbabilisticLatentModel`,
:class:`FitConfig`, :func:`fit_model`, :func:`concordance_index`,
:func:`restore_model`, :func:`load_fit`, :func:`restore_cell`.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

os.environ.setdefault("TORCH_DISABLE_DYNAMO", "1")
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
import torch.nn.functional as F  # noqa: E402

RESPONSE_CURVES = ("linear", "monotone_spline")
SPLINE_KNOT_PROBABILITIES = (0.001, 0.03, 0.15, 0.5, 0.85, 0.97, 0.999)
PRIOR_KNOTS = 7
# Pseudo output name under which the prior's knot values are written to
# parameters.csv, so restore_model can rebuild the prior exactly.
LATENT_PRIOR_ROW_NAME = "__latent_prior__"


@dataclass(frozen=True)
class OutputSpec:
    """Observation family of one visible output.

    ``kind`` is ``probability``, ``binary`` or ``ordinal``.  ``observation_column``
    names the dataset column holding the likelihood-scale value when it differs
    from ``name``.
    """

    name: str
    kind: str
    nominal_threshold: float | None = None
    n_categories: int | None = None
    observation_column: str | None = None


def gauss_legendre_grid(
    n_points: int, *, dtype: torch.dtype = torch.float64
) -> tuple[torch.Tensor, torch.Tensor]:
    """Return nodes on [0, 1] and weights normalized for a Uniform(0, 1) prior."""

    if int(n_points) < 2:
        raise ValueError("quadrature requires at least two points")
    nodes, weights = np.polynomial.legendre.leggauss(int(n_points))
    nodes = (nodes + 1.0) / 2.0
    weights = weights / 2.0
    return torch.as_tensor(nodes, dtype=dtype), torch.as_tensor(weights, dtype=dtype)


def logit(value: float | torch.Tensor, eps: float = 1e-9) -> float | torch.Tensor:
    if isinstance(value, torch.Tensor):
        bounded = value.clamp(eps, 1.0 - eps)
        return torch.log(bounded) - torch.log1p(-bounded)
    bounded_float = min(1.0 - eps, max(eps, float(value)))
    return math.log(bounded_float) - math.log1p(-bounded_float)


def inverse_softplus(value: float) -> float:
    """Inverse softplus used for parameter initialisation."""

    if value <= 0:
        raise ValueError("inverse softplus is only defined here for positive values")
    return math.log(math.expm1(value))


def _restore_inverse_softplus(value: float) -> float:
    """Inverse softplus used when restoring parameters from parameters.csv."""

    if value <= 0.0:
        raise ValueError(f"inverse softplus needs a positive value, got {value}")
    if value > 20.0:
        return value + math.log1p(-math.exp(-value))
    return math.log(math.expm1(value))


class OutputParameters(nn.ParameterDict):
    def _key_to_attr(self, key: str) -> str:
        return super()._key_to_attr(key).replace("%", "%25").replace(".", "%2E")


class ProbabilisticLatentModel(nn.Module):
    """Mixed-output latent-trait model with a fitted latent prior."""

    min_slope = 1e-3
    min_gap = 1e-3

    def __init__(
        self,
        specs: Sequence[OutputSpec],
        *,
        quadrature_points: int = 128,
        probability_epsilon: float = 1e-4,
        response_curve: str = "linear",
        spline_knot_probabilities: Sequence[float] = SPLINE_KNOT_PROBABILITIES,
        prior_knots: int = PRIOR_KNOTS,
        dtype: torch.dtype = torch.float64,
    ) -> None:
        super().__init__()
        self.specs = tuple(specs)
        if not self.specs:
            raise ValueError("at least one output specification is required")
        if not 0.0 < float(probability_epsilon) < 0.5:
            raise ValueError("probability_epsilon must lie in (0, 0.5)")
        self.probability_epsilon = float(probability_epsilon)
        if response_curve not in RESPONSE_CURVES:
            raise ValueError("response_curve must be 'linear' or 'monotone_spline'")
        self.response_curve = str(response_curve)
        if int(prior_knots) < 3:
            raise ValueError("a fitted latent prior needs at least three knots")
        self.prior_knots = int(prior_knots)
        self.dtype = dtype

        nodes, weights = gauss_legendre_grid(quadrature_points, dtype=dtype)
        self.register_buffer("z_grid", nodes)
        self.register_buffer("log_prior_weights", torch.log(weights))
        self.register_buffer("logit_z_grid", logit(nodes))
        spline_knots = torch.as_tensor(spline_knot_probabilities, dtype=dtype)
        if (
            spline_knots.ndim != 1
            or spline_knots.numel() < 3
            or torch.any(spline_knots <= 0.0)
            or torch.any(spline_knots >= 1.0)
            or torch.any(spline_knots[1:] <= spline_knots[:-1])
        ):
            raise ValueError(
                "spline_knot_probabilities must be at least three increasing values in (0, 1)"
            )
        self.register_buffer("spline_knot_probabilities", spline_knots)
        self.register_buffer("spline_knot_logits", logit(spline_knots))

        # The prior multiplies the uniform quadrature weights by a free
        # log-density, piecewise linear in logit(z); zero knots give Uniform(0, 1).
        self.register_buffer(
            "prior_knot_logits",
            torch.linspace(
                float(self.logit_z_grid[0]),
                float(self.logit_z_grid[-1]),
                self.prior_knots,
                dtype=dtype,
            ),
        )
        self.prior_knot_values = nn.Parameter(torch.zeros(self.prior_knots, dtype=dtype))

        self.raw_probability_precision = nn.Parameter(
            torch.tensor(inverse_softplus(18.0), dtype=dtype)
        )

        # Registration order fixes the parameter order seen by gradient clipping.
        self.binary_intercepts = OutputParameters()
        self.binary_raw_slopes = OutputParameters()
        self.ordinal_raw_slopes = OutputParameters()
        self.ordinal_first_cutpoints = OutputParameters()
        self.ordinal_raw_gaps = OutputParameters()
        self.spline_first_values = OutputParameters()
        self.spline_raw_gaps = OutputParameters()

        slope_start = inverse_softplus(1.0 - self.min_slope)
        gap_start = inverse_softplus(1.0 - self.min_gap)
        for spec in self.specs:
            if spec.kind == "probability":
                continue
            if spec.kind == "binary":
                if self.response_curve == "monotone_spline":
                    self._initialize_spline_curve(spec.name, initial_slope=1.0)
                else:
                    self.binary_raw_slopes[spec.name] = nn.Parameter(
                        torch.tensor(slope_start, dtype=dtype)
                    )
                    self.binary_intercepts[spec.name] = nn.Parameter(
                        torch.tensor(0.0, dtype=dtype)
                    )
                continue
            if spec.kind == "ordinal":
                n_categories = int(spec.n_categories or 0)
                if n_categories < 2:
                    raise ValueError(f"ordinal output {spec.name!r} needs at least two categories")
                if self.response_curve == "monotone_spline":
                    self._initialize_spline_curve(spec.name, initial_slope=1.0)
                else:
                    self.ordinal_raw_slopes[spec.name] = nn.Parameter(
                        torch.tensor(slope_start, dtype=dtype)
                    )
                self.ordinal_first_cutpoints[spec.name] = nn.Parameter(
                    torch.tensor(-1.5, dtype=dtype)
                )
                self.ordinal_raw_gaps[spec.name] = nn.Parameter(
                    torch.full((n_categories - 2,), gap_start, dtype=dtype)
                )
                continue
            raise ValueError(f"unknown output kind {spec.kind!r} for {spec.name!r}")

    # Curves
    def _initialize_spline_curve(self, name: str, *, initial_slope: float) -> None:
        knot_values = float(initial_slope) * self.spline_knot_logits
        gaps = torch.diff(knot_values)
        self.spline_first_values[name] = nn.Parameter(knot_values[0].detach().clone())
        self.spline_raw_gaps[name] = nn.Parameter(
            torch.as_tensor(
                [inverse_softplus(float(gap) - self.min_gap) for gap in gaps],
                dtype=self.dtype,
            )
        )

    def spline_knot_values(self, name: str) -> torch.Tensor:
        first = self.spline_first_values[name].reshape(1)
        gaps = self.min_gap + F.softplus(self.spline_raw_gaps[name])
        return torch.cat((first, first + torch.cumsum(gaps, dim=0)))

    def monotone_spline(self, name: str) -> torch.Tensor:
        """Evaluate a monotone piecewise-linear curve on the quadrature grid."""

        x = self.spline_knot_logits
        y = self.spline_knot_values(name)
        interval = torch.bucketize(self.logit_z_grid, x[1:-1])
        x0 = x[interval]
        x1 = x[interval + 1]
        weight = ((self.logit_z_grid - x0) / (x1 - x0)).clamp(0.0, 1.0)
        return y[interval] + weight * (y[interval + 1] - y[interval])

    def prior_log_weights(self) -> torch.Tensor:
        """Normalized log quadrature weights under the fitted latent prior."""

        x = self.prior_knot_logits
        y = self.prior_knot_values
        interval = torch.bucketize(self.logit_z_grid, x[1:-1])
        x0 = x[interval]
        x1 = x[interval + 1]
        weight = ((self.logit_z_grid - x0) / (x1 - x0)).clamp(0.0, 1.0)
        log_density = y[interval] + weight * (y[interval + 1] - y[interval])
        unnormalized = self.log_prior_weights + log_density
        return unnormalized - torch.logsumexp(unnormalized, dim=0)

    @property
    def probability_precision(self) -> torch.Tensor:
        return 2.0 + F.softplus(self.raw_probability_precision)

    def binary_slope(self, name: str) -> torch.Tensor:
        return self.min_slope + F.softplus(self.binary_raw_slopes[name])

    def binary_intercept(self, spec: OutputSpec) -> torch.Tensor:
        return self.binary_intercepts[spec.name]

    def binary_logits(self, spec: OutputSpec) -> torch.Tensor:
        """logit P(y=1 | z_q) on the quadrature grid."""

        if self.response_curve == "monotone_spline":
            return self.monotone_spline(spec.name)
        slope = self.binary_slope(spec.name)
        return self.binary_intercept(spec) + slope * self.logit_z_grid

    def binary_implied_threshold(self, spec: OutputSpec) -> torch.Tensor:
        """Return z where the fitted Bernoulli probability equals one half."""

        if self.response_curve == "monotone_spline":
            values = self.spline_knot_values(spec.name)
            if bool(values[0] >= 0.0):
                return torch.zeros((), dtype=self.dtype, device=self.z_grid.device)
            if bool(values[-1] < 0.0):
                return torch.ones((), dtype=self.dtype, device=self.z_grid.device)
            upper = torch.nonzero(values >= 0.0, as_tuple=False)[0, 0]
            lower = upper - 1
            fraction = -values[lower] / (values[upper] - values[lower])
            logit_crossing = self.spline_knot_logits[lower] + fraction * (
                self.spline_knot_logits[upper] - self.spline_knot_logits[lower]
            )
            return torch.sigmoid(logit_crossing)
        return torch.sigmoid(-self.binary_intercept(spec) / self.binary_slope(spec.name))

    def ordinal_slope(self, name: str) -> torch.Tensor:
        return self.min_slope + F.softplus(self.ordinal_raw_slopes[name])

    def ordinal_cutpoints(self, spec: OutputSpec) -> torch.Tensor:
        first = self.ordinal_first_cutpoints[spec.name].reshape(1)
        raw_gaps = self.ordinal_raw_gaps[spec.name]
        if raw_gaps.numel() == 0:
            return first
        gaps = self.min_gap + F.softplus(raw_gaps)
        return torch.cat((first, first + torch.cumsum(gaps, dim=0)))

    def ordinal_probabilities(self, spec: OutputSpec) -> torch.Tensor:
        """Return P(y=c | z_q) with shape [quadrature_point, category]."""

        score = (
            self.monotone_spline(spec.name)
            if self.response_curve == "monotone_spline"
            else self.ordinal_slope(spec.name) * self.logit_z_grid
        )
        cutpoints = self.ordinal_cutpoints(spec)
        cumulative = torch.sigmoid(cutpoints[None, :] - score[:, None])
        pieces = [cumulative[:, :1]]
        if cumulative.shape[1] > 1:
            pieces.append(cumulative[:, 1:] - cumulative[:, :-1])
        pieces.append(1.0 - cumulative[:, -1:])
        probabilities = torch.cat(pieces, dim=1)
        return probabilities.clamp_min(torch.finfo(self.dtype).tiny)

    def probability_mean(self) -> torch.Tensor:
        """The direct-probability likelihood anchors E[y | z] to z."""

        return self.z_grid

    def conditional_mean(self, spec: OutputSpec) -> torch.Tensor:
        """E[y | z_q] on the likelihood scale (ordinal categories are 1..K)."""

        if spec.kind == "probability":
            return self.probability_mean()
        if spec.kind == "binary":
            return torch.sigmoid(self.binary_logits(spec))
        if spec.kind == "ordinal":
            probabilities = self.ordinal_probabilities(spec)
            categories = torch.arange(
                1,
                probabilities.shape[1] + 1,
                dtype=self.dtype,
                device=self.z_grid.device,
            )
            return probabilities @ categories
        raise ValueError(spec.kind)

    # Likelihood
    def output_log_prob(self, output_index: int, observations: torch.Tensor) -> torch.Tensor:
        """Return log p(y_i | z_q) with shape [case, quadrature_point]."""

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
            result = (
                safe_values[:, None] * F.logsigmoid(logits)[None, :]
                + (1.0 - safe_values[:, None]) * F.logsigmoid(-logits)[None, :]
            )
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
            raise ValueError(
                f"expected observations with shape [n, {len(self.specs)}], got {tuple(values.shape)}"
            )
        for output_index, spec in enumerate(self.specs):
            column = values[:, output_index]
            column = column[torch.isfinite(column)]
            if column.numel() == 0:
                continue
            if spec.kind in {"probability", "binary"} and (
                torch.any(column < 0.0) or torch.any(column > 1.0)
            ):
                raise ValueError(f"{spec.name} contains values outside [0, 1]")
            if spec.kind == "binary" and torch.any((column != 0.0) & (column != 1.0)):
                raise ValueError(f"{spec.name} contains non-binary values")
            if spec.kind == "ordinal":
                n_categories = int(spec.n_categories or 0)
                if torch.any(column != torch.round(column)) or torch.any(column < 1) or torch.any(
                    column > n_categories
                ):
                    raise ValueError(
                        f"{spec.name} must contain integer categories 1..{n_categories}"
                    )

    def log_likelihood_by_output(self, observations: torch.Tensor) -> torch.Tensor:
        """Return shape [case, output, quadrature_point]."""

        return torch.stack(
            [self.output_log_prob(j, observations[:, j]) for j in range(len(self.specs))],
            dim=1,
        )

    def marginal_log_prob(
        self,
        observations: torch.Tensor,
        include_outputs: Sequence[int] | None = None,
    ) -> torch.Tensor:
        """Evaluate log integral p(z) product_j p(y_j | z) dz per case."""

        log_by_output = self.log_likelihood_by_output(observations)
        if include_outputs is not None:
            indices = torch.as_tensor(include_outputs, dtype=torch.long, device=observations.device)
            log_by_output = log_by_output.index_select(1, indices)
        joint = log_by_output.sum(dim=1) + self.prior_log_weights()[None, :]
        return torch.logsumexp(joint, dim=1)

    def posterior_log_weights(
        self,
        observations: torch.Tensor,
        include_outputs: Sequence[int] | None = None,
    ) -> torch.Tensor:
        """Return normalized log p(z_q | selected observations) for every case."""

        log_by_output = self.log_likelihood_by_output(observations)
        if include_outputs is not None:
            indices = torch.as_tensor(include_outputs, dtype=torch.long, device=observations.device)
            log_by_output = log_by_output.index_select(1, indices)
        log_joint = log_by_output.sum(dim=1) + self.prior_log_weights()[None, :]
        return log_joint - torch.logsumexp(log_joint, dim=1, keepdim=True)

    def regularization(self) -> torch.Tensor:
        terms = [parameter.square().mean() for parameter in self.parameters()]
        return torch.stack(terms).mean() if terms else torch.zeros((), dtype=self.dtype)

    # Export
    def parameter_rows(self) -> list[dict[str, Any]]:
        """Derived parameters, in the layout of parameters.csv."""

        rows: list[dict[str, Any]] = []
        for spec in self.specs:
            base = {"output_name": spec.name, "output_kind": spec.kind}
            if spec.kind == "probability":
                rows.append({**base, "parameter": "precision", "value": float(self.probability_precision)})
            elif spec.kind == "binary":
                rows.append(
                    {**base, "parameter": "nominal_threshold", "value": spec.nominal_threshold}
                )
                rows.append(
                    {
                        **base,
                        "parameter": "implied_threshold",
                        "value": float(self.binary_implied_threshold(spec)),
                    }
                )
                if self.response_curve == "monotone_spline":
                    rows.extend(self._spline_parameter_rows(base, spec.name))
                else:
                    rows.append(
                        {**base, "parameter": "slope", "value": float(self.binary_slope(spec.name))}
                    )
                    rows.append(
                        {
                            **base,
                            "parameter": "intercept",
                            "value": float(self.binary_intercept(spec)),
                        }
                    )
            elif spec.kind == "ordinal":
                if self.response_curve == "monotone_spline":
                    rows.extend(self._spline_parameter_rows(base, spec.name))
                else:
                    rows.append(
                        {**base, "parameter": "slope", "value": float(self.ordinal_slope(spec.name))}
                    )
                for cutpoint_index, value in enumerate(self.ordinal_cutpoints(spec), start=1):
                    rows.append(
                        {
                            **base,
                            "parameter": f"cutpoint_{cutpoint_index}",
                            "value": float(value),
                        }
                    )
        base = {"output_name": LATENT_PRIOR_ROW_NAME, "output_kind": "latent_prior"}
        rows.extend(
            {**base, "parameter": f"prior_knot_value_{index}", "value": float(value)}
            for index, value in enumerate(self.prior_knot_values, start=1)
        )
        return rows

    def _spline_parameter_rows(self, base: dict[str, Any], name: str) -> list[dict[str, Any]]:
        return [
            {**base, "parameter": f"spline_value_{index}", "value": float(value)}
            for index, value in enumerate(self.spline_knot_values(name), start=1)
        ]


# Fitting
@dataclass(frozen=True)
class FitConfig:
    epochs: int = 30000
    patience: int = 500
    min_delta: float = 1e-5
    learning_rate: float = 2e-2
    # The paper's fits are unpenalized.  The zero-weighted penalty term stays in
    # the objective because removing it changes floating-point rounding.
    l2: float = 0.0
    progress_every: int = 2000


def fit_model(
    model: nn.Module,
    train_values: np.ndarray,
    validation_values: np.ndarray,
    config: FitConfig,
    *,
    progress_label: str = "model",
) -> dict[str, Any]:
    """Maximum marginal likelihood with early stopping on validation NLL.

    Works for any model exposing ``validate_observations``,
    ``marginal_log_prob``, ``regularization`` and ``dtype`` (also the 2-D model).
    The best-validation state is restored before returning.
    """

    model.validate_observations(train_values)
    model.validate_observations(validation_values)
    train = torch.as_tensor(train_values, dtype=model.dtype, device=model.z_grid.device)
    validation = torch.as_tensor(validation_values, dtype=model.dtype, device=model.z_grid.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=float(config.learning_rate))

    best_state: dict[str, torch.Tensor] | None = None
    best_validation_nll = math.inf
    best_epoch = -1
    stale = 0
    epochs_ran = 0

    for epoch in range(int(config.epochs)):
        model.train()
        optimizer.zero_grad()
        train_nll_tensor = -model.marginal_log_prob(train).mean()
        objective = train_nll_tensor + float(config.l2) * model.regularization()
        if not torch.isfinite(objective):
            raise RuntimeError(f"non-finite training objective at epoch {epoch}")
        objective.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0)
        optimizer.step()
        epochs_ran = epoch + 1

        model.eval()
        with torch.no_grad():
            validation_nll = float(-model.marginal_log_prob(validation).mean())
            train_nll = float(train_nll_tensor)
        if validation_nll < best_validation_nll - float(config.min_delta):
            best_state = {
                name: value.detach().cpu().clone() for name, value in model.state_dict().items()
            }
            best_validation_nll = validation_nll
            best_epoch = epoch
            stale = 0
        else:
            stale += 1

        if int(config.progress_every) > 0 and (
            epoch == 0 or (epoch + 1) % int(config.progress_every) == 0
        ):
            print(
                f"[{progress_label}] epoch={epoch + 1} train_nll={train_nll:.6f} "
                f"validation_nll={validation_nll:.6f}",
                flush=True,
            )
        if stale >= int(config.patience):
            break

    if best_state is None:
        raise RuntimeError("fit never produced a finite validation objective")
    model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        best_train_nll = float(-model.marginal_log_prob(train).mean())
    return {
        "fit_success": 1,
        "best_epoch": int(best_epoch),
        "epochs_ran": int(epochs_ran),
        "best_train_mean_nll": float(best_train_nll),
        "best_validation_mean_nll": float(best_validation_nll),
    }


def concordance_index(actual: Iterable[float], prediction: Iterable[float]) -> tuple[float, int]:
    """Harrell-style c-index over pairs with different outcomes; ties score 1/2."""

    y = np.asarray(list(actual), dtype=float)
    score = np.asarray(list(prediction), dtype=float)
    finite = np.isfinite(y) & np.isfinite(score)
    y = y[finite]
    score = score[finite]
    concordant = 0.0
    comparable = 0
    for row_index in range(len(y) - 1):
        outcome_difference = y[row_index] - y[row_index + 1 :]
        score_difference = score[row_index] - score[row_index + 1 :]
        usable = outcome_difference != 0
        if not np.any(usable):
            continue
        products = outcome_difference[usable] * score_difference[usable]
        concordant += float(np.sum(products > 0))
        concordant += 0.5 * float(np.sum(score_difference[usable] == 0))
        comparable += int(np.sum(usable))
    if comparable == 0:
        return math.nan, 0
    return concordant / comparable, comparable


# Restoring fitted models
def _parameter_lookup(rows: pd.DataFrame, output_name: str, parameter: str) -> float:
    selected = rows[(rows["output_name"] == output_name) & (rows["parameter"] == parameter)]
    if len(selected) != 1:
        raise ValueError(
            f"expected one parameter row for {output_name}/{parameter}, found {len(selected)}"
        )
    return float(selected["value"].iloc[0])


def restore_model(
    specs: Sequence[OutputSpec],
    parameter_rows: pd.DataFrame,
    *,
    quadrature_points: int,
    probability_epsilon: float,
    response_curve: str = "linear",
    spline_knot_probabilities: Sequence[float] = SPLINE_KNOT_PROBABILITIES,
) -> ProbabilisticLatentModel:
    """Rebuild a fitted model (on CPU, in eval mode) from its parameters.csv rows."""

    model = ProbabilisticLatentModel(
        specs,
        quadrature_points=int(quadrature_points),
        probability_epsilon=float(probability_epsilon),
        response_curve=str(response_curve),
        spline_knot_probabilities=spline_knot_probabilities,
    )

    def restore_spline(name: str) -> None:
        values = np.array(
            [
                _parameter_lookup(parameter_rows, name, f"spline_value_{index}")
                for index in range(1, len(spline_knot_probabilities) + 1)
            ],
            dtype=float,
        )
        model.spline_first_values[name].copy_(torch.tensor(values[0], dtype=model.dtype))
        raw_gaps = [_restore_inverse_softplus(gap - model.min_gap) for gap in np.diff(values)]
        model.spline_raw_gaps[name].copy_(torch.tensor(raw_gaps, dtype=model.dtype))

    with torch.no_grad():
        knot_values = [
            _parameter_lookup(parameter_rows, LATENT_PRIOR_ROW_NAME, f"prior_knot_value_{index}")
            for index in range(1, model.prior_knot_values.numel() + 1)
        ]
        model.prior_knot_values.copy_(torch.tensor(knot_values, dtype=model.dtype))
        for spec in specs:
            name = spec.name
            if spec.kind == "probability":
                precision = _parameter_lookup(parameter_rows, name, "precision")
                model.raw_probability_precision.copy_(
                    torch.tensor(_restore_inverse_softplus(precision - 2.0), dtype=model.dtype)
                )
            elif spec.kind == "binary":
                if response_curve == "monotone_spline":
                    restore_spline(name)
                    continue
                slope = _parameter_lookup(parameter_rows, name, "slope")
                model.binary_raw_slopes[name].copy_(
                    torch.tensor(_restore_inverse_softplus(slope - model.min_slope), dtype=model.dtype)
                )
                intercept = _parameter_lookup(parameter_rows, name, "intercept")
                model.binary_intercepts[name].copy_(torch.tensor(intercept, dtype=model.dtype))
            elif spec.kind == "ordinal":
                if response_curve == "monotone_spline":
                    restore_spline(name)
                else:
                    slope = _parameter_lookup(parameter_rows, name, "slope")
                    model.ordinal_raw_slopes[name].copy_(
                        torch.tensor(
                            _restore_inverse_softplus(slope - model.min_slope), dtype=model.dtype
                        )
                    )
                cutpoints = np.array(
                    [
                        _parameter_lookup(parameter_rows, name, f"cutpoint_{index}")
                        for index in range(1, int(spec.n_categories or 0))
                    ],
                    dtype=float,
                )
                model.ordinal_first_cutpoints[name].copy_(
                    torch.tensor(cutpoints[0], dtype=model.dtype)
                )
                if len(cutpoints) > 1:
                    raw_gaps = [
                        _restore_inverse_softplus(gap - model.min_gap) for gap in np.diff(cutpoints)
                    ]
                    model.ordinal_raw_gaps[name].copy_(torch.tensor(raw_gaps, dtype=model.dtype))
            else:  # pragma: no cover - domain construction rejects this
                raise ValueError(spec.kind)
    model.eval()
    return model


def load_fit(fit_dir: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Read ``parameters.csv`` and ``run_metadata.json`` of a fit directory."""

    fit_dir = Path(fit_dir)
    parameters = pd.read_csv(fit_dir / "parameters.csv")
    metadata = json.loads((fit_dir / "run_metadata.json").read_text(encoding="utf-8"))
    return parameters, metadata


def restore_cell(
    parameters: pd.DataFrame,
    metadata: dict[str, Any],
    domain: str,
    model_key: str,
    specs: Sequence[OutputSpec],
) -> ProbabilisticLatentModel:
    """Restore the fitted model of one domain/model cell of a fit directory."""

    rows = parameters[parameters["domain"].eq(domain) & parameters["model_key"].eq(model_key)]
    return restore_model(
        specs,
        rows,
        quadrature_points=int(metadata["quadrature_points"]),
        probability_epsilon=float(metadata["probability_epsilon"]),
        response_curve=str(metadata["response_curve"]),
        spline_knot_probabilities=metadata["spline_knot_probabilities"],
    )
