"""Fit and score held-out-domain models whose test latents come from three reports.

Appendix figure "Held out framing and domain generalization with probability
report inputs".  The generic cross-domain schema holds the stated
probability, the Likert report, the probability-quintile report and the
twelve parameterized decisions.  Models are fitted on the other two domains:

* ``domain_out_family_in``: one link per framing family (Eq. 1);
* ``domain_out_family_out``: the target family is masked and one global
  decision link is learned from the other two families.

On the target domain's test cases the latent posterior conditions only on the
three report questions; target decisions are scored with
:func:`latent_beliefs.holdout.common.exact_binary_metrics`.

Usage::

    python -m latent_beliefs.holdout.report_domain                  # -> build/fits/holdout_report_domain
    python -m latent_beliefs.holdout.report_domain --shard 0 --shards 8

Outputs: ``report_prompt_metrics.csv`` (one row per regime x target decision;
read by the report grid), ``report_parameters.csv``, ``report_fit_summary.csv``
and ``run_metadata.json``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import torch

from latent_beliefs.config import REFIT_DIR
from latent_beliefs.holdout import common as C
from latent_beliefs.latent import domains as D
from latent_beliefs.latent.model import OutputSpec, fit_model
from latent_beliefs.models import PIPELINE_POSITION

ANALYSIS = "holdout_report_domain"
REGIMES = ("domain_out_family_in", "domain_out_family_out")
NO_FAMILY = "__none__"
REPORT_SPECS = (
    OutputSpec("reported_probability", "probability"),
    OutputSpec("likert_report", "ordinal", n_categories=5),
    OutputSpec("probability_quintile_report", "ordinal", n_categories=5),
)
ALL_SPECS = REPORT_SPECS + C.GENERIC_SPECS
FIT_KEYS = ["regime", "held_out_domain", "model_key", "held_out_family"]
METRIC_KEYS = ["regime", "domain", "model_key", "target_generic_output"]


def report_column_mapping(cfg: D.DomainConfig) -> dict[str, str]:
    probability = [spec for spec in cfg.output_specs if spec.kind == "probability"]
    ordinal = [spec for spec in cfg.output_specs if spec.kind == "ordinal"]
    quintile = [spec for spec in ordinal if spec.name == "probability_bin_quintile"]
    likert = [spec for spec in ordinal if spec.name != "probability_bin_quintile"]
    if len(probability) != 1 or len(quintile) != 1 or len(likert) != 1:
        raise ValueError(f"{cfg.key} report questions are not uniquely identifiable")
    return {
        "reported_probability": probability[0].observation_column or probability[0].name,
        "likert_report": likert[0].observation_column or likert[0].name,
        "probability_quintile_report": quintile[0].observation_column or quintile[0].name,
    }


def generic_values(frame: pd.DataFrame, cfg: D.DomainConfig) -> np.ndarray:
    """[case, output] matrix in ALL_SPECS order."""

    mapping = report_column_mapping(cfg) | C.source_column_mapping(cfg)
    return frame[[mapping[spec.name] for spec in ALL_SPECS]].to_numpy(dtype=float)


def report_evidence(values: np.ndarray) -> np.ndarray:
    """Keep only the three report columns."""

    masked = np.full_like(np.asarray(values, dtype=float), np.nan)
    masked[:, : len(REPORT_SPECS)] = values[:, : len(REPORT_SPECS)]
    return masked


def mask_family(values: np.ndarray, family: str) -> np.ndarray:
    masked = np.asarray(values, dtype=float).copy()
    for index, spec in enumerate(ALL_SPECS):
        if C.GENERIC_FAMILIES.get(spec.name) == family:
            masked[:, index] = np.nan
    return masked


def fit_seed(base_seed: int, regime: str, held_out_domain: str, model_key: str, family: str) -> int:
    family_index = 0 if family == NO_FAMILY else C.FAMILIES.index(family) + 1
    return (
        int(base_seed)
        + 100_000 * REGIMES.index(regime)
        + 10_000 * list(D.DOMAIN_CONFIGS).index(held_out_domain)
        + 100 * PIPELINE_POSITION[model_key]
        + family_index
    )


def parameter_rows(model: C.SharedFamilyLatentModel) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in REPORT_SPECS:
        if spec.kind == "probability":
            rows.append(
                {"component": "probability", "output": spec.name, "parameter": "precision",
                 "value": float(model.probability_precision.detach())}
            )
            continue
        rows.append(
            {"component": "ordinal", "output": spec.name, "parameter": "slope",
             "value": float(model.ordinal_slope(spec.name).detach())}
        )
        rows.extend(
            {"component": "ordinal", "output": spec.name, "parameter": f"cutpoint_{index}", "value": float(value.detach())}
            for index, value in enumerate(model.ordinal_cutpoints(spec), start=1)
        )
    rows.extend(
        {"component": "latent_prior", "output": "__latent_prior__", "parameter": f"prior_knot_value_{index}",
         "value": float(value.detach())}
        for index, value in enumerate(model.prior_knot_values, start=1)
    )
    rows.extend(
        {"component": "family_link", "output": item["family"], "parameter": item["parameter"], "value": item["value"]}
        for item in model.family_parameter_rows()
    )
    return rows


def target_metric_rows(
    model: C.SharedFamilyLatentModel,
    values: np.ndarray,
    cfg: D.DomainConfig,
    model_key: str,
    model_display: str,
    regime: str,
    families: Sequence[str],
) -> list[dict[str, Any]]:
    weights = C.posterior_weights(model, report_evidence(values))
    mapping = C.source_column_mapping(cfg)
    rows: list[dict[str, Any]] = []
    for target_index, target in enumerate(ALL_SPECS):
        family = C.GENERIC_FAMILIES.get(target.name)
        if family not in families:
            continue
        rows.append(
            {
                "domain": cfg.key,
                "domain_display": cfg.display,
                "model_key": model_key,
                "model_display": model_display,
                "capability_rank": C.capability_rank(model_key),
                "family": family,
                "target_generic_output": target.name,
                "target_output": mapping[target.name],
                "target_threshold": float(target.nominal_threshold),
                "regime": regime,
                **C.exact_binary_metrics(values[:, target_index], C.predict_binary(model, weights, target)),
            }
        )
    return rows


def fit_cell(
    cell: C.Cell, datasets: Mapping[str, pd.DataFrame], args: argparse.Namespace, device: torch.device
) -> dict[str, pd.DataFrame]:
    regime, domain, model_key, family = cell
    cfg = D.DOMAIN_CONFIGS[domain]
    source_domains = "+".join(key for key in D.DOMAIN_CONFIGS if key != domain)
    train_raw, train_counts = C.pooled_source_values(
        datasets, held_out_domain=domain, model_key=model_key, split="train", values=generic_values
    )
    validation_raw, validation_counts = C.pooled_source_values(
        datasets, held_out_domain=domain, model_key=model_key, split="val", values=generic_values
    )
    target_test = C.model_split(datasets[domain], model_key, "test")
    test_values = generic_values(target_test, cfg)

    if regime == "domain_out_family_in":
        links, families = C.GENERIC_FAMILIES, list(C.FAMILIES)
        train_values, validation_values = train_raw, validation_raw
        label = f"reports/{regime}/{domain}/{model_key}"
    else:
        links, families = {name: C.GLOBAL_LINK for name in C.GENERIC_FAMILIES}, [family]
        train_values, validation_values = mask_family(train_raw, family), mask_family(validation_raw, family)
        label = f"reports/{regime}/{domain}/{model_key}/{family}"
    seed = fit_seed(int(args.seed), regime, domain, model_key, family)
    C.seed_everything(seed)
    model = C.build_model(ALL_SPECS, links, args, device)
    print(f"Fitting {label}", flush=True)
    fit_info = fit_model(model, train_values, validation_values, C.fit_config(args), progress_label=label)
    common = {
        "regime": regime,
        "held_out_domain": domain,
        "held_out_family": family,
        "model_key": model_key,
        "source_domains": source_domains,
        "n_train": int(len(train_values)),
        "n_validation": int(len(validation_values)),
        "n_target_test": int(len(test_values)),
        "seed": seed,
        **C.source_counts(train_counts, validation_counts),
    }
    metrics = target_metric_rows(
        model, test_values, cfg, model_key, str(target_test["model_display"].iloc[0]), regime, families
    )
    return {
        "report_prompt_metrics.csv": pd.DataFrame(metrics),
        "report_fit_summary.csv": pd.DataFrame([common | fit_info]),
        "report_parameters.csv": pd.DataFrame([common | row for row in parameter_rows(model)]).sort_values(
            FIT_KEYS + ["component", "output", "parameter"]
        ),
    }


def merge(tables: dict[str, list[pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    return {
        name: pd.concat(frames, ignore_index=True).sort_values(
            METRIC_KEYS if name == "report_prompt_metrics.csv" else FIT_KEYS
        )
        for name, frames in tables.items()
    }


def main() -> None:
    args = C.fit_parser(__doc__, REFIT_DIR / ANALYSIS).parse_args()
    domains, models = C.canonical_selection(args)
    cells = [
        (regime, domain, model, family)
        for domain in domains
        for model in models
        for regime, family in [(REGIMES[0], NO_FAMILY)] + [(REGIMES[1], family) for family in C.FAMILIES]
    ]
    metadata = C.fit_metadata(ANALYSIS, args, str(C.resolve_device(str(args.device))))
    metadata["report_specs"] = [asdict(spec) for spec in REPORT_SPECS]
    metadata["posterior_evidence"] = "stated probability, Likert report and probability-quintile report"
    metadata["seed_rule"] = (
        "seed + 100000 * regime_position + 10000 * domain_position + 100 * model_position"
        " + (0 if family in else 1 + family_position)"
    )
    C.run_cells(cells, fit_cell, merge, metadata, args)


if __name__ == "__main__":
    main()
