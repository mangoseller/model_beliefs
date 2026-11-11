# Local activation pilot

Uses the existing 500 Gemma 3 4B CKD cases and their released splits: 313 training, 84 validation, and 103 test. No new behavioral responses are collected.

The local activation source is `mlx-community/gemma-3-4b-it-4bit`, pinned at `93724907d4ed1745d2fe50baadf3b0b01a65abf2`. These are 4-bit local weights; the response source is the paper's hosted `google.gemma-3-4b-it`. This is a checkpoint-transfer pilot, not a verified same-inference-system experiment. A positive result needs matched-checkpoint confirmation.

At the user's request, the same protocol is repeated for Gemma 3 12B, using `mlx-community/gemma-3-12b-it-4bit` at revision `86cc6a8dedbc456dd0e4af01a9d09f396f77e558`. The released 12B split differs from the 4B split, so 12B cases are reassigned to the 4B split and all 12B baselines are freshly fitted. This allows paired comparison on the same 103 test cases. The released 12B parameters must not be used: their fitting data overlap the aligned test set. Larger-model artifacts are in `build/activation_pilot_12b`; pass `--larger` to each script.

Activation caches use float32. The initial float16 attempt overflowed on some residual coordinates; those caches were regenerated before fitting. No probe uses overflowed activations.

## Fixed evaluation task

Eight observed decisions (four thresholds and four cost framings) predict four betting decisions. All three families are represented in fitting data: this is held-out-case prediction, **not zero-shot framing transfer**. Reported probabilities anchor the latent scale during baseline fitting but are not supplied as prediction evidence.

The baseline uses the repository's shared-threshold likelihood and seven-knot prior. Its parameters are frozen while fitting the activation direction. The extension shifts latent log-odds by one linear projection of a decision-independent residual vector. The collection prompt contains only the patient profile and focal CKD proposition. It contains no answer, threshold, payoff, or ground-truth outcome.

Eight evenly spaced post-block layers and four L2 penalties (0.001, 0.01, 0.1, 1) are selected by validation conditional log loss. Activations are centered and normalized by a single layer-wide RMS scale estimated on fitting cases. This preserves direction geometry while making the penalty scale comparable. Probe optimization is deterministic L-BFGS from zero. The baseline is refit on train plus validation for the previously selected epoch count, then the selected probe is refit and test predictions are evaluated once.

Controls: activation-only predictions; a matched-penalty shuffled-activation refit (one shuffle, not a permutation significance test); and a raw-profile-feature shift selected using validation. The raw-profile control excludes the real CKD label and derived risk score. The paper's released, more flexible 16-output model is also evaluated on the same prediction task as a contextual comparator. It is not the nested baseline and used more response types during fitting.

Primary uncertainty estimate: paired bootstrap of the behavior-only minus augmented log-loss difference, resampling 103 cases rather than 412 individual decisions. This interval is conditional on the fitted models and does not quantify training-set or layer-selection variability.

Before any test evaluation, validation diagnostics showed that the shared-threshold baseline was substantially worse than the released flexible model (log losses 0.905 and 0.654). Therefore the same activation experiment is also run with separate intercepts and positive slopes for each decision, matching the paper's main binary-likelihood form. That refitted baseline uses the same thirteen fitting outputs as the shared-threshold pilot; it is distinct from the released sixteen-output fit. Both specifications use the same predeclared layer/penalty grid. This additional comparison was chosen on validation evidence, not test results.

## Reproduce

Run from the repository root, using the existing `latent-beliefs` environment. Additional packages are installed in `build/pilot_dependencies`, leaving the environment itself unchanged.

```sh
PYTHONPATH=build/pilot_dependencies:. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/activation_pilot/extract.py
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/activation_pilot/fit.py
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/activation_pilot/report.py
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/activation_pilot/fit.py --flexible
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/activation_pilot/report.py --flexible
```

