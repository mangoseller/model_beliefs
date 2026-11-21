"""Appendix figure: outcome sufficiency of the latent belief.

Per domain x model: the prevalence-normalized L1 dependence of the ground-truth
outcome on each eligible decision given the posterior-mean latent belief
(faint points), their mean (squares) and joint case-bootstrap 95% intervals
(see :mod:`latent_beliefs.evaluation.outcome_sufficiency`).  The reference
simulations are not plotted.

Inputs: ``EVALUATION_DIR/outcome_sufficiency/outcome_sufficiency_l1{,_per_prompt}.csv``.
Writes ``figS_outcome_sufficiency_l1.csv`` (model summaries) and
``figS_outcome_sufficiency_l1_per_prompt.csv``.
"""

from __future__ import annotations

from pathlib import Path

from latent_beliefs.figures import style

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.config import EVALUATION_DIR  # noqa: E402

STEM = "figS_outcome_sufficiency_l1"
DATA_DIR = EVALUATION_DIR / "outcome_sufficiency"


def draw(summary: pd.DataFrame, family: pd.DataFrame):
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.7), sharey=True, layout="constrained")
    for ax, domain in zip(axes, style.DOMAIN_ORDER):
        sub = summary[summary.domain.eq(domain)].set_index("model_key").loc[list(style.ALL_MODELS)]
        for x, model in enumerate(style.ALL_MODELS):
            vals = family[family.domain.eq(domain) & family.model_key.eq(model)].l1_normalized.dropna()
            ax.scatter(x + np.linspace(-0.07, 0.07, len(vals)), vals, color="#0072B2", s=12, alpha=0.25)
        x = np.arange(len(sub))
        # Percentile intervals need not contain the point estimate, so the
        # endpoints are drawn directly rather than with errorbar(yerr=...).
        ax.vlines(x, sub.ci95_low, sub.ci95_high, color="#0072B2", lw=1.3)
        ax.hlines(sub.ci95_low, x - 0.065, x + 0.065, color="#0072B2", lw=1.3)
        ax.hlines(sub.ci95_high, x - 0.065, x + 0.065, color="#0072B2", lw=1.3)
        ax.scatter(x, sub.l1_mean, color="#0072B2", marker="s", s=34, zorder=4, label="Observed mean; joint 95% CI")
        style.model_ticks(ax, style.ALL_MODELS)
        ax.set_title(style.DOMAIN_DISPLAY[domain])
        ax.set_xlim(-0.5, len(sub) - 0.5)
    axes[0].set_ylim(bottom=0)
    axes[0].set_ylabel(r"$L_1$ conditional on $z$ / $P(Y=1)$")
    return fig


def render(data_dir: Path = DATA_DIR) -> None:
    summary = pd.read_csv(data_dir / "outcome_sufficiency_l1.csv", float_precision="round_trip")
    family = pd.read_csv(data_dir / "outcome_sufficiency_l1_per_prompt.csv", float_precision="round_trip")
    style.write_data(summary, STEM)
    style.write_data(family, f"{STEM}_per_prompt")
    style.apply_style()
    style.save_figure(draw(summary, family), STEM)


if __name__ == "__main__":
    render()
