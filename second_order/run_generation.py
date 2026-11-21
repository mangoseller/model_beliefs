"""Cached, resumable generation for every prompt of the two datasets.

Each item is keyed by ``sha256(json([model, dataset, case_id, condition, output, replicate]))``
(``sample`` is appended when nonzero) and appended to
``data/generation_cache/<model>.jsonl`` once it returns.  A rerun skips every
cached key and never re-calls the model for it.  A cached item whose prompt or
generation settings differ from the current ones raises an error, so a changed
dataset can never silently mix with old responses.

CKD rows are one call each (A and B carry two replicates in the file).  Bespoke rows
get ``samples`` calls per run, and A, B and B_F a second run (``replicates``).  The
bespoke comprehension follow-ups are two-turn items (output ``comprehension``): the C
prompt, the cached C response and the row's question, generated after the C answers.
The CKD checks are ordinary standalone rows.

Backends: ``vllm`` (local open weights on CUDA, one seed per item), ``hf``
(transformers fallback) and ``synthetic`` (``second_order/synthetic.py``: text from
known parameters, for end-to-end checks).  A single user message and no system
prompt; temperature from ``generation`` and the token limit per answer type (reasoning
then JSON for probabilities, a single letter otherwise: ``datasets.<name>.max_tokens``),
both overridable per model (``temperature``, ``top_p``, ``top_k``, ``max_tokens``,
``enable_thinking``).  vLLM also keeps the top log-probabilities of the first
answer token (``generation.logprobs``), so the letter answers can be scored by
answer probability as well as by the sampled choice.

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
    # Multi-turn items (bespoke comprehension follow-ups): ((role, text), ...); ``prompt`` is the last turn.
    messages: tuple[tuple[str, str], ...] | None = None

    @property
    def key(self) -> str:
        return item_key(self.model, self.dataset, self.case_id, self.condition, self.output, self.replicate,
                        self.sample)

    @property
    def prompt_sha(self) -> str:
        text = self.prompt if self.messages is None else json.dumps(self.messages)
        return hashlib.sha256(text.encode()).hexdigest()

    def chat(self) -> list[dict[str, str]]:
        turns = self.messages if self.messages is not None else (("user", self.prompt),)
        return [{"role": role, "content": text} for role, text in turns]


FOLLOWUP = "comprehension"  # output name of the bespoke comprehension follow-ups


def item_key(model: str, dataset: str, case_id: str, condition: str, output: str, replicate: int,
             sample: int = 0) -> str:
    fields = [model, dataset, case_id, condition, output, int(replicate)] + ([int(sample)] if sample else [])
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def settings_signature(model: str, dataset: str) -> str:
    """Hash of what besides the prompt shapes a response: backend, weights, sampling, chat template."""
    mcfg = C.model_config(model)
    payload = {"backend": mcfg["backend"], "weights": mcfg.get("path"), "revision": mcfg.get("revision"),
               "sampling": {o: sampling(model, dataset, o) for o in ("probability", "choice")},
               "template": template_kwargs(model), "truth": mcfg.get("truth")}
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
    signatures: dict[tuple[str, str], str] = {}
    out = []
    for item in items:
        if item.model not in by_model:  # read each cache file once
            by_model[item.model] = Cache(item.model)
        cache = by_model[item.model]
        record = cache.get(item.key)
        if record is not None and record["prompt_sha"] != item.prompt_sha:
            raise ValueError(f"cached prompt differs for {item}: template changed?")
        if record is not None and "settings_sha" in record:
            sig = (item.model, item.dataset)
            if sig not in signatures:
                signatures[sig] = settings_signature(*sig)
            if record["settings_sha"] != signatures[sig]:
                raise ValueError(f"cached response for {item.key} was generated with other settings "
                                 f"(sampling, chat template or weights) than {item.model} has now")
        out.append(record)
    return out


# ---------------------------------------------------------------- backends
def sampling(model: str, dataset: str, output: str) -> dict[str, Any]:
    """Temperature, top-p, top-k, token limit and log-probabilities, overridable per model."""
    from second_order.datasets import max_tokens

    gen, mcfg = C.load()["generation"], C.model_config(model)
    return {"temperature": float(mcfg.get("temperature", gen["temperature"])),
            "top_p": float(mcfg.get("top_p", 1.0)), "top_k": int(mcfg.get("top_k", -1)),
            "max_tokens": int(mcfg.get("max_tokens", max_tokens(dataset, output))),
            "logprobs": int(gen.get("logprobs") or 0)}


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
    params = []
    for i in items:
        s = sampling(model, i.dataset, i.output)
        params.append(SamplingParams(n=1, temperature=s["temperature"], top_p=s["top_p"], top_k=s["top_k"],
                                     max_tokens=s["max_tokens"], seed=item_seed(seed, i),
                                     logprobs=s["logprobs"] or None))
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
        first = completion.logprobs[0] if completion.logprobs else {}
        records.append({"text": completion.text, "ok": True, "error": None,
                        "stop_reason": completion.finish_reason,
                        "usage": {"input_tokens": len(out.prompt_token_ids),
                                  "output_tokens": len(completion.token_ids)},
                        "first_token_logprobs": {lp.decoded_token: lp.logprob for lp in first.values()},
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
    s = sampling(model, items[0].dataset, items[0].output)
    s["max_tokens"] = max(sampling(model, i.dataset, i.output)["max_tokens"] for i in items)  # one limit per batch
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
    """Generate every uncached item (all items must be for one model and one dataset)."""
    if not items:
        return
    pairs = {(i.model, i.dataset) for i in items}
    if len(pairs) != 1:
        raise ValueError("run() takes items for one model and one dataset")
    model, dataset = pairs.pop()
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
    signature = settings_signature(model, dataset)
    started = time.time()
    tokens_out = errors = 0
    # Prompts of one case and condition share their evidence, so keep them in one batch (prefix caching).
    todo.sort(key=lambda i: (i.condition, i.case_id, i.replicate, i.output, i.sample))
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


def experiment_items(model: str, dataset: str, case_ids: list[str], conditions: list[str] | None = None,
                     outputs: tuple[str, ...] | None = None, batteries=None) -> list[Item]:
    """Every prompt of a dataset for these cases, each run and sample of it."""
    from second_order import datasets as DS

    frame = DS.rows(dataset, batteries)
    frame = frame[frame.case_id.isin(set(case_ids))]
    if conditions:
        frame = frame[frame.condition.isin(set(conditions))]
    if outputs:
        frame = frame[frame.output.isin(set(outputs))]
    items = []
    for r in frame.itertuples(index=False):
        for extra in range(DS.replicates(dataset, r.condition)):
            for sample in range(DS.samples(dataset)):
                items.append(Item(model, r.case_id, r.condition, r.output, int(r.replicate) + extra, r.prompt,
                                  sample, dataset))
    return items


def comprehension_items(model: str, dataset: str, case_ids: list[str]) -> tuple[list[Item], int]:
    """Bespoke follow-ups for every cached C answer; also the number still missing a parent."""
    from second_order import datasets as DS
    from second_order.bespoke import COMPREHENSION_CONDITIONS

    if dataset != "bespoke":
        return [], 0
    parents = [i for i in experiment_items(model, dataset, case_ids, list(COMPREHENSION_CONDITIONS))
               if i.replicate == 0]
    items, missing = [], 0
    for parent, record in zip(parents, lookup(parents)):
        if record is None or not record.get("ok"):
            missing += record is None
            continue
        question = DS.row(dataset, parent.case_id, parent.condition, parent.output)["comprehension_q"]
        messages = (("user", parent.prompt), ("assistant", record["text"] or ""), ("user", question))
        items.append(Item(model, parent.case_id, parent.condition, FOLLOWUP, parent.replicate, question,
                          parent.sample, dataset, messages))
    return items, missing


def select_cases(n: int | str) -> list[str]:
    """CKD patients: all, or a seeded sample of ``n``."""
    import numpy as np

    from second_order import datasets as DS

    ids = DS.case_ids("ckd")
    if n == "all":
        return ids
    rng = np.random.default_rng(int(C.load()["seed"]))
    return sorted(rng.choice(ids, size=int(n), replace=False).tolist())


def dataset_cases(dataset: str, cases: str | None = None) -> list[str]:
    """Case ids of one dataset: a file of ids, an integer or 'all' (default ``main_cases``).

    An integer selects CKD patients only; the bespoke dataset runs every item unless a file of ids names some.
    """
    from second_order import datasets as DS

    if cases and Path(cases).exists():
        wanted = set(Path(cases).read_text().split())
        return [c for c in DS.case_ids(dataset) if c in wanted]
    if dataset == "ckd":
        return select_cases(cases or C.load()["main_cases"])
    return DS.case_ids(dataset)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", nargs="+", default=None, help="default: the model's datasets")
    parser.add_argument("--stage", choices=("main", "comprehension", "all"), default="all")
    parser.add_argument("--cases", default=None, help="'all', an integer, or a file of case ids")
    args = parser.parse_args()
    for dataset in args.dataset or C.datasets_for(args.model):
        case_ids = dataset_cases(dataset, args.cases)
        label = f"{args.model}/{dataset}"
        if args.stage in ("main", "all"):
            run(experiment_items(args.model, dataset, case_ids), label=label)
        if args.stage in ("comprehension", "all") and dataset == "bespoke":
            items, missing = comprehension_items(args.model, dataset, case_ids)
            if missing:
                print(f"[{label}/comprehension] {missing} C answers not generated yet; their follow-ups wait",
                      flush=True)
            run(items, label=f"{label}/comprehension")


if __name__ == "__main__":
    main()
