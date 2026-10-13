"""Paper Figure 2: prediction under domain and framing holdout.

In all four fitting regimes, each test case's latent state is inferred from
the eight decisions in the two parameterized framing families other than the
prediction target's family.

Input: ``EVALUATION_DIR/holdout_grid_eight_decision/holdout_information_model_summary.csv``.
"""

from __future__ import annotations

from latent_beliefs.config import EVALUATION_DIR
from latent_beliefs.figures import holdout_grid

STEM = "fig9_holdout_generalization_eight_decision_evidence"
DATA_PATH = EVALUATION_DIR / "holdout_grid_eight_decision" / "holdout_information_model_summary.csv"


def render(data_path=DATA_PATH) -> None:
    holdout_grid.render(STEM, data_path)


if __name__ == "__main__":
    render()
