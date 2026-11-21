"""Freeze residual-stream activations for later interpretability work (no generation).

    python -m second_order.freeze --model qwen3_8b [--dataset ckd bespoke]

vLLM does not expose hidden states, so this is a separate prefill-only pass with
transformers, after generation: each prompt is replayed together with the model's
cached response (exactly as generated), and the residual stream is saved at every
layer and three positions:

* ``evidence_end``: the last token of the evidence the model is shown (the CKD
  profile block, or the bespoke ``Evidence:`` list);
* ``prompt_end``: the last token of the chat-formatted prompt (where generation began);
* ``pre_answer``: the last token before the final answer (the last ``{"probability"``
  JSON object, or the last ``Choice:`` line), after the model's reasoning.

Which items: ``activations`` in the config (default: the numeric-probability output of
every case and condition, main run, first sample), so that every belief the estimators
use has its activations.  Layers: transformers' ``hidden_states``: index 0 is the
embedding output, i is the residual stream after block i, and the last index has the
final norm applied (Qwen3).

Output in ``data/activations/<model>/<dataset>/``:

* ``acts.npy``: ``[items, 3 positions, layers + 1, hidden]``, bfloat16 stored bit for bit
  as uint16 (load with ``torch.from_numpy(a).view(torch.bfloat16)``, or
  ``freeze.load(path)``);
* ``index.parquet``: one row per item (case, condition, output, run, sample, token
  counts, the token index of each position, -1 where a position was not found);
* ``meta.json``: model, revision, dtype, positions, layer convention, shapes.

About 0.9 MB per item for Qwen3 8B (37 x 4,096) and 1.3 MB for Qwen3 14B (41 x 5,120).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import config as C
from second_order.run_generation import Item, experiment_items, lookup, template_kwargs

POSITIONS = ("evidence_end", "prompt_end", "pre_answer")
ANSWER_JSON = re.compile(r'\{\s*"probability"')
ANSWER_CHOICE = re.compile(r"(?im)^[ \t]*[*_#`]*[ \t]*Choice\b")


def settings() -> dict:
    return {"outputs": {"ckd": ["probability"], "bespoke": ["probability"]}, "samples": 1, "replicates": [0],
            **(C.load().get("activations") or {})}


def freeze_items(model: str, dataset: str, case_ids: list[str], conditions: list[str]) -> list[Item]:
    s = settings()
    items = experiment_items(model, dataset, case_ids, conditions, tuple(s["outputs"].get(dataset, [])))
    return [i for i in items if i.replicate in s["replicates"] and i.sample < int(s["samples"])]


def evidence_end(item: Item) -> int:
    """Character offset just past the evidence block in the prompt (-1 if not found)."""
    if item.dataset == "bespoke":
        from second_order import bespoke as BS

        found = BS.items_by_case()[item.case_id]
        block = BS.EVIDENCE_HEADER + BS.evidence_lines(found.observations(BS.shown_setting(item.condition)))
    else:
        from second_order.conditions import model_profile
        from second_order.prompts_second_order import PROFILE_HEADER, cases

        block = PROFILE_HEADER + model_profile(cases().loc[item.case_id], item.condition)
    start = item.prompt.find(block)
    return -1 if start < 0 else start + len(block)


def answer_start(output: str, text: str) -> int:
    pattern = ANSWER_JSON if output == "probability" else ANSWER_CHOICE
    matches = list(pattern.finditer(text))
    return matches[-1].start() if matches else -1


def token_at(offsets: list[tuple[int, int]], char_end: int) -> int:
    """Index of the last token that starts before ``char_end`` (-1 if none)."""
    found = [k for k, (start, end) in enumerate(offsets) if start < char_end and end > start]
    return found[-1] if found else -1


class Replayer:
    def __init__(self, model: str):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        cfg = C.model_config(model)
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(cfg["path"], revision=cfg.get("revision"))
        self.lm = AutoModelForCausalLM.from_pretrained(cfg["path"], revision=cfg.get("revision"),
                                                       torch_dtype=getattr(torch, cfg.get("dtype", "bfloat16")),
                                                       device_map=cfg.get("device_map", "auto"))
        self.lm.eval()
        text_cfg = self.lm.config.get_text_config()
        self.layers, self.hidden = text_cfg.num_hidden_layers + 1, text_cfg.hidden_size
        self.kwargs = template_kwargs(model)

    def encode(self, prompt: str, response: str):
        template = self.tok.apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True,
                                                tokenize=False, **self.kwargs)
        prompt_ids = self.tok(template, add_special_tokens=False)["input_ids"]
        enc = self.tok(template + response, add_special_tokens=False, return_offsets_mapping=True)
        if list(enc["input_ids"][:len(prompt_ids)]) != list(prompt_ids):  # a token straddles the boundary
            reply = self.tok(response, add_special_tokens=False, return_offsets_mapping=True)
            base = self.tok(template, add_special_tokens=False, return_offsets_mapping=True)
            ids = list(base["input_ids"]) + list(reply["input_ids"])
            offsets = list(base["offset_mapping"]) + [(a + len(template), b + len(template))
                                                      for a, b in reply["offset_mapping"]]
        else:
            ids, offsets = list(enc["input_ids"]), list(enc["offset_mapping"])
        return template, ids, offsets, len(prompt_ids)

    def run(self, item: Item, response: str | None):
        torch = self.torch
        template, ids, offsets, n_prompt = self.encode(item.prompt, response or "")
        prompt_at = template.find(item.prompt)
        evidence = evidence_end(item)
        answer = answer_start(item.output, response) if response else -1
        index = [token_at(offsets, prompt_at + evidence) if evidence >= 0 and prompt_at >= 0 else -1,
                 n_prompt - 1,
                 token_at(offsets, len(template) + answer) if answer >= 0 else -1]
        with torch.inference_mode():
            out = self.lm(torch.tensor([ids], device=self.lm.device), output_hidden_states=True)
        acts = np.zeros((len(POSITIONS), self.layers, self.hidden), dtype=np.uint16)
        for p, k in enumerate(index):
            if k >= 0:
                stacked = torch.stack([h[0, k] for h in out.hidden_states]).to(torch.bfloat16)
                acts[p] = stacked.view(torch.int16).cpu().numpy().view(np.uint16)
        return acts, index, len(ids), n_prompt


def load(folder: Path):
    """(activations as a float32 array [items, positions, layers, hidden], index, meta)."""
    import torch

    raw = np.load(Path(folder) / "acts.npy", mmap_mode="r")
    acts = torch.from_numpy(np.array(raw).view(np.int16)).view(torch.bfloat16).float().numpy()
    return acts, pd.read_parquet(Path(folder) / "index.parquet"), json.loads((Path(folder) / "meta.json").read_text())


def freeze(model: str, dataset: str, case_ids: list[str], conditions: list[str], replayer: Replayer) -> Path:
    from tqdm import tqdm

    folder = C.ACTIVATIONS / model / dataset
    folder.mkdir(parents=True, exist_ok=True)
    items = freeze_items(model, dataset, case_ids, conditions)
    records = lookup(items)
    meta = {"model": model, "weights": C.model_config(model).get("path"),
            "revision": C.model_config(model).get("revision"), "dataset": dataset, "positions": list(POSITIONS),
            "shape": [len(items), len(POSITIONS), replayer.layers, replayer.hidden],
            "dtype": "bfloat16 stored as uint16 bits (torch.from_numpy(a.view(np.int16)).view(torch.bfloat16))",
            "layers": "hidden_states: 0 = embeddings, i = after block i, last = after the final norm",
            "chat_template": replayer.kwargs, "settings": settings(), "complete": False}
    done_path = folder / "meta.json"
    if done_path.exists() and json.loads(done_path.read_text()).get("complete") \
            and json.loads(done_path.read_text())["shape"] == meta["shape"]:
        print(f"[freeze {model}/{dataset}] already complete: {folder}", flush=True)
        return folder
    acts = np.lib.format.open_memmap(folder / "acts.npy", mode="w+", dtype=np.uint16, shape=tuple(meta["shape"]))
    rows = []
    started = time.time()
    interval = float(os.environ.get("SO_PROGRESS_INTERVAL", 30))
    for n, (item, record) in enumerate(tqdm(list(zip(items, records)), desc=f"freeze {model}/{dataset}",
                                            unit="item", mininterval=interval)):
        response = record["text"] if record and record.get("ok") else None
        a, index, n_tokens, n_prompt = replayer.run(item, response)
        acts[n] = a
        rows.append({"row": n, "case_id": item.case_id, "condition": item.condition, "output": item.output,
                     "replicate": item.replicate, "sample": item.sample, "has_response": response is not None,
                     "n_tokens": n_tokens, "n_prompt_tokens": n_prompt, "prompt_sha": item.prompt_sha,
                     **{f"token_{p}": k for p, k in zip(POSITIONS, index)}})
    acts.flush()
    del acts
    pd.DataFrame(rows).to_parquet(folder / "index.parquet", index=False)
    meta.update(complete=True, minutes=round((time.time() - started) / 60, 2),
                missing={p: int(sum(r[f"token_{p}"] < 0 for r in rows)) for p in POSITIONS})
    done_path.write_text(json.dumps(meta, indent=2, default=str))
    print(f"[freeze {model}/{dataset}] {len(rows)} items in {meta['minutes']} min -> {folder}; "
          f"positions not found: {meta['missing']}", flush=True)
    return folder


def main() -> None:
    from second_order.run_generation import dataset_cases

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
    conditions = args.conditions or C.scheduled_conditions(args.model)
    for dataset in args.dataset or C.datasets_for(args.model):
        freeze(args.model, dataset, dataset_cases(dataset, args.cases, False), conditions, replayer)


if __name__ == "__main__":
    main()
