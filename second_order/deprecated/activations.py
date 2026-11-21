"""Replay and residual-stream extraction on a local MLX checkpoint (plan, Phase 4).

For each case and condition, four cached prompt-and-response pairs are fed
through the local model (no generation): numeric probability, Likert,
probability bin and plain decision.  Post-block residuals at every layer are
saved at three positions:

* P1: last token of the profile block;
* P2: last prompt token (end of the chat generation prefix);
* P3: last token before the final answer (the ``{"probability"`` JSON object
  or the final ``Choice:`` line), after the model's reasoning.

Where a condition has no cached response (e.g. hosted models before the
B/C/D runs), the prompt alone is replayed and P3 is left missing.  The mean
log-probability of the replayed response under the local model is stored as
a fidelity check for hosted responses.

Per-item files ``data/activations/{model}/items/{condition}/{case}__{replay}.npz``
are resumable; ``pack`` consolidates them into one ``[items, hidden]`` array
per position and layer for probe fitting.

    python -m second_order.deprecated.activations extract --model gemma3_4b --act-model gemma3_4b_mlx \
        --conditions A B C_john D_john M_S4 B_F C_F M_F [--cases all|smoke]
    python -m second_order.deprecated.activations pack --model gemma3_4b
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time

import numpy as np
import pandas as pd

from second_order import config as C
from second_order.prompts_second_order import profile_end_offset, prompt_for
from second_order.run_generation import Item, lookup, select_cases, smoke_cases

REPLAYS = ("probability", "likert", "probability_bin_quintile", "decision_no_threshold")
HALF = {"probability": "h1", "likert": "h1", "probability_bin_quintile": "h2",
        "decision_no_threshold": "h2"}
POSITIONS = ("P1", "P2", "P3")
ANSWER_JSON = re.compile(r'\{\s*"(?:probability|posterior_probability|ckd_probability)"')
ANSWER_CHOICE = re.compile(r"(?im)^[ \t]*[*_#`]*[ \t]*Choice\b")


def answer_offset(output: str, text: str) -> int | None:
    pattern = ANSWER_JSON if output == "probability" else ANSWER_CHOICE
    matches = list(pattern.finditer(text))
    return matches[-1].start() if matches else None


class Extractor:
    def __init__(self, act_model: str):
        import mlx.core as mx
        from mlx_lm import load

        self.mx = mx
        path = C.ROOT / C.model_config(act_model)["path"]
        self.model, self.tok = load(str(path))
        self.core = self.model.language_model.model
        self.hf = self.tok._tokenizer
        self.revision = json.loads((path / "config.json").read_text()).get("_name_or_path", str(path))

    def encode(self, prompt: str, response: str | None):
        template = self.tok.apply_chat_template([{"role": "user", "content": prompt}],
                                                add_generation_prompt=True, tokenize=False)
        ids_ref = self.tok.apply_chat_template([{"role": "user", "content": prompt}],
                                               add_generation_prompt=True)
        text = template + (response or "")
        enc = self.hf(text, add_special_tokens=False, return_offsets_mapping=True)
        ids, offsets = enc["input_ids"], enc["offset_mapping"]
        n_prompt = len(ids_ref)
        if list(ids[:n_prompt]) != list(ids_ref):
            raise ValueError("chat-template tokenization mismatch")
        return template, ids, offsets, n_prompt

    def forward(self, ids: list[int], positions: list[int], n_prompt: int, score: bool):
        mx = self.mx
        from mlx_lm.models.base import create_attention_mask

        core = self.core
        tokens = mx.array([ids])
        h = core.embed_tokens(tokens)
        h *= mx.array(core.args.hidden_size ** 0.5, mx.bfloat16).astype(h.dtype)
        global_mask = create_attention_mask(h, None)
        local_mask = create_attention_mask(h, None, window_size=core.window_size)
        pos = mx.array(positions)
        vectors = []
        for j, layer in enumerate(core.layers):
            mask = global_mask if j % core.sliding_window_pattern == core.sliding_window_pattern - 1 \
                else local_mask
            h = layer(h, mask, None)
            vectors.append(h[0, pos].astype(mx.float32))
        acts = mx.stack(vectors)  # [layers, positions, hidden]
        logprob = (np.nan, 0)
        if score and len(ids) > n_prompt:
            normed = core.norm(h[0, n_prompt - 1:-1])
            lm = self.model.language_model
            logits = core.embed_tokens.as_linear(normed) if lm.tie_word_embeddings else lm.lm_head(normed)
            logits = logits.astype(mx.float32)
            lse = mx.logsumexp(logits, axis=-1)
            target = mx.array(ids[n_prompt:])
            picked = mx.take_along_axis(logits, target[:, None], axis=-1)[:, 0]
            lp = picked - lse
            mx.eval(lp)
            logprob = (float(lp.sum()), len(ids) - n_prompt)
        mx.eval(acts)
        return np.array(acts, copy=True), logprob

    def check_identity(self, ids: list[int]) -> float:
        """Max abs difference between the manual pass and the model's own logits (last token)."""
        mx = self.mx
        ref = self.model(mx.array([ids]))[0, -1].astype(mx.float32)
        core = self.core
        acts, _ = self.forward(ids, [len(ids) - 1], len(ids), False)
        h = mx.array(acts[-1, 0])[None]
        lm = self.model.language_model
        normed = core.norm(h)
        mine = (core.embed_tokens.as_linear(normed) if lm.tie_word_embeddings else lm.lm_head(normed))[0]
        return float(mx.max(mx.abs(mine.astype(mx.float32) - ref)))