The extraction expects the pinned checkpoint in `build/activation_pilot/model`. Per-case activation caches are resumable. The fitting script saves baseline parameters, normalization statistics, probe weights, layer search, selected configuration, per-decision test predictions, aggregate metrics, and numerical checks under `build/activation_pilot`.

Checks include zero-shift identity with the repository implementation, withheld-target invariance, and a 128-versus-256-point quadrature comparison. The fitted shared-threshold baseline includes the probability-report channel plus twelve parameterized binary decisions; the other three original output types are omitted for this pilot.

## Direct-probe follow-up

After inspecting the initial 4B results, the user requested a simpler directly trained internal probe. `direct.py` freezes the previously selected flexible-model layer and response curves, and fits a single shared scalar plus intercept to the four training betting responses. It uses ordinary binary logistic likelihood, with no latent integration or additional activation noise. L2 is selected from the same four values on validation cases. A direct raw-profile scalar with the same frozen response curves is the control. Run `PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python experiments/activation_pilot/direct.py`, adding `--larger` for 12B once its cached activations and flexible fit are available.

This follow-up reuses an already inspected test set and is explicitly exploratory. Its bootstrap intervals describe conditional sampling uncertainty, not independent confirmation of a method selected after seeing earlier results. The 4B internal scalar improves test log loss from 0.6662 (external behavior) to 0.6457 and mean AUROC from 0.5022 to 0.5984, but its advantage over the direct raw-profile control remains uncertain. Artifacts are in `build/activation_pilot/direct`.

## Scientific interpretation and proposed next experiment

The user clarified that the central question is whether the paper's weaker belief-model performance on small models reflects its specification or inability to measure the relevant latent state externally. The implemented runs are preliminary frozen-response-curve diagnostics, not a decisive test of that question. In particular, success at predicting four betting responses need not demonstrate a shared belief across the full response battery.

The next proposed experiment is to jointly fit the activation direction, latent prior, and the paper's main monotone response curves on all sixteen outputs. Retain a single effective latent log-odds variable, with a deterministic activation-dependent prior shift; do not add activation noise yet. Evaluate the paper's reciprocal report-to-decision and decision-to-report prediction tasks, plus withheld-output prediction, using the same internally informed scalar across tasks. Compare with both the original behavioral-only model and a richer behavioral-only monotone-spline model, as well as a raw-input-feature control. The shared-threshold specification is a stricter instruction-consistency test and should not be the primary basis for claims about belief coherence.

Both 4B and 12B diagnostic runs are complete. The 12B shared-threshold run is excluded from comparative conclusions because changing quadrature resolution from 128 to 256 shifts some predicted probabilities by as much as 0.342. A subsequent fixed-parameter audit through 2048 points confirms that this is a numerical integration issue; no new layer or penalty was chosen from test results. The flexible 12B model changes by at most 0.000327 at 256 points, with negligible aggregate log-loss changes through 2048. The combined report excludes the unstable specification and retains its raw artifacts for diagnosis.

The paired size comparison shows greater log-loss improvement for 12B than 4B, not the hypothesized smaller-model advantage. The direct raw-profile control is competitive with the direct internal probe, especially for 12B. These results do not establish a reversal of the paper's conclusion. The final comparison and fixed-parameter quadrature audit are under `build/activation_comparison`.

Improvement without changing response-curve forms would support an external-measurement limitation; improvement from richer behavioral-only curves would support a response-specification limitation. Broad, substantial gains closing the small/large model performance gap would be more persuasive than the modest betting-only gains observed so far. The joint sixteen-output experiment is proposed here, not implemented or run by these scripts.

## Token-position diagnostic

`extract_positions.py` reuses the exact neutral prompt and pinned local checkpoints,
and caches five fixed extraction rules at every post-block layer:

| Cache position | Definition |
|---|---|
| `ckd_end` | Final subtoken of CKD in the concluding proposition (`D` in these tokenizers) |
| `proposition_period` | Period completing the proposition |
| `user_end_of_turn` | User end-of-turn marker immediately after that period |
| `assistant_prefix_end` | Newline ending the assistant-generation prefix; original pilot position |
| `proposition_mean` | Float32 arithmetic mean across the whole concluding sentence, including its period |

