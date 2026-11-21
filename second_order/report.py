"""Shared report helpers: model display names and markdown tables."""

from __future__ import annotations

import pandas as pd

from latent_beliefs.models import MODEL_DISPLAY

LOCAL_DISPLAY = {"gemma3_4b_mlx": "Gemma 3 4B (local 4-bit)", "gemma3_12b_mlx": "Gemma 3 12B (local 4-bit)",
                 "qwen3_8b": "Qwen3 8B", "qwen3_14b": "Qwen3 14B", "qwen3_8b_hf": "Qwen3 8B (transformers)",
                 "qwen3_0_6b_hf": "Qwen3 0.6B (smoke)", "synthetic": "Synthetic"}


def display(model: str) -> str:
    return MODEL_DISPLAY.get(model) or LOCAL_DISPLAY.get(model, model)


def md_table(frame: pd.DataFrame, cols: list[str], digits: int = 3) -> str:
    if frame.empty:
        return "_(no rows)_\n"
    f = frame[cols].copy()
    for c in f.columns:
        if f[c].dtype.kind == "f":
            f[c] = f[c].map(lambda v: "" if pd.isna(v) else f"{v:.{digits}f}")
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "".join("| " + " | ".join(str(v) for v in r) + " |\n" for r in f.to_numpy())
