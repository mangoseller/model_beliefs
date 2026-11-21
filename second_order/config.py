"""Paths and the run configuration of the experiment.

Environment overrides (used by the end-to-end tests and for scratch runs):
``SO_CONFIG`` (config file), ``SO_DATA_DIR`` (generation cache, responses,
latents, beliefs, activations), ``SO_RESULTS_DIR`` (tables and reports, default ``outputs/``), ``SO_FIGURES_DIR`` and
``SO_BESPOKE_ITEMS`` (the bespoke item file).  The CKD cases and first-order
prompts always come from the paper's release in ``data/release``.
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
RELEASE = ROOT / "data" / "release"
RELEASE_CASES = RELEASE / "sources" / "ckd" / "cases.csv"
RELEASE_PROMPTS = RELEASE / "prompts" / "ckd"


@lru_cache(maxsize=None)
def load(path: str | None = None) -> dict[str, Any]:
    return yaml.safe_load(Path(path or CONFIG_PATH).read_text())


def model_config(model: str) -> dict[str, Any]:
    return load()["models"][model]


def conditions() -> list[str]:
    """Conditions every model runs."""
    return list(load()["experiment"]["conditions"])


def replicates(condition: str) -> int:
    """Independent runs of a condition (2 for A and B*, used for the reliability rho)."""
    return int(load()["experiment"]["replicates"].get(condition, 1))


def datasets_for(model: str) -> list[str]:
    return list(model_config(model)["datasets"])


def bespoke_items_path() -> Path:
    path = Path(os.environ.get("SO_BESPOKE_ITEMS", load()["bespoke"]["items"]))
    return path if path.is_absolute() else ROOT / path
