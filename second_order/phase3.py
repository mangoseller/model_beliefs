"""Phase 3 driver: parse -> matrices -> pooled and separate fits -> latents -> beliefs.

    python -m second_order.phase3 --model gemma3_4b_mlx --smoke
    python -m second_order.phase3 --model openai_terra            # main case set

Writes:
  data/responses/{model}/{condition}.parquet
  data/latents/{model}.parquet           (pooled and separate fits)
  data/latents/{model}_matrix.parquet    (16-output matrix per case x condition)
  data/latents/{model}_parameters.parquet
  data/beliefs/{model}/latent.parquet, direct.parquet
  results/fits/{model}_fit_summary.csv, {model}_anchor_check.csv
  results/retention/{model}.csv
  docs/transcripts_C/{model}/{C_john,C_user}.md   (20 random transcripts each)
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from second_order import beliefs as BL
from second_order import config as C
from second_order.fit_pooled import anchor_check, fit_all
from second_order.prompts_second_order import build_prompt
from second_order.responses import collect, matrix, retention
from second_order.run_generation import select_cases, smoke_cases


def write_transcripts(model: str, long: pd.DataFrame, seed: int, n: int) -> None:
    folder = C.DOCS / "transcripts_C" / model
    folder.mkdir(parents=True, exist_ok=True)
    for condition in ("C_john", "C_user"):
        part = long[(long.condition == condition) & long.text.notna()]
        if part.empty:
            continue
        pick = part.sample(n=min(n, len(part)), random_state=seed)
        with (folder / f"{condition}.md").open("w") as handle:
            handle.write(f"# {model}: {len(pick)} random {condition} transcripts\n\n"
                         f"Refusal-like responses in {condition} (all {len(part)} responses): "
                         f"{int(part.refusal_like.sum())}.\n\n")
            for _, row in pick.sort_values(["case_id", "output"]).iterrows():
                handle.write(f"## {row.case_id} / {row.output} / parse: {row.status}, "
                             f"value: {row.value}\n\n### Prompt\n\n```\n"
                             f"{build_prompt(row.case_id, condition, row.output)}```\n\n"
                             f"### Response\n\n```\n{row.text}\n```\n\n")


def run(model: str, case_ids: list[str], conditions: list[str]) -> None:
    cfg = C.load()
    seed = int(cfg["seed"])
    long = collect(model, case_ids, conditions)
    wide = matrix(long)
    (C.LATENTS).mkdir(parents=True, exist_ok=True)
    wide.to_parquet(C.LATENTS / f"{model}_matrix.parquet", index=False)
    ret = retention(long)
    (C.RESULTS / "retention").mkdir(parents=True, exist_ok=True)
    ret.to_csv(C.RESULTS / "retention" / f"{model}.csv", index=False)
    print(ret.to_string(index=False), flush=True)

    latents, summaries, params = [], [], []
    for kind in ("pooled", "separate"):
        lat, summ, par = fit_all(model, wide, kind=kind, base_seed=seed)
        latents.append(lat), summaries.append(summ), params.append(par)
    latents = pd.concat(latents, ignore_index=True)
    complete_all = wide[wide.complete].groupby("case_id").condition.nunique() == len(conditions)
    latents["complete_all_conditions"] = latents.case_id.map(complete_all).fillna(False)
    latents.to_parquet(C.LATENTS / f"{model}.parquet", index=False)
    pd.concat(params, ignore_index=True).to_parquet(C.LATENTS / f"{model}_parameters.parquet",
                                                    index=False)
    (C.RESULTS / "fits").mkdir(parents=True, exist_ok=True)
    pd.concat(summaries, ignore_index=True).to_csv(C.RESULTS / "fits" / f"{model}_fit_summary.csv",
                                                   index=False)
    anchor_check(latents).to_csv(C.RESULTS / "fits" / f"{model}_anchor_check.csv", index=False)

    BL.write(model, "latent", BL.latent_rows(latents))
    BL.write(model, "direct", BL.direct_rows(wide))
    write_transcripts(model, long, seed, int(cfg["analysis"]["transcripts_per_condition"]))
    print(f"{model}: analysis set (complete in all {len(conditions)} conditions): "
          f"{int(complete_all.sum())} cases", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--conditions", nargs="+", default=None)
    args = parser.parse_args()
    cfg = C.load()
    case_ids = smoke_cases() if args.smoke else select_cases(cfg["main_cases"])
    run(args.model, case_ids, args.conditions or list(cfg["conditions"]))


if __name__ == "__main__":
    main()
