"""Paths and the run configuration of the second-order experiment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from latent_beliefs.config import PACKAGE_ROOT

ROOT = PACKAGE_ROOT
CONFIG_PATH = ROOT / "configs" / "second_order.yaml"
DATA = ROOT / "data"
RESPONSES = DATA / "responses"
LATENTS = DATA / "latents"
BELIEFS = DATA / "beliefs"
ACTIVATIONS = DATA / "activations"
CACHE = DATA / "generation_cache"
RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"
DOCS = ROOT / "docs"
RELEASE_CASES = DATA / "release" / "sources" / "ckd" / "cases.csv"
FIRST_ORDER_DATASET = ROOT / "build" / "datasets" / "ckd.csv"
RELEASE_RESPONSES = DATA / "release" / "responses" / "ckd"
RELEASE_PROMPTS = DATA / "release" / "prompts" / "ckd"
RELEASE_MAIN_FIT = DATA / "release" / "fits" / "main"


@lru_cache(maxsize=None)
def load(path: str | None = None) -> dict[str, Any]:
    return yaml.safe_load(Path(path or CONFIG_PATH).read_text())


def model_config(model: str) -> dict[str, Any]:
    return load()["models"][model]


def scheduled_conditions(model: str) -> list[str]:
    """Conditions this model runs: the default schedule, plus the extras for the models that get them."""
    schedule = load()["schedule"]
    extra = schedule["extra"] if split_source(model) in schedule["extra_models"] else []
    return list(schedule["default"]) + list(extra)


def split_source(model: str) -> str:
    """Model whose condition A split assignment this model reuses."""
    return model_config(model).get("split_from", model)
