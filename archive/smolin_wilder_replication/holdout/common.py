"""Shared pieces of the held-out framing / domain analyses (Results, "Prediction", Eq. 1).

The twelve parameterized binary decisions of each domain belong to three
framing families (threshold, betting, cost), each asked at the normative
thresholds 5%, 10%, 20% and 33%.  For zero-shot transfer, a family's decisions
share one link

    logit P(y_j = 1 | z) = alpha + beta * (logit(z) - logit(tau_j)),   beta > 0,

implemented by :class:`SharedFamilyLatentModel`.  Domain-holdout fits pool two
source domains on a *generic* schema (``threshold_05pct``, ``betting_20pct``,
...) to which each domain's decision outputs are mapped.

This module also provides the cell runner used by every holdout fit stage:
each fit cell is written atomically to ``<output>/cells/...`` (so the grid can
be split with ``--shard i --shards n``) and the cells are merged once all are
complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

from archive.smolin_wilder_replication.config import DOMAINS
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.model import (
    FitConfig,
    OutputSpec,
    ProbabilisticLatentModel,
    concordance_index,
    inverse_softplus,
    logit,
)
from archive.smolin_wilder_replication.models import PIPELINE_POSITION, pipeline_order

# Sorted family order; it enters the per-cell fit seeds.
FAMILIES = ("betting", "cost", "threshold")
THRESHOLDS = (0.05, 0.10, 0.20, 1.0 / 3.0)
GLOBAL_LINK = "global_parameterized_binary_link"
C_INDEX_SCORE_DECIMALS = 12
DEFAULT_SEED = 20260819


def capability_rank(model_key: str) -> int:
    """Rank used for ordering and seeds (positions include the excluded model)."""

    return PIPELINE_POSITION[model_key] + 1


def output_family(output_name: str) -> str | None:
    """Framing family of a parameterized binary output, else None."""

    if "no_threshold" in output_name:
        return None
    if "bet_payoff" in output_name:
        return "betting"
    if "cost_fn" in output_name:
        return "cost"
    if "threshold" in output_name:
        return "threshold"
    return None


def threshold_label(value: float) -> str:
    if math.isclose(float(value), 1.0 / 3.0, rel_tol=0.0, abs_tol=1e-9):
        return "33pct"
    return f"{int(round(100.0 * float(value))):02d}pct"


def generic_name(family: str, threshold: float) -> str:
    return f"{family}_{threshold_label(threshold)}"


GENERIC_SPECS = tuple(
    OutputSpec(generic_name(family, threshold), "binary", threshold)
    for family in ("threshold", "betting", "cost")
    for threshold in THRESHOLDS
)
GENERIC_FAMILIES = {spec.name: spec.name.rsplit("_", maxsplit=1)[0] for spec in GENERIC_SPECS}


def family_of(name: str) -> str | None:
    """Framing family of a generic or domain-specific decision name."""

    return GENERIC_FAMILIES[name] if name in GENERIC_FAMILIES else output_family(name)


# Model
class SharedFamilyLatentModel(ProbabilisticLatentModel):
    """Latent model whose listed binary outputs share one link per family.

    Outputs absent from ``binary_families`` keep their own intercept and slope.
    """

    def __init__(
        self,
        specs: Sequence[OutputSpec],
        *,
        binary_families: Mapping[str, str],
        **kwargs: Any,
    ) -> None:
        super().__init__(specs, **kwargs)
        spec_by_name = {spec.name: spec for spec in specs}
        for name in binary_families:
            spec = spec_by_name[name]
            if spec.kind != "binary" or spec.nominal_threshold is None:
                raise ValueError(f"shared-family output {name!r} must be thresholded binary")
        self.binary_families = dict(binary_families)
        families = sorted(set(self.binary_families.values()))
        slope_start = inverse_softplus(1.0 - self.min_slope)
        self.family_intercepts = nn.ParameterDict(
            {family: nn.Parameter(torch.tensor(0.0, dtype=self.dtype)) for family in families}
        )
        self.family_raw_slopes = nn.ParameterDict(
            {family: nn.Parameter(torch.tensor(slope_start, dtype=self.dtype)) for family in families}
        )

    def family_slope(self, family: str) -> torch.Tensor:
        return self.min_slope + F.softplus(self.family_raw_slopes[family])

    def binary_logits(self, spec: OutputSpec) -> torch.Tensor:
        family = self.binary_families.get(spec.name)
        if family is None:
            return super().binary_logits(spec)
        centered = self.logit_z_grid - logit(float(spec.nominal_threshold))
        return self.family_intercepts[family] + self.family_slope(family) * centered

    def binary_implied_threshold(self, spec: OutputSpec) -> torch.Tensor:
        family = self.binary_families.get(spec.name)
        if family is None:
            return super().binary_implied_threshold(spec)
        crossing = logit(float(spec.nominal_threshold)) - (
            self.family_intercepts[family] / self.family_slope(family)
        )
        return torch.sigmoid(crossing)

    def family_parameter_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for family in sorted(self.family_intercepts):
            rows.append(
                {"family": family, "parameter": "offset", "value": float(self.family_intercepts[family].detach())}
            )
            rows.append(
                {"family": family, "parameter": "slope", "value": float(self.family_slope(family).detach())}
            )
        return rows


def build_model(
    specs: Sequence[OutputSpec],
    binary_families: Mapping[str, str],
    args: argparse.Namespace,
    device: torch.device,
) -> SharedFamilyLatentModel:
    return SharedFamilyLatentModel(
        specs,
        binary_families=binary_families,
        quadrature_points=int(args.quadrature_points),
        probability_epsilon=float(args.probability_epsilon),
    ).to(device)


def posterior_weights(model: ProbabilisticLatentModel, values: np.ndarray) -> torch.Tensor:
    """p(z_q | visible values) per case; NaN entries are not conditioned on."""

    observations = torch.as_tensor(values, dtype=model.dtype, device=model.z_grid.device)
    with torch.no_grad():
        return torch.exp(model.posterior_log_weights(observations))


def predict_binary(model: ProbabilisticLatentModel, weights: torch.Tensor, spec: OutputSpec) -> np.ndarray:
    """Posterior-predictive P(y = 1) of one binary output."""

    with torch.no_grad():
        return (weights @ torch.sigmoid(model.binary_logits(spec))).cpu().numpy()


# Data
def source_column_mapping(cfg: D.DomainConfig) -> dict[str, str]:
    """Generic decision name -> the domain's dataset column."""

    result: dict[str, str] = {}
    for spec in cfg.output_specs:
        family = output_family(spec.name)
        if spec.kind != "binary" or family is None or spec.nominal_threshold is None:
            continue
        key = generic_name(family, float(spec.nominal_threshold))
        if key in result:
            raise ValueError(f"{cfg.key} maps multiple outputs to generic output {key!r}")
        result[key] = spec.observation_column or spec.name
    if set(result) != set(GENERIC_FAMILIES):
        raise ValueError(f"invalid generic mapping for {cfg.key}")
    return result


