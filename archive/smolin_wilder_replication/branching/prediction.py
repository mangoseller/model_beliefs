"""Reciprocal prediction from the same versus a different reasoning prefix (Figure 6c).

The main fitted latent model of each domain and model is restored unchanged.
For every complete behavior-branch (all 16 follow-ups parsed) two tasks are
evaluated, as in Figure 1:

``reports_to_decisions``  condition on the three probability reports, predict
    the thirteen decisions (posterior-predictive means);
``decisions_to_reports``  condition on the thirteen decisions, predict the
    three reports.

Same prefix: evidence and targets come from the same branch.  Different
prefix: the evidence comes from another complete branch of the same case,
drawn uniformly at random for every branch, 1,000 times.  Scores are c-indices
averaged over targets within a task, then over the two tasks with equal weight.

Usage::

    python -m archive.smolin_wilder_replication.branching.prediction

Inputs: ``PARSED_DIR/branching/behavior/response_values.csv`` and
``FITS_DIR/main`` (``parameters.csv``, ``run_metadata.json``).
Outputs (``EVALUATION_DIR/branching_prediction``):
``branch_latent_cindex_summary.csv`` (one row per model x domain),
``branch_latent_cindex_by_prompt.csv`` (one row per target output),
``branch_latent_cindex_run_summary.json``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import torch

from archive.smolin_wilder_replication.branching.design import BRANCHING_PARSED_DIR
from archive.smolin_wilder_replication.config import EVALUATION_DIR, FITS_DIR
from archive.smolin_wilder_replication.latent.domains import DOMAIN_CONFIGS
from archive.smolin_wilder_replication.latent.model import (
    OutputSpec,
    ProbabilisticLatentModel,
    load_fit,
    restore_cell,
)
from archive.smolin_wilder_replication.models import MODEL_DISPLAY

REPETITIONS = 1000
SEED = 20260811
GROUP_KEYS = ["model_alias", "model_display", "domain"]
CONDITION_ORDER = ("reports_to_decisions", "decisions_to_reports")
CONDITION_LABEL = {
    "reports_to_decisions": "Probability reports to decisions",
    "decisions_to_reports": "Decisions to probability reports",
}


def concordance_index_fast(outcome: np.ndarray, predictor: np.ndarray) -> tuple[float, int, int]:
    """Compute c-index, excluding outcome ties and half-crediting predictor ties."""
    y = np.asarray(outcome, dtype=float)
    x = np.asarray(predictor, dtype=float)
    finite = np.isfinite(y) & np.isfinite(x)
    y = y[finite]
    x = x[finite]
    if len(y) < 2:
        return float("nan"), 0, int(len(y))

    x_values = np.unique(x)
    x_rank = np.searchsorted(x_values, x)
    order = np.argsort(y, kind="stable")
    y = y[order]
    x_rank = x_rank[order]
    previous = np.zeros(len(x_values), dtype=np.int64)
    previous_n = 0
    comparable = 0
    concordant = 0.0
    start = 0
    while start < len(y):
        end = start + 1
        while end < len(y) and y[end] == y[start]:
            end += 1
        current = np.bincount(x_rank[start:end], minlength=len(x_values))
        cumulative = np.cumsum(previous)
        strictly_lower = np.concatenate(([0], cumulative[:-1]))
        concordant += float(np.sum(current * (strictly_lower + 0.5 * previous)))
        comparable += int((end - start) * previous_n)
        previous += current
        previous_n += end - start
        start = end
    if comparable == 0:
        return float("nan"), 0, int(len(y))
    return float(concordant / comparable), comparable, int(len(y))


def branch_variant(spec: OutputSpec, domain: str) -> str:
    """Follow-up name of a model output (the CKD Likert output differs)."""
    if domain == "ckd" and spec.name == "likert_ckd_likelihood":
        return "likert"
    return str(spec.name)


def complete_branch_matrix(
    responses: pd.DataFrame, domain: str, specs: Sequence[OutputSpec]
) -> tuple[pd.DataFrame, np.ndarray, list[str]]:
    """Complete branches (rows) by the 16 outputs in model order (columns)."""
    variants = [branch_variant(spec, domain) for spec in specs]
    if len(variants) != 16:
        raise ValueError(f"{domain} main model has {len(variants)} outputs, expected 16")
    selected = responses[responses["prompt_variant"].isin(variants)].copy()
    missing = sorted(set(variants) - set(selected["prompt_variant"]))
    if missing:
        raise ValueError(f"{domain} branching responses lack: {', '.join(missing)}")
    if selected.duplicated(["branch_id", "prompt_variant"]).any():
        raise ValueError(f"{domain} has duplicate branch/prompt response rows")

    branch_rows = (
        selected[["branch_id", "instance_id"]]
        .drop_duplicates()
        .sort_values(["instance_id", "branch_id"])
        .reset_index(drop=True)
    )
    value_wide = selected.pivot(
        index="branch_id", columns="prompt_variant", values="parsed_value"
    ).reindex(index=branch_rows["branch_id"], columns=variants)
    values = value_wide.to_numpy(dtype=float)
    complete = np.isfinite(values).all(axis=1)
    return branch_rows.loc[complete].reset_index(drop=True), values[complete], variants


def condition_indices(specs: Sequence[OutputSpec]) -> dict[str, dict[str, list[int]]]:
    decisions = [index for index, spec in enumerate(specs) if spec.kind == "binary"]
    reports = [index for index, spec in enumerate(specs) if spec.kind in {"probability", "ordinal"}]
    if len(decisions) != 13 or len(reports) != 3:
        raise ValueError(f"expected 13 decisions and 3 reports, got {len(decisions)} and {len(reports)}")
    return {
        "reports_to_decisions": {"evidence": reports, "targets": decisions},
        "decisions_to_reports": {"evidence": decisions, "targets": reports},
    }


def reciprocal_group_predictors(
    model: ProbabilisticLatentModel, values: np.ndarray
) -> dict[str, np.ndarray]:
    """Posterior-predictive means for the two complementary held-out groups."""
    observations = torch.as_tensor(values, dtype=model.dtype, device=model.z_grid.device)
    model.validate_observations(observations)
    conditions = condition_indices(model.specs)
    predictions = {
        condition: np.full(tuple(observations.shape), np.nan, dtype=float)
        for condition in CONDITION_ORDER
    }
    model.eval()
    with torch.no_grad():
        log_by_output = model.log_likelihood_by_output(observations)
        prior = model.prior_log_weights()[None, :]
        for condition in CONDITION_ORDER:
            evidence = conditions[condition]["evidence"]
            log_joint = log_by_output[:, evidence, :].sum(dim=1) + prior
            log_weights = log_joint - torch.logsumexp(log_joint, dim=1, keepdim=True)
            weights = torch.exp(log_weights)
            for target_index in conditions[condition]["targets"]:
                spec = model.specs[target_index]
                predictions[condition][:, target_index] = (
                    (weights @ model.conditional_mean(spec)).cpu().numpy()
                )
    return predictions


def task_cindices(
    per_prompt_cindices: np.ndarray, conditions: dict[str, dict[str, list[int]]]
) -> dict[str, np.ndarray]:
    """Average prompts within each reciprocal task, leaving tasks separate."""
    return {
        condition: np.nanmean(per_prompt_cindices[..., spec["targets"]], axis=-1)
        for condition, spec in conditions.items()
    }


def draw_other_replicate_indices(branch_rows: pd.DataFrame, rng: np.random.Generator) -> np.ndarray:
    """Draw another complete branch of the same case for every branch."""
    candidates = {
        instance_id: group.index.to_numpy(dtype=int)
        for instance_id, group in branch_rows.groupby("instance_id", sort=False)
    }
    result = np.full(len(branch_rows), -1, dtype=int)
    for position, instance_id in enumerate(branch_rows["instance_id"]):
        choices = candidates.get(instance_id, np.array([], dtype=int))
        choices = choices[choices != position]
        if len(choices):
            result[position] = int(choices[int(rng.integers(len(choices)))])
    if np.any(result < 0):
        raise ValueError("every branch needs a different complete replicate in its case")
    return result


def evaluate_group(
    responses: pd.DataFrame,
    model: ProbabilisticLatentModel,
    domain: str,
    repetitions: int,
    seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate both reciprocal tasks in one model/domain cell."""
    branch_rows, values, variants = complete_branch_matrix(responses, domain, model.specs)
    conditions = condition_indices(model.specs)
    own_predictions = reciprocal_group_predictors(model, values)

    same_results = np.full(len(variants), np.nan, dtype=float)
    same_metadata: list[tuple[int, int] | None] = [None] * len(variants)
    target_condition: dict[int, str] = {}
    for condition in CONDITION_ORDER:
        for target_index in conditions[condition]["targets"]:
            c_index, pairs, n = concordance_index_fast(
                values[:, target_index], own_predictions[condition][:, target_index]
            )
            same_results[target_index] = c_index
            same_metadata[target_index] = (pairs, n)
            target_condition[target_index] = condition

    rng = np.random.default_rng(seed)
    random_cindices = np.full((repetitions, len(variants)), np.nan, dtype=float)
    for repetition in range(repetitions):
        other_indices = draw_other_replicate_indices(branch_rows, rng)
        for condition in CONDITION_ORDER:
            other_predictions = own_predictions[condition][other_indices]
            for target_index in conditions[condition]["targets"]:
                random_cindices[repetition, target_index] = concordance_index_fast(
                    values[:, target_index], other_predictions[:, target_index]
                )[0]

    prompt_rows: list[dict[str, Any]] = []
    for target_index, (variant, spec) in enumerate(zip(variants, model.specs, strict=True)):
        condition = target_condition[target_index]
        draws = random_cindices[:, target_index]
        finite_draws = draws[np.isfinite(draws)]
        pairs, n = same_metadata[target_index]
        prompt_rows.append(
            {
                "prompt_variant": variant,
                "output_name": spec.name,
                "output_kind": spec.kind,
                "condition": condition,
                "condition_label": CONDITION_LABEL[condition],
                "evidence_output_count": len(conditions[condition]["evidence"]),
                "target_output_count": len(conditions[condition]["targets"]),
                "n_complete_branches": int(len(branch_rows)),
                "n_observations_own_branch": n,
                "n_comparable_pairs_own_branch": pairs,
                "own_branch_c_index": same_results[target_index],
                "random_other_replicate_c_index_mean": (
                    float(np.mean(finite_draws)) if len(finite_draws) else np.nan
                ),
                "random_other_replicate_c_index_sd": (
                    float(np.std(finite_draws, ddof=1)) if len(finite_draws) > 1 else np.nan
                ),
                "random_other_replicate_c_index_p025": (
                    float(np.quantile(finite_draws, 0.025)) if len(finite_draws) else np.nan
                ),
                "random_other_replicate_c_index_p975": (
                    float(np.quantile(finite_draws, 0.975)) if len(finite_draws) else np.nan
                ),
                "random_repetitions": repetitions,
            }
        )
    own_tasks = task_cindices(same_results, conditions)
    random_tasks = task_cindices(random_cindices, conditions)
    own_mean_task = float(np.mean([float(own_tasks[condition]) for condition in CONDITION_ORDER]))
    random_mean_task = np.mean(
        np.column_stack([random_tasks[condition] for condition in CONDITION_ORDER]), axis=1
    )
    repetition_rows = pd.DataFrame(
        {
            "repetition": np.arange(1, repetitions + 1),
            "random_other_replicate_reports_to_decisions_c_index": random_tasks["reports_to_decisions"],
            "random_other_replicate_decisions_to_reports_c_index": random_tasks["decisions_to_reports"],
            "random_other_replicate_mean_task_c_index": random_mean_task,
            "own_branch_reports_to_decisions_c_index": float(own_tasks["reports_to_decisions"]),
            "own_branch_decisions_to_reports_c_index": float(own_tasks["decisions_to_reports"]),
            "own_branch_mean_task_c_index": own_mean_task,
        }
    )
    return pd.DataFrame(prompt_rows), repetition_rows


