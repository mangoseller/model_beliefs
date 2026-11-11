# Orientation: the first-order reproduction, mapped for the second-order extension

Phase 0 of the second-order belief build plan (11 Nov 2026). Nothing in
`latent_beliefs/` has been changed. New code lives in `second_order/`.

## 1. Where things live

| Step | Module | Notes |
|---|---|---|
| Case construction | `latent_beliefs/ckd/cases.py` (`PROFILE_FEATURES`, `read_cases`), `ckd/nhanes.py`, `ckd/risk.py` | Two seeded cohorts of 500 cases each, stratified by risk decile. The prompts use `original500`. The bin edges use all 1,000 risk scores. Cases are in `data/release/sources/ckd/cases.csv`, with the true label in `ckd`. |
| Profile rendering | `ckd/prompts.py::patient_profile` | 12 lines in `PROFILE_FEATURES` order, one per field, e.g. `Self-reported diabetes (diab) is Yes.`. Each line has a short key in parentheses (`htn`, `diab`, `inc`, `cvd`, `age`, `rce`, `edu`, `pa`, `smkstat`, `sex`, `bmi`, `sodium`). |
| Prompt templates | `ckd/prompts.py` (`probability_prompt`, `decision_prompt`, `threshold_prompt`, `betting_prompt`, `cost_prompt`, `likert_prompt`, `probability_bin_prompt`, `task_specs`) | 16 task specs in collection order. Each prompt is one user message with no system prompt. |
| Rendered prompts | `build/prompts/ckd/<task_id>.jsonl` + `tasks.json` (copies in `data/release/prompts/ckd/`) | `prompt_id = original500__prompt_NNNN__<variant>` |
| Model calls | `collection/records.py`, `collection/bedrock.py`, `collection/openai.py`, `collection/materialize.py` | **Batch APIs only**: Bedrock Converse batch for the 7 open-weight models, OpenAI Responses batch for GPT-5.6 Luna and Terra. Record id = `sha256(task_id:prompt_id)[:32]`. No synchronous client and no response cache beyond the materialised files. |
| Cached responses | `data/release/responses/ckd/<task_id>/<model>.jsonl` | One response per (model, prompt): 500 × 16 × 9 = 72,000 records. |
| Parsing | `ckd/parse.py` (`parse_response(family, text)`, `parse_probability`, `parse_decision`, `parse_ordinal5`) | Strict, with no imputation. Decisions are read from the last `Choice:` line, matched by letter or by option phrase. Bins are matched by their exact percentage text. Writes `response_values.csv`, `paired_responses.csv` and `parse_diagnostics.csv`. |
| Complete-case matrix and split | `latent_beliefs/datasets.py` | One row per (model, case) where all 16 outputs parse. Seed `20260703 + 1009·(SPLIT_INDEX[model]+1)`. 20% test, then 20% of the remainder validation, then `ensure_comparable_holdout` moves cases so every output varies within each holdout. **The split is drawn separately per model** from that model's own complete cases, so case-to-split assignment differs between models. |
| Latent model | `latent/model.py` (`ProbabilisticLatentModel`, `fit_model`, `restore_cell`, `concordance_index`) | Prior over logit z: a 7-knot piecewise-linear log-density on a 128-point Gauss–Legendre grid. Probability report: Beta with mean z and fitted precision (this anchors the scale). Decisions: `a_j + b_j·logit z` with `b_j > 0`. Ordinals: ordered logit. |
| Output specs | `latent/domains.py::CKD_SPECS` | 1 probability, 13 binary, 2 ordinal, with dataset column names. |
| Fitting | `latent/fit.py` | Adam, lr 0.02, up to 30,000 epochs, patience 500, min_delta 1e-5, early stopping on validation NLL. Cell seed `20260811 + 10·domain + PIPELINE_POSITION`. The released fit (`data/release/fits/main`) was run on CUDA. Its `latent_posteriors.csv` holds **test cases only**, conditioned on all 16 outputs. |
| Evaluation (coherence) | `evaluation/reciprocal.py` (`condition_indices`, `reciprocal_predictions`, `target_metrics`, `summarize_metrics`) | Reports → decisions and decisions → reports on the test split. Gives mean c-index and pooled R² against the best test constant. **Its bootstrap resamples target prompts** (10,000 draws), not cases. |
| Other evaluations | `evaluation/leave_one_out.py`, `probability_sufficiency.py`, `outcome_sufficiency.py`, `holdout/*` | Not needed for the second-order arm. |

