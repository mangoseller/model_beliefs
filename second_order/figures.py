"""Figures of the curse-of-knowledge experiment, regenerated from ``outputs/curse/`` only (no model calls).

Headline and diagnostics:

* ``curse_schematic``: what lambda, phi and tau measure, on two lines of log-odds
  gaps (illustrative values);
* ``curse_decomposition``: the same diagram with each run's estimates, one row per
  model, dataset, method and setting;
* ``curse_estimates``: lambda, phi and tau per model, dataset, method and setting,
  with Camerer et al.'s w;
* ``curse_scatter``: the two regressions behind lambda;
* ``curse_linearity``: binned means of l_C - l_M and l_D - l_M against x with the
  fitted lines (the partial-linearity assumption);
* ``curse_case_leakage``: the distribution of per-case leakage (l_C - l_M) / (l_D - l_M);
* ``curse_comprehension``: lambda on all answers and on comprehension-passing answers.

After the paper's figures:

* ``curse_thresholds``: implied against instructed decision thresholds for the
  model's own fit family and PERSON's (Figure 5);
* ``curse_decision_curves``: the screening decision against the latent belief,
  per condition (Figure 3).

For the bespoke dataset:

* ``curse_bayes``: stated logits against the Bayes logit of the believer's evidence.

Plus ``retention``.  The style helpers here are shared with the deprecated RQ figures
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


FAMILY_COLOR = {"self": "#52514e", "john": "#2a78d6", "user": "#eb6834"}
KIND_MARKER = {"threshold": "o", "bet": "s", "cost": "^", "plain": "D"}
ROLE_COLOR = {"A": "#52514e", "B": "#8a8984", "D": "#7fb0eb", "M": "#1baf7a", "C": "#eb6834"}
SETTING_CONDITIONS = {"partial": ("A", "B", "D_john", "M_S4", "C_john"), "false": ("A", "B_F", "D_F", "M_F", "C_F")}


def groups_of(frame, keys=("model", "dataset", "method")):
    return list(frame.groupby(list(keys), sort=False).size().index)


def curse_thresholds(thr):
    """Implied vs instructed decision thresholds, own fit family against PERSON's (paper Figure 5)."""
    thr = thr[thr.nominal_threshold.notna()]
    if thr.empty:
        return
    models = list(thr.model.unique())
    fig, axes = plt.subplots(1, len(models), figsize=(3.6 * len(models), 3.4), squeeze=False)
    ticks = [0.05, 0.10, 0.20, 1 / 3]
    for ax, model in zip(axes[0], models):
        ax.plot([0.03, 0.4], [0.03, 0.4], color=MUTED, lw=0.8, ls=":")
        for (family, kind), g in thr[thr.model == model].groupby(["family", "kind"]):
            ax.scatter(g.nominal_threshold, g.implied_threshold, color=FAMILY_COLOR.get(family, INK),
                       marker=KIND_MARKER.get(kind, "o"), s=28, label=f"{family} · {kind}")
        ax.set_xscale("log"), ax.set_yscale("log")
        ax.set_xticks(ticks, ["5%", "10%", "20%", "33%"]), ax.set_yticks(ticks, ["5%", "10%", "20%", "33%"])
        ax.minorticks_off()
        ax.set_xlabel("Instructed threshold", fontsize=8, color=INK2)
        style(ax, display(model))
    axes[0][0].set_ylabel("Threshold implied by the fit", fontsize=8, color=INK2)
    axes[0][-1].legend(fontsize=6.5, frameon=False, loc="upper left")
    save(fig, "curse_thresholds")


