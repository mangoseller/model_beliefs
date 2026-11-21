# Second-order beliefs in language models

When a language model says what *another agent* believes, is it reporting a
separate belief built from that agent's evidence, or its own belief under
another name? This repository measures that, extending the latent-belief
method of Smolin and Wilder, *Beliefs and Behavior in Language Models*
([arXiv:2609.07943](https://arxiv.org/abs/2609.07943)).

It contains two things:

- **`second_order/`**: the second-order belief experiment (work in progress).
- **`latent_beliefs/`**: the original paper's replication package, unchanged.
  Its documentation is in [`docs/REPLICATION.md`](docs/REPLICATION.md).

## The experiment in one example

A patient's chart is shown to the model. John, a physician described as
having the same training and judgement as the model, holds a different copy.

| We ask the model | Condition | Example |
| --- | --- | --- |
| Its own view, from the real chart | A | 30% chance of kidney disease |
| Its own view, from John's copy | B | 12% |
| What John thinks; John has the real chart too | D | 28% |
| What John thinks; the model sees only John's copy | M | 13% |
| What John thinks; the model sees the real chart, John has his copy | C | 25% |

**Leakage (λ)** is where C sits between M and D: 0 means the model fully took
John's perspective, 1 means it reported its own belief as John's. Here
λ ≈ 0.84. Across patients it is estimated as a ratio of two regression slopes,
`1 − T_C / T_M`, on the model's own gap between A and B.

John's copy differs from the real chart in one of two ways:

- **Ignorance**: fields are missing (pairs `S4`, `S1`, `H`).
- **False belief**: one field is wrong (pair `F`).

Every condition is answered through the paper's 16 outputs (three probability
reports and thirteen decisions), on its 500 chronic-kidney-disease cases and
nine models.

## The current study: the curse of knowledge in Qwen3 8B and 14B

The current study measures the curse of knowledge in Qwen3 8B and Qwen3 14B:
how much of the model's private evidence leaks into the belief it attributes
to someone who lacks that evidence.

- **λ (leakage)** is estimated as β_C / β_D, a ratio of two regression slopes
  on the model's own gap l_A − l_B. It is compared with Camerer et al.'s
  w ≈ 0.51 for people.
- **ρ (reliability)** comes from a second run of A and B.
- **φ (responsiveness)** is how strongly the attributed belief follows the
  evidence.
- **τ = φ(1 − λ)** is a theory-of-mind score.

λ is estimated in a partial-evidence setting (John's copy lacks fields) and a
false-belief setting (a colleague read John one line wrongly). Each setting
is run on two datasets:

- **CKD**: beliefs from the latent fit and from the stated probability.
- **A bespoke dataset** of fictional items with known Bayes answers: stated
  probability only, plus placebo items and comprehension follow-ups
  (`docs/BESPOKE.md`).

Commands are in the [runbook](docs/RUNBOOK.md#curse-of-knowledge-experiment-qwen3-8b-and-14b).
Design and checks are in the
[implementation notes](docs/IMPLEMENTATION_NOTES.md#the-curse-of-knowledge-experiment).

## Research questions

| | Question | Measured by |
| --- | --- | --- |
| RQ1 | **Separation.** How much of the model's private knowledge leaks into the belief it attributes? Does capability remove it? Is a false belief harder than a missing fact? | λ per model and pair |
| RQ2 | **Coherence.** Is the attributed belief a belief at all: do the 16 answers about John agree, and does his belief move when he learns more? | Reports-to-decisions prediction; tracking along an evidence ladder |
| RQ3 | **Mechanism.** Where leakage occurs, by what route does the model's private knowledge reach its answer about John? | Probes, steering and blocking attention on open-weight models |

## Status

| Part | State |
| --- | --- |
| Conditions and prompts (15 conditions, tested) | Built |
| Generation with caching, resume and a cost-approval gate | Built (synchronous APIs; batch submission not yet) |
| Latent fits, calibration and the shared belief format | Built |
| Leakage estimators, contrasts and bootstrap (tested) | Built |
| Analysis and figures by research question | Built |
| Synthetic recovery of the estimator | Built; one scenario run so far |
| Activation extraction and probes | Built for the first design; updates for RQ3 pending |
| Steering and attention-blocking experiments (RQ3) | Not built |
| Curse-of-knowledge experiment: D_F, matched own-belief layout, second runs, bespoke dataset, comprehension follow-ups, vLLM/transformers/synthetic backends, ρ/φ/τ estimators, `second_order.curse` report and figures | Built; recovered end to end on the synthetic backend (`tests.test_pipeline`) |
| Main data collection | Not started |

No experimental results are in this repository yet.

## Quick start

```bash
conda env create -f environment.yml     # or environment-portable.yml off macOS arm64
conda activate latent-beliefs
pip install pyarrow                     # parquet support for second_order/
```

The second-order code reads the replication results bundle (cases, prompts
and condition A responses). Download it from Zenodo
(<https://doi.org/10.5281/zenodo.23350428>) and extract it here, which creates
`data/release/`; then run `./reproduce.sh analysis` once to build
`build/datasets/ckd.csv`.

Checks that make no model calls:

```bash
python -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke tests.test_generation
python -m unittest tests.test_pipeline          # end to end on the synthetic backend (a few minutes)
python -m second_order.example_prompts          # writes docs/example_prompts.md
python -m second_order.simulate --lambdas 0.3 --kappa 0.7 --rho -0.2
```

Running the experiment (model calls; each run prints its call count and
stops for approval above 1,000 calls):

```bash
python -m second_order.run_generation --model openai_terra --dry-run
python -m second_order.run_generation --model openai_terra --approve
python -m second_order.phase3 --model openai_terra     # parse, fit, export beliefs
python -m second_order.analyze                         # results/*.csv and RESULTS.md
python -m second_order.figures                         # figures/rq*.pdf
```

The curse-of-knowledge models run locally on a CUDA machine:

```bash
python -m second_order.run_generation --model qwen3_8b --approve   # CKD and bespoke, then comprehension turns
python -m second_order.phase3 --model qwen3_8b
python -m second_order.curse                                       # results/curse/RESULTS.md
```

[`docs/RUNBOOK.md`](docs/RUNBOOK.md) has the full command list, including the
pilot, activations and probes.

## Layout

```
configs/second_order.yaml    models, withheld sets, the per-model schedule, seeds, budget
second_order/                the experiment (see STRUCTURE.md)
  conditions.py                the condition table: roles A, B, C, M, D; pairs S4, S1, H, F
  prompts_second_order.py      prompts for every condition
  run_generation.py            cached, resumable generation
  fit_pooled.py, measurement.py  latent fits and shrinkage calibration
  leakage.py                   λ (= β_C/β_D), ρ, φ, τ and their checks, with the case bootstrap
  bespoke.py                   the bespoke dataset: item file, Bayes answers, prompts
  comprehension.py             comprehension follow-up questions and their parser
  datasets.py                  CKD and bespoke behind one interface
  synthetic.py                 text-level simulator with known parameters
  curse.py                     curse-of-knowledge tables and report
  coherence.py, analyze.py, figures.py   analysis by research question
  activations.py, probes.py    internal readouts for open-weight models
  simulate.py                  synthetic recovery of the estimators
scripts/                     standalone checks
tests/                       prompt, parser, estimator and cache tests
docs/                        runbook, implementation notes, orientation, example prompts
latent_beliefs/              the replication package (unchanged)
```

## Documentation

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): commands for every stage.
- [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md): design
  choices, the estimator and what has been validated.
- [`docs/ORIENTATION.md`](docs/ORIENTATION.md): how the first-order code is
  organised and the baseline it reproduces.
- [`docs/example_prompts.md`](docs/example_prompts.md): rendered prompts for
  every condition.
- [`docs/BESPOKE.md`](docs/BESPOKE.md): the bespoke item file format.
- [`STRUCTURE.md`](STRUCTURE.md): a map of both packages.
- [`docs/REPLICATION.md`](docs/REPLICATION.md): reproducing the original
  paper's figures and tables.

## License and citation

The code is released under the MIT License ([`LICENSE`](LICENSE)). The
replication package and its results bundle are the work of the original
authors; if you use them, cite the paper (see [`CITATION.cff`](CITATION.cff)
and [`docs/REPLICATION.md`](docs/REPLICATION.md) for data attributions).
