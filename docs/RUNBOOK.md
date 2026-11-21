# Second-order experiment: runbook

Every command runs from the repository root with

```sh
conda activate latent-beliefs
PY=python
export PYTHONPATH=.        # add the folders holding pyarrow and MLX if they are installed outside the environment
```

The pinned environment is unchanged. `second_order/` additionally needs
`pyarrow` and `pyyaml`. Local generation needs `vllm`, or `torch` with
`transformers` and `accelerate` (CUDA); MLX needs `mlx-lm` (Apple silicon).
All settings are in `configs/second_order.yaml`.

The `SO_CONFIG`, `SO_DATA_DIR`, `SO_RESULTS_DIR`, `SO_FIGURES_DIR` and
`SO_BESPOKE_ITEMS` environment variables redirect a run (`second_order/config.py`).

## Checks that run without model calls

```sh
$PY -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke tests.test_generation
$PY -m unittest tests.test_pipeline                                  # end to end on the synthetic backend (minutes)
$PY -m second_order.bespoke                                          # the bespoke item file: Bayes answers, warnings
$PY -m second_order.example_prompts                                  # docs/example_prompts.md
./reproduce.sh analysis                                              # the original paper's figures and tables
$PY -m second_order.baseline_check --out-dir build/second_order_baseline   # first-order CKD numbers still reproduce
```