def generic_values(frame: pd.DataFrame, cfg: D.DomainConfig) -> np.ndarray:
    """[case, generic decision] matrix in GENERIC_SPECS order."""

    mapping = source_column_mapping(cfg)
    return frame[[mapping[spec.name] for spec in GENERIC_SPECS]].to_numpy(dtype=float)


def load_datasets() -> dict[str, pd.DataFrame]:
    return {domain: D.load_dataset(domain) for domain in D.DOMAIN_CONFIGS}


def model_split(dataset: pd.DataFrame, model_key: str, split: str) -> pd.DataFrame:
    rows = dataset[dataset["model_key"].eq(model_key) & dataset["split"].eq(split)].reset_index(drop=True)
    if rows.empty:
        raise ValueError(f"no {split} rows for {model_key}")
    return rows


def pooled_source_values(
    datasets: Mapping[str, pd.DataFrame],
    *,
    held_out_domain: str,
    model_key: str,
    split: str,
    values: Callable[[pd.DataFrame, D.DomainConfig], np.ndarray] = generic_values,
) -> tuple[np.ndarray, dict[str, int]]:
    """Stack one split of the two source domains (never the held-out domain)."""

    pieces: list[np.ndarray] = []
    counts: dict[str, int] = {}
    for domain, cfg in D.DOMAIN_CONFIGS.items():
        if domain == held_out_domain:
            continue
        rows = model_split(datasets[domain], model_key, split)
        pieces.append(values(rows, cfg))
        counts[domain] = int(len(rows))
    return np.concatenate(pieces, axis=0), counts


