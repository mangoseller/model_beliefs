"""Render every paper figure into ``FIGURES_DIR`` (PDF) and ``FIGURE_DATA_DIR`` (tidy CSV).

Usage::

    python -m latent_beliefs.figures                       # all ten figures
    python -m latent_beliefs.figures --only fig5_implied_thresholds
"""

from __future__ import annotations

import argparse

from latent_beliefs.figures import style  # noqa: F401  (selects the Agg backend first)

import matplotlib.pyplot as plt  # noqa: E402

from latent_beliefs.figures import (  # noqa: E402
    fig1_loo_prediction_r2,
    fig2_decision_curves,
    fig3_probability_sufficiency_latent_probability,
    fig5_implied_thresholds,
    fig9_holdout_generalization_eight_decision_evidence,
    fig9_holdout_generalization_report_evidence,
    fig678_branching_composite,
    figA_probability_sufficiency_r2,
    figS_latent_sensitivity,
    figS_outcome_sufficiency_l1,
)

# Paper order: main text, then appendix.
FIGURES = {
    module.STEM: module.render
    for module in (
        fig1_loo_prediction_r2,
        fig9_holdout_generalization_eight_decision_evidence,
        fig2_decision_curves,
        fig3_probability_sufficiency_latent_probability,
        fig5_implied_thresholds,
        fig678_branching_composite,
        figS_latent_sensitivity,
        fig9_holdout_generalization_report_evidence,
        figA_probability_sufficiency_r2,
        figS_outcome_sufficiency_l1,
    )
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=tuple(FIGURES), default=list(FIGURES))
    args = parser.parse_args()
    for stem in args.only:
        # Each figure starts from the default style, as in a fresh process.
        with plt.rc_context():
            FIGURES[stem]()


if __name__ == "__main__":
    main()
