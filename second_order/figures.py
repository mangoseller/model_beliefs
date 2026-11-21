"""Figures of the curse-of-knowledge experiment, regenerated from ``results/curse/`` only (no model calls).

``curse_estimates`` (lambda, phi and tau per model, dataset, method and setting, with
Camerer et al.'s w), ``curse_scatter`` (the two regressions behind lambda) and
``retention``.  The style helpers here are shared with the deprecated RQ figures
(``second_order.deprecated.rq_figures``).

    python -m second_order.figures
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from second_order import config as C  # noqa: E402
from second_order.report import display  # noqa: E402

INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
CONDITION_ORDER = ["A", "B", "B_F", "D_john", "M_S4", "C_john", "D_F", "M_F", "C_F", "D_user", "C_user"]
CONDITION_COLOR = {"A": "#52514e", "B": "#8a8984", "B_F": "#b8b7b2", "D_john": "#7fb0eb", "M_S4": "#1baf7a",
                   "C_john": "#2a78d6", "D_F": "#f3a37f", "M_F": "#7ccfae", "C_F": "#eb6834",
                   "D_user": "#f9cdb8", "C_user": "#c4521f"}
PAIR_COLOR = {"S4": "#2a78d6", "S1": "#7fb0eb", "H": "#1baf7a", "F": "#eb6834"}
SETTING_COLOR = {"partial": PAIR_COLOR["S4"], "false": PAIR_COLOR["F"]}


def style(ax, title=None):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, fontsize=9, color=INK, loc="left")


def save(fig, name):
    C.FIGURES.mkdir(parents=True, exist_ok=True)
    fig.savefig(C.FIGURES / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(C.FIGURES / f"{name}.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def read(name):
    path = C.RESULTS / name
    return pd.read_csv(path) if path.exists() and path.stat().st_size > 1 else pd.DataFrame()


def errbar(ax, x, r, color, marker="o", hollow=False, label=None):
    ax.errorbar(x, r.estimate, yerr=[[r.estimate - r.ci_lower], [r.ci_upper - r.estimate]], fmt=marker,
                color=color, mfc="white" if hollow else color, ms=6, elinewidth=1.2, capsize=0, label=label)


def retention(ret):
    if ret.empty:
        return
    # One group of bars per model (and dataset, when the table has several).
    ret = ret.assign(group=[display(m) if not d or d == "ckd" else f"{display(m)} · {d}"
                            for m, d in zip(ret.model, ret.get("dataset", pd.Series("", index=ret.index)))])
    groups = list(ret.group.unique())
    conds = [c for c in dict.fromkeys(CONDITION_ORDER + list(ret.condition)) if c in set(ret.condition)]
    fig, ax = plt.subplots(figsize=(max(4, 1.2 * len(groups) * len(conds) / 4), 3))
    w = 0.8 / len(conds)
    for ci, cond in enumerate(conds):
        d = ret[ret.condition == cond].set_index("group").reindex(groups)
        ax.bar(np.arange(len(groups)) + ci * w, d.retention, width=w * 0.9,
               color=CONDITION_COLOR.get(cond, "#6250d6"), label=cond)
    ax.axhline(0.9, color=INK2, lw=1, ls="--")
    ax.set_xticks(np.arange(len(groups)) + 0.4 - w / 2, groups, fontsize=7)
    ax.set_ylim(0, 1.02)
    ax.set_ylabel("Complete-case retention", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, ncol=4, loc="lower left")
    style(ax, "Retention by model and condition (dashed: 90%)")
    save(fig, "retention")


def curse_estimates(est):
    """lambda, phi and tau with 95% intervals, one row per model, dataset and method."""
    L = est[(est.case_set == "all") & (est.setting != "false_minus_partial")]
    groups = list(L.groupby(["model", "dataset", "method"], sort=False).size().index)
    if not groups:
        return
    w = float(C.load()["experiment"]["camerer_w"])
    fig, axes = plt.subplots(1, 3, figsize=(10, 0.45 * len(groups) + 1.6), sharey=True)
    panels = (("lambda", "Leakage λ (dotted: Camerer et al. w)", (0, w, 1)), ("phi", "Responsiveness φ", (1,)),
              ("tau", "Theory-of-mind score τ = φ(1 − λ)", (1,)))
    for ax, (statistic, title, refs) in zip(axes, panels):
        for ref in refs:
            ax.axvline(ref, color=MUTED, lw=0.8, ls=":" if ref == w and statistic == "lambda" else "-")
        for i, (model, dataset, method) in enumerate(groups):
            for j, setting in enumerate(("partial", "false")):
                r = L[(L.model == model) & (L.dataset == dataset) & (L.method == method) & (L.setting == setting)
                      & (L.statistic == statistic)]
                if r.empty or not np.isfinite(r.estimate.iloc[0]):
                    continue
                r = r.iloc[0]
                ax.errorbar(r.estimate, i + (j - 0.5) * 0.3, xerr=[[r.estimate - r.ci_lower], [r.ci_upper - r.estimate]],
                            fmt="o", color=SETTING_COLOR[setting], ms=5, elinewidth=1.2, capsize=0,
                            label=setting if i == 0 else None)
        style(ax, title)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(range(len(groups)), [f"{display(m)} · {d} · {k}" for m, d, k in groups], fontsize=7)
    axes[0].invert_yaxis()
    axes[0].legend(fontsize=7, frameon=False, loc="lower right")
    save(fig, "curse_estimates")


def curse_scatter(scatter, est):
    """Per model, dataset and method: l_C - l_M and l_D - l_M against x = l_A - l_B, by setting."""
    if scatter.empty:
        return
    groups = list(scatter.groupby(["model", "dataset", "method"], sort=False).size().index)
    fig, axes = plt.subplots(len(groups), 2, figsize=(7.5, 2.8 * len(groups)), squeeze=False)
    for i, (model, dataset, method) in enumerate(groups):
        for j, setting in enumerate(("partial", "false")):
            ax = axes[i][j]
            d = scatter[(scatter.model == model) & (scatter.dataset == dataset) & (scatter.method == method)
                        & (scatter.setting == setting)]
            if len(d) < 3:
                ax.set_visible(False)
                continue
            x = d.x_A_minus_B.to_numpy()
            grid = np.linspace(x.min(), x.max(), 20)
            for column, color, label in (("y_D_minus_M", MUTED, "D − M (slope β_D)"),
                                         ("y_C_minus_M", SETTING_COLOR[setting], "C − M (slope β_C)")):
                ax.scatter(x, d[column], s=6, color=color, alpha=0.5, lw=0)
                slope, intercept = np.polyfit(x, d[column], 1)
                ax.plot(grid, intercept + slope * grid, color=color, lw=1.5, label=f"{label} = {slope:.2f}")
            lam = est[(est.model == model) & (est.dataset == dataset) & (est.method == method)
                      & (est.setting == setting) & (est.statistic == "lambda") & (est.case_set == "all")]
            tag = f", λ = {lam.estimate.iloc[0]:.2f}" if len(lam) and np.isfinite(lam.estimate.iloc[0]) else ""
            style(ax, f"{display(model)} · {dataset} · {method} · {setting}{tag}")
            ax.set_xlabel("l_A − l_B (logits)", fontsize=8, color=INK2)
            ax.legend(fontsize=6.5, frameon=False, loc="upper left")
    save(fig, "curse_scatter")


def main() -> None:
    est = read("curse/estimates.csv")
    if not est.empty:
        curse_estimates(est)
        curse_scatter(read("curse/scatter.csv"), est)
    retention(read("curse/retention.csv"))
    print(f"figures written to {C.FIGURES}")


if __name__ == "__main__":
    main()
