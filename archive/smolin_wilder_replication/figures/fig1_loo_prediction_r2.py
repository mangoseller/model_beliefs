"""Paper Figure 1: reciprocal prediction between probability reports and decisions.

Top row: mean AUROC/concordance over target prompts; bottom row: pooled
out-of-sample R^2.  Faint points are individual target prompts; whiskers are
the prompt-bootstrap 95% intervals of :mod:`archive.smolin_wilder_replication.evaluation.reciprocal`.

Input: ``EVALUATION_DIR/reciprocal/{metrics_by_target,model_summary}.csv``.
"""

from __future__ import annotations

from pathlib import Path

from archive.smolin_wilder_replication.figures import style

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from archive.smolin_wilder_replication.config import EVALUATION_DIR  # noqa: E402

STEM = "fig1_loo_prediction_r2"
DATA_DIR = EVALUATION_DIR / "reciprocal"
CONDITION_ORDER = ("reports_to_decisions", "decisions_to_reports")
CONDITION_STYLE = {
    "reports_to_decisions": {
        "color": "#2F6B9A",
        "marker": "o",
        "label": "Probability reports $\\rightarrow$ decisions",
    },
    "decisions_to_reports": {
        "color": "#D97824",
        "marker": "s",
        "label": "Decisions $\\rightarrow$ probability reports",
    },
}
CINDEX_YLIM = (0.35, 1.00)
R2_YLIM = (-0.40, 1.00)
JITTER_SEED = 20260825
JITTER_SD = 0.055


def load_data(data_dir: Path = DATA_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    metrics = pd.read_csv(data_dir / "metrics_by_target.csv")
    summary = pd.read_csv(data_dir / "model_summary.csv")
    return (
        metrics[metrics["model_key"].isin(style.ALL_MODELS)].copy(),
        summary[summary["model_key"].isin(style.ALL_MODELS)].copy(),
    )


def plotted_data(metrics: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame:
    """One tidy table containing every target and summary mark."""
    identity = ["domain", "domain_display", "model_key", "model_display", "model_position", "condition", "condition_label"]
    targets = metrics[identity + ["output_name", "output_kind", "c_index", "r2_vs_best_test_constant"]].copy()
    targets.insert(len(identity), "series", "target_prompt")
    targets["c_index_ci_lower"] = np.nan
    targets["c_index_ci_upper"] = np.nan
    targets["r2_ci_lower"] = np.nan
    targets["r2_ci_upper"] = np.nan

    summaries = summary[
        identity
        + [
            "mean_c_index",
            "mean_c_index_ci_lower",
            "mean_c_index_ci_upper",
            "pooled_r2",
            "pooled_r2_ci_lower",
            "pooled_r2_ci_upper",
        ]
    ].copy()
    summaries.insert(len(identity), "series", "summary")
    summaries["output_name"] = ""
    summaries["output_kind"] = ""
    summaries["c_index"] = summaries.pop("mean_c_index")
    summaries["r2_vs_best_test_constant"] = summaries.pop("pooled_r2")
    summaries["c_index_ci_lower"] = summaries.pop("mean_c_index_ci_lower")
    summaries["c_index_ci_upper"] = summaries.pop("mean_c_index_ci_upper")
    summaries["r2_ci_lower"] = summaries.pop("pooled_r2_ci_lower")
    summaries["r2_ci_upper"] = summaries.pop("pooled_r2_ci_upper")
    return pd.concat([targets, summaries], ignore_index=True, sort=False).sort_values(
        ["domain", "model_position", "condition", "series", "output_name"]
    )


def draw(metrics: pd.DataFrame, summary: pd.DataFrame):
    """Overlay both reciprocal directions in one two-metric figure."""
    fig, axes = plt.subplots(2, 3, figsize=(13.5, 7.5), sharex="col", sharey="row", layout="constrained")
    positions = np.arange(len(style.ALL_MODELS), dtype=float)
    model_position = {model: index for index, model in enumerate(style.ALL_MODELS)}
    offsets = {"reports_to_decisions": -0.12, "decisions_to_reports": 0.12}
    rng = np.random.default_rng(JITTER_SEED)
    metric_rows = (
        ("c_index", "mean_c_index", CINDEX_YLIM),
        ("r2_vs_best_test_constant", "pooled_r2", R2_YLIM),
    )

    legend_handles = []
    for row, (metric, summary_metric, limits) in enumerate(metric_rows):
        for column, domain in enumerate(style.DOMAIN_ORDER):
            axis = axes[row, column]
            for condition in CONDITION_ORDER:
                cstyle = CONDITION_STYLE[condition]
                offset = offsets[condition]
                panel = metrics[metrics["condition"].eq(condition) & metrics["domain"].eq(domain)]
                finite = panel[np.isfinite(panel[metric])]
                x = finite["model_key"].map(model_position).to_numpy(dtype=float)
                x += offset + rng.normal(0.0, JITTER_SD, size=len(x))
                axis.scatter(x, finite[metric], s=12, alpha=0.18, color=cstyle["color"], linewidths=0, zorder=2)

                cell_summary = (
                    summary[summary["condition"].eq(condition) & summary["domain"].eq(domain)]
                    .set_index("model_key")
                    .loc[list(style.ALL_MODELS)]
                )
                means = cell_summary[summary_metric].to_numpy(dtype=float)
                intervals = cell_summary[[f"{summary_metric}_ci_lower", f"{summary_metric}_ci_upper"]].to_numpy(
                    dtype=float
                )
                interval_lower = np.maximum(intervals[:, 0], limits[0])
                interval_upper = np.minimum(intervals[:, 1], limits[1])
                axis.errorbar(
                    positions + offset,
                    means,
                    yerr=np.vstack(
                        [np.maximum(0.0, means - interval_lower), np.maximum(0.0, interval_upper - means)]
                    ),
                    fmt="none",
                    ecolor=cstyle["color"],
                    elinewidth=1.0,
                    capsize=2.0,
                    capthick=1.0,
                    alpha=0.9,
                    zorder=3,
                )
                line = axis.plot(positions + offset, means, color=cstyle["color"], lw=1.2, alpha=0.75, zorder=3)[0]
                axis.scatter(
                    positions + offset,
                    means,
                    s=58,
                    color=cstyle["color"],
                    marker=cstyle["marker"],
                    edgecolors="white",
                    linewidths=0.7,
                    zorder=4,
                )
                if row == 0 and column == 0:
                    line.set_label(cstyle["label"])
                    legend_handles.append(line)

            axis.set_xlim(-0.6, len(style.ALL_MODELS) - 0.4)
            axis.set_ylim(*limits)
            axis.axhline(
                0.5 if metric == "c_index" else 0.0,
                color=style.REFERENCE_GRAY,
                linestyle="--",
                linewidth=0.9,
                alpha=0.7,
                zorder=1,
            )
            if row == 0:
                axis.set_title(style.DOMAIN_DISPLAY[domain])
            else:
                style.model_ticks(axis, style.ALL_MODELS)

    axes[0, 0].set_ylabel("Mean AUROC / concordance")
    axes[1, 0].set_ylabel("Pooled out-of-sample $R^2$")
    fig.legend(handles=legend_handles, loc="outside upper center", ncol=2, handlelength=2.4, columnspacing=2.0)
    return fig


def render(data_dir: Path = DATA_DIR) -> None:
    style.apply_style()
    metrics, summary = load_data(data_dir)
    style.save_figure(draw(metrics, summary), STEM)
    style.write_data(plotted_data(metrics, summary), STEM)


if __name__ == "__main__":
    render()