`./reproduce.sh analysis` writes to `build/`, or to `LB_BUILD_DIR` if it is
set. Off the pinned macOS environment, the CKD c-indices match the paper to
within about 0.01, and R² matches exactly (README, "Reproducing the original
paper").

## The generation cache

- **Cache file.** Responses are cached per item in
  `data/generation_cache/<model>.jsonl`.
- **Key.** `sha256([model, case, condition, output, replicate])`, with
  `sample` appended when it is nonzero.
- **Reruns** skip cached items.
- **Refused records.** A cached record is refused if its prompt hash differs
  from the prompt now built. It is also refused if it was generated with
  other settings: sampling, chat-template arguments (such as `enable_thinking`)
  or weights.
- **Cost gate.** A run over 1,000 calls prints its call count and cost
  estimate, then stops unless `--approve` is passed. Add `--dry-run` to see
  only the estimate.

Every backend sends the whole conversation, so the two-turn comprehension
follow-ups work on all of them.

| Backend | Needs |
|---|---|
| `vllm` (Qwen3 8B, 14B) | CUDA. One seed per item. |
| `hf` (transformers fallback, `*_hf` entries) | CUDA or CPU. One seed per batch, so a resumed run is not bit-reproducible. |
| `synthetic` | Nothing; text from known parameters (`second_order/synthetic.py`). |
| `bedrock` (7 open-weight models) | `boto3` credentials with `bedrock-runtime:Converse` in the region that serves the models (`AWS_PROFILE`, `AWS_REGION`). Synchronous Converse calls, 8 threads. |
| `openai` (GPT-5.6 Luna and Terra) | `OPENAI_API_KEY`. Responses API, reasoning effort medium. |
| `mlx` (local 4-bit Gemma) | Apple silicon. Batch 24 for 4B (a batch of 48 ran out of GPU memory in 16 GB). |

The settings match condition A's collection: temperature 1, 4,096 output
tokens, no system prompt. A model entry may override `temperature`, `top_p`,
`top_k`, `max_tokens` and `enable_thinking`.

## Curse-of-knowledge experiment (Qwen3 8B and 14B)

Settings are in `experiment`, `bespoke` and the `qwen3_*` model entries of the
config. Each model runs nine conditions, `A B C_john M_S4 D_john B_F C_F M_F D_F`,
on two datasets:

- **CKD**: 500 cases × 16 outputs, plus a second run of A, B and B_F (for ρ).
- **Bespoke**: the local item file × 20 samples.

A comprehension follow-up comes after each C answer.

On a CUDA machine:

```sh
pip install vllm                       # brings a matching torch; transformers for the hf fallback
# Copy data/release/ (cases, prompts) and the bespoke item file (config `bespoke.items`).
$PY -m second_order.bespoke                                       # check the item file: Bayes answers, warnings
$PY -m second_order.run_generation --model qwen3_8b --dry-run     # call counts per dataset
$PY -m second_order.run_generation --model qwen3_8b --approve     # both datasets, then the comprehension turns
$PY -m second_order.run_generation --model qwen3_14b --approve
```

Approximate calls per model:

| Part | Calls |
|---|---|
| CKD: 9 conditions × 500 cases × 16 outputs | 72,000 |
| CKD: second runs of A, B, B_F | 24,000 |
| CKD: comprehension | 1,000 |
| Bespoke: 20 items × 20 samples × 12 runs | 4,800 |
| Bespoke: comprehension | 800 |

vLLM serves the model locally, with one seed per item, temperature 1, at most
4,096 tokens and thinking off. Qwen3 14B in bf16 needs about 30 GB of GPU
memory (`tensor_parallel_size` in the model entry spreads it over GPUs).

- `--dataset ckd` or `--dataset bespoke` runs one dataset.
- `--stage comprehension` runs only the follow-ups, once the C answers are cached.
- Generation resumes from the cache after an interruption.

The rest needs no GPU (copy `data/generation_cache/` back if the GPU is elsewhere):

```sh
$PY -m second_order.phase3 --model qwen3_8b      # parse, latent fits (CKD), beliefs, retention, comprehension
$PY -m second_order.phase3 --model qwen3_14b
$PY -m second_order.curse                        # outputs/curse/*.csv and outputs/curse/RESULTS.md
$PY -m second_order.figures                      # figures/curse_estimates, curse_scatter, retention
```

Latent fits run on CPU but slowly, roughly an hour per model for 500 cases.
On a CUDA machine, phase 3 is faster.

Variants:

- `qwen3_8b_hf` uses transformers instead of vLLM.
- `qwen3_0_6b_hf` is a CPU-sized smoke test of the chat template, the
  follow-up turn and parsing, e.g. `--model qwen3_0_6b_hf --dataset bespoke --cases 2`.
- `synthetic` writes text from known λ, φ and noise; it is what
  `tests.test_pipeline` runs.

## Deprecated: the earlier RQ1–RQ3 design

These modules live in `second_order/deprecated/` and still run. The current
study does not use them.

```sh
# Idealised estimator checks
$PY -m second_order.deprecated.simulate --lambdas 0 0.3 0.7 1       # latent-level recovery (--kappa, --rho, --hedge)
$PY -m second_order.deprecated.estimator_bias_check                 # original vs matched-control estimator

# Phase 1 pilot (50 flagged cases, numeric probability x 3 per set H, S1-S4 and the flip F; A topped up to 3)
$PY -m second_order.deprecated.pilot generate --model openai_terra --approve
$PY -m second_order.deprecated.pilot generate --model qwen3_32b --approve
$PY -m second_order.deprecated.pilot report                         # outputs/pilot/pilot_summary.csv

# Main run on the hosted models. With no --conditions, a model runs its scheduled conditions
# (config `schedule`): B, C_john, D_john, M_S4, B_F, C_F, M_F, B_H, C_H, M_H for all models, plus
# B_S1, C_S1, C_user, D_user for three. A comes from the release cache, so these models need
# build/datasets/ckd.csv (from ./reproduce.sh analysis) for their splits.
$PY -m second_order.run_generation --model openai_terra --dry-run
$PY -m second_order.run_generation --model openai_terra --approve
$PY -m second_order.phase3 --model openai_terra

# Smoke run on a local MLX model (regenerates A too)
$PY -m second_order.run_generation --model gemma3_4b_mlx --smoke --approve
$PY -m second_order.phase3 --model gemma3_4b_mlx --smoke

# Analysis and figures by research question (no model calls)
$PY -m second_order.deprecated.analyze            # outputs/*.csv, outputs/disagreements/, outputs/RESULTS.md
$PY -m second_order.deprecated.rq_figures         # figures/rq1_*, rq1c_*, rq2_*, rq3_*, retention
```

The hosted models' default schedule still includes C_john and C_F, so
`run_generation` also asks their comprehension follow-ups. Pass
`--stage main` to skip them.

### Activations and probes (RQ3)

On a Mac (MLX, Gemma 3 4B and 12B, 4-bit):

```sh
$PY -m second_order.deprecated.activations extract --model gemma3_4b --act-model gemma3_4b_mlx \
    --conditions A B C_john D_john M_S4 B_F C_F M_F       # P3 only where responses are cached
$PY -m second_order.deprecated.activations pack --model gemma3_4b
$PY -m second_order.deprecated.probes --model gemma3_4b   # outputs/probes/gemma3_4b/, data/beliefs/gemma3_4b/probe.parquet
```

On a CUDA instance, for Gemma 3 27B, Qwen3 32B and Llama 3.1 70B:

1. Install `torch`, `transformers` and `accelerate`.
2. Copy over `data/generation_cache/`, `data/release/` and `build/datasets/ckd.csv`.
3. Use the `*_hf` config entries, e.g. `--model gemma3_27b --act-model gemma3_27b_hf`.

Activations need about 1 MB per item for a 4B model, scaling with layers ×
hidden size. 70B needs about 2 × 80 GB GPUs in bf16.
