"""Evaluate the 2 x 2 domain/framing holdout grid (Figure 2 and its appendix report-evidence variant).

All four regimes predict the same twelve parameterized decisions of every
domain and model from one fixed body of test-time evidence; they differ only
in whether the target domain and the target framing family informed the
fitted parameters:

=========================  ===============================================
domain in, framing in      main fit (``fits/main``), restored here
domain in, framing out     ``fits/holdout_framing`` (scored when fitted)
domain out, framing in     ``--evidence eight-decision``: ``fits/holdout_domain``
                           restored here; ``reports``: ``fits/holdout_report_domain``
domain out, framing out    ``eight-decision``: ``fits/holdout_joint``;
                           ``reports``: ``fits/holdout_report_domain``
=========================  ===============================================

Evidence: ``eight-decision`` (Figure 2) conditions the latent posterior on the
eight decisions of the two non-target families; ``reports`` (appendix)
conditions on the stated probability and the two ordinal reports.

Usage::

    python -m latent_beliefs.holdout.grid --evidence eight-decision  # -> evaluation/holdout_grid_eight_decision
    python -m latent_beliefs.holdout.grid --evidence reports         # -> evaluation/holdout_grid_report

Outputs: ``holdout_information_prompt_metrics.csv`` (one row per domain x model
x target decision x regime: Brier score, c-index, best-constant baseline and
R^2) and ``holdout_information_model_summary.csv`` (per domain x model x regime:
mean c-index and pooled R^2 with 95% percentile intervals from a bootstrap
over the twelve target decisions).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import torch

from latent_beliefs.config import EVALUATION_DIR, FITS_DIR, ensure
from latent_beliefs.holdout import common as C
from latent_beliefs.latent import domains as D
from latent_beliefs.latent.model import OutputSpec, inverse_softplus, load_fit, restore_cell
from latent_beliefs.models import pipeline_order

EVIDENCE = {"eight-decision": "eight_decision", "reports": "report"}
REGIME_ORDER = (
    "domain_in_family_in",
    "domain_in_family_out",
    "domain_out_family_in",
    "domain_out_family_out",
)
REGIME_LABELS = {
    "domain_in_family_in": "Domain in, framing in",
    "domain_in_family_out": "Domain in, framing out",
    "domain_out_family_in": "Domain out, framing in",
    "domain_out_family_out": "Domain out, framing out",
}
COMMON_COLUMNS = [
    "domain",
    "domain_display",
    "model_key",
    "model_display",
    "capability_rank",
    "family",
    "target_generic_output",
    "target_output",
    "target_threshold",
    "regime",
    "regime_label",
    "brier_score",
    "c_index",
]
BOOTSTRAP_REPS = 10_000
BOOTSTRAP_SEED = 20260821
DISPLAY = {key: cfg.display for key, cfg in D.DOMAIN_CONFIGS.items()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence", choices=tuple(EVIDENCE), default="eight-decision")
    parser.add_argument("--fits-dir", type=Path, default=FITS_DIR)
    parser.add_argument("--output-dir", type=Path, default=None, help="Default: <build>/evaluation/holdout_grid_<evidence>.")
    return parser.parse_args()


# Evidence
def eight_decision_evidence(values: np.ndarray, specs: Sequence[OutputSpec], target_family: str) -> np.ndarray:
    """Keep exactly the eight decisions of the two non-target families."""

    masked = np.full_like(np.asarray(values, dtype=float), np.nan)
    visible = 0
    for index, spec in enumerate(specs):
        family = C.family_of(spec.name)
        if spec.kind == "binary" and family in C.FAMILIES and family != target_family:
            masked[:, index] = values[:, index]
            visible += 1
    if visible != 8:
        raise ValueError(f"expected eight visible decisions, found {visible}")
    return masked


def report_evidence(values: np.ndarray, specs: Sequence[OutputSpec]) -> np.ndarray:
    """Keep the stated probability and the two ordinal reports."""

    masked = np.full_like(np.asarray(values, dtype=float), np.nan)
    for index, spec in enumerate(specs):
        if spec.kind in {"probability", "ordinal"}:
            masked[:, index] = values[:, index]
    return masked


# Regimes
def metric_row(
    *, domain: str, model_key: str, model_display: str, target: OutputSpec, target_output: str,
    regime: str, actual: np.ndarray, predicted: np.ndarray,
) -> dict[str, Any]:
    return {
        "domain": domain,
        "domain_display": DISPLAY[domain],
        "model_key": model_key,
        "model_display": model_display,
        "capability_rank": C.capability_rank(model_key),
        "family": C.family_of(target.name),
        "target_generic_output": C.generic_name(C.family_of(target.name), float(target.nominal_threshold)),
        "target_output": target_output,
        "target_threshold": float(target.nominal_threshold),
        "regime": regime,
        "regime_label": REGIME_LABELS[regime],
        **C.exact_binary_metrics(actual, predicted),
    }


def evaluate_main_fit(fit_dir: Path, evidence: str) -> pd.DataFrame:
    """Domain in, framing in: the main fit's own decision links."""

    parameters, metadata = load_fit(fit_dir)
    rows: list[dict[str, Any]] = []
    for domain, cfg in D.DOMAIN_CONFIGS.items():
        dataset = D.load_dataset(domain)
        mapping = C.source_column_mapping(cfg)
        for model_key in pipeline_order():
            test = C.model_split(dataset, model_key, "test")
            model = restore_cell(parameters, metadata, domain, model_key, cfg.output_specs)
            values = D.likelihood_values(test, cfg.output_specs)
            model_display = str(test["model_display"].iloc[0])
            if evidence == "report":
                report_weights = C.posterior_weights(model, report_evidence(values, cfg.output_specs))
            for family in C.FAMILIES:
                if evidence == "eight_decision":
                    weights = C.posterior_weights(model, eight_decision_evidence(values, cfg.output_specs, family))
                else:
                    weights = report_weights
                for generic in C.GENERIC_SPECS:
                    if C.GENERIC_FAMILIES[generic.name] != family:
                        continue
                    target_index = next(
                        index for index, spec in enumerate(cfg.output_specs)
                        if (spec.observation_column or spec.name) == mapping[generic.name]
                    )
                    target = cfg.output_specs[target_index]
                    rows.append(
                        metric_row(
                            domain=domain, model_key=model_key, model_display=model_display, target=target,
                            target_output=mapping[generic.name], regime="domain_in_family_in",
                            actual=values[:, target_index], predicted=C.predict_binary(model, weights, target),
                        )
                    )
    return pd.DataFrame(rows)


