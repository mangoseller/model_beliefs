"""Appendix figure: domain and framing holdout with the three belief reports as evidence.

Input: ``EVALUATION_DIR/holdout_grid_report/holdout_information_model_summary.csv``.
"""

from __future__ import annotations

from latent_beliefs.config import EVALUATION_DIR
from latent_beliefs.figures import holdout_grid

STEM = "fig9_holdout_generalization_report_evidence"
DATA_PATH = EVALUATION_DIR / "holdout_grid_report" / "holdout_information_model_summary.csv"


def render(data_path=DATA_PATH) -> None:
    holdout_grid.render(STEM, data_path)


if __name__ == "__main__":
    render()
