# Design

How the code implements the Methods write-up on the two datasets in `datasets/`. Every
setting is in [`second_order/config.yaml`](../second_order/config.yaml); the data are
described in [`data.md`](data.md).

## Conditions

### Prompt adjustments to the released data

The loader (`second_order/datasets.py`) adjusts the released prompts in three ways. Each call
uses a fresh context.

- **Verified chart (CKD, false setting).** Wherever the model has a chart, including the
  controls, it says “Your chart gives the verified patient information.” PERSON accepts their
  own chart. Partial trials keep the released wording.
- **Reasoned multiple choice (CKD).** The released multiple-choice prompts (the 15 questions,
  the checks and the role probes) end with “Answer with a single letter.” Qwen3 reasons anyway,
  even with thinking off, so the loader asks for the paper's format instead: reasoning first,
  then a final `Choice: <letter>` line, with the same 4,096-token limit as the probability
  question. The last `Choice:` line is scored (anything after it must be the chosen option's
  text), as is a reply that is just the letter or option.
- **Follow-up format (bespoke).** The follow-up question gets the statement format its scorer
  reads: “Answer on one line in the form "Draw 1: <result>."” or “Write one line per draw in the
  form "Draw <number>: <result>."” (`datasets.followup_question`). The follow-up is the second
  turn, after the C answer, so no belief measurement depends on it.

| CKD condition (partial / false) | Model's chart | PERSON's chart | Whose belief? |
|---|---|---|---|
| A / A_F | Full | — | Model |
| B / B_F | Partial / altered | — | Model |
| M / M_F | Partial / altered | Same as model | PERSON |
| D / D_F | Full | Full | PERSON |
| C / C_F | Full | Partial / altered | PERSON |
| M_single / M_single_F | — | Partial / altered | PERSON |
| D_single | — | Full | PERSON |

- **Partial setting:** PERSON's evidence E_p lacks some of E. In CKD, three fields are
  withheld; in the bespoke items, PERSON saw only some observations.
- **False setting:** E_f alters part of E. In CKD, two fields are changed; in the bespoke
  items, a friend reported some observations wrongly.
- **References for C.** Primary CKD estimates use M and D (release names M2 and D2).
  These prompts preserve both ownership headers and their order. The second analysis uses
  the original one-chart controls. Layout comparison plots use the same patients in both panels.
  - The bespoke items have only the single-record references.
- **PERSON** is a different named person per CKD patient (20 names) and per bespoke item.
- **Second run.** CKD A and B come in two independently shuffled replicates. Bespoke A, B
  and B_F are run twice (`datasets.bespoke.replicates`). The two gaps give ρ.
- **Comprehension.**
  - CKD: standalone yes/no checks after C, each in both option orders.
  - Bespoke: after each C answer, a second turn asks the row's free-text question, scored
    by the observations named in the answer (`datasets.score_comprehension`).
- **Diagnostics** (analysed apart from the leakage estimator):
  - CKD role swaps: C_swap trades the two ownership headers; probe and probe_swap ask
    which record holds a field.
  - The bespoke report-assignment control C_ctrl_F: C_F's layout (the actual results, then
    what PERSON was told), but the friend made no mistakes, so its no-leakage value is D_F.
  - Two bespoke placebo items, whose withheld evidence is irrelevant.

## Beliefs

- **Direct elicitation:** the logit of the stated probability, clipped to [0.01, 0.99].
  Both datasets ask for it the same way: reasoning first, then the JSON object
  `{"probability": <number>}` with a number between 0 and 1, read from that object only.
  Bespoke items get `samples` calls per item, condition and run; the belief is the mean
  sample logit, with its standard error as `sd`. Belief files also include the mean probability and its clipped logit.
- **Latent ("Bayesian marginalization", CKD only):** the Smolin & Wilder latent model,
  with the first-order fit's settings, fitted to the probability question and the 15
  multiple-choice questions.
  - Each question in each option order is its own output, with its own item parameters,
    so position bias is absorbed.
  - One pooled fit covers the model's own beliefs (A, B, B_F, both replicates) and
    another covers PERSON's (M, D, their one-chart controls and C), so C and its references share
    item parameters and a prior.
  - A row enters the fit when at least `latent.min_output_share` of its outputs parsed;
    the model masks the rest.
  - Splits are seeded per model, over patients.
  - Letter answers are scored by the sampled choice, decoded through each row's option
    text. vLLM also records the top log-probabilities of the first answer token, so
    answer-probability scoring can be added without regenerating.
  - With `latent.anchor_cutoffs`, each decision with a stated cutoff c is fixed to cross
    1/2 at c, where c = 1/(1+k) for `threshold_k`, `bet_k` and `cost_k`. Off by default,
    as in the paper.