def curse_decision_curves(curves):
    """P(recommend screening) against the latent belief per condition (paper Figure 3)."""
    if curves.empty:
        return
    models = list(curves.model.unique())
    fig, axes = plt.subplots(len(models), 2, figsize=(8, 3 * len(models)), squeeze=False)
    for i, model in enumerate(models):
        for j, (setting, conds) in enumerate(SETTING_CONDITIONS.items()):
            ax = axes[i][j]
            for cond in conds:
                g = curves[(curves.model == model) & (curves.condition == cond)]
                if g.empty:
                    continue
                color = CONDITION_COLOR.get(cond, INK)
                ax.plot(g.z, g.p_decide, color=color, lw=1.6, label=f"{cond} (n={int(g.n_cases.iloc[0])})")
                ax.fill_between(g.z, g.ci_lower, g.ci_upper, color=color, alpha=0.15, lw=0)
            ax.set_xlabel("Posterior mean belief z (all 16 outputs)", fontsize=8, color=INK2)
            ax.set_ylim(-0.02, 1.02)
            style(ax, f"{display(model)} · {setting}")
            ax.legend(fontsize=6.5, frameon=False, loc="lower right")
        axes[i][0].set_ylabel("P(recommend screening)", fontsize=8, color=INK2)
    save(fig, "curse_decision_curves")


def curse_bayes(points):
    """Bespoke items: mean stated logit against the Bayes logit of the believer's evidence."""
    if points.empty:
        return
    models = list(points.model.unique())
    fig, axes = plt.subplots(len(models), 2, figsize=(7.5, 3.4 * len(models)), squeeze=False)
    lim = np.nanmax(np.abs(np.r_[points.value, points.bayes_logit])) * 1.05
    for i, model in enumerate(models):
        for j, (title, roles) in enumerate((("own beliefs (A, B, B_F)", ("A", "B")),
                                            ("about PERSON (D, M, C)", ("D", "M", "C")))):
            ax = axes[i][j]
            ax.plot([-lim, lim], [-lim, lim], color=MUTED, lw=0.8, ls=":")
            for role in roles:
                g = points[(points.model == model) & (points.role == role)]
                ax.errorbar(g.bayes_logit, g.value, yerr=1.96 * g.sd.fillna(0), fmt="o", ms=4, lw=0.8,
                            color=ROLE_COLOR[role], mfc="white" if role == "C" else ROLE_COLOR[role],
                            label=role, alpha=0.85)
            ax.set_xlim(-lim, lim), ax.set_ylim(-lim, lim)
            ax.set_xlabel("Bayes logit, believer's evidence", fontsize=8, color=INK2)
            style(ax, f"{display(model)} · {title}")
            ax.legend(fontsize=7, frameon=False, loc="upper left")
        axes[i][0].set_ylabel("Mean stated logit", fontsize=8, color=INK2)
    save(fig, "curse_bayes")


def curse_linearity(scatter, est, bins: int = 8):
    """Binned means of the two gap regressions' outcomes against x, with the fitted lines."""
    if scatter.empty:
        return
    groups = groups_of(scatter)
    fig, axes = plt.subplots(len(groups), 2, figsize=(7.5, 2.6 * len(groups)), squeeze=False)
    for i, (model, dataset, method) in enumerate(groups):
        for j, setting in enumerate(("partial", "false")):
            ax = axes[i][j]
            d = scatter[(scatter.model == model) & (scatter.dataset == dataset) & (scatter.method == method)
                        & (scatter.setting == setting)]
            if len(d) < 3:
                ax.set_visible(False)
                continue
            x = d.x_A_minus_B.to_numpy()
            edges = np.unique(np.quantile(x, np.linspace(0, 1, min(bins, len(d)) + 1)))
            which = np.clip(np.digitize(x, edges[1:-1]), 0, len(edges) - 2)
            for column, color, label in (("y_D_minus_M", MUTED, "D − M"),
                                         ("y_C_minus_M", SETTING_COLOR[setting], "C − M")):
                y = d[column].to_numpy()
                mx = [x[which == b].mean() for b in range(len(edges) - 1) if (which == b).any()]
                my = [y[which == b].mean() for b in range(len(edges) - 1) if (which == b).any()]
                se = [y[which == b].std(ddof=1) / np.sqrt((which == b).sum()) if (which == b).sum() > 1 else 0
                      for b in range(len(edges) - 1) if (which == b).any()]
                ax.errorbar(mx, my, yerr=1.96 * np.array(se), fmt="o", ms=4, color=color, lw=1, label=label)
                slope, intercept = np.polyfit(x, y, 1)
                grid = np.linspace(x.min(), x.max(), 20)
                ax.plot(grid, intercept + slope * grid, color=color, lw=1, ls="--")
            ax.axhline(0, color=GRID, lw=0.8)
            ax.set_xlabel("l_A − l_B (binned)", fontsize=8, color=INK2)
            style(ax, f"{display(model)} · {dataset} · {method} · {setting}")
            ax.legend(fontsize=6.5, frameon=False, loc="upper left")
    save(fig, "curse_linearity")


