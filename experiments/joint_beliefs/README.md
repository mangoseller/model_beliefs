# Joint sixteen-output belief experiment

This implements the first joint model proposed in
[`plans/testing_internal_belief_coherence.md`](../../plans/testing_internal_belief_coherence.md).
It reuses cached CKD activations. No LLM inference or new responses are required.

For every case, `s = logit(z_q) + x @ w` is the effective latent log-odds.
The same shift enters all thirteen binary decisions, both ordinal reports, and
the numerical probability report. The probe has no intercept. The seven-knot
prior and all response parameters are optimized jointly with the probe. The
probability-report mean remains `sigmoid(s)`, anchoring the scale. Integration
uses the original latent grid and its prior weights, not a shifted integration
measure. Both original linear and monotone-spline response curves are supported.

`model.py` provides marginal likelihood, evidence-masked posterior prediction,
per-target predictive log probabilities, and fixed-parameter quadrature changes.
Changing quadrature resolution preserves the fitted prior's knot locations.

## Run one candidate

From the repository root:

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -m unittest experiments.joint_beliefs.test_model -v
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/benchmark.py
```

Defaults: Gemma 4B, post-block layer 25, L2 penalty 1, 128 quadrature points,
float64 CPU arithmetic, two PyTorch threads, 313 training cases and 84 validation
cases. Layer and penalty are fixed from the earlier pilot for this timing run;
they have not been selected for the new joint objective. Test rows are excluded
before loading activations and constructing response arrays. No test predictions
are produced.

The benchmark first prepares a full sixteen-output behavioral baseline on the
training cases, selecting its checkpoint on validation cases. This is timed
separately because baseline preparation can be shared across candidate fits.
It does not initialize from a released train-plus-validation fit.

The candidate starts from that baseline and zero probe weights. Full-batch Adam
uses learning rate 0.02 for the behavioral parameters and 0.001 for the probe,
with joint gradient-norm clipping at 10. The objective is mean per-case negative
joint log likelihood plus `penalty * sum(w**2)`; it is not divided by sixteen.
Probe features use training-only centering and one layer-wide RMS scale.

Both stages have a 12,000-epoch cap and stop after 250 epochs without a validation
NLL improvement of at least 1e-5. Candidate checkpoint selection uses unpenalized
validation joint NLL across all sixteen outputs and includes the initial
zero-probe checkpoint. An epoch includes a full training forward/backward update
and a validation forward pass. The reported fit time includes optimizer-loop
bookkeeping and progress logging, but excludes preparation and final diagnostics.
Early stopping is a validation criterion, not proof of optimization convergence.

Options include `--larger`, `--layer`, `--penalty`, `--response-curve`,
`--quadrature`, `--epochs`, `--patience`, `--threads`, and `--output`.
Use a distinct `--output` directory for every run; existing artifacts are never
overwritten. For 12B, use `--larger --layer 28` and a new output directory. Its
splits are aligned to the 4B split, and the baseline is freshly fitted.

## Artifacts and checks

Default artifacts: `build/joint_beliefs/benchmark_4b_layer25_l2_1/`.

- `config.json`: exact configuration, package versions, source/data hashes.
- `case_splits.csv`, `normalization.npz`: case membership and training-only preprocessing.
- `baseline.pt`, `baseline.json`: reusable baseline initialization and preparation time.
- `candidate.pt`, `candidate_history.csv`, `candidate_timing.json`: best checkpoint,
  per-epoch diagnostics, elapsed time, best epoch, stopping reason, and throughput.
- `validation_predictions_summary.csv`: reciprocal and leave-one-output-out
  validation NLL and MSE, with continuous, binary, and ordinal outputs identified.
- `benchmark.json`: consolidated timings, zero-shift identity, withheld-target
  invariance, and a doubled-resolution quadrature audit on validation cases.

Synthetic tests verify zero-shift equivalence against the repository likelihoods
and predictions for both response-curve forms, missing-data handling, shared
activation influence across every output, finite gradients and a finite-difference
probe-gradient check, withheld-target invariance, and preservation of parameters
when changing quadrature. The benchmark repeats identity and leakage checks on
actual validation cases. Its quadrature flag requires NLL change and prediction
changes to be below 0.001; ordinal predictions are measured in category units.

This is a single-candidate timing benchmark, not a completed model-selection or
scientific comparison. It does not run the layer/penalty grid, raw-feature and
shuffled controls, test-set evaluations, or fresh-case confirmation. The existing
quantized-local-versus-hosted-response limitation still applies.

## Measured benchmark, 2026-11-08

On the local Apple M1 Pro (16 GB RAM), using the defaults above:

| Component | Measured elapsed time |
|---|---:|
| Behavioral baseline preparation | 33.66 seconds |
| One joint candidate fit | 32.57 seconds |
| Final validation diagnostics | 0.85 seconds |
| Total, including data preparation | 67.81 seconds |

The candidate stopped by validation patience after 1,111 epochs, restoring epoch
861. Average throughput was 29.31 seconds per 1,000 epochs. The 12,000-epoch cap
was not reached; at this throughput a capped candidate would take approximately
5.9 minutes. These are measurements of one 4B configuration, not a guarantee for
other layers, penalties, response-curve families, or 12B.

Validation joint NLL decreased from 4.1164 at initialization to 3.4084. This is
the checkpoint-selection metric, not independent evidence of generalization.
The zero-shift maximum likelihood error was 2.13e-14; withheld-target prediction
change was exactly zero. Doubling quadrature from 128 to 256 changed validation
joint NLL by 0.000206 and predictions by at most 0.000410 across the evaluated
tasks, passing the predeclared 0.001 tolerances. All four synthetic tests passed.

Detailed measurements and configuration are in
[`benchmark.json`](../../build/joint_beliefs/benchmark_4b_layer25_l2_1/benchmark.json).

## Full experiment

The full run completed on 2026-11-08. See the
[combined report](../../build/joint_beliefs/full_experiment/report.html),
[4B report](../../build/joint_beliefs/full_experiment/4b/report.html), and
[12B report](../../build/joint_beliefs/full_experiment/12b/report.html).
All 20 model/ablation evaluations passed selected-fit and final-prediction
numerical checks and target-leakage checks, producing 94,760 prediction rows.

| Size | Curves | Probability-report R²: behavior | + activation | + patient features | Decision log loss: behavior → + activation |
|---|---|---:|---:|---:|---:|
| 4B | Original | 0.157 | 0.653 | 0.648 | 0.268 → 0.276 |
| 4B | Spline | 0.157 | 0.669 | 0.647 | 0.272 → 0.279 |
| 12B | Original | 0.632 | 0.865 | 0.887 | 0.188 → 0.190 |
| 12B | Spline | 0.631 | 0.864 | 0.876 | 0.186 → 0.189 |

Probability reports are predicted from the thirteen decisions; decisions are
predicted from all three reports. The larger probability-report NLL gain for 4B
relative to 12B has a positive paired interval under both specifications, but
neither shows aggregate decision-log-loss gains. Richer behavioral-only curves
do not clearly improve the primary scores. These patterns motivate calibration
and broader-coherence diagnostics rather than a conclusion that beliefs are absent.

The run took 330.6 minutes of wall time, including a documented 116-minute
low-power sleep and earlier shorter sleeps. Saved fit timers sum to 324.2
worker-minutes; workers overlap and discarded interrupted work is excluded.
[Fit timings](../../build/joint_beliefs/full_experiment/fit_timings.csv) and
[run summary](../../build/joint_beliefs/full_experiment/report_summary.json)
record these separately. No new LLM inference was required.

`run.py` implements a resumable four-cell experiment: 4B and 12B crossed with
original response curves and monotone splines. Each cell fits a behavioral
baseline, searches eight layers and four penalties for the activation model,
searches four penalties for the patient-feature control, and fits one shuffled
activation control at the selected activation layer and penalty. This gives
128 activation candidates, 16 patient-feature candidates, four shuffled fits,
and four initial behavioral baselines, before numerical repairs and final refits.
An activation-only ablation uses the fitted activation model without external
test evidence and requires no additional fitting.

The patient-feature comparison measures incremental predictive utility of an
activation readout. It is not a necessary test for the existence of coherent
beliefs: an evidence-based belief can be reconstructed equally well from its
inputs. A successful shared-scalar patient-feature model could itself support
behavioral coherence, while leaving the internal representation unidentified.

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py initialize
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py search --size 4b
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py search --size 12b
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py lock
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py final --size 4b
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py final --size 12b
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/report.py
```

