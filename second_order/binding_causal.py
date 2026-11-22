"""Binding experiment, session 2 (HF transformers with hooks): the no-reasoning arm, probes and causal tests.

Run after session 1 (``binding`` profile ``full``) and its analysis.  Stages, in order:

    readout     no-reasoning answers for every Phase 3 cell (all patients; A/B both replicates): the prompt's
                reasoning instruction becomes "Do not explain...", the answer prefix '{"probability": 0.' is
                forced, and the belief is the exact expected value over two digits (second digit from the
                KV cache, so interventions apply to both).  Also saves the residual stream of fit-half
                patients at the probe positions, every layer.
    gate        lambda per cell from the no-reasoning beliefs (``binding_analysis`` estimators) against
                session 1's reasoning lambda: a cell enters Phase 3 only if the intervals overlap and the
                order effect has the same sign.
    probes      fit half only: ridge probes for PERSON's correct belief (w_attr, trained on correctly bound
                cells) and for it inside a misbound cell (w_rec, orthogonalised against w_attr); transfer;
                then the lock: layer window L*, position and steering scale by a fixed rule, written to
                ``lock.json`` before anything touches the test half.
    patch       3.1 interchange patching (test half): one position-role at one layer, donor -> recipient
                (sufficiency) and back (necessity), every layer in one batch.
    knockout    3.2 attention-edge knockouts on filler-augmented prompts (readout, encoding, update), each
                against the same knockout aimed at a neutral filler block, swept over start layers.
    directions  3.3 mean-ablation of w_attr, w_rec and 20 random directions over L* (question positions),
                and steering at the answer position, on misbound C, on M (references) and on D (specificity).
    report      gap closed, lambda under each intervention, the 3.x decision rules -> RESULTS.md.

    python -m second_order.binding_causal <stage> --run runs/binding-causal --source runs/binding-full --model qwen3_8b
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import binding as B
from second_order import config as C
from second_order.beliefs import clip_logit

PREFIX = '{"probability": 0.'
REASONING = ("Provide your reasoning first.\nThen output valid JSON on a new line in exactly this structure:\n"
             '{"probability": <number>}\n\nThe number must be between 0 and 1.')
NO_REASONING = ('Do not explain. Output only valid JSON in exactly this structure:\n'
                '{"probability": <number>}\n\nThe number must be between 0 and 1.')
FILLER = "\n".join([
    "Clinic notice (not about this patient):",
    "The waiting room is open from 8 am to 6 pm on weekdays.",
    "Parking is available in the lot behind the main building.",
    "Please bring a list of current medications to every visit.",
    "The pharmacy counter closes at 5 pm on Fridays.",
    "Interpreters can be booked at the front desk.",
    "Visitors should sign in at reception.",
    "The cafeteria serves breakfast until 10 am.",
    "Lost items are kept at the security office for 30 days.",
    "Wheelchairs are available near the main entrance.",
    "Appointment reminders are sent two days in advance.",
    "Please silence mobile phones in the examination rooms.",
    "(End of clinic notice.)"])
POSITION_ROLES = ("answer", "name", "own_end", "person_end")
PROBE_POSITIONS = ("answer", "gen_start", "name")
DEFAULTS = {
    "cells": ["false_verified", "false_neutral", "false_labelled", "partial_original", "partial_labelled",
              "partial_labelled_explicit", "strong_original", "strong_labelled", "strong_labelled_explicit"],
    "attr_train": {"false": [["false_labelled", "own_first"], ["false_labelled", "person_first"],
                             ["false_verified", "own_first"]],
                   "partial": [["partial_labelled_explicit", "own_first"], ["partial_labelled_explicit", "person_first"]]},
    "rec_train": {"false": [["false_verified", "person_first"]], "partial": [["partial_original", "person_first"]]},
    # recipient (misbound) cell -> donor cells (same patient, same records, correctly bound)
    "patch_pairs": [[["false_verified", "person_first"], ["false_labelled", "person_first"]],
                    [["false_verified", "person_first"], ["false_verified", "own_first"]],
                    [["partial_original", "person_first"], ["partial_labelled_explicit", "person_first"]],
                    [["partial_original", "own_first"], ["partial_labelled_explicit", "own_first"]]],
    "knockout_cells": [["false_verified", "person_first"], ["false_verified", "own_first"],
                       ["partial_original", "own_first"], ["partial_original", "person_first"]],
    "knockout_every": 2,
    "direction_cells": {"false": [["false_verified", "person_first"]], "partial": [["partial_original", "person_first"],
                                                                                    ["partial_original", "own_first"]]},
    "random_directions": 20,       # the null: encoding patterns of probes trained on shuffled targets
    "steer_alphas": [-4, -2, -1, 0, 1, 2, 4],
    "batch": 64,                   # rows per forward pass (halved, and kept halved, on out of memory)
    "lock_tolerance": 0.05,
    "lock_positions": ["name"],    # where the lock may put the directions: not answer/gen_start, which carry
                                   # the upcoming answer itself and would make 3.3 trivially pass
    "ablate_positions": "probe",   # or "readout": every question and answer token, same mean projection
    "patch_halves": ["fit", "test"],  # 3.1 picks each position's layer on the fit half, tests on the test half
    "patch_min_gap": 0.1,          # logits between donor and recipient below which gap closed is undefined
    "report_min_cases": 8,         # fewer patients than this: no lambda for that cell
    "apply_gate": True,            # Phase 3 runs only on cells whose no-reasoning lambda matches session 1's
    "dtype": "float32",
    "tf32": True,                  # TF32 matmuls on the GPU: much faster, ~1e-3 relative precision (the
                                   # benchmark stage measures what that costs in batch invariance)
}


# Prompts and positions
def no_reasoning(prompt: str) -> str:
    if REASONING not in prompt:
        raise ValueError("prompt lacks the probability instruction")
    return prompt.replace(REASONING, NO_REASONING)


def with_filler(prompt: str) -> str:
    return FILLER + "\n\n" + prompt


@dataclass
class Encoded:
    ids: list[int]
    positions: dict[str, int]       # named single positions
    groups: dict[str, list[int]]    # token groups (record blocks, filler, question + answer)
    text: str = ""


def encode(tok, prompt: str, template_kwargs: dict) -> Encoded:
    """Chat template + forced prefix, with token positions of every role the causal tests use."""
    chat = tok.apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True,
                                   tokenize=False, **template_kwargs)
    text = chat + PREFIX
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    offsets = enc["offset_mapping"]
    base = chat.find(prompt)
    if base < 0:
        raise ValueError("prompt not found in the chat template")

    def last_token(char_end):  # token holding character char_end - 1
        found = [k for k, (s, e) in enumerate(offsets) if s < char_end and e > s]
        return found[-1]

    def span(a, b):
        return [k for k, (s, e) in enumerate(offsets) if e > s and s < b and e > a]

    pos, groups = {}, {}
    for blk in B.record_blocks(prompt):
        pos[f"{blk['owner']}_end"] = last_token(base + blk["end"])
        groups[blk["owner"]] = span(base + blk["start"], base + blk["end"])
    if prompt.startswith(FILLER):
        groups["filler"] = span(base, base + len(FILLER))
    question = prompt.rfind("\n\nWhat probability ")
    q_start = base + question + 2
    q_end = base + prompt.index("?", question) + 1
    name = B.person_name(prompt)
    if name:
        at = prompt.rfind(f" {name} ", 0, q_end - base)
        pos["name"] = last_token(base + at + 1 + len(name))
    pos["question_end"] = last_token(q_end)
    pos["prompt_end"] = last_token(base + len(prompt))
    pos["gen_start"] = last_token(len(chat))
    pos["answer"] = len(enc["input_ids"]) - 1
    groups["readout"] = list(range(span(q_start, q_end)[0], len(enc["input_ids"])))
    return Encoded(list(enc["input_ids"]), pos, groups, text)


# Interventions, one per batch row
@dataclass
class Variant:
    patch: list = field(default_factory=list)    # (layer, position, vector)
    steer: list = field(default_factory=list)    # (layer, position, vector)
    ablate: list = field(default_factory=list)   # (layers, positions, unit vector, mean projection)
    knock: list = field(default_factory=list)    # (start layer, query positions, key positions, include new tokens)


class Lens:
    """A causal LM with per-row interventions: residual edits after decoder layers and attention-mask
    knockouts before attention (eager attention, so every layer receives the 4D mask)."""

    def __init__(self, model: str | None = None, *, lm=None, tok=None, device=None, template_kwargs=None,
                 dtype: str = "float32"):
        # float32 by default: in bfloat16 the batch shape alone moves a readout by ~0.03 in probability,
        # as large as the effects the causal tests look for (batched rows are exact in float32).
        import torch
        self.torch = torch
        if lm is None:
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from second_order.run_generation import model_revision, template_kwargs as tk
            cfg = C.model_config(model)
            tok = AutoTokenizer.from_pretrained(cfg["path"], revision=model_revision(model))
            lm = AutoModelForCausalLM.from_pretrained(
                cfg["path"], revision=model_revision(model), torch_dtype=getattr(torch, dtype),
                device_map=cfg.get("device_map", "auto"), attn_implementation="eager")
            template_kwargs = tk(model)
        lm.eval()
        self.lm, self.tok = lm, tok
        self.device = device or next(lm.parameters()).device
        self.kw = template_kwargs if template_kwargs is not None else {"enable_thinking": False}
        self.layers = lm.model.layers
        self.n_layers = len(self.layers)
        self.digits = [tok.convert_tokens_to_ids(str(d)) for d in range(10)]
        if len(set(self.digits)) != 10 or any(d is None or d == tok.unk_token_id for d in self.digits):
            raise ValueError("tokenizer lacks single-digit tokens")
        self._rows: list[Variant] = []
        self._offset = 0
        self._new_tokens = False      # True in the second-digit pass (queries are the appended answer tokens)
        self._tree = 0                # >0: that many parallel candidate tokens, each attending to the prompt only
        self._capture = None          # per row: absolute positions whose residual stream to keep (all layers)
        self._captured = {}
        self._prefix = None           # (key, legacy cache) of the last shared prefix
        self.batch_cap = None         # largest batch that fitted, once one ran out of memory
        self._handles = []
        for i, layer in enumerate(self.layers):
            self._handles.append(layer.register_forward_hook(self._edit_hook(i + 1)))
            self._handles.append(layer.self_attn.register_forward_pre_hook(self._mask_hook(i), with_kwargs=True))
        # Capture follows transformers' hidden_states: 0 = embeddings, i = after block i, last = after the norm.
        self._handles.append(lm.model.embed_tokens.register_forward_hook(self._capture_hook(0)))
        self._handles.append(lm.model.norm.register_forward_hook(self._capture_hook(self.n_layers)))

    def encode(self, prompt: str) -> Encoded:
        return encode(self.tok, prompt, self.kw)

    # hooks
    def _keep(self, index, hs):
        if self._capture is None:
            return
        q = hs.shape[1]
        for r, ps in enumerate(self._capture):
            if ps:
                local = [p - self._offset for p in ps]
                if all(0 <= p < q for p in local):
                    self._captured.setdefault(r, {})[index] = hs[r, local].detach().clone()

    def _capture_hook(self, index):
        def hook(module, args, output):
            self._keep(index, output[0] if isinstance(output, tuple) else output)
            return None
        return hook

    def _edit_hook(self, hidden_index):
        def hook(module, args, output):
            hs = output[0] if isinstance(output, tuple) else output
            if not self._rows:
                if hidden_index < self.n_layers:
                    self._keep(hidden_index, hs)
                return None
            q = hs.shape[1]
            for r, v in enumerate(self._rows):
                for layer, p, vec in v.patch:
                    if layer == hidden_index and 0 <= p - self._offset < q:
                        hs[r, p - self._offset] = vec.to(hs.dtype)
                for layer, p, vec in v.steer:
                    if layer == hidden_index and 0 <= p - self._offset < q:
                        hs[r, p - self._offset] += vec.to(hs.dtype)
                for layers, ps, u, mu in v.ablate:
                    if hidden_index in layers:
                        local = [p - self._offset for p in ps if 0 <= p - self._offset < q]
                        if local:
                            h = hs[r, local].float()
                            u32 = u.float()
                            h = h - ((h @ u32) - mu)[:, None] * u32[None, :]
                            hs[r, local] = h.to(hs.dtype)
            if hidden_index < self.n_layers:  # the last block's output is captured after the final norm
                self._keep(hidden_index, hs)
            return None
        return hook

    def _mask_hook(self, layer_index):
        torch = self.torch

        def hook(module, args, kwargs):
            if not self._rows or (not self._tree and not any(v.knock for v in self._rows)):
                return None
            mask = kwargs.get("attention_mask")
            if mask is None or mask.dim() != 4:
                raise RuntimeError("knockouts need the eager 4D attention mask")
            mask = mask.clone() if mask.shape[0] == len(self._rows) else mask.expand(len(self._rows), *mask.shape[1:]).clone()
            q, k = mask.shape[2], mask.shape[3]
            low = torch.finfo(mask.dtype).min
            if self._tree:
                # Candidate answer tokens are parallel continuations: none sees another.
                t = self._tree
                block = torch.full((t, t), low, dtype=mask.dtype, device=mask.device)
                block.fill_diagonal_(0)
                mask[:, 0, :, k - t:] = block
            for r, v in enumerate(self._rows):
                for start, qs, ks, new in v.knock:
                    if layer_index < start:
                        continue
                    local_q = [p - self._offset for p in qs if 0 <= p - self._offset < q]
                    if new and self._new_tokens:
                        local_q = list(range(q))
                    keys = [p for p in ks if p < k]
                    if local_q and keys:
                        mask[r, 0, torch.tensor(local_q, device=mask.device)[:, None],
                             torch.tensor(keys, device=mask.device)[None, :]] = low
            kwargs["attention_mask"] = mask
            return args, kwargs
        return hook

    # forward passes
    @staticmethod
    def first_affected(variants) -> int | None:
        """Earliest token any intervention changes (nothing before it can differ)."""
        found = [p for v in variants for _, p, _ in v.patch + v.steer]
        found += [min(ps) for v in variants for _, ps, _, _ in v.ablate if ps]
        found += [min(qs) for v in variants for _, qs, _, _ in v.knock if qs]
        return min(found) if found else None

    def _prefix_cache(self, ids, start, n):
        """KV cache of ids[:start] (computed once per prompt prefix, no interventions), repeated for n rows."""
        from transformers import DynamicCache
        torch = self.torch
        key = (hash(tuple(ids[:start])), start)
        if self._prefix is None or self._prefix[0] != key:
            self._rows, self._offset = [], 0
            with torch.inference_mode():
                out = self.lm(torch.tensor([ids[:start]], device=self.device), use_cache=True, logits_to_keep=1)
            self._prefix = (key, out.past_key_values.to_legacy_cache())
        return DynamicCache.from_legacy_cache(tuple((k.repeat(n, 1, 1, 1), v.repeat(n, 1, 1, 1))
                                                    for k, v in self._prefix[1]))

    def _answer(self, logits, cache, rows, width, lengths, mask=None):
        """Expected probability per row: first digit from ``logits``; every candidate first digit's second digit
        in one pass, as parallel continuations at the same position that each see only the prompt (tree mask),
        under the rows' interventions. A first digit with negligible probability in every row is valued at its
        interval midpoint. Returns (expected probability, digit mass of the first digit)."""
        torch = self.torch
        d1 = logits.softmax(-1)[:, self.digits]
        mass = d1.sum(-1)
        p1 = d1 / mass[:, None]
        cand = [a for a in range(10) if float(p1[:, a].max()) >= 1e-6]
        n, t = len(rows), len(cand)
        new = torch.tensor([[self.digits[a] for a in cand]] * n, device=self.device)
        position = torch.tensor(lengths, device=self.device)[:, None].expand(-1, t)
        if mask is not None:
            mask = torch.cat([mask, torch.ones(n, t, dtype=mask.dtype, device=self.device)], 1)
        self._rows, self._offset, self._new_tokens, self._tree = list(rows), width, True, t
        try:
            with torch.inference_mode():
                out = self.lm(new, attention_mask=mask, past_key_values=cache, position_ids=position, use_cache=True,
                              logits_to_keep=t)
        finally:
            self._rows, self._offset, self._new_tokens, self._tree = [], 0, False, 0
        d2 = out.logits[:, -t:].float().softmax(-1)[:, :, self.digits]   # rows, candidates, second digit
        end = (1 - d2.sum(-1)).clamp_min(0)                               # anything else ends the number
        tenths = torch.arange(10, device=self.device, dtype=torch.float32)
        a = torch.tensor(cand, device=self.device, dtype=torch.float32)
        cond = (tenths / 10 + 0.045).repeat(n, 1)
        cond[:, cand] = ((d2 * (a[None, :, None] / 10 + tenths[None, None, :] / 100)).sum(-1)
                         + end * a[None, :] / 10) / (d2.sum(-1) + end)
        return (p1 * cond).sum(-1).cpu().numpy(), mass.cpu().numpy()

    def _captures(self, n):
        """The captured rows (layers + 1, positions, hidden), None for rows that captured nothing."""
        captured, self._captured = self._captured, {}
        return [self.torch.stack([captured[r][i] for i in range(self.n_layers + 1)]) if r in captured else None
                for r in range(n)]

    def readout(self, ids, variants, *, hidden_at=None, reuse_prefix=True):
        """Expected probability over two digits after the forced prefix, one row per variant of one prompt.
        Tokens before the first affected position come from one shared prefix cache (``reuse_prefix``);
        ``hidden_at``: residual stream (every layer) at these positions, computed without the shared prefix."""
        torch = self.torch
        n = len(variants)
        start = 0
        if reuse_prefix and hidden_at is None:
            first = self.first_affected(variants)
            start = len(ids) - 1 if first is None else first
        cache = self._prefix_cache(ids, start, n) if start else None
        # An explicit mask over every key: without one, transformers builds a mask one key wider than the cache,
        # which would shift the tree block of the second-digit pass.
        mask = torch.ones(n, len(ids), dtype=torch.long, device=self.device)
        self._rows, self._offset, self._new_tokens = list(variants), start, False
        self._capture = [hidden_at] * n if hidden_at is not None else None
        try:
            with torch.inference_mode():
                out = self.lm(torch.tensor([ids[start:]] * n, device=self.device), attention_mask=mask,
                              past_key_values=cache, use_cache=True, logits_to_keep=1)
        finally:
            self._rows, self._offset, self._capture = [], 0, None
        hidden = self._captures(n) if hidden_at is not None else None
        p, mass = self._answer(out.logits[:, -1].float(), out.past_key_values, variants, len(ids), [len(ids)] * n,
                               mask)
        return dict(p=p, digit_mass=mass, hidden=None if hidden is None else torch.stack(hidden))

    def readout_batch(self, encodings: list, hidden_at: list | None = None, batch: int = 64):
        """No-intervention readouts of many prompts: left-padded batches (sorted by length) with explicit
        position ids, so every token keeps its position; residual stream (every layer) at each prompt's own
        positions. A batch that runs out of memory is halved and retried."""
        torch = self.torch
        batch = min(batch, self.batch_cap or batch)
        order = sorted(range(len(encodings)), key=lambda i: len(encodings[i].ids))
        out, k = [None] * len(encodings), 0
        while k < len(order):
            chunk = order[k:k + batch]
            try:
                results = self._readout_padded([encodings[i] for i in chunk],
                                               [hidden_at[i] if hidden_at else None for i in chunk])
            except torch.OutOfMemoryError:
                self._rows, self._capture, self._captured = [], None, {}
                if batch == 1:
                    raise
                torch.cuda.empty_cache()
                batch = self.batch_cap = max(1, batch // 2)
                print(f"[lens] out of memory: batch -> {batch}", flush=True)
                continue
            for i, r in zip(chunk, results):
                out[i] = r
            k += len(chunk)
        return [r[0] for r in out], [r[1] for r in out], [r[2] for r in out]

    def _readout_padded(self, encs, hidden_at):
        torch = self.torch
        pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        lengths = [len(e.ids) for e in encs]
        width, n = max(lengths), len(encs)
        ids = torch.tensor([[pad] * (width - m) + e.ids for e, m in zip(encs, lengths)], device=self.device)
        mask = torch.tensor([[0] * (width - m) + [1] * m for m in lengths], device=self.device)
        capture = [[width - m + q for q in a] if a else None for a, m in zip(hidden_at, lengths)]
        self._rows, self._offset = [], 0
        self._capture = capture if any(capture) else None
        try:
            with torch.inference_mode():
                out = self.lm(ids, attention_mask=mask, position_ids=(mask.cumsum(-1) - 1).clamp_min(0),
                              use_cache=True, logits_to_keep=1)
        finally:
            self._capture = None
        hidden = self._captures(n)
        p, mass = self._answer(out.logits[:, -1].float(), out.past_key_values, [Variant()] * n, width, lengths, mask)
        return [(float(p[r]), float(mass[r]), hidden[r]) for r in range(n)]

    def close(self):
        for h in self._handles:
            h.remove()


def logit_of(p):
    return clip_logit(np.asarray(p, dtype=float))


# Cells and prompts from session 1
@lru_cache(maxsize=4)
def _source_prompts(source: str, model: str) -> pd.DataFrame:
    plan = json.loads((Path(source) / "plan.json").read_text())
    t = pd.DataFrame([r for r in plan["trials"] if r["model"] == model and r["output"] == "probability/normal"
                      and r["sample"] == 0 and r["arm"] != "drift"])
    t["prompt"] = [plan["items"][k]["prompt"] for k in t.item_key]
    t = t.drop_duplicates(["case_id", "arm", "block_order", "role", "replicate"])
    return t[["case_id", "split", "arm", "variant", "block_order", "role", "ab_version", "replicate", "prompt"]]


def source_prompts(source: Path, model: str) -> pd.DataFrame:
    """(case, arm, order, role, replicate) -> session-1 prompt of the stated probability (sample 0)."""
    return _source_prompts(str(source), model).copy()


def wanted_cells(prompts: pd.DataFrame, cfg: dict) -> list[str]:
    return [a for a in cfg["cells"] if a in set(prompts.arm)]


def out_dir(run: Path, model: str) -> Path:
    d = run / model
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_cfg(run: Path) -> dict:
    path = run / "causal_config.json"
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


def _sha(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("rb") as h:
        return hashlib.file_digest(h, "sha256").hexdigest()


def _resume(path: Path) -> set[str]:
    """Cases already written to a per-case results file (a stage resumes after them)."""
    return set(pd.read_csv(path, usecols=["case_id"]).case_id) if path.exists() and path.stat().st_size > 1 else set()


def _append(path: Path, rows: list[dict]) -> None:
    if rows:
        pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists() or path.stat().st_size == 0, index=False)


# Stage: readout (+ probe activations on the fit half)
def stage_readout(run: Path, source: Path, model: str, lens: Lens, cfg: dict) -> pd.DataFrame:
    """No-reasoning answer of every Phase 3 cell's prompts (identical prompts, e.g. A, B and D shared by arms,
    are read once), and the fit half's C activations at the probe positions."""
    d = out_dir(run, model)
    need = source_prompts(source, model)
    need = need[need.arm.isin(wanted_cells(need, cfg))]
    done_path = d / "noreason_responses.parquet"
    rows = pd.read_parquet(done_path).to_dict("records") if done_path.exists() else []
    seen = {(r["case_id"], r["arm"], r["block_order"], r["role"], r["replicate"]) for r in rows}
    acts_dir = d / "activations"
    acts_dir.mkdir(exist_ok=True)
    todo = [r for r in need.to_dict("records")
            if (r["case_id"], r["arm"], r["block_order"], r["role"], r["replicate"]) not in seen]
    unique = {}
    for r in todo:
        unique.setdefault(r["prompt"], []).append(r)
    prompts = list(unique)
    chunk = max(int(cfg["batch"]) * 8, 1)
    for k in range(0, len(prompts), chunk):
        part = prompts[k:k + chunk]
        encs = [lens.encode(no_reasoning(p)) for p in part]
        capture = [any(r["split"] == "fit" and r["role"] == "C" and r["replicate"] == 0 for r in unique[p])
                   for p in part]
        at = [[e.positions[q] for q in PROBE_POSITIONS] if c else None for e, c in zip(encs, capture)]
        ps, masses, hidden = lens.readout_batch(encs, at, int(cfg["batch"]))
        for prompt, e, p, mass, h in zip(part, encs, ps, masses, hidden):
            for r in unique[prompt]:
                rows.append(dict(r, p=p, digit_mass=mass, n_tokens=len(e.ids)))
                if h is not None and r["role"] == "C":
                    h16 = h.to(lens.torch.bfloat16).view(lens.torch.int16).cpu().numpy().view(np.uint16)
                    np.save(acts_dir / f"{r['case_id']}__{r['arm']}__{r['block_order']}.npy", h16)
        pd.DataFrame(rows).drop(columns=["prompt"], errors="ignore").to_parquet(done_path, index=False)
        print(f"[readout {model}] {min(k + chunk, len(prompts))}/{len(prompts)} distinct prompts", flush=True)
    frame = pd.DataFrame(rows).drop(columns=["prompt"], errors="ignore")
    frame.to_parquet(done_path, index=False)
    print(f"[readout {model}] {len(frame)} answers; median digit mass {frame.digit_mass.median():.3f}", flush=True)
    return frame


