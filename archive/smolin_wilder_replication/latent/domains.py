"""The sixteen analysed outputs of each domain and their observation matrices.

Every domain has one stated probability, thirteen binary decisions (one
unparameterized decision plus threshold, betting, and cost framings at
5%, 10%, 20% and 33%) and two five-point ordinal reports (Likert likelihood and
probability quintile).  Analysis matrices are read from
``DATASETS_DIR/<domain>.csv`` (one row per model x case, with a ``split``
column taking values ``train``/``val``/``test``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from archive.smolin_wilder_replication.config import DATASETS_DIR
from archive.smolin_wilder_replication.latent.model import OutputSpec


@dataclass(frozen=True)
class DomainConfig:
    key: str
    display: str
    output_specs: tuple[OutputSpec, ...]

    @property
    def dataset_path(self) -> Path:
        return DATASETS_DIR / f"{self.key}.csv"


def probability(name: str) -> OutputSpec:
    return OutputSpec(name, "probability")


def binary(name: str, threshold: float | None) -> OutputSpec:
    return OutputSpec(name, "binary", nominal_threshold=threshold)


def ordinal(name: str) -> OutputSpec:
    return OutputSpec(name, "ordinal", n_categories=5)


CKD_SPECS = (
    probability("probability"),
    binary("decision_no_threshold", None),
    binary("threshold_05pct", 0.05),
    binary("bet_payoff_19to1", 0.05),
    binary("cost_fn19", 0.05),
    binary("template_01_threshold_10pct", 0.10),
    binary("template_02_threshold_20pct", 0.20),
    binary("template_03_threshold_33pct", 1.0 / 3.0),
    binary("bet_payoff_2to1", 1.0 / 3.0),
    binary("bet_payoff_4to1", 0.20),
    binary("bet_payoff_9to1", 0.10),
    binary("template_07_cost_fn2", 1.0 / 3.0),
    binary("template_08_cost_fn4", 0.20),
    binary("template_09_cost_fn9", 0.10),
    ordinal("likert_ckd_likelihood"),
    ordinal("probability_bin_quintile"),
)

WEREWOLF_SPECS = (
    probability("probability"),
    binary("decision_no_threshold", None),
    binary("threshold_10pct", 0.10),
    binary("threshold_20pct", 0.20),
    binary("threshold_33pct", 1.0 / 3.0),
    binary("bet_payoff_2to1", 1.0 / 3.0),
    binary("bet_payoff_4to1", 0.20),
    binary("bet_payoff_9to1", 0.10),
    binary("cost_fn2", 1.0 / 3.0),
    binary("cost_fn4", 0.20),
    binary("cost_fn9", 0.10),
    binary("threshold_05pct", 0.05),
    binary("bet_payoff_19to1", 0.05),
    binary("cost_fn19", 0.05),
    ordinal("likert"),
    ordinal("probability_bin_quintile"),
)

FORECASTING_SPECS = (
    probability("probability_positive"),
    binary("bet_payoff_19to1", 0.05),
    binary("bet_payoff_2to1", 1.0 / 3.0),
    binary("bet_payoff_4to1", 0.20),
    binary("bet_payoff_9to1", 0.10),
    binary("experiment_priority_cost_fn19", 0.05),
    binary("experiment_priority_cost_fn2", 1.0 / 3.0),
    binary("experiment_priority_cost_fn4", 0.20),
    binary("experiment_priority_cost_fn9", 0.10),
    binary("experiment_priority_no_threshold", None),
    binary("experiment_priority_threshold_05pct", 0.05),
    binary("experiment_priority_threshold_10pct", 0.10),
    binary("experiment_priority_threshold_20pct", 0.20),
    binary("experiment_priority_threshold_33pct", 1.0 / 3.0),
    ordinal("likert"),
    ordinal("probability_bin_quintile"),
)

# Insertion order is the canonical domain order (it also enters fit seeds).
DOMAIN_CONFIGS = {
    "ckd": DomainConfig("ckd", "CKD screening", CKD_SPECS),
    "werewolf": DomainConfig("werewolf", "Werewolf", WEREWOLF_SPECS),
    "forecasting": DomainConfig("forecasting", "Scientific forecasting", FORECASTING_SPECS),
}

# Identifier columns carried through to per-case outputs, when present.
ID_COLUMNS = ("source_prompt_id", "case_id", "scenario_id")


def load_dataset(domain: str) -> pd.DataFrame:
    """Read one domain's analysis matrix."""

    return pd.read_csv(DOMAIN_CONFIGS[domain].dataset_path)


def model_splits(
    dataset: pd.DataFrame, model_key: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (all rows, train, validation, test) of one model, each re-indexed."""

    group = dataset[dataset["model_key"] == model_key].reset_index(drop=True)
    if group.empty:
        raise ValueError(f"dataset has no rows for {model_key}")
    train = group[group["split"] == "train"].reset_index(drop=True)
    validation = group[group["split"] == "val"].reset_index(drop=True)
    test = group[group["split"] == "test"].reset_index(drop=True)
    return group, train, validation, test


def likelihood_values(frame: pd.DataFrame, specs: Sequence[OutputSpec]) -> np.ndarray:
    """[case, output] matrix of likelihood-scale values (NaN = missing)."""

    columns = [spec.observation_column or spec.name for spec in specs]
    missing = [column for column in columns if column not in frame]
    if missing:
        raise ValueError(f"dataset is missing likelihood columns: {', '.join(missing)}")
    return frame[columns].to_numpy(dtype=float)
