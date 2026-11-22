"""Paired CKD chart-order / authority experiment on the released prompts.

Every CKD patient's C, M and D prompts are shown in both block orders (``own_first``: the model's own
record first; ``person_first``: PERSON's record first), in three settings: the partial records with their
original wording, the false records with the "verified" cue, and the false records with the cue removed
(``neutral``). Only whole record blocks move; headers, facts and their order within a block are kept. A and
B take no order. The full 31-output latent battery is asked of every cell.

A prompt identical to one of the base run's is reused (its archived answer and activations), and identical
new prompts within the plan are one item, so only missing calls are made:

    python -m second_order.order_followup prepare --run runs/<name> --base-run <base run dir or tarball> \\
        --base-activations <base run dir or activations tar> [--models qwen3_8b qwen3_14b]
    python -m second_order.order_followup status   --run runs/<name>
    python -m second_order.order_followup generate --run runs/<name> --model qwen3_8b
    python -m second_order.order_followup capture  --run runs/<name> --model qwen3_8b
    python -m second_order.order_followup export   --run runs/<name> --model qwen3_8b

``binding`` reuses this module's plan files, caches, scoring and activation capture.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import tarfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import config as C, datasets as DS, run_generation as G

CUE = " Your chart gives the verified patient information."
ORDERS = ("own_first", "person_first")
PROTOCOL = "ckd-order-authority-v1"
# The base run asked for reasoning before the choice; every reused prompt is verified against its cache hash.
CHOICE_INSTRUCTION = ("Answer with a single letter.",
                      "Provide your reasoning first.\nThen output your answer on a new line in exactly this format:\n"
                      "Choice: <the letter of one option>")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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


def make_plan(model: str, cases=None) -> tuple[list[dict], dict]:
    """(trials, items): every trial of the design, and one item per distinct prompt (per patient, output and
    replicate; replicates are never merged). A prompt identical to a base-run prompt keeps the base run's key."""
    frame = release_frame(cases)
    originals = {}
    for r in frame.to_dict("records"):
        item = G.Item(model, r["case_id"], r["condition"], r["output"], r["replicate"], r["prompt"])
        originals.setdefault((item.case_id, item.output, item.replicate, item.prompt_sha), item)
    trials, items = [], {}
    for setting, wording in [("partial", "original"), ("false", "verified"), ("false", "neutral")]:
        for role in ["A", "B", "M", "C", "D"]:
            base = role + ("_F" if setting == "false" else "")
            for r in frame[frame.condition == base].to_dict("records"):
                for order in ORDERS:
                    ordered = role in "MCD"
                    prompt = transform(r["prompt"], order if ordered else None, wording == "neutral")
                    condition = f"followup_v1__{setting}__{wording}__{order if ordered else 'shared'}__{role}"
                    candidate = G.Item(model, r["case_id"], condition, r["output"], r["replicate"], prompt)
                    original = originals.get((candidate.case_id, candidate.output, candidate.replicate,
                                              candidate.prompt_sha))
                    item = original or candidate
                    source = "baseline" if original else "new"
                    trials.append(dict(model=model, dataset="ckd", case_id=r["case_id"], setting=setting,
                                       wording=wording, block_order=order, source_block_order=r["block_order"],
                                       role=role, base_condition=base, output=r["output"], replicate=r["replicate"],
                                       sample=0, check_answer=r["check_answer"], check_fact=r["check_fact"],
                                       check_relation=r["check_relation"], item_key=item.key, source=source))
                    items[item.key] = asdict(item) | {"key": item.key, "prompt_sha": item.prompt_sha, "source": source}
    # Identical new prompts of different cells (e.g. neutral D = partial D) become one item; trials keep their cell.
    canonical, aliases = {}, {}
    for key, item in items.items():
        identity = (item["case_id"], item["output"], item["replicate"], item["prompt_sha"])
        aliases[key] = canonical.setdefault(identity, key)
    for row in trials:
        row["item_key"] = aliases[row["item_key"]]
    return trials, {key: value for key, value in items.items() if aliases[key] == key}