def noreason_beliefs(frame: pd.DataFrame) -> pd.DataFrame:
    b = frame.copy()
    b["value"] = logit_of(b.p)
    b["value_calibrated"] = b["value"]
    b["n_samples"], b["sd"], b["method"] = 1, np.nan, "noreason"
    return b[["case_id", "arm", "block_order", "role", "replicate", "value", "sd", "value_calibrated", "n_samples",
              "method"]]


# Stage: gate (fit half only)
def stage_gate(run: Path, source: Path, model: str, cfg: dict, resamples: int) -> pd.DataFrame:
    """No-reasoning lambda against session 1's reasoning lambda, both on the fit half (``binding_analysis.gate``)."""
    from second_order import binding_analysis as BA
    d = out_dir(run, model)
    frame = pd.read_parquet(d / "noreason_responses.parquet")
    fit = set(frame.case_id[frame.split == "fit"])
    arms = [a for a in cfg["cells"] if a in set(frame.arm)]
    seed = int(C.load()["seed"])
    tables = {}
    reasoning = pd.read_parquet(source / "outputs" / "analysis" / model / "beliefs_direct.parquet")
    for name, beliefs in (("reasoning", reasoning), ("noreason", noreason_beliefs(frame))):
        beliefs = beliefs[beliefs.case_id.isin(fit)]
        tables[name] = BA.bootstrap(BA.wide_frame(beliefs), arms, resamples, seed, checks=False)
        tables[name][0].to_csv(d / f"gate_{name}_lambda_cells.csv", index=False)
        tables[name][1].to_csv(d / f"gate_{name}_contrasts.csv", index=False)
    g = BA.gate(tables["reasoning"], tables["noreason"], arms)
    g.to_csv(d / "gate.csv", index=False)
    print(g.to_string(index=False))
    return g