def restore_domain_holdout(rows: pd.DataFrame, metadata: dict[str, Any]) -> C.SharedFamilyLatentModel:
    """Rebuild a domain-holdout fit from its shared_parameters.csv rows."""

    def value(component: str, output: str, parameter: str) -> float:
        selected = rows[rows["component"].eq(component) & rows["output"].eq(output) & rows["parameter"].eq(parameter)]
        if len(selected) != 1:
            raise ValueError(f"expected one parameter {component}/{output}/{parameter}, found {len(selected)}")
        return float(selected["value"].iloc[0])

    model = C.SharedFamilyLatentModel(
        C.GENERIC_SPECS,
        binary_families=C.GENERIC_FAMILIES,
        quadrature_points=int(metadata["quadrature_points"]),
        probability_epsilon=float(metadata["probability_epsilon"]),
    )
    with torch.no_grad():
        for family in C.FAMILIES:
            model.family_intercepts[family].copy_(
                model.family_intercepts[family].new_tensor(value("family_link", family, "offset"))
            )
            model.family_raw_slopes[family].copy_(
                model.family_raw_slopes[family].new_tensor(
                    inverse_softplus(value("family_link", family, "slope") - model.min_slope)
                )
            )
        knots = [
            value("latent_prior", "__latent_prior__", f"log_density_knot_{index}")
            for index in range(1, model.prior_knot_values.numel() + 1)
        ]
        model.prior_knot_values.copy_(model.prior_knot_values.new_tensor(knots))
    model.eval()
    return model


