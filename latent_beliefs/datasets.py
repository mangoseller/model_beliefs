"""Build the complete-case 16-output analysis matrix of each domain.

Reads the strict-parsed responses ``<parsed>/<domain>/response_values.csv``
(one row per model x case x output) and writes ``<datasets>/<domain>.csv``:
one row per model and case for which all 16 outputs parsed (Methods, "Action spaces", and the
complete-case retention table in the appendix), with

* a raw value column ``{output}`` and a model-input column ``{output}__input``
  for each output: probabilities and binary decisions are used as-is; the two
  five-point ordinal outputs are rescaled to ``(v - 1) / 4``;
* a per-model ``split`` (train/val/test) used to fit and evaluate every
  latent-variable model.

Splits are drawn separately for each model from its own complete cases: 20% of
cases go to test and 20% of the remainder to validation, then cases are moved
from train into a holdout split when needed so that every nonconstant output
varies within it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from latent_beliefs.config import DATASETS_DIR, DOMAINS, PARSED_DIR, ensure
from latent_beliefs.models import MODEL_DISPLAY, SPLIT_INDEX

SEED = 20260703
TEST_FRAC = 0.20
VAL_FRAC_WITHIN_TRAIN = 0.20


@dataclass(frozen=True)
class DomainSpec:
    case_column: str  # case identifier in response_values.csv
    output_column: str  # column naming the output (prompt variant)
    label_column: str | None  # ground truth carried into the dataset
    # (dataset column, value in ``output_column``, kind) in dataset column order.
    outputs: tuple[tuple[str, str, str], ...]


DOMAIN_SPECS = {
    "ckd": DomainSpec(
        case_column="source_prompt_id",
        output_column="prompt_variant",
        label_column=None,
        outputs=(
            ("probability", "probability", "probability"),
            ("decision_no_threshold", "decision_no_threshold", "binary"),
            ("threshold_05pct", "threshold_05pct", "binary"),
            ("bet_payoff_19to1", "bet_payoff_19to1", "binary"),
            ("cost_fn19", "cost_fn19", "binary"),
            ("template_01_threshold_10pct", "template_01_threshold_10pct", "binary"),
            ("template_02_threshold_20pct", "template_02_threshold_20pct", "binary"),
            ("template_03_threshold_33pct", "template_03_threshold_33pct", "binary"),
            ("bet_payoff_2to1", "bet_payoff_2to1", "binary"),
            ("bet_payoff_4to1", "bet_payoff_4to1", "binary"),
            ("bet_payoff_9to1", "bet_payoff_9to1", "binary"),
            ("template_07_cost_fn2", "template_07_cost_fn2", "binary"),
            ("template_08_cost_fn4", "template_08_cost_fn4", "binary"),
            ("template_09_cost_fn9", "template_09_cost_fn9", "binary"),
            ("likert_ckd_likelihood", "likert", "ordinal"),
            ("probability_bin_quintile", "probability_bin_quintile", "ordinal"),
        ),
    ),
    "werewolf": DomainSpec(
        case_column="case_id",
        output_column="prompt_family",
        label_column="label_is_werewolf",
        outputs=tuple(
            (name, name, kind)
            for name, kind in (
                ("probability", "probability"),
                ("decision_no_threshold", "binary"),
                ("threshold_10pct", "binary"),
                ("threshold_20pct", "binary"),
                ("threshold_33pct", "binary"),
                ("bet_payoff_2to1", "binary"),
                ("bet_payoff_4to1", "binary"),
                ("bet_payoff_9to1", "binary"),
                ("cost_fn2", "binary"),
                ("cost_fn4", "binary"),
                ("cost_fn9", "binary"),
                ("threshold_05pct", "binary"),
                ("bet_payoff_19to1", "binary"),
                ("cost_fn19", "binary"),
                ("likert", "ordinal"),
                ("probability_bin_quintile", "ordinal"),
            )
        ),
    ),
    "forecasting": DomainSpec(
        case_column="scenario_id",
        output_column="framing",
        label_column="gold_positive",
        outputs=tuple(
            (name, name, kind)
            for name, kind in (
                ("probability_positive", "probability"),
                ("bet_payoff_19to1", "binary"),
                ("bet_payoff_2to1", "binary"),
                ("bet_payoff_4to1", "binary"),
                ("bet_payoff_9to1", "binary"),
                ("experiment_priority_cost_fn19", "binary"),
                ("experiment_priority_cost_fn2", "binary"),
                ("experiment_priority_cost_fn4", "binary"),
                ("experiment_priority_cost_fn9", "binary"),
                ("experiment_priority_no_threshold", "binary"),
                ("experiment_priority_threshold_05pct", "binary"),
                ("experiment_priority_threshold_10pct", "binary"),
                ("experiment_priority_threshold_20pct", "binary"),
                ("experiment_priority_threshold_33pct", "binary"),
                ("likert", "ordinal"),
                ("probability_bin_quintile", "ordinal"),
            )
        ),
    ),
}


def output_names(domain: str) -> list[str]:
    return [name for name, _, _ in DOMAIN_SPECS[domain].outputs]


def to_input(kind: str, value: float) -> float:
    if kind == "ordinal":
        return (value - 1.0) / 4.0
    return value


def parsed_values(spec: DomainSpec, rows: pd.DataFrame) -> dict[str, float]:
    """Case ID -> parsed value of one output, over strictly parsed rows."""
    values = rows["parsed_value"]
    keep = (rows["parse_ok"] == 1) & values.notna()
    return {
        str(case_id): float(value)
        for case_id, value in zip(rows.loc[keep, spec.case_column], values[keep])
    }


def comparable_pair_count(y_true: np.ndarray) -> int:
    """Number of pairs with distinct values (pairs a c-index can rank)."""
    y = np.asarray(y_true, dtype=float)
    y = y[np.isfinite(y)]
    if len(y) < 2:
        return 0
    _, counts = np.unique(y, return_counts=True)
    total = len(y) * (len(y) - 1) // 2
    tied = int(np.sum(counts * (counts - 1) // 2))
    return int(total - tied)


def ensure_comparable_holdout(
    names: list[str],
    holdout_ids: set[str],
    train_ids: set[str],
    by_output: dict[str, dict[str, float]],
) -> None:
    """Move a training case into the holdout for each nonconstant output that is
    constant within the holdout."""
    for name in names:
        values = by_output[name]
        all_ids = set(values)
        if comparable_pair_count(np.array([values[i] for i in all_ids], dtype=float)) == 0:
            continue
        if comparable_pair_count(np.array([values[i] for i in holdout_ids], dtype=float)) > 0:
            continue
        holdout_values = {values[i] for i in holdout_ids}
        candidate = None
        for source_id in sorted(train_ids):
            if not holdout_values or values[source_id] not in holdout_values:
                candidate = source_id
                break
        if candidate is None and train_ids:
            candidate = sorted(train_ids)[0]
        if candidate is not None:
            train_ids.remove(candidate)
            holdout_ids.add(candidate)


def build(domain: str, raw: pd.DataFrame) -> pd.DataFrame:
    spec = DOMAIN_SPECS[domain]
    names = output_names(domain)
    kinds = {name: kind for name, _, kind in spec.outputs}
    labels: dict[str, Any] = {}
    if spec.label_column is not None:
        labels = (
            raw[[spec.case_column, spec.label_column]]
            .dropna()
            .drop_duplicates(spec.case_column)
            .set_index(spec.case_column)[spec.label_column]
            .astype(int)
            .to_dict()
        )

    rows: list[dict[str, Any]] = []
    # Models are processed (and written) in split-index order.
    split_index = SPLIT_INDEX[domain]
    for model in sorted(split_index, key=split_index.__getitem__):
        model_rows = raw[raw["model_alias"] == model]
        by_output = {
            name: parsed_values(spec, model_rows[model_rows[spec.output_column] == variant])
            for name, variant, _ in spec.outputs
        }
        common = sorted(set.intersection(*(set(v) for v in by_output.values())))

        rng = np.random.default_rng(SEED + 1009 * (split_index[model] + 1))
        shuffled = np.array(common, dtype=object)
        rng.shuffle(shuffled)
        n_test = max(1, int(round(TEST_FRAC * len(shuffled))))
        test_ids = set(shuffled[:n_test])
        train_val_ids = shuffled[n_test:]
        n_val = max(1, int(round(VAL_FRAC_WITHIN_TRAIN * len(train_val_ids))))
        val_ids = set(train_val_ids[:n_val])
        train_ids = set(train_val_ids[n_val:])
        ensure_comparable_holdout(names, test_ids, train_ids, by_output)
        ensure_comparable_holdout(names, val_ids, train_ids, by_output)

        for case_id in common:
            split = "test" if case_id in test_ids else "val" if case_id in val_ids else "train"
            record: dict[str, Any] = {"model_key": model, "model_display": MODEL_DISPLAY[model]}
            if spec.case_column != "source_prompt_id":
                record[spec.case_column] = case_id
            # Every dataset also carries the case ID as ``source_prompt_id``.
            record["source_prompt_id"] = case_id
            record["split"] = split
            if spec.label_column is not None:
                record[spec.label_column] = labels.get(case_id, np.nan)
            for name in names:
                value = by_output[name][case_id]
                record[name] = value
                record[f"{name}__input"] = to_input(kinds[name], value)
            rows.append(record)
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--parsed-dir", type=Path, default=PARSED_DIR)
    parser.add_argument("--out-dir", type=Path, default=DATASETS_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure(args.out_dir)
    for domain in args.domains:
        raw = pd.read_csv(args.parsed_dir / domain / "response_values.csv", low_memory=False)
        dataset = build(domain, raw)
        path = args.out_dir / f"{domain}.csv"
        dataset.to_csv(path, index=False)
        counts = dataset.groupby(["model_key", "split"], sort=False).size().unstack(fill_value=0)
        print(f"Wrote {path}")
        print(counts.to_string())


if __name__ == "__main__":
    main()