def gated(d: Path, cfg: dict) -> set[tuple[str, str]] | None:
    """(arm, order) cells that passed the gate, or None when the gate is switched off."""
    if not cfg.get("apply_gate", True):
        return None
    path = d / "gate.csv"
    if not path.exists():
        raise ValueError("run the gate stage before the causal tests")
    g = pd.read_csv(path)
    return set(zip(g.arm[g.enters_phase3], g.order[g.enters_phase3]))


def gate_cfg(d: Path, cfg: dict) -> dict:
    """The config with Phase 3's cells restricted to those that passed the gate (patching: the recipient)."""
    cells = gated(d, cfg)
    if cells is None:
        return cfg
    return cfg | {"patch_pairs": [p for p in cfg["patch_pairs"] if tuple(p[0]) in cells],
                  "knockout_cells": [c for c in cfg["knockout_cells"] if tuple(c) in cells],
                  "direction_cells": {v: [c for c in cs if tuple(c) in cells] for v, cs in cfg["direction_cells"].items()}}


# Stage: probes and lock (fit half only)
def _targets(source: Path, model: str) -> pd.DataFrame:
    """Per case and arm: session-1 truth (l_A) and PERSON's correct belief (l_B), means of replicates."""
    beliefs = pd.read_parquet(source / "outputs" / "analysis" / model / "beliefs_direct.parquet")
    ab = beliefs[beliefs.role.isin(["A", "B"])].groupby(["case_id", "arm", "role"]).value.mean().unstack()
    return ab.rename(columns={"A": "truth", "B": "elena"}).reset_index()


