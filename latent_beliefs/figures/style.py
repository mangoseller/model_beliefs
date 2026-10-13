"""Shared style for the paper figures.

Import this module before ``matplotlib.pyplot`` is used anywhere (it selects
the Agg backend).  Every figure module uses these constants for ordering,
naming and color, and writes its PDF and tidy data with :func:`save_figure`
and :func:`write_data`.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "latent-beliefs-matplotlib-cache"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.config import DOMAIN_DISPLAY, DOMAINS, FIGURE_DATA_DIR, FIGURES_DIR, ensure  # noqa: E402,F401 (DOMAIN_DISPLAY is re-exported)
from latent_beliefs.models import BRANCHING_MODELS, EXAMPLE_MODELS, FIGURE_ORDER, MODEL_DISPLAY  # noqa: E402

# ---------------------------------------------------------------- ordering
DOMAIN_ORDER = DOMAINS
ALL_MODELS = FIGURE_ORDER
TIER_MODELS = EXAMPLE_MODELS
# Panel order of the branching models (DeepSeek V3.2 first: top tree row, left bars).
BRANCH_MODELS = ("deepseek_v32", "gemma3_12b")
assert set(BRANCH_MODELS) == set(BRANCHING_MODELS)

# ---------------------------------------------------------------- colors
# Okabe-Ito framing palette.
FRAMING_STYLE = {
    "threshold": {"color": "#0072B2", "marker": "o", "label": "Threshold"},
    "bet": {"color": "#E69F00", "marker": "s", "label": "Bet"},
    "cost": {"color": "#009E73", "marker": "^", "label": "Cost"},
}
BRANCH_MODEL_COLOR = {"deepseek_v32": "#2F6B9A", "gemma3_12b": "#D97824"}
ACCENT_DARK = "#333333"
REFERENCE_GRAY = "#888888"


def apply_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.labelsize": 11,
            "legend.fontsize": 9.5,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": "0.85",
            "grid.alpha": 0.45,
            "grid.linewidth": 0.8,
            "axes.axisbelow": True,
            "legend.frameon": False,
        }
    )


def model_ticks(ax, models, rotation: float = 40) -> None:
    """Categorical model axis: positions 0..n-1, display names, rotated."""
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels([MODEL_DISPLAY.get(m, m) for m in models], rotation=rotation, ha="right")


def save_figure(fig, stem: str, figures_dir: Path | None = None) -> Path:
    """Write ``<figures>/<stem>.pdf`` (no creation date, so reruns are byte-stable)."""
    path = ensure(Path(figures_dir or FIGURES_DIR)) / f"{stem}.pdf"
    # The paper's PDFs were saved right after a 300-dpi PNG of the same figure.
    # The final layout depends on the state left by earlier draws, so render
    # that PNG (discarded) first to reproduce the paper's layout exactly.
    fig.savefig(io.BytesIO(), format="png", dpi=300, bbox_inches="tight")
    fig.savefig(path, bbox_inches="tight", metadata={"CreationDate": None})
    plt.close(fig)
    print(f"Wrote {path}", flush=True)
    return path


def write_data(frame: pd.DataFrame, name: str, data_dir: Path | None = None) -> Path:
    """Write the tidy data behind a figure to ``<figure_data>/<name>.csv``."""
    path = ensure(Path(data_dir or FIGURE_DATA_DIR)) / f"{name}.csv"
    frame.to_csv(path, index=False)
    return path