## 2. Models and domains with cached outputs

All nine models have complete cached outputs for all three domains (CKD,
Werewolf, forecasting): 500 cases × 16 outputs, one sample each. CKD summary
from the cache:

| Model | Provider / model id | Complete CKD cases | Parse failures (of 8,000) | Mean input / output tokens |
|---|---|---|---|---|
| Gemma 3 4B | Bedrock `google.gemma-3-4b-it` | 500 | 0 | 293 / 231 |
| Gemma 3 12B | Bedrock `google.gemma-3-12b-it` | 500 | 0 | 293 / 363 |
| Llama 3.1 70B | Bedrock `us.meta.llama3-1-70b-instruct-v1:0` | 479 | 21 | 270 / 376 |
| Gemma 3 27B | Bedrock `google.gemma-3-27b-it` | 500 | 0 | 293 / 306 |
| Qwen3 32B | Bedrock `qwen.qwen3-32b-v1:0` | 493 | 7 | 277 / 452 |
| DeepSeek V3.2 | Bedrock `deepseek.v3.2` | 486 | 14 | 257 / 480 |
| GLM-5 | Bedrock `zai.glm-5` | 499 | 1 | 261 / 414 |
| GPT-5.6 Luna | OpenAI `gpt-5.6-luna`, reasoning effort medium | 499 | 1 | 263 / 242 |
| GPT-5.6 Terra | OpenAI `gpt-5.6-terra`, reasoning effort medium | 500 | 0 | 263 / 186 |

Split sizes (train/val/test) come from `build/datasets/ckd.csv`. For example,
Gemma 3 4B is 313/84/103 and GPT-5.6 Terra is 320/80/100.

## 3. Baseline check (condition A)

Command: `python -m second_order.baseline_check --out-dir <dir>`. It re-parses
the cached responses, rebuilds the complete-case matrix, and re-runs
`evaluation.reciprocal` on CKD from the released main fit, writing into
`<dir>` without touching `build/`.

- The rebuilt `response_values.csv` and `datasets/ckd.csv` are byte-identical to `build/`.
- Reciprocal summary against the earlier run (`build/evaluation/reciprocal/model_summary.csv`, 7 Nov): 18 of 18 rows compared, **maximum absolute difference 0** on every metric and interval. They match to 3 decimals, and in fact exactly.
- Values: `docs/baseline_ckd.csv`. Intervals are the repo's 95% percentile intervals from resampling target prompts.

| Model | Reports → decisions: c-index | Reports → decisions: R² | Decisions → reports: c-index | Decisions → reports: R² |
|---|---|---|---|---|
| Gemma 3 4B | 0.753 [0.640, 0.861] | 0.018 [-0.001, 0.063] | 0.530 [0.474, 0.609] | -0.009 [-0.067, 0.160] |
| Gemma 3 12B | 0.936 [0.920, 0.953] | 0.416 [0.309, 0.490] | 0.864 [0.835, 0.921] | 0.479 [0.467, 0.639] |
| Llama 3.1 70B | 0.880 [0.834, 0.922] | 0.435 [0.315, 0.549] | 0.866 [0.840, 0.902] | 0.637 [0.485, 0.655] |
| Gemma 3 27B | 0.912 [0.894, 0.930] | 0.506 [0.446, 0.564] | 0.885 [0.849, 0.910] | 0.683 [0.658, 0.700] |
| Qwen3 32B | 0.908 [0.891, 0.924] | 0.452 [0.388, 0.503] | 0.864 [0.833, 0.897] | 0.568 [0.478, 0.611] |
| DeepSeek V3.2 | 0.970 [0.958, 0.979] | 0.708 [0.652, 0.758] | 0.918 [0.893, 0.945] | 0.784 [0.727, 0.841] |
| GLM-5 | 0.936 [0.925, 0.948] | 0.577 [0.519, 0.629] | 0.872 [0.848, 0.897] | 0.687 [0.664, 0.693] |
| GPT-5.6 Luna | 0.982 [0.972, 0.990] | 0.780 [0.721, 0.834] | 0.956 [0.911, 0.980] | 0.930 [0.877, 0.943] |
| GPT-5.6 Terra | 0.996 [0.992, 0.998] | 0.891 [0.855, 0.916] | 0.940 [0.884, 0.976] | 0.910 [0.855, 0.949] |

