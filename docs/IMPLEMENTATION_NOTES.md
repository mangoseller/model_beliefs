# Implementation notes: where the build departs from or adds to the plan

Neutral record for Sam and his mentor. Commands are in `docs/RUNBOOK.md`.
The map of the first-order code is in `docs/ORIENTATION.md`.

## Code layout (adapting the plan's suggested layout)

| Plan | Here |
|---|---|
| `src/conditions.py` | `second_order/conditions.py` |
| `src/prompts_second_order.py` | `second_order/prompts_second_order.py` |
| `src/run_generation.py` | `second_order/run_generation.py` (plus `pilot.py` for Phase 1) |
| `src/fit_pooled.py` | `second_order/fit_pooled.py`, with `measurement.py` (sampling and calibration) and `phase3.py` (driver) |
| `src/beliefs.py` | `second_order/beliefs.py` |
| `src/activations.py`, `src/probes.py` | `second_order/activations.py`, `second_order/probes.py` |
| `src/leakage.py`, `src/agreement.py` | `second_order/leakage.py`, `second_order/coherence.py`, `second_order/analyze.py` (agreement, Phase 6) |
| `src/simulate.py`, `src/figures.py` | `second_order/simulate.py`, `second_order/figures.py` |

`latent_beliefs/` is untouched. `python -m second_order.baseline_check` confirms
that the first-order numbers still reproduce exactly.

## Prompts (Phase 2)

- **Layout.** The plan's C/D template puts the profile first. The
  first-order prompts put it last. The default here is the plan's layout:
  profile, agent paragraph, redirected question, then the first-order answer
  format verbatim. Setting `layout: repo` in the config gives the
  alternative. Both layouts are tested. Consequence: A/B and C/D prompts
  differ in layout as well as content, so part of the A − D contrast is a
  format effect.
- **Question wording.** These are minimal redirections of the repo's
  wording, not the plan table's paraphrases. For example, the threshold
  question keeps "the patient should be screened … if the probability that
  they have CKD is at least 10%", prefixed "John follows this rule:" and
  followed by "Does John screen this patient?". The bet keeps the repo's
  three-bullet payoff list. Options and answer formats are byte-identical to
  the first-order prompt, so `ckd.parse.parse_response` is used unchanged.
- **Agent paragraph.** Each sentence is on one line; the plan's line breaks
  looked like wrapping. Withheld fields are listed as `- Self-reported
  diabetes (diab)`, i.e. the profile's own field names.
