# Binding experiment

Builds on a completed run of the main experiment (the *base run*). When the model attributes a belief
to PERSON and leaks its own evidence into it, three accounts predict different things:

- **Misbinding:** the model attaches content to the wrong owner. Making ownership explicit should remove
  the leak.
- **Curse of knowledge:** PERSON's evidence is represented correctly and still overridden. The leak
  should survive explicit ownership and an instruction to take PERSON's point of view.
- **Absence not read as ignorance:** a missing field is not taken to mean that PERSON lacks it. Stating
  the absence should remove the leak.

Every cell transforms a released CKD prompt (`second_order/binding.py`), so field order, names and
questions are identical across arms.

## Arms

A cell is (variant, binding, absence, block order).

| Arm | PERSON's evidence | Blocks | Absence |
|---|---|---|---|
| `partial_original` | release E_p (3 fields withheld) | release headers | implicit |
| `false_verified` | release E_f (2 fields edited) | release headers and "verified" cue | — |
| `false_neutral` | E_f | release headers, cue removed | — |
| `*_labelled` | | "Record X (PERSON's record)" … "(End of Record X)", "Record Y (your record)" | |
| `*_instructed` | | labelled, plus "Answer from PERSON's point of view … not your own." | |
| `*_explicit` | | | "Record X has no entry for …; PERSON does not know these values." |
| `strong_*` | E without age, diabetes and hypertension, built as the release builds E_p | | |

Every patient sees both block orders (`own_first`, `person_first`). A and B are shared by every arm with
the same own-belief version. Identical prompts are one item: a cell that reproduces a base-run prompt
reuses its archived answer, and the first three arms are exactly the chart-order follow-up's cells
(below), under the same item keys.

| Profile | Run directory | Models | Patients | Arms | Readouts |
|---|---|---|---|---|---|
| `pilot` | `runs/binding-pilot` | Qwen3 8B | 30 (fit half) | all | stated probability ×10, checks |
| `full` | `runs/binding-full` | Qwen3 8B, 14B | 150 | all | stated probability ×5; latent battery on five arms; 2% drift repeats; activations |
| `thinking` | `runs/binding-thinking` | Qwen3 8B, 14B, thinking on (12,288 tokens) | 150 | the six deciding arms | stated probability ×3 |
| `32b` | `runs/binding-32b` | Qwen3 32B | 150 | all | stated probability ×5 |

## Running

Run on a GPU host with the base run and its activation index in `runs/`, in the pinned environment that
`run_experiment.sh` installs. Prepare every plan from the base run; a plan runs only if its content
digest matches `second_order/binding_plans.json`. The parts, in priority order:

| Part | Run | What |
|---|---|---|
| `part1` | `binding-pilot` | the `pilot` profile |
| `part3a` | `binding-full` | the `full` profile; also generates the chart-order follow-up's items |
| `part2` | `20261121-101311-order-followup` | the chart-order follow-up: imports its answers and captures from part 3a, then exports and analyses |
| `part3b` | `binding-thinking` | the `thinking` profile |
| `part3d` | `binding-causal` | the causal stages on part 3a |
| `part3c` | `binding-32b` | the `32b` profile |

Every stage resumes from its cache, and later parts import earlier parts' answers by item key instead of
regenerating them. `second_order.package_run` builds each part's archives (`--what main` and
`--what activations`). Only each model's `index.parquet` and `meta.json` are read from the base run's
activations, so the small `_activations_index.tar` suffices. `generate --engine '<json>'` overrides vLLM
engine settings, and `SO_FAST_SEEDED_SAMPLING=1` swaps in a bit-identical replacement for vLLM's per-row
seeded sampler (`run_generation.patch_seeded_sampling`). Engine settings and the sampler are outside the
settings signature: they change speed, not answers.

Individual stages:

```bash
python -m second_order.binding {prepare,verify,status,import-cache,generate,capture,export} --run <run> ...
python -m second_order.binding_analysis --run <run> --model <model>
python -m second_order.binding_causal <stage> --run runs/binding-causal --source runs/binding-full --model <model>
```

## Analysis (`second_order/binding_analysis.py`)

Direct beliefs are the mean clipped logit over samples. Latent beliefs come from one pooled refit over
all latent cells, with an own-belief family and a PERSON family as in `fit_pooled`. For each method,
arm and order (and both orders pooled, and the patients whose check is right in both option orders),
the Methods statistics get a paired patient bootstrap; the order, binding and absence contrasts come
from the same draws. Outputs: `lambda_cells.csv`, `contrasts.csv`, `checks.csv`, `retention.csv`,
`drift.csv`, `lambda_cells_rules.csv`, `decisions.json` and `RESULTS.md`.

