"""Fit and score the held-out framing family ("domain in, framing out"; Figure 2).

For every domain, model and target framing family, the domain's sixteen-output
latent model is fitted with all four target-family decisions masked.  Every
parameterized decision shares one global link (Eq. 1), learned only from the
two visible families and applied unchanged to the unseen family.  Test-case
latent posteriors never see the target family and condition on either

* ``eight_decision``: the eight decisions of the two visible families
  (Figure 2), or
* ``report``: the stated probability and the two ordinal reports
  (appendix variant).

Each cell is fitted once and scored under both evidence sets.

Usage::

    python -m archive.smolin_wilder_replication.holdout.framing                       # -> build/fits/holdout_framing
    python -m archive.smolin_wilder_replication.holdout.framing --shard 0 --shards 8

Outputs: ``full_family_prompt_metrics_{eight_decision,report}.csv`` (one row
per target decision; read by the grid), ``global_link_parameters.csv`` (global
offset and slope, implied thresholds), ``fit_summary.csv`` and
``run_metadata.json``.
"""

from __future__ import annotations

import argparse
import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import torch

from archive.smolin_wilder_replication.config import REFIT_DIR
from archive.smolin_wilder_replication.holdout import common as C
from archive.smolin_wilder_replication.latent import domains as D
from archive.smolin_wilder_replication.latent.model import OutputSpec, fit_model
from archive.smolin_wilder_replication.models import PIPELINE_POSITION

ANALYSIS = "holdout_framing"
EVIDENCE = ("eight_decision", "report")
MERGE_KEYS = ["domain", "model_key", "held_out_family"]


def metrics_file(evidence: str) -> str:
    return f"full_family_prompt_metrics_{evidence}.csv"


def fit_seed(base_seed: int, domain: str, model_key: str, family: str) -> int:
    return (
        int(base_seed)
        + 10_000 * list(D.DOMAIN_CONFIGS).index(domain)
        + 100 * PIPELINE_POSITION[model_key]
        + C.FAMILIES.index(family)
    )


def masked_family_values(frame: pd.DataFrame, specs: Sequence[OutputSpec], family: str) -> np.ndarray:
    values = D.likelihood_values(frame, specs)
    for index, spec in enumerate(specs):
        if C.output_family(spec.name) == family:
            values[:, index] = np.nan
    return values


def evidence_values(frame: pd.DataFrame, specs: Sequence[OutputSpec], family: str, evidence: str) -> np.ndarray:
    """Test-time evidence; the target family is always masked."""

    values = masked_family_values(frame, specs, family)
    for index, spec in enumerate(specs):
        if evidence == "report":
            keep = spec.kind in {"probability", "ordinal"}
        else:
            keep = C.output_family(spec.name) in C.FAMILIES
        if not keep:
            values[:, index] = np.nan
    return values


def fit_cell(
    cell: C.Cell, datasets: Mapping[str, pd.DataFrame], args: argparse.Namespace, device: torch.device
) -> dict[str, pd.DataFrame]:
    domain, model_key, family = cell
    cfg = D.DOMAIN_CONFIGS[domain]
    specs = cfg.output_specs
    train = C.model_split(datasets[domain], model_key, "train")
    validation = C.model_split(datasets[domain], model_key, "val")
    test = C.model_split(datasets[domain], model_key, "test")
    model_display = str(train["model_display"].iloc[0])
    targets = sorted(
        (spec for spec in specs if spec.kind == "binary" and C.output_family(spec.name) == family),
        key=lambda spec: (float(spec.nominal_threshold), spec.name),
    )
    shared = {
        spec.name: C.GLOBAL_LINK
        for spec in specs
        if spec.kind == "binary" and C.output_family(spec.name) in C.FAMILIES
    }

    seed = fit_seed(int(args.seed), domain, model_key, family)
    C.seed_everything(seed)
    model = C.build_model(specs, shared, args, device)
    label = f"full-family-global/{domain}/{model_key}/{family}"
    print(f"Fitting {label}", flush=True)
    fit_info = fit_model(
        model,
        masked_family_values(train, specs, family),
        masked_family_values(validation, specs, family),
        C.fit_config(args),
        progress_label=label,
    )
    fits = pd.DataFrame(
        [
            {
                "domain": domain,
                "model_key": model_key,
                "held_out_family": family,
                "visible_families": ",".join(other for other in C.FAMILIES if other != family),
                "n_train": len(train),
                "n_validation": len(validation),
                "n_test": len(test),
                **fit_info,
            }
        ]
    )

    base = {"domain": domain, "model_key": model_key, "held_out_family": family}
    parameter_rows: list[dict[str, Any]] = [
        {**base, "output": "__global__", "normative_threshold": math.nan, "parameter": row["parameter"], "value": float(row["value"])}
        for row in model.family_parameter_rows()
    ]
    implied = {spec.name: float(model.binary_implied_threshold(spec).detach()) for spec in targets}
    parameter_rows.extend(
        {
            **base,
            "output": spec.name,
            "normative_threshold": float(spec.nominal_threshold),
            "parameter": "held_out_implied_threshold",
            "value": implied[spec.name],
        }
        for spec in targets
    )

    tables = {"fit_summary.csv": fits, "global_link_parameters.csv": pd.DataFrame(parameter_rows)}
    for evidence in EVIDENCE:
        weights = C.posterior_weights(model, evidence_values(test, specs, family, evidence))
        rows: list[dict[str, Any]] = []
        for target in targets:
            predicted = C.predict_binary(model, weights, target)
            train_actual = train[target.name].to_numpy(dtype=float)
            test_actual = test[target.name].to_numpy(dtype=float)
            rows.append(
                {
                    "domain": domain,
                    "domain_display": cfg.display,
                    "model_key": model_key,
                    "model_display": model_display,
                    "capability_rank": C.capability_rank(model_key),
                    "held_out_family": family,
                    "held_out_output": target.name,
                    "held_out_threshold": float(target.nominal_threshold),
                    "fitted_implied_threshold": implied[target.name],
                    "n_train": int(len(train)),
                    "n_test": int(len(test)),
                    "train_prevalence": float(np.mean(train_actual)),
                    "test_prevalence": float(np.mean(test_actual)),
                    **C.binary_metrics(test_actual, predicted, C.smoothed_constant_probability(train_actual)),
                }
            )
        metrics = pd.DataFrame(rows)
        metrics["r2_vs_constant"] = 1.0 - metrics["relative_brier"]
        tables[metrics_file(evidence)] = metrics
    return tables


def merge(tables: dict[str, list[pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    # Multi-key sorts are stable: the thresholds stay ascending within a cell.
    return {name: pd.concat(frames, ignore_index=True).sort_values(MERGE_KEYS) for name, frames in tables.items()}


def main() -> None:
    args = C.fit_parser(__doc__, REFIT_DIR / ANALYSIS).parse_args()
    domains, models = C.canonical_selection(args)
    cells = [(domain, model, family) for domain in domains for model in models for family in C.FAMILIES]
    metadata = C.fit_metadata(ANALYSIS, args, str(C.resolve_device(str(args.device))))
    metadata["posterior_evidence"] = {
        "eight_decision": "the eight decisions of the two visible families",
        "report": "stated probability, Likert report and probability-quintile report",
    }
    metadata["seed_rule"] = "seed + 10000 * domain_position + 100 * model_position + family_position"
    C.run_cells(cells, fit_cell, merge, metadata, args)


if __name__ == "__main__":
    main()
