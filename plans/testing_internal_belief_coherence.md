# Next experiments: does internal information change the conclusion about small-model beliefs?

## Central question

The aim is not merely to improve action prediction. It is to test whether the weaker predictive performance of the paper's one-dimensional belief model on smaller language models reflects limitations of the statistical specification or of external belief measurement, rather than an absence of a useful coherent belief state.

Reference: [Smolin and Wilder, *Beliefs and Behavior in Language Models*](https://arxiv.org/pdf/2609.07943).

The desired result is a single, ideally linearly decodable internal feature that supports a shared, interpretable latent belief across diverse responses. Improvements restricted to one action family are insufficient to establish that result.

This document records the joint experiment motivated by the initial pilot,
retaining its activation caches and implementation checks. The four-cell joint
comparison below is now complete; fresh-case confirmation, transfer, and causal
tests remain future work.

Completion update (2026-11-08): both model sizes and both response-curve
specifications completed their 32-candidate activation grids, four patient-feature
candidates, shuffled controls, fixed-epoch final fits, and test evaluations.
All numerical and target-leakage checks passed across 94,760 prediction rows.
See the [full report](../build/joint_beliefs/full_experiment/report.html) and
[implementation notes](../experiments/joint_beliefs/README.md).

With original curves, adding activations raises numerical-report R² from 0.157
to 0.653 for 4B and from 0.632 to 0.865 for 12B. Spline curves yield a similar
pattern. Neither specification improves aggregate decision log loss from reports.
Patient-feature models also recover the numerical reports well; this is
compatible with evidence-based beliefs and does not refute internal coherence.
The main outstanding questions concern broad predictive adequacy and calibration,
including the sparse-response issue documented in the diagnostic follow-up below.

The initial single-candidate 4B benchmark took 32.57 seconds, plus 33.66 seconds
for reusable baseline preparation. Full-run timings are saved separately; its
5.51-hour wall duration includes a documented 116-minute low-power sleep and
earlier shorter interruptions, while overlapping saved fits sum to 5.40 worker-hours.

## Distinguish the explanations

| Explanation | Relevant test | What a positive result would mean |
|---|---|---|
| External measurement is inadequate | Add activations while retaining the paper's main response-likelihood forms | External responses miss information useful for identifying the shared latent belief |
| Response curves are misspecified | Improve behavioral-only response curves without adding activations | Some apparent incoherence reflects the assumed mapping from belief to responses |
| Both problems matter | Combine activation-informed inference and richer monotone response curves | Better measurement and a better response model are jointly necessary |
| No adequate one-dimensional structure under these tests | Neither extension gives broad held-out predictive gains | These particular extensions do not overturn the paper's tentative conclusion |

A null result remains specific to the chosen checkpoint, extraction prompt, layer, linear direction, likelihood family, and domain. It cannot establish that no suitable internal belief representation exists.

## What the completed 4B pilot actually shows

The pilot uses existing Gemma 3 4B CKD responses and local 4-bit MLX activations. It preserves the released split: 313 fitting cases, 84 validation cases, and 103 test cases. Eight threshold/cost decisions predict four betting decisions. All response families are represented in training, so this is not zero-shot framing generalization.

The original pilot fits thirteen response channels but evaluates four betting targets; it does not yet reproduce the full sixteen-output battery. The activation direction is fitted with the behavioral likelihood parameters frozen.

| 4B method | Test log loss, lower is better | Mean per-decision AUROC, higher is better |
|---|---:|---:|
| Refit flexible behavioral baseline | 0.6662 | 0.5022 |
| Frozen-curve latent model plus activation shift | 0.6537 | 0.5842 |
| Directly trained internal scalar, no latent integration | 0.6457 | 0.5984 |
| Direct raw-patient-feature scalar | 0.6559 | 0.5562 |

For the augmented latent model, the paired log-loss improvement is $0.0125$, with a 95% case-bootstrap interval of $[-0.0034,\,0.0292]$. Its secondary AUROC improvement is $0.0820$, with interval $[0.0251,\,0.1377]$.

The direct internal scalar improves log loss over external behavior by $0.0205$, with interval $[0.0014,\,0.0406]$. However, this was an exploratory follow-up after examining the same test set. Its advantage over the raw-patient-feature control is uncertain: the log-loss gain interval includes zero. Conditional bootstrap intervals do not make an adaptively chosen follow-up an independent confirmation.

These results motivate further work on an internal signal. They do not demonstrate a coherent belief across the full response battery or reverse the paper's conclusion. In particular, a direction trained to predict betting can encode a tendency to accept bets rather than a general belief about CKD.

Artifacts: [frozen-curve results](../build/activation_pilot/flexible/report.html), [direct-probe results](../build/activation_pilot/direct/report.html), and [implementation notes](../experiments/activation_pilot/README.md).

## Priority 1: jointly fit the full activation-informed latent model

### Use the paper's main model, rather than the shared-threshold restriction

The main binary likelihood permits separate response curves:

$$
P(a_{ij}=1\mid s_i)=\operatorname{sigmoid}(\alpha_j+\beta_j s_i),
\qquad \beta_j>0.
$$

The shared-threshold specification,

$$
P(a_{ij}=1\mid s_i)=\operatorname{sigmoid}\left(\alpha+\beta[s_i-\operatorname{logit}(\tau_j)]\right),
$$

imposes additional consistency across instructed thresholds and payoffs. A small model can fail that restriction because of instruction-following or framing effects even if a shared latent belief remains predictive. Keep it as a secondary instruction-consistency test, not the primary test of belief coherence.

### Retain one latent dimension and one activation direction

For a fixed layer $\ell$, use a decision-independent residual vector $h_i^{(\ell)}$, centered and scaled using fitting cases only:

$$
u_i\sim p_0(u;\eta),\qquad
s_i=u_i+w^\top\widetilde h_i^{(\ell)},\qquad
z_i=\operatorname{sigmoid}(s_i).
$$

All response types use the same effective belief:

$$
a_{ij}\mid s_i\sim F_j(s_i;\theta_j).
$$

Retain the paper's binary, ordinal, and numerical-probability likelihoods initially. In particular, the probability-report likelihood anchors the effective belief scale. Do not give each output its own activation direction or let activations enter the response heads through a separate route.

The key change from the pilot is to fit $w$, the prior parameters $\eta$, and all response parameters $\theta$ jointly. Frozen response curves learned from externally inferred beliefs need not be the appropriate curves for an internally informed belief.

### Train on the joint response distribution

Use all sixteen response types and a penalized conditional marginal likelihood:

$$
\mathcal L(w,\theta,\eta)
=-\frac{1}{N}\sum_i
\log\int p_0(u;\eta)
\prod_{j\in J_i}
p_{\theta_j}\left(a_{ij}\mid u+w^\top\widetilde h_i^{(\ell)}\right)\,du
+\lambda\lVert w\rVert_2^2.
$$

Here $J_i$ contains the available training responses for case $i$. This is not a separate supervised direction for each target family. The single scalar must support the joint response structure through interpretable monotone likelihoods.

There is no new activation-noise parameter in this first extension. Uncertainty remains in $u_i$ and the response likelihoods. Setting $w=0$ recovers the behavioral-only model.

Joint fitting is generally nonconvex. Initialize from the behavioral fit with $w=0$; a previously fitted activation direction can provide a second initialization. Choose between initializations using validation data only. Keep the number of starts small and report optimization diagnostics.

## Priority 2: evaluate broad coherence using the paper's tasks

For observed outputs $O$, infer

$$
p(u_i\mid a_{i,O},h_i)
\propto p_0(u_i;\eta)
\prod_{j\in O}
p_{\theta_j}(a_{ij}\mid u_i+w^\top\widetilde h_i).
$$

Integrate over this posterior to predict withheld outputs. Target values must never enter the conditioning posterior.

Reproduce the following evaluation tasks on unseen cases:

1. Probability assessments predict decisions.
2. Decisions predict probability assessments.
3. Individual outputs are withheld while other outputs provide evidence.

For each task, compare identical external evidence with and without activations. Use the same fitted direction across all tasks. Do not choose a separate layer, direction, or hyperparameter configuration for each test target family.

Retain the paper's concordance/AUROC and out-of-sample $R^2$ measures for comparability. Also report proper predictive scores and calibration, separating binary log loss from continuous/ordinal results so that the meaning of each score is clear. Predeclare the aggregate criterion for selecting a layer and regularization strength on validation cases.

Check whether gains are broad across response types. An average improvement driven by one family should not be described as restored general coherence. Treat zero-shot framing or domain transfer as subsequent, stronger tests: the main model's per-output response curves cannot be evaluated zero-shot for a completely unseen output without additional structure.

## Priority 3: separate measurement gains from response-form gains

Use this comparison matrix, beginning with the first column:

| Available information | Paper's main monotone likelihoods | Richer monotone likelihoods |
|---|---|---|
| External behavior only | Reproduced baseline | Functional-form control |
| External behavior plus one internal scalar | Joint activation-informed model | Combined extension |

The repository already supports monotone-spline response curves. Reuse that implementation for the behavioral-only control, and assess whether it accounts for the apparent benefit attributed to internals. The paper already investigated richer specifications; this control should be a matched comparison in our setting, not presented as a wholly untested idea.

Add a raw-patient-feature variant with the same one-dimensional bottleneck and response model. It tests the incremental predictive utility of reading activations rather than reconstructing a state from the evidence. A linear raw-feature control is not an exhaustive control for all nonlinear functions of the input.

This is not a necessary hurdle for identifying coherent belief. If patient evidence produces a belief that generates responses, both an activation readout and a patient-feature model can predict those responses equally well. Matching the control is compatible with an internal belief, and beating it would not by itself identify the mechanism. A successful patient-feature model with the same shared scalar could itself support behavioral coherence, while leaving the neural representation unidentified. Keep that distinction separate from claims about incremental readout utility or causal internal representation.

Keep a shuffled-activation sanity check. A single shuffle is not a permutation significance test. If a substantive claim depends on comparison with chance probes, use repeated shuffled fits with the same selection procedure.

## Priority 4: compare 4B and 12B on identical cases

The larger-model comparison is already requested. The existing 12B responses are available locally. Its original case split differs from 4B, so assign 12B cases to the 4B split and refit all 12B behavioral baselines. Do not reuse released 12B fitted parameters on that aligned test set: some aligned test cases were in their original training data.

Use the same prompt, token position, relative layer-search coverage, response tasks, and model-selection protocol for both sizes. Compare both absolute predictive performance and the incremental benefit from activations. Bootstrap paired case IDs across model sizes when estimating a difference in gains.

The expected pattern is stronger behavioral-only prediction for 12B and a larger internal-information benefit for 4B. This is a hypothesis, not a constraint on the results. Better 12B performance alone does not establish the proposed explanation; a smaller gain for 12B also needs uncertainty estimates.

The 12B frozen-curve and direct-probe diagnostics have now finished. They are not the joint sixteen-output experiment proposed above.

| Model | Behavioral-only AUROC | Behavior plus activation AUROC | Direct internal scalar AUROC |
|---|---:|---:|---:|
| Gemma 3 4B | 0.5022 | 0.5842 | 0.5984 |
| Gemma 3 12B | 0.7488 | 0.9114 | 0.9300 |

These rows use the flexible likelihood and the same 103 test cases. The 12B augmented model improves log loss from $0.4228$ to $0.3216$, versus $0.6662$ to $0.6537$ for 4B. The direct 12B probe reaches $0.2888$, but a direct raw-profile scalar is close at $0.2918$; their difference is uncertain. Thus this pilot does not show the expected smaller-model advantage or reverse the small-model coherence conclusion. The stronger gain occurs for 12B, and raw case information explains much of its benefit.

The 12B shared-threshold run failed a quadrature sensitivity check and is excluded from comparative conclusions. The flexible-model result is stable under the higher-resolution audit. See the [size comparison](../build/activation_comparison/report.html) and [quadrature audit](../build/activation_comparison/quadrature_audit.json).

## What would meaningfully challenge the paper's tentative conclusion?

Look for all of the following:

- One internal scalar supports substantially better held-out prediction across several response types, through monotone interpretable curves.
- The internally informed small model closes a substantial portion of the small/large behavioral prediction gap. Define the comparison and success criterion before the next confirmatory evaluation; there is no universal AUROC cutoff for belief coherence.
- Broad predictive adequacy survives reasonable response-model changes. Compare with richer behavioral-only curves to distinguish external measurement from likelihood specification, and with patient-feature models to quantify incremental readout utility. Outperforming the patient-feature model is not required for coherent belief.
- The result is stable to reasonable fitting choices and is confirmed on genuinely new cases or another domain.

Such evidence would support the narrower conclusion that a useful one-dimensional belief-like state exists but is poorly measured by the chosen external responses. The same evidence-based state might also be reconstructed from patient features. It would not by itself establish a causal role for an activation direction or settle stronger philosophical claims about belief.

If richer behavioral-only curves recover performance without activations, the conclusion should instead emphasize response-model misspecification. If only one response family improves, describe a family-specific predictive signal. If no robust improvement appears, do not force a reversal of the paper's finding.

## Diagnostic follow-up: why can adding external evidence hurt?

The completed 4B joint fits motivate a narrower diagnostic before a larger
representation search. With spline curves, removing external evidence from the
same activation model improves test decision log loss by 0.01347 (95% paired
case interval 0.00111 to 0.02910) and ordinal-report NLL by 0.05022 (0.01004 to
0.11554). These are post hoc comparisons, conditional on the fitted models,
without multiplicity correction.

The [saved-prediction diagnostic](../build/joint_beliefs/full_experiment/4b/evidence_diagnostics/report.html)
locates the decision deterioration in a minority of large losses: adding reports
helps 59% of test patients but worsens the mean score. Several rare negative
threshold/cost responses receive especially confident positive predictions.
All sixteen raw responses for each of the two inspected high-loss cases match
the parsed dataset, and patient profiles match across prompts. Probability-bin
categories use dataset-specific boundaries; category 4 is consistent with a
15% report, so category numbers must not be interpreted as equal-width bins.

Response sparsity is a concrete contributor worth testing: `cost_fn19` and
`template_08_cost_fn4` each have zero negative responses among all 397
training-plus-validation cases, but one negative test response. The cost-ratio-2
question has just one training and one validation negative. Response curves can
therefore become very confident with little or no information about exceptions;
conditioning can sharpen those predictions further. This does not explain every
loss, particularly the ordinal-report effects, but motivates smoothing or
regularizing rare-response probabilities before interpreting failures as a lack
of shared belief.

The current fitting objective penalizes the probe weights but not the binary
response intercepts (or the spline's common vertical shift). For an all-positive
response channel, increasing that intercept improves the likelihood toward its
boundary optimum at probability one. Validation containing only positives does
not penalize that direction either. Early stopping keeps finite parameters but
does not supply information about the missing negative class. This is a concrete
statistical calibration issue in the present specification, not evidence against
an evidence-based belief in the language model.

A [frozen-model validation check](../build/joint_beliefs/full_experiment/4b/validation_evidence/report.html)
multiplies observed-response log likelihoods by a scalar while leaving the prior
and target likelihoods unchanged. No test cases are loaded or parameters
refitted. For spline decision prediction, quarter-weight evidence gives NLL
0.23267 versus 0.23697 at full weight and 0.23740 with no evidence. Downweighting
the numerical probability report alone also helps, so the symptom does not
require multiple external answers. However, full decision evidence improves
ordinal validation predictions, unlike the test result. This is a lead about
excessive influence, not an established universal failure or identified cause.

The next targeted comparison should distinguish:

1. **Likelihood calibration:** compare the current model with a development-tuned
   evidence weight and a response model allowing occasional atypical answers,
   including smoothing or regularization for nearly constant response channels.
   Check whether the large losses on rare responses shrink without losing the
   predictive benefit of typical responses.
2. **Dependence beyond one belief:** compare isolated evidence channels with
   groups of similar questions. Use model-based predictive checks that account
   for uncertainty in the shared latent belief; raw response correlations alone
   are not evidence against conditional independence.
3. **An inadequate shared scalar:** only after those checks, compare a modest
   structured extension with matched patient-feature and flexibility controls.
   Better prediction would not by itself establish a causal belief variable.

Choose the correction and its success criterion using development data. The
current validation cases already selected the original models, and the
diagnostics were motivated by inspected test results. Confirmation therefore
requires new cases. Preserve the completed experiment as the original
comparison rather than replacing it with a test-guided correction.

### Completed calibration follow-up (2026-11-08)

The first targeted step has now run across both sizes, both curve forms, and
behavior/activation/patient-feature models. See the
[calibration report](../build/joint_beliefs/calibration_followup/report.html) and
[implementation](../experiments/belief_calibration/README.md). This freezes the
learned models and selects normalized binary-response smoothing, evidence
tempering, or their combination on validation. It also includes an untuned
small probability floor. All choices were locked before test evaluation;
68 evaluations and 322,184 predictions passed numerical checks.

For 4B activations, the selected combined correction reduces decision log loss
from 0.27592 to 0.26744 with original curves and from 0.27948 to 0.27388 with
splines. Nominal paired improvement intervals are `[0.00012, 0.02028]` and
`[0.00155, 0.01056]`. However, the corrected activation-versus-behavior
decision-gain intervals include zero for both sizes and both curve forms.
Calibration reduces the earlier deterioration without establishing added
decision-predictive value over behavior alone.

Most 4B smoothing-only mean gains come from the three constant fitting
channels, with wide intervals crossing zero. The selected spline activation
combination instead uses half-strength evidence with no smoothing and improves
variable fitting channels too. Sparse-response handling and excessive evidence
influence are therefore distinguishable contributors in these diagnostics.
The numerical-report signal remains; 12B probability R² improves to about 0.89–0.90,
although its calibration NLL-gain intervals include zero.

This remains exploratory on the previously inspected test set. It does not
constitute a new model fit with response priors or independent confirmation.
The next stronger step is development-selected response regularization or
likelihood refinement followed by fresh-case confirmation, keeping patient-feature
utility separate from the question of shared belief structure.

## Later directions, after the joint model

### Direct internal scalar as a diagnostic

Retain the direct logistic probe as a cheap diagnostic of decodable behavioral information. Compare it with external behavior and an analogous raw-feature scalar. It is useful for distinguishing a weak internal signal from a poor fusion procedure, but standalone prediction does not establish a shared latent belief across the complete battery. The initial 4B version has already been run and is summarized above.

### Learn how reliable the internal measurement is

If joint fitting produces a useful shared signal but combining it with behavior remains problematic, test an activation-conditioned prior with a global reliability parameter:

$$
p(s\mid h)
=\frac{1}{Z(h)}p_0(s)
\exp\left[-\frac{(s-(w^\top h+b))^2}{2\sigma_h^2}\right].
$$

Small $\sigma_h$ makes the activation estimate influential; as $\sigma_h\to\infty$, the prior returns to $p_0(s)$. Normalize over $s$ and train through response likelihood or withheld-response prediction. Do not maximize an unnormalized density of the learned projection itself. Begin with one global reliability parameter, not a separate confidence network or per-case noise predictor.

This changes both the location and concentration of the effective prior. It is not simply adding random perturbations to activations at prediction time. Its initial purpose would be to improve measurement combination, not to replace the primary coherence test.

### Diagnose excessive trust in external evidence

If internal-only predictions consistently outperform combined predictions, vary the number of observed external responses and examine whether additional evidence helps. As a diagnostic, a validation-selected tempering parameter can reduce the influence of the behavioral likelihood:

$$
p(s\mid a_O,h)\propto p(s\mid h)
\left[\prod_{j\in O}p(a_j\mid s)\right]^\gamma,
\qquad 0\leq\gamma\leq1.
$$

This is a generalized posterior, not the original generative model. Improvement could indicate miscalibration, dependence among responses, or other likelihood misspecification. Do not interpret it automatically as evidence for a hidden belief.

### Stronger confirmation

After the predictive result is established, consider matched-checkpoint response collection, fresh-case or cross-domain confirmation, stability across neutral extraction prompts, and eventually interventions on the learned direction. These address progressively stronger questions and should not all be added to the initial fitting experiment.

## Practical and evidential constraints

- Reuse cached activations. Joint fitting and reliability experiments require no additional LLM inference for existing cases and extraction prompts.
- Continue to select layers using validation cases within the training partition, not fitted training performance or test outcomes.
- Keep extraction independent of the target decision, payoff, answer, and true outcome. Cache one case-level vector per layer.
- Preserve the original and augmented models as explicit nested comparisons. Verify $w=0$ identity, withheld-target invariance, and quadrature accuracy.
- Store residuals as float32: the pilot found that some Gemma coordinates exceed float16 range.
- The local activations come from 4-bit MLX checkpoints, while the paper's responses came from hosted inference. This limits claims about the exact same model state; matched-checkpoint confirmation is required for a strong interpretation.
- Activations are extracted from a neutral pre-generation prompt, whereas released decision responses can include reasoning. The current experiment tests a case-level predictive relationship, not a readout paired with the exact reasoning state that produced each answer. Matched-prefix measurements are a useful later check.
- The current 4B test set has already influenced the choice of follow-up methods. Treat further runs on it as exploratory. Nested resampling can assess robustness, but genuinely fresh cases or an untouched domain provide stronger confirmation after adaptive research decisions.
- Report uncertainty at the case level, and distinguish uncertainty conditional on a fitted model from variability in training, layer selection, and experimental design.

Recommended order: **jointly fit the full one-dimensional model; evaluate across the paper's response tasks; add the behavioral-only flexibility control; assess the model-size comparison; then consider reliability/noise and stronger confirmation.**