def summarize(prompts: pd.DataFrame, repetition_results: pd.DataFrame, repetitions: int) -> pd.DataFrame:
    summary_rows = []
    for key, group in repetition_results.groupby(GROUP_KEYS, sort=True):
        draws = group["random_other_replicate_mean_task_c_index"].to_numpy(float)
        prompt_group = prompts[(prompts["model_alias"] == key[0]) & (prompts["domain"] == key[2])]
        own = float(group["own_branch_mean_task_c_index"].iloc[0])
        row = {
            **dict(zip(GROUP_KEYS, key, strict=True)),
            "n_complete_branches": int(prompt_group["n_complete_branches"].iloc[0]),
            "n_prompt_variants_total": int(len(prompt_group)),
            "n_prompt_variants_evaluable": int(prompt_group["own_branch_c_index"].notna().sum()),
            "n_tasks_total": len(CONDITION_ORDER),
            "n_tasks_evaluable": int(
                sum(
                    np.isfinite(group[f"own_branch_{condition}_c_index"].iloc[0])
                    for condition in CONDITION_ORDER
                )
            ),
            "own_branch_mean_task_c_index": own,
            "random_other_replicate_mean_task_c_index_mean": float(np.mean(draws)),
            "random_other_replicate_mean_task_c_index_sd": float(np.std(draws, ddof=1)),
            "random_other_replicate_mean_task_c_index_p025": float(np.quantile(draws, 0.025)),
            "random_other_replicate_mean_task_c_index_p975": float(np.quantile(draws, 0.975)),
            "own_minus_random_c_index": own - float(np.mean(draws)),
            "random_repetitions": repetitions,
        }
        for condition in CONDITION_ORDER:
            condition_draws = group[f"random_other_replicate_{condition}_c_index"].to_numpy(float)
            row[f"own_branch_{condition}_c_index"] = float(
                group[f"own_branch_{condition}_c_index"].iloc[0]
            )
            row[f"random_other_replicate_{condition}_c_index_mean"] = float(np.mean(condition_draws))
            row[f"random_other_replicate_{condition}_c_index_sd"] = float(
                np.std(condition_draws, ddof=1)
            )
            row[f"random_other_replicate_{condition}_c_index_p025"] = float(
                np.quantile(condition_draws, 0.025)
            )
            row[f"random_other_replicate_{condition}_c_index_p975"] = float(
                np.quantile(condition_draws, 0.975)
            )
        summary_rows.append(row)
    return pd.DataFrame(summary_rows)


