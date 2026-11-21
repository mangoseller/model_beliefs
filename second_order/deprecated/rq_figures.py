"""Figures of the earlier RQ1-RQ3 design, regenerated from ``outputs/`` only (deprecated; no model calls).

RQ1: ``rq1_lambda_vs_capability`` (lambda for pair S4 against capability, with kappa),
     ``rq1_scatter`` (the two regressions behind lambda), ``rq1c_pairs`` (lambda per pair).
RQ2: ``rq2_coherence`` (coherence by condition), and the ladder rungs in ``rq1c_pairs``.
RQ3: ``rq3_lambda_by_method`` (latent, direct, probes), ``rq3_probe_layers``.
Plus ``retention``.

    python -m second_order.deprecated.rq_figures
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from second_order import config as C
from second_order.deprecated.analyze import GEMMA_PARAMS, OPEN_WEIGHT
from second_order.figures import (
    CONDITION_COLOR, CONDITION_ORDER, INK2, MUTED, PAIR_COLOR, errbar, plt, read, retention, save, style,
)
from second_order.report import display

def rq2_coherence(coh):
    co = coh[(coh.statistic == "mean_c_index") & (coh.direction == "reports_to_decisions")
             & coh.condition.isin(CONDITION_ORDER)]
    if co.empty:
        return
    co = co.assign(variant=co.get("variant", pd.Series(index=co.index, dtype=object)).fillna(co.method))
    variants = sorted(co.variant.unique())
    models = list(co.model.unique())
    fig, axes = plt.subplots(1, len(variants), figsize=(3.4 * len(variants), 3.2), squeeze=False, sharey=True)
    n = len(CONDITION_ORDER)
    for ax, v in zip(axes[0], variants):
        d = co[co.variant == v]
        for mi, model in enumerate(models):
            for ci, cond in enumerate(CONDITION_ORDER):
                r = d[(d.model == model) & (d.condition == cond)]
                if len(r):
                    errbar(ax, mi + (ci - (n - 1) / 2) * 0.11, r.iloc[0], CONDITION_COLOR[cond],
                           label=cond if mi == 0 else None)
        ax.set_xticks(range(len(models)), [display(m) for m in models], rotation=30, ha="right", fontsize=7)
        style(ax, v)
    axes[0][0].set_ylabel("Mean c-index, reports → decisions (test)", fontsize=8, color=INK2)
    axes[0][-1].legend(fontsize=7, frameon=False, loc="lower right")
    save(fig, "rq2_coherence")


def headline(leak):
    """All complete cases, pooled fit with raw outcomes, selected probe layers."""
    L = leak[(leak.case_set == "all_complete")]
    if "selected_layer" in L:
        L = L[L.selected_layer.map(lambda v: True if pd.isna(v) else bool(v))]
    return L[(L.method != "latent") | ((L.fit == "pooled") & (L.outcome_values == "raw"))]


def stat(L, statistic, framing="john", pair="S4"):
    return L[(L.statistic == statistic) & (L.framing == framing) & (L.pair == pair)]


def rq1_lambda_vs_capability(leak):
    L = headline(leak)
    L = L[L.method.isin(["latent", "direct"])]
    if stat(L, "T_C").empty:
        return
    floor = float(C.load()["analysis"]["capability_floor"])
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.2), squeeze=False)
    for ax, method in zip(axes[0][:2], ("latent", "direct")):
        for model, g in L[L.method == method].groupby("model"):
            cap = g.capability_condition_A_c_index.iloc[0]
            lam, kap = stat(g, "lambda"), stat(g, "kappa_halves")
            if lam.empty:
                continue
            r = lam.iloc[0]
            if np.isfinite(r.estimate):
                errbar(ax, cap, r, PAIR_COLOR["S4"], hollow=cap < floor)
            else:  # blanked by the ratio rule: shown, not dropped
                ax.plot(cap, 0, "x", color=MUTED, ms=7)
            if len(kap) and np.isfinite(kap.iloc[0].estimate):
                ax.plot(cap, kap.iloc[0].estimate, "_", color=INK2, ms=12, mew=2)
            ax.annotate(display(model), (cap, r.estimate if np.isfinite(r.estimate) else 0), fontsize=6,
                        color=INK2, xytext=(4, 2), textcoords="offset points")
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.axhline(1, color=MUTED, lw=0.8, ls=":")
        ax.set_xlabel("Condition A c-index (reports → decisions)", fontsize=8, color=INK2)
        style(ax, f"RQ1a/b: λ for pair S4, John ({method})")
    ax = axes[0][2]
    g = stat(L[(L.method == "latent") & L.model.isin(GEMMA_PARAMS)], "lambda")
    for _, r in g[np.isfinite(g.estimate)].iterrows():
        errbar(ax, GEMMA_PARAMS[r.model], r, PAIR_COLOR["S4"])
    ax.set_xscale("log")
    ax.set_xticks([4, 12, 27], ["4", "12", "27"])
    ax.minorticks_off()
    ax.set_xlim(3, 36)
    ax.set_xlabel("Gemma 3 parameters (B)", fontsize=8, color=INK2)
    style(ax, "λ by Gemma size (latent)")
    fig.text(0.01, -0.05, "Circle with 95% interval: λ = 1 − T_C / T_M (hollow: condition A c-index below the "
             "capability floor).  Bar: κ.  ×: λ blank because T_M's interval includes 0.", fontsize=7, color=INK2)
    axes[0][0].set_ylabel("λ (0 = no leakage, 1 = own belief)", fontsize=8, color=INK2)
    save(fig, "rq1_lambda_vs_capability")


def rq1_scatter(scatter, leak):
    if scatter.empty:
        return
    scatter = scatter[(scatter.framing == "john") & scatter.pair.isin(["S4", "F"])]
    models, pairs = list(scatter.model.unique()), ["S4", "F"]
    if not models:
        return
    fig, axes = plt.subplots(len(models), 2, figsize=(7, 3 * len(models)), squeeze=False)
    L = headline(leak)
    for i, model in enumerate(models):
        for j, pair in enumerate(pairs):
            ax = axes[i][j]
            d = scatter[(scatter.model == model) & (scatter.pair == pair)]
            if d.empty:
                ax.set_axis_off()
                continue
            s = L[(L.model == model) & (L.method == "latent") & (L.framing == "john")
                  & (L.pair == pair)].set_index("statistic")
            xs = np.linspace(d.x_A_minus_B.min(), d.x_A_minus_B.max(), 10)
            for col, slope, icpt, color, label in (("y_D_minus_M", "T_M", "intercept_M", MUTED, "D − M (no leakage)"),
                                                   ("y_D_minus_C", "T_C", "intercept_C", PAIR_COLOR[pair], "D − C")):
                if d[col].notna().any():
                    ax.scatter(d.x_A_minus_B, d[col], s=8, color=color, alpha=0.6, label=label, lw=0)
                if slope in s.index:
                    ax.plot(xs, s.loc[icpt].estimate + s.loc[slope].estimate * xs, color=color, lw=2)
            ax.set_xlabel("ℓ_A − ℓ_B (the model's own gap)", fontsize=8, color=INK2)
            ax.set_ylabel("ℓ_D − ℓ (answers about John)", fontsize=8, color=INK2)
            ax.legend(fontsize=7, frameon=False)
            style(ax, f"{display(model)}, pair {pair}")
    save(fig, "rq1_scatter")


def rq1c_pairs(leak):
    """λ per pair (S4, F, H with a matched control) and tracking τ per pair, John, latent."""
    L = headline(leak)
    L = L[(L.method == "latent") & (L.framing == "john")]
    if L.empty:
        return
    models = list(L.model.unique())
    fig, axes = plt.subplots(1, 2, figsize=(max(7, 1.3 * len(models) + 3), 3.2), squeeze=False)
    for ax, statistic, title in ((axes[0][0], "lambda", "RQ1c: λ by pair"),
                                 (axes[0][1], "tau_halves", "RQ2 ladder: tracking τ = κ(1 − λ) by pair")):
        pairs = [p for p in ("S4", "S1", "H", "F") if len(L[(L.statistic == statistic) & (L.pair == p)])]
        for mi, model in enumerate(models):
            for pi, pair in enumerate(pairs):
                r = L[(L.model == model) & (L.statistic == statistic) & (L.pair == pair)]
                if len(r) and np.isfinite(r.iloc[0].estimate):
                    errbar(ax, mi + (pi - (len(pairs) - 1) / 2) * 0.16, r.iloc[0], PAIR_COLOR[pair],
                           label=pair if mi == 0 else None)
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_xticks(range(len(models)), [display(m) for m in models], rotation=30, ha="right", fontsize=7)
        ax.legend(fontsize=7, frameon=False)
        style(ax, title)
    save(fig, "rq1c_pairs")


def rq3_lambda_by_method(leak):
    L = headline(leak)
    L = L[L.model.isin(OPEN_WEIGHT) & (L.statistic == "lambda") & (L.framing == "john") & L.pair.isin(["S4", "F"])]
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
            for pair, off in (("S4", -0.12), ("F", 0.12)):
                r = d[(d.label == lab) & (d.pair == pair)]
                if len(r) and np.isfinite(r.iloc[0].estimate):
                    errbar(ax, li + off, r.iloc[0], PAIR_COLOR[pair], label=pair if li == 0 else None)
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.axhline(1, color=MUTED, lw=0.8, ls=":")
        ax.set_xticks(range(len(labels)), labels, rotation=30, ha="right", fontsize=7)
        style(ax, display(model))
    axes[0][0].set_ylabel("λ (behavioural or probe)", fontsize=8, color=INK2)
    axes[0][-1].legend(fontsize=7, frameon=False)
    save(fig, "rq3_lambda_by_method")


def rq3_probe_layers(leak):
    L = leak[(leak.method == "probe") & (leak.case_set == "all_complete") & (leak.probe_type == "report")
             & (leak.statistic == "lambda") & (leak.framing == "john")] if "probe_type" in leak else pd.DataFrame()
    if L.empty:
        return
    models = list(L.model.unique())
    positions = sorted(L.position.unique())
    fig, axes = plt.subplots(len(models), len(positions), figsize=(3.6 * len(positions), 2.8 * len(models)),
                             squeeze=False, sharey=True)
    for i, model in enumerate(models):
        for j, pos in enumerate(positions):
            ax = axes[i][j]
            for pair in ("S4", "F"):
                d = L[(L.model == model) & (L.position == pos) & (L.pair == pair)]
                d = d.drop_duplicates("layer").sort_values("layer")
                ax.fill_between(d.layer, d.ci_lower, d.ci_upper, color=PAIR_COLOR[pair], alpha=0.15, lw=0)
                ax.plot(d.layer, d.estimate, color=PAIR_COLOR[pair], lw=2, label=pair)
            ax.axhline(0, color=MUTED, lw=0.8)
            ax.axhline(1, color=MUTED, lw=0.8, ls=":")
            ax.set_xlabel("Layer", fontsize=8, color=INK2)
            style(ax, f"{display(model)}, report probe, {pos}")
            ax.legend(fontsize=7, frameon=False)
        axes[i][0].set_ylabel("Probe λ", fontsize=8, color=INK2)
    save(fig, "rq3_probe_layers")


def main() -> None:
    leak, coh = read("leakage.csv"), read("coherence.csv")
    if not coh.empty:
        rq2_coherence(coh)
    if not leak.empty:
        rq1_lambda_vs_capability(leak)
        rq1_scatter(read("scatter_points.csv"), leak)
        rq1c_pairs(leak)
        rq3_lambda_by_method(leak)
        rq3_probe_layers(leak)
    retention(read("retention.csv"))
    print(f"figures written to {C.FIGURES}")


if __name__ == "__main__":
    main()
