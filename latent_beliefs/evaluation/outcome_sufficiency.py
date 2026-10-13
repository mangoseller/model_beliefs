"""Outcome sufficiency of the latent belief (Appendix: outcome sufficiency figure).

For each domain x model and each eligible decision prompt ``D`` (at least six
test cases in each choice class), estimate how much the ground-truth outcome
``Y`` still depends on the decision after conditioning on the all-response
posterior mean ``z`` of the main fit::

    L1 = mean_i  pi(z_i) (1 - pi(z_i)) |mu1(z_i) - mu0(z_i)|

with ``pi(z) = P(D=1|z)``, ``mu_d(z) = P(Y=1|z, D=d)`` estimated by local-linear
regression (Gaussian kernel, bandwidth by 4-fold cross-validation over a fixed
grid).  Per-prompt estimates are normalized by the domain's pooled test-row
outcome prevalence (over the nine paper models) and averaged per model.

Uncertainty: 2,000 joint case-bootstrap draws (same case indices for z, Y and
all prompts; bandwidths fixed; whole draws rejected if any prompt loses
eligibility).

Usage::

    python -m latent_beliefs.evaluation.outcome_sufficiency --workers 4

Run with ``OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1``.  Outputs (in
``EVALUATION_DIR/outcome_sufficiency``): ``outcome_sufficiency_l1.csv`` (model
summaries), ``outcome_sufficiency_l1_per_prompt.csv``, and the bootstrap draws
``outcome_sufficiency_l1_bootstrap.csv.gz``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from latent_beliefs.config import DOMAINS, EVALUATION_DIR, FITS_DIR, SOURCES_DIR, ensure
from latent_beliefs.latent import domains as D
from latent_beliefs.models import FIGURE_ORDER

STEM = "outcome_sufficiency_l1"
GRID = np.geomspace(0.08, 1.25, 9)
OUTCOME_COLUMN = {"werewolf": "label_is_werewolf", "forecasting": "gold_positive"}
CKD_CASES_PATH = SOURCES_DIR / "ckd" / "cases.csv"


# ---------------------------------------------------------------- estimator
def smoother_matrix(x, query, bandwidth):
    """Local-linear smoother as a linear operator (before clipping to [0, 1])."""
    x, query = np.asarray(x), np.asarray(query)
    h = max(float(bandwidth) * max(float(np.std(x)), 1e-6), 1e-8)
    delta = x[None, :] - query[:, None]
    w = np.exp(-0.5 * (delta / h) ** 2)
    a = w.sum(axis=1) + 1e-8
    b = (w * delta).sum(axis=1)
    c = (w * delta**2).sum(axis=1) + 1e-8
    return w * (c[:, None] - b[:, None] * delta) / (a * c - b**2)[:, None]


def predict(x, y, query, bandwidth):
    return np.clip(smoother_matrix(x, query, bandwidth) @ y, 0, 1)


class SmootherPlan:
    """Cache fixed-design operators; choose a separate bandwidth for each y."""

    def __init__(self, x, query, seed):
        self.full = np.stack([smoother_matrix(x, query, h) for h in GRID])
        folds = np.array_split(np.random.default_rng(seed).permutation(len(x)), 4)
        self.cv = np.zeros((len(GRID), len(x), len(x)))
        self.loss_weight = np.zeros(len(x))
        for test in folds:
            train = np.setdiff1d(np.arange(len(x)), test)
            if len(train) < 4 or not len(test):
                continue
            self.loss_weight[test] = 1 / len(test)
            for k, h in enumerate(GRID):
                self.cv[k][np.ix_(test, train)] = smoother_matrix(x[train], x[test], h)
        if self.loss_weight.sum() == 0:
            raise ValueError("Insufficient observations for four-fold CV")

    def fit_predict(self, y):
        y = np.asarray(y, dtype=float)
        one = y.ndim == 1
        if one:
            y = y[:, None]
        losses = np.stack(
            [(self.loss_weight[:, None] * (np.clip(matrix @ y, 0, 1) - y) ** 2).sum(axis=0) for matrix in self.cv]
        )
        chosen = losses.argmin(axis=0)
        result = np.empty((self.full.shape[1], y.shape[1]))
        for k in np.unique(chosen):
            keep = chosen == k
            result[:, keep] = np.clip(self.full[k] @ y[:, keep], 0, 1)
        if one:
            return result[:, 0], float(GRID[chosen[0]])
        return result, GRID[chosen]


def eligible(decisions):
    n = len(decisions)
    counts = decisions.sum(axis=0)
    return (counts >= 6) & (n - counts >= 6)


def estimate(z, y, d, seed=0, bandwidths=None):
    if not eligible(np.asarray(d)[:, None])[0]:
        raise ValueError("need >=6 observations in each choice class")
    one = np.asarray(d) == 1
    if bandwidths is None:
        pi, hp = SmootherPlan(z, z, seed).fit_predict(d)
        m1, h1 = SmootherPlan(z[one], z, seed + 101).fit_predict(y[one])
        m0, h0 = SmootherPlan(z[~one], z, seed + 202).fit_predict(y[~one])
        bandwidths = (hp, h1, h0)
    else:
        hp, h1, h0 = bandwidths
        pi = predict(z, d, z, hp)
        m1 = predict(z[one], y[one], z, h1)
        m0 = predict(z[~one], y[~one], z, h0)
    return float(np.mean(pi * (1 - pi) * np.abs(m1 - m0))), bandwidths


def joint_bootstrap(z, y, decisions, bandwidths, rho, draws, seed):
    """Same case indices for all fixed eligible prompts; reject whole invalid draws."""
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(draws):
        idx = rng.integers(0, len(z), len(z))
        ds = decisions[idx]
        if not eligible(ds).all():
            continue
        vals = [estimate(z[idx], y[idx], ds[:, j], bandwidths=h)[0] for j, h in enumerate(bandwidths)]
        values.append(np.mean(vals) / rho)
    return np.asarray(values)


# ---------------------------------------------------------------- inputs
def load_ckd_labels(path: Path = CKD_CASES_PATH) -> pd.DataFrame:
    """CKD ground truth per case: ``source_prompt_id`` (``<cohort>__<prompt_id>``) and ``ckd``."""

    from latent_beliefs.ckd.cases import read_cases  # imported lazily: pulls in xgboost

    cases = read_cases(path)
    return pd.DataFrame(
        {"source_prompt_id": cases["source_cohort"] + "__" + cases["prompt_id"], "ckd": cases["ckd"]}
    )


def load_cells(fit_dir: Path, ckd_cases: Path):
    posts = pd.read_csv(fit_dir / "latent_posteriors.csv")
    cells = []
    for domain in DOMAINS:
        data = D.load_dataset(domain)
        data = data[data.split.eq("test") & data.model_key.isin(FIGURE_ORDER)].copy()
        if domain == "ckd":
            data = data.merge(load_ckd_labels(ckd_cases), on="source_prompt_id", how="left", validate="many_to_one")
            outcome = "ckd"
        else:
            outcome = OUTCOME_COLUMN[domain]
        keys = ["model_key", "source_prompt_id"]
        data = data.merge(
            posts[posts.domain.eq(domain)][keys + ["z_mean"]], on=keys, how="left", validate="one_to_one"
        )
        if data[["z_mean", outcome]].isna().any().any():
            raise ValueError(f"{domain}: missing latent or ground-truth label")
        names = [spec.name for spec in D.DOMAIN_CONFIGS[domain].output_specs if spec.kind == "binary"]
        assert len(names) == 13, names
        rho = float(data[outcome].mean())
        for model in FIGURE_ORDER:
            group = data[data.model_key.eq(model)].sort_values("source_prompt_id")
            z = group.z_mean.to_numpy(float)
            y = group[outcome].to_numpy(float)
            ds = group[names].to_numpy(float)
            assert len(group) and np.isfinite(ds).all() and np.isin(ds, [0, 1]).all()
            cells.append(dict(domain=domain, model_key=model, z=z, y=y, decisions=ds, names=names, rho=rho))
    return cells


# ---------------------------------------------------------------- analysis
def analyze_cell(job):
    cell, draws, seed = job
    z, y, ds = (cell[k] for k in ("z", "y", "decisions"))
    keep = eligible(ds)
    names = np.asarray(cell["names"])[keep]
    selected = ds[:, keep]
    if not keep.any():
        raise ValueError("No eligible decision prompts")
    family = []
    bandwidths = []
    estimates = []
    for j, d in enumerate(selected.T):
        value, h = estimate(z, y, d, seed=seed + 10 * j)
        estimates.append(value)
        bandwidths.append(h)
        family.append(
            dict(
                output_name=names[j],
                l1_raw=value,
                l1_normalized=value / cell["rho"],
                n_choice_1=int(d.sum()),
                n_choice_0=int(len(d) - d.sum()),
                bandwidth_pi=h[0],
                bandwidth_mu1=h[1],
                bandwidth_mu0=h[2],
                warning="",
            )
        )
    for name, d in zip(np.asarray(cell["names"])[~keep], ds[:, ~keep].T):
        family.append(
            dict(
                output_name=name,
                l1_raw=np.nan,
                l1_normalized=np.nan,
                n_choice_1=int(d.sum()),
                n_choice_0=int(len(d) - d.sum()),
                warning="fewer than six observations in a choice class",
            )
        )
    boot = joint_bootstrap(z, y, selected, bandwidths, cell["rho"], draws, seed + 100_000)
    if len(boot) < min(400, draws // 5):
        raise ValueError(f"Only {len(boot)}/{draws} jointly valid bootstrap draws")
    lo, hi = np.quantile(boot, [0.025, 0.975])
    summary = dict(
        n_test=len(z),
        n_prompts=len(names),
        n_excluded=int((~keep).sum()),
        base_rate=cell["rho"],
        cell_base_rate=float(y.mean()),
        l1_mean=float(np.mean(estimates) / cell["rho"]),
        ci95_low=lo,
        ci95_high=hi,
        bootstrap_draws=draws,
        bootstrap_valid=len(boot),
        seed=seed,
    )
    common = {k: cell[k] for k in ("domain", "model_key")}
    return (
        {**common, **summary},
        [{**common, **row} for row in family],
        [{**common, "replicate": i, "value": float(v)} for i, v in enumerate(boot)],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fit-dir", type=Path, default=FITS_DIR / "main")
    parser.add_argument("--ckd-cases", type=Path, default=CKD_CASES_PATH)
    parser.add_argument("--output-dir", type=Path, default=EVALUATION_DIR / "outcome_sufficiency")
    parser.add_argument("--bootstrap-draws", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260907)
    args = parser.parse_args()
    out = ensure(Path(args.output_dir))
    # Cell seeds follow the (domain, figure-order model) enumeration.
    jobs = [
        (c, args.bootstrap_draws, args.seed + 1000 * i)
        for i, c in enumerate(load_cells(Path(args.fit_dir), Path(args.ckd_cases)))
    ]
    summaries, families, replicates = [], [], []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for summary, family, reps in pool.map(analyze_cell, jobs):
            summaries.append(summary)
            families.extend(family)
            replicates.extend(reps)
            print(
                f"{summary['domain']}/{summary['model_key']}: L1={summary['l1_mean']:.4f} "
                f"[{summary['ci95_low']:.4f}, {summary['ci95_high']:.4f}]",
                flush=True,
            )
    summary, family, reps = map(pd.DataFrame, (summaries, families, replicates))
    summary.to_csv(out / f"{STEM}.csv", index=False)
    family.to_csv(out / f"{STEM}_per_prompt.csv", index=False)
    reps.to_csv(out / f"{STEM}_bootstrap.csv.gz", index=False, compression={"method": "gzip", "mtime": 0})
    print(f"Wrote results to {out}", flush=True)


if __name__ == "__main__":
    main()
