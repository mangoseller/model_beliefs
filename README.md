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
python -m unittest tests.test_prompts_second_order tests.test_leakage tests.test_bespoke tests.test_generation
python -m unittest tests.test_pipeline          # end to end on the synthetic backend (a few minutes)
python -m second_order.bespoke                  # check the bespoke item file
python -m second_order.example_prompts          # writes docs/example_prompts.md
```

The experiment (each run prints its call count and stops for approval above
1,000 calls):

```bash
python -m second_order.run_generation --model qwen3_8b --approve   # CKD and bespoke, then comprehension turns
python -m second_order.phase3 --model qwen3_8b                     # parse, latent fits, beliefs
python -m second_order.curse                                       # results/curse/*.csv, results/curse/RESULTS.md
python -m second_order.figures                                     # figures/curse_*
```

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
  deprecated/                  the earlier RQ1-RQ3 design (see below)
tests/                       prompt, parser, estimator, cache and end-to-end tests
docs/                        runbook, implementation notes, bespoke schema, orientation, example prompts
latent_beliefs/              the replication package (unchanged)
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
- [`STRUCTURE.md`](STRUCTURE.md): a map of both packages.
- [`docs/REPLICATION.md`](docs/REPLICATION.md): reproducing the original
  paper's figures and tables.

## License and citation

The code is released under the MIT License ([`LICENSE`](LICENSE)). The
replication package and its results bundle are the work of the original
authors; if you use them, cite the paper (see [`CITATION.cff`](CITATION.cff)
and [`docs/REPLICATION.md`](docs/REPLICATION.md) for data attributions).
