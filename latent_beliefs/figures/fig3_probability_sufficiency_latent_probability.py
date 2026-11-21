"""Paper Figure 4: share of the latent model's predictive signal carried by the stated probability.

Per domain x model: ``(mean_j c_prob,j - 0.5) / (mean_j c_latent,j - 0.5)`` over
the fifteen non-probability targets, with target-bootstrap 95% whiskers (see
:mod:`latent_beliefs.evaluation.probability_sufficiency`).  Drawn whiskers are
clipped to the axis range; the data CSV keeps the raw values.

Input: ``EVALUATION_DIR/probability_sufficiency/concordance_share.csv``.
"""

from __future__ import annotations

from pathlib import Path

from latent_beliefs.figures import style

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.config import EVALUATION_DIR  # noqa: E402

STEM = "fig3_probability_sufficiency_latent_probability"
DATA_PATH = EVALUATION_DIR / "probability_sufficiency" / "concordance_share.csv"
# Okabe-Ito colors plus redundant marker shapes, dodged around each model.
DOMAIN_STYLE = {
    "ckd": dict(marker="o", color="#0072B2", dodge=0.0),
    "werewolf": dict(marker="s", color="#D55E00", dodge=-0.16),
    "forecasting": dict(marker="^", color="#009E73", dodge=0.16),
}


def draw(shares: pd.DataFrame, ylim: tuple[float, float]):
    fig, ax = plt.subplots(figsize=(9.5, 5.2), layout="constrained")
    for domain in style.DOMAIN_ORDER:
        sub = shares[shares["domain"] == domain].set_index("model_key")
        st = DOMAIN_STYLE[domain]
        x = np.arange(len(style.ALL_MODELS), dtype=float) + st["dodge"]
        y = np.array([sub.loc[m, "share_captured"] for m in style.ALL_MODELS])
        lo = np.array([sub.loc[m, "ci_lower"] for m in style.ALL_MODELS])
        hi = np.array([sub.loc[m, "ci_upper"] for m in style.ALL_MODELS])
        y_c = np.clip(y, *ylim)
        lo_c = np.clip(lo, *ylim)
        hi_c = np.clip(hi, *ylim)
        ax.errorbar(
            x,
            y_c,
            yerr=np.vstack([y_c - lo_c, hi_c - y_c]),
            marker=st["marker"],
            markersize=6.5,
            markerfacecolor=st["color"],
            markeredgecolor="white",
            markeredgewidth=0.8,
            color=st["color"],
            ecolor=st["color"],
            elinewidth=1.25,
            capsize=2.2,
            linestyle="none",
            zorder=3,
            label=style.DOMAIN_DISPLAY[domain],
        )

    ax.set_ylim(*ylim)
    ax.set_xlim(-0.7, len(style.ALL_MODELS) - 0.3)
    ax.set_ylabel("Share of predictable signal captured")
    style.model_ticks(ax, style.ALL_MODELS)
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index(style.DOMAIN_DISPLAY[d]) for d in style.DOMAIN_ORDER]
    ax.legend([handles[i] for i in order], [labels[i] for i in order], loc="lower right", frameon=False)
    return fig


def render(data_path: Path = DATA_PATH) -> None:
    style.apply_style()
    shares = pd.read_csv(data_path, float_precision="round_trip")
    # Ratios and their intervals can exceed one; show the full intervals.
    lower = min(0.0, float(shares["ci_lower"].min()) - 0.02)
    upper = max(1.02, float(shares["ci_upper"].max()) + 0.02)
    style.save_figure(draw(shares, (lower, upper)), STEM)
    style.write_data(shares, STEM)


if __name__ == "__main__":
    render()