## Estimators

Per case, a patient or an item, let $x = \ell_A - \ell_B$. With M and D the reference pair,
$\beta_C$ and $\beta_D$ are the OLS slopes of $\ell_C - \ell_M$ and $\ell_D - \ell_M$ on $x$:

| | Estimator | Reported |
|---|---|---|
| Leakage | $\lambda = \beta_C / \beta_D$ | per setting and reference |
| Reliability | $\rho = \mathrm{Cov}(x, x') / \mathrm{Var}(x)$ | per setting |
| Theory-of-mind score | $\tau_s = (\beta_{D,s} - \beta_{C,s}) / \rho_s$ | per setting and reference |
| Responsiveness | $\varphi = \beta_D / \rho$ | shared by the two settings |

The primary table uses patients complete across both settings when at least five qualify.
If fewer qualify, it gives the available setting-specific fits and omits the pooled estimate.
Each two-panel CKD layout figure uses the intersection for both layouts. The exported
`scatter.csv` identifies the cohort, patient and fitted coefficients for every point.
`layouts.csv` gives the paired difference and its bootstrap interval.

- **Shared φ.** The shared $\varphi$ is the pooled within-setting $\beta_D$ over the pooled
  $\rho$, so each setting keeps its own intercept. Each setting's own $\beta_D/\rho$ is
  reported as `phi_setting`; the difference between the two tests the sharing.
- **τ with the shared φ.** The Methods identity $\ell_C - \ell_M = \varphi\lambda(\ell_A - \ell_B)$
  gives $\varphi\lambda = \beta_C/\rho$, so `tau_shared` $= \varphi_{\mathrm{pooled}} - \beta_{C,s}/\rho_s$
  and `phi_lambda` $= \beta_{C,s}/\rho_s$. Like τ, neither divides by $\beta_D$: a model whose
  attributions ignore the evidence scores τ ≈ 0, and a failed manipulation (ρ ≈ 0) is blanked.
- **Additions to the Methods:**
  - **Calibration.** Latent posterior means shrink toward the prior, by different
    amounts in the model's fit and in PERSON's, which would scale φ. So ρ, φ and τ use
    each fit's posterior means de-shrunk with its own calibration line
    (`fit_pooled.calibration`). λ uses the values as measured; its outcomes come from one fit,
    so shrinkage cancels.
  - **Uncertainty.** A paired bootstrap over patients or items carries all of a case's
    conditions and replicates together, and gives 95% percentile intervals.
  - **Fieller set.** λ also gets a 95% Fieller confidence set from the bootstrap
    covariance of $\beta_C$ and $\beta_D$, which is unbounded when $\beta_D$ is weak.
  - **Ratio rule.** Any other ratio is left blank when its denominator's interval
    includes 0.
- **Case sets** (CKD): every estimate is repeated on the patients right on both option
  orders of their check, and by assigned record order (PERSON's record first, or the
  model's own first).
- **Direction contrast** (CKD): on the 64 patients randomised to sicker or healthier edits,
  false-setting λ per direction and the difference, bootstrapped within label × direction.

## Checks of the assumptions

| Assumption | Check |
|---|---|
| Affine form | φ from levels: Cov(ℓ_D, ℓ_A′)/Cov(ℓ_A, ℓ_A′) and Cov(ℓ_M, ℓ_B′)/Cov(ℓ_B, ℓ_B′); quadratic terms of both gap regressions |
| Partial linearity | λ for x > 0 and x < 0 separately, and within thirds of \|x\|; the level ratio mean(ℓ_C − ℓ_M)/mean(ℓ_D − ℓ_M) |
| Independent noise | ρ uses independent calls; bespoke placebo items, whose three gaps should be 0 |
| φ shared | `phi_setting` per setting and its false − partial difference |

`RESULTS.md` lists every check that fails under **Anomalies**, along with weak λ
denominators, low comprehension accuracy and low retention.

## Validation

- `tests/test_datasets.py` checks the datasets against the table above:
  - which records every CKD condition shows;
  - that M and D differ only in the record;
  - that the bespoke Bayes answers follow the believer.
- `tests/test_leakage.py` simulates the Methods model with noise large enough to attenuate
  β_D. It checks that λ, ρ, the shared φ and τ are recovered for both references, and that
  the Fieller set, the ratio rule and the curvature check work.
- `tests/test_pipeline.py` runs the whole pipeline on the `synthetic` backend. That
  backend answers the real prompts from planted λ, φ and noise, reading the records each
  prompt shows. The test checks that every dataset, belief method and reference
  recovers them.
