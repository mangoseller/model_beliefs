"""Shared drawing for the domain/framing holdout figures (paper Figure 2 and its appendix variant).

Rows show mean AUROC and pooled out-of-sample R^2; columns are the three
domains.  Within every model, four color/shape-coded estimates vary which
domain and framing information was available when fitting the latent model.
Whiskers are prompt-bootstrap 95% intervals.

Input: ``holdout_information_model_summary.csv`` of a holdout-grid evaluation.
"""

from __future__ import annotations

from pathlib import Path

from latent_beliefs.figures import style

import matplotlib.lines as mlines  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.models import MODEL_DISPLAY  # noqa: E402

REGIME_ORDER = (
    "domain_in_family_in",
    "domain_in_family_out",
    "domain_out_family_in",
    "domain_out_family_out",
)
REGIME_STYLE = {
    "domain_in_family_in": {"label": "Domain in, framing in", "color": "#0072B2", "marker": "o", "dodge": -0.24},
    "domain_in_family_out": {"label": "Domain in, framing out", "color": "#E69F00", "marker": "s", "dodge": -0.08},
    "domain_out_family_in": {"label": "Domain out, framing in", "color": "#009E73", "marker": "^", "dodge": 0.08},
    "domain_out_family_out": {"label": "Domain out, framing out", "color": "#CC79A7", "marker": "D", "dodge": 0.24},
}
C_INDEX_YLIM = (0.50, 1.00)
R2_YLIM = (-1.10, 1.00)


def load_data(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path)
    required = {
        "domain",
        "model_key",
        "regime",
        "mean_c_index",
        "mean_c_index_ci_lower",
        "mean_c_index_ci_upper",
        "pooled_r2",
        "pooled_r2_ci_lower",
        "pooled_r2_ci_upper",
    }
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"missing holdout-grid columns: {sorted(missing)}")

    data = data[
        data["domain"].isin(style.DOMAIN_ORDER)
        & data["model_key"].isin(style.ALL_MODELS)
        & data["regime"].isin(REGIME_ORDER)
    ].copy()
    expected = len(style.DOMAIN_ORDER) * len(style.ALL_MODELS) * len(REGIME_ORDER)
    if len(data) != expected:
        raise ValueError(f"holdout grid has {len(data)} rows; expected {expected}")
    counts = data.groupby(["domain", "model_key"], sort=False)["regime"].nunique()
    if not counts.eq(len(REGIME_ORDER)).all():
        raise ValueError("holdout grid does not have all four regimes in every cell")

    data["domain_display"] = data["domain"].map(style.DOMAIN_DISPLAY)
    data["model_display"] = data["model_key"].map(MODEL_DISPLAY)
    data["model_position"] = data["model_key"].map({model: index for index, model in enumerate(style.ALL_MODELS)})
    data["regime_label"] = data["regime"].map({key: s["label"] for key, s in REGIME_STYLE.items()})
    data["regime_dodge"] = data["regime"].map({key: s["dodge"] for key, s in REGIME_STYLE.items()})
    return data.sort_values(["domain", "model_position", "regime"], kind="stable").reset_index(drop=True)


def _draw_regime(ax, panel: pd.DataFrame, regime: str, metric: str) -> None:
    rstyle = REGIME_STYLE[regime]
    rows = panel[panel["regime"].eq(regime)].set_index("model_key")
    x = np.arange(len(style.ALL_MODELS), dtype=float) + float(rstyle["dodge"])
    values = np.array([rows.loc[model, metric] for model in style.ALL_MODELS], dtype=float)
    lower = np.array([rows.loc[model, f"{metric}_ci_lower"] for model in style.ALL_MODELS], dtype=float)
    upper = np.array([rows.loc[model, f"{metric}_ci_upper"] for model in style.ALL_MODELS], dtype=float)
    limits = C_INDEX_YLIM if metric == "mean_c_index" else R2_YLIM
    draw_values = np.clip(values, *limits)
    draw_lower = np.minimum(np.clip(lower, *limits), draw_values)
    draw_upper = np.maximum(np.clip(upper, *limits), draw_values)
    ax.errorbar(
        x,
        draw_values,
        yerr=np.vstack([draw_values - draw_lower, draw_upper - draw_values]),
        fmt=rstyle["marker"],
        markersize=5.2,
        markerfacecolor=rstyle["color"],
        markeredgecolor="white",
        markeredgewidth=0.65,
        color=rstyle["color"],
        ecolor=rstyle["color"],
        elinewidth=1.0,
        capsize=1.8,
        linestyle="none",
        zorder=3,
    )


def draw(data: pd.DataFrame):
    fig, axes = plt.subplots(2, 3, figsize=(13.5, 7.5), sharex="col", sharey="row", layout="constrained")
    for column, domain in enumerate(style.DOMAIN_ORDER):
        panel = data[data["domain"].eq(domain)]
        for row, metric in enumerate(("mean_c_index", "pooled_r2")):
            ax = axes[row, column]
            for regime in REGIME_ORDER:
                _draw_regime(ax, panel, regime, metric)
            ax.set_xlim(-0.6, len(style.ALL_MODELS) - 0.4)
            if metric == "mean_c_index":
                ax.set_ylim(*C_INDEX_YLIM)
                ax.set_title(style.DOMAIN_DISPLAY[domain])
            else:
                ax.axhline(0.0, color=style.REFERENCE_GRAY, linestyle="--", linewidth=0.9, alpha=0.7, zorder=1)
                ax.set_ylim(*R2_YLIM)
                style.model_ticks(ax, style.ALL_MODELS)

    axes[0, 0].set_ylabel("Mean AUROC")
    axes[1, 0].set_ylabel("Pooled out-of-sample $R^2$")
    handles = [
        mlines.Line2D(
            [],
            [],
            color=s["color"],
            marker=s["marker"],
            linestyle="none",
            markersize=6,
            markeredgecolor="white",
            markeredgewidth=0.65,
            label=s["label"],
        )
        for s in REGIME_STYLE.values()
    ]
    fig.legend(handles=handles, loc="outside lower center", ncols=4, columnspacing=1.5, handletextpad=0.4)
    return fig


def render(stem: str, path: Path) -> None:
    data = load_data(path)
    style.write_data(data, stem)
    style.apply_style()
    style.save_figure(draw(data), stem)