The two size searches may run concurrently, as may the two final stages after
locking. Do not run the same size/stage concurrently with itself. Completed
candidate checkpoints are reused on restart. The `lock` stage requires all four
cells to finish selection, and `final` verifies the selection and protocol hashes
before accessing test cases. Source snapshots and the protocol are saved under
`build/joint_beliefs/full_experiment/`.

Independent cells can also be searched concurrently with `search --size 12b
--curve linear` and `search --size 12b --curve monotone_spline`. Do not overlap a
whole-size search with a cell search for that size. The running experiment moved
the remaining 12B cells into separate workers after 4B completed; completed
checkpoints were reused and the interrupted candidate was restarted. This
operational change is recorded in `parallel_cells.json`, without changing the
training protocol.

Once a cell's behavioral baseline exists, its predeclared raw-feature candidates
can be prepared concurrently with its activation search:

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/prefit_controls.py --size 12b --curve monotone_spline
```

Both workers must use the current runner, which holds an exclusive per-candidate
file lock before checking or writing completion. The search consumes the same
cached candidate artifacts and retains sole responsibility for selection. This
schedule was used for the last 12B cell after 24 activation candidates completed;
the short interrupted candidate was restarted and its partial history preserved.
`parallel_controls.json` records the change. Fitting settings did not change.
After those controls finished, `--activation-tail` reused the helper for the last
layer at the final two predeclared penalties, with the same locks and artifacts.

At the user's request for results as they become available, rolling evaluation
is also supported. A completed cell can be locked and evaluated while the other
predeclared searches continue unchanged:

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py lock --size 4b --curve linear
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/run.py final --size 4b --curve linear
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/partial_report.py --size 4b --curve linear
```

