# Second-order beliefs in language models

When a language model says what *another agent* believes, does it reason from that agent's
evidence, or report its own belief under another name? This repository measures how much of a
model's private evidence leaks into the beliefs it attributes to others. It extends the
latent-belief method of Smolin and Wilder, *Beliefs and Behavior in Language Models*
([arXiv:2609.07943](https://arxiv.org/abs/2609.07943)).

| Folder | Contents |
| --- | --- |
| `second_order/` | The experiment: the curse of knowledge in Qwen3 8B and 14B |
| `latent_beliefs/` | The paper's replication package, unchanged ([`docs/REPLICATION.md`](docs/REPLICATION.md)) |
| `model_beliefs/`, `scripts/`, `datasets/` | First-order belief readouts on small models ([`model_beliefs/README.md`](model_beliefs/README.md)) |

## The experiment

A patient's chart is shown to the model. John, a physician with the same training and
judgement as the model, holds a different copy.

| We ask the model | Condition | Example |
| --- | --- | --- |
| Its own view, from the real chart | A | 30% chance of kidney disease |
| Its own view, from John's copy | B | 12% |
| What John thinks; John has the real chart too | D | 28% |
| What John thinks; the model sees only John's copy | M | 13% |
| What John thinks; the model sees the real chart, John has his copy | C | 25% |

**Leakage λ** is where C sits between M and D, in log-odds: 0 means the model fully takes John's
perspective, 1 means it gives its own belief as John's. Here λ ≈ 0.84; for people, Camerer,
Loewenstein and Weber (1989) find about 0.51.

![What λ, φ and τ measure](docs/figures/curse_schematic.png)

Across cases, with x = l_A − l_B:

- λ = β_C / β_D, the slopes of l_C − l_M and l_D − l_M on x;
- ρ = Cov(x, x′) / Var(x), the reliability of x, from a second run of A and B;
- φ = β_D / ρ, how strongly John's belief follows the evidence;
- τ = φ(1 − λ), a theory-of-mind score.

Two settings: **partial evidence** (John's copy lacks three fields) and **false belief** (a
colleague read John one line wrongly). Two datasets:

- **CKD**: 500 kidney-disease cases × 16 outputs, with beliefs from the paper's latent fit and
  from the stated probability.
- **A bespoke dataset**: fictional items with known Bayes answers, placebo items and
  comprehension checks ([`docs/BESPOKE.md`](docs/BESPOKE.md)).

The design, estimators and assumption checks are in
[`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md).

## Running it

On a fresh Linux GPU machine (e.g. vast.ai):

```bash
git clone https://github.com/mangoseller/model_beliefs && cd model_beliefs
# optional: the bespoke item file at data/bespoke/items.yaml (else a placeholder stands in)
bash run_experiment.sh --smoke                    # a quick full check first: 20 cases
nohup bash run_experiment.sh > run.out 2>&1 &     # then everything
```

`run_experiment.sh` installs the pinned environment (`requirements/`) and the Claude Code and
Codex CLIs. It downloads the data and the model weights, reproduces the original paper, then
for each model:

1. tunes vLLM and smoke-tests the answers;
2. runs every condition and follow-up;
3. freezes residual-stream activations;
4. fits, analyses and draws the figures.

Everything lands in `runs/<run_id>/`, which is packaged as `runs/<run_id>.tar.gz` (activations in
`runs/<run_id>_activations.tar`). Open `RUN_REPORT.md` first: it has the results, the figures and
every setting.

- `--run-id <run_id>` resumes a run without regenerating anything.
- Expect about 120 GB of disk. Qwen3 14B needs a GPU with 40 GB or more; 24 GB is enough for 8B.
- `CPU_ONLY=1 bash run_experiment.sh --models synthetic --smoke` runs the same pipeline without
  a GPU, on a simulator with known answers.

[`docs/RUNBOOK.md`](docs/RUNBOOK.md) covers each stage, the individual commands and the figures.

## Tests

```bash
python -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke \
    tests.test_generation tests.test_runner      # about a minute
python -m unittest tests.test_pipeline           # end to end on the synthetic backend, a few minutes
```

The tests read the paper's results bundle from `data/release/`
([Zenodo](https://doi.org/10.5281/zenodo.23350428)).

## Reproducing the original paper

`./reproduce.sh analysis` rebuilds the paper's figures and tables from the released bundle in
about five minutes on CPU; the runner does this in every run. Against the paper's CKD
reports↔decisions numbers (`docs/baseline_ckd.csv`):
- pooled R² matches exactly;
- c-indices match to within 0.009, the platform tolerance noted in
  [`docs/REPLICATION.md`](docs/REPLICATION.md) for runs outside the pinned macOS build.

## Layout

```
second_order/        the experiment (module map: STRUCTURE.md)
  deprecated/        the earlier RQ1–RQ3 design across nine hosted models, still runnable
latent_beliefs/      the paper's replication package
configs/             configs/second_order.yaml: every setting of the experiment
tests/               unit and end-to-end tests
docs/                runbook, implementation notes, bespoke schema, example prompts
run_experiment.sh    one-command setup and run; requirements/ pins its packages
outputs/, runs/      results of local commands and of full runs (git-ignored)
results/             write-ups of model_beliefs runs
```

## License and citation

The code is released under the MIT License ([`LICENSE`](LICENSE)). The replication package and
its results bundle are the work of the original authors; if you use them, cite the paper (see
[`CITATION.cff`](CITATION.cff) and [`docs/REPLICATION.md`](docs/REPLICATION.md)).
