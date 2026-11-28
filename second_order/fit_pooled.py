"""Pooled latent fits of the CKD answers, and latent export.

A pooled fit shares item parameters and one latent prior across the conditions whose
instructions are identical (``conditions.fit_family``): the model's own belief (A and
every B, both replicates) and PERSON's belief (M, D, their one-chart controls and C).  The leakage
estimator relies on C and its references sharing that one prior.

The outputs are the CKD questions of ``latent.batteries`` (``responses.latent_specs``):
the stated probability (Beta likelihood, which anchors the scale), each decision as a
binary item and the Likert and quintile scales as ordered items, every question in each
option order an output of its own, so position bias is absorbed by its own parameters.
Each fit uses the model (``ProbabilisticLatentModel``), optimiser (``fit_model``) and
settings of the first-order main fit: 128-point Gauss-Legendre quadrature, Adam lr
0.02, at most 30,000 epochs, patience 500, early stopping on validation rows.  Item
parameters are estimated on train-split patients only.

With ``latent.anchor_cutoffs``, decisions with a stated cutoff c (threshold, bet and
cost questions: c = 1 / (1 + k)) are fixed to cross 1/2 at c: logit P(screen | z) =
b (logit z - logit c).  By default their intercepts are free, as in the paper.

Posteriors of logit z (mean and sd) are exported for every usable row (``responses.matrix``).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Sequence

import numpy as np
import pandas as pd
import torch

from archive.smolin_wilder_replication.latent.domains import likelihood_values
from archive.smolin_wilder_replication.latent.model import FitConfig, ProbabilisticLatentModel, fit_model
from second_order import config as C
from second_order.beliefs import clip_logit
from second_order.responses import configured_specs

QUADRATURE_POINTS = 128
PROBABILITY_EPSILON = 1e-4


class AnchoredLatentModel(ProbabilisticLatentModel):
    """The paper's model with each stated-cutoff decision crossing 1/2 at its cutoff."""

    def binary_intercept(self, spec):
        if spec.nominal_threshold is None:
            return super().binary_intercept(spec)
        c = self.z_grid.new_tensor(spec.nominal_threshold)
        return -self.binary_slope(spec.name) * torch.logit(c)


def new_model() -> ProbabilisticLatentModel:
    cls = AnchoredLatentModel if C.load()["latent"].get("anchor_cutoffs") else ProbabilisticLatentModel
    return cls(configured_specs(), quadrature_points=QUADRATURE_POINTS, probability_epsilon=PROBABILITY_EPSILON)


def groups(conditions: Sequence[str]) -> list[tuple[str, ...]]:
    """Fit groups (one per fit family) for the conditions present, in a stable order."""
    from second_order.conditions import CONDITIONS, fit_family

    ordered = [c for c in CONDITIONS if c in set(conditions)]
    return [group for family in ("self", "person")
            if (group := tuple(c for c in ordered if fit_family(c) == family))]


