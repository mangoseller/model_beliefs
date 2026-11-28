"""Probability readouts and tokenization shared by the binding and steering experiments."""
from __future__ import annotations

import numpy as np

PREFIX = '{"probability": 0.'
REASONING = ("Provide your reasoning first.\nThen output valid JSON on a new line in exactly this structure:\n"
             '{"probability": <number>}\n\nThe number must be between 0 and 1.')
NO_REASONING = ('Do not explain. Output only valid JSON in exactly this structure:\n'
                '{"probability": <number>}\n\nThe number must be between 0 and 1.')


def no_reasoning(prompt: str) -> str:
    if REASONING not in prompt:
        raise ValueError("prompt lacks the probability instruction")
    return prompt.replace(REASONING, NO_REASONING)


def render_chat(tok, messages, template_kwargs, *, tokenize=False):
    """Apply the model's configured template to messages and append its generation prompt."""
    return tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=tokenize, **template_kwargs)


def chat_text(tok, prompt: str, template_kwargs: dict) -> str:
    """The prompt as one user turn in the model's chat template, ready for the reply."""
    messages = [{"role": "user", "content": prompt}]
    return render_chat(tok, messages, template_kwargs)


def encode_prompt(tok, prompt, template_kwargs, prefix):
    """Return token IDs, character offsets, and where the prompt sits inside its chat template."""
    chat = chat_text(tok, prompt, template_kwargs)
    encoded = tok(chat + prefix, add_special_tokens=False, return_offsets_mapping=True)
    start = chat.find(prompt)
    if start < 0:
        raise ValueError("prompt not found in the chat template")
    return list(encoded["input_ids"]), np.asarray(encoded["offset_mapping"]).reshape(-1, 2), start, len(chat)


def first_difference(sequences, default=None):
    """First position at which the sequences disagree within their common length."""
    shortest = min(map(len, sequences))
    return next((i for i in range(shortest) if len({seq[i] for seq in sequences}) > 1), default)


def set_tf32(enabled):
    import torch
    torch.backends.cuda.matmul.allow_tf32 = enabled
    torch.backends.cudnn.allow_tf32 = enabled


class CausalReadout:
    """Shared model setup, two-digit probability calculation and batching for causal interventions."""

    def __init__(self, lm, tok, device, template_kwargs, statistic):
        import torch
        self.torch = torch
        lm.eval()
        self.lm, self.tok = lm, tok
        self.device = device if device is not None else next(lm.parameters()).device
        self.kw = template_kwargs if template_kwargs is not None else {"enable_thinking": False}
        self.statistic = statistic
        self.layers = lm.model.layers
        self.n_layers = len(self.layers)
        self.digits = [tok.convert_tokens_to_ids(str(digit)) for digit in range(10)]
        if len(set(self.digits)) != 10 or any(d is None or d == tok.unk_token_id for d in self.digits):
            raise ValueError("tokenizer lacks single-digit tokens")
        self.pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
        self.batch_cap = None

    def close(self):
        for handle in self._handles:
            handle.remove()

    def _batches(self, items, size, run, on_oom=None):
        """Yield consecutive batch results, retaining the smaller size after an out-of-memory retry."""
        size = min(size, self.batch_cap or size)
        done = 0
        while done < len(items):
            try:
                result = run(items[done:done + size])
            except self.torch.OutOfMemoryError:
                if on_oom is not None:
                    on_oom()
                if size == 1:
                    raise
                self.torch.cuda.empty_cache()
                size = self.batch_cap = max(1, size // 2)
                print(f"[{type(self).__name__.lower()}] out of memory: batch -> {size}", flush=True)
                continue
            yield result
            done += size

    def _padded_tokens(self, seqs, *, left):
        """Pad a chunk on the chosen side and return IDs, mask, lengths and first real columns."""
        torch = self.torch
        lengths = [len(tokens) for tokens in seqs]
        width = max(lengths)
        starts = [width - n if left else 0 for n in lengths]
        ids = torch.full((len(seqs), width), self.pad, dtype=torch.long)
        mask = torch.zeros_like(ids)
        for row, tokens in enumerate(seqs):
            columns = slice(starts[row], starts[row] + lengths[row])
            ids[row, columns] = torch.tensor(tokens, dtype=torch.long)
            mask[row, columns] = 1
        return ids.to(self.device), mask.to(self.device), lengths, starts

    def _branch_inputs(self, tokens, lengths):
        """Every candidate token continues each row at the same next position."""
        ids = self.torch.tensor([tokens] * len(lengths), device=self.device)
        positions = self.torch.tensor(lengths, device=self.device)[:, None].expand_as(ids)
        return ids, positions

    def _isolate_branches(self, mask, count):
        """Allow each parallel continuation to see its own token and none of its siblings."""
        block = mask.new_full((count, count), self.torch.finfo(mask.dtype).min)
        mask[:, 0, :, -count:] = block.fill_diagonal_(0)

    def _digit_readout(self, logits, continuations, min_share):
        """Score every first digit above the protocol's cutoff by running its second-token continuation."""
        torch = self.torch
        rows = logits.shape[0]
        first_digit = logits.softmax(-1)[:, self.digits]       # [rows, 10]
        mass = first_digit.sum(-1)
        first_share = first_digit / mass[:, None]
        if self.statistic == "expected_p":
            def score(probability):
                return probability
        else:
            def score(probability):
                probability = probability.clamp(0.01, 0.99)
                return torch.log(probability) - torch.log1p(-probability)
        tenths = torch.arange(10, device=self.device, dtype=torch.float32)
        # per first digit, the expected score of the answers that start with it; a first digit without weight
        # gets the middle of its interval
        expected = score(tenths / 10 + 0.045).repeat(rows, 1)
        candidates = [digit for digit, peak in enumerate(first_share.amax(0).tolist()) if peak >= min_share]
        second_logits = continuations([self.digits[digit] for digit in candidates])
        second_digit = second_logits.softmax(-1)[:, :, self.digits]            # [rows, candidates, 10]
        ends = (1 - second_digit.sum(-1)).clamp_min(0)                         # anything else ends the number
        first = torch.tensor(candidates, device=self.device, dtype=torch.float32)
        two_digit_scores = score(first[None, :, None] / 10 + tenths[None, None, :] / 100)
        one_digit_scores = score(first[None, :] / 10)
        expected[:, candidates] = (((second_digit * two_digit_scores).sum(-1) + ends * one_digit_scores)
                                   / (second_digit.sum(-1) + ends))
        value = (first_share * expected).sum(-1)
        if self.statistic != "expected_p":
            value = torch.sigmoid(value)
        return value.cpu().numpy(), mass.cpu().numpy()
