"""Synthetic recovery test (plan, Phase 3).

Simulates cases with known latents and a known lambda, generates the 16
outputs from measurement models fitted to real condition A data (the
released main fit), runs the full pooled fit and the Phase 5 estimators, and
checks that each lambda is recovered inside its 95% interval.

    logit z_A ~ fitted prior of the A/B measurement model
    logit z_B = logit z_A - delta,   delta ~ Normal(delta_mean, delta_sd)
    logit z_C = (1 - lambda) logit z_B + lambda logit z_A
    logit z_D = logit z_A

A and B outputs come from one measurement model; C and D from another real
model's fit, so the C/D fit has different item parameters from the A/B fit
(as with real prompts), and comparability rests on the probability anchor.

    python -m second_order.simulate [--lambdas 0 0.3 0.7 1] [--n 500]
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
from second_order.fit_pooled import fit_all
from second_order.measurement import sample_outputs, sample_prior
from second_order.leakage import leakage_table

OUT = C.RESULTS / "synthetic_recovery"


def measurement(model_key: str):
    parameters, metadata = load_fit(C.RELEASE_MAIN_FIT)
    return restore_cell(parameters, metadata, "ckd", model_key, CKD_SPECS)


def simulate(lam: float, *, n: int, delta_mean: float, delta_sd: float, ab_model: str,
             cd_model: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    m_ab, m_cd = measurement(ab_model), measurement(cd_model)
    zA = sample_prior(m_ab, n, rng)
    zB = zA - rng.normal(delta_mean, delta_sd, n)
    zC = (1 - lam) * zB + lam * zA
    latent = {"A": zA, "B": zB, "C_john": zC, "D_john": zA, "C_user": zC, "D_user": zA}
    split = rng.choice(["train", "val", "test"], size=n, p=[0.64, 0.16, 0.20])
    case_ids = [f"sim_{i:04d}" for i in range(n)]
    parts = []
    for condition, lz in latent.items():
        outputs = sample_outputs(m_ab if condition in ("A", "B") else m_cd, lz, rng)
        part = pd.DataFrame(outputs)
        part.insert(0, "case_id", case_ids)
        part["condition"], part["true_logit_z"] = condition, lz
        part["split"], part["complete"] = split, True
        part["framing"] = "self" if condition in ("A", "B") else condition.split("_")[1]
        part.insert(0, "model", "synthetic")
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def run_one(lam: float, args, seed: int) -> pd.DataFrame:
    cache = OUT / "latents" / f"{args.tag}_lam{lam}_seed{seed}.parquet"
    if cache.exists():
        latents = pd.read_parquet(cache)
    else:
        wide = simulate(lam, n=args.n, delta_mean=args.delta_mean, delta_sd=args.delta_sd,
                        ab_model=args.ab_model, cd_model=args.cd_model, seed=seed)
        latents, _, _ = fit_all("synthetic", wide, kind="pooled", base_seed=seed,
                                config=FitConfig(progress_every=0))
        truth = wide.set_index(["case_id", "condition"])["true_logit_z"]
        latents["true_logit_z"] = [truth[(c, k)] for c, k in zip(latents.case_id, latents.condition)]
        cache.parent.mkdir(parents=True, exist_ok=True)
        latents.to_parquet(cache, index=False)
    table = leakage_table(latent_rows(latents), "latent", resamples=args.resamples, seed=seed)
    table.insert(0, "true_lambda", lam)
    table.insert(1, "replicate_seed", seed)
    truth = {"lambda_C_john": lam, "lambda_C_user": lam, "normalised_lambda_john": lam,
             "normalised_lambda_user": lam, "lambda_D_john": 1.0, "lambda_D_user": 1.0,
             "lambda_0": 0.0, "C_john_two_weight_wA": lam, "C_john_two_weight_wB": 1 - lam,
             "C_user_two_weight_wA": lam, "C_user_two_weight_wB": 1 - lam}
    table["target"] = table["statistic"].map(truth)
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
            show = table[table.target.notna()][["statistic", "estimate", "ci_lower", "ci_upper", "covered"]]
            print(f"lambda={lam} replicate={r}\n{show.round(3).to_string(index=False)}", flush=True)
    result = pd.concat(tables, ignore_index=True)
    for k, v in vars(args).items():
        result[f"setting_{k}"] = str(v)
    result.to_csv(OUT / f"recovery_{args.tag}.csv", index=False)


if __name__ == "__main__":
    main()