def source_counts(train_counts: Mapping[str, int], validation_counts: Mapping[str, int]) -> dict[str, int]:
    return {
        **{f"n_train_{domain}": train_counts.get(domain, 0) for domain in D.DOMAIN_CONFIGS},
        **{f"n_validation_{domain}": validation_counts.get(domain, 0) for domain in D.DOMAIN_CONFIGS},
    }


# Metrics
def smoothed_constant_probability(actual: np.ndarray) -> float:
    return float((np.sum(actual) + 0.5) / (len(actual) + 1.0))


def clip_probability(values: np.ndarray, epsilon: float = 1e-6) -> np.ndarray:
    return np.clip(np.asarray(values, dtype=float), epsilon, 1.0 - epsilon)


def binary_metrics(actual: np.ndarray, predicted: np.ndarray, baseline: float) -> dict[str, float]:
    """Metrics of clipped predictions against a constant training-prevalence baseline.

    Used where predictions are scored inside the fitting stage (framing and
    joint holdouts); the c-index ranks the unrounded clipped predictions.
    """

    actual = np.asarray(actual, dtype=float)
    predicted = clip_probability(predicted)
    constant = np.full(len(actual), clip_probability(np.array([baseline]))[0])
    brier = float(np.mean((actual - predicted) ** 2))
    constant_brier = float(np.mean((actual - constant) ** 2))
    log_loss = float(-np.mean(actual * np.log(predicted) + (1.0 - actual) * np.log1p(-predicted)))
    c_index, n_pairs = concordance_index(actual, predicted)
    return {
        "brier_score": brier,
        "constant_brier_score": constant_brier,
        "relative_brier": brier / constant_brier if constant_brier > 0 else math.nan,
        "log_loss": log_loss,
        "accuracy": float(np.mean((predicted >= 0.5) == actual.astype(bool))),
        "c_index": float(c_index),
        "n_comparable_pairs": int(n_pairs),
    }


def exact_binary_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float | int]:
    """Brier score, best-constant baseline and c-index of unclipped predictions.

    Ranking scores are rounded to twelve decimals so that predictions tied up
    to machine precision count as ties on every device.
    """

    finite = np.isfinite(actual) & np.isfinite(predicted)
    actual = np.asarray(actual[finite], dtype=float)
    predicted = np.asarray(predicted[finite], dtype=float)
    if len(actual) == 0:
        raise ValueError("target prompt has no finite evaluation observations")
    brier = float(np.mean((actual - predicted) ** 2))
    prevalence = float(np.mean(actual))
    best_constant_brier = float(np.mean((actual - prevalence) ** 2))
    c_index, n_pairs = concordance_index(actual, np.round(predicted, decimals=C_INDEX_SCORE_DECIMALS))
    return {
        "n_target_test": int(len(actual)),
        "test_prevalence": prevalence,
        "brier_score": brier,
        "best_constant_brier_score": best_constant_brier,
        "r2_vs_best_constant": (1.0 - brier / best_constant_brier if best_constant_brier > 0.0 else math.nan),
        "c_index": float(c_index),
        "n_comparable_pairs": int(n_pairs),
    }


# Fit stages
def fit_parser(description: str, default_output: Path) -> argparse.ArgumentParser:
    """Arguments shared by every holdout fitting stage (canonical defaults)."""

    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--models", nargs="+", choices=pipeline_order(), default=list(pipeline_order()))
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--merge-only", action="store_true")
    parser.add_argument("--quadrature-points", type=int, default=128)
    parser.add_argument("--epochs", type=int, default=30000)
    parser.add_argument("--patience", type=int, default=500)
    parser.add_argument("--min-delta", type=float, default=1e-5)
    parser.add_argument("--lr", type=float, default=2e-2)
    parser.add_argument("--probability-epsilon", type=float, default=1e-4)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--progress-every", type=int, default=2000)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    return parser


