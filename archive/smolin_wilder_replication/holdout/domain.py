"""Fit the held-out-domain models ("domain out, framing in"; Figure 2).

For each model and target domain, a latent model over the twelve generic
parameterized decisions is fitted on the train/validation rows of the other
two domains.  Each framing family (threshold, betting, cost) has one link
(Eq. 1) shared by both source domains and all four thresholds; the latent
prior is fitted too.  No target-domain row enters fitting or early stopping.
The fitted parameters are evaluated on the target domain by
``archive.smolin_wilder_replication.holdout.grid``.

Usage::

    python -m archive.smolin_wilder_replication.holdout.domain                       # -> build/fits/holdout_domain
    python -m archive.smolin_wilder_replication.holdout.domain --shard 0 --shards 4  # one of 4 workers

Outputs: ``shared_parameters.csv`` (held_out_domain, model_key, source_domains,
component, output, parameter, value: family offsets/slopes and the prior's
log-density knots), ``fit_summary.csv`` (sample sizes, seed, optimization
diagnostics) and ``run_metadata.json``.
"""

from __future__ import annotations

import argparse
from typing import Any, Mapping

import pandas as pd
import torch

from archive.smolin_wilder_replication.config import REFIT_DIR
from archive.smolin_wilder_replication.holdout import common as C
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.model import fit_model
from archive.smolin_wilder_replication.models import PIPELINE_POSITION

ANALYSIS = "holdout_domain"


def fit_seed(base_seed: int, held_out_domain: str, model_key: str) -> int:
    return int(base_seed) + 10_000 * list(D.DOMAIN_CONFIGS).index(held_out_domain) + 100 * PIPELINE_POSITION[model_key]


def parameter_rows(model: C.SharedFamilyLatentModel) -> list[dict[str, Any]]:
    rows = [
        {"component": "family_link", "output": item["family"], "parameter": item["parameter"], "value": float(item["value"])}
        for item in model.family_parameter_rows()
    ]
    rows.extend(
        {
            "component": "latent_prior",
            "output": "__latent_prior__",
            "parameter": f"log_density_knot_{index}",
            "value": float(value.detach()),
        }
        for index, value in enumerate(model.prior_knot_values, start=1)
    )
    return rows


def fit_cell(
    cell: C.Cell, datasets: Mapping[str, pd.DataFrame], args: argparse.Namespace, device: torch.device
) -> dict[str, pd.DataFrame]:
    held_out_domain, model_key = cell
    source_domains = "+".join(domain for domain in D.DOMAIN_CONFIGS if domain != held_out_domain)
    train_values, train_counts = C.pooled_source_values(
        datasets, held_out_domain=held_out_domain, model_key=model_key, split="train"
    )
    validation_values, validation_counts = C.pooled_source_values(
        datasets, held_out_domain=held_out_domain, model_key=model_key, split="val"
    )
    target_test = C.model_split(datasets[held_out_domain], model_key, "test")

    seed = fit_seed(int(args.seed), held_out_domain, model_key)
    C.seed_everything(seed)
    model = C.build_model(C.GENERIC_SPECS, C.GENERIC_FAMILIES, args, device)
    label = f"domain-holdout/{held_out_domain}/{model_key}"
    print(f"Fitting {label} from {source_domains.replace('+', ', ')}", flush=True)
    fit_info = fit_model(model, train_values, validation_values, C.fit_config(args), progress_label=label)

    fits = pd.DataFrame(
        [
            {
                "held_out_domain": held_out_domain,
                "model_key": model_key,
                "source_domains": source_domains,
                "n_train": int(len(train_values)),
                "n_validation": int(len(validation_values)),
                **C.source_counts(train_counts, validation_counts),
                "n_target_test": int(len(target_test)),
                "seed": seed,
                **fit_info,
            }
        ]
    )
    parameters = pd.DataFrame(
        [
            {"held_out_domain": held_out_domain, "model_key": model_key, "source_domains": source_domains, **row}
            for row in parameter_rows(model)
        ]
    ).sort_values(["held_out_domain", "model_key", "component", "output", "parameter"])
    return {"fit_summary.csv": fits, "shared_parameters.csv": parameters}


def merge(tables: dict[str, list[pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    return {name: pd.concat(frames, ignore_index=True) for name, frames in tables.items()}


def main() -> None:
    args = C.fit_parser(__doc__, REFIT_DIR / ANALYSIS).parse_args()
    domains, models = C.canonical_selection(args)
    cells = [(domain, model) for domain in domains for model in models]
    metadata = C.fit_metadata(ANALYSIS, args, str(C.resolve_device(str(args.device))))
    metadata["generic_family_mapping"] = C.GENERIC_FAMILIES
    metadata["seed_rule"] = "seed + 10000 * domain_position + 100 * model_position"
    C.run_cells(cells, fit_cell, merge, metadata, args)


if __name__ == "__main__":
    main()
