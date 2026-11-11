# Calibration follow-up to the joint belief experiment

This isolates two potential contributors to overconfident predictions without
changing the learned scalar, layer, probe penalty, prior, or response curves.
It uses the existing 4B/12B, original/spline, behavior/activation/patient-feature
fits. The original experiment and its results remain unchanged.

For every binary response, the smoothing model replaces its conditional
probability with `(1 - epsilon) * p + epsilon / 2`. The corresponding normalized
likelihood is used both to condition on binary evidence and to score binary
targets. Probability and ordinal response likelihoods are unchanged. This
preserves monotonicity and allows occasional answers that the fitted curve
would otherwise consider nearly impossible.

Evidence tempering multiplies observed log likelihoods by `temperature`, leaving
target likelihoods and prior weights unchanged. Values below one define a
generalized posterior. Smoothing at temperature one remains a normalized
generative response model; tempering is a diagnostic correction.

## Design

- Fit calibration choices using the existing models trained on 313 cases and
  their 84 validation cases. No test rows enter this stage.
- Search epsilon in `[0, .001, .005, .01, .025, .05, .1]` and temperature in
  `[0, .1, .25, .5, .75, 1]`. Select smoothing alone, tempering alone, and their
  combination independently for each of the twelve fitted models.
- Minimize the equally weighted average of three validation mean NLLs:
  binary reports-to-decisions, numerical decisions-to-reports, and ordinal
  decisions-to-reports. This is a fixed weighted proper-score criterion;
  it prevents the thirteen binary outputs from dominating
  solely by their count. Report each component separately.
- Use one global pair for every output and task of a model. Within `1e-5` of
  the minimum, prefer smaller epsilon and then larger temperature.
- Include an untuned fixed-floor comparator, epsilon `1 / (n_fit + 1)`.
  It is a half-count shrinkage heuristic, not a full Bayesian fit. The final
  binary floor is `0.5 / 398`, reflecting 397 fitting cases.
- Lock all choices before applying them to the original final models fitted
  on 397 cases and scoring the same 103 test patients. No final models are
  retrained in this experiment.
- Include original and corrected activation-only ablations, reciprocal tasks,
  probability-only variants, and leave-one-output-out prediction.
- Check quadrature at 512, 1024, 2048, and if necessary 4096 points, preserving
  fitted prior knots. Selection must stabilize and group NLL changes must be
  below .001. Test prediction and individual NLL changes must be below .001.
- Reproduce each saved uncorrected model at its original evaluation resolution
  to within `1e-8` before testing the correction. Use 10,000 paired patient
  bootstrap draws for primary contrasts and decision-family effects.

The source and input/checkpoint hashes are recorded under
`build/joint_beliefs/calibration_followup`. The output contains 68 model/arm
evaluations and 322,184 prediction rows when complete.

## Commands

Run from the repository root with `PYTHONPATH=.` and the `latent-beliefs` Python
environment:

```sh
python -m unittest experiments.belief_calibration.test_model experiments.belief_calibration.test_run -v
python experiments/belief_calibration/run.py initialize
python experiments/belief_calibration/run.py select --size 4b
python experiments/belief_calibration/run.py select --size 12b
python experiments/belief_calibration/run.py lock
python experiments/belief_calibration/run.py evaluate --size 4b
python experiments/belief_calibration/run.py evaluate --size 12b
python experiments/belief_calibration/report.py
```

The two selection commands may run concurrently, as may the two evaluation
commands after locking. Do not run the same stage/size twice concurrently.
Completed per-model stages are reused. Preserve the initialized fitting source;
changing it after initialization causes verification to fail.

## Interpretation

This is an exploratory follow-up motivated by an already inspected test set;
validation also selected the original models. Intervals condition on the fitted
and selected models and do not adjust for multiple comparisons. No fresh-case
confirmation or new LLM inference occurs. A correction must eventually be
confirmed on new cases.

Improvements concentrated in constant fitting channels can reflect sensible
handling of rare exceptions without establishing stronger shared belief
structure. The report separates those channels from variable ones. Calibration
success also does not distinguish all forms of likelihood misspecification,
dependence, and latent-state inadequacy. Failure of a frozen-model correction
would not rule out retraining with response-level priors.

Patient-feature models remain an information benchmark. Matching them is
compatible with coherent beliefs formed from the patient evidence; incremental
readout utility is a different question from whether beliefs exist.

## Completed results, 2026-11-08

The [full calibration report](../../build/joint_beliefs/calibration_followup/report.html)
contains all 68 evaluations and 322,184 predictions. All integration checks
passed, and all twelve unmodified models reproduce their original saved scores
at the original quadrature resolution. Six tests verify model identities,
normalization, target exclusion, missing evidence, and selection behavior.

Decision log loss from all three reports, lower is better:

| Size | Curves | Activation, original | Activation, selected combined correction | Matched behavior-only |
|---|---|---:|---:|---:|
| 4B | Original | 0.27592 | 0.26744 | 0.26761 |
| 4B | Spline | 0.27948 | 0.27388 | 0.27212 |
| 12B | Original | 0.18976 | 0.18822 | 0.18797 |
| 12B | Spline | 0.18901 | 0.18677 | 0.18583 |

The 4B activation-model combined-correction improvements have nominal 95%
paired intervals `[0.00012, 0.02028]` and `[0.00155, 0.01056]`. Corrected
activation-versus-behavior decision-gain intervals include zero in every cell.
Thus calibration reduces the previous deterioration but does not establish
additional decision-predictive value over behavior alone.

All activation and patient-feature models selected temperature 0.5; behavior
models retained temperature 1. The combined search can select epsilon zero.
In particular, the 4B spline activation correction uses no smoothing, and its
improvement extends to variable fitting channels. Smoothing-only mean gains
for 4B are concentrated in the three constant fitting channels and have wide
intervals crossing zero. These are distinct diagnostics of sparse-response
handling and excessive evidence influence.

The numerical-report prediction signal remains. For 12B, activation-model
probability-report R² rises from 0.865 to 0.889 with original curves and from
0.864 to 0.899 with splines; the corresponding calibration NLL-gain intervals
still include zero. The uncertainty and exploratory qualifications above apply
to every result. No test-based parameter choice or original-model refit occurred.