def curse_case_leakage(scatter):
    """Per-case leakage (l_C - l_M) / (l_D - l_M), for cases with |l_D - l_M| above its median."""
    if scatter.empty:
        return
    groups = groups_of(scatter)
    fig, axes = plt.subplots(len(groups), 2, figsize=(7.5, 2.3 * len(groups)), squeeze=False)
    for i, (model, dataset, method) in enumerate(groups):
        for j, setting in enumerate(("partial", "false")):
            ax = axes[i][j]
            d = scatter[(scatter.model == model) & (scatter.dataset == dataset) & (scatter.method == method)
                        & (scatter.setting == setting)]
            den = d.y_D_minus_M.to_numpy()
            keep = np.abs(den) > np.median(np.abs(den)) if len(d) else np.array([], bool)
            if keep.sum() < 3:
                ax.set_visible(False)
                continue
            ratio = np.clip(d.y_C_minus_M.to_numpy()[keep] / den[keep], -1, 2)
            ax.hist(ratio, bins=np.linspace(-1, 2, 31), color=SETTING_COLOR[setting], alpha=0.8)
            ax.axvline(np.median(ratio), color=INK, lw=1.2, label=f"median {np.median(ratio):.2f}")
            for ref in (0, 1):
                ax.axvline(ref, color=MUTED, lw=0.8, ls=":")
            ax.set_xlabel("(l_C − l_M) / (l_D − l_M), clipped to [−1, 2]", fontsize=8, color=INK2)
            style(ax, f"{display(model)} · {dataset} · {method} · {setting} (n={int(keep.sum())})")
            ax.legend(fontsize=6.5, frameon=False)
    save(fig, "curse_case_leakage")


def curse_comprehension(est):
    """lambda on all answers against lambda on answers whose comprehension follow-up was correct."""
    L = est[(est.statistic == "lambda") & est.setting.isin(["partial", "false"])]
    if L.empty or "comprehension_pass" not in set(L.case_set):
        return
    groups = groups_of(L, ("model", "dataset", "method", "setting"))
    fig, ax = plt.subplots(figsize=(6, 0.4 * len(groups) + 1.4))
    w = float(C.load()["experiment"]["camerer_w"])
    for ref in (0, w, 1):
        ax.axvline(ref, color=MUTED, lw=0.8, ls=":" if ref == w else "-")
    for i, (model, dataset, method, setting) in enumerate(groups):
        for k, (case_set, hollow) in enumerate((("all", False), ("comprehension_pass", True))):
            r = L[(L.model == model) & (L.dataset == dataset) & (L.method == method) & (L.setting == setting)
                  & (L.case_set == case_set)]
            if r.empty or not np.isfinite(r.estimate.iloc[0]):
                continue
            r = r.iloc[0]
            ax.errorbar(r.estimate, i + (k - 0.5) * 0.3, xerr=[[r.estimate - r.ci_lower], [r.ci_upper - r.estimate]],
                        fmt="o", ms=5, color=SETTING_COLOR[setting], mfc="white" if hollow else SETTING_COLOR[setting],
                        elinewidth=1.1, capsize=0, label=case_set if i == 0 else None)
    ax.set_yticks(range(len(groups)), [f"{display(m)} · {d} · {k} · {s}" for m, d, k, s in groups], fontsize=7)
    ax.invert_yaxis()
    ax.legend(fontsize=7, frameon=False, loc="lower right")
    style(ax, "λ on all answers (filled) and comprehension-passing answers (hollow)")
    save(fig, "curse_comprehension")