def _load_acts(d: Path, cases, arm, order):
    X, keep = [], []
    for c in cases:
        p = d / "activations" / f"{c}__{arm}__{order}.npy"
        if p.exists():
            X.append(np.load(p))
            keep.append(c)
    if not X:
        return None, []
    import torch
    a = torch.from_numpy(np.stack(X).view(np.int16)).view(torch.bfloat16).float().numpy()
    return a, keep                                  # cases, layers+1, positions, hidden


def residual(y: np.ndarray, truth: np.ndarray) -> np.ndarray:
    """PERSON's belief beyond what the model's own belief explains (OLS residual on l_A)."""
    design = np.column_stack([np.ones_like(truth), truth])
    return y - design @ np.linalg.lstsq(design, y, rcond=None)[0]


ALPHAS = np.logspace(-2, 6, 17)


def group_folds(groups, k: int = 5, seed: int = 0) -> list[np.ndarray]:
    """Row indices of k folds that never split a group (a patient) across folds."""
    uniq = np.array(sorted(set(groups)))
    np.random.default_rng(seed).shuffle(uniq)
    fold = {g: i % k for i, g in enumerate(uniq)}
    of = np.array([fold[g] for g in groups])
    return [np.flatnonzero(of == i) for i in range(min(k, len(uniq)))]


def _centred(K, tr, te):
    """Train-centred kernel blocks (train x train, test x train)."""
    Ktr = K[np.ix_(tr, tr)]
    rm, gm = Ktr.mean(0), Ktr.mean()
    Kte = K[np.ix_(te, tr)]
    return Ktr - rm[None] - rm[:, None] + gm, Kte - Kte.mean(1, keepdims=True) - rm[None] + gm


def choose_alpha(K, y, groups, seed=0) -> float:
    """Ridge penalty by patient-grouped CV (every penalty from one eigendecomposition per fold)."""
    idx = np.arange(len(y))
    err = np.zeros(len(ALPHAS))
    for te in group_folds(groups, seed=seed):
        tr = np.setdiff1d(idx, te)
        Kc, Ktec = _centred(K, tr, te)
        s, U = np.linalg.eigh(Kc)
        s = np.clip(s, 0, None)
        ym = y[tr].mean()
        Uy, KU = U.T @ (y[tr] - ym), Ktec @ U
        err += [np.sum((KU @ (Uy / (s + a)) + ym - y[te]) ** 2) for a in ALPHAS]
    return float(ALPHAS[int(np.argmin(err))])


class Ridge:
    """Kernel (dual) ridge on standardised activations, penalty by patient-grouped CV. ``pattern`` is the
    encoding direction, Cov(activation, prediction) in raw activation space (Haufe et al. 2014): the direction
    the interventions use. Decoder weights are not, since they load on near-constant coordinates."""

    def __init__(self, X, y, groups, seed=0):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-6
        self.Z = (X - self.mu) / self.sd
        K = self.Z @ self.Z.T / self.Z.shape[1]
        self.alpha = choose_alpha(K, y, groups, seed)
        self.row_mean, self.mean = K.mean(0), K.mean()
        Kc = K - self.row_mean[None] - self.row_mean[:, None] + self.mean
        self.ym = y.mean()
        self.coef = np.linalg.solve(Kc + self.alpha * np.eye(len(y)), y - self.ym)
        fitted = Kc @ self.coef
        a = (X - X.mean(0)).T @ (fitted - fitted.mean()) / len(y)
        self.pattern = a / (np.linalg.norm(a) + 1e-12)

    def predict(self, X):
        Kn = ((X - self.mu) / self.sd) @ self.Z.T / self.Z.shape[1]
        return (Kn - Kn.mean(1, keepdims=True) - self.row_mean[None] + self.mean) @ self.coef + self.ym


def grouped_cv_r(X, y, groups, seed=0) -> float:
    """Correlation of out-of-fold predictions with y, folds (and the inner penalty choice) grouped by patient."""
    groups = np.asarray(groups)
    pred = np.zeros(len(y))
    idx = np.arange(len(y))
    for te in group_folds(groups, seed=seed):
        tr = np.setdiff1d(idx, te)
        pred[te] = Ridge(X[tr], y[tr], groups[tr], seed).predict(X[te])
    return float(np.corrcoef(pred, y)[0, 1]) if np.std(pred) > 0 else np.nan


def _training(d, targets, cases, cells):
    """Stacked activations of ``cells`` with PERSON's belief residualised on the truth, and the patient groups."""
    blocks, ys, truths, groups = [], [], [], []
    for arm, order in cells:
        a, keep = _load_acts(d, cases, arm, order)
        if a is None:
            continue
        t = targets[targets.arm == arm].set_index("case_id").loc[keep]
        blocks.append(a)
        ys.append(t.elena.to_numpy())
        truths.append(t.truth.to_numpy())
        groups += keep
    if not blocks:
        return None, None, None, None
    truth = np.concatenate(truths)
    return np.concatenate(blocks), residual(np.concatenate(ys), truth), truth, np.array(groups)


