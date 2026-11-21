# Running

## Everything in one command

On a Linux machine with an NVIDIA GPU (48 GB fits Qwen3 14B in bf16; 24 GB is enough
for 8B alone) and about 120 GB of free disk:

```bash
bash run_experiment.sh --smoke                    # 20 CKD cases, 3 samples, short fits
nohup bash run_experiment.sh > run.out 2>&1 &     # the full run
bash run_experiment.sh --run-id <run_id>          # resume; cached responses are never regenerated
CPU_ONLY=1 bash run_experiment.sh --models synthetic --smoke   # the same pipeline without a GPU
```

`run_experiment.sh` installs the pinned environment (`requirements/lock-cuda.txt`) and
the Claude Code and Codex CLIs. It then runs `second_order/runner.py`, whose stages are:

| Stage | What it does |
|---|---|
| `fetch_data` | Downloads the paper's results bundle from Zenodo and checks it. Resolves the bespoke items (`datasets/bespoke_items.yaml`, else the example items as a marked placeholder) |
| `fetch_models` | Downloads each model and pins its snapshot hash in the run's config |
| `checks` | Unit tests, the bespoke item checks, GPU and disk |
| `paper` | `latent_beliefs/reproduce.sh analysis`, compared with the paper's CKD numbers |
| `tune` | Times a few vLLM engine settings on real calls and keeps the fastest. Stops the run if answers don't parse or are truncated |
| `generate` | Every condition, run, sample and follow-up, with a progress bar. Each model's fits start in the background as soon as its generation ends |
| `activations` | Residual-stream activations (every layer) for the numeric-probability answers |
| `fits` | Waits for the background latent fits |
| `analyze` | `second_order.curse` (tables, `RESULTS.md`) and `second_order.figures` |
| `report` | `RUN_REPORT.md` and `record/`: every constant, the estimator, usage, example prompts |
| `package` | `runs/<run_id>.tar.gz`; the activations go in `runs/<run_id>_activations.tar` |

Everything a run writes is under `runs/<run_id>/`. A failed stage is recorded, and
later stages are skipped (`--continue-on-error` overrides this). `fits`, `analyze`,
`report` and `package` always run. `bash run_experiment.sh --help` lists every option.

## Step by step

```bash
python -m second_order.bespoke                          # check the bespoke items: Bayes answers, warnings
python -m second_order.run_generation --model qwen3_8b  # both datasets, then the comprehension turns
python -m second_order.phase3 --model qwen3_8b          # parse, latent fits, beliefs, retention
python -m second_order.curse                            # outputs/curse/*.csv and RESULTS.md
python -m second_order.figures                          # figures/
```

`SO_CONFIG`, `SO_DATA_DIR`, `SO_RESULTS_DIR`, `SO_FIGURES_DIR` and `SO_BESPOKE_ITEMS`
redirect a run (`second_order/config.py`).

## Calls per model

| Part | Calls |
|---|---|
| CKD: 9 conditions × 500 cases × 16 outputs | 72,000 |
| CKD: second runs of A, B, B_F | 24,000 |
| CKD: comprehension (C, C_F) | 1,000 |
| Bespoke: 20 items × 20 samples × 12 runs | 4,800 |
| Bespoke: comprehension | 800 |

Sampling is at temperature 1, with at most 4,096 output tokens, thinking off and no
system prompt.

## The generation cache

Responses are cached in `data/generation_cache/<model>.jsonl`. Each item is keyed by
`sha256([model, case, condition, output, replicate(, sample)])`. Reruns skip cached
items. A cached record is refused if its prompt, or its generation settings (sampling,
chat template, weights), differ from the current ones.

| Backend | Use |
|---|---|
| `vllm` | Qwen3 8B and 14B on CUDA; one seed per item |
| `hf` | transformers fallback (`qwen3_8b_hf`), and `qwen3_0_6b_hf` for a CPU check of the chat template and parsing |
| `synthetic` | text written from known λ, φ and noise; `tests.test_pipeline` runs it |

## Figures

| Figure | Shows |
|---|---|
| `curse_schematic` | What λ, φ and τ measure |
| `curse_decomposition` | The same diagram with each setting's estimates |
| `curse_estimates` | λ and τ per setting and the shared φ, against Camerer et al.'s w |
| `curse_scatter`, `curse_linearity` | The two regressions behind λ, and their binned means |
| `curse_case_leakage` | Per-case (ℓ_C − ℓ_M)/(ℓ_D − ℓ_M) |
| `curse_comprehension` | λ on all answers and on comprehension-passing answers |
| `curse_decision_curves` | The screening decision against the latent belief (after the paper's Figure 3) |
| `curse_thresholds` | Implied against instructed thresholds, own fit and PERSON's (after Figure 5) |
| `curse_bayes` | Bespoke answers against the Bayes answer of the believer's evidence |
| `retention` | Complete-call retention by condition |

## Tests

```bash
python -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke \
    tests.test_generation tests.test_runner
python -m unittest tests.test_pipeline                  # end to end on the synthetic backend (minutes)
```
