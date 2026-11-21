"""Sampling from a fitted latent measurement model, and shrinkage calibration.

Posterior means of logit z are shrunk toward the prior, by different amounts in
different fits.  For the regressions behind rho, phi and tau to be unbiased given the
true latent, each posterior mean is de-shrunk with its fit's own calibration line: simulate logit z from the fitted prior, generate outputs
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
    from second_order.fit_pooled import posteriors

    rng = np.random.default_rng(seed)
    lz = sample_prior(model, n, rng)
    post = posteriors(model, pd.DataFrame(sample_outputs(model, lz, rng)), predict_decision=False)
    slope, intercept = np.polyfit(lz, post["ell"].to_numpy(), 1)
    return float(slope), float(intercept)
