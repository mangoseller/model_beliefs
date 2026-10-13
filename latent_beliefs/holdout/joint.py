"""Fit and score the joint held-out domain and framing ("domain out, framing out"; Figure 2).

For every model, target domain and target framing family, a latent model over
the twelve generic decisions is fitted on the other two domains with all four
target-family decisions masked.  All parameterized decisions share one global
link (Eq. 1), so nothing is specific to a domain or family.  On the target
domain's test cases, the latent posterior conditions on the eight decisions
of the two visible families, and the four target-family decisions are
predicted zero-shot.

Usage::

    python -m latent_beliefs.holdout.joint                        # -> build/fits/holdout_joint
    python -m latent_beliefs.holdout.joint --shard 0 --shards 8

Outputs: ``combined_prompt_metrics.csv`` (one row per target decision; read by
the eight-decision grid), ``global_parameters.csv`` (global link and prior
knots), ``fit_summary.csv`` and ``run_metadata.json``.
"""

from __future__ import annotations

import argparse
from typing import Any, Mapping

import numpy as np
import pandas as pd
import torch

from latent_beliefs.config import REFIT_DIR
from latent_beliefs.holdout import common as C
from latent_beliefs.latent import domains as D
from latent_beliefs.latent.model import fit_model
from latent_beliefs.models import PIPELINE_POSITION

ANALYSIS = "holdout_joint"
# Cells (and merged rows) run over families in this order.
CELL_FAMILY_ORDER = ("threshold", "betting", "cost")


def fit_seed(base_seed: int, held_out_domain: str, held_out_family: str, model_key: str) -> int:
    return (
        int(base_seed)
        + 10_000 * list(D.DOMAIN_CONFIGS).index(held_out_domain)
        + 1_000 * C.FAMILIES.index(held_out_family)
        + 10 * PIPELINE_POSITION[model_key]
    )


def mask_family(values: np.ndarray, family: str) -> np.ndarray:
    masked = np.asarray(values, dtype=float).copy()
    for index, spec in enumerate(C.GENERIC_SPECS):
        if C.GENERIC_FAMILIES[spec.name] == family:
            masked[:, index] = np.nan
    return masked


def parameter_rows(model: C.SharedFamilyLatentModel) -> list[dict[str, Any]]:
    rows = [
        {"component": "global_link", "parameter": item["parameter"], "value": float(item["value"])}
        for item in model.family_parameter_rows()
    ]
    rows.extend(
        {"component": "latent_prior", "parameter": f"log_density_knot_{index}", "value": float(value.detach())}
        for index, value in enumerate(model.prior_knot_values, start=1)
    )
    return rows


def fit_cell(
    cell: C.Cell, datasets: Mapping[str, pd.DataFrame], args: argparse.Namespace, device: torch.device
) -> dict[str, pd.DataFrame]:
    held_out_domain, model_key, held_out_family = cell
    target_cfg = D.DOMAIN_CONFIGS[held_out_domain]
    source_domains = "+".join(domain for domain in D.DOMAIN_CONFIGS if domain != held_out_domain)
    visible_families = "+".join(family for family in C.FAMILIES if family != held_out_family)
    train_raw, train_counts = C.pooled_source_values(
        datasets, held_out_domain=held_out_domain, model_key=model_key, split="train"
    )
    validation_raw, validation_counts = C.pooled_source_values(
        datasets, held_out_domain=held_out_domain, model_key=model_key, split="val"
    )
    train_values = mask_family(train_raw, held_out_family)
    validation_values = mask_family(validation_raw, held_out_family)
    target_test = C.model_split(datasets[held_out_domain], model_key, "test")
    target_raw = C.generic_values(target_test, target_cfg)

    seed = fit_seed(int(args.seed), held_out_domain, held_out_family, model_key)
    C.seed_everything(seed)
    model = C.build_model(C.GENERIC_SPECS, {spec.name: C.GLOBAL_LINK for spec in C.GENERIC_SPECS}, args, device)
    label = f"combined/{held_out_domain}/{held_out_family}/{model_key}"
    print(f"Fitting {label}", flush=True)
    fit_info = fit_model(model, train_values, validation_values, C.fit_config(args), progress_label=label)
    common = {
        "held_out_domain": held_out_domain,
        "held_out_family": held_out_family,
        "model_key": model_key,
        "source_domains": source_domains,
        "visible_families": visible_families,
        "n_train": int(len(train_values)),
        "n_validation": int(len(validation_values)),
        "n_target_test": int(len(target_test)),
        "seed": seed,
        **C.source_counts(train_counts, validation_counts),
    }

    weights = C.posterior_weights(model, mask_family(target_raw, held_out_family))
    mapping = C.source_column_mapping(target_cfg)
    targets = sorted(
        (spec for spec in C.GENERIC_SPECS if C.GENERIC_FAMILIES[spec.name] == held_out_family),
        key=lambda spec: float(spec.nominal_threshold),
    )
    metric_rows: list[dict[str, Any]] = []
    for target in targets:
        target_index = C.GENERIC_SPECS.index(target)
        predicted = C.predict_binary(model, weights, target)
        actual = target_raw[:, target_index]
        baseline = C.smoothed_constant_probability(train_raw[:, target_index])
        metrics = C.binary_metrics(actual, predicted, baseline)
        metric_rows.append(
            {
                "held_out_domain": held_out_domain,
                "held_out_domain_display": target_cfg.display,
                "held_out_family": held_out_family,
                "model_key": model_key,
                "model_display": str(target_test["model_display"].iloc[0]),
                "capability_rank": C.capability_rank(model_key),
                "source_domains": source_domains,
                "visible_families": visible_families,
                "target_generic_output": target.name,
                "target_domain_output": mapping[target.name],
                "target_threshold": float(target.nominal_threshold),
                "n_target_test": int(len(actual)),
                "source_train_prevalence_smoothed": float(baseline),
                "r2_vs_source_constant": 1.0 - float(metrics["relative_brier"]),
                **metrics,
            }
        )
    return {
        "combined_prompt_metrics.csv": pd.DataFrame(metric_rows),
        "fit_summary.csv": pd.DataFrame([{**common, **fit_info}]),
        "global_parameters.csv": pd.DataFrame([{**common, **row} for row in parameter_rows(model)]).sort_values(
            ["held_out_domain", "held_out_family", "model_key", "component", "parameter"]
        ),
    }


def merge(tables: dict[str, list[pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    return {name: pd.concat(frames, ignore_index=True) for name, frames in tables.items()}


def main() -> None:
    args = C.fit_parser(__doc__, REFIT_DIR / ANALYSIS).parse_args()
    domains, models = C.canonical_selection(args)
    cells = [
        (domain, model, family)
        for domain in domains
        for model in models
        for family in CELL_FAMILY_ORDER
    ]
    metadata = C.fit_metadata(ANALYSIS, args, str(C.resolve_device(str(args.device))))
    metadata["seed_rule"] = "seed + 10000 * domain_position + 1000 * family_position + 10 * model_position"
    C.run_cells(cells, fit_cell, merge, metadata, args)


if __name__ == "__main__":
    main()
