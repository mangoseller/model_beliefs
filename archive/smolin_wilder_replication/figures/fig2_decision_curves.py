"""Paper Figure 3: smoothed decision curves of the low/medium/high example models.

One panel per domain.  Each line is a Nadaraya-Watson smoother (Gaussian
kernel, bandwidth 0.10) of the domain's unparameterized decision against the
all-output posterior-mean latent belief of the main fit (test cases), evaluated
over the 5th-95th percentile of the observed z.  Ribbons are pointwise 95%
paired-case bootstrap intervals (posterior means and bandwidth held fixed).

Inputs: ``FITS_DIR/main/latent_posteriors.csv`` (``z_mean``) and ``DATASETS_DIR/<domain>.csv``.
"""

from __future__ import annotations

from pathlib import Path

from archive.smolin_wilder_replication.figures import style

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402

from archive.smolin_wilder_replication.config import DATASETS_DIR, FITS_DIR  # noqa: E402
from archive.smolin_wilder_replication.models import MODEL_DISPLAY  # noqa: E402

STEM = "fig2_decision_curves"
POSTERIORS_PATH = FITS_DIR / "main" / "latent_posteriors.csv"
DECISION_OUTPUTS = {
    "ckd": "decision_no_threshold",
    "werewolf": "decision_no_threshold",
    "forecasting": "experiment_priority_no_threshold",
}
# Colorblind-safe warm-to-cool accents, weakest -> strongest.
MODEL_COLORS = dict(zip(style.TIER_MODELS, ("#D55E00", "#56B4E9", "#0072B2")))
PANEL_LETTERS = ("A", "B", "C")
LINEWIDTH = 2.25
BANDWIDTH = 0.10
N_BOOTSTRAP = 2_000
BOOTSTRAP_ALPHA = 0.05
BOOTSTRAP_SEED = 20260817
BAND_ALPHA = 0.14


def nw_smooth(z: np.ndarray, y: np.ndarray, bandwidth: float = BANDWIDTH, n_grid: int = 200):
    """Nadaraya-Watson mean of y on a grid over the 5th-95th percentile of z."""
    lo, hi = np.quantile(z, [0.05, 0.95])
    grid = np.linspace(lo, hi, n_grid)
    weights = np.exp(-0.5 * ((grid[:, None] - z[None, :]) / bandwidth) ** 2)
    smoothed = weights @ y / weights.sum(axis=1)
    return grid, smoothed


def nw_bootstrap_interval(z: np.ndarray, y: np.ndarray, grid: np.ndarray, rng: np.random.Generator):
    """Pointwise paired-case bootstrap interval for the smoother."""
    n_obs = len(z)
    kernel_weights = np.exp(-0.5 * ((grid[:, None] - z[None, :]) / BANDWIDTH) ** 2)
    resample_counts = rng.multinomial(n_obs, np.full(n_obs, 1.0 / n_obs), size=N_BOOTSTRAP)
    denominator = kernel_weights @ resample_counts.T
    numerator = kernel_weights @ (resample_counts * y).T
    bootstrap_curves = numerator / denominator
    lower, upper = np.quantile(bootstrap_curves, (BOOTSTRAP_ALPHA / 2.0, 1.0 - BOOTSTRAP_ALPHA / 2.0), axis=1)
    return lower, upper


def render(posteriors_path: Path = POSTERIORS_PATH, datasets_dir: Path = DATASETS_DIR) -> None:
    style.apply_style()
    plt.rcParams.update(
        {
            "axes.titlesize": 11.5,
            "axes.labelsize": 10.5,
            "axes.linewidth": 0.8,
            "grid.color": "#D7DDE3",
            "grid.alpha": 0.72,
            "grid.linewidth": 0.7,
            "lines.solid_capstyle": "round",
        }
    )

    posteriors = pd.read_csv(posteriors_path)
    datasets = {domain: pd.read_csv(datasets_dir / f"{domain}.csv") for domain in style.DOMAIN_ORDER}
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    tidy_rows: list[dict] = []

    fig, axes = plt.subplots(1, len(style.DOMAIN_ORDER), figsize=(12.6, 4.25), sharey=True, constrained_layout=True)
    fig.get_layout_engine().set(h_pad=0.08, w_pad=0.08, hspace=0.02, wspace=0.04)

    for col_index, domain in enumerate(style.DOMAIN_ORDER):
        ax = axes[col_index]
        output_name = DECISION_OUTPUTS[domain]
        data = datasets[domain]
        for model_key in style.TIER_MODELS:
            color = MODEL_COLORS[model_key]
            post = posteriors[(posteriors["domain"] == domain) & (posteriors["model_key"] == model_key)][
                ["source_prompt_id", "z_mean"]
            ]
            joined = post.merge(
                data.loc[data["model_key"] == model_key, ["source_prompt_id", output_name]],
                on="source_prompt_id",
                how="inner",
            ).dropna(subset=[output_name, "z_mean"])
            if joined.empty:
                continue
            z_obs = joined["z_mean"].to_numpy()
            y_obs = joined[output_name].to_numpy(dtype=float)

            grid, smoothed = nw_smooth(z_obs, y_obs)
            lower, upper = nw_bootstrap_interval(z_obs, y_obs, grid, rng)
            ax.fill_between(grid, lower, upper, color=color, alpha=BAND_ALPHA, linewidth=0, zorder=1)
            ax.plot(grid, smoothed, color=color, lw=LINEWIDTH, zorder=3)
            for g, s, lo, hi in zip(grid, smoothed, lower, upper):
                tidy_rows.append(
                    {
                        "domain": domain,
                        "model_key": model_key,
                        "output_name": output_name,
                        "z_grid": float(g),
                        "smoothed_p_action": float(s),
                        "bootstrap_lower_95": float(lo),
                        "bootstrap_upper_95": float(hi),
                        "bandwidth": BANDWIDTH,
                        "n_obs": len(z_obs),
                        "n_bootstrap": N_BOOTSTRAP,
                        "bootstrap_seed": BOOTSTRAP_SEED,
                    }
                )

        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(-0.02, 1.02)
        ax.set_xticks((0.0, 0.25, 0.5, 0.75, 1.0))
        ax.set_yticks((0.0, 0.25, 0.5, 0.75, 1.0))
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0, decimals=0))
        ax.set_title(f"{PANEL_LETTERS[col_index]}   {style.DOMAIN_DISPLAY[domain]}", loc="left", pad=10)
        ax.set_xlabel(r"Latent state, $z$")
        ax.tick_params(axis="both", length=3.5, width=0.8)
        if col_index == 0:
            ax.set_ylabel(r"Action probability, $P(a = 1)$")

    handles = [plt.Line2D([], [], color=MODEL_COLORS[m], lw=LINEWIDTH, label=MODEL_DISPLAY[m]) for m in style.TIER_MODELS]
    fig.legend(
        handles=handles,
        loc="outside upper center",
        ncol=len(style.TIER_MODELS),
        columnspacing=2.2,
        handlelength=3.0,
        handletextpad=0.65,
        borderaxespad=0.15,
    )
    style.save_figure(fig, STEM)
    style.write_data(pd.DataFrame(tidy_rows), STEM)


if __name__ == "__main__":
    render()
