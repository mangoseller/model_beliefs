"""Sampling from a fitted latent measurement model, and shrinkage calibration.

Posterior means of logit z are shrunk toward the prior, by different amounts
for different output subsets (13 decisions shrink more than all 16 outputs).
For the leakage regression's outcome side to be unbiased given the true
latent, each subset's posterior mean is de-shrunk with the fit's own
calibration line: simulate logit z from the fitted prior, generate outputs
from the fitted item model, and regress the posterior mean on the true value
(slope s, intercept a); the calibrated belief is (ell - a) / s.  This uses
only the fitted model, never C or D labels.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import torch


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
                a = float(model.binary_intercepts[spec.name])
                b = float(model.binary_slope(spec.name))
                p = 1 / (1 + np.exp(-(a + b * lz)))
                out[spec.name] = (rng.uniform(size=len(z)) < p).astype(float)
            else:
                b = float(model.ordinal_slope(spec.name))
                cuts = model.ordinal_cutpoints(spec).numpy()
                cum = 1 / (1 + np.exp(-(cuts[None, :] - b * lz[:, None])))
                u = rng.uniform(size=len(z))[:, None]
                out[spec.name] = 1.0 + (u > cum).sum(axis=1)
    return out


def calibration(model, *, n: int = 20000, seed: int = 0) -> pd.DataFrame:
    """Per output subset: slope and intercept of posterior mean on true logit z."""
    from second_order.fit_pooled import SUBSETS, posteriors

    rng = np.random.default_rng(seed)
    lz = sample_prior(model, n, rng)
    frame = pd.DataFrame(sample_outputs(model, lz, rng))
    post = posteriors(model, frame)
    rows = []
    for name in SUBSETS:
        slope, intercept = np.polyfit(lz, post[f"ell_{name}"].to_numpy(), 1)
        rows.append({"subset": name, "slope": float(slope), "intercept": float(intercept),
                     "n_simulated": n})
    return pd.DataFrame(rows)
