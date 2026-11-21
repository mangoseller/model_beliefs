# Code structure

This file maps both packages: `latent_beliefs` (the replication package, most
of this file) and `second_order` (the second-order belief experiment, at the
end).

The package `latent_beliefs` is organised by pipeline stage.  Every stage is a
module with a command-line interface (`python -m latent_beliefs.<module>`)
whose defaults read and write the standard locations in `config.py`.  A
no-argument run therefore performs the paper's step.  `reproduce.sh` chains
the stages.

## Data flow

```
 public datasets (data/raw)                      results bundle (data/release)
        |                                                 |
   sources --> prompts --> collection --> responses ------+
                 |                            |
                 |                          parse ----> datasets ----> refit (GPU) --> fits
                 |                            |            |                           |
                 +------ branching (prompts, collect, parse) ----------------+         |
                                                           |                 |         |
                                                           +---> evaluation <+---------+
                                                                      |
                                                              figures, tables
```

Stage outputs land under `build/`:

| Stage | Output directory |
|---|---|
| sources | `build/sources/<domain>/` |
| prompts | `build/prompts/<domain>/` |
| parse | `build/parsed/<domain>/` |
| datasets | `build/datasets/<domain>.csv` |
| refit | `build/fits/<fit>/` |
| evaluation | `build/evaluation/<analysis>/` |
| figures | `build/figures/<stem>.pdf`, `build/figure_data/<stem>.csv` |
| tables | `build/tables/*.tex` |

The analysis stages read fits from `LB_FITS_DIR` (default
`data/release/fits`) and responses from `LB_RESPONSES_DIR` (default
`data/release/responses`).

## Shared modules

| Module | Role |
|---|---|
| `config.py` | All paths (overridable with `LB_*` environment variables), domain keys and labels |
| `models.py` | The nine models: provider IDs, display names, figure order, example and branching models, and the pinned position maps behind seeds and `model_position` columns |
| `release.py` | `pack`, `manifest`, `verify`, `unpack` the results bundle |
| `datasets.py` | One shared builder for the three domains' complete-case 16-output matrices.  It draws the seeded per-model train/validation/test splits and repairs holdouts so that every output varies within them |

## Domains: sources → prompts → strict parsing

Each domain subpackage builds its cases from public data, renders the 16
prompts, and strictly parses the responses.  Parsing writes
`response_values.csv`, `paired_responses.csv`, and `parse_diagnostics.csv`.

**`ckd/`: chronic-kidney-disease screening**

| Module | Role |
|---|---|
| `download.py` | 64 NHANES XPT files, checked against `assets/ckd/nhanes_xpt_sha256.txt` |
| `nhanes.py` | Pooled adult NHANES table; CKD label from CKD-EPI 2021 eGFR < 60 or albumin-creatinine ratio ≥ 30 |
| `risk.py` | 5-fold cross-validated XGBoost risk score on 26 features (used only to stratify sampling) |
| `cases.py` | Two seeded, risk-decile-stratified 500-case cohorts.  Prompts use the first; quintile bin edges use all 1,000 cases |
| `prompts.py` | Patient-profile rendering and the 16 prompt families |
| `parse.py` | Strict parser; also exposes `parse_response` and `normalize_probability` |

**`werewolf/`: Werewolf games**

| Module | Role |
|---|---|
| `download.py` | Kaggle archive via `kagglehub` |
| `cases.py` | Seeded sampler of 500 (focal player, target, cut point) cases.  Enforces one case per replay and stratum quotas, and keeps only evidence visible to the focal player |
| `prompts.py` | Transcript rendering and the 16 prompt families |
| `parse.py` | Strict parser |
| `guards.py` | Answer-consistency checks used by the parser: conflicting options, wrong target, commitment |

**`forecasting/`: scientific forecasting**

| Module | Role |
|---|---|
| `download.py` | Code Ocean data files; manual Voelkel et al. supplement |
| `R/primary_scenarios.R` | Condition pairs from the primary archive |
| `R/megastudy_effects.R` | Megastudy effects |
| `R/gold.R` | Ground-truth sign on the response scale shown in the prompt |
| `sources.py` | Runs the R steps; extracts the Voelkel treatment texts; assembles the 500 scenarios |
| `prompts.py` | The 16 framings from `assets/forecasting/framing_variants.md` |
| `parse.py` | Strict parser |

## Collection

| Module | Role |
|---|---|
| `collection/records.py` | Loads `tasks.json` and prompts; record IDs (`sha256(task_id:prompt_id)[:32]`) |
| `collection/bedrock.py` | Bedrock Converse batch: prepare, upload, submit, status, download |
| `collection/openai.py` | OpenAI Responses batch: prepare, submit, status, download |
| `collection/materialize.py` | Batch outputs → `responses/<domain>/<task_id>/<model>.jsonl` |

## Branching experiments (`branching/`)

| Module | Role |
|---|---|
| `design.py` | The two experiments (20 prefixes × 16 prompts; 5 prefixes × 5 probability answers) and the case selection |
| `prompts.py` | First-stage reasoning prompt and follow-up task texts, split from the domain prompts |
| `collect.py` | Two-stage Bedrock batch with exact replay of the assistant prefix |
| `materialize.py` | Compact response files; each prefix is stored once |
| `parse.py` | Reuses the domain parsers |
| `icc.py` | Per-case prefix intraclass correlation with a bootstrap (Figure 6b) |
| `prediction.py` | Reports↔decisions prediction from same-prefix vs different-prefix evidence, using the fixed main fit (Figure 6c) |

## Latent-variable model