def canonical_selection(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Selected domains and models in canonical (domain, pipeline) order."""

    args.domains = [domain for domain in DOMAINS if domain in args.domains]
    args.models = [model for model in pipeline_order() if model in args.models]
    return args.domains, args.models


def fit_config(args: argparse.Namespace) -> FitConfig:
    return FitConfig(
        epochs=int(args.epochs),
        patience=int(args.patience),
        min_delta=float(args.min_delta),
        learning_rate=float(args.lr),
        l2=0.0,
        progress_every=int(args.progress_every),
    )


def resolve_device(requested: str) -> torch.device:
    if requested == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("--device cuda requested but CUDA is unavailable")
    return torch.device(requested)


def seed_everything(seed: int) -> None:
    torch.manual_seed(seed)
    np.random.seed(seed)


def fit_metadata(analysis: str, args: argparse.Namespace, device: str) -> dict[str, Any]:
    """run_metadata.json of a holdout fit directory (read back by the grid)."""

    return {
        "analysis": analysis,
        "domains": list(args.domains),
        "models": list(args.models),
        "latent_prior": "fitted",
        "dtype": "float64",
        "quadrature_points": int(args.quadrature_points),
        "probability_epsilon": float(args.probability_epsilon),
        "fit_config": asdict(fit_config(args)),
        "seed": int(args.seed),
        "device": device,
        "generic_specs": [asdict(spec) for spec in GENERIC_SPECS],
        "domain_column_mappings": {
            domain: source_column_mapping(cfg) for domain, cfg in D.DOMAIN_CONFIGS.items()
        },
    }


def datasets_sha256() -> dict[str, str]:
    return {
        domain: hashlib.sha256(cfg.dataset_path.read_bytes()).hexdigest()
        for domain, cfg in D.DOMAIN_CONFIGS.items()
    }


Cell = tuple[str, ...]


def run_cells(
    cells: Sequence[Cell],
    fit_cell: Callable[[Cell, Mapping[str, pd.DataFrame], argparse.Namespace, torch.device], dict[str, pd.DataFrame]],
    merge: Callable[[dict[str, list[pd.DataFrame]]], dict[str, pd.DataFrame]],
    metadata: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Fit this shard's incomplete cells, then merge if every cell is complete.

    Cells are written to ``<output>/cells/<cell...>``; ``merge`` receives the
    per-cell tables (read back from CSV) in ``cells`` order.
    """

    output_dir = Path(args.output_dir)
    device = resolve_device(str(args.device))
    digest = datasets_sha256()
    if not args.merge_only:
        print(f"Using torch device: {device}", flush=True)
        datasets: dict[str, pd.DataFrame] | None = None
        for index, cell in enumerate(cells):
            if index % int(args.shards) != int(args.shard):
                continue
            leaf = output_dir.joinpath("cells", *cell)
            if (leaf / "complete.json").exists():
                if json.loads((leaf / "complete.json").read_text())["dataset_sha256"] != digest:
                    raise ValueError(f"{leaf} was fitted to different datasets")
                print(f"Complete: {'/'.join(cell)}", flush=True)
                continue
            if datasets is None:
                datasets = load_datasets()
            tables = fit_cell(cell, datasets, args, device)
            leaf.parent.mkdir(parents=True, exist_ok=True)
            attempt = Path(tempfile.mkdtemp(prefix=f"{leaf.name}.attempt-", dir=leaf.parent))
            for filename, frame in tables.items():
                frame.to_csv(attempt / filename, index=False)
            (attempt / "complete.json").write_text(
                json.dumps({"cell": list(cell), "dataset_sha256": digest, "device": str(device)}, indent=2) + "\n"
            )
            os.rename(attempt, leaf)
    leaves = [output_dir.joinpath("cells", *cell) for cell in cells]
    incomplete = [leaf for leaf in leaves if not (leaf / "complete.json").exists()]
    if incomplete:
        print(f"{len(incomplete)} of {len(cells)} cells not yet complete; skipping merge", flush=True)
        return
    devices = {json.loads((leaf / "complete.json").read_text())["device"] for leaf in leaves}
    filenames = sorted(path.name for path in leaves[0].glob("*.csv"))
    tables = {name: [pd.read_csv(leaf / name) for leaf in leaves] for name in filenames}
    for filename, frame in merge(tables).items():
        frame.to_csv(output_dir / filename, index=False)
    metadata = {**metadata, "device": ",".join(sorted(devices)), "dataset_sha256": digest}
    (output_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Merged {len(cells)} cells into {output_dir}", flush=True)
