"""Paper Figure 5: learned decision thresholds vs instructed thresholds.

For each parameterized decision of the example models, the main fit's
observation model ``logit P(Y=1 | z) = intercept + slope * logit(z)`` crosses
one half at ``z = sigmoid(-intercept / slope)`` (the ``implied_threshold``
parameter, checked here against intercept and slope).

Input: ``FITS_DIR/main/parameters.csv``.
"""

from __future__ import annotations

from pathlib import Path

from latent_beliefs.figures import style

import matplotlib.lines as mlines  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.config import FITS_DIR  # noqa: E402
from latent_beliefs.models import MODEL_DISPLAY  # noqa: E402

STEM = "fig5_implied_thresholds"
PARAMETERS_PATH = FITS_DIR / "main" / "parameters.csv"
# Rows run from most to least capable.
ROW_MODELS = tuple(reversed(style.TIER_MODELS))
FRAMING_ORDER = ("threshold", "bet", "cost")
DODGE = {"threshold": -0.006, "bet": 0.0, "cost": 0.006}
XTICKS = [0.05, 0.10, 0.20, 1 / 3]
XTICK_LABELS = ["0.05", "0.10", "0.20", "0.33"]


def output_framing(output_name: str) -> str | None:
    if "no_threshold" in output_name:
        return None
    if "bet_payoff" in output_name:
        return "bet"
    if "cost_fn" in output_name:
        return "cost"
    if "threshold" in output_name:
        return "threshold"
    return None


def load_tidy(path: Path = PARAMETERS_PATH) -> pd.DataFrame:
    """The example models' threshold grid from the fitted parameters."""
    parameters = pd.read_csv(path)
    selected = parameters[
        parameters["output_kind"].eq("binary")
        & parameters["model_key"].isin(ROW_MODELS)
        & parameters["parameter"].isin(["nominal_threshold", "implied_threshold", "intercept", "slope"])
    ].copy()
    keys = ["domain", "domain_display", "model_key", "model_display", "model_position", "output_name"]
    if selected.duplicated(keys + ["parameter"]).any():
        raise ValueError("duplicate latent-model parameter rows")
    tidy = selected.pivot(index=keys, columns="parameter", values="value").reset_index()
    tidy.columns.name = None
    tidy = tidy[tidy["nominal_threshold"].notna()].copy()
    tidy["framing"] = tidy["output_name"].map(output_framing)
    if tidy["framing"].isna().any():
        unknown = sorted(tidy.loc[tidy["framing"].isna(), "output_name"].unique())
        raise ValueError(f"unrecognized parameterized binary outputs: {unknown}")

    expected_rows = len(style.DOMAIN_ORDER) * len(ROW_MODELS) * len(FRAMING_ORDER) * 4
    if len(tidy) != expected_rows:
        raise ValueError(f"latent-threshold grid has {len(tidy)} rows; expected {expected_rows}")
    if not tidy.groupby(["domain", "model_key", "framing"], sort=False).size().eq(4).all():
        raise ValueError("each domain/model/framing cell must contain four thresholds")
    if not np.isfinite(tidy[["intercept", "slope", "implied_threshold"]]).all().all():
        raise ValueError("fitted threshold parameters must all be finite")
    if not tidy["slope"].gt(0).all():
        raise ValueError("fitted binary slopes must be positive")
    reconstructed = 1.0 / (1.0 + np.exp(tidy["intercept"] / tidy["slope"]))
    if not np.allclose(reconstructed, tidy["implied_threshold"], rtol=1e-10, atol=1e-12):
        raise ValueError("stored implied thresholds do not match intercept/slope crossings")

    tidy = tidy.rename(columns={"domain": "setting", "implied_threshold": "latent_implied_threshold"})
    tidy["nominal_label"] = tidy["nominal_threshold"].map({0.05: "0.05", 0.10: "0.10", 0.20: "0.20", 1 / 3: "0.33"})
    tidy["domain_position"] = tidy["setting"].map({d: i for i, d in enumerate(style.DOMAIN_ORDER)})
    tidy["row_position"] = tidy["model_key"].map({m: i for i, m in enumerate(ROW_MODELS)})
    tidy["framing_position"] = tidy["framing"].map({f: i for i, f in enumerate(FRAMING_ORDER)})
    return tidy.sort_values(
        ["row_position", "domain_position", "framing_position", "nominal_threshold"]
    ).reset_index(drop=True)


def draw(tidy: pd.DataFrame):
    fig, axes = plt.subplots(
        len(ROW_MODELS),
        len(style.DOMAIN_ORDER),
        figsize=(10.5, 3 * len(ROW_MODELS)),
        sharex=True,
        sharey=True,
        layout="constrained",
        squeeze=False,
    )
    for row, model in enumerate(ROW_MODELS):
        for column, domain in enumerate(style.DOMAIN_ORDER):
            ax = axes[row, column]
            panel = tidy[tidy["model_key"].eq(model) & tidy["setting"].eq(domain)]
            ax.plot([0.02, 0.37], [0.02, 0.37], ls="--", color=style.ACCENT_DARK, lw=1.2, zorder=1)
            for framing in FRAMING_ORDER:
                fstyle = style.FRAMING_STYLE[framing]
                points = panel[panel["framing"].eq(framing)].sort_values("nominal_threshold")
                ax.scatter(
                    points["nominal_threshold"].to_numpy() + DODGE[framing],
                    points["latent_implied_threshold"],
                    color=fstyle["color"],
                    marker=fstyle["marker"],
                    s=54,
                    edgecolors="white",
                    linewidths=0.8,
                    zorder=3,
                )
            ax.set_xlim(0.02, 0.37)
            ax.set_ylim(-0.03, 1.03)
            ax.set_xticks(XTICKS)
            ax.set_xticklabels(XTICK_LABELS)
            if row == 0:
                ax.set_title(style.DOMAIN_DISPLAY[domain])
            if row == len(ROW_MODELS) - 1:
                ax.set_xlabel("Nominal threshold")
            if column == 0:
                ax.set_ylabel(MODEL_DISPLAY[model], fontweight="bold")

    fig.supylabel("Learned decision threshold in latent belief $z$", fontsize=11)
    handles = [
        mlines.Line2D(
            [],
            [],
            color=style.FRAMING_STYLE[framing]["color"],
            marker=style.FRAMING_STYLE[framing]["marker"],
            markeredgecolor="white",
            markeredgewidth=0.8,
            markersize=6.5,
            linestyle="none",
            label=style.FRAMING_STYLE[framing]["label"],
        )
        for framing in FRAMING_ORDER
    ]
    handles.append(mlines.Line2D([], [], color=style.ACCENT_DARK, ls="--", lw=1.2, label="Instructed threshold"))
    fig.legend(handles=handles, loc="outside lower center", ncol=4)
    return fig


def render(path: Path = PARAMETERS_PATH) -> None:
    style.apply_style()
    tidy = load_tidy(path)
    style.write_data(tidy, STEM)
    style.save_figure(draw(tidy), STEM)


if __name__ == "__main__":
    render()