The partial report can be refreshed as control predictions finish. It requires
completed behavioral and activation predictions and a locked selection. The
operational amendment is recorded in `rolling_evaluation.json`; the original
search runner is archived as `source/run_before_rolling_evaluation.py`. Fitting,
selection, and numerical rules are unchanged. Do not run a cell's rolling final
stage concurrently with its whole-size final stage.

Full evaluation includes the paper's report-to-decision and decision-to-report
tasks, probability-only variants, and leave-one-output-out prediction. The three
reports are numerical probability, Likert, and probability quintile. All thirteen
binary decisions serve as decision evidence. All tasks share each model's one
fitted direction. Models predict the released model responses, not clinical
ground-truth labels.

Selected models receive a training/validation quadrature audit. If mean joint NLL
changes by more than 0.001 at doubled resolution, the same selected configuration
is refitted at progressively higher resolution (up to 1024 points), without
reselecting its layer or penalty. Final evaluation separately doubles quadrature
up to 2048 while keeping parameters fixed; both prediction and individual
predictive NLL changes must be below 0.001. Failed audits are flagged explicitly.

Final behavioral models are fitted on the 397 train-plus-validation cases for the
validation-selected number of epochs. Activation and control models start from
the refitted behavioral model and train for their selected joint epoch counts.
All preprocessing is re-estimated on those 397 cases only. No layer, penalty,
epoch count, or fitted parameter is selected using test performance.

The report includes separate proper scores for binary, continuous probability,
and ordinal targets; concordance/AUROC; R²; calibration bins; per-output results;
decision-family effects; paired case-bootstrap contrasts; and paired model-size
gain comparisons. Intervals use 10,000 draws of the same 103 case IDs, retaining
all outputs and both model sizes in each draw. They are conditional on the fitted
models and unadjusted for multiple comparisons. One shuffled fit is a sanity
check rather than a permutation significance test. All findings remain
exploratory because this test set influenced the earlier research decisions.

Additional verification:

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -m unittest experiments.joint_beliefs.test_model experiments.joint_beliefs.test_run experiments.joint_beliefs.test_report -v
```

`test_run` checks the batched evaluation against the evidence-masked model API,
including the ablation and ordinal category probabilities, and verifies training
preprocessing and shuffle partition isolation. `test_report` checks case pairing,
gain direction, uncertainty signs, AUROC, and R² against analytically known cases.

## Evidence-fusion diagnostics

At the user's request to investigate why adding external responses can worsen
predictions, two post hoc diagnostics use the existing fitted 4B models:

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/evidence_diagnostics.py --size 4b
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/joint_beliefs/validation_evidence.py
```

The [error-localization report](../../build/joint_beliefs/full_experiment/4b/evidence_diagnostics/report.html)
compares saved predictions as external evidence is added. It breaks down paired
NLL changes by output kind, family, target, and patient. The spline model's
decision deterioration is driven by a minority of large losses despite helping
59% of patients. All 32 raw responses for the two inspected high-loss cases
match the dataset; their patient profiles are identical across the sixteen
prompts. Probability-bin categories use dataset-specific risk boundaries, not
equal 20-percentage-point bins.

The diagnostic also records negative-response counts by split. Two 4B cost
questions have no negative training or validation responses but one test negative
each. Such sparse channels offer a concrete reason to investigate smoothing and
calibration of rare responses; they do not explain every observed loss.

The [validation sensitivity report](../../build/joint_beliefs/full_experiment/4b/validation_evidence/report.html)
loads no test rows and refits no parameters. It multiplies observed-response log
likelihoods in the posterior by weights 0, 0.1, 0.25, 0.5, 0.75, 1, and 1.5,
keeping target likelihoods and priors unchanged. It also isolates evidence
families. Reducing evidence weight helps validation decision log loss and
numerical-report density scores; ordinal validation predictions still benefit
from full evidence. This supports investigating excessive influence but does
not establish a single cause of test deterioration. All integration checks pass,
and weight-1 predictions match the original evaluator.

Both diagnostics were motivated by inspected test results. Validation cases
also selected the original models. These analyses are descriptive, do not alter
the main experiment, and do not establish independent confirmation. A follow-up
should choose a correction on development data and evaluate on new cases.
