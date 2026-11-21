"""Appendix figure: held-out prediction under linear, monotone-spline, and 2-D latents.

Each point is the macro mean over the same sixteen leave-one-output-out
targets: mean c-index and mean out-of-sample R^2 (constant baseline: the
output's test-set mean).  Also writes the companion table
``figS_latent_sensitivity_delta_summary.csv`` (mean change vs the main model).

Inputs: ``EVALUATION_DIR/leave_one_out{,_spline}/{metrics_summary,leave_one_output_out_predictions}.csv``
and ``FITS_DIR/two_d/leave_one_output_out_{metrics,predictions}.csv``.
"""

from __future__ import annotations

from pathlib import Path

from latent_beliefs.figures import style

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from latent_beliefs.config import EVALUATION_DIR, FITS_DIR  # noqa: E402

STEM = "figS_latent_sensitivity"
MAIN_DIR = EVALUATION_DIR / "leave_one_out"
SPLINE_DIR = EVALUATION_DIR / "leave_one_out_spline"
TWO_D_DIR = FITS_DIR / "two_d"
METHOD_STYLE = {
    "main_1d_linear": {"label": "Main: 1-D linear", "color": "#333333", "marker": "o"},
    "monotone_spline": {"label": "1-D monotone spline", "color": "#0072B2", "marker": "s"},
    "two_dimensional": {"label": "2-D linear", "color": "#D55E00", "marker": "^"},
}


def _mean_r2(path: Path, *, latent_dim: int | None = None) -> pd.DataFrame:
    predictions = pd.read_csv(path, low_memory=False)
    if latent_dim is not None:
        predictions = predictions[predictions["latent_dim"].eq(latent_dim)].copy()
    finite = predictions[np.isfinite(predictions["actual"]) & np.isfinite(predictions["prediction"])].copy()
    finite["sq_err"] = (finite["actual"] - finite["prediction"]) ** 2
    by_output = finite.groupby(
        ["domain", "model_key", "model_display", "output_name"], as_index=False, sort=False
    ).agg(n_test=("actual", "size"), mse=("sq_err", "mean"), test_var_ddof1=("actual", "var"))
    by_output["baseline_test_mse"] = by_output["test_var_ddof1"] * (by_output["n_test"] - 1) / by_output["n_test"]
    by_output["r2"] = np.where(
        by_output["baseline_test_mse"] > 0.0, 1.0 - by_output["mse"] / by_output["baseline_test_mse"], np.nan
    )
    return by_output.groupby(["domain", "model_key", "model_display"], as_index=False, sort=False).agg(
        mean_r2=("r2", "mean"), n_defined_r2=("r2", "count")
    )


def _standard_summary(path: Path, method: str) -> pd.DataFrame:
    source = pd.read_csv(path / "metrics_summary.csv")
    result = source[["domain", "model_key", "model_display", "mean_c_index"]].copy()
    result = result.merge(
        _mean_r2(path / "leave_one_output_out_predictions.csv"),
        on=["domain", "model_key", "model_display"],
        validate="one_to_one",
    )
    result["method"] = method
    return result


def two_dimensional_summary(path: Path) -> pd.DataFrame:
    source = pd.read_csv(path / "leave_one_output_out_metrics.csv")
    source = source[source["latent_dim"].eq(2)].copy()
    rows = []
    for keys, group in source.groupby(["domain", "model_key", "model_display"], sort=False):
        cindex = group["c_index"].to_numpy(dtype=float)
        rows.append(
            {
                "domain": keys[0],
                "model_key": keys[1],
                "model_display": keys[2],
                "mean_c_index": float(np.mean(cindex[np.isfinite(cindex)])),
                "method": "two_dimensional",
            }
        )
    return pd.DataFrame(rows).merge(
        _mean_r2(path / "leave_one_output_out_predictions.csv", latent_dim=2),
        on=["domain", "model_key", "model_display"],
        validate="one_to_one",
    )