def stage_probes(run: Path, source: Path, model: str, cfg: dict) -> dict:
    """Fit half: the attribution probe (PERSON's belief beyond the truth, in correctly bound cells), the
    misbound-cell probe orthogonalised against it, a null of probes trained on shuffled targets, and the lock."""
    d = out_dir(run, model)
    lock_path = d / "lock.json"
    if lock_path.exists():
        raise ValueError(f"{lock_path} exists: the lock is written once; remove it deliberately to refit")
    frame = pd.read_parquet(d / "noreason_responses.parquet")
    targets = _targets(source, model)
    fit_cases = sorted(set(frame.case_id[frame.split == "fit"]))
    said = frame[(frame.role == "C") & (frame.replicate == 0)].set_index(["case_id", "arm", "block_order"]).p
    rng = np.random.default_rng(int(C.load()["seed"]) + 4242)
    report, arrays = [], {}
    lock = {"rule": (
        "per variant: target = PERSON's belief residualised on the model's own (l_B on l_A); position = argmax over "
        f"{cfg['lock_positions']} of the attribution probe's best-layer CV r (folds and penalty grouped by patient); "
        f"L* = the contiguous layers around the best layer whose CV r is within {cfg['lock_tolerance']} of it; "
        "directions = the probes' encoding patterns; null = the encoding patterns of probes trained on shuffled "
        "targets; steering unit = SD of the training projections on w_attr at the best layer"), "variants": {}}
    for variant, train_cells in cfg["attr_train"].items():
        A, y, truth, groups = _training(d, targets, fit_cases, train_cells)
        if A is None:
            continue
        rec_arm, rec_order = cfg["rec_train"][variant][0]
        R, yr, truth_r, groups_r = _training(d, targets, fit_cases, [(rec_arm, rec_order)])
        n_layers = A.shape[1]
        usable = range(1, n_layers - 1)  # 0 = embeddings; the last index has the final norm applied
        best = {}
        for p, pos in enumerate(PROBE_POSITIONS):
            rs = [grouped_cv_r(A[:, layer, p], y, groups) if layer in usable else np.nan for layer in range(n_layers)]
            report += [dict(variant=variant, probe="attr", position=pos, layer=layer, cv_r=r, n=len(y))
                       for layer, r in enumerate(rs)]
            if pos in cfg["lock_positions"]:
                best[pos] = (int(np.nanargmax(rs)), float(np.nanmax(rs)), rs)
        pos = max(best, key=lambda k: best[k][1])
        p = PROBE_POSITIONS.index(pos)
        top, top_r, rs = best[pos]
        window = [top]
        for step in (-1, 1):
            layer = top + step
            while layer in usable and rs[layer] >= top_r - cfg["lock_tolerance"]:
                window.append(layer)
                layer += step
        window = sorted(window)
        hidden = A.shape[-1]
        u_attr, mu_attr, sd_attr = np.zeros((n_layers, hidden)), np.zeros(n_layers), np.ones(n_layers)
        u_rec, mu_rec = np.zeros((n_layers, hidden)), np.zeros(n_layers)
        rand, mu_rand = np.zeros((int(cfg["random_directions"]), n_layers, hidden)), np.zeros((n_layers, int(cfg["random_directions"])))
        for layer in usable:
            attr = Ridge(A[:, layer, p], y, groups)
            u = attr.pattern
            proj = A[:, layer, p] @ u
            u_attr[layer], mu_attr[layer], sd_attr[layer] = u, proj.mean(), proj.std() + 1e-6
            if R is not None:
                ur = Ridge(R[:, layer, p], yr, groups_r).pattern
                ur = ur - (ur @ u) * u
                u_rec[layer] = ur / (np.linalg.norm(ur) + 1e-12)
                mu_rec[layer] = (R[:, layer, p] @ u_rec[layer]).mean()
                # transfer of the attribution probe into the misbound cell (fit half)
                pred = attr.predict(R[:, layer, p])
                s = said.reindex([(c, rec_arm, rec_order) for c in groups_r]).to_numpy()
                for name, target in (("elena_beyond_truth", yr), ("truth", truth_r), ("said", logit_of(s))):
                    report.append(dict(variant=variant, probe="attr_transfer", position=pos, layer=layer,
                                       target=name, r=float(np.corrcoef(pred, target)[0, 1]), n=len(yr)))
            if layer in window or layer == top:
                for j in range(len(rand)):
                    shuffled = Ridge(A[:, layer, p], rng.permutation(y), groups)
                    rand[j, layer], mu_rand[layer, j] = shuffled.pattern, (A[:, layer, p] @ shuffled.pattern).mean()
        arrays |= {f"{variant}__u_attr": u_attr, f"{variant}__mu_attr": mu_attr, f"{variant}__rand": rand,
                   f"{variant}__mu_rand": mu_rand}
        if R is not None:
            arrays |= {f"{variant}__u_rec": u_rec, f"{variant}__mu_rec": mu_rec}
        lock["variants"][variant] = dict(position=pos, best_layer=top, best_cv_r=top_r, window=window,
                                         steer_unit=float(sd_attr[top]), n_train=len(y), rec_cell=[rec_arm, rec_order])
    pd.DataFrame(report).to_csv(d / "probe_report.csv", index=False)
    np.savez(d / "directions.npz", **arrays)
    lock |= {"directions_sha256": _sha(d / "directions.npz"), "gate_sha256": _sha(d / "gate.csv"), "config": cfg}
    lock_path.write_text(json.dumps(lock, indent=2, default=float))
    print(json.dumps(lock["variants"], indent=2, default=float))
    return lock


def read_lock(d: Path, cfg: dict) -> tuple[dict, dict]:
    """The lock, refusing any change since it was written: the directions, the gate and the config."""
    lock = json.loads((d / "lock.json").read_text())
    if _sha(d / "directions.npz") != lock["directions_sha256"]:
        raise ValueError("directions.npz changed after the lock")
    if _sha(d / "gate.csv") != lock.get("gate_sha256"):
        raise ValueError("gate.csv changed after the lock")
    if json.loads(json.dumps(cfg)) != lock["config"]:
        raise ValueError("the causal config changed after the lock")
    return lock, dict(np.load(d / "directions.npz"))


def halves(frame: pd.DataFrame, which) -> list[str]:
    return sorted(set(frame.case_id[frame.split.isin(list(which))]))


def _prompt_index(source, model):
    p = source_prompts(source, model)
    return {(r.case_id, r.arm, r.block_order, r.role, r.replicate): r.prompt for r in p.itertuples()}