def stable_seed(base: int, *parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return (int(base) + int(digest[:6], 16)) % (2**31)


def fit_group(wide: pd.DataFrame, conditions: Sequence[str], seed: int,
              config: FitConfig | None = None) -> tuple[ProbabilisticLatentModel, dict[str, Any]]:
    # ``latent_fit`` in the config may override FitConfig fields (the end-to-end test shortens fits).
    config = config or FitConfig(progress_every=0, **(C.load().get("latent_fit") or {}))
    rows = wide[wide.condition.isin(conditions) & wide.usable]
    train, val = rows[rows.split == "train"], rows[rows.split == "val"]
    if train.empty or val.empty:
        raise ValueError(f"{conditions}: no train or validation rows")
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = new_model()
    specs = model.specs
    info = fit_model(model, likelihood_values(train, specs), likelihood_values(val, specs), config)
    info.update({
        "conditions": "+".join(conditions), "seed": seed, "n_train_rows": len(train),
        "n_val_rows": len(val), "hit_epoch_limit": bool(info["epochs_ran"] >= config.epochs),
        "fit_config": str(asdict(config)),
    })
    return model, info


@torch.no_grad()
def posteriors(model: ProbabilisticLatentModel, frame: pd.DataFrame, batch_size: int = 256, *,
               predict_decision: bool = True) -> pd.DataFrame:
    """Posterior summaries of logit z per row; ``z_without_decision`` leaves both screening decisions out."""
    parts = []
    predictors = [i for i, spec in enumerate(model.specs) if not spec.name.startswith("decision/")]
    prior = model.prior_log_weights()
    for start in range(0, len(frame), batch_size):
        batch = frame.iloc[start:start + batch_size]
        obs = torch.as_tensor(likelihood_values(batch, model.specs), dtype=model.dtype)
        logp = model.log_likelihood_by_output(obs)
        w = (logp.sum(1) + prior).softmax(1)
        mean = w @ model.logit_z_grid
        var = (w * (model.logit_z_grid - mean[:, None]).square()).sum(1)
        values = {"ell": mean.numpy(), "sd": var.sqrt().numpy(), "z_mean": (w @ model.z_grid).numpy()}
        if predict_decision:
            withheld = (logp[:, predictors].sum(1) + prior).softmax(1) @ model.z_grid
            values["z_without_decision"] = withheld.numpy()
        parts.append(pd.DataFrame(values, index=batch.index))
    return pd.concat(parts)


# Shrinkage calibration: posterior means shrink toward the prior, by different amounts per fit.
# Simulate logit z from the fitted prior and outputs from the fitted items, regress the posterior
# mean on the truth (slope s, intercept a), and de-shrink: (ell - a) / s.  Never uses C or D labels.
def sample_prior(model, n: int, rng: np.random.Generator) -> np.ndarray:
    with torch.no_grad():
        w = torch.exp(model.prior_log_weights()).numpy()
        grid = model.logit_z_grid.numpy()
    idx = rng.choice(len(grid), size=n, p=w / w.sum())
    spacing = np.diff(grid, prepend=grid[0] - (grid[1] - grid[0]))
    return grid[idx] + rng.uniform(-0.5, 0.5, n) * spacing[idx]


def sample_outputs(model, lz: np.ndarray, rng: np.random.Generator) -> dict[str, np.ndarray]:
    z = 1 / (1 + np.exp(-lz))
    out = {}
    with torch.no_grad():
        for spec in model.specs:
            if spec.kind == "probability":
                phi = float(model.probability_precision)
                out[spec.name] = np.clip(rng.beta(z * phi, (1 - z) * phi), 0.0, 1.0)
            elif spec.kind == "binary":
                logits = np.interp(lz, model.logit_z_grid.numpy(), model.binary_logits(spec).numpy())
                out[spec.name] = (rng.uniform(size=len(z)) < 1 / (1 + np.exp(-logits))).astype(float)
            else:
                b = float(model.ordinal_slope(spec.name))
                cuts = model.ordinal_cutpoints(spec).numpy()
                cum = 1 / (1 + np.exp(-(cuts[None, :] - b * lz[:, None])))
                u = rng.uniform(size=len(z))[:, None]
                out[spec.name] = 1.0 + (u > cum).sum(axis=1)
    return out


def calibration(model, *, n: int = 20000, seed: int = 0) -> tuple[float, float]:
    """Slope and intercept of the posterior mean of logit z on its true value, by simulation."""
    rng = np.random.default_rng(seed)
    lz = sample_prior(model, n, rng)
    post = posteriors(model, pd.DataFrame(sample_outputs(model, lz, rng)), predict_decision=False)
    slope, intercept = np.polyfit(lz, post["ell"].to_numpy(), 1)
    return float(slope), float(intercept)


def parameter_frame(model: ProbabilisticLatentModel) -> pd.DataFrame:
    rows = model.parameter_rows()
    for row in list(rows):
        if row["parameter"] != "nominal_threshold":
            continue
        question = row["output_name"].partition("/")[0]
        family, _, k = question.partition("_")
        if family in {"threshold", "bet", "cost"}:
            rows.append(dict(row, parameter="payoff_threshold", value=1 / (1 + float(k))))
        if family == "threshold":
            rows.append(dict(row, parameter="printed_threshold"))
    return pd.DataFrame(rows)


def fit_all(model_key: str, wide: pd.DataFrame, *, base_seed: int,
            config: FitConfig | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fit every group; return (latents, fit summary, parameters)."""
    latents, summaries, params = [], [], []
    for group in groups(wide.condition.unique()):
        seed = stable_seed(base_seed, model_key, "pooled", *group)
        rows = wide[wide.condition.isin(group) & wide.usable]
        if not {"train", "val"} <= set(rows.split):
            summaries.append({"model": model_key, "conditions": "+".join(group), "seed": seed,
                              "status": "insufficient training or validation cases", "n_train_rows": (rows.split == "train").sum(),
                              "n_val_rows": (rows.split == "val").sum()})
            continue
        model, info = fit_group(wide, group, seed, config)
        post = posteriors(model, rows)
        slope, intercept = calibration(model, seed=seed)
        post["cal"] = (post["ell"] - intercept) / slope
        info["calibration_slope"], info["calibration_intercept"] = slope, intercept
        keep = [c for c in ("model", "case_id", "condition", "replicate", "split") if c in rows]
        part = pd.concat([rows[keep], post], axis=1)
        part["numeric_probability"] = rows["probability/normal"].to_numpy()
        part["fit_group"] = "+".join(group)
        latents.append(part)
        summaries.append({"model": model_key, "status": "fitted", **info})
        p = parameter_frame(model)
        p["fit_group"] = "+".join(group)
        params.append(p)
        print(f"  fit {model_key}/{'+'.join(group)}: epochs={info['epochs_ran']} "
              f"val_nll={info['best_validation_mean_nll']:.4f}", flush=True)
    keys = ["model", "case_id", "condition", "replicate", "split"]
    empty_latents = wide[keys].iloc[:0].copy()
    for col in ("cal", "ell", "sd", "z_mean", "z_without_decision", "numeric_probability"):
        empty_latents[col] = pd.Series(dtype=float)
    return (pd.concat(latents, ignore_index=True) if latents else empty_latents, pd.DataFrame(summaries),
            pd.concat(params, ignore_index=True) if params else pd.DataFrame(columns=["fit_group", "output_name", "parameter", "value"]))


def anchor_check(latents: pd.DataFrame) -> pd.DataFrame:
    """Mean numeric report vs mean posterior z per condition."""
    g = latents.groupby(["model", "condition"])
    return pd.DataFrame({
        "n": g.size(),
        "mean_numeric_probability": g.numeric_probability.mean(),
        "mean_posterior_z": g.z_mean.mean(),
        "mean_ell": g.ell.mean(),
        "mean_logit_numeric": g.numeric_probability.apply(lambda s: pd.Series(clip_logit(s)).mean()),
    }).reset_index()