| Module | Role |
|---|---|
| `latent/model.py` | The model.  Fitted 7-knot prior over logit(z); Beta probability reports; monotone logit decisions; ordered-logit ordinal reports; optional monotone-spline response curves; 128-point Gauss–Legendre quadrature; Adam fitting with early stopping; concordance index; restoring a fit from `parameters.csv` |
| `latent/domains.py` | The 16 output specifications per domain and dataset loading |
| `latent/fit.py` | Fits every domain × model cell (main and spline), with sharding and merging |
| `latent/two_d.py` | 2-D latent sensitivity fit and its leave-one-output-out evaluation |
| `holdout/common.py` | Framing-shared link of Eq. (1), generic decision schema, cell runner |
| `holdout/framing.py` | Framing held out (evidence: eight decisions or the three reports) |
| `holdout/domain.py` | Domain held out |
| `holdout/joint.py` | Both held out |
| `holdout/report_domain.py` | Report-evidence domain holdouts |
| `holdout/grid.py` | The 2 × 2 information grid (Figure 2 and its appendix variant) |

## Evaluation (`evaluation/`)

| Module | Analysis | Paper |
|---|---|---|
| `reciprocal.py` | Reports → decisions and decisions → reports, with bootstrap CIs | Figure 1 |
| `leave_one_out.py` | Single-output held-out prediction (main and spline fits) | Figure 4 inputs; appendix |
| `probability_sufficiency.py` | Probability-report-only vs all-other-output posteriors | Figure 4; appendix R² |
| `outcome_sufficiency.py` | L1 outcome-sufficiency statistic with local-linear smoothers and a joint case bootstrap | Appendix |

## Figures and tables

`figures/` has one module per paper figure, named by its file stem, plus
`style.py` for the shared visual language and `holdout_grid.py` for the two
grid figures.  `python -m latent_beliefs.figures` renders all ten.
`tables/retention.py` writes the appendix complete-case retention table.

## Assets

| File | Contents |
|---|---|
| `assets/ckd/nhanes_xpt_sha256.txt` | Digests of the NHANES files used |
| `assets/forecasting/framing_variants.md` | The 16 forecasting framings (hand-authored prompt text) |
| `assets/forecasting/megastudy_prompt_enrichment.json` | Reference-arm text for the 7 anti-democratic-attitudes megastudy cases |
| `assets/branching/selected_instances.csv` | The 30 cases per domain used in the branching experiments |

## The second-order experiment (`second_order/`)

Runs beside `latent_beliefs` and never modifies it. Every module has a
command-line interface (`python -m second_order.<module>`); settings are in
`configs/second_order.yaml`.

```
 condition table --> prompts --> generation (cache) --> responses --> pooled fits --> beliefs
                                                                          |             |
                                              activations --> probes -----+------------>|
                                                                                        v
                                                              leakage, coherence --> analyze --> figures
```

| Module | Role |
|---|---|
| `config.py` | Paths, the config loader, and each model's scheduled conditions |
| `conditions.py` | The condition table (roles A, B, C, M, D; pairs S4, S1, H, F; framings) and profile rendering: withheld fields, the flipped field |
| `prompts_second_order.py` | Prompts for every condition, built from the first-order prompt with minimal edits |
| `example_prompts.py` | Renders `docs/example_prompts.md` |
| `run_generation.py` | Cached, resumable generation (Bedrock, OpenAI, local MLX, vLLM, transformers, synthetic) with a cost-approval gate; second runs, samples and two-turn comprehension items |
| `pilot.py` | Phase 1 pilot: how far each withheld set and the flip move the model's own answer |
| `responses.py` | Parses cached responses with the first-order parser (after removing `<think>` blocks); complete-case matrices; seeded splits for local models; retention; comprehension scoring |
| `fit_pooled.py` | One pooled latent fit per family (own belief, John, user); posteriors for each output subset |
| `measurement.py` | Sampling from a fitted measurement model; shrinkage calibration |
| `phase3.py` | Driver: parse, fit, export latents and beliefs, retention, transcripts |
| `beliefs.py` | The shared belief format every method writes and the analysis reads |
| `leakage.py` | The matched-control λ (= β_C/β_D), ρ, φ and τ from a second run, the affine checks, the halves-based κ and τ, contrasts, the ratio rule and the paired case bootstrap |
| `coherence.py` | Reports-to-decisions coherence per condition and its contrasts |
| `analyze.py` | Tables by research question, method agreement, anomalies, `RESULTS.md` |
| `figures.py` | Figures by research question, from `results/` only |
| `activations.py` | Replays prompts and responses through an open-weight model and stores residual-stream activations |
| `probes.py` | Report and truth probes with controls; probe readouts in the belief format |
| `simulate.py` | Synthetic recovery of the estimators through the full fit |
| `bespoke.py` | The bespoke dataset: item file (schema in `docs/BESPOKE.md`), exact Bayes answers, prompts for every condition, comprehension questions |
| `comprehension.py` | Comprehension follow-ups: the question format and the strict parser |
| `datasets.py` | CKD and bespoke behind one interface (outputs, cases, prompts, samples) |
| `synthetic.py` | The `synthetic` backend: response text from known λ, φ and noise |
| `curse.py` | Curse-of-knowledge tables (λ, ρ, φ, τ, checks, placebo, Bayes, comprehension) and `results/curse/RESULTS.md` |
| `baseline_check.py` | Confirms the first-order baseline still reproduces |

Outputs: `data/generation_cache/`, `data/responses/`, `data/latents/`,
`data/beliefs/`, `data/activations/`, `results/` (the curse-of-knowledge tables
in `results/curse/`), `figures/` and `RESULTS.md`, all ignored by git. The
bespoke item file is local too (`data/bespoke/items.yaml` by default);
`tests/fixtures/bespoke_items.yaml` is a small example used by the tests. `scripts/estimator_bias_check.py` is a standalone
comparison of the two leakage estimators; `tests/` holds the prompt, parser,
estimator and cache tests.
