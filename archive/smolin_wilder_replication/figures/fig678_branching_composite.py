"""Paper Figure 6: reasoning-prefix branching (panels a-c).

a. Branching trees for six Werewolf cases per model: root = mean of the 25
   elicited probabilities, prefix nodes = mean of each prefix's five answers,
   leaves = the raw answers (fixed x-stagger, no jitter).  Cases: drop those
   whose pooled mean probability (both models) is above 0.9 or below 0.1, rank
   the rest by the two-model mean prefix ICC (ties by instance ID), and take
   ranks ``round(linspace(0, n - 1, 6))``, ascending left to right.
b. Mean within-case prefix ICC with 95% case-bootstrap intervals.
c. Mean task prediction c-index for the same reasoning prefix (solid) and for a
   different prefix (hatched; mean and 2.5/97.5 percentiles over random
   reassignments), bars anchored at the chance level 0.5.

Inputs: ``PARSED_DIR/branching/probability/probability_responses.csv``,
``EVALUATION_DIR/branching_icc/icc_within_instance{,_summary}.csv``,
``EVALUATION_DIR/branching_prediction/branch_latent_cindex_summary.csv``.
Writes one data CSV per panel: ``fig678_branching_composite_{a_examples,b_icc,c_prediction}.csv``.
"""

from __future__ import annotations

from pathlib import Path

from archive.smolin_wilder_replication.figures import style

import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from archive.smolin_wilder_replication.branching.design import BRANCHING_PARSED_DIR  # noqa: E402
from archive.smolin_wilder_replication.config import EVALUATION_DIR  # noqa: E402
from archive.smolin_wilder_replication.models import MODEL_DISPLAY  # noqa: E402

STEM = "fig678_branching_composite"
RESPONSES_PATH = BRANCHING_PARSED_DIR / "probability" / "probability_responses.csv"
ICC_DIR = EVALUATION_DIR / "branching_icc"
PREDICTION_PATH = EVALUATION_DIR / "branching_prediction" / "branch_latent_cindex_summary.csv"
MODELS = style.BRANCH_MODELS

# Panel a
TREE_DOMAIN = "werewolf"
N_EXAMPLES = 6
PREFIX_COLORS = [plt.get_cmap("tab10")(i) for i in range(5)]
ROOT_COLOR = "#404040"
# Band-local x offsets (band i spans [i - 0.5, i + 0.5]).
ROOT_OFF = 0.10
PREFIX_OFF = 0.50
LEAF_OFFS = np.linspace(0.84, 0.92, 5)

# Panel c
BAR_WIDTH = 0.18
# Bar centers within a domain group: [first same, first diff, second same, second diff].
BAR_OFFSETS = (-0.29, -0.10, 0.10, 0.29)
DIFF_ALPHA = 0.45
DIFF_HATCH = "///"
C_BOTTOM = 0.5