def comparison_data(main_dir: Path, spline_dir: Path, two_d_dir: Path) -> pd.DataFrame:
    frames = [
        _standard_summary(main_dir, "main_1d_linear"),
        _standard_summary(spline_dir, "monotone_spline"),
        two_dimensional_summary(two_d_dir),
    ]
    reference_keys = set(zip(frames[0]["domain"], frames[0]["model_key"], strict=True))
    for method, frame in zip(METHOD_STYLE, frames, strict=True):
        keys = set(zip(frame["domain"], frame["model_key"], strict=True))
        if keys != reference_keys:
            raise ValueError(
                f"{method} grid mismatch; missing={sorted(reference_keys - keys)}, extra={sorted(keys - reference_keys)}"
            )
    result = pd.concat(frames, ignore_index=True)
    return result[result["model_key"].isin(style.ALL_MODELS)].copy()


def delta_summary(data: pd.DataFrame) -> pd.DataFrame:
    wide = data.pivot(index=["domain", "model_key"], columns="method", values=["mean_c_index", "mean_r2"])
    rows = []
    for domain in style.DOMAIN_ORDER:
        selected = wide.loc[domain]
        for method in ("monotone_spline", "two_dimensional"):
            rows.append(
                {
                    "domain": domain,
                    "method": method,
                    "n_models": int(len(selected)),
                    "mean_delta_c_index": float(
                        (selected[("mean_c_index", method)] - selected[("mean_c_index", "main_1d_linear")]).mean()
                    ),
                    "mean_delta_r2": float(
                        (selected[("mean_r2", method)] - selected[("mean_r2", "main_1d_linear")]).mean()
                    ),
                }
            )
    return pd.DataFrame(rows)


def draw(data: pd.DataFrame):
    models = [model for model in style.ALL_MODELS if model in set(data["model_key"])]
    x = np.arange(len(models))
    fig, axes = plt.subplots(len(style.DOMAIN_ORDER), 2, figsize=(12.5, 10.0), sharex=True, layout="constrained")
    for row, domain in enumerate(style.DOMAIN_ORDER):
        for column, metric in enumerate(("mean_c_index", "mean_r2")):
            ax = axes[row, column]
            panel = data[data["domain"].eq(domain)].set_index(["method", "model_key"])
            for method, mstyle in METHOD_STYLE.items():
                values = np.array([panel.loc[(method, model), metric] for model in models], dtype=float)
                ax.plot(
                    x,
                    values,
                    color=mstyle["color"],
                    marker=mstyle["marker"],
                    linewidth=1.6,
                    markersize=5.5,
                    label=mstyle["label"],
                )
            if column == 0:
                ax.set_ylabel(f"{style.DOMAIN_DISPLAY[domain]}\nC-index")
                ax.set_ylim(0.5, 1.01)
            else:
                ax.axhline(0.0, color="0.65", linestyle="--", linewidth=0.9)
                ax.set_ylabel(f"{style.DOMAIN_DISPLAY[domain]}\nOut-of-sample $R^2$")
                ax.set_ylim(-0.2, 1.0)
            if row == 0:
                ax.set_title("Held-out output prediction c-index" if column == 0 else "Held-out output prediction $R^2$")
            if row == len(style.DOMAIN_ORDER) - 1:
                style.model_ticks(ax, models)
            else:
                ax.tick_params(labelbottom=False)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncols=3, bbox_to_anchor=(0.5, 1.02))
    return fig


def render(main_dir: Path = MAIN_DIR, spline_dir: Path = SPLINE_DIR, two_d_dir: Path = TWO_D_DIR) -> None:
    data = comparison_data(main_dir, spline_dir, two_d_dir)
    style.write_data(data, STEM)
    style.write_data(delta_summary(data), f"{STEM}_delta_summary")
    style.apply_style()
    style.save_figure(draw(data), STEM)


if __name__ == "__main__":
    render()
