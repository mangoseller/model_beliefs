# Second-order experiment: runbook

Every command runs from the repository root with

```sh
conda activate latent-beliefs
PY=python
export PYTHONPATH=.        # add the folders holding pyarrow and MLX if they are installed outside the environment
```

The pinned environment is unchanged; `second_order/` additionally needs
`pyarrow`, and local runs need MLX and `mlx-lm` (Apple silicon) or `torch`,
`transformers` and `accelerate` (CUDA). All settings are in
`configs/second_order.yaml`.

## Checks that run without model calls

```sh
$PY -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke tests.test_generation
$PY -m unittest tests.test_pipeline                                  # end to end on the synthetic backend (minutes)
$PY -m second_order.baseline_check --out-dir build/second_order_baseline   # first-order numbers still reproduce
$PY -m second_order.simulate --lambdas 0 0.3 0.7 1                   # synthetic recovery (--kappa, --rho, --hedge set the scenario)
$PY scripts/estimator_bias_check.py                                  # idealised comparison of the two estimators
$PY -m second_order.example_prompts                                  # docs/example_prompts.md
```

## Generation

Responses are cached per item in `data/generation_cache/<model>.jsonl`. The
key is `sha256([model, case, condition, output, replicate])`. Reruns skip
cached items. A run over 1,000 calls prints its call count and cost
estimate, then stops unless `--approve` is passed. Add `--dry-run` to see
only the estimate.

| Backend | Needs |
|---|---|
| `bedrock` (7 open-weight models) | `boto3` credentials with `bedrock-runtime:Converse` in the region that serves the models (`AWS_PROFILE`, `AWS_REGION`). Synchronous Converse calls, 8 threads. |
| `openai` (GPT-5.6 Luna and Terra) | `OPENAI_API_KEY`. Responses API, reasoning effort medium. |
| `mlx` (local 4-bit Gemma) | Apple silicon. Batch 24 for 4B (a batch of 48 ran out of GPU memory in 16 GB). |

The settings match condition A's collection: temperature 1, 4,096 output
tokens, one user message, no system prompt. The first-order data were
collected with the **batch** APIs. Synchronous calls are the same models and
settings but cost about twice as much. Batch submission is not implemented
yet.

```sh
# Phase 1 pilot (50 flagged cases, numeric probability x 3 per set H, S1-S4 and the flip F; A topped up to 3)
$PY -m second_order.pilot generate --model openai_terra --approve
$PY -m second_order.pilot generate --model qwen3_32b --approve
$PY -m second_order.pilot report          # results/pilot/pilot_summary.csv

# Phase 3 main run, per model. With no --conditions, a model runs its scheduled conditions
# (config `schedule`): B, C_john, D_john, M_S4, B_F, C_F, M_F, B_H, C_H, M_H for all models, plus
# B_S1, C_S1, C_user, D_user for three. A comes from the release cache.
$PY -m second_order.run_generation --model openai_terra --dry-run
$PY -m second_order.run_generation --model openai_terra --approve
$PY -m second_order.phase3 --model openai_terra     # parse, fits, latents, beliefs, retention, transcripts

# Smoke run (plan: one model through Phase 5 first)
$PY -m second_order.run_generation --model gemma3_4b_mlx --smoke --approve   # local; regenerates A too
$PY -m second_order.phase3 --model gemma3_4b_mlx --smoke

```

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
`--dataset ckd` or `--dataset bespoke` runs one dataset; `--stage comprehension`
runs only the follow-ups, once the C answers are cached. Generation resumes
from the cache after an interruption.

The rest needs no GPU (copy `data/generation_cache/` back if the GPU is elsewhere):

```sh
$PY -m second_order.phase3 --model qwen3_8b      # parse, latent fits (CKD), beliefs, retention, comprehension
$PY -m second_order.phase3 --model qwen3_14b
$PY -m second_order.curse                        # results/curse/*.csv and results/curse/RESULTS.md
$PY -m second_order.figures                      # figures/curse_estimates, curse_scatter (+ the RQ figures)
```

Variants:

- `qwen3_8b_hf` uses transformers instead of vLLM.
- `qwen3_0_6b_hf` is a CPU-sized smoke test of the chat template and parsing,
  e.g. `--model qwen3_0_6b_hf --dataset bespoke --cases 2`.
- `synthetic` writes text from known λ, φ and noise; it is what
  `tests.test_pipeline` runs.

The `SO_CONFIG`, `SO_DATA_DIR`, `SO_RESULTS_DIR`, `SO_FIGURES_DIR` and
`SO_BESPOKE_ITEMS` environment variables redirect a run (`second_order/config.py`).

## Phase 4: activations and probes

On this Mac (MLX, Gemma 3 4B and 12B, 4-bit):

```sh
$PY -m second_order.activations extract --model gemma3_4b --act-model gemma3_4b_mlx \
    --conditions A B C_john D_john M_S4 B_F C_F M_F       # P3 only where responses are cached
$PY -m second_order.activations pack --model gemma3_4b
$PY -m second_order.probes --model gemma3_4b              # results/probes/gemma3_4b/, data/beliefs/gemma3_4b/probe.parquet
```

On a CUDA instance, for Gemma 3 27B, Qwen3 32B and Llama 3.1 70B: install
`torch`, `transformers` and `accelerate`. Copy over `data/generation_cache/`,
`data/release/` and `build/datasets/ckd.csv`. Then use the `*_hf` config
entries, e.g.
`--model gemma3_27b --act-model gemma3_27b_hf`. Activations need about 1 MB
per item for a 4B model, scaling with layers × hidden size. 70B needs about
2 × 80 GB GPUs in bf16.

## Analysis and figures (no model calls)

```sh
$PY -m second_order.analyze            # results/*.csv, results/disagreements/, RESULTS.md
$PY -m second_order.figures            # figures/rq1_*, rq1c_*, rq2_*, rq3_*, retention (.pdf, .png), from results/ only
```