def select_instances(resp: pd.DataFrame, icc_path: Path) -> pd.DataFrame:
    pooled = resp.groupby("instance_id")["probability"].mean()
    eligible = pooled[(pooled <= 0.9) & (pooled >= 0.1)].index
    icc = pd.read_csv(icc_path)
    icc = icc[(icc["domain"] == TREE_DOMAIN) & (icc["model_alias"].isin(MODELS)) & (icc["instance_id"].isin(eligible))]
    mean_icc = (
        icc.groupby("instance_id", as_index=False)["prefix_icc"]
        .mean()
        .sort_values(["prefix_icc", "instance_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    ranks = np.round(np.linspace(0, len(mean_icc) - 1, N_EXAMPLES)).astype(int)
    chosen = mean_icc.iloc[ranks].reset_index(drop=True)
    chosen["selection_rank"] = np.arange(1, N_EXAMPLES + 1)
    return chosen[["instance_id", "prefix_icc", "selection_rank"]]


def load_tree_data(responses_path: Path, icc_path: Path):
    """Return the selected cases' responses and their case -> rank map."""
    resp = pd.read_csv(responses_path)
    resp = resp[
        (resp["domain"] == TREE_DOMAIN)
        & (resp["model_alias"].isin(MODELS))
        & (resp["parse_ok"] == 1)
        & resp["probability"].notna()
    ].copy()
    chosen = select_instances(resp, icc_path)
    order = dict(zip(chosen["instance_id"], chosen["selection_rank"]))
    resp = resp[resp["instance_id"].isin(order)].copy()
    resp["selection_rank"] = resp["instance_id"].map(order)
    return resp, order


def draw_tree_row(ax, resp: pd.DataFrame, order: dict, model: str, show_xticklabels: bool) -> list[dict]:
    """Draw one model's branching-tree row; return the plotted leaves."""
    tidy_rows: list[dict] = []
    sub = resp[resp["model_alias"] == model]
    for inst_id, rank in order.items():
        bx = (rank - 1) - 0.5  # band start
        inst = sub[sub["instance_id"] == inst_id]
        root_y = inst["probability"].mean()
        root_x = bx + ROOT_OFF
        for k in range(5):
            grp = inst[inst["prefix_replicate"] == k + 1].sort_values("probability_replicate")
            if grp.empty:
                continue
            color = PREFIX_COLORS[k]
            px = bx + PREFIX_OFF
            py = grp["probability"].mean()
            ax.plot([root_x, px], [root_y, py], color=color, alpha=0.55, linewidth=1.0, zorder=2)
            for j, (_, row) in enumerate(grp.iterrows()):
                lx = bx + LEAF_OFFS[j]
                ly = row["probability"]
                ax.plot([px, lx], [py, ly], color=color, alpha=0.55, linewidth=1.0, zorder=2)
                ax.scatter([lx], [ly], s=14, color=color, alpha=0.8, linewidths=0, zorder=3)
                tidy_rows.append(
                    {
                        "model_alias": model,
                        "domain": TREE_DOMAIN,
                        "instance_id": inst_id,
                        "prefix_index": k + 1,
                        "replicate_index": int(row["probability_replicate"]),
                        "probability": ly,
                        "selection_rank": rank,
                    }
                )
            ax.scatter([px], [py], s=45, color=color, edgecolors="white", linewidths=0.9, zorder=4)
        ax.scatter([root_x], [root_y], s=70, color=ROOT_COLOR, edgecolors="white", linewidths=1.0, zorder=5)
    ax.set_title(MODEL_DISPLAY[model])
    ax.set_ylabel("Elicited probability")
    ax.set_ylim(-0.03, 1.03)
    ax.set_xlim(-0.55, N_EXAMPLES - 0.45)
    ax.set_xticks(range(N_EXAMPLES))
    if show_xticklabels:
        ax.set_xticklabels([f"Case {i + 1}" for i in range(N_EXAMPLES)])
    else:
        ax.set_xticklabels([])
    return tidy_rows


# Panel b
def draw_icc_panel(ax, summary_path: Path) -> list[dict]:
    df = pd.read_csv(summary_path)
    df = df[df["model_alias"].isin(MODELS)].copy()
    width = 0.32
    x = np.arange(len(style.DOMAIN_ORDER))
    tidy = []
    for j, model in enumerate(MODELS):
        rows = df.set_index(["model_alias", "domain"])
        means = [rows.loc[(model, d), "mean_instance_prefix_icc"] for d in style.DOMAIN_ORDER]
        lo = [rows.loc[(model, d), "mean_instance_prefix_icc_ci95_low"] for d in style.DOMAIN_ORDER]
        hi = [rows.loc[(model, d), "mean_instance_prefix_icc_ci95_high"] for d in style.DOMAIN_ORDER]
        yerr = np.vstack([np.array(means) - np.array(lo), np.array(hi) - np.array(means)])
        ax.bar(
            x + (j - 0.5) * width,
            means,
            width=width,
            color=style.BRANCH_MODEL_COLOR[model],
            edgecolor="white",
            linewidth=1.0,
            label=MODEL_DISPLAY[model],
            yerr=yerr,
            ecolor=style.ACCENT_DARK,
            capsize=3,
            error_kw={"linewidth": 1.1},
        )
        for d, m, l, h in zip(style.DOMAIN_ORDER, means, lo, hi):
            tidy.append(
                {
                    "model_alias": model,
                    "domain": d,
                    "mean_instance_prefix_icc": m,
                    "mean_instance_prefix_icc_ci95_low": l,
                    "mean_instance_prefix_icc_ci95_high": h,
                }
            )
    ax.set_xticks(x)
    ax.set_xticklabels([style.DOMAIN_DISPLAY[d] for d in style.DOMAIN_ORDER])
    ax.set_ylabel("Intraclass correlation")
    ax.set_ylim(0, 1)
    ax.legend(loc="upper left")
    return tidy


# Panel c
PREDICTION_COLUMNS = (
    "own_branch_mean_task_c_index",
    "random_other_replicate_mean_task_c_index_mean",
    "random_other_replicate_mean_task_c_index_p025",
    "random_other_replicate_mean_task_c_index_p975",
)


def draw_prediction_bars(ax, prediction_path: Path, bottom: float) -> pd.DataFrame:
    df = pd.read_csv(prediction_path).set_index(["model_alias", "domain"])
    x = np.arange(len(style.DOMAIN_ORDER))
    for mi, model in enumerate(MODELS):
        color = style.BRANCH_MODEL_COLOR[model]
        own, other, p025, p975 = (
            np.array([df.loc[(model, d), column] for d in style.DOMAIN_ORDER]) for column in PREDICTION_COLUMNS
        )
        ax.bar(
            x + BAR_OFFSETS[2 * mi],
            own - bottom,
            width=BAR_WIDTH,
            bottom=bottom,
            color=color,
            edgecolor="white",
            linewidth=1.0,
        )
        x_diff = x + BAR_OFFSETS[2 * mi + 1]
        ax.bar(
            x_diff,
            other - bottom,
            width=BAR_WIDTH,
            bottom=bottom,
            color=color,
            alpha=DIFF_ALPHA,
            hatch=DIFF_HATCH,
            edgecolor="white",
            linewidth=1.0,
        )
        ax.errorbar(
            x_diff,
            other,
            yerr=np.vstack([other - p025, p975 - other]),
            fmt="none",
            ecolor=style.ACCENT_DARK,
            elinewidth=1.1,
            capsize=2.5,
        )

    ax.set_xticks(x)
    ax.set_xticklabels([style.DOMAIN_DISPLAY[d] for d in style.DOMAIN_ORDER])
    ax.tick_params(axis="x", length=0)
    ax.set_ylabel("Mean task prediction c-index")
    ax.set_ylim(bottom, 1.0)
    style_handles = [
        mpatches.Patch(facecolor=style.REFERENCE_GRAY, edgecolor="white", linewidth=1.0, label="Same reasoning prefix"),
        mpatches.Patch(
            facecolor=style.REFERENCE_GRAY,
            alpha=DIFF_ALPHA,
            hatch=DIFF_HATCH,
            edgecolor="white",
            linewidth=1.0,
            label="Different reasoning prefix",
        ),
    ]
    # Above the axes: the bars span the whole panel interior.
    ax.legend(
        handles=style_handles,
        loc="lower right",
        bbox_to_anchor=(1.0, 1.0),
        ncols=2,
        columnspacing=1.4,
        handlelength=1.4,
    )
    return pd.DataFrame(
        [
            {"model_alias": model, "domain": domain, **{c: df.loc[(model, domain), c] for c in PREDICTION_COLUMNS}}
            for domain in style.DOMAIN_ORDER
            for model in MODELS
        ]
    )


def place_panel_labels(fig, items) -> None:
    """Bold panel letters flush with each panel's leftmost extent (after layout)."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    inv = fig.transFigure.inverted()
    for ax, letter in items:
        x0 = inv.transform((ax.get_tightbbox(renderer).x0, 0))[0]
        fig.text(x0, ax.get_position().y1 + 0.006, letter, fontsize=15, fontweight="bold", va="bottom", ha="left")


def render(
    responses_path: Path = RESPONSES_PATH,
    icc_dir: Path = ICC_DIR,
    prediction_path: Path = PREDICTION_PATH,
) -> None:
    style.apply_style()
    resp, order = load_tree_data(responses_path, icc_dir / "icc_within_instance.csv")

    fig = plt.figure(figsize=(12.5, 10), layout="constrained")
    gs = fig.add_gridspec(3, 2, height_ratios=[1.0, 1.0, 1.35])
    ax_t1 = fig.add_subplot(gs[0, :])
    ax_t2 = fig.add_subplot(gs[1, :], sharex=ax_t1, sharey=ax_t1)
    ax_b = fig.add_subplot(gs[2, 0])
    ax_c = fig.add_subplot(gs[2, 1])

    leaves = draw_tree_row(ax_t1, resp, order, MODELS[0], show_xticklabels=False)
    leaves += draw_tree_row(ax_t2, resp, order, MODELS[1], show_xticklabels=True)
    # Manually shared axes do not hide the upper tick labels automatically.
    ax_t1.tick_params(labelbottom=False)
    icc = draw_icc_panel(ax_b, icc_dir / "icc_within_instance_summary.csv")
    prediction = draw_prediction_bars(ax_c, prediction_path, bottom=C_BOTTOM)
    place_panel_labels(fig, [(ax_t1, "a"), (ax_b, "b"), (ax_c, "c")])
    style.save_figure(fig, STEM)

    leaves = pd.DataFrame(leaves).sort_values(["model_alias", "selection_rank", "prefix_index", "replicate_index"])
    style.write_data(leaves, f"{STEM}_a_examples")
    style.write_data(pd.DataFrame(icc), f"{STEM}_b_icc")
    style.write_data(prediction, f"{STEM}_c_prediction")


if __name__ == "__main__":
    render()
