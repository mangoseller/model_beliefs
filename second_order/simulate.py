"""Synthetic recovery test (plan, Phase 3).

Simulates cases with known latents and a known lambda under the plan's
attribution model, generates the 16 outputs from measurement models fitted
to real condition A data (the released main fit), runs the full pooled fit
and the Phase 5 estimators, and reports whether each target is recovered.

    own belief:        logit z_A;  logit z_B = logit z_A - gap
    about the agent:   g(l) = alpha + kappa * l + u,   u = rho * (z_A - mean) + noise
    logit z_D = g(z_A);  logit z_M = g(z_B);  logit z_C = g(z_B + lambda * gap)

Pair S4 has ignorance-like gaps (independent of z_A); pair F has flip-like
gaps (their sign depends on z_A).  ``--hedge c`` compresses C alone by c
(the case in which the matched-control lambda is expected to be biased; RQ1
assumption 10).  Own-belief outputs come from one measurement model; outputs
about the agent from another real model's fit, so the two fits have
different item parameters, as with real prompts.

    python -m second_order.simulate [--lambdas 0 0.3 0.7 1] [--n 500] [--kappa 0.7] [--rho -0.2]
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import torch

from latent_beliefs.latent.domains import CKD_SPECS
from latent_beliefs.latent.model import FitConfig, load_fit, restore_cell
from second_order import config as C
from second_order.beliefs import latent_rows
from second_order.conditions import CONDITIONS, pair_conditions
from second_order.fit_pooled import fit_all
from second_order.measurement import sample_outputs, sample_prior
from second_order.leakage import leakage_table

OUT = C.RESULTS / "synthetic_recovery"


def measurement(model_key: str):
    parameters, metadata = load_fit(C.RELEASE_MAIN_FIT)
    return restore_cell(parameters, metadata, "ckd", model_key, CKD_SPECS)


def simulate(lam: float, *, n: int, delta_mean: float, delta_sd: float, ab_model: str,
             cd_model: str, seed: int, kappa: float = 1.0, alpha: float = 0.0, rho: float = 0.0,
             hedge: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    m_own, m_agent = measurement(ab_model), measurement(cd_model)
    zA = sample_prior(m_own, n, rng)
    u = rho * (zA - zA.mean()) + (rng.normal(0, 0.3, n) if rho else 0.0)

    def g(value: np.ndarray) -> np.ndarray:  # how an answer about the agent follows a belief
        return alpha + kappa * value + u

    gaps = {
        "S4": rng.normal(delta_mean, delta_sd, n),  # ignorance-like
        # flip-like: the sign depends on the patient's risk, as a hypertension flip does
        "F": np.where(rng.random(n) < 1 / (1 + np.exp(-(zA - np.median(zA)))), 1.0, -1.0)
        * np.abs(rng.normal(0.7, 0.25, n)),
    }
    latent = {"A": zA, "D_john": g(zA), "D_user": g(zA)}
    for pair, gap in gaps.items():
        k = pair_conditions("john", pair)
        zB = zA - gap
        latent[k["B"]] = zB
        latent[k["C"]] = alpha + hedge * (g(zB + lam * gap) - alpha)
        latent[k["M"]] = g(zB)
    latent["C_user"] = latent["C_john"]
    split = rng.choice(["train", "val", "test"], size=n, p=[0.64, 0.16, 0.20])
    case_ids = [f"sim_{i:04d}" for i in range(n)]
    parts = []
    for condition, lz in latent.items():
        own = CONDITIONS[condition].about == "self"
        part = pd.DataFrame(sample_outputs(m_own if own else m_agent, lz, rng))
        part.insert(0, "case_id", case_ids)
        part["condition"], part["true_logit_z"] = condition, lz
        part["split"], part["complete"] = split, True
        part["framing"] = CONDITIONS[condition].framing
        part.insert(0, "model", "synthetic")
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def run_one(lam: float, args, seed: int) -> pd.DataFrame:
    tag = f"{args.tag}_lam{lam}_k{args.kappa}_r{args.rho}_h{args.hedge}_seed{seed}"
    cache = OUT / "latents" / f"{tag}.parquet"
    if cache.exists():
        latents = pd.read_parquet(cache)
    else:
        wide = simulate(lam, n=args.n, delta_mean=args.delta_mean, delta_sd=args.delta_sd,
                        ab_model=args.ab_model, cd_model=args.cd_model, seed=seed, kappa=args.kappa,
                        alpha=args.alpha, rho=args.rho, hedge=args.hedge)
        latents, _, _ = fit_all("synthetic", wide, kind="pooled", base_seed=seed,
                                config=FitConfig(progress_every=0))
        truth = wide.set_index(["case_id", "condition"])["true_logit_z"]
        latents["true_logit_z"] = [truth[(c, k)] for c, k in zip(latents.case_id, latents.condition)]
        cache.parent.mkdir(parents=True, exist_ok=True)
        latents.to_parquet(cache, index=False)
    table = leakage_table(latent_rows(latents), "latent", resamples=args.resamples, seed=seed)
    table.insert(0, "true_lambda", lam)
    table.insert(1, "replicate_seed", seed)
    # Targets under the simulated model.  The original estimator's target is lambda only
    # when kappa = 1 and rho = 0; it is listed so its bias is visible beside the new one.
    targets = {"lambda": lam, "kappa": args.kappa, "tau": args.kappa * (1 - lam), "lambda_0": 0.0,
               "normalised_lambda_original": lam}
    table["target"] = [targets.get(s) if f in ("john", "user") else None
                       for s, f in zip(table.statistic, table.framing)]
    table["covered"] = (table.ci_lower <= table.target) & (table.target <= table.ci_upper)
    table["covered"] = table["covered"].astype(object)
    table.loc[table.target.isna(), "covered"] = None
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lambdas", nargs="+", type=float, default=[0.0, 0.3, 0.7, 1.0])
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--delta-mean", type=float, default=0.0)
    parser.add_argument("--delta-sd", type=float, default=1.0)
    parser.add_argument("--ab-model", default="openai_terra")
    parser.add_argument("--cd-model", default="gemma3_27b")
    parser.add_argument("--resamples", type=int, default=2000)
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--kappa", type=float, default=1.0, help="how strongly answers about the agent follow evidence")
    parser.add_argument("--alpha", type=float, default=0.0, help="constant shift of answers about the agent")
    parser.add_argument("--rho", type=float, default=0.0, help="patient effect: slope of u on z_A")
    parser.add_argument("--hedge", type=float, default=1.0, help="compression of C alone (1 = none)")
    parser.add_argument("--tag", default="main")
    parser.add_argument("--seed-offset", type=int, default=0)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tables = []
    base = int(C.load()["seed"]) + args.seed_offset
    for r in range(args.replicates):
        for i, lam in enumerate(args.lambdas):
            table = run_one(lam, args, seed=base + 1000 * r + i)
            tables.append(table)
            show = table[table.target.notna()][["framing", "pair", "statistic", "target", "estimate", "ci_lower",
                                                 "ci_upper", "covered"]]
            print(f"lambda={lam} replicate={r}\n{show.round(3).to_string(index=False)}", flush=True)
    result = pd.concat(tables, ignore_index=True)
    for k, v in vars(args).items():
        result[f"setting_{k}"] = str(v)
    result.to_csv(OUT / f"recovery_{args.tag}.csv", index=False)


if __name__ == "__main__":
    main()
