"""Paths and the run configuration of the second-order experiment.

Environment overrides (used by the end-to-end tests and for scratch runs):
``SO_CONFIG`` (config file), ``SO_DATA_DIR`` (generation cache, responses,
latents, beliefs, activations), ``SO_RESULTS_DIR`` (results), ``SO_FIGURES_DIR`` and
``SO_BESPOKE_ITEMS`` (the bespoke item file).  The released first-order data
always come from ``data/release``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from latent_beliefs.config import PACKAGE_ROOT

ROOT = PACKAGE_ROOT
CONFIG_PATH = Path(os.environ.get("SO_CONFIG", ROOT / "configs" / "second_order.yaml"))
DATA = Path(os.environ.get("SO_DATA_DIR", ROOT / "data"))
RESPONSES = DATA / "responses"
LATENTS = DATA / "latents"
BELIEFS = DATA / "beliefs"
ACTIVATIONS = DATA / "activations"
CACHE = DATA / "generation_cache"
RESULTS = Path(os.environ.get("SO_RESULTS_DIR", ROOT / "results"))
FIGURES = Path(os.environ.get("SO_FIGURES_DIR", ROOT / "figures"))
DOCS = ROOT / "docs"
RELEASE = ROOT / "data" / "release"
RELEASE_CASES = RELEASE / "sources" / "ckd" / "cases.csv"
FIRST_ORDER_DATASET = ROOT / "build" / "datasets" / "ckd.csv"
RELEASE_RESPONSES = RELEASE / "responses" / "ckd"
RELEASE_PROMPTS = RELEASE / "prompts" / "ckd"
RELEASE_MAIN_FIT = RELEASE / "fits" / "main"


@lru_cache(maxsize=None)
def load(path: str | None = None) -> dict[str, Any]:
    return yaml.safe_load(Path(path or CONFIG_PATH).read_text())


def model_config(model: str) -> dict[str, Any]:
    return load()["models"][model]


def scheduled_conditions(model: str) -> list[str]:
    """Conditions this model runs.

    A model entry with its own ``conditions`` list runs exactly those (the
    curse-of-knowledge models); otherwise the default schedule, plus the
    extras for the models that get them.
    """
    own = model_config(model).get("conditions")
    if own:
        return list(own)
    schedule = load()["schedule"]
    extra = schedule["extra"] if split_source(model) in schedule["extra_models"] else []
    return list(schedule["default"]) + list(extra)


def split_source(model: str) -> str:
    """Model whose condition A split assignment this model reuses."""
    return model_config(model).get("split_from", model)


def self_layout(model: str | None = None) -> str:
    """Layout of own-belief prompts (A, B*) for a model.

    Models whose condition A comes from the release keep the first-order
    layout, so their B prompts match the released A prompts.
    """
    if model is not None and model_config(model).get("cached_a"):
        return "first_order"
    return load()["self_layout"]


def replicates(condition: str) -> int:
    """Independent runs of a condition (2 for A and B*, used for the reliability rho)."""
    return int(load()["experiment"]["replicates"].get(condition, 1))


def datasets_for(model: str) -> list[str]:
    return list(model_config(model).get("datasets", ["ckd"]))


def bespoke_items_path() -> Path:
    path = Path(os.environ.get("SO_BESPOKE_ITEMS", load()["bespoke"]["items"]))
    return path if path.is_absolute() else ROOT / path
