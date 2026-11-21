# Design

How the code implements the Methods write-up. Every setting is in
[`second_order/config.yaml`](../second_order/config.yaml).

## Conditions

| Condition | The model sees | Belief asked for | PERSON has |
|---|---|---|---|
| A | E | its own | |
| B, B_F | E_p, E_f | its own | |
| M, M_F | E_p, E_f | PERSON's | what the model sees |
| C, C_F | E | PERSON's | E_p, E_f |
| D, D_F | E | PERSON's | E |

`second_order/conditions.py` holds this table; nothing else lists conditions.

- **Partial setting** (B, C, M, D): PERSON's copy lists some fields as `Not recorded`
  (CKD: `withheld` in the config), or PERSON saw a subset of the observations (bespoke).
- **False setting** (B_F, C_F, M_F, D_F): a colleague read PERSON the evidence and got
  part of it wrong (CKD: one Yes/No field, `flip_field`). M_F, C_F and D_F all frame
  PERSON's evidence as reported.
- **One template for M and D.** In each setting, M and D differ only in the evidence
  block. The tests check this byte for byte.
- **Same suffix.** Every prompt is the evidence, then (about PERSON) one paragraph on
  what PERSON knows, then the question and the first-order answer format. The answer
  format is cut verbatim from the paper's prompts, so A and B are the first-order
  questions with the same suffix as C, M and D.
- **Second run.** A, B and B_F run twice. The two independent gaps x and x′ give ρ.
- **Comprehension.** After each C answer (CKD: the numeric probability only), a second
  turn asks which evidence PERSON had. The analysis is repeated on the answers whose
  follow-up was correct.

## Beliefs

- **Latent ("Bayesian marginalization", CKD only).** The 16 CKD outputs are fitted with
  the Smolin & Wilder latent model, with the first-order fit's settings. One pooled
  fit covers the model's own beliefs (A, B, B_F, both runs) and another covers PERSON's
  (C, M, D and their false-setting versions). C, M and D therefore share item
  parameters and a prior. Each Qwen model gets its own seeded train/validation/test split.
- **Direct elicitation.** The logit of the stated probability, clipped to [0.01, 0.99].
  Bespoke items are sampled `bespoke.samples` times per condition and run. Their belief
  is the mean sample logit, with its standard error as `sd`.

## Estimators

With $x = \ell_A - \ell_B$ per case, and $\beta_C$, $\beta_D$ the OLS slopes of
$\ell_C - \ell_M$ and $\ell_D - \ell_M$ on $x$:

| | Estimator | Reported |
|---|---|---|
| Leakage | $\lambda = \beta_C / \beta_D$ | per setting |
| Reliability | $\rho = \mathrm{Cov}(x, x') / \mathrm{Var}(x)$ | per setting |
| Theory-of-mind score | $\tau = (\beta_D - \beta_C) / \rho$ | per setting |
| Responsiveness | $\varphi = \beta_D / \rho$ | shared by the two settings |

The shared $\varphi$ is the pooled within-setting $\beta_D$ divided by the pooled $\rho$, so
each setting keeps its own intercept. Each setting's own $\beta_D/\rho$ is also reported
(`phi_setting`); the difference between the two tests the sharing assumption.

Two choices go beyond the Methods text:

- **Calibration.** Latent posterior means shrink toward the prior, by different
  amounts in the model's fit and in PERSON's, and that would scale φ. So ρ, φ and τ
  use each fit's posterior means de-shrunk with its own calibration line
  (`measurement.py`). λ is a ratio of slopes on outcomes from one fit, so it uses the
  values as measured; the two agree when there is no shrinkage. For stated
  probabilities, the calibrated values are the values.
- **Uncertainty.** A paired bootstrap over cases gives 95% percentile intervals, with
  latent item parameters held fixed. A ratio is left blank when its denominator's
  interval includes 0.

## Checks of the assumptions

| Assumption | Check |
|---|---|
| Affine form | φ from levels: Cov(ℓ_D, ℓ_A′)/Cov(ℓ_A, ℓ_A′) and Cov(ℓ_M, ℓ_B′)/Cov(ℓ_B, ℓ_B′); quadratic terms of both gap regressions |
| Partial linearity | λ for x > 0 and x < 0 separately, and within thirds of \|x\|; the level ratio mean(ℓ_C − ℓ_M)/mean(ℓ_D − ℓ_M) |
| Independent noise | ρ uses independent calls; bespoke placebo items, whose three gaps should be 0 |
| φ shared | `phi_setting` per setting and its false − partial difference |

`RESULTS.md` lists every check that fails under **Anomalies**.

## Validation

- The unit tests (`tests/test_leakage.py`) simulate the Methods model with noise large
  enough to attenuate β_D. They check that λ, ρ, the shared φ and τ are recovered, and
  that the ratio rule and the curvature check work.
- `tests/test_pipeline.py` runs the whole pipeline on the `synthetic` backend. That
  backend writes response text from planted λ, φ and noise. The test checks that both
  settings, both datasets and both belief methods recover them.