# Run directory
def stream_member(source, suffix):
    """Yield one file of a run directory or run tarball, without extracting the archive."""
    source = Path(source)
    if source.is_dir():
        with (source / suffix).open("rb") as f:
            yield f
    else:
        with tarfile.open(source, "r|*") as tar:
            for member in tar:
                if member.isfile() and member.name.endswith("/" + suffix):
                    yield tar.extractfile(member)
                    return
        raise ValueError(f"{suffix} missing in {source}")


def read_member(source, suffix) -> bytes:
    for f in stream_member(source, suffix):
        return f.read()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def configure(root: Path) -> None:
    """Point the config and the generation cache at a run directory."""
    C.CONFIG_PATH = root / "config.yaml"
    C.load.cache_clear()
    C.CACHE = root / "data/generation_cache"
    G.cache_index.cache_clear()


def install_config(root: Path, config_bytes: bytes) -> None:
    """Write the run's config (refusing to replace a different one) and configure the run."""
    path = root / "config.yaml"
    if path.exists() and path.read_bytes() != config_bytes:
        raise ValueError(f"{root} already uses a different config")
    path.write_bytes(config_bytes)
    configure(root)
    if not all(DS.ckd_checksums().values()):
        raise ValueError("CKD release checksums failed")


def write_plan(root: Path, plan: dict) -> None:
    """Add the plan's digest and write it, refusing to replace a different plan."""
    plan["digest"] = digest(plan)
    path = root / "plan.json"
    if path.exists() and json.loads(path.read_text())["digest"] != plan["digest"]:
        raise ValueError("plan changed; use another run directory")
    atomic_json(path, plan)


def item_from(row: dict) -> G.Item:
    return G.Item(**{k: row[k] for k in G.Item.__dataclass_fields__})


def new_items(plan: dict, model: str) -> list[G.Item]:
    return [item_from(v) for v in plan["items"].values() if v["model"] == model and v["source"] == "new"]


def capture_items(plan: dict, model: str) -> list[dict]:
    """New stated-probability items of replicate 0: the items whose activations are captured."""
    return [v for v in plan["items"].values() if v["model"] == model and v["source"] == "new"
            and v["output"] == "probability/normal" and v["replicate"] == 0]


# Stages
def prepare(args) -> None:
    root = args.run.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # The archived config carries the pinned weight revisions and sampling settings.
    install_config(root, read_member(args.base_run, "config.yaml"))
    trials, items, signatures = [], {}, {}
    for model in args.models:
        ts, its = make_plan(model)
        trials.extend(ts)
        items.update(its)
        signatures[model] = G.settings_signature(model, "ckd")
    plan = dict(protocol=PROTOCOL, models=args.models, settings=signatures,
                base_run=str(args.base_run.resolve()), base_activations=str(args.base_activations.resolve()),
                trials=trials, items=items)
    write_plan(root, plan)
    for model in args.models:
        wanted = {k: v for k, v in items.items() if v["model"] == model and v["source"] == "baseline"}
        copy_baseline(root, model, wanted, signatures[model], args.base_run, args.base_activations)
    atomic_json(root / "prepared.json", {"plan_digest": plan["digest"], "budget": summary(plan)})
    print(json.dumps(summary(plan), indent=2))