Character offsets identify the concluding sentence and tokens independently of
responses and diagnoses. Token IDs must match both chat-template tokenization and
the original cache. Every extracted case checks its original-position vectors
against the old cache at every layer (maximum absolute error below 1e-4). The
first newly extracted case also checks the manual forward pass against the model.
Existing pilot artifacts are preserved.

```sh
PYTHONPATH=build/pilot_dependencies:. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -u experiments/activation_pilot/extract_positions.py --both --validate-only
PYTHONPATH=build/pilot_dependencies:. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -u experiments/activation_pilot/extract_positions.py --both
PYTHONPATH=build/pilot_dependencies:. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -m unittest experiments.activation_pilot.test_positions -v
```

`--both` queues 4B then 12B in separate processes to release model memory between
runs. `--limit 1` performs a first-case check; `--larger` chooses only 12B.
Metal/GPU access is required for extraction. Outputs go to
`build/activation_positions/{4b,12b}/`: `activations/*.npz`, `extraction.json`,
and live `progress.json`. Cache axes are **layer, position, hidden coordinate**;
arrays use float32. Each cache records token IDs, exact token spans, and a
provenance fingerprint. Writes are atomic; a file lock prevents duplicate runs.
Rerunning resumes validated caches. Changed provenance or invalid existing caches
cause an explicit failure rather than silently mixing results; use a new
`--output-root` for a changed extraction protocol.

After extraction, the development-only diagnostic is:

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -u experiments/activation_pilot/position_search.py
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -u experiments/activation_pilot/position_search.py --larger
```

This freezes the original flexible pilot's training baseline and jointly searches
five positions, eight layers, and four L2 values on the original 313/84 train/val
split. It repeats the complete grid with one fixed within-partition shuffle and
fits the raw-profile control. One shuffle is a diagnostic, not a permutation
significance test. All preprocessing uses training cases. Test rows are removed
before constructing response arrays or reading activation files. No test
predictions are produced. Output includes the search table, overall and
per-position selections, normalized probe parameters, validation predictions,
and withheld-target invariance check. Existing output directories are refused;
use `--output` for a distinct run. `--positions`, `--layers` (1-based), and
`--penalties` allow a reduced smoke check.

This implements the position diagnostic and its single-split development search.
Nested cross-validation, full-battery joint fitting, and fresh-case confirmation
are subsequent work, not results established by this script. Validation scores
are selection scores; any later reuse of the inspected pilot test set remains
exploratory. The local-versus-hosted checkpoint limitation still applies.

## Unpenalized-intercept follow-up

`affine.py` repeats the original flexible pilot with shift `x @ w + b`, where
`x = (h - mean) / scale` and only `w` receives the L2 penalty. A free intercept
makes this exactly equivalent to an affine readout of the uncentered residual
vector with the same scale. This is separate from the direct scalar follow-up:
the latent integration and eight observed decisions remain in the predictor.

```sh
PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python -u experiments/activation_pilot/affine.py --both
```

The script reuses the original token caches and frozen training/final flexible
baselines, searches the same eight layers and four penalties on validation, and
refits the selected probe on training plus validation. It also fits an affine
probe at the original selected configuration, an intercept-only correction,
an affine raw-profile control, and a matched-configuration shuffled control.
Original predictions are retained as comparators. Checks cover exact baseline
reproduction, affine equivalence under centering, withheld-target invariance,
and doubled-resolution quadrature. Paired intervals resample patients.

Artifacts go to `build/activation_pilot/affine` and
`build/activation_pilot_12b/affine`, including `report.html`, `results.json`,
`layer_search.csv`, fitted probes, and `test_predictions.csv`. Existing output
directories are refused. This is exploratory reuse of an inspected test set.