**Open item for Sam:** I compared against the 7 Nov `build/` evaluation, the
only earlier record of these numbers I found on this machine. If you recorded
the numbers somewhere else, point me to it and I will compare against that.

## 4. Repo settings that differ from, or add to, the plan's reference settings

| Setting | Plan | Repo (kept) |
|---|---|---|
| Prompt layout | C template puts the profile first, then the agent paragraph, then the instruction | Every first-order prompt is **question → "Provide your reasoning first" → answer format → options → `Patient profile:` block last**. Phase 2 has to decide where the agent paragraph goes. See §6. |
| Threshold wording | "screen if the probability of CKD is at least τ" | "The patient should be screened if the probability that they have CKD is at least {10%, 20%, 33.3%, 5%}." |
| Bet wording | "gains r points … loses 1" | A three-bullet payoff list, ending "would you recommend accepting this bet?" |
| Cost wording | "a miss costs r times a false alarm" | "the cost of a false negative … is r times the cost of a false positive …" |
| Plain decision | "Would … recommend screening" | "Decide whether this patient should be screened for CKD." Options: Recommend screening / Recommend not screening. |
| Generation | 16 outputs, T = 1 | T = 1, max 4,096 output tokens, no system prompt. OpenAI models use reasoning effort `medium`. No reasoning settings are passed to Bedrock models. |
| Seeds | Fixed seeds | **No sampling seed exists.** Bedrock and OpenAI batch calls at T = 1 take no seed, so a response is identified by (task_id, prompt_id, model) only. The "seed" in the new cache key can therefore only be a replicate index (0 for the main run, 1 for B′), not a reproducible RNG seed. |
| Fit | Max marginal likelihood, 128-point GL | Same, plus the Adam and early-stopping settings in §1. The repo does not record whether a fit hit the epoch limit. That has to be added in the new fit code. |
| Bootstrap | Paired case bootstrap, 2,000 draws | The first-order code resamples target prompts (10,000). The second-order intervals will use a case bootstrap as the plan says, so they are not directly comparable with §3. |
| Split | 20% test, then 20% of the rest validation | Same, but drawn per model and adjusted by `ensure_comparable_holdout` (§1). |

## 5. Open-weight models: how the cache was generated and what runs locally

All seven open-weight models' cached outputs were generated on **hosted**
Amazon Bedrock. None were generated locally.

This machine is an Apple M1 Pro with 16 GB unified memory, about 280 GB free
disk, and no CUDA. The earlier local activation work (`experiments/activation_pilot`, not in this branch)
already runs MLX locally. `mlx` 0.32.4 and `mlx-lm` 0.32.2 are installed in
`build/pilot_dependencies`, and residual capture with chunked prefill is
implemented in `experiments/werewolf_beliefs/extract.py::forward` (local only).

| Model | Local weights on this machine | Runnable here with activations |
|---|---|---|
| Gemma 3 4B | `build/activation_pilot/model`: `mlx-community/gemma-3-4b-it-4bit` @ `93724907…`, 34 layers | Yes (used already). 4-bit, so not the hosted checkpoint. |
| Gemma 3 12B | `build/activation_pilot_12b/model`: `mlx-community/gemma-3-12b-it-4bit` @ `86cc6a8d…`, 48 layers, hidden size 3,840 | Yes (used already). 4-bit. |
| Gemma 3 27B | none | Probably not in 16 GB: 4-bit weights are about 15–16 GB. Needs a bigger machine or GPU. |
| Qwen3 32B | none | No (memory). |
| Llama 3.1 70B | none | No (memory). |
| DeepSeek V3.2, GLM-5 | none | No (hundreds of billions of parameters). |

So on this machine Phase 4 can cover the two Gemma sizes, with the 4-bit
checkpoint caveat already recorded in the activation pilot README. The
fidelity check (local log-probability of cached hosted responses) is new and
not implemented in the existing pilot.