def run_analysis(
    responses: pd.DataFrame,
    parameters: pd.DataFrame,
    metadata: dict[str, Any],
    repetitions: int,
    seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prompt_parts = []
    repetition_parts = []
    response_groups = {
        key: group for key, group in responses.groupby(["model_alias", "domain"], sort=True)
    }
    # Seed per (model, domain) cell in sorted order.
    for group_index, key in enumerate(sorted(response_groups)):
        model_alias, domain = key
        specs = DOMAIN_CONFIGS[domain].output_specs
        model = restore_cell(parameters, metadata, domain, model_alias, specs)
        prompt, repetition = evaluate_group(
            response_groups[key], model, domain, repetitions, seed + 100_003 * (group_index + 1)
        )
        labels = {
            "model_alias": model_alias,
            "model_display": MODEL_DISPLAY[model_alias],
            "domain": domain,
        }
        for column, value in labels.items():
            prompt[column] = value
            repetition[column] = value
        prompt_parts.append(prompt)
        repetition_parts.append(repetition)
    prompts = pd.concat(prompt_parts, ignore_index=True)
    repetition_results = pd.concat(repetition_parts, ignore_index=True)
    return prompts, summarize(prompts, repetition_results, repetitions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--responses",
        type=Path,
        default=BRANCHING_PARSED_DIR / "behavior" / "response_values.csv",
    )
    parser.add_argument("--fit-dir", type=Path, default=FITS_DIR / "main")
    parser.add_argument("--out-dir", type=Path, default=EVALUATION_DIR / "branching_prediction")
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    responses = pd.read_csv(args.responses)
    parameters, metadata = load_fit(args.fit_dir)
    prompts, summary = run_analysis(responses, parameters, metadata, args.repetitions, args.seed)
    prompts = prompts.sort_values(["domain", "model_alias", "condition", "prompt_variant"])
    summary = summary.sort_values(["domain", "model_alias"])
    args.out_dir.mkdir(parents=True, exist_ok=True)
    prompts.to_csv(args.out_dir / "branch_latent_cindex_by_prompt.csv", index=False)
    summary.to_csv(args.out_dir / "branch_latent_cindex_summary.csv", index=False)
    run_summary = {
        "source_fit_directory": str(args.fit_dir),
        "training": "paper main 16-output probabilistic latent model; observation parameters fixed",
        "outputs": "the same 16 outputs in every domain",
        "held_out_tasks": {
            "reports_to_decisions": "condition on all 3 probability-report prompts and predict all 13 binary decisions",
            "decisions_to_reports": "condition on all 13 binary decisions and predict all 3 probability-report prompts",
        },
        "same_prefix_predictor": "posterior-predictive target means after conditioning on the complementary prompt group from the target branch",
        "different_prefix_predictor": "posterior-predictive target means after conditioning on the complementary prompt group from another reasoning prefix of the same underlying case",
        "complete_case_rule": "all 16 branching responses must parse for both reciprocal tasks",
        "aggregation": "mean target c-index within each reciprocal task, followed by an equal-weight mean of the two task scores",
        "random_repetitions": args.repetitions,
        "seed": args.seed,
        "n_model_domain_cells": int(len(summary)),
    }
    (args.out_dir / "branch_latent_cindex_run_summary.json").write_text(
        json.dumps(run_summary, indent=2) + "\n", encoding="utf-8"
    )
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
