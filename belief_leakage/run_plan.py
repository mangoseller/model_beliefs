"""Run plans, cached generations and activation captures shared by the CKD follow-ups."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from belief_leakage import config as C, run_generation as G
from belief_leakage.datasets import final_choice
from belief_leakage.package_run import file_sha256


def released_items(model, records):
    """The first baseline item for each released prompt, keeping outputs and replicates distinct."""
    originals = {}
    for r in records:
        item = G.Item(model, r["case_id"], r["condition"], r["output"], r["replicate"], r["prompt"])
        originals.setdefault((item.case_id, item.output, item.replicate, item.prompt_sha), item)
    return originals


class Builder:
    """Accumulates trials and unique items; identical prompts share one item."""

    def __init__(self, model, originals):
        self.model, self.originals = model, originals
        self.items, self.identity, self.trials = {}, {}, []

    def resolve(self, item: G.Item, source: str = "new") -> str:
        prompt = (item.case_id, item.output, item.replicate, item.prompt_sha)
        identity = (*prompt, item.sample)
        if identity not in self.identity:
            original = self.originals.get(prompt) if item.sample == 0 else None
            chosen = original or item
            self.identity[identity] = chosen.key
            self.items[chosen.key] = asdict(chosen) | {"key": chosen.key, "prompt_sha": chosen.prompt_sha,
                                                       "source": "baseline" if original else source}
        return self.identity[identity]

    def add(self, trial: dict, item: G.Item) -> None:
        key = self.resolve(item)
        self.trials.append(trial | {"item_key": key, "source": self.items[key]["source"]})


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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
    from belief_leakage.freeze import index_digest
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


def collect_models(models, make_plan):
    trials, items, signatures = [], {}, {}
    for model in models:
        model_trials, model_items = make_plan(model)
        trials.extend(model_trials)
        items.update(model_items)
        signatures[model] = G.settings_signature(model, "ckd")
    return trials, items, signatures


def finish_prepare(root, plan, summary, *, require_baseline=False):
    """Copy reused answers and activation references before marking the plan ready for inference."""
    for model in plan["models"]:
        wanted = {k: v for k, v in plan["items"].items() if v["model"] == model and v["source"] == "baseline"}
        if wanted or require_baseline:
            copy_baseline(root, model, wanted, plan["settings"][model], plan["base_run"], plan["base_activations"])
    budget = summary(plan)
    atomic_json(root / "prepared.json", {"plan_digest": plan["digest"], "budget": budget})
    print(json.dumps(budget, indent=2))


def status(plan, budget):
    result = budget(plan)
    for model in plan["models"]:
        result[model]["pending_generations"] = len(G.pending(new_items(plan, model)))
    print(json.dumps(result, indent=2))
    return result


def generate_items(root, model, items, label):
    result = G.run(items, label=label)
    atomic_json(root / f"generation_{model}.json", result)
    if G.pending(items):
        raise RuntimeError("generation incomplete; rerun to retry failed calls")


def capture(root: Path, model: str, replayer_factory=None) -> None:
    """Residual-stream activations of every capture item. An existing capture whose identity and checksum
    match is kept; a damaged one is replayed."""
    plan = read_plan(root)
    if model not in plan["models"]:
        raise ValueError(f"model {model} is not in the prepared plan")
    capture_responses(root, model, capture_items(plan, model), replayer_factory)


def capture_responses(root, model, rows, replayer_factory=None):
    """Replay the selected items from a verified plan."""
    from belief_leakage.freeze import POSITIONS, Replayer
    items = [item_from(row) for row in rows]
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


def score(item: G.Item, record: dict | None, trial: dict):
    """``responses.score_answer`` on the answer after the base run's parser cleanup: for choices, the last
    "Choice:" line and the lines after it, without Markdown emphasis; for probabilities, trailing emphasis
    stripped (unless the answer ends in a code fence) so a terminal JSON object is found."""
    from belief_leakage.responses import score_answer, visible_text
    text = visible_text((record or {}).get("text", "")) or ""
    if item.output.startswith("probability"):
        if not text.endswith("```"):
            text = text.rstrip("*_` \t\n")
    else:
        choice = final_choice(text)
        text = re.sub(r"[*_`]", "", text if choice is None else choice)
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
