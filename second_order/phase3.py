"""Parse, fit and export beliefs: responses -> matrices -> pooled fits -> latents -> beliefs.

    python -m second_order.phase3 --model qwen3_8b                # CKD and the bespoke dataset

Writes, for CKD:
  data/latents/{model}.parquet, {model}_matrix.parquet, {model}_parameters.parquet
  data/beliefs/{model}/ckd_latent.parquet, ckd_direct.parquet
  outputs/fits/{model}_fit_summary.csv, {model}_anchor_check.csv
  outputs/checks/{model}.csv                     (every check and role probe, scored)
for the bespoke dataset (numeric probability only, no latent fit):
  data/beliefs/{model}/bespoke_direct.parquet    (mean sample logit per item, condition and run)
  outputs/comprehension/{model}.csv              (the follow-ups, scored)
and for both:
  data/responses/{model}/{dataset}/{condition}.parquet
  outputs/retention/{model}.csv, outputs/transcripts/{model}/{dataset}_{condition}.md
"""

from __future__ import annotations

import argparse

import pandas as pd

from second_order import beliefs as BL
from second_order import config as C
from second_order import datasets as DS
from second_order.fit_pooled import anchor_check, fit_all
from second_order.responses import checks, collect, comprehension, matrix, retention
from second_order.run_generation import dataset_cases


def write_transcripts(model: str, dataset: str, long: pd.DataFrame, seed: int, n: int) -> None:
    """Random parsed and unparsed answers per C condition, with their prompts."""
    folder = C.RESULTS / "transcripts" / model
    folder.mkdir(parents=True, exist_ok=True)
    for condition in sorted(c for c in long.condition.unique() if c.startswith("C")):
        part = long[(long.condition == condition) & long.text.notna()]
        if part.empty:
            continue
        pick = part.sample(n=min(n, len(part)), random_state=seed)
        with (folder / f"{dataset}_{condition}.md").open("w") as handle:
            handle.write(f"# {model}: {len(pick)} random {dataset} {condition} transcripts\n\n")
            for _, r in pick.sort_values(["case_id", "output"]).iterrows():
                prompt = DS.row(dataset, r.case_id, condition, r.output, r.replicate)["prompt"]
                handle.write(f"## {r.case_id} / {r.output} / {r.status}, value {r.value}\n\n### Prompt\n\n"
                             f"```\n{prompt}\n```\n\n### Response\n\n```\n{r.text}\n```\n\n")


def run_ckd(model: str, long: pd.DataFrame, seed: int) -> None:
    wide = matrix(long)
    C.LATENTS.mkdir(parents=True, exist_ok=True)
    wide.to_parquet(C.LATENTS / f"{model}_matrix.parquet", index=False)
    latents, summaries, params = fit_all(model, wide, base_seed=seed)
    latents.to_parquet(C.LATENTS / f"{model}.parquet", index=False)
    params.to_parquet(C.LATENTS / f"{model}_parameters.parquet", index=False)
    (C.RESULTS / "fits").mkdir(parents=True, exist_ok=True)
    summaries.to_csv(C.RESULTS / "fits" / f"{model}_fit_summary.csv", index=False)
    anchor_check(latents).to_csv(C.RESULTS / "fits" / f"{model}_anchor_check.csv", index=False)
    BL.write(model, "latent", BL.latent_rows(latents), "ckd")
    BL.write(model, "direct", BL.direct_rows(long), "ckd")
    scored = checks(long)
    if not scored.empty:
        (C.RESULTS / "checks").mkdir(parents=True, exist_ok=True)
        scored.to_csv(C.RESULTS / "checks" / f"{model}.csv", index=False)
    print(f"{model}: {int(wide.usable.sum())} of {len(wide)} CKD rows usable for the latent fit "
          f"({int(wide.complete.sum())} complete)", flush=True)


def run_bespoke(model: str, long: pd.DataFrame, case_ids: list[str]) -> pd.DataFrame:
    BL.write(model, "direct", BL.direct_rows(long), "bespoke")
    comp = comprehension(model, "bespoke", case_ids)
    if not comp.empty:
        (C.RESULTS / "comprehension").mkdir(parents=True, exist_ok=True)
        comp.drop(columns="text").to_csv(C.RESULTS / "comprehension" / f"{model}.csv", index=False)

    return comp


def run(model: str, datasets: list[str] | None = None, cases: str | None = None) -> None:
    """Every dataset of the model (``cases``: 'all', an integer or a file of case ids)."""
    seed = int(C.load()["seed"])
    rets = []
    for dataset in datasets or C.datasets_for(model):
        ids = dataset_cases(dataset, cases)
        long = collect(model, dataset, ids)
        if dataset == "ckd":
            run_ckd(model, long, seed)
        else:
            rets.append(retention(run_bespoke(model, long, ids)))
        rets.append(retention(long))
        write_transcripts(model, dataset, long, seed, int(C.load()["analysis"]["transcripts_per_condition"]))
    ret = pd.concat(rets, ignore_index=True)
    (C.RESULTS / "retention").mkdir(parents=True, exist_ok=True)
    ret.to_csv(C.RESULTS / "retention" / f"{model}.csv", index=False)
    print(ret.to_string(index=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", nargs="+", default=None)
    parser.add_argument("--cases", default=None, help="'all', an integer, or a file of case ids")
    args = parser.parse_args()
    run(args.model, args.dataset, args.cases)


if __name__ == "__main__":
    main()