**Credentials:** no AWS or OpenAI credentials are visible in this shell (no
`AWS_*` or `OPENAI_API_KEY` environment variables, no `~/.aws`). Phases 1, 3
and 6 need new generation calls, so they cannot start until access is set up.

## 6. What must change so a `condition` field passes through

Everything below goes in new modules under `second_order/`. The first-order
functions are imported and called, not edited.

1. **Profile ablation.** `patient_profile` has no field filter. The new code
   renders the same lines by reusing `FEATURE_TEXT`, `FEATURE_KEYS`,
   `format_feature_value` and `FEATURE_UNITS`, dropping withheld lines
   entirely. The full-field render must be byte-identical to `patient_profile`
   (to be tested).
2. **Prompt builders.** The builders take only `profile` (plus their
   parameter) and hard-code "you"-directed wording, so they cannot be
   redirected by argument. Condition B reuses them directly with the partial
   profile. C and D need parallel builders that keep the same answer-format
   and option strings (so `parse_response` applies unchanged) and change only
   the question sentence and the added agent paragraph. Decision for Phase 2:
   the repo puts the profile last, so either (a) follow the plan's order
   (profile → agent paragraph → question → format), which changes layout
   relative to A/B, or (b) keep the repo's order and put the agent paragraph
   after the profile block. (b) means the question mentions John before he is
   introduced. I'll render both for stop point 3 unless you pick one first.
3. **Identifiers.** `prompt_id` and `task_id` must carry the condition, e.g.
   `ckd_so_<condition>_<variant>` and `original500__prompt_NNNN__<condition>__<variant>`,
   so record ids and parsed rows never collide with condition A. The new
   response cache key is `sha256(model, case, condition, output, replicate)`.
4. **Generation.** The repo only has batch collection with no response cache
   beyond the materialised files. A new `run_generation` module needs a
   per-item cache keyed as above, a call-count and cost estimate with an
   approval gate above 1,000 calls, and either a synchronous client or a
   wrapper around the existing batch prepare/submit/download steps. Settings
   are copied from `collection/records.py` (T = 1, 4,096 tokens) and
   `collection/openai.py` (reasoning effort medium).
5. **Parsing.** `parse_response(family, text)` is reused as is. The
   per-condition table needs a `condition` column, and complete-case status is
   computed per (model, condition, case).
6. **Split.** `datasets.build` would redraw a split from each condition's
   complete cases. For B, C and D, the new code must instead **reuse each
   model's condition A split assignment** from `build/datasets/ckd.csv`.
   Cases complete in A determine split membership. The analysis set is cases
   complete in all six conditions.
7. **Fitting.** `ProbabilisticLatentModel` has no condition-specific
   parameters, so a pooled fit (A + B, C-John + D-John, C-user + D-user) is
   the same model fitted to stacked rows. Item parameters and the 7-knot prior
   are shared, trained on train-split rows, with early stopping on validation
   rows. Posteriors are needed for **all cases** (the repo exports test only)
   and for output subsets: all 16, the 3 reports, the 13 decisions, plain +
   threshold (5), and bet + cost (8). `posterior_log_weights(...,
   include_outputs=...)` already supports this. Condition A must be refit
   within fit 1 rather than taken from the released fit. The released fit
   stays the baseline for §3.
8. **Evaluation.** `reciprocal_predictions` and `target_metrics` take any
   restored model and test matrix, so coherence per condition can call them
   unchanged. Case-bootstrap intervals and the A − D and D − C paired
   differences are new code.
9. **Dependencies.** The plan's outputs are `.parquet`, but neither the
   environment nor `build/pilot_dependencies` has `pyarrow`. Either I install
   it into `build/pilot_dependencies` (the pattern the activation pilot uses,
   which leaves the pinned environment unchanged), or the outputs are CSV.

## 7. Decisions needed before Phase 1

- Confirm the baseline reference (§3).
- Model access for generation: Bedrock (bucket and role, batch only) and
  OpenAI keys, or another route. Also confirm the pilot models: the plan's
  strong + mid-range, e.g. GPT-5.6 Terra and Qwen3 32B or Gemma 3 27B.
- Parquet via `pyarrow` in `build/pilot_dependencies`, or CSV.
- Prompt layout for C and D (§6, item 2). This can wait for stop point 3.
