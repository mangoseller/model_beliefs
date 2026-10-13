"""Filesystem layout for the replication package.

Three roots, each overridable with an environment variable:

``LB_RAW_DIR`` (default ``data/raw``)
    Public source datasets that the user downloads: NHANES XPT files, the
    Kaggle Werewolf replay archive, and the Ashokkumar, Hewitt et al. (2026)
    Code Ocean materials.

``LB_RELEASE_DIR`` (default ``data/release``)
    The redistributed results bundle: every LLM response used in the paper and
    the fitted latent-variable models.  Stage outputs that are expensive or
    hardware-dependent to regenerate (GPU model fits) are read from here.

``LB_BUILD_DIR`` (default ``build``)
    Everything the pipeline generates: cases, prompts, parsed responses,
    analysis matrices, evaluations, figures, and tables.

Fitted models are read from ``LB_FITS_DIR`` (default ``<release>/fits``).  To
use your own refits instead, run the fitting stage (which writes to
``<build>/fits``) and set ``LB_FITS_DIR=build/fits``.  Likewise, parsing reads
responses from ``LB_RESPONSES_DIR`` (default ``<release>/responses``); after
collecting and materializing new responses, point it at ``<build>/responses``.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _dir(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser().resolve() if value else default


RAW_DIR = _dir("LB_RAW_DIR", PACKAGE_ROOT / "data" / "raw")
RELEASE_DIR = _dir("LB_RELEASE_DIR", PACKAGE_ROOT / "data" / "release")
BUILD_DIR = _dir("LB_BUILD_DIR", PACKAGE_ROOT / "build")
FITS_DIR = _dir("LB_FITS_DIR", RELEASE_DIR / "fits")
RESPONSES_DIR = _dir("LB_RESPONSES_DIR", RELEASE_DIR / "responses")
# The Kaggle Werewolf archive is ~57 GB; point this at an existing copy (for
# example kagglehub's cache) to avoid downloading it again.
WEREWOLF_REPLAY_DIR = _dir(
    "LB_WEREWOLF_REPLAY_DIR", RAW_DIR / "werewolf" / "werewolf-dataset-v4"
)

# Static inputs shipped with the code (hand-authored prompt text, etc.).
ASSETS_DIR = PACKAGE_ROOT / "latent_beliefs" / "assets"

# ---------------------------------------------------------------- build tree
SOURCES_DIR = BUILD_DIR / "sources"  # case sets and ground truth
PROMPTS_DIR = BUILD_DIR / "prompts"  # exact prompt texts sent to models
COLLECTION_DIR = BUILD_DIR / "collection"  # batch inputs/outputs
MATERIALIZED_DIR = BUILD_DIR / "responses"  # newly collected responses
PARSED_DIR = BUILD_DIR / "parsed"  # strict-parsed response values
DATASETS_DIR = BUILD_DIR / "datasets"  # complete-case analysis matrices
REFIT_DIR = BUILD_DIR / "fits"  # optional local refits
EVALUATION_DIR = BUILD_DIR / "evaluation"  # held-out evaluations
FIGURES_DIR = BUILD_DIR / "figures"  # paper figures (PDF)
FIGURE_DATA_DIR = BUILD_DIR / "figure_data"  # tidy CSV behind every figure
TABLES_DIR = BUILD_DIR / "tables"  # LaTeX tables

DOMAINS = ("ckd", "werewolf", "forecasting")
"""Domain keys: chronic-kidney-disease screening, Werewolf, and scientific
forecasting (built from Ashokkumar, Hewitt et al., Nature, 2026)."""

# Domain labels used in figures and figure data.
DOMAIN_DISPLAY = {
    "ckd": "Kidney disease",
    "werewolf": "Werewolf",
    "forecasting": "Scientific forecasting",
}


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
