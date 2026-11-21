"""How much of the latent model's predictive signal the stated probability carries.

For every domain x model, the main fit is restored unchanged and the latent
posterior is conditioned on the numerical probability report alone; the
fifteen other outputs are predicted from it and compared with the
leave-one-output-out predictions (posterior from all fifteen other outputs).

* Paper Figure 4 (concordance share): ``(mean_j c_prob,j - 0.5) / (mean_j c_latent,j - 0.5)``
  over the jointly evaluable targets (all fifteen unless a c-index is undefined).
* Appendix probability-sufficiency figure (pooled R^2): ``1 - sum_j SSE_j / sum_j SST_j``
  for both predictors, with SST_j around the test-set mean of target j.

Both carry 95% percentile intervals from 10,000 bootstrap resamples of the
fifteen target outputs.

Usage::

    python -m latent_beliefs.evaluation.probability_sufficiency

Inputs: ``FITS_DIR/main`` and ``DATASETS_DIR``;
``EVALUATION_DIR/leave_one_out/{leave_one_output_out_predictions,metrics_by_output}.csv``.
Outputs (``EVALUATION_DIR/probability_sufficiency``):
``probability_only_predictions.csv`` (test case x target),
``concordance_by_target.csv``, ``concordance_share.csv`` (Figure 4 data),
``r2_by_target.csv``, ``pooled_r2.csv`` (appendix figure data).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from latent_beliefs.config import DOMAIN_DISPLAY, DOMAINS, EVALUATION_DIR, FITS_DIR, ensure
from latent_beliefs.latent import domains as D
from latent_beliefs.latent.model import (
    ProbabilisticLatentModel,
    concordance_index,
    load_fit,
    restore_cell,
)
from latent_beliefs.models import FIGURE_ORDER, MODEL_DISPLAY

BOOTSTRAP_REPS = 10_000
BOOTSTRAP_SEED = 20260811
N_TARGETS = 15
PROBABILITY_OUTPUT = {"ckd": "probability", "werewolf": "probability", "forecasting": "probability_positive"}
# Domain labels as shown in the paper figures.
METHOD_ORDER = ("latent", "probability_only")
METHOD_DISPLAY = {"latent": "All other outputs", "probability_only": "Numerical probability only"}
# Bootstrap seeds: seed + 1000 * domain index + figure-order model index.
DOMAIN_INDEX = {domain: index for index, domain in enumerate(DOMAINS)}
MODEL_INDEX = {model: index for index, model in enumerate(FIGURE_ORDER)}


# Predictions
def probability_conditioned_predictions(
    model: ProbabilisticLatentModel, likelihood_values: np.ndarray, raw_test: pd.DataFrame
) -> pd.DataFrame:
    """Predict every other output from p(z | numerical probability) only."""
    observations = torch.as_tensor(likelihood_values, dtype=model.dtype, device=model.z_grid.device)
    probability_indices = [index for index, spec in enumerate(model.specs) if spec.kind == "probability"]
    if len(probability_indices) != 1:
        raise ValueError(f"Expected one numerical probability output, found {len(probability_indices)}")
    probability_index = probability_indices[0]
    target_indices = [index for index in range(len(model.specs)) if index != probability_index]

    model.eval()
    parts: list[pd.DataFrame] = []
    with torch.no_grad():
        log_by_output = model.log_likelihood_by_output(observations)
        log_joint = log_by_output[:, probability_index, :] + model.prior_log_weights()[None, :]
        log_weights = log_joint - torch.logsumexp(log_joint, dim=1, keepdim=True)
        weights = torch.exp(log_weights)
        for target_index in target_indices:
            spec = model.specs[target_index]
            prediction = weights @ model.conditional_mean(spec)
            parts.append(
                pd.DataFrame(
                    {
                        "row_index": np.arange(len(observations)),
                        "output_name": spec.name,
                        "output_kind": spec.kind,
                        "actual": raw_test[spec.name].to_numpy(dtype=float),
                        "prediction": prediction.cpu().numpy(),
                    }
                )
            )
    return pd.concat(parts, ignore_index=True)


def generate_probability_conditioned_predictions(fit_dir: Path) -> pd.DataFrame:
    """Restore each fixed fit and condition it on the probability output only."""
    parameters, metadata = load_fit(fit_dir)
    parts: list[pd.DataFrame] = []
    for domain in DOMAINS:
        config = D.DOMAIN_CONFIGS[domain]
        dataset = D.load_dataset(domain)
        for model_key in FIGURE_ORDER:
            _, _, _, raw_test = D.model_splits(dataset, model_key)
            if raw_test.empty:
                raise ValueError(f"{domain}/{model_key} lacks test rows")
            model = restore_cell(parameters, metadata, domain, model_key, config.output_specs)
            values = D.likelihood_values(raw_test, config.output_specs)
            predictions = probability_conditioned_predictions(model, values, raw_test)
            predictions.insert(0, "model_key", model_key)
            predictions.insert(0, "domain", domain)
            parts.append(predictions)
            print(f"Conditioned latent model on probability for {domain}/{model_key}", flush=True)
    return pd.concat(parts, ignore_index=True)


# Concordance share
def align_prediction_frames(latent_predictions: pd.DataFrame, probability_predictions: pd.DataFrame) -> pd.DataFrame:
    """Align both predictors on the exact same test outcomes."""
    keys = ["domain", "model_key", "row_index", "output_name"]
    for name, frame in (("latent", latent_predictions), ("probability-only", probability_predictions)):
        if frame.duplicated(keys).any():
            raise ValueError(f"{name} predictions are not unique on {keys}")
    columns = keys + ["output_kind", "actual", "prediction"]
    latent = latent_predictions[columns].rename(
        columns={"output_kind": "output_kind_latent", "actual": "actual_latent", "prediction": "prediction_latent"}
    )
    probability = probability_predictions[columns].rename(
        columns={
            "output_kind": "output_kind_probability",
            "actual": "actual_probability",
            "prediction": "prediction_probability",
        }
    )
    aligned = latent.merge(probability, on=keys, how="outer", validate="one_to_one", indicator=True)
    if not aligned["_merge"].eq("both").all():
        raise ValueError(f"Prediction rows do not align: {aligned['_merge'].value_counts().to_dict()}")
    if not aligned["output_kind_latent"].eq(aligned["output_kind_probability"]).all():
        raise ValueError("Output kinds differ between the two predictors")
    if not np.allclose(aligned["actual_latent"], aligned["actual_probability"], equal_nan=True):
        raise ValueError("Actual outcomes differ between the two predictors")
    return aligned.drop(columns=["_merge"]).rename(
        columns={"output_kind_latent": "output_kind", "actual_latent": "actual"}
    )


def paired_output_cindices(aligned: pd.DataFrame) -> pd.DataFrame:
    """Target-level c-indices on common finite test observations."""
    rows = []
    group_keys = ["domain", "model_key", "output_name", "output_kind"]
    for key, group in aligned.groupby(group_keys, sort=False):
        finite = group[
            np.isfinite(group["actual"])
            & np.isfinite(group["prediction_latent"])
            & np.isfinite(group["prediction_probability"])
        ]
        actual = finite["actual"].to_numpy(dtype=float)
        c_latent, n_pairs_latent = concordance_index(actual, finite["prediction_latent"].to_numpy(dtype=float))
        c_prob, n_pairs_prob = concordance_index(actual, finite["prediction_probability"].to_numpy(dtype=float))
        rows.append(
            {
                **dict(zip(group_keys, key, strict=True)),
                "n_test": int(len(finite)),
                "c_latent": float(c_latent),
                "c_prob": float(c_prob),
                "n_comparable_pairs_latent": int(n_pairs_latent),
                "n_comparable_pairs_prob": int(n_pairs_prob),
            }
        )
    return pd.DataFrame(rows)


def concordance_pairs(
    loo_predictions: pd.DataFrame, loo_metrics: pd.DataFrame, probability_predictions: pd.DataFrame
) -> pd.DataFrame:
    latent = loo_predictions[loo_predictions["model_key"].isin(FIGURE_ORDER)].copy()
    is_probability_target = np.zeros(len(latent), dtype=bool)
    for domain, output_name in PROBABILITY_OUTPUT.items():
        is_probability_target |= latent["domain"].eq(domain) & latent["output_name"].eq(output_name)
    latent = latent[~is_probability_target].copy()
    pairs = paired_output_cindices(align_prediction_frames(latent, probability_predictions))

    # The latent c-index is taken from metrics_by_output.csv (computed before the
    # per-row predictions passed through CSV, which can perturb exact ties).
    tracked = loo_metrics[["domain", "model_key", "output_name", "c_index"]].rename(
        columns={"c_index": "c_latent_tracked"}
    )
    pairs = pairs.merge(tracked, on=["domain", "model_key", "output_name"], how="left", validate="one_to_one")
    if (pairs["c_latent"] - pairs["c_latent_tracked"]).abs().dropna().gt(1e-3).any():
        raise ValueError("Recomputed and tracked LOO c-indices disagree by more than 0.001")
    pairs["c_latent_recomputed_from_csv"] = pairs["c_latent"]
    pairs["c_latent"] = pairs["c_latent_tracked"]
    pairs = pairs.drop(columns=["c_latent_tracked"])
    expected = len(DOMAINS) * len(FIGURE_ORDER) * N_TARGETS
    if len(pairs) != expected:
        raise ValueError(f"Expected {expected} target cells; found {len(pairs)}")
    return pairs


def summarize_cindex_pairs(pairs: pd.DataFrame) -> pd.DataFrame:
    """Ratio of macro-average excess c-indices per cell, with target bootstrap."""
    if pairs.duplicated(["domain", "model_key", "output_name"]).any():
        raise ValueError("Paired c-index rows must be unique by domain/model/output")
    rows = []
    for (domain, model), cell in pairs.groupby(["domain", "model_key"], sort=False):
        cell = cell.sort_values("output_name").reset_index(drop=True)
        if len(cell) != N_TARGETS:
            raise ValueError(f"Expected {N_TARGETS} targets for {domain}/{model}; found {len(cell)}")
        evaluable = cell.dropna(subset=["c_latent", "c_prob"]).reset_index(drop=True)
        if evaluable.empty:
            raise ValueError(f"No jointly evaluable targets for {domain}/{model}")
        share = (evaluable["c_prob"].mean() - 0.5) / (evaluable["c_latent"].mean() - 0.5)

        seed = BOOTSTRAP_SEED + 1_000 * DOMAIN_INDEX[domain] + MODEL_INDEX[model]
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(evaluable), size=(BOOTSTRAP_REPS, len(evaluable)))
        c_lat = evaluable["c_latent"].to_numpy(dtype=float)[idx].mean(axis=1)
        c_prb = evaluable["c_prob"].to_numpy(dtype=float)[idx].mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            draws = (c_prb - 0.5) / (c_lat - 0.5)
        finite_draws = draws[np.isfinite(draws)]
        if finite_draws.size == 0:
            ci_lower = ci_upper = np.nan
        else:
            ci_lower, ci_upper = np.percentile(finite_draws, [2.5, 97.5])

        rows.append(
            dict(
                domain=domain,
                domain_display=DOMAIN_DISPLAY[domain],
                model_key=model,
                model_display=MODEL_DISPLAY[model],
                mean_c_latent=evaluable["c_latent"].mean(),
                mean_c_prob=evaluable["c_prob"].mean(),
                share_captured=share,
                ci_lower=ci_lower,
                ci_upper=ci_upper,
                n_families_total=len(cell),
                n_families_used=len(evaluable),
                n_families_unevaluable=len(cell) - len(evaluable),
                aggregation="ratio_of_macro_mean_excess_c_indices",
                bootstrap_unit="target_output",
                bootstrap_reps=BOOTSTRAP_REPS,
                bootstrap_seed=seed,
                bootstrap_finite_reps=int(finite_draws.size),
                latent_predictor="target_loo_posterior_from_all_other_outputs",
                probability_predictor="fixed_latent_model_posterior_from_numerical_probability_only",
            )
        )
        print(
            f"{domain:9s} {model:14s} used={len(evaluable):2d}/{len(cell):2d} "
            f"share={share:.3f} [{ci_lower:.3f}, {ci_upper:.3f}]",
            flush=True,
        )
    return pd.DataFrame(rows)


# Pooled R^2
def prediction_components(predictions: pd.DataFrame, method: str) -> pd.DataFrame:
    """Per-output SSE and SST (around the test mean) on the reported scale."""
    finite = predictions[np.isfinite(predictions["actual"]) & np.isfinite(predictions["prediction"])].copy()
    finite["sq_error"] = (finite["actual"] - finite["prediction"]) ** 2
    finite["actual_centered"] = finite["actual"] - finite.groupby(
        ["domain", "model_key", "output_name"], sort=False
    )["actual"].transform("mean")
    finite["constant_sq_error"] = finite["actual_centered"] ** 2
    return finite.groupby(["domain", "model_key", "output_name"], as_index=False, sort=False).agg(
        **{
            f"n_test_{method}": ("actual", "size"),
            f"sse_{method}": ("sq_error", "sum"),
            f"sst_{method}": ("constant_sq_error", "sum"),
        }
    )


def r2_components(loo_predictions: pd.DataFrame, probability_predictions: pd.DataFrame) -> pd.DataFrame:
    components = prediction_components(loo_predictions, "latent").merge(
        prediction_components(probability_predictions, "probability_only"),
        on=["domain", "model_key", "output_name"],
        how="inner",
        validate="one_to_one",
    )
    expected = len(DOMAINS) * len(FIGURE_ORDER) * N_TARGETS
    if len(components) != expected:
        raise ValueError(f"Expected {expected} aligned non-probability outputs; found {len(components)}")
    if not components["n_test_latent"].eq(components["n_test_probability_only"]).all():
        raise ValueError("The two latent estimates use different test row counts")
    if not np.allclose(components["sst_latent"], components["sst_probability_only"]):
        raise ValueError("The two latent estimates do not use identical outcomes")
    components["sst"] = components["sst_latent"]
    return components


def pooled_r2(sse: np.ndarray, sst: np.ndarray) -> float:
    total_sst = float(np.sum(sst))
    if total_sst <= 0.0:
        return np.nan
    return 1.0 - float(np.sum(sse)) / total_sst


def summarize_components(components: pd.DataFrame) -> pd.DataFrame:
    """Pooled R^2 of both predictors per cell, bootstrapping the fifteen targets."""
    rows = []
    for (domain, model), cell in components.groupby(["domain", "model_key"], sort=False):
        cell = cell.sort_values("output_name").reset_index(drop=True)
        sst = cell["sst"].to_numpy(dtype=float)
        seed = BOOTSTRAP_SEED + 1_000 * DOMAIN_INDEX[domain] + MODEL_INDEX[model]
        rng = np.random.default_rng(seed)
        indices = rng.integers(0, len(cell), size=(BOOTSTRAP_REPS, len(cell)))
        sampled_sst = sst[indices].sum(axis=1)

        values = {}
        intervals = {}
        for method in METHOD_ORDER:
            sse = cell[f"sse_{method}"].to_numpy(dtype=float)
            values[method] = pooled_r2(sse, sst)
            draws = 1.0 - sse[indices].sum(axis=1) / sampled_sst
            lower, upper = np.percentile(draws, [2.5, 97.5])
            intervals[method] = (float(lower), float(upper))

        ratio = values["probability_only"] / values["latent"]
        for method in METHOD_ORDER:
            lower, upper = intervals[method]
            rows.append(
                {
                    "domain": domain,
                    "domain_display": DOMAIN_DISPLAY[domain],
                    "model_key": model,
                    "model_display": MODEL_DISPLAY[model],
                    "model_position": MODEL_INDEX[model],
                    "method": method,
                    "method_display": METHOD_DISPLAY[method],
                    "pooled_r2": values[method],
                    "ci_lower": lower,
                    "ci_upper": upper,
                    "probability_only_to_latent_r2_ratio": ratio,
                    "n_outputs": len(cell),
                    "n_test_observations": int(cell["n_test_latent"].sum()),
                    "bootstrap_reps": BOOTSTRAP_REPS,
                    "bootstrap_seed": seed,
                }
            )
    return pd.DataFrame(rows).sort_values(["domain", "model_position", "method"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fit-dir", type=Path, default=FITS_DIR / "main")
    parser.add_argument("--loo-dir", type=Path, default=EVALUATION_DIR / "leave_one_out")
    parser.add_argument("--output-dir", type=Path, default=EVALUATION_DIR / "probability_sufficiency")
    args = parser.parse_args()
    out = ensure(Path(args.output_dir))

    probability_predictions = generate_probability_conditioned_predictions(Path(args.fit_dir))
    loo_predictions = pd.read_csv(args.loo_dir / "leave_one_output_out_predictions.csv", low_memory=False)
    loo_metrics = pd.read_csv(args.loo_dir / "metrics_by_output.csv")

    pairs = concordance_pairs(loo_predictions, loo_metrics, probability_predictions)
    shares = summarize_cindex_pairs(pairs)
    components = r2_components(loo_predictions, probability_predictions)
    summary = summarize_components(components)

    probability_predictions.to_csv(out / "probability_only_predictions.csv", index=False)
    pairs.to_csv(out / "concordance_by_target.csv", index=False)
    shares.to_csv(out / "concordance_share.csv", index=False)
    components.to_csv(out / "r2_by_target.csv", index=False)
    summary.to_csv(out / "pooled_r2.csv", index=False)
    print(f"Wrote results to {out}", flush=True)


if __name__ == "__main__":
    main()
