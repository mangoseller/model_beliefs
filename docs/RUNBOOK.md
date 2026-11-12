# Second-order build: runbook

Every command runs from the repository root with

```sh
PY=/Users/sam/miniforge3/envs/latent-beliefs/bin/python
export PYTHONPATH=build/so_dependencies:build/pilot_dependencies:.
```

`build/so_dependencies` holds `pyarrow` only. `build/pilot_dependencies`
holds MLX and `mlx-lm` for local runs. The pinned environment is unchanged.
All settings are in `configs/second_order.yaml`.

## Checks that run without model calls

```sh
$PY -m unittest tests.test_prompts_second_order tests.test_leakage   # prompt, parser, estimator, cache tests
$PY -m second_order.baseline_check --out-dir build/second_order_baseline   # first-order numbers still reproduce
$PY -m second_order.simulate --lambdas 0 0.3 0.7 1                   # synthetic recovery (add --kappa 0.7 --rho -0.2 --hedge 0.7 for the plan's scenarios)
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
settings but cost about twice as much. Whether to add batch submission is a
decision for Sam.

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
