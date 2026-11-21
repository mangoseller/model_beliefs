"""Paths and the run configuration of the experiment.

Environment overrides (used by the end-to-end tests and for scratch runs):
``SO_CONFIG`` (config file), ``SO_DATA_DIR`` (generation cache, responses,
latents, beliefs, activations), ``SO_RESULTS_DIR`` (tables and reports, default ``outputs/``), ``SO_FIGURES_DIR`` and
``SO_DATASETS_DIR`` (the folder of the two datasets, default ``datasets/``).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from latent_beliefs.config import PACKAGE_ROOT

ROOT = PACKAGE_ROOT
CONFIG_PATH = Path(os.environ.get("SO_CONFIG", Path(__file__).with_name("config.yaml")))
DATA = Path(os.environ.get("SO_DATA_DIR", ROOT / "data"))
RESPONSES = DATA / "responses"
LATENTS = DATA / "latents"
BELIEFS = DATA / "beliefs"
ACTIVATIONS = DATA / "activations"
CACHE = DATA / "generation_cache"
RESULTS = Path(os.environ.get("SO_RESULTS_DIR", ROOT / "outputs"))
FIGURES = Path(os.environ.get("SO_FIGURES_DIR", ROOT / "figures"))
DATASETS = Path(os.environ.get("SO_DATASETS_DIR", ROOT / "datasets"))


@lru_cache(maxsize=None)
def load(path: str | None = None) -> dict[str, Any]:
    return yaml.safe_load(Path(path or CONFIG_PATH).read_text())


def model_config(model: str) -> dict[str, Any]:
    return load()["models"][model]


def datasets_for(model: str) -> list[str]:
    return list(model_config(model)["datasets"])


def resolve(path: str) -> Path:
    """A dataset path from the config (``datasets/...``), under ``SO_DATASETS_DIR`` when it is set."""
    p = Path(path)
    if p.is_absolute():
        return p
    return DATASETS / p.relative_to("datasets") if p.parts[0] == "datasets" else ROOT / p
