"""Freeze residual-stream activations for later interpretability work (no generation).

    python -m belief_leakage.freeze --model qwen3_8b [--dataset ckd bespoke]

vLLM does not expose hidden states, so this is a separate prefill-only pass with
transformers, after generation: each prompt is replayed together with the model's
cached response (exactly as generated), and the residual stream is saved at every
layer and three positions:

* ``evidence_end``: the last token before the question: the end of the records (CKD) or
  of the scenario and evidence (bespoke);
* ``prompt_end``: the last token of the chat-formatted prompt (where generation began);
* ``pre_answer``: the last token before the final answer: for probability questions,
  before the last ``{"probability"`` JSON object, after the model's reasoning; for
  multiple-choice answers (not captured by default), the same token as ``prompt_end``.

Which items: ``activations`` in the config (default: the probability question of every
case and condition, main run, first sample), so that every direct belief the estimators
use has its activations.  Layers: transformers' ``hidden_states``: index 0 is the
embedding output, i is the residual stream after block i, and the last index has the
final norm applied (Qwen3).

Output in ``data/activations/<model>/<dataset>/``:

* ``acts.npy``: ``[items, 3 positions, layers + 1, hidden]``, bfloat16 stored bit for bit
  as uint16 (load with ``torch.from_numpy(a.view(np.int16)).view(torch.bfloat16)``, or
  ``freeze.load(path)``);
* ``index.parquet``: one row per item (case, condition, output, run, sample, token
  counts, the token index of each position, -1 where a position was not found);
* ``meta.json``: model, revision, dtype, positions, layer convention, shapes.

About 0.9 MB per item for Qwen3 8B (37 x 4,096) and 1.3 MB for Qwen3 14B (41 x 5,120).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from belief_leakage import config as C
from belief_leakage.package_run import file_sha256
from belief_leakage.readout import render_chat
from belief_leakage.run_generation import Item, experiment_items, load_hf_model, lookup, model_revision, template_kwargs

POSITIONS = ("evidence_end", "prompt_end", "pre_answer")
ANSWER_JSON = re.compile(r'\{\s*"probability"')
# Where the answer instructions begin; the question is the paragraph before.
INSTRUCTIONS = ("\n\nProvide your reasoning first.", "\nAnswer with a single")


def settings() -> dict:
    return C.section_settings("activations")


def freeze_items(model: str, dataset: str, case_ids: list[str], conditions: list[str] | None = None) -> list[Item]:
    s = settings()
    items = experiment_items(model, dataset, case_ids, conditions, tuple(s["outputs"].get(dataset, [])))
    return [i for i in items if i.replicate in s["replicates"] and i.sample < int(s["samples"])]


def evidence_end(item: Item) -> int:
    """Character offset just past everything before the question paragraph (-1 if not found)."""
    end = max(item.prompt.rfind(marker) for marker in INSTRUCTIONS)
    return -1 if end < 0 else item.prompt.rfind("\n\n", 0, end)


def answer_start(output: str, text: str) -> int:
    """Character offset of the final answer in a response (-1 if none)."""
    if not output.startswith("probability"):
        return 0
    matches = list(ANSWER_JSON.finditer(text))
    return matches[-1].start() if matches else -1


def token_at(offsets: list[tuple[int, int]], char_end: int) -> int:
    """Index of the last token that starts before ``char_end``: the token holding its last character."""
    found = [k for k, (start, end) in enumerate(offsets) if start < char_end and end > start]
    return found[-1] if found else -1


class Replayer:
    def __init__(self, model: str):
        import torch
        self.torch = torch
        self.lm, self.tok = load_hf_model(model)
        self.lm.eval()
        text_cfg = self.lm.config.get_text_config()
        self.layers, self.hidden = text_cfg.num_hidden_layers + 1, text_cfg.hidden_size
        self.kwargs = template_kwargs(model)

    def tokens_and_positions(self, item, record):
        template = render_chat(self.tok, item.chat(), self.kwargs)
        enc = self.tok(template, add_special_tokens=False, return_offsets_mapping=True)
        prompt = record["prompt_token_ids"]
        reply = record["token_ids"]
        if list(enc["input_ids"]) != prompt:
            raise ValueError(f"replay tokenizer disagrees with the cached prompt for {item.key}")
        def decode(ids):
            return self.tok.decode(ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)

        decoded = decode(reply)
        answer = answer_start(item.output, decoded)
        pre = -1
        if answer >= 0:
            # Find the last complete emitted token before the brace, including when a token
            # contains both preceding whitespace and the brace. Never retokenize the answer.
            left, right = 0, len(reply)
            while left < right:
                mid = (left + right + 1) // 2
                if len(decode(reply[:mid])) <= answer:
                    left = mid
                else:
                    right = mid - 1
            pre = len(prompt) + left - 1
        evidence = evidence_end(item)
        start = template.find(item.prompt)
        end = token_at(enc["offset_mapping"], start + evidence) if evidence >= 0 and start >= 0 else -1
        return prompt + reply, [end, len(prompt) - 1, pre], len(prompt)

    def run(self, ids, index):
        torch = self.torch
        with torch.inference_mode():
            out = self.lm(torch.tensor([ids], device=self.lm.device), output_hidden_states=True)
        acts = np.zeros((len(POSITIONS), self.layers, self.hidden), dtype=np.uint16)
        for p, k in enumerate(index):
            if k >= 0:
                stacked = torch.stack([h[0, k] for h in out.hidden_states]).to(torch.bfloat16)
                acts[p] = stacked.view(torch.int16).cpu().numpy().view(np.uint16)
        return acts


def index_digest(index: pd.DataFrame) -> str:
    return hashlib.sha256(json.dumps(index.to_dict("records"), sort_keys=True).encode()).hexdigest()


def verified_files(folder):
    meta = json.loads((folder / "meta.json").read_text())
    index = pd.read_parquet(folder / "index.parquet")
    path = folder / "acts.npy"
    raw = np.load(path, mmap_mode="r")
    if (not meta.get("complete") or list(raw.shape) != meta["shape"] or raw.dtype != np.uint16
            or len(index) != len(raw) or not np.array_equal(index.row, np.arange(len(index)))
            or list(meta["positions"]) != list(POSITIONS) or index_digest(index) != meta.get("items_sha256")):
        raise ValueError(f"inconsistent activation metadata or item index in {folder}")
    if file_sha256(path) != meta.get("array_sha256"):
        raise ValueError(f"activation array checksum mismatch in {folder}")
    return raw, index, meta


def load(folder: Path):
    """Verify the saved capture before decoding bfloat16 activations to float32."""
    import torch

    folder = Path(folder)
    with (folder / ".freeze.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        raw, index, meta = verified_files(folder)
        acts = torch.from_numpy(np.array(raw).view(np.int16)).view(torch.bfloat16).float().numpy()
    return acts, index, meta


def freeze(model: str, dataset: str, case_ids: list[str], conditions: list[str] | None, replayer: Replayer) -> Path:
    folder = C.ACTIVATIONS / model / dataset
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".freeze.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _freeze(folder, model, dataset, case_ids, conditions, replayer)


def _freeze(folder, model, dataset, case_ids, conditions, replayer):
    from tqdm import tqdm

    items = freeze_items(model, dataset, case_ids, conditions)
    records = lookup(items)
    meta = {"model": model, "weights": C.model_config(model).get("path"),
            "revision": model_revision(model), "dataset": dataset, "positions": list(POSITIONS),
            "shape": [len(items), len(POSITIONS), replayer.layers, replayer.hidden],
            "dtype": "bfloat16 stored as uint16 bits (torch.from_numpy(a.view(np.int16)).view(torch.bfloat16))",
            "layers": "hidden_states: 0 = embeddings, i = after block i, last = after the final norm",
            "chat_template": replayer.kwargs, "settings": settings(), "complete": False}
    done_path = folder / "meta.json"
    entries = []
    for item, record in zip(items, records, strict=True):
        if not record or not record.get("ok") or not record.get("prompt_token_ids") or "token_ids" not in record:
            raise ValueError(f"activation replay needs a successful generation with token IDs for {item.key}")
        ids, index, n_prompt = replayer.tokens_and_positions(item, record)
        response_hash = hashlib.sha256(json.dumps([record["text"], record["token_ids"]]).encode()).hexdigest()
        entries.append({"row": len(entries), "item_key": item.key, "case_id": item.case_id, "condition": item.condition,
                        "output": item.output, "replicate": item.replicate, "sample": item.sample, "has_response": True,
                        "n_tokens": len(ids), "n_prompt_tokens": n_prompt, "prompt_sha": item.prompt_sha,
                        "response_sha": response_hash, "settings_sha": record["settings_sha"],
                        **{f"token_{p}": k for p, k in zip(POSITIONS, index, strict=True)}})
    manifest = pd.DataFrame(entries)
    meta["items_sha256"] = index_digest(manifest)
    if done_path.exists():
        try:
            previous = json.loads(done_path.read_text())
        except ValueError:
            previous = {}
        if previous.get("complete") and all(previous.get(k) == v for k, v in meta.items() if k != "complete"):
            try:
                verified_files(folder)
            except (OSError, ValueError, KeyError):
                pass  # The cache identifies the requested capture. Rebuild its damaged files.
            else:
                print(f"[freeze {model}/{dataset}] verified {folder}", flush=True)
                return folder
    temporary = done_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(meta, indent=2, default=str))
    temporary.replace(done_path)
    acts = np.lib.format.open_memmap(folder / "acts.npy", mode="w+", dtype=np.uint16, shape=tuple(meta["shape"]))
    started = time.time()
    interval = float(os.environ.get("SO_PROGRESS_INTERVAL", 30))
    for n, record in enumerate(tqdm(records, desc=f"freeze {model}/{dataset}", unit="item", mininterval=interval)):
        index = [entries[n][f"token_{p}"] for p in POSITIONS]
        acts[n] = replayer.run(record["prompt_token_ids"] + record["token_ids"], index)
    acts.flush()
    del acts
    meta["array_sha256"] = file_sha256(folder / "acts.npy")
    manifest.to_parquet(folder / "index.parquet", index=False)
    meta.update(complete=True, minutes=round((time.time() - started) / 60, 2),
                missing={p: int(sum(r[f"token_{p}"] < 0 for r in entries)) for p in POSITIONS})
    temporary.write_text(json.dumps(meta, indent=2, default=str))
    temporary.replace(done_path)
    print(f"[freeze {model}/{dataset}] {len(entries)} items in {meta['minutes']} min -> {folder}; "
          f"positions not found: {meta['missing']}", flush=True)
    return folder


def main() -> None:
    from belief_leakage.run_generation import dataset_cases

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", nargs="+", default=None)
    parser.add_argument("--cases", default=None, help="'all', an integer, or a file of case ids")
    parser.add_argument("--conditions", nargs="+", default=None)
    args = parser.parse_args()
    if C.model_config(args.model)["backend"] not in ("vllm", "hf"):
        print(f"{args.model}: no local weights to replay (backend {C.model_config(args.model)['backend']})")
        return
    replayer = Replayer(args.model)
    conditions = args.conditions
    for dataset in args.dataset or C.datasets_for(args.model):
        freeze(args.model, dataset, dataset_cases(dataset, args.cases), conditions, replayer)


if __name__ == "__main__":
    main()
