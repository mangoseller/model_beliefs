"""Cached, resumable generation for every condition.

Each item is keyed by ``sha256(json([model, case_id, condition, output, replicate]))``
(``sample`` is appended when nonzero) and appended to
``data/generation_cache/<model>.jsonl`` once it returns.  A rerun skips every
cached key and never re-calls the model for it.  A cached item whose prompt or
generation settings differ from the current ones raises an error, so a changed
template can never silently mix with old responses.

``replicate`` is an independent run of a condition (two runs of A and every
B, ``experiment.replicates``, give the reliability rho); ``sample`` indexes
the samples within a run (``bespoke.samples``).  Comprehension follow-ups are
two-turn items (output ``comprehension:<output>``): the C prompt, the cached C
response and the question; they are generated after the C answers they follow.

Backends: ``vllm`` (local open weights on CUDA, one seed per item), ``hf``
(transformers fallback) and ``synthetic`` (``second_order/synthetic.py``: text
from known parameters, for end-to-end checks).  A single user message and no
system prompt, as in the first-order collection; sampling from ``generation``,
overridden per model (``temperature``, ``top_p``, ``top_k``, ``max_tokens``,
``enable_thinking``).

    python -m second_order.run_generation --model qwen3_8b [--dataset ckd bespoke] [--stage all]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from tqdm import tqdm

from second_order import config as C

@dataclass(frozen=True)
class Item:
    model: str
    case_id: str
    condition: str
    output: str
    replicate: int
    prompt: str
    sample: int = 0
    dataset: str = "ckd"
    # Multi-turn items (comprehension follow-ups): ((role, text), ...); ``prompt`` is the last turn.
    messages: tuple[tuple[str, str], ...] | None = None

    @property
    def key(self) -> str:
        return item_key(self.model, self.case_id, self.condition, self.output, self.replicate, self.sample)

    @property
    def prompt_sha(self) -> str:
        text = self.prompt if self.messages is None else json.dumps(self.messages)
        return hashlib.sha256(text.encode()).hexdigest()

    def chat(self) -> list[dict[str, str]]:
        turns = self.messages if self.messages is not None else (("user", self.prompt),)
        return [{"role": role, "content": text} for role, text in turns]


def item_key(model: str, case_id: str, condition: str, output: str, replicate: int, sample: int = 0) -> str:
    fields = [model, case_id, condition, output, int(replicate)] + ([int(sample)] if sample else [])
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def settings_signature(model: str) -> str:
    """Hash of what besides the prompt shapes a response: backend, weights, sampling, chat template."""
    mcfg = C.model_config(model)
    payload = {"backend": mcfg["backend"], "weights": mcfg.get("path"), "revision": mcfg.get("revision"),
               "sampling": sampling(model), "template": template_kwargs(model), "truth": mcfg.get("truth")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def item_seed(seed: int, item: Item) -> int:
    return (int(seed) + int(item.key[:8], 16)) % (2**31)


# ---------------------------------------------------------------- cache
class Cache:
    def __init__(self, model: str, root: Path | None = None):
        self.path = (root or C.CACHE) / f"{model}.jsonl"
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


def lookup(items: list[Item]) -> list[dict[str, Any] | None]:
    """Cached response record for each item (None if not generated yet)."""
    by_model: dict[str, Cache] = {}
    signatures: dict[str, str] = {}
    out = []
    for item in items:
        if item.model not in by_model:  # read each cache file once
            by_model[item.model] = Cache(item.model)
        cache = by_model[item.model]
        record = cache.get(item.key)
        if record is not None and record["prompt_sha"] != item.prompt_sha:
            raise ValueError(f"cached prompt differs for {item}: template changed?")
        if record is not None and "settings_sha" in record:
            if item.model not in signatures:
                signatures[item.model] = settings_signature(item.model)
            if record["settings_sha"] != signatures[item.model]:
                raise ValueError(f"cached response for {item.key} was generated with other settings "
                                 f"(sampling, chat template or weights) than {item.model} has now")
        out.append(record)
    return out


# ---------------------------------------------------------------- backends
def sampling(model: str) -> dict[str, Any]:
    """Temperature, top-p, top-k and token limit: the generation block, overridden per model."""
    gen, mcfg = C.load()["generation"], C.model_config(model)
    return {"temperature": float(mcfg.get("temperature", gen["temperature"])),
            "top_p": float(mcfg.get("top_p", 1.0)), "top_k": int(mcfg.get("top_k", -1)),
            "max_tokens": int(mcfg.get("max_tokens", gen["max_tokens"]))}


def template_kwargs(model: str) -> dict[str, Any]:
    """Chat-template arguments (Qwen3: thinking on or off)."""
    mcfg = C.model_config(model)
    return {"enable_thinking": bool(mcfg["enable_thinking"])} if "enable_thinking" in mcfg else {}


_LOCAL: dict[str, Any] = {}


def generate_vllm(model: str, items: list[Item], seed: int) -> list[dict[str, Any]]:
    from vllm import LLM, SamplingParams

    mcfg = C.model_config(model)
    if model not in _LOCAL:
        # Engine settings (max_num_seqs, max_num_batched_tokens, gpu_memory_utilization,
        # enable_prefix_caching, ...) come from the model's `engine` entry, which the runner's
        # tune stage fills in; they change speed, not what is sampled.
        engine = {"gpu_memory_utilization": 0.9, "enable_prefix_caching": True, **(mcfg.get("engine") or {})}
        _LOCAL[model] = LLM(model=mcfg["path"], revision=mcfg.get("revision"), dtype=mcfg.get("dtype", "auto"),
                            max_model_len=mcfg.get("max_model_len"), seed=int(seed),
                            tensor_parallel_size=int(mcfg.get("tensor_parallel_size", 1)), **engine)
    llm = _LOCAL[model]
    s = sampling(model)
    params = [SamplingParams(n=1, temperature=s["temperature"], top_p=s["top_p"], top_k=s["top_k"],
                             max_tokens=s["max_tokens"], seed=item_seed(seed, i)) for i in items]
    # Render each conversation with the model's chat template here (thinking on or off), as
    # LLM.chat would; LLM.chat in vLLM 0.8.5 takes no chat-template arguments.
    tok = llm.get_tokenizer()
    prompts = [{"prompt_token_ids": tok.apply_chat_template(i.chat(), add_generation_prompt=True, tokenize=True,
                                                            **template_kwargs(model))} for i in items]
    started = time.time()
    outputs = llm.generate(prompts, params, use_tqdm=False)
    elapsed = time.time() - started
    records = []
    for item, out in zip(items, outputs):
        completion = out.outputs[0]
        records.append({"text": completion.text, "ok": True, "error": None,
                        "stop_reason": completion.finish_reason,
                        "usage": {"input_tokens": len(out.prompt_token_ids),
                                  "output_tokens": len(completion.token_ids)},
                        "seed": item_seed(seed, item), "batch_seconds": elapsed})
    return records


def generate_hf(model: str, items: list[Item], seed: int) -> list[dict[str, Any]]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    mcfg = C.model_config(model)
    if model not in _LOCAL:
        tok = AutoTokenizer.from_pretrained(mcfg["path"], revision=mcfg.get("revision"), padding_side="left")
        lm = AutoModelForCausalLM.from_pretrained(mcfg["path"], revision=mcfg.get("revision"),
                                                  torch_dtype=getattr(torch, mcfg.get("dtype", "bfloat16")),
                                                  device_map=mcfg.get("device_map", "auto"))
        lm.eval()
        _LOCAL[model] = (lm, tok)
    lm, tok = _LOCAL[model]
    s = sampling(model)
    texts = [tok.apply_chat_template(i.chat(), add_generation_prompt=True, tokenize=False, **template_kwargs(model))
             for i in items]
    enc = tok(texts, return_tensors="pt", padding=True, add_special_tokens=False).to(lm.device)
    # One seed per batch (batched sampling cannot take per-item seeds), so a resumed run with
    # different batches is not bit-reproducible; vllm seeds every item.
    batch_seed = item_seed(seed, items[0])
    torch.manual_seed(batch_seed)
    started = time.time()
    with torch.no_grad():
        out = lm.generate(**enc, do_sample=True, temperature=s["temperature"], top_p=s["top_p"],
                          top_k=s["top_k"] if s["top_k"] > 0 else None, max_new_tokens=s["max_tokens"],
                          pad_token_id=tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id)
    elapsed = time.time() - started
    eos = getattr(lm.generation_config, "eos_token_id", None)
    stops = {tok.eos_token_id, tok.pad_token_id} | set([eos] if isinstance(eos, int) else (eos or []))
    records = []
    for row, mask in zip(out[:, enc["input_ids"].shape[1]:], enc["attention_mask"]):
        ids = row.tolist()
        n_out = next((k for k, t in enumerate(ids) if t in stops), len(ids))
        records.append({"text": tok.decode(ids[:n_out], skip_special_tokens=True), "ok": True, "error": None,
                        "stop_reason": "length" if n_out >= s["max_tokens"] else "end",
                        "usage": {"input_tokens": int(mask.sum()), "output_tokens": n_out},
                        "seed": batch_seed, "batch_seconds": elapsed})
    return records


def generate_synthetic(model: str, items: list[Item], seed: int) -> list[dict[str, Any]]:
    from second_order.synthetic import generate

    return generate(model, items, seed)


BACKENDS = {"vllm": generate_vllm, "hf": generate_hf, "synthetic": generate_synthetic}


# ---------------------------------------------------------------- driver
def pending(items: list[Item]) -> list[Item]:
    records = lookup(items)
    return [item for item, record in zip(items, records) if record is None]


def run(items: list[Item], *, label: str = "") -> None:
    """Generate every uncached item (all items must be for one model)."""
    if not items:
        return
    models = {i.model for i in items}
    if len(models) != 1:
        raise ValueError("run() takes items for one model")
    model = models.pop()
    todo = pending(items)
    print(f"[{label or model}] {len(items)} items, {len(items) - len(todo)} cached, {len(todo)} to call",
          flush=True)
    if not todo:
        return
    backend = C.model_config(model)["backend"]
    generate = BACKENDS[backend]
    batch = int(C.model_config(model).get("batch_size", 32))
    seed = int(C.load()["seed"])
    cache = Cache(model)
    signature = settings_signature(model)
    started = time.time()
    tokens_out = errors = 0
    # Group by output so batches have similar lengths.
    todo.sort(key=lambda i: (i.output, i.condition, i.case_id, i.replicate, i.sample))
    # One bar over every call (stderr; in a log file it updates every 30 s instead of continuously).
    bar = tqdm(total=len(todo), desc=label or model, unit="call", smoothing=0.1, dynamic_ncols=True,
               mininterval=float(os.environ.get("SO_PROGRESS_INTERVAL", 1 if sys.stderr.isatty() else 30)),
               file=sys.stderr)
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        results = generate(model, chunk, seed)
        cache.append({"key": i.key, "model": i.model, "dataset": i.dataset, "case_id": i.case_id,
                      "condition": i.condition, "output": i.output, "replicate": i.replicate,
                      "sample": i.sample, "prompt_sha": i.prompt_sha, "settings_sha": signature,
                      "backend": backend,
                      "time": time.time(), **r} for i, r in zip(chunk, results))
        tokens_out += sum((r.get("usage") or {}).get("output_tokens") or 0 for r in results)
        errors += sum(not r.get("ok") for r in results)
        bar.set_postfix_str(f"{tokens_out / max(time.time() - started, 1e-9):,.0f} out tok/s; {errors} errors; "
                            f"now {chunk[-1].dataset}/{chunk[-1].output}", refresh=False)
        bar.update(len(chunk))
    bar.close()
    print(f"[{label or model}] {len(todo)} calls in {(time.time() - started) / 60:.1f} min; "
          f"{tokens_out:,} output tokens; {errors} errors", flush=True)


def experiment_items(model: str, dataset: str, case_ids: list[str], conditions: list[str],
                     outputs: tuple[str, ...] | None = None) -> list[Item]:
    """Every run and sample of each condition, case and output of one dataset."""
    from second_order import datasets as DS

    outputs = outputs or DS.outputs(dataset)
    items = []
    for condition in conditions:
        for case_id in case_ids:
            for output in outputs:
                prompt = DS.prompt(dataset, case_id, condition, output)
                for replicate in range(C.replicates(condition)):
                    for sample in range(DS.samples(dataset)):
                        items.append(Item(model, case_id, condition, output, replicate, prompt, sample, dataset))
    return items


def comprehension_items(model: str, dataset: str, case_ids: list[str],
                        conditions: list[str]) -> tuple[list[Item], int]:
    """Follow-up items for every cached C answer that gets one; also the number still missing a parent."""
    from second_order import datasets as DS
    from second_order.comprehension import followup_output

    wanted = [c for c in C.load()["experiment"]["comprehension"]["conditions"] if c in conditions]
    parents = experiment_items(model, dataset, case_ids, wanted, DS.comprehension_outputs(dataset))
    items, missing = [], 0
    for parent, record in zip(parents, lookup(parents)):
        if record is None or not record.get("ok"):
            missing += record is None
            continue
        question = DS.comprehension(dataset, parent.case_id, parent.condition).question
        messages = (("user", parent.prompt), ("assistant", record["text"] or ""), ("user", question))
        items.append(Item(model, parent.case_id, parent.condition, followup_output(parent.output),
                          parent.replicate, question,
                          parent.sample, dataset, messages))
    return items, missing


def select_cases(n: int | str) -> list[str]:
    """CKD case ids: all, or a seeded sample of ``n``."""
    import numpy as np

    from second_order.prompts_second_order import cases

    ids = list(cases().index)
    if n == "all":
        return ids
    rng = np.random.default_rng(int(C.load()["seed"]))
    return sorted(rng.choice(ids, size=int(n), replace=False).tolist())


def dataset_cases(dataset: str, cases: str | None = None) -> list[str]:
    """Case ids of one dataset: a file of ids, an integer or 'all' (default ``main_cases``).

    An integer selects CKD cases only; the bespoke dataset runs every item unless a file of ids names some.
    """
    from second_order import datasets as DS

    if cases and Path(cases).exists():
        wanted = Path(cases).read_text().split()
        return [c for c in wanted if DS.dataset_of(c) == dataset]
    if dataset == "ckd":
        return select_cases(cases or C.load()["main_cases"])
    return DS.case_ids(dataset)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", nargs="+", default=None, help="default: the model's datasets")
    parser.add_argument("--stage", choices=("main", "comprehension", "all"), default="all")
    parser.add_argument("--conditions", nargs="+", default=None, help="default: experiment.conditions")
    parser.add_argument("--cases", default=None, help="'all', an integer, or a file of case ids")
    args = parser.parse_args()
    conditions = args.conditions or C.conditions()
    for dataset in args.dataset or C.datasets_for(args.model):
        case_ids = dataset_cases(dataset, args.cases)
        label = f"{args.model}/{dataset}"
        if args.stage in ("main", "all"):
            run(experiment_items(args.model, dataset, case_ids, conditions), label=label)
        if args.stage in ("comprehension", "all"):
            items, missing = comprehension_items(args.model, dataset, case_ids, conditions)
            if missing:
                print(f"[{label}/comprehension] {missing} C answers not generated yet; their follow-ups wait",
                      flush=True)
            run(items, label=f"{label}/comprehension")


if __name__ == "__main__":
    main()