- **E conditions (Phase 6).** The plan gives no template. The E prompt
  quotes the full first-order prompt between `"""` marks and introduces it
  with the agent description ("Another copy of you" / "A much smaller and
  less capable AI language model" / "The most capable AI language model
  currently available") "was given the message below in a separate
  conversation and answered it". It ends with "Predict the answer it gave",
  answered in the quoted message's format, so the same parsers apply.

## Generation

- **No sampling seed for hosted models.** Bedrock and OpenAI take no seed at
  T = 1. The `replicate` field in the cache key is a replicate index. Local
  MLX runs are seeded per batch.
- **Synchronous APIs.** The first-order data used batch APIs. The new
  `bedrock` and `openai` backends make synchronous calls with the same
  settings. Batch submission is not implemented; that is a decision for Sam
  (cost versus latency).
- **Cost estimate.** Prices are left as nulls in the config, so the gate
  prints token counts until Sam fills them in.

## Latent fits and the leakage estimator (Phases 3 and 5)

- **Pooling.** Fits pool A+B, C-John+D-John and C-user+D-user. E
  conditions are fitted one per group. The `separate` robustness variant
  fits each condition alone. Same model, optimiser and settings as the
  first-order main fit. Item parameters come from train rows, with early
  stopping on validation rows.
- **Split.** Each model's condition A split from `build/datasets/ckd.csv`
  is reused. Local MLX models use the split of the hosted model of the same
  size.
- **Shrinkage calibration (a change to the plan's estimator, found by the
  synthetic recovery test).** Posterior means of logit z shrink toward the
  prior, and they shrink more for 13 decisions than for all 16 outputs. With
  the plan's regression run on raw posterior means, λ = 0.7 came out as
  0.51 (raw slope) and 0.65–0.67 (λ_C/λ_D), and one interval missed 0.7.
  The fix is to de-shrink every **outcome-side** belief (ℓ_C, ℓ_D,
  ℓ_B^dec, and the null's two B halves). Each fit's own calibration line is
  used: simulate from the fitted prior and item model, then regress the
  posterior mean on the true logit z. The result is stored as
  `value_calibrated` in the belief format; for non-latent methods it equals
  `value`. The regressor side stays as the plan specifies. With an
  outcome side unbiased given the truth, the regressor's attenuation is
  identical in λ_C and λ_D, so **λ_C/λ_D ("normalised λ") is the
  recovered quantity and the headline**. The raw λ_C and λ_D are still
  reported. Raw λ_D is about the regressor's reliability (0.8–0.95 in
  simulations), not 1.
- **Synthetic recovery** (`results/synthetic_recovery/`). n = 500 cases.
  Logit z_A is drawn from GPT-5.6 Terra's fitted prior; Δ ~ N(0, 1). A/B
  outputs come from Terra's fitted item model and C/D outputs from Gemma 3
  27B's, so the two fits have different item parameters. Main run, 2,000
  resamples, λ_C/λ_D (95% interval):

  | True λ | John | User |
  |---|---|---|
  | 0 | 0.017 [−0.045, 0.075] | −0.049 [−0.119, 0.012] |
  | 0.3 | 0.280 [0.220, 0.336] | 0.271 [0.209, 0.329] |
  | 0.7 | 0.682 [0.637, 0.725] | 0.724 [0.678, 0.773] |
  | 1 | 1.030 [0.988, 1.071] | 0.989 [0.937, 1.044] |

  λ_0 covers 0 in all four runs. Raw λ_C is attenuated and misses its
  target in 5 of 8 cases; the two-weight regressions are attenuated
  likewise. Both are reported as descriptive statistics, not recovery
  targets. Three further replicates per λ are in
  `recovery_replicates.csv`.
- **Null for probes.** The probe has only two replay halves, so the null
  regresses (B_h1 − B_h2) on (A − B_all). Its noise is uncorrelated only if
  the two halves have equal noise variance.
- **Direct method.** Two-weight regressions only, as the plan specifies.
  Coherence for direct and probe uses a logistic link per decision, fitted
  on train rows (reports → decisions only).

## Coherence intervals

These are paired case bootstraps (2,000 resamples) on test cases complete
in all six conditions. The first-order Figure 1 intervals resample target
prompts, so the two kinds of interval are not directly comparable.

## Probes (Phase 4)

- Local checkpoints are 4-bit MLX Gemma 3 (4B, 12B); the responses came from
  hosted bf16 Gemma. The mean log-probability per token of the replayed
  hosted responses is reported as a fidelity check.
- Training rows are individual replays (4 per case). The penalty grid is
  C ∈ {0.001, 0.01, 0.1} for the truth probe and α ∈ {10, …, 10⁴} for the
  report probe. Layer and penalty are chosen per position by validation
  AUROC (truth, case-level, condition A) or validation R² (report).
- **Hosted Gemma 3 4B** has cached responses only for A, so B/C/D are
  replayed as prompts alone. P1 and P2 readouts exist for all conditions,
  and P3 only for A. The P3 truth probe is therefore trained on A only, and
  P3 λ cannot be computed until B/C/D responses exist. P2 is the
  prompt-only reading: it measures what the model represents before
  reasoning.
- **P1 check.** With the plan layout, C-John, D-John, C-user and D-user
  share the token prefix up to the end of the profile, so their P1 readouts
  must agree; this is checked. A's prefix differs (its question comes
  first), so A-versus-C agreement at P1 is reported but not expected.
- Larger open-weight models need a GPU instance and the `hf` extractor
  (`*_hf` config entries). Hosted generation needs API keys only.

## Pilot (Phase 1)

The plan's pilot models (one strong, one mid-range hosted model) need API
access. The pilot ran here on local 4-bit Gemma 3 4B (`gemma3_4b_mlx`),
50 flagged cases, 3 samples, with condition A also regenerated locally.
`results/pilot/`. The "remarks on missing fields" count is a regex
screen, and it flags condition A at a similar rate (false positives such as
"lack of cardiovascular disease"), so it needs reading by a person.
