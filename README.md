# Second-order beliefs in language models

When a language model says what *another agent* believes, is it reporting a
separate belief built from that agent's evidence, or its own belief under
another name? This repository measures that, extending the latent-belief
method of Smolin and Wilder, *Beliefs and Behavior in Language Models*
([arXiv:2609.07943](https://arxiv.org/abs/2609.07943)).

It contains three things:

- **`second_order/`**: the second-order belief experiment (work in progress).
- **`latent_beliefs/`**: the original paper's replication package, unchanged.
  Its documentation is in [`docs/REPLICATION.md`](docs/REPLICATION.md).
- **`model_beliefs/`, `scripts/`, `datasets/`**: first-order belief
  readouts on small models (stated, decision-based and probe-based), with
  model organisms. See
  [the model_beliefs toolkit](#the-model_beliefs-toolkit-first-order-readouts-on-small-models) below.

## The current study: the curse of knowledge in Qwen3 8B and 14B

How much of the model's private evidence leaks into the belief it attributes
to someone who lacks that evidence? The comparison point is Camerer,
Loewenstein and Weber (1989), who find a weight w ≈ 0.51 for people.

A patient's chart is shown to the model. John is a physician described as
having the same training and judgement as the model; he holds a different
copy of the chart.

| We ask the model | Condition | Example |
| --- | --- | --- |
| Its own view, from the real chart | A | 30% chance of kidney disease |
| Its own view, from John's copy | B | 12% |
| What John thinks; John has the real chart too | D | 28% |
| What John thinks; the model sees only John's copy | M | 13% |
| What John thinks; the model sees the real chart, John has his copy | C | 25% |

**Leakage λ** is where C sits between M and D: 0 means the model fully took
John's perspective, 1 means it gave its own belief as John's. Here λ ≈ 0.84.

![What λ, φ and τ measure](docs/figures/curse_schematic.png)

Across cases, with x = l_A − l_B in log-odds:

- λ = β_C / β_D, the slopes of l_C − l_M and l_D − l_M on x;
- ρ = Cov(x, x′) / Var(x), the reliability, from a second run of A and B;
- φ = β_D / ρ, how strongly the attributed belief follows the evidence;
- τ = φ(1 − λ), a theory-of-mind score.

λ is estimated in two settings:

- **partial evidence**: John's copy lacks three fields (pair `S4`);
- **false belief**: a colleague read John one line wrongly (pair `F`). This
  setting has its own D_F, so M_F and D_F share one template.

Each setting runs on two datasets:

- **CKD**: 500 chronic-kidney-disease cases × 16 outputs. Beliefs come from
  the paper's latent fit and from the stated probability.
- **A bespoke dataset**: fictional items with known Bayes answers. It uses
  the stated probability only, and adds placebo items and comprehension
  follow-ups ([`docs/BESPOKE.md`](docs/BESPOKE.md)).

The commands are in the
[runbook](docs/RUNBOOK.md#curse-of-knowledge-experiment-qwen3-8b-and-14b), and
the design and its checks are in the
[implementation notes](docs/IMPLEMENTATION_NOTES.md#the-curse-of-knowledge-experiment).

## Status

| Part | State |
| --- | --- |
| Conditions and prompts, CKD and bespoke (tested) | Built |
| Generation with caching, resume, second runs, samples, comprehension turns and a cost gate; vLLM, transformers, synthetic, Bedrock and OpenAI backends | Built |
| Latent fits, calibration and the shared belief format | Built |
| Estimators λ, ρ, φ, τ, the assumption checks and the case bootstrap (tested) | Built |
| `second_order.curse` report and figures | Built |
| One-command runner: setup, inference tuning and smoke test, generation, frozen activations, fits, report, tarball | Built; run end to end on the synthetic model (CPU); GPU stages not yet run |
| End-to-end recovery of the planted parameters on the synthetic backend | Passing (`tests.test_pipeline`) |
| Data collection for Qwen3 8B and 14B | Not started (needs a CUDA machine) |

No experimental results are in this repository yet.

## Quick start

```bash
conda env create -f environment.yml     # or environment-portable.yml off macOS arm64
conda activate latent-beliefs
pip install pyarrow pyyaml              # second_order/; on a CUDA machine also: pip install vllm
```

The second-order code reads the replication results bundle (cases, prompts
and bin edges). Download it from Zenodo
(<https://doi.org/10.5281/zenodo.23350428>) and extract it here, which creates
`data/release/`. Put the bespoke item file at `data/bespoke/items.yaml`.

Checks that make no model calls:

```bash
python -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke tests.test_generation \
    tests.test_runner
python -m unittest tests.test_pipeline          # end to end on the synthetic backend (a few minutes)
python -m second_order.bespoke                  # check the bespoke item file
python -m second_order.example_prompts          # writes docs/example_prompts.md
```

The experiment (each run prints its call count and stops for approval above
1,000 calls):

```bash
python -m second_order.run_generation --model qwen3_8b --approve   # CKD and bespoke, then comprehension turns
python -m second_order.phase3 --model qwen3_8b                     # parse, latent fits, beliefs
python -m second_order.curse                                       # outputs/curse/*.csv, outputs/curse/RESULTS.md
python -m second_order.figures                                     # figures/curse_*
bash run_experiment.sh                                             # or all of the above, end to end (below)
```

## Running everything on a GPU instance

`run_experiment.sh` does the whole study in one command on a fresh Linux GPU
machine (e.g. vast.ai), then packages it:

```bash
git clone https://github.com/mangoseller/model_beliefs && cd model_beliefs
# put the bespoke item file at data/bespoke/items.yaml (else a placeholder stands in)
nohup bash run_experiment.sh > run.out 2>&1 &     # or --smoke first: 20 cases, 3 samples
```

1. It installs `uv`, a Python 3.11 environment and the pinned dependencies
   (`requirements/`, including vLLM). It also installs the Claude Code and
   Codex CLIs into `~/.local/bin`, adding Node.js if needed, and puts them on
   `PATH`, including in `~/.bashrc`. Log in with `claude` or `codex`, or set
   `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. `INSTALL_AGENTS=0` skips this.
2. It downloads the Zenodo bundle and the Qwen3 8B and 14B weights, and pins
   their exact Hugging Face snapshots.
3. It runs the unit tests and reproduces the original paper.
4. It **tunes inference and smoke-tests the model**. A sample of about 1,000
   real calls runs under a few vLLM engine settings (concurrent sequences,
   token budget, KV-cache memory, prefix caching), and the fastest is kept.
   The run stops here if the answers don't parse or are truncated.
5. It generates every condition, run, sample and comprehension follow-up,
   with a progress bar. Each model's latent fits start in the background on
   CPU, and meanwhile the GPU **freezes residual-stream activations**: every
   layer at three positions (end of the evidence, end of the prompt, just
   before the answer) for every numeric-probability answer. That's about
   4 GB for 8B and 6 GB for 14B.
6. It analyses the results and draws every figure.
7. It writes `runs/<run_id>/RUN_REPORT.md` and `record/`: every constant, the
   estimator as implemented, model revisions, tuning trials, hardware, data
   checksums, usage and example prompts.
8. It ends with `runs/<run_id>.tar.gz`, plus `runs/<run_id>_activations.tar`
   for the activations.

The console (`run.out`) shows `[stage n/11]` lines, progress bars for
downloads, inference and freezing, background-fit status every 5 minutes,
and a summary of λ, φ and τ at the end.

`--run-id <run_id>` resumes an interrupted run without regenerating anything.
Expect roughly 120 GB of disk. A 48 GB GPU runs Qwen3 14B in bf16; 24 GB is
enough for 8B alone. Options: `bash run_experiment.sh --help`. Without a GPU,
`CPU_ONLY=1 bash run_experiment.sh --models synthetic --smoke` runs the same
pipeline on the synthetic model.

The figures (`second_order.figures`):

- **The decomposition:** `curse_schematic` and `curse_decomposition` (the
  diagram above, with each run's estimates).
- **Headline:** `curse_estimates` (λ, φ, τ with Camerer et al.'s w).
- **After the paper's Figures 3 and 5:** `curse_decision_curves` and
  `curse_thresholds`.
- **Assumption checks:**
  - `curse_scatter` and `curse_linearity` (the regressions and their linearity);
  - `curse_case_leakage` (per-case leakage);
  - `curse_comprehension` (λ with and without comprehension failures);
  - `curse_bayes` (bespoke answers against Bayes).
- **Data quality:** `retention`.

## Reproducing the original paper

`./reproduce.sh analysis` rebuilds every figure and table of Smolin and
Wilder from the released bundle in about five minutes on CPU
([`docs/REPLICATION.md`](docs/REPLICATION.md)). The second-order work never
modifies `latent_beliefs/` or `reproduce.sh`.

Checked against `docs/baseline_ckd.csv`, the CKD reports↔decisions numbers
recorded from the paper:

| Metric | Match in the portable environment |
| --- | --- |
| Pooled R² | Exact |
| Mean c-index | Within 0.004 |
| C-index interval bounds | Within 0.009 |

The paper's exact values need the pinned macOS build: off that build, the
replication notes expect c-indices to differ by up to about 0.01, because
rounding flips a few tied predictions.

## Layout

```
configs/second_order.yaml    models, settings, datasets, schedule, seeds, budget
second_order/                the experiment (see STRUCTURE.md)
  conditions.py                the condition table: roles A, B, C, M, D; pairs S4, F (and S1, H)
  prompts_second_order.py      CKD prompts for every condition, comprehension questions
  bespoke.py                   the bespoke dataset: item file, Bayes answers, prompts, parsing
  datasets.py                  CKD and bespoke behind one interface
  comprehension.py             comprehension follow-up questions and their parser
  run_generation.py            cached, resumable generation (all backends)
  synthetic.py                 text-level simulator with known parameters
  responses.py, phase3.py      parsing, matrices, retention, comprehension scoring
  fit_pooled.py, measurement.py  latent fits and shrinkage calibration
  beliefs.py                   the shared belief format
  leakage.py                   λ (= β_C/β_D), ρ, φ, τ and their checks, with the case bootstrap
  curse.py, figures.py, report.py  tables, RESULTS.md and figures
  runner.py, runner_record.py  the one-command run and its record (run_experiment.sh)
  tune.py, freeze.py           inference tuning and smoke test; frozen activations
  deprecated/                  the earlier RQ1-RQ3 design (see below)
run_experiment.sh            bootstrap for a fresh GPU instance; requirements/ pins its packages
tests/                       prompt, parser, estimator, cache, runner and end-to-end tests
docs/                        runbook, implementation notes, bespoke schema, orientation, example prompts
latent_beliefs/              the replication package (unchanged)
model_beliefs/, scripts/, datasets/   first-order readouts on small models (see below)
outputs/                     second_order tables and reports (git-ignored)
runs/                        one folder and tarball per run_experiment.sh run (git-ignored)
results/                     write-ups of runs (tracked)
```

## Deprecated: the earlier RQ1–RQ3 design

`second_order/deprecated/` holds the earlier design of this experiment. It
asked three questions on the paper's nine hosted models:

- **RQ1, separation:** λ across models, pairs and framings.
- **RQ2, coherence:** whether the 16 answers about John agree, and how his
  belief moves along an evidence ladder.
- **RQ3, mechanism:** internal readouts on open-weight models.

The folder contains:

- `analyze`, `coherence` and `rq_figures`;
- `activations` and `probes`;
- the Phase 1 `pilot`, which chose the withheld set S4;
- `simulate` and `estimator_bias_check`.

It still runs (`python -m second_order.deprecated.<module>`, commands in the
runbook), and `tests.test_pipeline` exercises its analysis. Nothing in the
current study imports it.

## Documentation

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): commands for every stage.
- [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md): design
  choices, the estimators and what has been validated.
- [`docs/BESPOKE.md`](docs/BESPOKE.md): the bespoke item file format.
- [`docs/ORIENTATION.md`](docs/ORIENTATION.md): how the first-order code is
  organised and the baseline it reproduces.
- [`docs/example_prompts.md`](docs/example_prompts.md): rendered CKD prompts
  for every condition.
- [`STRUCTURE.md`](STRUCTURE.md): a map of the packages.
- [`docs/REPLICATION.md`](docs/REPLICATION.md): reproducing the original
  paper's figures and tables.

## The `model_beliefs` toolkit: first-order readouts on small models

Extracting model beliefs via direct questioning, decision inference, and residual-stream
directions; validated against model organisms with known beliefs.

Three readouts of what a small model believes about a yes/no unknown `y`:

1. **Ask it**: its stated probability, Likert answer and probability bin.
2. **Infer it from its decisions**: the latent belief `z` of Smolin & Wilder (2026), *Beliefs and
   Behavior in Language Models*, [arXiv:2609.07943](https://arxiv.org/abs/2609.07943).
3. **Read it from inside the model**: a linear probe on the residual stream trained to match `z`.
   (Not built yet.)

### Layout

```
model_beliefs/questions.py         builds the paper's 16 prompts from a scenario file
datasets/                          one scenario JSON per domain (format: datasets/README.md)
  ckd.json                         chronic kidney disease screening (paper wording, pilot cases)
  werewolf.json                    is the target player a werewolf (paper wording, pilot cases)
  insecure_code.json               our own case (draft): does this code have a vulnerability
scripts/run_model_on_question.py   runs a model on the 16 questions, saves answers + activations
scripts/extract_model_belief.py    fits the paper's latent belief model, writes belief readouts
scripts/control_model_organism.py  (todo) checks readouts against model organisms with known beliefs
data/                              run outputs (git-ignored, large)
results/                           write-ups of runs
```

### Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

Runs on an Apple-silicon Mac (MPS), on CUDA, or on CPU.

### Run

```bash
# 1. answers + activations (default model Qwen/Qwen2.5-1.5B-Instruct)
.venv/bin/python scripts/run_model_on_question.py --scenario datasets/ckd.json
.venv/bin/python scripts/run_model_on_question.py --scenario datasets/ckd.json --flip-options   # letter-bias control

# ICL: plant a belief in the system prompt
.venv/bin/python scripts/run_model_on_question.py --scenario datasets/ckd.json \
    --system "You are certain this patient has CKD." --tag icl_yes

# model organism given as a LoRA adapter
.venv/bin/python scripts/run_model_on_question.py --scenario datasets/insecure_code.json \
    --model <base> --adapter <adapter>

# 2. belief readouts
.venv/bin/python scripts/extract_model_belief.py data/ckd__Qwen2.5-1.5B-Instruct
```

Useful flags: `--n-cases N` (quick test), `--no-reasoning` (answer only, much faster),
`--flip-options` (reverse option order, answers mapped back), `--samples K`,
`--max-new-tokens`, `--no-activations`.

**Always run decisions in both option orders.** Small models have a strong letter bias; see
`results/pilot_2026-11-04.md`.

### What gets saved

`data/<domain>__<model>[__tag][__noreason][__flip]/`
- `responses.jsonl`: one row per case × question × sample. Holds the prompt, the reply, the parsed
  answer (`answer`, `choice`, `action`), and `option_probs` (the model's probability on each option
  letter, read right after `Choice:`).
- `activations/<case>.npz`: float16 arrays `[rows, layers+1, d_model]` at `context_end` (end of the
  case details), `prompt_end` (end of the prompt) and `answer` (just before the answer, after the
  model's reasoning). Row k matches the k-th row for that case in `responses.jsonl`.
- `beliefs.csv` / `fit.json` (from `extract_model_belief.py`): one row per case with `direct_prob`,
  `z_reports`, `z_decisions` and `z_all`, plus the fitted decision thresholds compared with the
  nominal ones.

### Differences from the paper (so far)

- The cases are small hand-written pilots. The paper uses 500 per domain, from NHANES (kidney) and
  the Kaggle Werewolf dataset (needs a login).
- With fewer than 30 cases, `extract_model_belief.py` fits everything in-sample and says so.
- `option_probs`, `--flip-options` and the activations are our additions.
- Sampling: temperature 1 as in the paper, but we keep the model's own top-p/top-k by default.
  Without them, Qwen 0.5B/1.5B drift into gibberish at temperature 1. Use `--pure-sampling` for
  the paper's exact setting.
- Speed on an M4 Mac with Qwen2.5-1.5B: 11+ minutes per case with reasoning, about 1 minute with
  `--no-reasoning`.

## License and citation

The code is released under the MIT License ([`LICENSE`](LICENSE)). The
replication package and its results bundle are the work of the original
authors; if you use them, cite the paper (see [`CITATION.cff`](CITATION.cff)
and [`docs/REPLICATION.md`](docs/REPLICATION.md) for data attributions).
