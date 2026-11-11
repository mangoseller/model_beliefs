"""The six Phase 5 figures, regenerated from ``results/`` only (no model calls).

    python -m second_order.figures
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from second_order import config as C  # noqa: E402
from second_order.analyze import GEMMA_PARAMS, OPEN_WEIGHT, display  # noqa: E402

INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
FRAMING_COLOR = {"john": "#2a78d6", "user": "#eb6834", "self": "#52514e"}
CONDITION_ORDER = ["A", "B", "D_john", "C_john", "D_user", "C_user"]
CONDITION_COLOR = {"A": "#52514e", "B": "#8a8984", "D_john": "#7fb0eb", "C_john": "#2a78d6",
                   "D_user": "#f3a37f", "C_user": "#eb6834"}


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


def fig1(coh):
    co = coh[(coh.statistic == "mean_c_index") & (coh.direction == "reports_to_decisions")
             & coh.condition.isin(CONDITION_ORDER)]
    if co.empty:
        return
    co = co.assign(variant=co.get("variant", pd.Series(index=co.index, dtype=object)).fillna(co.method))
    variants = sorted(co.variant.unique())
    models = list(co.model.unique())
    fig, axes = plt.subplots(1, len(variants), figsize=(3.4 * len(variants), 3.2), squeeze=False, sharey=True)
    for ax, v in zip(axes[0], variants):
        d = co[co.variant == v]
        for mi, model in enumerate(models):
            for ci, cond in enumerate(CONDITION_ORDER):
                r = d[(d.model == model) & (d.condition == cond)]
                if len(r):
                    errbar(ax, mi + (ci - 2.5) * 0.12, r.iloc[0], CONDITION_COLOR[cond],
                           label=cond if mi == 0 else None)
        ax.set_xticks(range(len(models)), [display(m) for m in models], rotation=30, ha="right", fontsize=7)
        style(ax, v)
    axes[0][0].set_ylabel("Mean c-index, reports → decisions (test)", fontsize=8, color=INK2)
    axes[0][-1].legend(fontsize=7, frameon=False, loc="lower right")
    save(fig, "fig1_coherence")


def headline(leak):
    L = leak[(leak.case_set == "all_complete")]
    if "selected_layer" in L:
        L = L[L.selected_layer.map(lambda v: True if pd.isna(v) else bool(v))]
    return L[(L.method != "latent") | (L.fit == "pooled")]


def fig2(leak):
    L = headline(leak)
    L = L[L.method == "latent"]
    if L.empty:
        return
    floor = float(C.load()["analysis"]["capability_floor"])
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.2), squeeze=False)
    for ax, framing in zip(axes[0][:2], ("john", "user")):
        for _, g in L.groupby("model"):
            cap = g.capability_condition_A_c_index.iloc[0]
            hollow = cap < floor
            s = g.set_index("statistic")
            if f"lambda_C_{framing}" not in s.index:
                continue
            errbar(ax, cap, s.loc[f"normalised_lambda_{framing}"], FRAMING_COLOR[framing], hollow=hollow)
            ax.plot(cap, s.loc[f"lambda_C_{framing}"].estimate, "s", color=FRAMING_COLOR[framing], ms=3.5)
            ax.plot(cap, s.loc[f"lambda_D_{framing}"].estimate, "_", color=INK2, ms=12, mew=2)
            ax.plot(cap, s.loc["lambda_0"].estimate, "x", color=MUTED, ms=6)
            ax.annotate(display(g.model.iloc[0]), (cap, s.loc[f"lambda_C_{framing}"].estimate),
                        fontsize=6, color=INK2, xytext=(4, 2), textcoords="offset points")
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_xlabel("Condition A c-index (reports → decisions)", fontsize=8, color=INK2)
        style(ax, f"Latent, {framing}")
    ax = axes[0][2]
    for framing in ("john", "user"):
        g = L[L.model.isin(GEMMA_PARAMS) & (L.statistic == f"normalised_lambda_{framing}")]
        for _, r in g.iterrows():
            errbar(ax, GEMMA_PARAMS[r.model] * (1.05 if framing == "user" else 1), r, FRAMING_COLOR[framing],
                   label=framing)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(dict(zip(labels, handles)).values(), dict(zip(labels, handles)).keys(), fontsize=7, frameon=False)
    ax.set_xscale("log")
    ax.set_xticks([4, 12, 27], ["4", "12", "27"])
    ax.minorticks_off()
    ax.set_xlim(3, 36)
    ax.set_xlabel("Gemma 3 parameters (B)", fontsize=8, color=INK2)
    style(ax, "Latent normalised λ by Gemma size")
    fig.text(0.01, -0.04, "Circle with 95% interval: normalised λ = λ_C / λ_D (hollow: condition A c-index below the "
             "capability floor).  Square: raw λ_C.  Bar: λ_D.  ×: λ_0.", fontsize=7, color=INK2)
    axes[0][0].set_ylabel("λ (slope)", fontsize=8, color=INK2)
    save(fig, "fig2_lambda_vs_capability")


def fig3(scatter, leak):
    if scatter.empty:
        return
    models = list(scatter.model.unique())
    fig, axes = plt.subplots(len(models), 2, figsize=(7, 3 * len(models)), squeeze=False)
    L = headline(leak)
    for i, model in enumerate(models):
        for j, framing in enumerate(("john", "user")):
            ax = axes[i][j]
            d = scatter[(scatter.model == model) & (scatter.framing == framing)]
            if d.empty:
                continue
            ax.scatter(d.x_A_minus_Bprob, d.y_D_minus_Bdec, s=8, color=MUTED, alpha=0.6, label="D", lw=0)
            ax.scatter(d.x_A_minus_Bprob, d.y_C_minus_Bdec, s=8, color=FRAMING_COLOR[framing], alpha=0.7,
                       label="C", lw=0)
            s = L[(L.model == model) & (L.method == "latent")].set_index("statistic")
            xs = np.linspace(d.x_A_minus_Bprob.min(), d.x_A_minus_Bprob.max(), 10)
            for cond, color in (("C", FRAMING_COLOR[framing]), ("D", INK2)):
                if f"lambda_{cond}_{framing}" in s.index:
                    ax.plot(xs, s.loc[f"c_{cond}_{framing}"].estimate + s.loc[f"lambda_{cond}_{framing}"].estimate * xs,
                            color=color, lw=2)
            ax.set_xlabel("ℓ_A − ℓ_B (probability reports)", fontsize=8, color=INK2)
            ax.set_ylabel("ℓ − ℓ_B (decisions), calibrated", fontsize=8, color=INK2)
            ax.legend(fontsize=7, frameon=False)
            style(ax, f"{display(model)}, {framing}")
    save(fig, "fig3_scatter")


def fig4(ret):
    if ret.empty:
        return
    models = list(ret.model.unique())
    conds = [c for c in CONDITION_ORDER + ["E_self", "E_weak", "E_strong"] if c in set(ret.condition)]
    fig, ax = plt.subplots(figsize=(max(4, 1.2 * len(models) * len(conds) / 4), 3))
    w = 0.8 / len(conds)
    for ci, cond in enumerate(conds):
        d = ret[ret.condition == cond].set_index("model").reindex(models)
        ax.bar(np.arange(len(models)) + ci * w, d.retention, width=w * 0.9,
               color=CONDITION_COLOR.get(cond, "#6250d6"), label=cond)
    ax.axhline(0.9, color=INK2, lw=1, ls="--")
    ax.set_xticks(np.arange(len(models)) + 0.4 - w / 2, [display(m) for m in models], fontsize=7)
    ax.set_ylim(0, 1.02)
    ax.set_ylabel("Complete-case retention", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, ncol=3, loc="lower left")
    style(ax, "Retention by model and condition (dashed: 90%)")
    save(fig, "fig4_retention")


def fig5(leak):
    L = headline(leak)
    L = L[L.model.isin(OPEN_WEIGHT) & L.statistic.isin(["lambda_C_john", "lambda_C_user",
                                                         "normalised_lambda_john", "normalised_lambda_user"])]
    if L.empty:
        return
    L = L.assign(label=L.apply(lambda r: r.method if r.method != "probe" else f"probe {r.probe_type} {r.position}",
                               axis=1))
    models = list(L.model.unique())
    fig, axes = plt.subplots(1, len(models), figsize=(3.6 * len(models), 3), squeeze=False, sharey=True)
    for ax, model in zip(axes[0], models):
        d = L[L.model == model]
        labels = sorted(d.label.unique())
        for li, lab in enumerate(labels):
            for framing, off in (("john", -0.12), ("user", 0.12)):
                r = d[(d.label == lab) & (d.statistic == f"normalised_lambda_{framing}")]
                if len(r):
                    errbar(ax, li + off, r.iloc[0], FRAMING_COLOR[framing], label=framing if li == 0 else None)
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_xticks(range(len(labels)), labels, rotation=30, ha="right", fontsize=7)
        style(ax, display(model))
    axes[0][0].set_ylabel("Normalised λ = λ_C / λ_D", fontsize=8, color=INK2)
    axes[0][-1].legend(fontsize=7, frameon=False)
    save(fig, "fig5_lambda_by_method")


def fig6(leak):
    L = leak[(leak.method == "probe") & (leak.case_set == "all_complete") & (leak.probe_type == "truth")
             & leak.statistic.isin(["normalised_lambda_john", "normalised_lambda_user"])] if "probe_type" in leak else pd.DataFrame()
    if L.empty:
        return
    models = list(L.model.unique())
    positions = sorted(L.position.unique())
    fig, axes = plt.subplots(len(models), len(positions), figsize=(3.6 * len(positions), 2.8 * len(models)),
                             squeeze=False, sharey=True)
    for i, model in enumerate(models):
        for j, pos in enumerate(positions):
            ax = axes[i][j]
            for framing in ("john", "user"):
                d = L[(L.model == model) & (L.position == pos) & (L.statistic == f"normalised_lambda_{framing}")]
                d = d.drop_duplicates("layer").sort_values("layer")
                ax.fill_between(d.layer, d.ci_lower, d.ci_upper, color=FRAMING_COLOR[framing], alpha=0.15, lw=0)
                ax.plot(d.layer, d.estimate, color=FRAMING_COLOR[framing], lw=2, label=framing)
            ax.axhline(0, color=MUTED, lw=0.8)
            ax.set_xlabel("Layer", fontsize=8, color=INK2)
            style(ax, f"{display(model)}, truth probe, {pos}")
            ax.legend(fontsize=7, frameon=False)
        axes[i][0].set_ylabel("Normalised λ = λ_C / λ_D", fontsize=8, color=INK2)
    save(fig, "fig6_probe_layers")


def main() -> None:
    leak, coh = read("leakage.csv"), read("coherence.csv")
    fig1(coh) if not coh.empty else None
    if not leak.empty:
        fig2(leak), fig3(read("scatter_points.csv"), leak), fig5(leak), fig6(leak)
    fig4(read("retention.csv"))
    print(f"figures written to {C.FIGURES}")


if __name__ == "__main__":
    main()
