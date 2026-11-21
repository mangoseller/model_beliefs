"""Pooled latent fits and latent export (plan, Phase 3).

A pooled fit shares item parameters and one latent prior across conditions
whose instructions are identical (``conditions.fit_family``): the model's own
belief (A and every B), John's belief (D, every C and every M) and the user's
belief (C-user, D-user).  The matched-control estimator relies on C, M and D
sharing that one prior.  The ``separate`` robustness variant fits every
condition on its own.  Each fit uses the model (`ProbabilisticLatentModel`),
optimiser (`fit_model`) and settings of the first-order main fit:
128-point Gauss-Legendre quadrature, Adam lr 0.02, at most 30,000 epochs,
patience 500, early stopping on validation rows.  Item parameters are
estimated on train-split rows only.

Second runs of the own-belief conditions (``replicate`` 1) are rows of the
"self" fit like any other; they share its item parameters and prior.

Posteriors of logit z are exported for every complete row from five output
subsets: all 16, the three probability reports (``prob``), the 13 decisions
(``dec``), plain + threshold decisions (``dec_h1``, 5 outputs) and bet + cost
decisions (``dec_h2``, 8 outputs).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Sequence

import numpy as np
import pandas as pd
import torch

from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values
from latent_beliefs.latent.model import FitConfig, ProbabilisticLatentModel, fit_model, restore_model
from second_order import config as C

SPEC_NAMES = [s.name for s in CKD_SPECS]
DEC_H1 = ["decision_no_threshold", "threshold_05pct", "template_01_threshold_10pct",
          "template_02_threshold_20pct", "template_03_threshold_33pct"]
DEC_H2 = ["bet_payoff_19to1", "bet_payoff_2to1", "bet_payoff_4to1", "bet_payoff_9to1",
          "cost_fn19", "template_07_cost_fn2", "template_08_cost_fn4", "template_09_cost_fn9"]
SUBSETS = {
    "all": SPEC_NAMES,
    "prob": [s.name for s in CKD_SPECS if s.kind in ("probability", "ordinal")],
    "dec": [s.name for s in CKD_SPECS if s.kind == "binary"],
    "dec_h1": DEC_H1,
    "dec_h2": DEC_H2,
}
assert sorted(DEC_H1 + DEC_H2) == sorted(SUBSETS["dec"])
QUADRATURE_POINTS = 128
PROBABILITY_EPSILON = 1e-4

def groups(kind: str, conditions: Sequence[str]) -> list[tuple[str, ...]]:
    """Fit groups for the conditions present, in a stable order."""
    from second_order.conditions import CONDITIONS, fit_family

    ordered = [c for c in CONDITIONS if c in set(conditions)]
    if kind == "separate":
        return [(c,) for c in ordered]
    return [tuple(c for c in ordered if fit_family(c) == family)
            for family in ("self", "john", "user")
            if any(fit_family(c) == family for c in ordered)]


def stable_seed(base: int, *parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return (int(base) + int(digest[:6], 16)) % (2**31)


def fit_group(wide: pd.DataFrame, conditions: Sequence[str], seed: int,
              config: FitConfig | None = None) -> tuple[ProbabilisticLatentModel, dict[str, Any]]:
    # ``latent_fit`` in the config may override FitConfig fields (the end-to-end test shortens fits).
    config = config or FitConfig(progress_every=0, **(C.load().get("latent_fit") or {}))
    rows = wide[wide.condition.isin(conditions) & wide.complete]
    train, val = rows[rows.split == "train"], rows[rows.split == "val"]
    if train.empty or val.empty:
        raise ValueError(f"{conditions}: no train or validation rows")
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = ProbabilisticLatentModel(CKD_SPECS, quadrature_points=QUADRATURE_POINTS,
                                     probability_epsilon=PROBABILITY_EPSILON)
    info = fit_model(model, likelihood_values(train, CKD_SPECS), likelihood_values(val, CKD_SPECS), config)
    info.update({
        "conditions": "+".join(conditions), "seed": seed, "n_train_rows": len(train),
        "n_val_rows": len(val), "hit_epoch_limit": bool(info["epochs_ran"] >= config.epochs),
        "fit_config": str(asdict(config)),
    })
    return model, info


def posteriors(model: ProbabilisticLatentModel, frame: pd.DataFrame) -> pd.DataFrame:
    """Posterior mean and sd of logit z (and mean z) for each subset of outputs."""
    obs = torch.as_tensor(likelihood_values(frame, CKD_SPECS), dtype=model.dtype)
    out: dict[str, np.ndarray] = {}
    with torch.no_grad():
        log_by_output = model.log_likelihood_by_output(obs)
        prior = model.prior_log_weights()[None, :]
        grid = model.logit_z_grid
        for name, members in SUBSETS.items():
            idx = [SPEC_NAMES.index(m) for m in members]
            joint = log_by_output[:, idx, :].sum(dim=1) + prior
            w = torch.exp(joint - torch.logsumexp(joint, dim=1, keepdim=True))
            mean = w @ grid
            var = (w * (grid[None, :] - mean[:, None]).square()).sum(dim=1)
            out[f"ell_{name}"] = mean.numpy()
            out[f"sd_{name}"] = var.clamp_min(0).sqrt().numpy()
            if name == "all":
                out["z_mean_all"] = (w @ model.z_grid).numpy()
    return pd.DataFrame(out, index=frame.index)


def parameter_frame(model: ProbabilisticLatentModel) -> pd.DataFrame:
    return pd.DataFrame(model.parameter_rows())


def restore(parameters: pd.DataFrame) -> ProbabilisticLatentModel:
    return restore_model(CKD_SPECS, parameters, quadrature_points=QUADRATURE_POINTS,
                         probability_epsilon=PROBABILITY_EPSILON)


def fit_all(model_key: str, wide: pd.DataFrame, *, kind: str, base_seed: int,
            config: FitConfig | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fit every group; return (latents, fit summary, parameters)."""
    latents, summaries, params = [], [], []
    conditions = [c for c in wide.condition.unique()]
    for group in groups(kind, conditions):
        seed = stable_seed(base_seed, model_key, kind, *group)
        model, info = fit_group(wide, group, seed, config)
        rows = wide[wide.condition.isin(group) & wide.complete]
        post = posteriors(model, rows)
        from second_order.measurement import calibration

        cal = calibration(model, seed=seed)
        for _, c in cal.iterrows():
            post[f"cal_{c.subset}"] = (post[f"ell_{c.subset}"] - c.intercept) / c.slope
            info[f"calibration_slope_{c.subset}"] = c.slope
            info[f"calibration_intercept_{c.subset}"] = c.intercept
        keep = [c for c in ("model", "case_id", "condition", "replicate", "framing", "split") if c in rows]
        part = pd.concat([rows[keep], post], axis=1)
        part["numeric_probability"] = rows["probability"].to_numpy()
        part["fit"] = kind
        part["fit_group"] = "+".join(group)
        latents.append(part)
        summaries.append({"model": model_key, "fit": kind, **info})
        p = parameter_frame(model)
        p["fit"], p["fit_group"] = kind, "+".join(group)
        params.append(p)
        print(f"  fit {model_key}/{kind}/{'+'.join(group)}: epochs={info['epochs_ran']} "
              f"val_nll={info['best_validation_mean_nll']:.4f}", flush=True)
    return (pd.concat(latents, ignore_index=True), pd.DataFrame(summaries),
            pd.concat(params, ignore_index=True))


def anchor_check(latents: pd.DataFrame) -> pd.DataFrame:
    """Mean numeric report vs mean posterior z per condition and fit."""
    g = latents.groupby(["model", "fit", "condition"])
    return pd.DataFrame({
        "n": g.size(),
        "mean_numeric_probability": g.numeric_probability.mean(),
        "mean_posterior_z": g.z_mean_all.mean(),
        "mean_ell_all": g.ell_all.mean(),
        "mean_logit_numeric": g.numeric_probability.apply(
            lambda s: np.mean(np.log(np.clip(s, .01, .99)) - np.log1p(-np.clip(s, .01, .99)))),
    }).reset_index()