def copy_baseline(root, model, wanted, signature, base_run, base_activations) -> None:
    """Copy the reused base-run answers (verified against prompt and settings) and write references to their
    archived activation rows; the arrays themselves are never copied or recomputed."""
    dest = root / "baseline_cache" / f"{model}.jsonl"
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        found, tmp = set(), dest.with_suffix(".tmp")
        with tmp.open("w") as out:
            for f in stream_member(base_run, f"data/generation_cache/{model}.jsonl"):
                for line in f:
                    record = json.loads(line)
                    key = record["key"]
                    if key not in wanted or not record.get("ok"):
                        continue
                    if record["prompt_sha"] != wanted[key]["prompt_sha"] or record["settings_sha"] != signature:
                        raise ValueError(f"baseline prompt/settings mismatch: {key}")
                    if key not in found:
                        out.write(json.dumps(record) + "\n")
                        found.add(key)
        if found != set(wanted):
            raise ValueError(f"{model}: {len(set(wanted) - found)} baseline generations missing; "
                             "refusing to regenerate them")
        tmp.replace(dest)
    from second_order.freeze import index_digest
    member = f"data/activations/{model}/ckd"
    index = pd.read_parquet(io.BytesIO(read_member(base_activations, f"{member}/index.parquet")))
    meta = json.loads(read_member(base_activations, f"{member}/meta.json"))
    if index_digest(index) != meta["items_sha256"] or not meta["complete"]:
        raise ValueError("baseline activation index integrity failure")
    wanted_acts = {k for k, v in wanted.items() if v["output"] == "probability/normal" and v["replicate"] == 0}
    refs = index[index.item_key.isin(wanted_acts)].copy()
    if set(refs.item_key) != wanted_acts:
        raise ValueError("missing baseline activation references")
    refs["source"] = "baseline"
    refs["array_member"] = f"{member}/acts.npy"
    refs["array_sha256"] = meta["array_sha256"]
    refs.to_parquet(root / f"baseline_activations_{model}.parquet", index=False)


def summary(plan: dict) -> dict:
    result = {}
    for model in plan["models"]:
        items = [v for v in plan["items"].values() if v["model"] == model]
        new = [v for v in items if v["source"] == "new"]
        result[model] = dict(reused_generations=len(items) - len(new), new_generations=len(new),
                             new_by_output=dict(Counter(v["output"] for v in new)),
                             new_activation_rows=len(capture_items(plan, model)))
    return result


def read_plan(root: Path) -> dict:
    """The run's plan, after checking its digest, its preparation and the current settings signatures."""
    configure(root)
    plan = json.loads((root / "plan.json").read_text())
    if digest({k: v for k, v in plan.items() if k != "digest"}) != plan["digest"]:
        raise ValueError("plan checksum mismatch")
    prepared = root / "prepared.json"
    if not prepared.exists() or json.loads(prepared.read_text()).get("plan_digest") != plan["digest"]:
        raise ValueError("baseline preparation is incomplete; rerun prepare before inference")
    for model in plan["models"]:
        if G.settings_signature(model, "ckd") != plan["settings"][model]:
            raise ValueError("sampling settings changed since preparation")
    return plan


def status(root: Path, plan: dict | None = None, budget=summary) -> dict:
    plan = plan or read_plan(root)
    result = budget(plan)
    for model in plan["models"]:
        result[model]["pending_generations"] = len(G.pending(new_items(plan, model)))
    print(json.dumps(result, indent=2))
    return result


def generate(root: Path, model: str) -> None:
    plan = read_plan(root)
    if model not in plan["models"]:
        raise ValueError(f"model {model} is not in the prepared plan")
    items = new_items(plan, model)
    # Small durable batches limit repeated work after an interruption; sampling is unchanged.
    if C.model_config(model)["backend"] == "vllm":
        C.model_config(model)["batch_size"] = 128
    result = G.run(items, label=f"{model}/order-followup")
    atomic_json(root / f"generation_{model}.json", result)
    if G.pending(items):
        raise RuntimeError("generation incomplete; rerun to retry failed calls")


