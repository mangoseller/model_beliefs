"""Appendix figure: probability sufficiency measured by pooled out-of-sample R^2.

Per domain x model, pooled R^2 over the fifteen non-probability targets for the
leave-one-output-out posterior (all other outputs) and for the posterior given
the numerical probability alone, with target-bootstrap 95% whiskers (see
:mod:`latent_beliefs.evaluation.probability_sufficiency`).

Input: ``EVALUATION_DIR/probability_sufficiency/pooled_r2.csv``.
"""

from __future__ import annotations

from pathlib import Path

from latent_beliefs.figures import style

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.config import EVALUATION_DIR  # noqa: E402
from latent_beliefs.evaluation.probability_sufficiency import METHOD_DISPLAY, METHOD_ORDER  # noqa: E402

STEM = "figA_probability_sufficiency_r2"
DATA_PATH = EVALUATION_DIR / "probability_sufficiency" / "pooled_r2.csv"
R2_YLIM = (-0.10, 1.0)
METHOD_STYLE = {
    "latent": {"color": "#4A4A4A", "marker": "o", "dodge": -0.10},
    "probability_only": {"color": "#0072B2", "marker": "^", "dodge": 0.10},
}


def draw(summary: pd.DataFrame):
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.6), sharex=True, sharey=True, layout="constrained")
    for ax, domain in zip(axes, style.DOMAIN_ORDER, strict=True):
        domain_rows = summary[summary["domain"] == domain]
        for method in METHOD_ORDER:
            mstyle = METHOD_STYLE[method]
            method_rows = domain_rows[domain_rows["method"] == method].set_index("model_key")
            x = np.arange(len(style.ALL_MODELS), dtype=float) + mstyle["dodge"]
            y = np.array([method_rows.loc[m, "pooled_r2"] for m in style.ALL_MODELS])
            lower = np.array([method_rows.loc[m, "ci_lower"] for m in style.ALL_MODELS])
            upper = np.array([method_rows.loc[m, "ci_upper"] for m in style.ALL_MODELS])
            ax.errorbar(
                x,
                y,
                yerr=np.vstack([y - lower, upper - y]),
                marker=mstyle["marker"],
                markersize=6.2,
                markerfacecolor=mstyle["color"],
                markeredgecolor="white",
                markeredgewidth=0.7,
                color=mstyle["color"],
                ecolor=mstyle["color"],
                elinewidth=1.1,
                capsize=2.0,
                linestyle="none",
                zorder=3,
                label=METHOD_DISPLAY[method],
            )
        ax.axhline(0.0, color=style.REFERENCE_GRAY, linestyle="--", linewidth=0.9, alpha=0.7, zorder=1)
        ax.set_title(style.DOMAIN_DISPLAY[domain])
        ax.set_xlim(-0.6, len(style.ALL_MODELS) - 0.4)
        ax.set_ylim(*R2_YLIM)
        style.model_ticks(ax, style.ALL_MODELS)

    axes[0].set_ylabel("Pooled out-of-sample $R^2$")
    handles, labels = axes[-1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=2, frameon=False)
    return fig


def render(data_path: Path = DATA_PATH) -> None:
    style.apply_style()
    summary = pd.read_csv(data_path, float_precision="round_trip")
    style.save_figure(draw(summary), STEM)
    style.write_data(summary, STEM)


if __name__ == "__main__":
    render()