# ---------------------------------------------------------------- the decomposition diagram
M_COLOR, C_COLOR, D_COLOR = CONDITION_COLOR["M_S4"], "#c4521f", CONDITION_COLOR["C_john"]


def brace(ax, x0: float, x1: float, y: float, height: float, *, color=INK2, lw: float = 1.0) -> None:
    """A horizontal curly brace from x0 to x1 at y, opening up (height < 0) or down (height > 0)."""
    if x1 - x0 < 1e-6:
        return
    t = np.linspace(0, 1, 201)
    half = np.where(t <= 0.5, t, 1 - t)
    shape = 1 / (1 + np.exp(-60 * (half - 0.03))) + 1 / (1 + np.exp(-60 * (half - 0.47)))
    ax.plot(x0 + t * (x1 - x0), y + height * shape / shape.max(), color=color, lw=lw, solid_capstyle="round")


def draw_decomposition(ax, lam: float, phi: float) -> None:
    """The model's own gap (top) and the belief attributed to PERSON (bottom), with delta = 1.

    l_B = l_M = 0, l_A = 1, l_D = phi, l_C = phi * lambda; dashed: D's shortfall from phi = 1.
    """
    top, bottom = 1.0, 0.0
    c, d = phi * lam, phi
    ax.plot([0, 1], [top, top], color=INK, lw=2.2, solid_capstyle="round")
    ax.plot([0, d], [bottom, bottom], color=INK, lw=2.2, solid_capstyle="round")
    ax.plot([d, 1], [bottom, bottom], color=MUTED, lw=1.4, ls=(0, (4, 3)))
    for x in (0, 1):
        ax.plot([x, x], [bottom, top], color=GRID, lw=1, ls=":", zorder=0)
    points = [(0, top, "$\\ell_B$", CONDITION_COLOR["B"]), (1, top, "$\\ell_A$", CONDITION_COLOR["A"]),
              (0, bottom, "$\\ell_M$", M_COLOR), (c, bottom, "$\\ell_C$", C_COLOR), (d, bottom, "$\\ell_D$", D_COLOR)]
    for x, y, label, color in points:
        ax.scatter([x], [y], s=70, color=color, zorder=3)
        ax.text(x, y + 0.1, label, color=color, ha="center", fontsize=12)
    ax.scatter([1], [bottom], s=70, facecolor="white", edgecolor=MUTED, lw=1.2, zorder=3)
    ax.text(1, bottom + 0.1, "$\\varphi = 1$", color=MUTED, ha="center", fontsize=10)
    ax.text(-0.05, top, "own", ha="right", va="center", fontsize=11, color=INK2)
    ax.text(-0.05, bottom, "PERSON", ha="right", va="center", fontsize=11, color=INK2)
    brace(ax, 0, 1, top + 0.3, 0.08)
    ax.text(0.5, top + 0.45, "$\\delta$", ha="center", fontsize=12, color=INK2)
    for x0, x1, label, color in ((0, c, "$\\lambda\\varphi\\delta$", C_COLOR), (c, d, "$\\tau\\delta$", D_COLOR),
                                 (d, 1, "$(1-\\varphi)\\delta$", MUTED)):
        brace(ax, x0, x1, bottom - 0.12, -0.07, color=color)
        ax.text((x0 + x1) / 2, bottom - 0.3, label, ha="center", va="top", fontsize=11, color=color)
    brace(ax, 0, d, bottom - 0.55, -0.07)
    ax.text(d / 2, bottom - 0.73, "$\\varphi\\delta$", ha="center", va="top", fontsize=11, color=INK2)
    ax.set_xlim(-0.2, 1.1)
    ax.set_ylim(bottom - 0.92, top + 0.62)
    ax.axis("off")


def curse_schematic(lam: float = 0.45, phi: float = 0.75) -> None:
    """What lambda, phi and tau measure (illustrative values)."""
    fig, ax = plt.subplots(figsize=(6.4, 3.3))
    draw_decomposition(ax, lam, phi)
    save(fig, "curse_schematic")