def capture(root: Path, model: str, replayer_factory=None, items=None) -> None:
    """Residual-stream activations of every capture item (or of ``items``: plan item dicts). An existing
    capture whose identity and checksum match is kept; a damaged one is replayed."""
    from second_order.freeze import POSITIONS, Replayer
    plan = read_plan(root)
    if model not in plan["models"]:
        raise ValueError(f"model {model} is not in the prepared plan")
    items = [item_from(v) for v in (capture_items(plan, model) if items is None else items)]
    records = G.lookup(items)
    if any(not r or not r.get("ok") for r in records):
        raise ValueError("finish generation before capturing activations")
    replayer = None
    for item, record in zip(items, records):
        folder = root / "data/activations" / model / "ckd" / item.key
        folder.mkdir(parents=True, exist_ok=True)
        meta_path, array_path = folder / "meta.json", folder / "acts.npy"
        identity = dict(item_key=item.key, prompt_sha=item.prompt_sha, settings_sha=record["settings_sha"],
                        response_sha=digest([record["text"], record["token_ids"]]), revision=G.model_revision(model))
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            if any(meta[k] != v for k, v in identity.items()):
                raise ValueError("existing activation identity mismatch")
            if array_path.exists() and file_sha256(array_path) == meta["array_sha256"]:
                continue
        if replayer is None:
            replayer = (replayer_factory or Replayer)(model)
        ids, positions, n_prompt = replayer.tokens_and_positions(item, record)
        acts = replayer.run(ids, positions)[None, ...]
        tmp = folder / "acts.tmp"
        with tmp.open("wb") as f:
            np.save(f, acts)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(array_path)
        atomic_json(meta_path, identity | dict(
            complete=True, shape=list(acts.shape), dtype="uint16 bfloat16 bits", positions=list(POSITIONS),
            token_positions=positions, n_prompt_tokens=n_prompt, n_tokens=len(ids),
            **{f"token_{p}": k for p, k in zip(POSITIONS, positions)},
            array_sha256=file_sha256(array_path), layers="0 embedding; i after block i; last after final norm"))
        print(f"[capture {model}] saved {item.case_id}/{item.condition} -> {folder}", flush=True)
    print(f"{model}: {len(items)} activation rows verified/captured (old arrays untouched)")


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


_CHOICE_LINE = re.compile(r"(?:^|[\s>#*_`:])choice[\s*_`]*:[\s*_`]*(.*?)\s*$", flags=re.I)


def score(item: G.Item, record: dict | None, trial: dict):
    """``responses.score_answer`` on the answer after the base run's parser cleanup: for choices, the last
    "Choice:" line and the lines after it, without Markdown emphasis; for probabilities, trailing emphasis
    stripped (unless the answer ends in a code fence) so a terminal JSON object is found."""
    from second_order.responses import score_answer, visible_text
    text = visible_text((record or {}).get("text", "")) or ""
    if item.output.startswith("probability"):
        if not text.endswith("```"):
            text = text.rstrip("*_` \t\n")
    else:
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        at = [k for k, line in enumerate(lines) if _CHOICE_LINE.search(line)]
        if at:
            text = " ".join([_CHOICE_LINE.search(lines[at[-1]]).group(1), *lines[at[-1] + 1:]]).strip()
        text = re.sub(r"[*_`]", "", text)
    return score_answer(item, None if record is None else record | {"text": text}, trial)


def scored_trials(root: Path, plan: dict, model: str):
    """(trial, item, record, score) for every trial of ``model``; each answer's prompt and settings are
    checked against the plan."""
    if model not in plan["models"]:
        raise ValueError(f"model {model} is not in the prepared plan")
    caches = {"baseline": G.Cache(model, root / "baseline_cache"), "new": G.Cache(model, C.CACHE)}
    for trial in plan["trials"]:
        if trial["model"] != model:
            continue
        data = plan["items"][trial["item_key"]]
        item = item_from(data)
        record = caches["baseline" if data["source"] == "baseline" else "new"].get(item.key)
        if record and (record["prompt_sha"] != item.prompt_sha or record["settings_sha"] != plan["settings"][model]):
            raise ValueError("response provenance mismatch")
        yield trial, item, record, score(item, record, trial)


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
