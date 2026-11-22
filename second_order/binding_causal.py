"""Binding experiment, session 2 (HF transformers with hooks): the no-reasoning arm, probes and causal tests.

Run after session 1 (``binding`` profile ``full``) and its analysis.  Stages, in order:

    readout     no-reasoning answers for every Phase 3 cell (all patients; A/B both replicates): the prompt's
                reasoning instruction becomes "Do not explain...", the answer prefix '{"probability": 0.' is
                forced, and the belief is the exact mean over the two-digit answer distribution of the clipped
                logit, or of the probability (``readout_statistic``); the second digit is read from the KV cache,
                so interventions act on both digits. Also saves the residual stream of fit-half patients at the
                probe positions, every layer.
    gate        fit half: lambda per cell from the no-reasoning beliefs (``binding_analysis`` estimators) against
                session 1's reasoning lambda, both 95% Fieller sets: a cell enters Phase 3 only if the intervals
                overlap and the order effect has the same sign. Also marks the correctly bound cells (lambda <=
                ``bound_max_lambda`` with and without reasoning), the only patching donors and w_attr training
                cells, and the cells whose knockouts 3.2 tests (no-reasoning lambda >= ``knockout_min_lambda``).
    probes      fit half only: ridge probes for PERSON's correct belief (w_attr, trained on correctly bound
                cells) and for it inside a misbound cell (w_rec, orthogonalised against w_attr); transfer; a
                null of probes on patient-permuted targets; then the lock: layer window L*, position and
                steering scale by a fixed rule, written once to ``lock.json`` before anything touches the test half.
    patch       3.1 interchange patching (the fit half picks the layer, the test half tests it): one
                position-role at one layer, donor -> recipient (sufficiency) and back (necessity), every layer
                in one batch with its own unpatched row.
    knockout    3.2 attention-edge knockouts on prompts with a neutral filler block between the records
                (readout, encoding, update), each against the same knockout aimed at as many filler tokens,
                swept over start layers.
    directions  3.3 mean-ablation of w_attr, w_rec and the null directions over L* at the locked (name)
                position, and steering there, on misbound C, on M (references) and on D (specificity).
    report      gap closed, lambda under each intervention, the 3.x decision rules -> RESULTS.md.

``benchmark`` (not part of ``all``) times one patient through the GPU stages and measures batch invariance.

    python -m second_order.binding_causal <stage> --run runs/binding-causal --source runs/binding-full --model qwen3_8b
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import binding as B
from second_order import config as C
from second_order.beliefs import clip_logit
from second_order.leakage import slope

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
    "The second-floor elevator is closed for maintenance on Tuesday mornings.",
    "Seasonal vaccination clinics run on the first Saturday of each month.",
    "Free wireless internet is available; ask reception for the password.",
    "Bicycle racks are located beside the east entrance.",
    "Hand sanitizer stations are placed at every corridor junction.",
    "A chaplain is available on request at any time of day.",
    "The gift shop is open from 9 am to 4 pm, Monday to Saturday.",
    "Fire exits are marked in green throughout the building.",
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
    "ablate_positions": "probe",   # only "probe": the locked position (readout positions are refused)
    "patch_halves": ["fit", "test"],  # 3.1 picks each position's layer on the fit half, tests on the test half
    "patch_min_gap": 0.1,          # logits between donor and recipient below which gap closed is undefined
    "report_min_cases": 8,         # fewer patients than this: no lambda for that cell
    "apply_gate": True,            # Phase 3 runs only on cells whose no-reasoning lambda matches session 1's
    "bound_max_lambda": 0.15,      # a donor or w_attr training cell must be correctly bound: fit-half lambda at
                                   # most this, with reasoning (session 1) and without
    "knockout_min_lambda": 0.3,    # 3.2 tests a cell only if its fit-half no-reasoning lambda leaves room for the
                                   # 0.3 drop
    "readout_statistic": "mean_logit",  # E[clip_logit(p)] over the two digits, as session 1's mean clipped logit;
                                   # "expected_p": logit of E[p]
    "probe_targets": "noreason",   # l_A, l_B for the probes: the no-reasoning readouts the activations come
                                   # from, or "reasoning" (session 1)
    "resamples": 2000,             # bootstrap draws of the gate and the report (locked)
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
    """The prompt with the neutral filler block right after its first record block (prepended if it has only
    one), so the filler control's keys sit as close to the queries as the records do."""
    blocks = B.record_blocks(prompt)
    if len(blocks) < 2:
        return FILLER + "\n\n" + prompt
    cut = min(b["end"] for b in blocks)  # a record block is a paragraph: "\n\n" follows it
    return prompt[:cut] + "\n\n" + FILLER + prompt[cut:]