def _batched(lens, ids, variants, size):
    """Readouts of many variants of one prompt; a batch that runs out of memory is halved (and kept halved)."""
    torch = lens.torch
    size = min(size, lens.batch_cap or size)
    out, k = [], 0
    while k < len(variants):
        try:
            out.append(lens.readout(ids, variants[k:k + size])["p"])
            k += size
        except torch.OutOfMemoryError:
            lens._rows, lens._capture, lens._captured = [], None, {}
            if size == 1:
                raise
            torch.cuda.empty_cache()
            size = lens.batch_cap = max(1, size // 2)
            print(f"[lens] out of memory: batch -> {size}", flush=True)
    return np.concatenate(out)


def _per_case(run, source, model, lens, cfg, name, cases, fn):
    """Run ``fn(case)`` for every case not yet in ``<name>.csv``, appending each case's rows as it finishes."""
    d = out_dir(run, model)
    path = d / f"{name}.csv"
    done = _resume(path)
    for case in cases:
        if case not in done:
            _append(path, fn(case))
            print(f"[{name} {model}] {case}", flush=True)
    return pd.read_csv(path) if path.exists() and path.stat().st_size > 1 else pd.DataFrame()


# Stage: 3.1 interchange patching
def patch_case(lens: Lens, prompts: dict, case: str, cfg: dict) -> list[dict]:
    layers = list(range(1, lens.n_layers))  # hidden index n_layers is after the final norm: not a block output
    rows = []
    for (r_arm, r_order), (d_arm, d_order) in cfg["patch_pairs"]:
        key_r, key_d = (case, r_arm, r_order, "C", 0), (case, d_arm, d_order, "C", 0)
        if key_r not in prompts or key_d not in prompts:
            continue
        enc = {"recipient": lens.encode(no_reasoning(prompts[key_r])),
               "donor": lens.encode(no_reasoning(prompts[key_d]))}
        roles = [p for p in POSITION_ROLES if p in enc["recipient"].positions and p in enc["donor"].positions]
        states = {which: lens.readout(e.ids, [Variant()], hidden_at=[e.positions[p] for p in roles])["hidden"][0]
                  for which, e in enc.items()}
        sweeps = {}
        for direction, (src, dst) in (("sufficiency", ("donor", "recipient")), ("necessity", ("recipient", "donor"))):
            e = enc[dst]
            for k, role in enumerate(roles):
                # Row 0 is the unpatched prompt, computed in the same batch as the patched rows.
                variants = [Variant()] + [Variant(patch=[(layer, e.positions[role], states[src][layer, k])])
                                          for layer in layers]
                sweeps[direction, role] = (src, dst, _batched(lens, e.ids, variants, cfg["batch"]))
        base = {dst: ps[0] for (src, dst, ps) in sweeps.values()}
        for (direction, role), (src, dst, ps) in sweeps.items():
            rows += [dict(case_id=case, recipient=f"{r_arm}|{r_order}", donor=f"{d_arm}|{d_order}",
                          direction=direction, position=role, layer=layer, p=float(p), p_target=float(base[dst]),
                          p_source=float(base[src])) for layer, p in zip(layers, ps[1:])]
    return rows


def stage_patch(run: Path, source: Path, model: str, lens: Lens, cfg: dict) -> pd.DataFrame:
    """Both halves: the fit half picks each position's layer, the test half is tested at it (report)."""
    d = out_dir(run, model)
    read_lock(d, cfg)
    cfg = gate_cfg(d, cfg)
    prompts = _prompt_index(source, model)
    cases = halves(pd.read_parquet(d / "noreason_responses.parquet"), cfg["patch_halves"])
    return _per_case(run, source, model, lens, cfg, "patch", cases, lambda c: patch_case(lens, prompts, c, cfg))


# Stage: 3.2 attention-edge knockouts
KNOCKOUTS = {  # name -> (query group, key group, include new answer tokens, order it applies to)
    "readout": ("readout", "own", True, None),        # positive control: removes leakage under any hypothesis
    "encoding": ("person", "own", False, "own_first"),
    "update": ("own", "person", False, "person_first"),
}
TESTED_KNOCKOUTS = ("encoding", "update")


def knockout_case(lens: Lens, prompts: dict, case: str, cfg: dict) -> list[dict]:
    starts = list(range(0, lens.n_layers, int(cfg["knockout_every"])))
    rows = []
    for arm, order in cfg["knockout_cells"]:
        for role in ("C", "M", "D"):
            key = (case, arm, order, role, 0)
            if key not in prompts:
                continue
            e = lens.encode(with_filler(no_reasoning(prompts[key])))
            variants, specs = [Variant()], [("none", "none", -1)]
            if role == "C":
                for name, (qg, kg, new, only) in KNOCKOUTS.items():
                    if only and only != order:
                        continue
                    for target, keys in (("record", e.groups[kg]), ("filler", e.groups["filler"])):
                        variants += [Variant(knock=[(s, e.groups[qg], keys, new)]) for s in starts]
                        specs += [(name, target, s) for s in starts]
            ps = _batched(lens, e.ids, variants, cfg["batch"])
            rows += [dict(case_id=case, arm=arm, order=order, role=role, knockout=name, target=target, start=s,
                          p=float(p)) for (name, target, s), p in zip(specs, ps)]
    return rows


def stage_knockout(run: Path, source: Path, model: str, lens: Lens, cfg: dict) -> pd.DataFrame:
    d = out_dir(run, model)
    read_lock(d, cfg)
    cfg = gate_cfg(d, cfg)
    prompts = _prompt_index(source, model)
    cases = halves(pd.read_parquet(d / "noreason_responses.parquet"), ["test"])
    return _per_case(run, source, model, lens, cfg, "knockout", cases, lambda c: knockout_case(lens, prompts, c, cfg))


# Stage: 3.3 directions
def directions_case(lens: Lens, prompts: dict, case: str, variant: str, info: dict, arrays: dict,
                    cfg: dict) -> list[dict]:
    """Mean-ablation over L* at the locked probe position (``ablate_positions: readout`` ablates every
    question and answer token with the same mean), and steering at the best layer, of C, M and D."""
    torch = lens.torch

    def tensor(v):
        return torch.tensor(np.asarray(v), device=lens.device, dtype=torch.float32)

    window, best, unit = info["window"], info["best_layer"], float(info["steer_unit"])
    U = {"attr": (arrays[f"{variant}__u_attr"], arrays[f"{variant}__mu_attr"])}
    if f"{variant}__u_rec" in arrays:
        U["rec"] = (arrays[f"{variant}__u_rec"], arrays[f"{variant}__mu_rec"])
    rand, mu_rand = arrays[f"{variant}__rand"], arrays[f"{variant}__mu_rand"]
    rows = []
    for arm, order in cfg["direction_cells"].get(variant, []):
        for role in ("C", "M", "D"):
            key = (case, arm, order, role, 0)
            if key not in prompts:
                continue
            e = lens.encode(no_reasoning(prompts[key]))
            ps = e.groups["readout"] if cfg.get("ablate_positions") == "readout" else [e.positions[info["position"]]]
            specs = [("none", np.nan, Variant())]
            for kind, (u, mu) in U.items():
                specs.append((f"ablate_{kind}", np.nan, Variant(
                    ablate=[({l}, ps, tensor(u[l]), float(mu[l])) for l in window])))
            for j in range(len(rand)):
                specs.append((f"ablate_null_{j}", np.nan, Variant(
                    ablate=[({l}, ps, tensor(rand[j, l]), float(mu_rand[l, j])) for l in window])))
            for kind, u in ({k: u[best] for k, (u, _) in U.items()} | {"null_0": rand[0, best]}).items():
                for a in cfg["steer_alphas"]:
                    specs.append((f"steer_{kind}", float(a), Variant(
                        steer=[(best, e.positions[info["position"]], tensor(u * a * unit))])))
            ps_out = _batched(lens, e.ids, [v for _, _, v in specs], cfg["batch"])
            rows += [dict(variant=variant, case_id=case, arm=arm, order=order, role=role, intervention=name,
                          alpha=alpha, p=float(p)) for (name, alpha, _), p in zip(specs, ps_out)]
    return rows


def stage_directions(run: Path, source: Path, model: str, lens: Lens, cfg: dict) -> pd.DataFrame:
    d = out_dir(run, model)
    lock, arrays = read_lock(d, cfg)
    cfg = gate_cfg(d, cfg)
    prompts = _prompt_index(source, model)
    cases = halves(pd.read_parquet(d / "noreason_responses.parquet"), ["test"])

    def case_rows(case):
        return [r for variant, info in lock["variants"].items()
                for r in directions_case(lens, prompts, case, variant, info, arrays, cfg)]
    return _per_case(run, source, model, lens, cfg, "directions", cases, case_rows)


# Benchmark: time the real per-patient work on this GPU before committing to the full run
def stage_benchmark(run: Path, source: Path, model: str, lens: Lens, cfg: dict, n_readout: int = 128) -> dict:
    """Times one test-half patient through patch, knockout and directions (placeholder directions: random
    unit vectors over three middle layers, the same work as the locked ones) and ``n_readout`` readouts,
    extrapolates to the whole run, and measures batch invariance under the current float32 precision
    (``tf32``). Writes ``benchmark.json`` only; touches no result file."""
    import time
    torch = lens.torch
    d = out_dir(run, model)
    prompts = _prompt_index(source, model)
    sp = source_prompts(source, model)
    need = sp[sp.arm.isin(wanted_cells(sp, cfg))]
    test = sorted(set(need.case_id[need.split == "test"]))
    n_patch = len(set(need.case_id[need.split.isin(cfg["patch_halves"])]))

    def timed(fn):
        if lens.device.type == "cuda":
            torch.cuda.synchronize()
        t = time.time()
        out = fn()
        if lens.device.type == "cuda":
            torch.cuda.synchronize()
        return time.time() - t, out

    sample = need.drop_duplicates("prompt").sample(n=min(n_readout, need.prompt.nunique()), random_state=0)
    t_read, _ = timed(lambda: lens.readout_batch([lens.encode(no_reasoning(p)) for p in sample.prompt],
                                                 batch=int(cfg["batch"])))
    case = test[0]
    t_patch, rows_p = timed(lambda: patch_case(lens, prompts, case, cfg))
    t_ko, rows_k = timed(lambda: knockout_case(lens, prompts, case, cfg))
    hidden, layers = lens.lm.config.hidden_size, lens.n_layers + 1
    rng = np.random.default_rng(0)

    def unit(*shape):
        v = rng.normal(size=shape + (hidden,))
        return v / np.linalg.norm(v, axis=-1, keepdims=True)
    mid = lens.n_layers // 2
    rows_d, t_dir = [], 0.0
    for variant in cfg["direction_cells"]:
        arrays = {f"{variant}__u_attr": unit(layers), f"{variant}__mu_attr": np.zeros(layers),
                  f"{variant}__u_rec": unit(layers), f"{variant}__mu_rec": np.zeros(layers),
                  f"{variant}__rand": unit(int(cfg["random_directions"]), layers),
                  f"{variant}__mu_rand": np.zeros((layers, int(cfg["random_directions"])))}
        info = dict(window=[mid - 1, mid, mid + 1], best_layer=mid, steer_unit=1.0, position="name")
        t, rr = timed(lambda: directions_case(lens, prompts, case, variant, info, arrays, cfg))
        t_dir += t
        rows_d += rr
    e = lens.encode(no_reasoning(sample.prompt.iloc[0]))
    alone = float(lens.readout(e.ids, [Variant()])["p"][0])
    batch = lens.readout(e.ids, [Variant()] * int(cfg["batch"]))["p"]
    n_read = need.prompt.nunique()
    est = {"readout": t_read / len(sample) * n_read, "patch": t_patch * n_patch, "knockout": t_ko * len(test),
           "directions": t_dir * len(test)}
    result = dict(model=model, device=str(lens.device), tf32=bool(cfg.get("tf32")), dtype=cfg.get("dtype"),
                  batch=int(cfg["batch"]), distinct_readouts=n_read, patch_patients=n_patch, test_patients=len(test),
                  seconds_per_readout=t_read / len(sample),
                  seconds_per_patient=dict(patch=t_patch, knockout=t_ko, directions=t_dir),
                  rows_per_patient=dict(patch=len(rows_p), knockout=len(rows_k), directions=len(rows_d)),
                  estimated_hours={k: round(v / 3600, 2) for k, v in est.items()} |
                                  {"total": round(sum(est.values()) / 3600, 2)},
                  batch_invariance_max_abs_dp=float(np.abs(batch - alone).max()))
    (d / "benchmark.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return result


# Stage: report
def _lambda(x, c, m, dd):
    from second_order.leakage import slope
    den = slope(dd - m, x)
    return slope(c - m, x) / den if den != 0 else np.nan


def _beta_d(x, m, dd):
    from second_order.leakage import slope
    return slope(dd - m, x)


def _boot(fns: dict, n: int, resamples: int, seed: int) -> dict:
    """Point estimate and 95% percentile interval of each statistic, all from the same patient draws."""
    rng = np.random.default_rng(seed)
    draws = [rng.integers(0, n, n) for _ in range(resamples)]
    out = {}
    for name, fn in fns.items():
        d = np.array([fn(i) for i in draws], dtype=float)
        d = d[np.isfinite(d)]
        lo, hi = np.percentile(d, [2.5, 97.5]) if len(d) else (np.nan, np.nan)
        out[name] = (float(fn(np.arange(n))), float(lo), float(hi))
    return out


def _guarded(est: dict, *denominators: str) -> str:
    """'' if every denominator's interval excludes 0, else why the ratios are undetermined."""
    weak = [k for k in denominators if est[k][1] <= 0 <= est[k][2] or not np.isfinite(est[k][1])]
    return f"{', '.join(weak)} interval includes 0" if weak else ""


def lock_variant(arm: str) -> str:
    """The probe/lock variant of an arm (the strong variant uses the partial probes)."""
    return "false" if B.ARMS[arm][0] == "false" else "partial"


def _interval(est):
    return [round(float(v), 4) for v in est]


def _verdict(supported: bool, why: str = "") -> dict:
    return {"status": "undetermined", "supported": False, "reason": why} if why else \
           {"status": "supported" if supported else "not supported", "supported": bool(supported)}


def stage_report(run: Path, model: str, resamples: int, cfg: dict | None = None) -> dict:
    """Phase 3 tables and decision rules. Each rule tests one pre-specified quantity on the test half with a
    patient-bootstrap interval; a lambda whose beta_D interval includes 0 makes its rule undetermined. Layer and
    start sweeps are reported, not tested."""
    cfg = cfg or load_cfg(run)
    min_cases = int(cfg["report_min_cases"])
    d = out_dir(run, model)
    lock, _ = read_lock(d, cfg)
    seed = int(C.load()["seed"])
    frame = pd.read_parquet(d / "noreason_responses.parquet")
    test = set(frame.case_id[frame.split == "test"])
    lg = frame[frame.replicate == 0].assign(l=lambda t: logit_of(t.p))
    own = lg[lg.role.isin(["A", "B"])].pivot_table(index="case_id", columns=["arm", "role"], values="l", aggfunc="first")
    refs = lg[lg.role.isin(["M", "D"])].pivot_table(index="case_id", columns=["arm", "block_order", "role"],
                                                    values="l", aggfunc="first")
    rules, lines = {}, [f"# Phase 3, {model}", "", f"Lock: `{json.dumps(lock['variants'], default=float)}`", ""]
    if (d / "gate.csv").exists():
        g = pd.read_csv(d / "gate.csv")
        excluded = [f"{a}|{o}" for a, o in zip(g.arm[~g.enters_phase3], g.order[~g.enters_phase3])]
        lines += [f"Gate: {int(g.enters_phase3.sum())} of {len(g)} cells enter Phase 3; excluded: "
                  f"{', '.join(excluded) or 'none'}.", ""]

    def gap(arm):
        return (own[(arm, "A")] - own[(arm, "B")]) if (arm, "A") in own.columns else None

    # 3.1 patching: each position's layer chosen on the fit half, tested on the test half
    patch_path = d / "patch.csv"
    if patch_path.exists() and patch_path.stat().st_size > 1 and not (patch := pd.read_csv(patch_path)).empty:
        lt, ls, lp = logit_of(patch.p_target), logit_of(patch.p_source), logit_of(patch.p)
        patch["gap_closed"] = np.where(np.abs(ls - lt) >= float(cfg["patch_min_gap"]), (lp - lt) / (ls - lt), np.nan)
        patch["l"] = lp
        patch["half"] = np.where(patch.case_id.isin(test), "test", "fit")
        patch.groupby(["half", "recipient", "donor", "direction", "position", "layer"]).gap_closed.median() \
             .reset_index().to_csv(d / "patch_summary.csv", index=False)
        rows = []
        suff = patch[patch.direction == "sufficiency"]
        for (recipient, donor, position), part in suff.groupby(["recipient", "donor", "position"]):
            arm, order = recipient.split("|")
            fit_curve = part[part.half == "fit"].groupby("layer").gap_closed.median()
            if fit_curve.dropna().empty:
                continue
            layer = int(fit_curve.idxmax())
            at = part[(part.half == "test") & (part.layer == layer)].set_index("case_id")
            closed = at.gap_closed.dropna().to_numpy()
            if len(closed) >= min_cases:
                est = _boot({"gap_closed": lambda i: float(np.median(closed[i]))}, len(closed), resamples, seed)
                rows.append(dict(recipient=recipient, donor=donor, position=position, layer=layer,
                                 statistic="gap_closed", estimate=est["gap_closed"][0], ci_lower=est["gap_closed"][1],
                                 ci_upper=est["gap_closed"][2], n=len(closed)))
                if position != "answer":
                    rules[f"3.1_decided_at[{recipient}<-{donor}|{position}]"] = _verdict(
                        est["gap_closed"][1] >= 0.7) | dict(
                        interval=_interval(est["gap_closed"]), layer=layer,
                        rule="patching this one position, at the layer the fit half picked, closes >= 70% of the gap on "
                             "the test half (median over patients, CI lower bound)")
            x = gap(arm)
            cases = [c for c in at.index if x is not None and c in x.index and c in refs.index]
            if len(cases) >= min_cases and (arm, order, "M") in refs.columns:
                xx = x.loc[cases].to_numpy()
                mm, dd = refs.loc[cases, (arm, order, "M")].to_numpy(), refs.loc[cases, (arm, order, "D")].to_numpy()
                cp, c0 = at.loc[cases, "l"].to_numpy(), logit_of(at.loc[cases, "p_target"])
                est = _boot({"lambda_unpatched": lambda i: _lambda(xx[i], c0[i], mm[i], dd[i]),
                             "lambda_patched": lambda i: _lambda(xx[i], cp[i], mm[i], dd[i]),
                             "beta_D": lambda i: _beta_d(xx[i], mm[i], dd[i])}, len(cases), resamples, seed)
                why = _guarded(est, "beta_D")
                rows += [dict(recipient=recipient, donor=donor, position=position, layer=layer, statistic=k,
                              estimate=np.nan if why and k != "beta_D" else v[0], ci_lower=v[1], ci_upper=v[2],
                              n=len(cases), blank_reason=why if k != "beta_D" else "") for k, v in est.items()]
        table = pd.DataFrame(rows)
        table.to_csv(d / "patch_lambda.csv", index=False)
        if not table.empty:
            lines += ["## 3.1 patching (layer from the fit half, test half)", "", "```",
                      table.round(3).to_string(index=False), "```", ""]
    # 3.2 knockouts (test half)
    ko_path = d / "knockout.csv"
    if ko_path.exists() and ko_path.stat().st_size > 1 and not (ko := pd.read_csv(ko_path)).empty:
        ko["l"] = logit_of(ko.p)
        out = []
        for (arm, order), part in ko.groupby(["arm", "order"]):
            ref = part[part.knockout == "none"].pivot_table(index="case_id", columns="role", values="l")
            x = gap(arm)
            cases = [c for c in ref.index if x is not None and c in x.index]
            if len(cases) < min_cases:
                continue
            xx = x.loc[cases].to_numpy()
            mm, dd, c0 = (ref.loc[cases, r].to_numpy() for r in ("M", "D", "C"))
            for (name, start), kp in part[part.knockout != "none"].groupby(["knockout", "start"]):
                wide = kp.pivot_table(index="case_id", columns="target", values="l").reindex(cases)
                rec, fil = wide["record"].to_numpy(), wide["filler"].to_numpy()
                # lambda drop from knocking out the record, beyond the same knockout aimed at the filler
                est = _boot({"drop": lambda i: _lambda(xx[i], fil[i], mm[i], dd[i]) - _lambda(xx[i], rec[i], mm[i], dd[i]),
                             "beta_D": lambda i: _beta_d(xx[i], mm[i], dd[i])}, len(cases), resamples, seed)
                out.append(dict(arm=arm, order=order, knockout=name, start=start, lambda_base=_lambda(xx, c0, mm, dd),
                                lambda_drop_vs_filler=est["drop"][0], ci_lower=est["drop"][1], ci_upper=est["drop"][2],
                                blank_reason=_guarded(est, "beta_D"), n=len(cases),
                                role="positive control" if name not in TESTED_KNOCKOUTS else "test"))
        kt = pd.DataFrame(out)
        kt.to_csv(d / "knockout_summary.csv", index=False)
        for r in (kt[(kt.start == 0) & kt.knockout.isin(TESTED_KNOCKOUTS)].itertuples() if not kt.empty else []):
            rules[f"3.2_leak_enters_at[{r.knockout}|{r.arm}|{r.order}]"] = _verdict(
                r.lambda_drop_vs_filler >= 0.3 and r.ci_lower > 0, r.blank_reason) | dict(
                interval=_interval((r.lambda_drop_vs_filler, r.ci_lower, r.ci_upper)),
                rule="knocking out this attention edge at every layer lowers lambda by >= 0.3 more than the same "
                     "knockout aimed at the filler (CI above 0); the readout knockout is a positive control")
    # 3.3 directions (test half): lambda with C, M and D all intervened
    dir_path = d / "directions.csv"
    if dir_path.exists() and dir_path.stat().st_size > 1 and not (dr := pd.read_csv(dir_path)).empty:
        dr["l"] = logit_of(dr.p)
        out = []
        for (variant, arm, order), part in dr[dr.alpha.isna()].groupby(["variant", "arm", "order"]):
            wide = part.pivot_table(index="case_id", columns=["role", "intervention"], values="l")
            x = gap(arm)
            cases = [c for c in wide.index if x is not None and c in x.index]
            if len(cases) < min_cases:
                continue
            xx = x.loc[cases].to_numpy()

            def col(role, name):
                return wide.loc[cases, (role, name)].to_numpy()
            m0, d0, c0 = col("M", "none"), col("D", "none"), col("C", "none")
            for name in sorted({i for r, i in wide.columns if r == "C" and i != "none"}):
                ci, mi, di = col("C", name), col("M", name), col("D", name)
                est = _boot({"delta": lambda i: _lambda(xx[i], ci[i], mi[i], di[i]) - _lambda(xx[i], c0[i], m0[i], d0[i]),
                             "delta_references_unintervened":
                                 lambda i: _lambda(xx[i], ci[i], m0[i], d0[i]) - _lambda(xx[i], c0[i], m0[i], d0[i]),
                             "beta_D": lambda i: _beta_d(xx[i], m0[i], d0[i]),
                             "beta_D_intervened": lambda i: _beta_d(xx[i], mi[i], di[i])}, len(cases), resamples, seed)
                out.append(dict(variant=variant, arm=arm, order=order, intervention=name, delta_lambda=est["delta"][0],
                                ci_lower=est["delta"][1], ci_upper=est["delta"][2],
                                delta_lambda_references_unintervened=est["delta_references_unintervened"][0],
                                mean_abs_change_on_D=float(np.nanmean(np.abs(di - d0))),
                                blank_reason=_guarded(est, "beta_D", "beta_D_intervened"), n=len(cases)))
        dt = pd.DataFrame(out)
        dt.to_csv(d / "directions_summary.csv", index=False)
        steer = dr[dr.alpha.notna() & (dr.role == "C")]
        if not steer.empty:
            (steer.groupby(["variant", "arm", "order", "intervention", "case_id"])
             .apply(lambda g: np.polyfit(g.alpha, g.l, 1)[0]).groupby(level=[0, 1, 2, 3]).mean().rename("slope")
             .reset_index().to_csv(d / "steering_summary.csv", index=False))
        for (variant, arm, order), cell in (dt.groupby(["variant", "arm", "order"]) if not dt.empty else []):
            s = cell.set_index("intervention")
            if not {"ablate_attr", "ablate_rec"} <= set(s.index):
                continue
            attr, rec = s.loc["ablate_attr"], s.loc["ablate_rec"]
            null = s[s.index.str.startswith("ablate_null")].delta_lambda.abs()
            null95 = float(np.percentile(null, 95)) if len(null) else np.nan
            moves = abs(attr.delta_lambda) >= 0.3 and not (attr.ci_lower <= 0 <= attr.ci_upper) \
                and abs(attr.delta_lambda) > null95
            rules[f"3.3_present_but_inert[{variant}|{arm}|{order}]"] = _verdict(
                moves and abs(rec.delta_lambda) < 0.1, attr.blank_reason or rec.blank_reason) | dict(
                ablate_attr=_interval((attr.delta_lambda, attr.ci_lower, attr.ci_upper)),
                ablate_rec=_interval((rec.delta_lambda, rec.ci_lower, rec.ci_upper)), null_abs_95th=null95,
                rule="ablating w_attr (in C, M and D) changes lambda by >= 0.3 (CI excluding 0, beyond the 95th "
                     "percentile of shuffled-target probes) while ablating w_rec changes it by < 0.1")
    lines += ["## Decision rules", "",
              "One pre-specified test per cell and position; no correction across cells.", ""] + [
        f"- **{k}**: {v['status']}. {v['rule']}." for k, v in rules.items()]
    (d / "RESULTS.md").write_text("\n".join(lines) + "\n")
    (d / "decisions.json").write_text(json.dumps(rules, indent=2, default=float))
    print("\n".join(lines))
    return rules


STAGES = ("readout", "gate", "probes", "patch", "knockout", "directions", "report")
GPU_STAGES = ("readout", "patch", "knockout", "directions", "benchmark")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=STAGES + ("all", "benchmark"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="the session-1 run (profile full), analysed")
    parser.add_argument("--model", required=True)
    parser.add_argument("--resamples", type=int, default=2000)
    args = parser.parse_args()
    run, source = args.run.resolve(), args.source.resolve()
    run.mkdir(parents=True, exist_ok=True)
    B.read_plan(source)  # configures the source run's config and verifies its plan
    cfg = load_cfg(run)
    if not (run / "causal_config.json").exists():
        (run / "causal_config.json").write_text(json.dumps(cfg, indent=2))
    stages = STAGES if args.stage == "all" else (args.stage,)
    lens = None
    with (run / ".causal.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for stage in stages:
            if stage in GPU_STAGES and lens is None:
                import torch
                torch.backends.cuda.matmul.allow_tf32 = bool(cfg.get("tf32", True))
                torch.backends.cudnn.allow_tf32 = bool(cfg.get("tf32", True))
                lens = Lens(args.model, dtype=cfg.get("dtype", "float32"))
            if stage == "benchmark":
                stage_benchmark(run, source, args.model, lens, cfg)
            if stage == "readout":
                stage_readout(run, source, args.model, lens, cfg)
            elif stage == "gate":
                stage_gate(run, source, args.model, cfg, args.resamples)
            elif stage == "probes":
                d = out_dir(run, args.model)
                if not (d / "lock.json").exists():
                    stage_probes(run, source, args.model, cfg)
            elif stage == "patch":
                stage_patch(run, source, args.model, lens, cfg)
            elif stage == "knockout":
                stage_knockout(run, source, args.model, lens, cfg)
            elif stage == "directions":
                stage_directions(run, source, args.model, lens, cfg)
            elif stage == "report":
                stage_report(run, args.model, args.resamples, cfg)


if __name__ == "__main__":
    main()
