# Design

How the code implements the Methods write-up on the two datasets in `datasets/`. Every
setting is in [`second_order/config.yaml`](../second_order/config.yaml); the data are
described in [`data.md`](data.md).

## Conditions

Every prompt is sent exactly as the dataset gives it, in a fresh context. Each row's
setting and case map to one condition key (`second_order/conditions.py`; false-setting
keys end in `_F`).

| Condition | The model's own evidence | PERSON's evidence | Belief asked for |
|---|---|---|---|
| A | E | | its own |
| B, B_F | E_p, E_f | | its own |
| M, M_F | | E_p, E_f | PERSON's |
| D (CKD: shared; bespoke false: D_F) | | E | PERSON's |
| M2, M2_F (CKD) | E_p, E_f | E_p, E_f | PERSON's |
| D2 (CKD, shared) | E | E | PERSON's |
| C, C_F | E | E_p, E_f | PERSON's |

- **Partial setting:** PERSON's evidence E_p lacks some of E. In CKD, three fields are
  withheld; in the bespoke items, PERSON saw only some observations.
- **False setting:** E_f alters part of E. In CKD, two fields are changed; in the bespoke
  items, a friend reported some observations wrongly.
- **References for C.**
  - In CKD, C shows two records, PERSON's and the model's own. Its matched no-leakage
    references, M2 and D2, have the same layout: two records, both PERSON's evidence (M2)
    or both full (D2). They give the headline estimate.
  - M and D show PERSON's record alone, as in the Methods; they give the single-record
    estimate, reported beside it.
  - The bespoke items have only the single-record references.
- **PERSON** is a different named person per CKD patient (20 names) and per bespoke item.
- **Second run.** CKD A and B come in two independently shuffled replicates. Bespoke A, B
  and B_F are run twice (`datasets.bespoke.replicates`). The two gaps give ρ.
- **Comprehension.**
  - CKD: standalone yes/no checks after C, each in both option orders.
  - Bespoke: after each C answer, a second turn asks the row's free-text question, scored
    by the observations named in the answer (`bespoke.score_comprehension`).
- **Diagnostics** (analysed apart from the leakage estimator):
  - CKD role swaps: C_swap trades the two ownership headers; probe and probe_swap ask
    which record holds a field.
  - The bespoke report-assignment control C_ctrl_F: C_F's layout, but the friend read
    PERSON the true record, so its no-leakage value is D_F.
  - Two bespoke placebo items, whose withheld evidence is irrelevant.

## Beliefs

- **Direct elicitation:** the logit of the stated probability, clipped to [0.01, 0.99].
  CKD answers a single number from 0 to 100, which is divided by 100. Bespoke answers
  are read from the JSON object, `samples` calls per item, condition and run; the belief
  is the mean sample logit, with its standard error as `sd`.
- **Latent ("Bayesian marginalization", CKD only):** the Smolin & Wilder latent model,
  with the first-order fit's settings, fitted to the probability question and the 15
  multiple-choice questions.
  - Each question in each option order is its own output, with its own item parameters,
    so position bias is absorbed.
  - One pooled fit covers the model's own beliefs (A, B, B_F, both replicates) and
    another covers PERSON's (every M, D, M2, D2 and C), so C and its references share
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
| Theory-of-mind score | $\tau = (\beta_D - \beta_C) / \rho$ | per setting and reference |
| Responsiveness | $\varphi = \beta_D / \rho$ | shared by the two settings |

- **Shared φ.** The shared $\varphi$ is the pooled within-setting $\beta_D$ over the pooled
  $\rho$, so each setting keeps its own intercept. Each setting's own $\beta_D/\rho$ is
  reported as `phi_setting`; the difference between the two tests the sharing.
- **Additions to the Methods:**
  - **Calibration.** Latent posterior means shrink toward the prior, by different
    amounts in the model's fit and in PERSON's, which would scale φ. So ρ, φ and τ use
    each fit's posterior means de-shrunk with its own calibration line
    (`measurement.py`). λ uses the values as measured; its outcomes come from one fit,
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
