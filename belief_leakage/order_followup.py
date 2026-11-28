"""Paired CKD chart-order / authority experiment on the released prompts.

Every CKD patient's C, M and D prompts are shown in both block orders (``own_first``: the model's own
record first; ``person_first``: PERSON's record first), in three settings: the partial records with their
original wording, the false records with the "verified" cue, and the false records with the cue removed
(``neutral``). Only whole record blocks move; headers, facts and their order within a block are kept. A and
B take no order. The full 31-output latent battery is asked of every cell.

A prompt identical to one of the base run's is reused (its archived answer and activations), and identical
new prompts within the plan are one item, so only missing calls are made:

    python -m belief_leakage.order_followup prepare --run runs/<name> --base-run <base run dir or tarball> \\
        --base-activations <base run dir or activations tar> [--models qwen3_8b qwen3_14b]
    python -m belief_leakage.order_followup status   --run runs/<name>
    python -m belief_leakage.order_followup generate --run runs/<name> --model qwen3_8b
    python -m belief_leakage.order_followup capture  --run runs/<name> --model qwen3_8b
    python -m belief_leakage.order_followup export   --run runs/<name> --model qwen3_8b

``run_plan`` handles the plan files, caches, scoring and activation capture for both CKD follow-ups.
"""
from __future__ import annotations

import argparse
import fcntl
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from belief_leakage import config as C, datasets as DS, run_generation as G, run_plan
# Existing run scripts also import these helpers from order_followup.
from belief_leakage.run_plan import (atomic_json, capture, capture_items, configure, copy_baseline, digest,
                                     file_sha256, install_config, item_from, new_items, read_member, read_plan,
                                     score, scored_trials, stream_member, write_plan)

CUE = " Your chart gives the verified patient information."
ORDERS = ("own_first", "person_first")
PROTOCOL = "ckd-order-authority-v1"
# The base run asked for reasoning before the choice; every reused prompt is verified against its cache hash.
CHOICE_INSTRUCTION = (DS.ARCHIVED_CHOICE_ASK, DS.CHOICE_ASK)


def release_frame(cases=None) -> pd.DataFrame:
    """The released CKD rows (of ``cases``, or every patient), with the base run's choice instruction."""
    frame = DS.rows("ckd", ["main"]).copy()
    frame["prompt"] = frame.prompt.str.replace(*CHOICE_INSTRUCTION, regex=False)
    return frame if cases is None else frame[frame.case_id.isin(set(cases))]


def transform(prompt: str, order: str | None = None, neutral: bool = False) -> str:
    """Move whole record blocks only (``order``) and optionally drop the verified-information cue."""
    blocks = prompt.split("\n\n")
    if order is not None:
        if len(blocks) < 3:
            raise ValueError("expected two record blocks followed by a question")
        own = [b for b in blocks[:2] if b.startswith("Your record for this patient:")]
        person = [b for b in blocks[:2] if "has only the following record for this patient" in b.split("\n")[0]]
        if len(own) != 1 or len(person) != 1 or order not in ORDERS:
            raise ValueError("unrecognized record layout")
        blocks[:2] = [own[0], person[0]] if order == "own_first" else [person[0], own[0]]
    result = "\n\n".join(blocks)
    if neutral:
        if result.count(CUE) != 1:
            raise ValueError("neutral condition requires exactly one verified-information cue")
        result = result.replace(CUE, "")
    return result


def order_trials(model, frame):
    """Yield each trial and its candidate item in the order used to assign canonical keys."""
    for setting, wording in [("partial", "original"), ("false", "verified"), ("false", "neutral")]:
        for role in ["A", "B", "M", "C", "D"]:
            base = role + ("_F" if setting == "false" else "")
            for r in frame[frame.condition == base].to_dict("records"):
                for order in ORDERS:
                    ordered = role in "MCD"
                    prompt = transform(r["prompt"], order if ordered else None, wording == "neutral")
                    condition = f"followup_v1__{setting}__{wording}__{order if ordered else 'shared'}__{role}"
                    candidate = G.Item(model, r["case_id"], condition, r["output"], r["replicate"], prompt)
                    trial = dict(model=model, dataset="ckd", case_id=r["case_id"], setting=setting,
                                 wording=wording, block_order=order, source_block_order=r["block_order"],
                                 role=role, base_condition=base, output=r["output"], replicate=r["replicate"],
                                 sample=0, check_answer=r["check_answer"], check_fact=r["check_fact"],
                                 check_relation=r["check_relation"])
                    yield trial, candidate


