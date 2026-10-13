"""The nine language models analysed in the paper.

Several random seeds and output columns in the original analysis were derived
from a model's *position* in a list.  Those lists also contained Llama 3.1 8B,
which was collected but excluded from the paper.  To reproduce every number
exactly, each position-derived quantity below records the original positions
explicitly; the gaps left by the excluded model are intentional.
"""

from __future__ import annotations

MODELS = (
    "deepseek_v32",
    "gemma3_4b",
    "gemma3_12b",
    "gemma3_27b",
    "glm5",
    "llama31_70b",
    "qwen3_32b",
    "openai_luna",
    "openai_terra",
)

MODEL_DISPLAY = {
    "deepseek_v32": "DeepSeek V3.2",
    "gemma3_4b": "Gemma 3 4B",
    "gemma3_12b": "Gemma 3 12B",
    "gemma3_27b": "Gemma 3 27B",
    "glm5": "GLM-5",
    "llama31_70b": "Llama 3.1 70B",
    "qwen3_32b": "Qwen3 32B",
    "openai_luna": "GPT-5.6 Luna",
    "openai_terra": "GPT-5.6 Terra",
}

# ---------------------------------------------------------------- providers
# Amazon Bedrock model IDs (Converse batch inference).
BEDROCK_MODEL_IDS = {
    "deepseek_v32": "deepseek.v3.2",
    "gemma3_4b": "google.gemma-3-4b-it",
    "gemma3_12b": "google.gemma-3-12b-it",
    "gemma3_27b": "google.gemma-3-27b-it",
    "glm5": "zai.glm-5",
    "llama31_70b": "us.meta.llama3-1-70b-instruct-v1:0",
    "qwen3_32b": "qwen.qwen3-32b-v1:0",
}
# OpenAI model IDs (Responses API batch).
OPENAI_MODEL_IDS = {
    "openai_luna": "gpt-5.6-luna",
    "openai_terra": "gpt-5.6-terra",
}

# ---------------------------------------------------------------- figure order
# Figure axes: ascending Artificial Analysis Intelligence Index v4.1.1
# (retrieved 2026-08-21): Gemma 3 4B 1, Gemma 3 12B 6, Llama 3.1 70B 7,
# Gemma 3 27B 7, Qwen3 32B 8, DeepSeek V3.2 25, GLM-5 41, GPT-5.6 Luna 52,
# GPT-5.6 Terra 57.  The tie at 7 is broken by the mean held-out c-index.
FIGURE_ORDER = (
    "gemma3_4b",
    "gemma3_12b",
    "llama31_70b",
    "gemma3_27b",
    "qwen3_32b",
    "deepseek_v32",
    "glm5",
    "openai_luna",
    "openai_terra",
)
# Low-, medium-, and high-capability examples in Figures 2 and 5.
EXAMPLE_MODELS = ("gemma3_4b", "qwen3_32b", "openai_terra")
# Models in the reasoning-prefix branching experiments.
BRANCHING_MODELS = ("gemma3_12b", "deepseek_v32")

# ---------------------------------------------------------------- positions
# Position of each model in the analysis pipeline.  Written to the
# ``model_position`` column of analysis tables and used for capability ranks
# (rank = position + 1) and per-cell seeds in the fitting stages.
PIPELINE_POSITION = {
    "gemma3_4b": 1,
    "gemma3_12b": 2,
    "llama31_70b": 3,
    "gemma3_27b": 4,
    "qwen3_32b": 5,
    "glm5": 6,
    "deepseek_v32": 7,
    "openai_luna": 8,
    "openai_terra": 9,
}
# Index used to seed each model's train/validation/test split
# (seed + 1009 * (index + 1)).  The CKD and Werewolf builders and the
# forecasting builder listed the models in different orders.
SPLIT_INDEX = {
    "ckd": {
        "deepseek_v32": 0,
        "gemma3_12b": 1,
        "gemma3_27b": 2,
        "gemma3_4b": 3,
        "llama31_70b": 4,
        "openai_luna": 6,
        "openai_terra": 7,
        "qwen3_32b": 8,
        "glm5": 9,
    },
    "werewolf": {
        "deepseek_v32": 0,
        "gemma3_12b": 1,
        "gemma3_27b": 2,
        "gemma3_4b": 3,
        "llama31_70b": 4,
        "openai_luna": 6,
        "openai_terra": 7,
        "qwen3_32b": 8,
        "glm5": 9,
    },
    "forecasting": {
        "deepseek_v32": 0,
        "gemma3_12b": 1,
        "gemma3_27b": 2,
        "gemma3_4b": 3,
        "glm5": 4,
        "llama31_70b": 5,
        "openai_luna": 7,
        "openai_terra": 8,
        "qwen3_32b": 9,
    },
}
# Position used to seed the reciprocal-prediction bootstrap
# (seed + 1000 * domain + 10 * position + condition).
RECIPROCAL_BOOTSTRAP_POSITION = {
    "gemma3_4b": 0,
    "gemma3_12b": 1,
    "llama31_70b": 3,
    "gemma3_27b": 4,
    "qwen3_32b": 5,
    "deepseek_v32": 6,
    "glm5": 7,
    "openai_luna": 8,
    "openai_terra": 9,
}


def pipeline_order() -> tuple[str, ...]:
    """Models sorted by pipeline position."""
    return tuple(sorted(PIPELINE_POSITION, key=PIPELINE_POSITION.__getitem__))


assert set(MODELS) == set(MODEL_DISPLAY) == set(FIGURE_ORDER) == set(PIPELINE_POSITION)
assert set(BEDROCK_MODEL_IDS) | set(OPENAI_MODEL_IDS) == set(MODELS)