class HFExtractor:
    """Same interface on a CUDA/CPU machine via Hugging Face transformers.

    ``hidden_states[i + 1]`` is the residual after block i.  In most HF
    decoder implementations the last entry has the final norm applied; this
    is recorded in provenance and the last layer should not be compared
    across backends.
    """

    def __init__(self, act_model: str):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        cfg = C.model_config(act_model)
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(cfg["path"])
        self.hf = self.tok
        self.model = AutoModelForCausalLM.from_pretrained(
            cfg["path"], torch_dtype=getattr(torch, cfg.get("dtype", "bfloat16")), device_map="auto")
        self.model.eval()
        self.n_layers = self.model.config.get_text_config().num_hidden_layers
        self.hidden = self.model.config.get_text_config().hidden_size
        self.revision = cfg["path"]

    def encode(self, prompt: str, response: str | None):
        template = self.tok.apply_chat_template([{"role": "user", "content": prompt}],
                                                add_generation_prompt=True, tokenize=False)
        ids_ref = self.tok(template, add_special_tokens=False)["input_ids"]
        enc = self.tok(template + (response or ""), add_special_tokens=False, return_offsets_mapping=True)
        if list(enc["input_ids"][:len(ids_ref)]) != list(ids_ref):
            raise ValueError("chat-template tokenization mismatch")
        return template, enc["input_ids"], enc["offset_mapping"], len(ids_ref)

    def forward(self, ids, positions, n_prompt, score):
        torch = self.torch
        with torch.no_grad():
            x = torch.tensor([ids], device=self.model.device)
            out = self.model(x, output_hidden_states=True)
            acts = torch.stack([h[0, positions].float() for h in out.hidden_states[1:]]).cpu().numpy()
            logprob = (np.nan, 0)
            if score and len(ids) > n_prompt:
                lp = torch.log_softmax(out.logits[0, n_prompt - 1:-1].float(), dim=-1)
                picked = lp.gather(1, x[0, n_prompt:, None])[:, 0]
                logprob = (float(picked.sum()), len(ids) - n_prompt)
        return acts, logprob

    def check_identity(self, ids) -> float:
        return 0.0  # hidden states come from the model's own forward pass


def token_index(offsets, char_end: int) -> int:
    """Index of the last token starting before ``char_end``."""
    idx = [i for i, (s, e) in enumerate(offsets) if s < char_end and e > s]
    return idx[-1]