**Comprehension checks.** For a yes/no field, the partial check "Did the record shown to PERSON include
a value for FACT? A. Yes / B. No" invites a letter that echoes the field's value. Partial checks are
therefore scored from the conclusion of the reasoning, falling back to the letter where the reasoning
states none (`second_order/check_scoring.py`); `check_passed_letter` keeps the letter scoring for
comparison. False-setting checks ask for the value and are scored by the letter.

**Decision rules.** Every λ interval is a 95% Fieller set. A rule is *supported*, *refuted* (its interval
excludes the claim), *inconclusive* (too imprecise), or *undetermined* (an unbounded Fieller set, i.e. a
weak β_D).

| Rule | Cell | Criterion |
|---|---|---|
| H1, misbinding | `false_labelled` | λ in [−0.1, 0.1] in both orders; person-first − own-first in (−0.2, 0.2); labelling lowers the person-first leak (`false_labelled − false_neutral`, CI upper < 0) |
| H2, curse of knowledge | `partial_instructed_explicit`, check passed, orders pooled | λ lower bound > 0.15 |
| H3, absence | `partial_labelled` vs `partial_labelled_explicit`, orders pooled | λ(implicit) − λ(explicit) > 0.3 with CI lower > 0, both β_D intervals excluding 0, and λ ≤ 0.1 in `partial_instructed_explicit` |

The rules use their own 10,000-draw bootstrap, with the Monte Carlo standard error of each deciding
quantity from 10 disjoint batches; a verdict that changes within 2 standard errors is flagged
`near_threshold`. H1 is also reported against `false_verified`, and without the C, M and D answers reused
from the base run. H2 is also reported on all patients and with letter scoring. A contrast whose draws are
inestimable more than 1% of the time cannot decide a rule. The latent method covers H1 only; the `strong`
variant's rules are exploratory. `RESULTS.md` and `decisions.json` list anomalies: drift between reused
and new answers beyond 0.1 logit, arms losing more than 5% of C, M or D answers to the token limit, and
verdicts that depend on the scoring or the subset.

## Causal stages (`second_order/binding_causal.py`)

Run on HF transformers in float32 with TF32 matmuls and eager attention, one intervention per batch row,
with the shared prompt prefix computed once. The readout replaces the reasoning instruction with "Do not
explain. Output only valid JSON", forces the prefix `{"probability": 0.` and takes the mean clipped logit
over the exact two-digit answer distribution.

| Stage | What |
|---|---|
| `benchmark` | times one patient and the readout's sensitivity to batch shape; writes `benchmark.json` |
| `readout` | no-reasoning answers for every patient, and fit-half activations at the probe positions |
| `gate` | per cell, on the fit half: no-reasoning λ against the reasoning λ; a cell passes if its λ is bounded, the Fieller sets overlap, and a clear reasoning order effect keeps its sign |
| `probes` | kernel-ridge probes of PERSON's belief beyond the model's own (fit half), with cross-validated penalty; interventions use each probe's encoding pattern; a shuffled-target null; `lock.json` fixes directions, gate and config by hash |
| `patch` | activation patching from correctly bound donor cells; the fit half picks each position's layer, the test half is tested at it |
| `knockout`, `directions` | attention knockout of a record block (against a filler-block control) and ablation/steering along the locked directions (test half) |
| `report` | one pre-specified test per cell and position on the test half, with patient-bootstrap intervals |

Settings are in `runs/binding-causal/causal_config.json`, written with the defaults on first use; edit
it before `probes`. Every later stage refuses to run if the lock's hashes no longer match.

## Chart-order follow-up (`second_order/order_followup.py`)

A paired extension of the base run: all 150 patients see both block orders of C, M and D in three
settings (partial; false with the "verified" cue; false without it), with the full 31-output latent
battery and the comprehension check. `prepare` adopts the base run's
config and model revisions, and verifies every reused answer's prompt and settings hash; it writes an
immutable `plan.json` and copies the reused answers to `baseline_cache/`. `generate`, `capture` and
`export` add only the missing calls; `status` reports the budget without loading weights.
Run each stage per model in its own process so VRAM is released between stages. The exports
(`outputs/<model>_responses.parquet`, `<model>_matrix.parquet`, checks, retention, activation references)
mark absent answers `not_generated`; `binding_analysis --order-followup` analyses them with the
estimators above.

## Tests

```bash
python -m unittest tests.test_binding tests.test_check_scoring tests.test_order_followup
python -m unittest tests.test_binding_causal   # needs a Qwen3 tokenizer (Qwen/Qwen3-0.6B or SO_TEST_TOKENIZER)
```

`test_binding` checks that every arm changes only what it should, that the follow-up's cells get its
exact keys, the patient split, resume and cross-run import, and that a synthetic model with a different
planted λ per cell is recovered by both belief methods. `test_binding_causal` runs a tiny random Qwen3 in
float32: token positions decode to the intended text, no-op hooks change nothing, the two-digit readout
matches explicit sequences, patching every position reproduces the donor, the shared-prefix cache is
exact, and the lock refuses tampered directions.