def evaluate_domain_holdout(fit_dir: Path) -> pd.DataFrame:
    """Domain out, framing in (eight-decision evidence): restored domain-holdout fits."""

    parameters = pd.read_csv(fit_dir / "shared_parameters.csv")
    metadata = json.loads((fit_dir / "run_metadata.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for domain, cfg in D.DOMAIN_CONFIGS.items():
        dataset = D.load_dataset(domain)
        mapping = C.source_column_mapping(cfg)
        for model_key in pipeline_order():
            model = restore_domain_holdout(
                parameters[parameters["held_out_domain"].eq(domain) & parameters["model_key"].eq(model_key)],
                metadata,
            )
            test = C.model_split(dataset, model_key, "test")
            values = C.generic_values(test, cfg)
            model_display = str(test["model_display"].iloc[0])
            for family in C.FAMILIES:
                weights = C.posterior_weights(model, eight_decision_evidence(values, C.GENERIC_SPECS, family))
                for target_index, target in enumerate(C.GENERIC_SPECS):
                    if C.GENERIC_FAMILIES[target.name] != family:
                        continue
                    rows.append(
                        metric_row(
                            domain=domain, model_key=model_key, model_display=model_display, target=target,
                            target_output=mapping[target.name], regime="domain_out_family_in",
                            actual=values[:, target_index], predicted=C.predict_binary(model, weights, target),
                        )
                    )
    return pd.DataFrame(rows)


def load_framing_holdout(fit_dir: Path, evidence: str) -> pd.DataFrame:
    """Domain in, framing out: metrics scored by the framing-holdout fit stage."""

    frame = pd.read_csv(fit_dir / f"full_family_prompt_metrics_{evidence}.csv").rename(
        columns={"held_out_family": "family", "held_out_output": "target_output", "held_out_threshold": "target_threshold"}
    )
    frame["target_generic_output"] = [
        C.generic_name(family, float(threshold)) for family, threshold in zip(frame["family"], frame["target_threshold"])
    ]
    frame["domain_display"] = frame["domain"].map(DISPLAY)
    frame["regime"] = "domain_in_family_out"
    frame["regime_label"] = REGIME_LABELS["domain_in_family_out"]
    return frame


def load_joint_holdout(fit_dir: Path) -> pd.DataFrame:
    """Domain out, framing out (eight-decision evidence): joint-holdout metrics."""

    frame = pd.read_csv(fit_dir / "combined_prompt_metrics.csv").rename(
        columns={"held_out_domain": "domain", "held_out_family": "family", "target_domain_output": "target_output"}
    )
    frame["domain_display"] = frame["domain"].map(DISPLAY)
    frame["regime"] = "domain_out_family_out"
    frame["regime_label"] = REGIME_LABELS["domain_out_family_out"]
    return frame


def load_report_domain_holdout(fit_dir: Path) -> pd.DataFrame:
    """Both domain-out regimes (report evidence)."""

    frame = pd.read_csv(fit_dir / "report_prompt_metrics.csv")
    frame["regime_label"] = frame["regime"].map(REGIME_LABELS)
    if set(frame["regime"]) != {"domain_out_family_in", "domain_out_family_out"}:
        raise ValueError("report domain-holdout metrics lack a regime")
    return frame


# Summary
def target_test_baselines() -> pd.DataFrame:
    """Best constant test predictor for every domain/model/target decision."""

    rows: list[dict[str, Any]] = []
    for domain, cfg in D.DOMAIN_CONFIGS.items():
        data = D.load_dataset(domain)
        mapping = C.source_column_mapping(cfg)
        for model_key in pipeline_order():
            test = C.model_split(data, model_key, "test")
            for spec in C.GENERIC_SPECS:
                actual = test[mapping[spec.name]].to_numpy(dtype=float)
                actual = actual[np.isfinite(actual)]
                prevalence = float(np.mean(actual))
                rows.append(
                    {
                        "domain": domain,
                        "model_key": model_key,
                        "target_output": mapping[spec.name],
                        "n_target_test": int(len(actual)),
                        "test_prevalence": prevalence,
                        "best_constant_brier_score": float(np.mean((actual - prevalence) ** 2)),
                    }
                )
    return pd.DataFrame(rows)


def summarize_model_regimes(comparison: pd.DataFrame) -> pd.DataFrame:
    """Mean c-index and pooled R^2 per dot, bootstrapping its twelve decisions."""

    domain_index = {domain: index for index, domain in enumerate(D.DOMAIN_CONFIGS)}
    regime_index = {regime: index for index, regime in enumerate(REGIME_ORDER)}
    rows: list[dict[str, Any]] = []
    keys = ["domain", "domain_display", "model_key", "model_display", "capability_rank", "regime", "regime_label"]
    for (domain, domain_display, model_key, model_display, rank, regime, regime_label), cell in comparison.groupby(
        keys, sort=False
    ):
        cell = cell.sort_values(["family", "target_threshold"]).reset_index(drop=True)
        c_index = cell["c_index"].to_numpy(float)
        n_test = cell["n_target_test"].to_numpy(float)
        sse = cell["brier_score"].to_numpy(float) * n_test
        sst = cell["best_constant_brier_score"].to_numpy(float) * n_test
        total_sst = float(np.sum(sst))
        if total_sst <= 0.0:
            raise ValueError(f"pooled R2 has zero denominator for {domain}/{model_key}/{regime}")
        pooled_r2 = 1.0 - float(np.sum(sse)) / total_sst

        # Seeds use the capability rank, which counts the excluded model.
        seed = BOOTSTRAP_SEED + 10_000 * domain_index[domain] + 100 * int(rank) + regime_index[regime]
        rng = np.random.default_rng(seed)
        indices = rng.integers(0, len(cell), size=(BOOTSTRAP_REPS, len(cell)))
        c_replicates = np.nanmean(c_index[indices], axis=1)
        replicate_sse = np.sum(sse[indices], axis=1)
        replicate_sst = np.sum(sst[indices], axis=1)
        valid = replicate_sst > 0.0
        r2_replicates = 1.0 - replicate_sse[valid] / replicate_sst[valid]
        c_lower, c_upper = np.nanpercentile(c_replicates, [2.5, 97.5])
        r2_lower, r2_upper = np.nanpercentile(r2_replicates, [2.5, 97.5])
        rows.append(
            {
                "domain": domain,
                "domain_display": domain_display,
                "model_key": model_key,
                "model_display": model_display,
                "capability_rank": int(rank),
                "regime": regime,
                "regime_label": regime_label,
                "target_prompts": int(cell["target_output"].nunique()),
                "evaluation_observations": int(np.sum(n_test)),
                "mean_c_index": float(np.nanmean(c_index)),
                "mean_c_index_ci_lower": float(c_lower),
                "mean_c_index_ci_upper": float(c_upper),
                "pooled_r2": pooled_r2,
                "pooled_r2_ci_lower": float(r2_lower),
                "pooled_r2_ci_upper": float(r2_upper),
                "macro_mean_prompt_r2": float(np.nanmean(cell["r2_vs_best_constant"].to_numpy(float))),
                "r2_prompts_defined": int(cell["r2_vs_best_constant"].count()),
                "bootstrap_reps": BOOTSTRAP_REPS,
                "bootstrap_seed": seed,
            }
        )
    summary = pd.DataFrame(rows).sort_values(["domain", "capability_rank", "regime"]).reset_index(drop=True)
    expected = len(D.DOMAIN_CONFIGS) * len(pipeline_order()) * len(REGIME_ORDER)
    if len(summary) != expected or not summary["target_prompts"].eq(12).all():
        raise ValueError("summary grid is incomplete")
    return summary


def main() -> None:
    args = parse_args()
    evidence = EVIDENCE[args.evidence]
    fits_dir = Path(args.fits_dir)
    output_dir = ensure(Path(args.output_dir or EVALUATION_DIR / f"holdout_grid_{evidence}"))
    parts = [evaluate_main_fit(fits_dir / "main", evidence), load_framing_holdout(fits_dir / "holdout_framing", evidence)]
    if evidence == "eight_decision":
        parts += [evaluate_domain_holdout(fits_dir / "holdout_domain"), load_joint_holdout(fits_dir / "holdout_joint")]
    else:
        parts.append(load_report_domain_holdout(fits_dir / "holdout_report_domain"))
    comparison = pd.concat([part[COMMON_COLUMNS] for part in parts], ignore_index=True)
    comparison = comparison.merge(
        target_test_baselines(), on=["domain", "model_key", "target_output"], how="left", validate="many_to_one"
    )
    comparison["r2_vs_best_constant"] = np.where(
        comparison["best_constant_brier_score"] > 0.0,
        1.0 - comparison["brier_score"] / comparison["best_constant_brier_score"],
        np.nan,
    )
    expected = len(D.DOMAIN_CONFIGS) * len(pipeline_order()) * len(C.GENERIC_SPECS) * len(REGIME_ORDER)
    if len(comparison) != expected or comparison.duplicated(["domain", "model_key", "target_output", "regime"]).any():
        raise ValueError(f"comparison has {len(comparison)} rows; expected {expected} unique cells")
    summary = summarize_model_regimes(comparison)
    comparison = comparison.sort_values(
        ["domain", "capability_rank", "family", "target_threshold", "regime"]
    ).reset_index(drop=True)
    comparison.to_csv(output_dir / "holdout_information_prompt_metrics.csv", index=False)
    summary.to_csv(output_dir / "holdout_information_model_summary.csv", index=False)
    metadata = {
        "posterior_evidence": {
            "eight_decision": "the eight decisions of the two non-target framing families",
            "report": "stated probability, Likert report and probability-quintile report",
        }[evidence],
        "fits_dir": str(fits_dir.resolve()),
        "bootstrap": {"method": "percentile bootstrap over the twelve target decisions", "reps": BOOTSTRAP_REPS,
                      "seed": BOOTSTRAP_SEED},
        "c_index_score_decimals": C.C_INDEX_SCORE_DECIMALS,
        "counts": {"prompt_regime_cells": int(len(comparison)), "summary_dots": int(len(summary))},
    }
    (output_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    overall = summary.groupby(["regime"], sort=False)[["mean_c_index", "pooled_r2"]].mean()
    print(overall.to_string(float_format=lambda value: f"{value:.4f}"), flush=True)
    print(f"Wrote {output_dir}", flush=True)


if __name__ == "__main__":
    main()
