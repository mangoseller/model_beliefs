"""Parse, fit and export beliefs: responses -> matrices -> pooled and separate fits -> latents -> beliefs.

    python -m second_order.phase3 --model gemma3_4b_mlx --smoke
    python -m second_order.phase3 --model openai_terra            # main case set
    python -m second_order.phase3 --model qwen3_8b                # CKD and the bespoke dataset

Writes, for CKD:
  data/responses/{model}/{condition}.parquet
  data/latents/{model}.parquet           (pooled and separate fits)
  data/latents/{model}_matrix.parquet    (16-output matrix per case x condition x run)
  data/latents/{model}_parameters.parquet
  data/beliefs/{model}/latent.parquet, direct.parquet
  outputs/fits/{model}_fit_summary.csv, {model}_anchor_check.csv
  outputs/transcripts_C/{model}/{condition}.md   (20 random transcripts per C condition)
for the bespoke dataset (numeric probability only, no latent fit):
  data/responses/{model}/bespoke/{condition}.parquet
  data/beliefs/{model}/bespoke_direct.parquet   (mean sample logit per item, condition and run)
and for both:
  outputs/retention/{model}.csv
  data/responses/{model}[/{dataset}]/comprehension.parquet, outputs/comprehension/{model}.csv
"""

from __future__ import annotations

import argparse

import pandas as pd

from second_order import beliefs as BL
from second_order import config as C
from second_order.fit_pooled import anchor_check, fit_all
from second_order.conditions import CONDITIONS
from second_order.prompts_second_order import prompt_for
from second_order.responses import collect, comprehension, comprehension_summary, matrix, retention
from second_order.run_generation import dataset_cases


def write_transcripts(model: str, long: pd.DataFrame, seed: int, n: int) -> None:
    folder = C.RESULTS / "transcripts_C" / model
    folder.mkdir(parents=True, exist_ok=True)
    for condition in [c for c in CONDITIONS if CONDITIONS[c].role == "C"]:
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
                             f"{prompt_for(model, row.case_id, condition, row.output)}```\n\n"
                             f"### Response\n\n```\n{row.text}\n```\n\n")


def run_ckd(model: str, case_ids: list[str], conditions: list[str], seed: int) -> pd.DataFrame:
    cfg = C.load()
    long = collect(model, case_ids, conditions)
    wide = matrix(long)
    (C.LATENTS).mkdir(parents=True, exist_ok=True)
    wide.to_parquet(C.LATENTS / f"{model}_matrix.parquet", index=False)

    latents, summaries, params = [], [], []
    for kind in ("pooled", "separate"):
        lat, summ, par = fit_all(model, wide, kind=kind, base_seed=seed)
        latents.append(lat), summaries.append(summ), params.append(par)
    latents = pd.concat(latents, ignore_index=True)
    # Informational only: each analysis uses the cases complete for its own pair (leakage.py).
    main_run = wide[wide.complete & (wide.replicate == 0)]
    complete_all = main_run.groupby("case_id").condition.nunique() == len(conditions)
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
    write_transcripts(model, long[long.replicate == 0], seed, int(cfg["analysis"]["transcripts_per_condition"]))
    print(f"{model}: cases complete in all {len(conditions)} conditions: "
          f"{int(complete_all.sum())} cases", flush=True)
    return long


def run_bespoke(model: str, case_ids: list[str], conditions: list[str]) -> pd.DataFrame:
    long = collect(model, case_ids, conditions, dataset="bespoke")
    BL.write(model, "direct", BL.sample_mean_rows(long), dataset="bespoke")
    return long


def run(model: str, conditions: list[str], datasets: list[str] | None = None, cases: str | None = None,
        smoke: bool = False) -> None:
    """Every dataset of the model (``cases``: 'all', an integer or a file of case ids)."""
    seed = int(C.load()["seed"])
    rets, comps = [], []
    for dataset in datasets or C.datasets_for(model):
        ids = dataset_cases(dataset, cases, smoke)
        long = run_ckd(model, ids, conditions, seed) if dataset == "ckd" else run_bespoke(model, ids, conditions)
        rets.append(retention(long))
        comps.append(comprehension(model, dataset, ids, conditions))
    ret = pd.concat(rets, ignore_index=True)
    (C.RESULTS / "retention").mkdir(parents=True, exist_ok=True)
    ret.to_csv(C.RESULTS / "retention" / f"{model}.csv", index=False)
    print(ret.to_string(index=False), flush=True)
    comp = pd.concat(comps, ignore_index=True)
    if not comp.empty:
        (C.RESULTS / "comprehension").mkdir(parents=True, exist_ok=True)
        comp.drop(columns="text").to_csv(C.RESULTS / "comprehension" / f"{model}.csv", index=False)
        print(comprehension_summary(comp).to_string(index=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", nargs="+", default=None)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--cases", default=None, help="'all', an integer, or a file of case ids")
    parser.add_argument("--conditions", nargs="+", default=None)
    args = parser.parse_args()
    run(args.model, args.conditions or C.scheduled_conditions(args.model), args.dataset, args.cases, args.smoke)


if __name__ == "__main__":
    main()