@dataclass
class Encoded:
    ids: list[int]
    positions: dict[str, int]       # named single positions
    groups: dict[str, list[int]]    # token groups (record blocks, filler, question + answer)


def encode(tok, prompt: str, template_kwargs: dict) -> Encoded:
    """Chat template + forced prefix, with token positions of every role the causal tests use."""
    chat = tok.apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True,
                                   tokenize=False, **template_kwargs)
    enc = tok(chat + PREFIX, add_special_tokens=False, return_offsets_mapping=True)
    starts, ends = np.array(enc["offset_mapping"]).reshape(-1, 2).T
    real = ends > starts  # tokens that cover text
    base = chat.find(prompt)
    if base < 0:
        raise ValueError("prompt not found in the chat template")

    def last_token(char_end):  # token holding character char_end - 1
        return int(np.flatnonzero(real & (starts < char_end))[-1])

    def span(a, b):  # tokens overlapping characters [a, b)
        return np.flatnonzero(real & (starts < b) & (ends > a)).tolist()

    pos, groups = {}, {}
    for blk in B.record_blocks(prompt):
        pos[f"{blk['owner']}_end"] = last_token(base + blk["end"])
        groups[blk["owner"]] = span(base + blk["start"], base + blk["end"])
    if FILLER in prompt:
        at = prompt.index(FILLER)
        groups["filler"] = span(base + at, base + at + len(FILLER))
    question = prompt.rfind("\n\nWhat probability ")
    if question < 0:
        raise ValueError("prompt lacks the probability question")
    q_start = base + question + 2
    q_end = base + prompt.index("?", question) + 1
    name = B.person_name(prompt)
    if name:
        at = prompt.rfind(f" {name} ", question, q_end - base)
        if at < 0:
            raise ValueError(f"{name} not found in the question")
        pos["name"] = last_token(base + at + 1 + len(name))
    pos["question_end"] = last_token(q_end)
    pos["prompt_end"] = last_token(base + len(prompt))
    pos["gen_start"] = last_token(len(chat))
    pos["answer"] = len(enc["input_ids"]) - 1
    groups["readout"] = list(range(span(q_start, q_end)[0], len(enc["input_ids"])))
    return Encoded(list(enc["input_ids"]), pos, groups)


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
                 dtype: str = "float32", statistic: str = "mean_logit"):
        # float32 by default: in bfloat16 the batch shape alone moves a readout by ~0.03 in probability,
        # as large as the effects the causal tests look for (batched rows are exact in float32).
        import torch
        self.torch = torch
        if statistic not in ("mean_logit", "expected_p"):
            raise ValueError(f"unknown readout statistic {statistic!r}")
        self.statistic = statistic
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
        # State of the current forward pass (set by _run)
        self._rows: list[Variant] = []
        self._offset = 0              # absolute position of the pass's first query token
        self._new_tokens = False      # True in the second-digit pass (queries are the appended answer tokens)
        self._tree = 0                # >0: that many parallel candidate tokens, each attending to the prompt only
        self._capture = None          # per row: absolute positions whose residual stream to keep (all layers)
        self._captured = {}
        self._mask = None             # the pass's attention mask, built at layer 0 and extended layer by layer
        self._prefix = None           # (prefix ids, legacy cache) of the last shared prefix
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

    @contextmanager
    def _run(self, rows=(), *, offset=0, new_tokens=False, tree=0, capture=None):
        """One forward pass (in inference mode) under ``rows``' interventions; the state is cleared afterwards."""
        self._rows, self._offset, self._new_tokens, self._tree = list(rows), offset, new_tokens, tree
        self._capture = capture
        if capture is not None:
            self._captured = {}
        try:
            with self.torch.inference_mode():
                yield
        finally:
            self._rows, self._offset, self._new_tokens, self._tree = [], 0, False, 0
            self._capture, self._mask = None, None

    # hooks
    def _keep(self, index, hs):
        if self._capture is None:
            return
        q = hs.shape[1]
        for r, ps in enumerate(self._capture):
            if ps:
                local = [p - self._offset for p in ps]
                if all(0 <= p < q for p in local):
                    self._captured.setdefault(r, {})[index] = hs[r, local]  # advanced indexing: a copy

    def _capture_hook(self, index):
        def hook(module, args, output):
            self._keep(index, output[0] if isinstance(output, tuple) else output)
        return hook

    def _edit_hook(self, hidden_index):
        def hook(module, args, output):
            hs = output[0] if isinstance(output, tuple) else output
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
        return hook

    def _mask_hook(self, layer_index):
        torch = self.torch

        def hook(module, args, kwargs):
            if not self._rows or (not self._tree and not any(v.knock for v in self._rows)):
                return None
            if layer_index == 0:
                mask = kwargs.get("attention_mask")
                if mask is None or mask.dim() != 4:
                    raise RuntimeError("knockouts need the eager 4D attention mask")
                n = len(self._rows)
                self._mask = mask.clone() if mask.shape[0] == n else mask.expand(n, *mask.shape[1:]).clone()
                if self._tree:
                    # Candidate answer tokens are parallel continuations: none sees another.
                    t = self._tree
                    block = torch.full((t, t), torch.finfo(mask.dtype).min, dtype=mask.dtype, device=mask.device)
                    block.fill_diagonal_(0)
                    self._mask[:, 0, :, -t:] = block
            mask = self._mask
            q, k = mask.shape[2], mask.shape[3]
            low = torch.finfo(mask.dtype).min
            # A knockout holds from its start layer on: it enters the mask there, which later layers reuse.
            for r, v in enumerate(self._rows):
                for start, qs, ks, new in v.knock:
                    if max(start, 0) != layer_index:
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
        """KV cache of ids[:start] (computed once per prompt prefix, no interventions), shared by n rows."""
        from transformers import DynamicCache
        prefix = tuple(ids[:start])
        if self._prefix is None or self._prefix[0] != prefix:
            with self._run():
                out = self.lm(self.torch.tensor([prefix], device=self.device), use_cache=True, logits_to_keep=1)
            self._prefix = (prefix, out.past_key_values.to_legacy_cache())
        # Broadcast views, not copies: the forward pass concatenates each layer's new keys onto them.
        return DynamicCache.from_legacy_cache(tuple((k.expand(n, -1, -1, -1), v.expand(n, -1, -1, -1))
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
        peak = p1.amax(0).tolist()
        cand = [a for a in range(10) if peak[a] >= 1e-6]
        n, t = len(rows), len(cand)
        new = torch.tensor([[self.digits[a] for a in cand]] * n, device=self.device)
        position = torch.tensor(lengths, device=self.device)[:, None].expand(-1, t)
        if mask is not None:
            mask = torch.cat([mask, torch.ones(n, t, dtype=mask.dtype, device=self.device)], 1)
        with self._run(rows, offset=width, new_tokens=True, tree=t):
            out = self.lm(new, attention_mask=mask, past_key_values=cache, position_ids=position, use_cache=True,
                          logits_to_keep=t)
        d2 = out.logits[:, -t:].float().softmax(-1)[:, :, self.digits]   # rows, candidates, second digit
        end = (1 - d2.sum(-1)).clamp_min(0)                               # anything else ends the number
        tenths = torch.arange(10, device=self.device, dtype=torch.float32)
        a = torch.tensor(cand, device=self.device, dtype=torch.float32)
        # f(p) is averaged over the two-digit answer distribution: the probability itself ("expected_p"), or its
        # clipped logit ("mean_logit", as session 1's belief is the mean clipped logit of its samples). A mean
        # logit is returned as the probability whose clipped logit it is, so logit_of(p) recovers it exactly.
        if self.statistic == "expected_p":
            def f(p):
                return p
        else:
            def f(p):
                p = p.clamp(0.01, 0.99)
                return torch.log(p) - torch.log1p(-p)
        cond = f(tenths / 10 + 0.045).repeat(n, 1)
        cond[:, cand] = ((d2 * f(a[None, :, None] / 10 + tenths[None, None, :] / 100)).sum(-1)
                         + end * f(a[None, :] / 10)) / (d2.sum(-1) + end)
        value = (p1 * cond).sum(-1)
        if self.statistic != "expected_p":
            value = torch.sigmoid(value)
        return value.cpu().numpy(), mass.cpu().numpy()

    def _captures(self, n):
        """The captured rows (layers + 1, positions, hidden), None for rows that captured nothing."""
        captured, self._captured = self._captured, {}
        return [self.torch.stack([captured[r][i] for i in range(self.n_layers + 1)]) if r in captured else None
                for r in range(n)]

    def _halving(self, items: list, size: int, run) -> list:
        """``run`` on consecutive chunks of ``items``. A chunk that runs out of memory is retried at half the
        size, which is kept (``batch_cap``) for every later call."""
        torch = self.torch
        size = min(size, self.batch_cap or size)
        out, k = [], 0
        while k < len(items):
            try:
                out.append(run(items[k:k + size]))
            except torch.OutOfMemoryError:
                self._captured = {}
                if size == 1:
                    raise
                torch.cuda.empty_cache()
                size = self.batch_cap = max(1, size // 2)
                print(f"[lens] out of memory: batch -> {size}", flush=True)
                continue
            k += size
        return out

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
        with self._run(variants, offset=start, capture=None if hidden_at is None else [hidden_at] * n):
            out = self.lm(torch.tensor([ids[start:]] * n, device=self.device), attention_mask=mask,
                          past_key_values=cache, use_cache=True, logits_to_keep=1)
        hidden = None if hidden_at is None else torch.stack(self._captures(n))
        p, mass = self._answer(out.logits[:, -1].float(), out.past_key_values, variants, len(ids), [len(ids)] * n,
                               mask)
        return dict(p=p, digit_mass=mass, hidden=hidden)

    def sweep(self, ids, variants, batch: int) -> np.ndarray:
        """``readout`` of many variants of one prompt, ``batch`` rows per forward pass."""
        return np.concatenate(self._halving(variants, batch, lambda part: self.readout(ids, part)["p"]))

    def readout_batch(self, encodings: list, hidden_at: list | None = None, batch: int = 64):
        """No-intervention readouts of many prompts: left-padded batches (sorted by length) with explicit
        position ids, so every token keeps its position; residual stream (every layer) at each prompt's own
        positions. Returns (probabilities, digit masses, residual streams or None), in input order."""
        hidden_at = hidden_at or [None] * len(encodings)
        order = sorted(range(len(encodings)), key=lambda i: len(encodings[i].ids))
        chunks = self._halving(order, batch, lambda part: self._readout_padded([encodings[i] for i in part],
                                                                               [hidden_at[i] for i in part]))
        out = dict(zip(order, (r for chunk in chunks for r in chunk)))
        return tuple([out[i][j] for i in range(len(encodings))] for j in range(3))

    def _readout_padded(self, encs, hidden_at):
        torch = self.torch
        pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        lengths = [len(e.ids) for e in encs]
        width, n = max(lengths), len(encs)
        ids = torch.tensor([[pad] * (width - m) + e.ids for e, m in zip(encs, lengths)], device=self.device)
        mask = torch.tensor([[0] * (width - m) + [1] * m for m in lengths], device=self.device)
        capture = [[width - m + q for q in a] if a else None for a, m in zip(hidden_at, lengths)]
        with self._run(capture=capture if any(capture) else None):
            out = self.lm(ids, attention_mask=mask, position_ids=(mask.cumsum(-1) - 1).clamp_min(0),
                          use_cache=True, logits_to_keep=1)
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


def _read_rows(path: Path, **kwargs) -> pd.DataFrame:
    """A per-case results file, empty if the stage has written nothing yet."""
    return pd.read_csv(path, **kwargs) if path.exists() and path.stat().st_size > 1 else pd.DataFrame()


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

    def key(r):
        return r["case_id"], r["arm"], r["block_order"], r["role"], r["replicate"]
    seen = {key(r) for r in rows}
    acts_dir = d / "activations"
    acts_dir.mkdir(exist_ok=True)
    todo = [r for r in need.to_dict("records") if key(r) not in seen]
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
    if not done_path.exists():  # nothing to read
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
    # Both on the fit half: a cell may serve as a donor or train w_attr only if it is correctly bound (lambda at
    # most bound_max_lambda with and without reasoning), and 3.2 tests a knockout only where the no-reasoning
    # lambda leaves room for its 0.3 drop.
    bound = float(cfg["bound_max_lambda"])
    g["correctly_bound"] = (g.lambda_reasoning <= bound) & (g.lambda_no_reasoning <= bound)
    g["knockout_testable"] = g.lambda_no_reasoning >= float(cfg["knockout_min_lambda"])
    g.to_csv(d / "gate.csv", index=False)
    print(g.to_string(index=False))
    return g


def gate_cells(d: Path, cfg: dict, column: str) -> set[tuple[str, str]] | None:
    """(arm, order) cells whose ``column`` of gate.csv is true (``enters_phase3``: passed the gate;
    ``correctly_bound``), or None when the gate is switched off."""
    if not cfg.get("apply_gate", True):
        return None
    path = d / "gate.csv"
    if not path.exists():
        raise ValueError("run the gate stage before the probes and the causal tests")
    g = pd.read_csv(path)
    if column not in g:
        raise ValueError(f"gate.csv has no {column} column: rerun the gate stage")
    return set(zip(g.arm[g[column]], g.order[g[column]]))


def gate_cfg(d: Path, cfg: dict) -> dict:
    """The config with Phase 3's cells restricted to those that passed the gate (patching: the recipient), and
    patching donors to correctly bound cells."""
    cells = gate_cells(d, cfg, "enters_phase3")
    if cells is None:
        return cfg
    bound = gate_cells(d, cfg, "correctly_bound")
    return cfg | {"patch_pairs": [p for p in cfg["patch_pairs"] if tuple(p[0]) in cells and tuple(p[1]) in bound],
                  "knockout_cells": [c for c in cfg["knockout_cells"] if tuple(c) in cells],
                  "direction_cells": {v: [c for c in cs if tuple(c) in cells] for v, cs in cfg["direction_cells"].items()}}


# Stage: probes and lock (fit half only)
def _targets(source: Path, model: str, frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per case and arm: the truth (l_A) and PERSON's correct belief (l_B), means of replicates. From the
    no-reasoning readouts in ``frame`` (the condition the activations come from), else session 1."""
    if frame is not None:
        beliefs = frame.assign(value=logit_of(frame.p))
    else:
        beliefs = pd.read_parquet(source / "outputs" / "analysis" / model / "beliefs_direct.parquet")
    ab = beliefs[beliefs.role.isin(["A", "B"])].groupby(["case_id", "arm", "role"]).value.mean().unstack()
    return ab.rename(columns={"A": "truth", "B": "person"}).reset_index()


def _load_acts(d: Path, cases, arm, order):
    """The saved activations of ``cases`` in one cell, as float32 (cases, layers + 1, positions, hidden), and
    the cases that have them."""
    X, keep = [], []
    for c in cases:
        p = d / "activations" / f"{c}__{arm}__{order}.npy"
        if p.exists():
            X.append(np.load(p))
            keep.append(c)
    if not X:
        return None, []
    # bfloat16 bits are the top half of the float32 with the same value
    return (np.stack(X).astype(np.uint32) << 16).view(np.float32), keep


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
    """Stacked activations of ``cells`` with PERSON's belief residualised on the truth, the patient groups, and
    each row's cell (index into ``cells``)."""
    blocks, ys, truths, groups, cell_of = [], [], [], [], []
    for j, (arm, order) in enumerate(cells):
        a, keep = _load_acts(d, cases, arm, order)
        if a is None:
            continue
        t = targets[targets.arm == arm].set_index("case_id").loc[keep]
        blocks.append(a)
        ys.append(t.person.to_numpy())
        truths.append(t.truth.to_numpy())
        groups += keep
        cell_of += [j] * len(keep)
    if not blocks:
        return None, None, None, None, None
    truth = np.concatenate(truths)
    return np.concatenate(blocks), residual(np.concatenate(ys), truth), truth, np.array(groups), np.array(cell_of)


def permute_patients(y: np.ndarray, groups: np.ndarray, cells: np.ndarray, rng) -> np.ndarray:
    """The null's targets: patient labels permuted, so a patient's rows in every cell take one other patient's
    targets, as real targets come per patient. A row whose new patient lacks that cell takes a target drawn from
    the same cell."""
    patients = np.array(sorted(set(groups)))
    to = dict(zip(patients, rng.permutation(patients)))
    where = {(g, c): i for i, (g, c) in enumerate(zip(groups, cells))}
    out = np.empty_like(y)
    for i, (g, c) in enumerate(zip(groups, cells)):
        j = where.get((to[g], c))
        out[i] = y[j] if j is not None else y[rng.choice(np.flatnonzero(cells == c))]
    return out


def stage_probes(run: Path, source: Path, model: str, cfg: dict) -> dict:
    """Fit half: the attribution probe (PERSON's belief beyond the truth, in correctly bound cells), the
    misbound-cell probe orthogonalised against it, a null of probes trained on shuffled targets, and the lock."""
    d = out_dir(run, model)
    lock_path = d / "lock.json"
    if lock_path.exists():
        raise ValueError(f"{lock_path} exists: the lock is written once; remove it deliberately to refit")
    frame = pd.read_parquet(d / "noreason_responses.parquet")
    targets = _targets(source, model, frame if cfg.get("probe_targets", "noreason") == "noreason" else None)
    fit_cases = sorted(set(frame.case_id[frame.split == "fit"]))
    bound = gate_cells(d, cfg, "correctly_bound")
    said = frame[(frame.role == "C") & (frame.replicate == 0)].set_index(["case_id", "arm", "block_order"]).p
    rng = np.random.default_rng(int(C.load()["seed"]) + 4242)
    report, arrays = [], {}
    lock = {"rule": (
        "per variant: target = PERSON's belief residualised on the model's own (l_B on l_A); position = argmax over "
        f"{cfg['lock_positions']} of the attribution probe's best-layer CV r (folds and penalty grouped by patient); "
        f"L* = the contiguous layers around the best layer whose CV r is within {cfg['lock_tolerance']} of it; "
        "directions = the probes' encoding patterns; null = the encoding patterns of probes trained on shuffled "
        "targets (patient labels permuted); steering unit = SD of the training projections on w_attr at the best "
        "layer; w_attr trains only on correctly bound cells (gate.csv); targets from "
        f"{cfg.get('probe_targets', 'noreason')} beliefs"), "variants": {}}
    for variant, train_cells in cfg["attr_train"].items():
        if bound is not None:  # w_attr is trained only on correctly bound cells
            dropped = [c for c in train_cells if tuple(c) not in bound]
            train_cells = [c for c in train_cells if tuple(c) in bound]
            lock.setdefault("attr_train_dropped", {})[variant] = dropped
            lock.setdefault("attr_train_used", {})[variant] = train_cells
        if not train_cells:
            continue
        A, y, _, groups, train_cell_of = _training(d, targets, fit_cases, train_cells)
        if A is None:
            continue
        rec_arm, rec_order = cfg["rec_train"][variant][0]
        R, yr, truth_r, groups_r, _ = _training(d, targets, fit_cases, [(rec_arm, rec_order)])
        if R is not None:  # what the attribution probe's transfer into the misbound cell is scored against
            transfer = {"elena_beyond_truth": yr, "truth": truth_r,
                        "said": logit_of(said.reindex([(c, rec_arm, rec_order) for c in groups_r]).to_numpy())}
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
        hidden, n_null = A.shape[-1], int(cfg["random_directions"])
        u_attr, mu_attr, sd_attr = np.zeros((n_layers, hidden)), np.zeros(n_layers), np.ones(n_layers)
        u_rec, mu_rec = np.zeros((n_layers, hidden)), np.zeros(n_layers)
        rand, mu_rand = np.zeros((n_null, n_layers, hidden)), np.zeros((n_layers, n_null))
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
                pred = attr.predict(R[:, layer, p])
                for name, target in transfer.items():
                    report.append(dict(variant=variant, probe="attr_transfer", position=pos, layer=layer,
                                       target=name, r=float(np.corrcoef(pred, target)[0, 1]), n=len(yr)))
            if layer in window:
                for j in range(n_null):
                    shuffled = Ridge(A[:, layer, p], permute_patients(y, groups, train_cell_of, rng), groups)
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
    partial = lock_path.with_suffix(".tmp")  # written whole, then renamed: never a truncated lock
    partial.write_text(json.dumps(lock, indent=2, default=float))
    partial.replace(lock_path)
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


def _per_case(d: Path, model: str, name: str, cases, fn) -> pd.DataFrame:
    """Run ``fn(case)`` for every case not yet in ``<name>.csv``, appending each case's rows as it finishes."""
    path = d / f"{name}.csv"
    done = set(_read_rows(path, usecols=["case_id"]).get("case_id", []))
    for case in cases:
        if case not in done:
            _append(path, fn(case))
            print(f"[{name} {model}] {case}", flush=True)
    return _read_rows(path)


# Stage: 3.1 interchange patching
def patch_case(lens: Lens, prompts: dict, case: str, cfg: dict) -> list[dict]:
    """Each patch pair's sweeps over layers, per position role: donor states into the recipient (sufficiency)
    and recipient states into the donor (necessity)."""
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
                sweeps[direction, role] = lens.sweep(e.ids, variants, cfg["batch"])
        other = {"sufficiency": "necessity", "necessity": "sufficiency"}
        for (direction, role), ps in sweeps.items():
            # p_target: this sweep's own unpatched row; p_source: the unpatched source prompt, from the sweep
            # that runs on it (same role)
            p_src = sweeps[other[direction], role][0]
            rows += [dict(case_id=case, recipient=f"{r_arm}|{r_order}", donor=f"{d_arm}|{d_order}",
                          direction=direction, position=role, layer=layer, p=float(p), p_target=float(ps[0]),
                          p_source=float(p_src)) for layer, p in zip(layers, ps[1:])]
    return rows


def stage_patch(run: Path, source: Path, model: str, lens: Lens, cfg: dict) -> pd.DataFrame:
    """Both halves: the fit half picks each position's layer, the test half is tested at it (report)."""
    d = out_dir(run, model)
    read_lock(d, cfg)
    cfg = gate_cfg(d, cfg)
    prompts = _prompt_index(source, model)
    cases = halves(pd.read_parquet(d / "noreason_responses.parquet"), cfg["patch_halves"])
    return _per_case(d, model, "patch", cases, lambda c: patch_case(lens, prompts, c, cfg))


# Stage: 3.2 attention-edge knockouts
KNOCKOUTS = {  # name -> (query group, key group, include new answer tokens, order it applies to)
    "readout": ("readout", "own", True, None),        # positive control: removes leakage under any hypothesis
    "encoding": ("person", "own", False, "own_first"),
    "update": ("own", "person", False, "person_first"),
}
TESTED_KNOCKOUTS = ("encoding", "update")


def matched_filler(e: Encoded, group: str) -> list[int]:
    """As many filler tokens as the knocked group has, the ones nearest that group."""
    keys, filler = e.groups[group], e.groups["filler"]
    if len(filler) < len(keys):  # not expected (the filler has ~230 tokens, a record block fewer): use all of it
        print(f"[knockout] filler ({len(filler)} tokens) shorter than the {group} block ({len(keys)})", flush=True)
    centre = (min(keys) + max(keys)) / 2
    return sorted(sorted(filler, key=lambda k: (abs(k - centre), k))[:len(keys)])


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
                    for target, keys in (("record", e.groups[kg]), ("filler", matched_filler(e, kg))):
                        variants += [Variant(knock=[(s, e.groups[qg], keys, new)]) for s in starts]
                        specs += [(name, target, s) for s in starts]
            ps = lens.sweep(e.ids, variants, cfg["batch"])
            rows += [dict(case_id=case, arm=arm, order=order, role=role, knockout=name, target=target, start=s,
                          p=float(p)) for (name, target, s), p in zip(specs, ps)]
    return rows


def stage_knockout(run: Path, source: Path, model: str, lens: Lens, cfg: dict) -> pd.DataFrame:
    d = out_dir(run, model)
    read_lock(d, cfg)
    cfg = gate_cfg(d, cfg)
    prompts = _prompt_index(source, model)
    cases = halves(pd.read_parquet(d / "noreason_responses.parquet"), ["test"])
    return _per_case(d, model, "knockout", cases, lambda c: knockout_case(lens, prompts, c, cfg))


# Stage: 3.3 directions
def directions_case(lens: Lens, prompts: dict, case: str, variant: str, info: dict, arrays: dict,
                    cfg: dict) -> list[dict]:
    """Mean-ablation over L* at the locked probe position, and steering there at the best layer, of C, M and D."""
    if cfg.get("ablate_positions", "probe") != "probe":
        raise ValueError("ablate_positions must be 'probe': the question and answer tokens carry the upcoming "
                         "answer itself, so ablating there would make 3.3 pass trivially")
    torch = lens.torch

    def tensor(v):
        return torch.tensor(np.asarray(v), device=lens.device, dtype=torch.float32)

    window, best, unit = info["window"], info["best_layer"], float(info["steer_unit"])
    kinds = ["attr"] + (["rec"] if f"{variant}__u_rec" in arrays else [])
    rand, mu_rand = arrays[f"{variant}__rand"], arrays[f"{variant}__mu_rand"]
    # (name, [(layer, unit vector, mean projection)]) per ablation, then (name, alpha, vector) per steering
    ablations = [(f"ablate_{kind}", [(l, tensor(arrays[f"{variant}__u_{kind}"][l]),
                                      float(arrays[f"{variant}__mu_{kind}"][l])) for l in window]) for kind in kinds]
    ablations += [(f"ablate_null_{j}", [(l, tensor(rand[j, l]), float(mu_rand[l, j])) for l in window])
                  for j in range(len(rand))]
    steer_dirs = [(kind, arrays[f"{variant}__u_{kind}"][best]) for kind in kinds] + [("null_0", rand[0, best])]
    steers = [(f"steer_{kind}", float(a), tensor(u * a * unit)) for kind, u in steer_dirs for a in cfg["steer_alphas"]]
    rows = []
    for arm, order in cfg["direction_cells"].get(variant, []):
        for role in ("C", "M", "D"):
            key = (case, arm, order, role, 0)
            if key not in prompts:
                continue
            e = lens.encode(no_reasoning(prompts[key]))
            pos = e.positions[info["position"]]
            specs = [("none", np.nan, Variant())]
            specs += [(name, np.nan, Variant(ablate=[({l}, [pos], u, mu) for l, u, mu in parts]))
                      for name, parts in ablations]
            specs += [(name, alpha, Variant(steer=[(best, pos, vec)])) for name, alpha, vec in steers]
            ps = lens.sweep(e.ids, [v for _, _, v in specs], cfg["batch"])
            rows += [dict(variant=variant, case_id=case, arm=arm, order=order, role=role, intervention=name,
                          alpha=alpha, p=float(p)) for (name, alpha, _), p in zip(specs, ps)]
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
    return _per_case(d, model, "directions", cases, case_rows)


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
    # What the precision setting costs where the tests compare readouts from different forward passes: a padded
    # batch of distinct prompts (readout, gate) and a prefix-cached sweep (patch, knockout, directions), each
    # against the same prompts alone, in logits, beside 3.1's minimum gap.
    few = [lens.encode(no_reasoning(p)) for p in sample.prompt.iloc[:8]]
    solo = np.array([float(lens.readout(x.ids, [Variant()], reuse_prefix=False)["p"][0]) for x in few])
    padded = np.array(lens.readout_batch(few, batch=len(few))[0])
    swept = np.array([float(lens.readout(x.ids, [Variant(steer=[(1, x.positions["answer"],
                                                                 torch.zeros(hidden, device=lens.device))])])["p"][0])
                      for x in few])
    noise = float(max(np.abs(logit_of(padded) - logit_of(solo)).max(), np.abs(logit_of(swept) - logit_of(solo)).max()))
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
                  batch_invariance_max_abs_dp=float(np.abs(batch - alone).max()),
                  precision_noise_max_abs_logit=noise, patch_min_gap=float(cfg["patch_min_gap"]),
                  precision_noise_warning=bool(noise > float(cfg["patch_min_gap"]) / 4))
    if result["precision_noise_warning"]:
        print(f"[benchmark] WARNING: readouts move by {noise:.3f} logit between forward passes under this precision, "
              f"over a quarter of patch_min_gap ({cfg['patch_min_gap']}); consider tf32: false", flush=True)
    (d / "benchmark.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return result


# Stage: report
def _lambda(x, c, m, dd):
    den = slope(dd - m, x)
    return slope(c - m, x) / den if den != 0 else np.nan


def _beta_d(x, m, dd):
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
    g = pd.read_csv(d / "gate.csv") if (d / "gate.csv").exists() else None
    if g is not None:
        excluded = [f"{a}|{o}" for a, o in zip(g.arm[~g.enters_phase3], g.order[~g.enters_phase3])]
        lines += [f"Gate: {int(g.enters_phase3.sum())} of {len(g)} cells enter Phase 3; excluded: "
                  f"{', '.join(excluded) or 'none'}.", ""]

    def gap(arm):
        return (own[(arm, "A")] - own[(arm, "B")]) if (arm, "A") in own.columns else None

    # 3.1 patching: each position's layer chosen on the fit half, tested on the test half
    patch = _read_rows(d / "patch.csv")
    if not patch.empty:
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
    ko = _read_rows(d / "knockout.csv")
    if not ko.empty:
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
        testable = None
        if g is not None and "knockout_testable" in g:
            testable = dict(zip(zip(g.arm, g.order), g.knockout_testable))
        for r in (kt[(kt.start == 0) & kt.knockout.isin(TESTED_KNOCKOUTS)].itertuples() if not kt.empty else []):
            name = f"3.2_leak_enters_at[{r.knockout}|{r.arm}|{r.order}]"
            if testable is not None and not testable.get((r.arm, r.order), False):
                rules[name] = {"status": "not applicable", "supported": False,
                               "reason": f"fit-half no-reasoning lambda below {cfg['knockout_min_lambda']}: no room "
                                         "for the 0.3 drop",
                               "rule": "knocking out this attention edge lowers lambda by >= 0.3 beyond the filler",
                               "interval": _interval((r.lambda_drop_vs_filler, r.ci_lower, r.ci_upper))}
                continue
            rules[name] = _verdict(
                r.lambda_drop_vs_filler >= 0.3 and r.ci_lower > 0, r.blank_reason) | dict(
                interval=_interval((r.lambda_drop_vs_filler, r.ci_lower, r.ci_upper)),
                rule="knocking out this attention edge at every layer lowers lambda by >= 0.3 more than the same "
                     "knockout aimed at the filler (CI above 0); the readout knockout is a positive control")
    # 3.3 directions (test half): lambda with C, M and D all intervened
    dr = _read_rows(d / "directions.csv")
    if not dr.empty:
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
    parser.add_argument("--resamples", type=int, default=None, help="default: the config's resamples (locked)")
    args = parser.parse_args()
    run, source = args.run.resolve(), args.source.resolve()
    run.mkdir(parents=True, exist_ok=True)
    B.read_plan(source)  # configures the source run's config and verifies its plan
    cfg = load_cfg(run)
    if not (run / "causal_config.json").exists():
        (run / "causal_config.json").write_text(json.dumps(cfg, indent=2))
    if args.resamples is not None and args.resamples != int(cfg["resamples"]):
        raise ValueError(f"--resamples {args.resamples} differs from the config's {cfg['resamples']}: set it in "
                         "causal_config.json before the probes stage (it is locked)")
    args.resamples = int(cfg["resamples"])
    stages = STAGES if args.stage == "all" else (args.stage,)
    lens = None
    locked = (out_dir(run, args.model) / "lock.json").exists
    with (run / ".causal.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for stage in stages:
            if stage in GPU_STAGES and lens is None:
                import torch
                torch.backends.cuda.matmul.allow_tf32 = bool(cfg.get("tf32", True))
                torch.backends.cudnn.allow_tf32 = bool(cfg.get("tf32", True))
                lens = Lens(args.model, dtype=cfg.get("dtype", "float32"),
                            statistic=cfg.get("readout_statistic", "mean_logit"))
            if stage == "benchmark":
                stage_benchmark(run, source, args.model, lens, cfg)
            elif stage == "readout":
                stage_readout(run, source, args.model, lens, cfg)
            elif stage in ("gate", "probes") and locked():
                print(f"[{stage} {args.model}] skipped: the lock exists (it fixes gate.csv and the directions)")
            elif stage == "gate":
                stage_gate(run, source, args.model, cfg, args.resamples)
            elif stage == "probes":
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
