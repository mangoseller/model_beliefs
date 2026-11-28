# model_beliefs (first-order toolkit)

Run the commands below from this folder (`cd archive/first_order_toolkit`).

Readouts of what a model believes about a yes/no unknown `y`, following Smolin & Wilder (2026),
*Beliefs and Behavior in Language Models*, [arXiv:2609.07943](https://arxiv.org/abs/2609.07943):

1. **Ask it**: its stated probability, Likert answer and probability bin.
2. **Infer it from its decisions**: the paper's latent belief `z`, fitted jointly from the 13
   decisions and 3 probability reports asked for each case.

`run_model_on_question.py` also saves residual-stream activations at fixed token positions, as
inputs for a linear probe trained against `z`.

## Layout

```
model_beliefs/questions.py          builds the paper's 16 prompts from a scenario file
../../datasets/first_order/            scenario JSON files and their format guide (README.md)
  ckd.json                          chronic kidney disease screening: paper wording, 8 hand-written profiles (no labels)
  ckd_nhanes.json                   same wording, 500 NHANES 2017-2018 adults
  werewolf.json                     is the target player a werewolf: paper wording, 4 short hand-written games
  werewolf_games.json               same wording, 500 cuts of Kaggle Werewolf replays
  insecure_code.json                does this code have a vulnerability: 8 hand-written snippets
  insecure_code_em.json             same wording, 500 snippets from the Emergent Misalignment data
scripts/run_model_on_question.py    runs a model on the 16 questions, saves answers + activations
scripts/extract_model_belief.py     fits the paper's latent belief model, writes belief readouts
scripts/build_ckd_dataset.py        builds ckd_nhanes.json (downloads NHANES)
scripts/build_werewolf_dataset.py   builds werewolf_games.json (downloads Kaggle replays)
scripts/build_code_dataset.py       builds insecure_code_em.json (downloads the EM data)
data/                               run outputs and raw downloads (git-ignored)
```

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

Runs on CUDA, Apple-silicon MPS or CPU.

## Run

```bash
# 1. answers + activations (default model Qwen/Qwen2.5-1.5B-Instruct)
.venv/bin/python scripts/run_model_on_question.py --scenario ../../datasets/first_order/ckd.json

# in-context belief: plant a belief in the system prompt
.venv/bin/python scripts/run_model_on_question.py --scenario ../../datasets/first_order/ckd.json \
    --system "You are certain this patient has CKD." --tag icl_yes

# model organism given as a LoRA adapter
.venv/bin/python scripts/run_model_on_question.py --scenario ../../datasets/first_order/insecure_code.json \
    --model <base> --adapter <adapter>

# 2. belief readouts
.venv/bin/python scripts/extract_model_belief.py data/ckd__Qwen2.5-1.5B-Instruct
```

Useful flags: `--n-cases N` (quick test), `--no-reasoning` (answer only, much faster),
`--orders both|normal|flip`, `--samples K`, `--max-new-tokens`, `--no-activations`.
`extract_model_belief.py --soft` fits on the option probabilities instead of the sampled letters,
and `--pool` fits one model across several runs.

**Run decisions in both option orders** (the default, `--orders both`). Small models have a strong
bias towards particular option letters; asking each question again with the options reversed
separates that bias from the belief. Answers are always saved in the original option order.

## What gets saved

`data/<domain>__<model>[__tag][__noreason][__normal|__flip]/`
- `meta.json`: run settings, model info and the question list.
- `responses.jsonl`: one row per case × option order × question × sample. Holds the prompt, the
  reply, the parsed answer (`answer`, `choice`, `action`), and `option_probs` (the model's
  probability on each option letter, read right after `Choice:`).
- `activations/<case>.npz`: float16 arrays `[rows, layers+1, d_model]` at `context_end` (end of the
  case details), `prompt_end` (end of the prompt) and `answer` (just before the answer, after the
  model's reasoning). Row k matches the k-th row for that case in `responses.jsonl`.
- `beliefs.csv` / `fit.json` (from `extract_model_belief.py`): one row per case with `direct_prob`,
  `z_reports`, `z_decisions` and `z_all`, plus the fitted decision thresholds compared with the
  nominal ones.

An interrupted run resumes: cases already in `responses.jsonl` are skipped.

## Differences from the paper

- With fewer than 30 cases, `extract_model_belief.py` fits everything in-sample and says so.
- `option_probs`, the option-order control and the activations are additions.
- Sampling uses temperature 1 as in the paper but keeps the model's own top-p/top-k by default:
  without them, small models (Qwen2.5 0.5B/1.5B) drift into gibberish at temperature 1. Use
  `--pure-sampling` for the paper's exact setting.
- With reasoning, generation is slow on long contexts; `--no-reasoning` is many times faster.