def extract(model: str, act_model: str, conditions: list[str], case_ids: list[str]) -> None:
    root = C.ACTIVATIONS / model
    backend = C.model_config(act_model)["backend"]
    ex = Extractor(act_model) if backend == "mlx" else HFExtractor(act_model)
    n_layers = len(ex.core.layers) if backend == "mlx" else ex.n_layers
    hidden = int(ex.core.args.hidden_size) if backend == "mlx" else ex.hidden
    provenance = {"response_model": model, "activation_model": act_model, "backend": backend,
                  "checkpoint": str(C.model_config(act_model)["path"]), "revision": ex.revision,
                  "layers": n_layers,
                  "hidden": hidden, "positions": POSITIONS, "replays": REPLAYS,
                  "location": "post transformer block, before final RMSNorm", "dtype": "float32"}
    root.mkdir(parents=True, exist_ok=True)
    (root / "provenance.json").write_text(json.dumps(provenance, indent=2))
    items = [Item(model, case_id, condition, output, 0, prompt_for(model, case_id, condition, output))
             for condition in conditions for case_id in case_ids for output in REPLAYS]
    records = lookup(items)
    started, done, checked = time.time(), 0, (root / "identity_check.json").exists()
    todo = [(i, r) for i, r in zip(items, records)
            if not (root / "items" / i.condition / f"{i.case_id}__{i.output}.npz").exists()]
    print(f"{model}: {len(items)} items, {len(todo)} to extract", flush=True)
    for item, record in todo:
        response = record["text"] if record is not None and record.get("ok") else None
        template, ids, offsets, n_prompt = ex.encode(item.prompt, response)
        if not checked:
            diff = ex.check_identity(ids[:n_prompt])
            (root / "identity_check.json").write_text(json.dumps({"max_abs_logit_diff": diff}))
            print(f"identity check (manual pass vs model logits): max abs diff {diff:.3g}", flush=True)
            checked = True
        # The chat template trims the message, so locate the stripped prompt.
        lead = len(item.prompt) - len(item.prompt.lstrip())
        prompt_start = template.index(item.prompt.strip()) - lead
        p1 = token_index(offsets, prompt_start + profile_end_offset(item.prompt, item.case_id, item.condition))
        p2 = n_prompt - 1
        p3 = -1
        if response is not None:
            off = answer_offset(item.output, response)
            if off is not None:
                p3 = token_index(offsets, len(template) + off)
        positions = [p1, p2, p3 if p3 >= 0 else p2]
        acts, (lp_sum, n_resp) = ex.forward(ids, positions, n_prompt, score=response is not None)
        if p3 < 0:
            acts[:, 2, :] = np.nan
        out = root / "items" / item.condition / f"{item.case_id}__{item.output}.npz"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.npz")
        np.savez(tmp, acts=acts.astype(np.float32), positions=np.array([p1, p2, p3]),
                 logprob_sum=lp_sum, n_response_tokens=n_resp, n_tokens=len(ids),
                 prompt_sha=hashlib.sha256(item.prompt.encode()).hexdigest(),
                 has_response=response is not None)
        tmp.replace(out)
        done += 1
        if done % 50 == 0:
            rate = (time.time() - started) / done
            print(f"{done}/{len(todo)} {rate:.2f}s/item eta {rate * (len(todo) - done) / 60:.1f} min",
                  flush=True)


def pack(model: str) -> None:
    root = C.ACTIVATIONS / model
    files = sorted((root / "items").glob("*/*.npz"))
    index = []
    first = np.load(files[0])
    n_layers, _, hidden = first["acts"].shape
    (root / "packed").mkdir(parents=True, exist_ok=True)
    arrays = {(p, l): np.lib.format.open_memmap(root / "packed" / f"{p}_L{l:02d}.npy", mode="w+",
                                                 dtype=np.float32, shape=(len(files), hidden))
              for p in POSITIONS for l in range(n_layers)}
    for i, path in enumerate(files):
        data = np.load(path)
        case_id, replay = path.stem.split("__")
        index.append({"row": i, "condition": path.parent.name, "case_id": case_id, "replay": replay,
                      "has_response": bool(data["has_response"]),
                      "logprob_sum": float(data["logprob_sum"]),
                      "n_response_tokens": int(data["n_response_tokens"]),
                      "p1": int(data["positions"][0]), "p2": int(data["positions"][1]),
                      "p3": int(data["positions"][2])})
        for pi, p in enumerate(POSITIONS):
            for l in range(n_layers):
                arrays[(p, l)][i] = data["acts"][l, pi]
    for a in arrays.values():
        a.flush()
    pd.DataFrame(index).to_parquet(root / "packed" / "index.parquet", index=False)
    print(f"packed {len(files)} items x {n_layers} layers x {len(POSITIONS)} positions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--model", required=True, help="model whose responses are replayed")
    e.add_argument("--act-model", required=True, help="local checkpoint (config key)")
    e.add_argument("--conditions", nargs="+", required=True)
    e.add_argument("--cases", default="all")
    p = sub.add_parser("pack")
    p.add_argument("--model", required=True)
    args = parser.parse_args()
    if args.cmd == "pack":
        pack(args.model)
        return
    case_ids = smoke_cases() if args.cases == "smoke" else select_cases(args.cases)
    extract(args.model, args.act_model, args.conditions, case_ids)


if __name__ == "__main__":
    main()