def make_plan(model: str, cases=None) -> tuple[list[dict], dict]:
    """(trials, items): every trial of the design, and one item per distinct prompt (per patient, output and
    replicate; replicates are never merged). A prompt identical to a base-run prompt keeps the base run's key."""
    frame = release_frame(cases)
    builder = run_plan.Builder(model, run_plan.released_items(model, frame.to_dict("records")))
    for trial, item in order_trials(model, frame):
        builder.add(trial, item)
    return builder.trials, builder.items


# Stages
def prepare(args) -> None:
    root = args.run.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # The archived config carries the pinned weight revisions and sampling settings.
    install_config(root, read_member(args.base_run, "config.yaml"))
    trials, items, signatures = run_plan.collect_models(args.models, make_plan)
    plan = dict(protocol=PROTOCOL, models=args.models, settings=signatures,
                base_run=str(args.base_run.resolve()), base_activations=str(args.base_activations.resolve()),
                trials=trials, items=items)
    write_plan(root, plan)
    run_plan.finish_prepare(root, plan, summary, require_baseline=True)


def summary(plan: dict) -> dict:
    result = {}
    for model in plan["models"]:
        items = [v for v in plan["items"].values() if v["model"] == model]
        new = [v for v in items if v["source"] == "new"]
        result[model] = dict(reused_generations=len(items) - len(new), new_generations=len(new),
                             new_by_output=dict(Counter(v["output"] for v in new)),
                             new_activation_rows=len(capture_items(plan, model)))
    return result


def status(root: Path) -> dict:
    return run_plan.status(read_plan(root), summary)


def generate(root: Path, model: str) -> None:
    plan = read_plan(root)
    if model not in plan["models"]:
        raise ValueError(f"model {model} is not in the prepared plan")
    items = new_items(plan, model)
    # Small durable batches limit repeated work after an interruption; sampling is unchanged.
    if C.model_config(model)["backend"] == "vllm":
        C.model_config(model)["batch_size"] = 128
    run_plan.generate_items(root, model, items, f"{model}/order-followup")


def export(root: Path, model: str) -> None:
    plan = read_plan(root)
    rows = [trial | dict(status=result.status, value=result.value if result.status == "parsed" else np.nan,
                         text=(record or {}).get("text"), prompt=item.prompt, condition=item.condition,
                         fit_family="self" if trial["role"] in "AB" else "person")
            for trial, item, record, result in scored_trials(root, plan, model)]
    long = pd.DataFrame(rows)
    out = root / "outputs"
    out.mkdir(exist_ok=True)
    long.to_parquet(out / f"{model}_responses.parquet", index=False)
    belief = long[~long.output.str.startswith("check/")]
    ids = ["model", "case_id", "setting", "wording", "block_order", "role", "replicate", "fit_family"]
    wide = belief.pivot(index=ids, columns="output", values="value").reset_index()
    wide.to_parquet(out / f"{model}_matrix.parquet", index=False)
    long.groupby(["setting", "wording", "block_order", "role", "status"]).size().rename("count").to_csv(
        out / f"{model}_retention.csv")
    long[long.output.str.startswith("check/")].to_parquet(out / f"{model}_checks.parquet", index=False)
    refs = pd.read_parquet(root / f"baseline_activations_{model}.parquet").to_dict("records")
    for data in capture_items(plan, model):
        folder = Path("data/activations") / model / "ckd" / data["key"]
        meta = root / folder / "meta.json"
        refs.append(dict(item_key=data["key"], source="new", array_member=str(folder / "acts.npy"), row=0,
                         complete=meta.exists()) | (json.loads(meta.read_text()) if meta.exists() else {}))
    atomic_json(out / f"{model}_activation_refs.json", refs)
    print(f"{model}: exported {len(long)} trial rows, {len(wide)} belief vectors; "
          f"missing answers: {(long.status == 'not_generated').sum()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=["prepare", "status", "generate", "capture", "export"])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--base-run", type=Path)
    parser.add_argument("--base-activations", type=Path)
    parser.add_argument("--models", nargs="+", default=["qwen3_8b", "qwen3_14b"])
    parser.add_argument("--model")
    args = parser.parse_args()
    args.run = args.run.resolve()
    args.run.mkdir(parents=True, exist_ok=True)
    with (args.run / ".followup.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.stage == "prepare":
            if not args.base_run or not args.base_activations:
                parser.error("prepare needs --base-run and --base-activations")
            prepare(args)
        elif args.stage == "status":
            status(args.run)
        elif not args.model:
            parser.error("stage requires --model")
        else:
            {"generate": generate, "capture": capture, "export": export}[args.stage](args.run, args.model)


if __name__ == "__main__":
    main()