def curse_decomposition(est):
    """The decomposition diagram with each run's estimates: C at phi * lambda, D at phi (delta = 1)."""
    L = est[(est.case_set == "all") & (est.setting != "false_minus_partial")]
    groups = [g for g in groups_of(L, ("model", "dataset", "method", "setting"))
              if {"phi", "phi_lambda"} <= set(L[(L.model == g[0]) & (L.dataset == g[1]) & (L.method == g[2])
                                                & (L.setting == g[3])].statistic)]
    if not groups:
        return
    w = float(C.load()["experiment"]["camerer_w"])
    fig, ax = plt.subplots(figsize=(8.6, 0.55 * len(groups) + 1.6))
    for i, (model, dataset, method, setting) in enumerate(groups):
        g = L[(L.model == model) & (L.dataset == dataset) & (L.method == method) & (L.setting == setting)]
        g = g.set_index("statistic")
        y = -i
        ax.plot([0, 1], [y + 0.18, y + 0.18], color=GRID, lw=2.5, solid_capstyle="round")
        ax.plot([0, 1], [y, y], color=GRID, lw=0.8, ls=":")
        ax.scatter([0], [y], s=40, color=M_COLOR, zorder=3)
        ax.scatter([1], [y], s=40, facecolor="white", edgecolor=MUTED, zorder=3)
        for stat, color in (("phi_lambda", C_COLOR), ("phi", D_COLOR)):
            r = g.loc[stat]
            if np.isfinite(r.estimate):
                ax.errorbar(r.estimate, y, xerr=[[r.estimate - r.ci_lower], [r.ci_upper - r.estimate]], fmt="o",
                            ms=6, color=color, elinewidth=1.4, capsize=0, zorder=4)
        if np.isfinite(g.loc["phi"].estimate):
            ax.plot([w * g.loc["phi"].estimate] * 2, [y - 0.12, y + 0.12], color=MUTED, lw=1)
        lam = g.loc["lambda"] if "lambda" in g.index else None
        text = (f"λ = {lam.estimate:.2f} [{lam.ci_lower:.2f}, {lam.ci_upper:.2f}]" if lam is not None
                and np.isfinite(lam.estimate) else "λ blank")
        ax.text(1.2, y, f"{text};  φ = {g.loc['phi'].estimate:.2f}", va="center", fontsize=7, color=INK2)
        ax.text(-0.04, y, f"{display(model)} · {dataset} · {method} · {setting}", ha="right", va="center",
                fontsize=7, color=INK2)
    ax.text(0, 0.55, "$l_M$", ha="center", color=M_COLOR, fontsize=10)
    ax.text(0.5, 0.55, "$l_C$ at $\\varphi\\lambda$,  $l_D$ at $\\varphi$", ha="center", color=INK2, fontsize=10)
    ax.text(1, 0.55, "$l_A$ / $\\varphi=1$", ha="center", color=MUTED, fontsize=10)
    ax.set_xlim(-0.75, 1.75)
    ax.set_ylim(-len(groups) + 0.4, 0.85)
    ax.axis("off")
    ax.set_title("Estimated decomposition, δ = 1 (grey bar: the model's own gap; ticks: Camerer et al.'s λ = "
                 f"{w:g}; bars: 95% intervals)", fontsize=9, color=INK, loc="left")
    save(fig, "curse_decomposition")


def main() -> None:
    curse_schematic()
    est = read("curse/estimates.csv")
    if not est.empty:
        curse_decomposition(est)
        curse_estimates(est)
        curse_scatter(read("curse/scatter.csv"), est)
        curse_linearity(read("curse/scatter.csv"), est)
        curse_case_leakage(read("curse/scatter.csv"))
        curse_comprehension(est)
    curse_thresholds(read("curse/thresholds.csv"))
    curse_decision_curves(read("curse/decision_curves.csv"))
    curse_bayes(read("curse/bespoke_points.csv"))
    retention(read("curse/retention.csv"))
    print(f"figures written to {C.FIGURES}")


if __name__ == "__main__":
    main()
