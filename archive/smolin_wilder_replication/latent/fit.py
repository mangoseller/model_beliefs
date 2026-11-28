"""Fit the probabilistic latent model to every domain x model cell (Methods, "Inference and evaluation").

The paper's fits (``linear``; the Appendix sensitivity analysis uses
``--response-curve monotone_spline``) were run on GPUs and are shipped in the
release bundle under ``fits/main`` and ``fits/spline``; this stage refits them.
Results match the shipped fits only approximately: floating-point differences
between devices can move the early-stopping epoch.

Each cell is fitted independently and written to ``<output>/cells/<domain>/<model>``
(cells already marked complete are skipped), so the grid can be split across
processes or machines with ``--shard i --shards n``.  Once every requested cell
is complete, the cells are merged into ``<output>/{parameters,latent_posteriors,
fit_summary}.csv`` plus ``run_metadata.json``.

Usage::

    python -m archive.smolin_wilder_replication.latent.fit                       # -> build/fits/main
    python -m archive.smolin_wilder_replication.latent.fit --response-curve monotone_spline
    python -m archive.smolin_wilder_replication.latent.fit --shard 0 --shards 8  # one of 8 workers
    python -m archive.smolin_wilder_replication.latent.fit --merge-only

Outputs (merged):

``parameters.csv``  domain, domain_display, model_key, model_display,
    model_position, output_name, output_kind, parameter, value -- reloadable
    with :func:`archive.smolin_wilder_replication.latent.model.restore_cell`.
``latent_posteriors.csv``  test-case posterior of ``z`` given all sixteen
    outputs: z_mean, z_sd, z_q025, z_median, z_q975 plus case identifiers.
``fit_summary.csv``  sample sizes, seeds and optimization diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import torch

from archive.smolin_wilder_replication.config import DOMAINS, REFIT_DIR
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.model import (
    PRIOR_KNOTS,
    RESPONSE_CURVES,
    SPLINE_KNOT_PROBABILITIES,
    FitConfig,
    ProbabilisticLatentModel,
    fit_model,
)
from archive.smolin_wilder_replication.models import PIPELINE_POSITION, pipeline_order

FIT_NAMES = {"linear": "main", "monotone_spline": "spline"}
RESULT_TABLES = ("fit_summary.csv", "parameters.csv", "latent_posteriors.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--response-curve", choices=RESPONSE_CURVES, default="linear")
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--models", nargs="+", choices=pipeline_order(), default=list(pipeline_order()))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Default: <build>/fits/main (linear) or <build>/fits/spline.",
    )
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--merge-only", action="store_true")
    parser.add_argument("--quadrature-points", type=int, default=128)
    parser.add_argument("--epochs", type=int, default=30000)
    parser.add_argument("--patience", type=int, default=500)
    parser.add_argument("--min-delta", type=float, default=1e-5)
    parser.add_argument("--lr", type=float, default=2e-2)
    parser.add_argument("--probability-epsilon", type=float, default=1e-4)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--progress-every", type=int, default=2000)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    return parser.parse_args()


def resolve_device(requested: str) -> torch.device:
    if requested == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("--device cuda requested but CUDA is unavailable")
    return torch.device(requested)


def cell_seed(base_seed: int, domain: str, model_key: str) -> int:
    """Fit seed of one cell (positions include the excluded model; see models.py)."""

    return int(base_seed) + list(D.DOMAIN_CONFIGS).index(domain) * 10 + PIPELINE_POSITION[model_key]


def posterior_summary(model: ProbabilisticLatentModel, log_by_output: torch.Tensor) -> pd.DataFrame:
    """Posterior moments and quantiles of z given all outputs."""

    with torch.no_grad():
        log_joint = (
            model.prior_log_weights()[None, :]
            + log_by_output[:, 0, :]
            + log_by_output[:, 1:, :].sum(dim=1)
        )
        log_weights = log_joint - torch.logsumexp(log_joint, dim=1, keepdim=True)
        weights = torch.exp(log_weights)
        mean = torch.sum(weights * model.z_grid[None, :], dim=1)
        variance = torch.sum(weights * (model.z_grid[None, :] - mean[:, None]).square(), dim=1)
        cdf = torch.cumsum(weights, dim=1)
        result: dict[str, np.ndarray] = {
            "z_mean": mean.cpu().numpy(),
            "z_sd": torch.sqrt(variance.clamp_min(0.0)).cpu().numpy(),
        }
        for probability, name in ((0.025, "z_q025"), (0.5, "z_median"), (0.975, "z_q975")):
            indices = torch.sum(cdf < probability, dim=1).clamp_max(model.z_grid.numel() - 1)
            result[name] = model.z_grid[indices].cpu().numpy()
    return pd.DataFrame(result)


def _insert_base(frame: pd.DataFrame, base: dict[str, Any]) -> pd.DataFrame:
    for column, value in reversed(list(base.items())):
        frame.insert(0, column, value)
    return frame


def fit_cell(
    domain: str,
    model_key: str,
    dataset: pd.DataFrame,
    args: argparse.Namespace,
    device: torch.device,
) -> dict[str, pd.DataFrame]:
    cfg = D.DOMAIN_CONFIGS[domain]
    group, train, validation, test = D.model_splits(dataset, model_key)
    model_display = str(group["model_display"].iloc[0])
    label = f"{domain}/{model_key}"
    print(f"Fitting {label}", flush=True)

    fit_seed = cell_seed(int(args.seed), domain, model_key)
    torch.manual_seed(fit_seed)
    np.random.seed(fit_seed)
    model = ProbabilisticLatentModel(
        cfg.output_specs,
        quadrature_points=int(args.quadrature_points),
        probability_epsilon=float(args.probability_epsilon),
        response_curve=str(args.response_curve),
    ).to(device)
    fit_info = fit_model(
        model,
        D.likelihood_values(train, cfg.output_specs),
        D.likelihood_values(validation, cfg.output_specs),
        fit_config(args),
        progress_label=label,
    )

    test_values = torch.as_tensor(D.likelihood_values(test, cfg.output_specs), dtype=model.dtype)
    with torch.no_grad():
        test_ll = model.log_likelihood_by_output(test_values)
    posterior = posterior_summary(model, test_ll)
    id_columns = [column for column in D.ID_COLUMNS if column in test]
    posterior = pd.concat([test[id_columns].reset_index(drop=True), posterior], axis=1)

    base = {
        "domain": domain,
        "domain_display": cfg.display,
        "model_key": model_key,
        "model_display": model_display,
        "model_position": PIPELINE_POSITION[model_key],
    }
    fit_summary = pd.DataFrame(
        [
            {
                **base,
                "fit_seed": int(fit_seed),
                "n_train": int(len(train)),
                "n_validation": int(len(validation)),
                "n_test": int(len(test)),
                **fit_info,
            }
        ]
    )
    return {
        "fit_summary.csv": fit_summary,
        "parameters.csv": _insert_base(pd.DataFrame(model.parameter_rows()), base),
        "latent_posteriors.csv": _insert_base(posterior, base),
    }


def fit_config(args: argparse.Namespace) -> FitConfig:
    return FitConfig(
        epochs=int(args.epochs),
        patience=int(args.patience),
        min_delta=float(args.min_delta),
        learning_rate=float(args.lr),
        l2=0.0,
        progress_every=int(args.progress_every),
    )


def run_metadata(args: argparse.Namespace, device: str) -> dict[str, Any]:
    return {
        "domains": list(args.domains),
        "models": list(args.models),
        "response_curve": str(args.response_curve),
        "spline_knot_probabilities": list(SPLINE_KNOT_PROBABILITIES),
        "latent_prior": "fitted",
        "prior_knots": int(PRIOR_KNOTS),
        "binary_parameterization": "standard_intercept",
        "quadrature_points": int(args.quadrature_points),
        "probability_epsilon": float(args.probability_epsilon),
        "fit_config": asdict(fit_config(args)),
        "seed": int(args.seed),
        "cell_seed_rule": "seed + 10 * domain_position + model_position",
        "device": device,
        "conditioning": "latent_posteriors condition on all sixteen test outputs",
    }


def dataset_sha256(domain: str) -> str:
    return hashlib.sha256(D.DOMAIN_CONFIGS[domain].dataset_path.read_bytes()).hexdigest()


def merge_cells(cells: Sequence[tuple[str, str]], output_dir: Path, metadata: dict[str, Any]) -> None:
    """Concatenate completed cells (in canonical cell order) into the fit directory."""

    leaves = [output_dir / "cells" / domain / model for domain, model in cells]
    for filename in RESULT_TABLES:
        frame = pd.concat([pd.read_csv(leaf / filename) for leaf in leaves], ignore_index=True)
        frame.to_csv(output_dir / filename, index=False)
    (output_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Merged {len(leaves)} cells into {output_dir}", flush=True)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir or REFIT_DIR / FIT_NAMES[args.response_curve])
    cells = [(domain, model) for domain in args.domains for model in args.models]
    device = resolve_device(str(args.device))
    if not args.merge_only:
        print(f"Using torch device: {device}", flush=True)
        datasets: dict[str, pd.DataFrame] = {}
        for index, (domain, model_key) in enumerate(cells):
            if index % int(args.shards) != int(args.shard):
                continue
            leaf = output_dir / "cells" / domain / model_key
            digest = dataset_sha256(domain)
            if (leaf / "complete.json").exists():
                if json.loads((leaf / "complete.json").read_text())["dataset_sha256"] != digest:
                    raise ValueError(f"{leaf} was fitted to a different {domain} dataset")
                print(f"Complete: {domain}/{model_key}", flush=True)
                continue
            if domain not in datasets:
                datasets[domain] = D.load_dataset(domain)
            tables = fit_cell(domain, model_key, datasets[domain], args, device)
            leaf.parent.mkdir(parents=True, exist_ok=True)
            attempt = Path(tempfile.mkdtemp(prefix=f"{model_key}.attempt-", dir=leaf.parent))
            for filename, frame in tables.items():
                frame.to_csv(attempt / filename, index=False)
            (attempt / "complete.json").write_text(
                json.dumps({"domain": domain, "model": model_key, "dataset_sha256": digest}, indent=2)
                + "\n"
            )
            os.rename(attempt, leaf)
    incomplete = [cell for cell in cells if not (output_dir / "cells" / cell[0] / cell[1] / "complete.json").exists()]
    if incomplete:
        print(f"{len(incomplete)} cells not yet complete; skipping merge", flush=True)
        return
    merge_cells(cells, output_dir, run_metadata(args, str(device)))


if __name__ == "__main__":
    main()
