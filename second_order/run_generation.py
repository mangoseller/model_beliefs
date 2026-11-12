"""Cached, resumable generation for every second-order condition.

Each item is keyed by ``sha256(json([model, case_id, condition, output, replicate]))``
and appended to ``data/generation_cache/<model>.jsonl`` once it returns.  A
rerun skips every cached key and never re-calls the model for it.  A cached
item whose prompt hash differs from the prompt now being built raises an
error, so a changed template can never silently mix with old responses.

Condition A of hosted models (replicate 0) is read from the released
first-order responses and is never regenerated.

Before any run of more than ``budget.approval_threshold_calls`` calls the call
count and an estimated cost are printed and the run stops unless
``--approve`` is passed.

Backends: ``mlx`` (local MLX checkpoint, batched, seeded), ``bedrock``
(Converse API, synchronous, threaded), ``openai`` (Responses API).  All use
temperature 1, at most 4,096 output tokens, a single user message and no
system prompt, as in the first-order collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from second_order import config as C

# Mean tokens per call in the first-order CKD cache (docs/ORIENTATION.md).
CACHE_TOKENS = {
    "deepseek_v32": (257, 480), "gemma3_12b": (293, 363), "gemma3_27b": (293, 306),
    "gemma3_4b": (293, 231), "glm5": (261, 414), "llama31_70b": (270, 376),
    "openai_luna": (263, 242), "openai_terra": (263, 186), "qwen3_32b": (277, 452),
}


@dataclass(frozen=True)
class Item:
    model: str
    case_id: str
    condition: str
    output: str
    replicate: int
    prompt: str

    @property
    def key(self) -> str:
        return item_key(self.model, self.case_id, self.condition, self.output, self.replicate)

    @property
    def prompt_sha(self) -> str:
        return hashlib.sha256(self.prompt.encode()).hexdigest()


def item_key(model: str, case_id: str, condition: str, output: str, replicate: int) -> str:
    payload = json.dumps([model, case_id, condition, output, int(replicate)])
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------- cache
class Cache:
    def __init__(self, model: str, root: Path = C.CACHE):
        self.path = root / f"{model}.jsonl"
        self.records: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        record = json.loads(line)
                        self.records[record["key"]] = record

    def __contains__(self, key: str) -> bool:
        return key in self.records

    def get(self, key: str) -> dict[str, Any] | None:
        return self.records.get(key)

    def append(self, records: Iterable[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.records[record["key"]] = record
            handle.flush()
            os.fsync(handle.fileno())


def released_a(model: str) -> dict[tuple[str, str], dict[str, Any]]:
    """(case_id, output) -> released first-order response record."""
    from second_order.prompts_second_order import specs

    result = {}
    for output, spec in specs().items():
        path = C.RELEASE_RESPONSES / spec["task_id"] / f"{model}.jsonl"
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                raw = json.loads(line)
                case_id = raw["prompt_id"].rsplit("__", 1)[0]
                result[(case_id, output)] = raw
    return result


def lookup(items: list[Item]) -> list[dict[str, Any] | None]:
    """Cached response record for each item (released A for hosted models)."""
    by_model: dict[str, Cache] = {}
    released: dict[str, dict] = {}
    out = []
    for item in items:
        cfg = C.model_config(item.model)
        if item.condition == "A" and item.replicate == 0 and cfg.get("cached_a"):
            if item.model not in released:
                released[item.model] = released_a(item.model)
            raw = released[item.model][(item.case_id, item.output)]
            out.append({"key": item.key, "text": raw.get("output_text") or "", "ok": bool(raw.get("ok")),
                        "source": "release", "stop_reason": raw.get("stop_reason"),
                        "usage": raw.get("usage")})
            continue
        cache = by_model.setdefault(item.model, Cache(item.model))
        record = cache.get(item.key)
        if record is not None and record["prompt_sha"] != item.prompt_sha:
            raise ValueError(f"cached prompt differs for {item}: template changed?")
        out.append(record)
    return out


# ---------------------------------------------------------------- cost
def estimate(model: str, n_calls: int, conditions: Iterable[str]) -> dict[str, Any]:
    cfg = C.load()
    backend = C.model_config(model)["backend"]
    base = CACHE_TOKENS.get(model) or CACHE_TOKENS.get(C.split_source(model), (290, 300))
    from second_order.conditions import CONDITIONS

    extra = cfg["budget"]["extra_input_tokens"] if any(CONDITIONS[c].about == "agent" for c in conditions
                                                       if c in CONDITIONS) else 0
    tin, tout = n_calls * (base[0] + extra), n_calls * base[1]
    price = cfg["budget"]["prices"].get(backend, {"input": None, "output": None})
    cost = None
    if backend == "mlx":
        cost = 0.0
    elif price["input"] is not None and price["output"] is not None:
        cost = tin / 1e6 * price["input"] + tout / 1e6 * price["output"]
    return {"model": model, "backend": backend, "calls": n_calls, "input_tokens": tin,
            "output_tokens": tout, "usd": cost}


# ---------------------------------------------------------------- backends
def generate_mlx(model: str, items: list[Item], seed: int) -> list[dict[str, Any]]:
    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.generate import batch_generate
    from mlx_lm.sample_utils import make_sampler

    cfg = C.load()
    mcfg = C.model_config(model)
    if model not in _MLX:
        _MLX[model] = load(str(C.ROOT / mcfg["path"]))
    lm, tok = _MLX[model]
    prompts = [tok.apply_chat_template([{"role": "user", "content": i.prompt}],
                                       add_generation_prompt=True) for i in items]
    batch_seed = (seed + int(items[0].key[:8], 16)) % (2**31)
    mx.random.seed(batch_seed)
    started = time.time()
    result = batch_generate(lm, tok, prompts, max_tokens=int(cfg["generation"]["max_tokens"]),
                            sampler=make_sampler(temp=float(cfg["generation"]["temperature"])),
                            verbose=False)
    elapsed = time.time() - started
    records = []
    for item, prompt_ids, text in zip(items, prompts, result.texts):
        n_out = len(tok.encode(text, add_special_tokens=False))
        records.append({"text": text, "ok": True, "error": None,
                        "stop_reason": "length" if n_out >= cfg["generation"]["max_tokens"] else "end",
                        "usage": {"input_tokens": len(prompt_ids), "output_tokens": n_out},
                        "seed": batch_seed, "batch_seconds": elapsed})
    return records


_MLX: dict[str, Any] = {}


def generate_bedrock(model: str, items: list[Item], seed: int) -> list[dict[str, Any]]:
    import boto3

    cfg = C.load()
    client = boto3.Session().client("bedrock-runtime")
    model_id = C.model_config(model)["model_id"]

    def call(item: Item) -> dict[str, Any]:
        try:
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": item.prompt}]}],
                inferenceConfig={"maxTokens": int(cfg["generation"]["max_tokens"]),
                                 "temperature": float(cfg["generation"]["temperature"])},
            )
            text = "".join(part.get("text", "") for part in response["output"]["message"]["content"])
            return {"text": text, "ok": True, "error": None, "stop_reason": response.get("stopReason"),
                    "usage": response.get("usage"), "seed": None}
        except Exception as exc:  # recorded, never retried silently
            return {"text": "", "ok": False, "error": repr(exc), "stop_reason": None, "usage": None,
                    "seed": None}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(call, items))


def generate_openai(model: str, items: list[Item], seed: int) -> list[dict[str, Any]]:
    import requests

    cfg = C.load()
    mcfg = C.model_config(model)
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}

    def call(item: Item) -> dict[str, Any]:
        body = {"model": mcfg["model_id"], "input": item.prompt,
                "reasoning": {"effort": mcfg.get("reasoning_effort", "medium")},
                "temperature": float(cfg["generation"]["temperature"]),
                "max_output_tokens": int(cfg["generation"]["max_tokens"])}
        try:
            r = requests.post("https://api.openai.com/v1/responses", json=body, headers=headers,
                              timeout=600)
            r.raise_for_status()
            data = r.json()
            text = "".join(c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
                           for c in o.get("content", []))
            return {"text": text, "ok": True, "error": None, "stop_reason": data.get("status"),
                    "usage": data.get("usage"), "seed": None}
        except Exception as exc:
            return {"text": "", "ok": False, "error": repr(exc), "stop_reason": None, "usage": None,
                    "seed": None}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(call, items))


BACKENDS = {"mlx": generate_mlx, "bedrock": generate_bedrock, "openai": generate_openai}


# ---------------------------------------------------------------- driver
def pending(items: list[Item]) -> list[Item]:
    records = lookup(items)
    return [item for item, record in zip(items, records) if record is None]


def run(items: list[Item], *, approve: bool = False, label: str = "") -> None:
    """Generate every uncached item (all items must be for one model)."""
    if not items:
        return
    models = {i.model for i in items}
    if len(models) != 1:
        raise ValueError("run() takes items for one model")
    model = models.pop()
    todo = pending(items)
    conditions = sorted({i.condition for i in todo})
    est = estimate(model, len(todo), conditions)
    usd = "n/a (prices not set)" if est["usd"] is None else f"${est['usd']:.2f}"
    print(f"[{label or model}] {len(items)} items, {len(items) - len(todo)} cached, "
          f"{len(todo)} to call; est. {est['input_tokens']:,} in / {est['output_tokens']:,} out "
          f"tokens; cost {usd}", flush=True)
    threshold = int(C.load()["budget"]["approval_threshold_calls"])
    if len(todo) > threshold and not approve:
        print(f"More than {threshold} calls: rerun with --approve to proceed.", flush=True)
        sys.exit(2)
    if not todo:
        return
    backend = C.model_config(model)["backend"]
    generate = BACKENDS[backend]
    batch = int(C.model_config(model).get("batch_size", 32))
    seed = int(C.load()["seed"])
    cache = Cache(model)
    done = 0
    started = time.time()
    # Group by output so batches have similar lengths.
    todo.sort(key=lambda i: (i.output, i.condition, i.case_id, i.replicate))
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        results = generate(model, chunk, seed)
        cache.append({"key": i.key, "model": i.model, "case_id": i.case_id, "condition": i.condition,
                      "output": i.output, "replicate": i.replicate, "prompt_sha": i.prompt_sha,
                      "backend": backend, "time": time.time(), **r} for i, r in zip(chunk, results))
        done += len(chunk)
        rate = (time.time() - started) / done
        print(f"[{label or model}] {done}/{len(todo)} done; {rate:.2f}s/call; "
              f"eta {rate * (len(todo) - done) / 60:.1f} min", flush=True)


def main_items(model: str, case_ids: list[str], conditions: list[str], replicate: int = 0,
               outputs: tuple[str, ...] | None = None) -> list[Item]:
    from second_order.prompts_second_order import OUTPUTS, build_prompt

    outputs = outputs or OUTPUTS
    return [Item(model, case_id, condition, output, replicate, build_prompt(case_id, condition, output))
            for condition in conditions for case_id in case_ids for output in outputs]


def select_cases(n: int | str, *, exclude: Iterable[str] = (), seed_offset: int = 0) -> list[str]:
    import numpy as np

    from second_order.prompts_second_order import cases

    ids = [c for c in cases().index if c not in set(exclude)]
    if n == "all":
        return ids
    rng = np.random.default_rng(int(C.load()["seed"]) + seed_offset)
    return sorted(rng.choice(ids, size=int(n), replace=False).tolist())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--conditions", nargs="+", default=None,
                        help="default: the model's scheduled conditions (config `schedule`)")
    parser.add_argument("--cases", default=None, help="'all', an integer, or a file of case ids")
    parser.add_argument("--smoke", action="store_true", help="use the smoke-run case set")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.smoke:
        case_ids = smoke_cases()
    elif args.cases and Path(args.cases).exists():
        case_ids = Path(args.cases).read_text().split()
    else:
        case_ids = select_cases(args.cases or C.load()["main_cases"])
    args.conditions = args.conditions or C.scheduled_conditions(args.model)
    items = main_items(args.model, case_ids, args.conditions)
    if args.dry_run:
        todo = pending(items)
        print(json.dumps(estimate(args.model, len(todo), args.conditions), indent=2))
        return
    run(items, approve=args.approve, label=f"{args.model}")


def smoke_cases() -> list[str]:
    return select_cases(C.load()["smoke"]["n_cases"], exclude=pilot_cases(), seed_offset=2)


def pilot_cases() -> list[str]:
    return select_cases(C.load()["pilot"]["n_cases"], seed_offset=1)


if __name__ == "__main__":
    main()
