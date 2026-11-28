"""Tiny Qwen and an explicit reference calculation for probability readouts."""
import os
import unittest

REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"


def snapshot(**kwargs):
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download("Qwen/Qwen3-0.6B", revision=REVISION, local_files_only=True, **kwargs)
    except Exception:
        return None


TOKENIZER = os.environ.get("SO_TEST_TOKENIZER") or snapshot(allow_patterns=["tokenizer*", "vocab.json", "merges.txt", "*.json"])
requires_tokenizer = unittest.skipUnless(TOKENIZER, "no Qwen3 tokenizer available")


def tiny_model(**config):
    import torch
    from transformers import AutoTokenizer, Qwen3Config, Qwen3ForCausalLM
    tok = AutoTokenizer.from_pretrained(TOKENIZER)
    cfg = Qwen3Config(vocab_size=len(tok), hidden_size=64, intermediate_size=128, num_hidden_layers=4,
                      num_attention_heads=4, num_key_value_heads=2, head_dim=16, max_position_embeddings=4096,
                      tie_word_embeddings=True, **config)
    torch.manual_seed(0)
    return Qwen3ForCausalLM(cfg).eval(), tok


def tiny_lens():
    import torch
    from belief_leakage.binding_causal import Lens
    lm, tok = tiny_model(attn_implementation="eager")
    return Lens(lm=lm.float(), tok=tok, device=torch.device("cpu"), template_kwargs={"enable_thinking": False})


def digit_probabilities(logits, ids, digits):
    """Run the full prompt again for each first digit, independently of either engine's cached readout."""
    first = logits(ids).softmax(-1)[digits]
    second = [logits(ids + [digit]).softmax(-1)[digits] for digit in digits]
    return first, second


def two_digit_value(p1, q2s, statistic="mean_logit"):
    """The readout from explicit first-digit probabilities ``p1`` and each first digit's second-digit
    probabilities ``q2s[a]``: E[p] ("expected_p"), or the probability whose logit is E[clip_logit(p)]."""
    import math

    def f(p):
        if statistic == "expected_p":
            return p
        p = min(max(p, 0.01), 0.99)
        return math.log(p) - math.log1p(-p)
    total = 0.0
    for a in range(10):
        q2 = [float(x) for x in q2s[a]]
        end = max(1 - sum(q2), 0.0)
        total += float(p1[a]) * (sum(q2[b] * f(a / 10 + b / 100) for b in range(10)) + end * f(a / 10)) / (sum(q2) + end)
    return total if statistic == "expected_p" else 1 / (1 + math.exp(-total))
