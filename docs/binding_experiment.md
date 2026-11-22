# Binding vs inhibition: the final experiment

Follows run `20261121-101311`. The question is whether leakage of the model's own knowledge into the belief it
attributes to PERSON comes from **misbinding** (content bound to the wrong owner), a **curse of knowledge**
(PERSON's evidence is represented correctly and still overridden), or **absence not read as ignorance** (a
missing field is not taken to mean PERSON lacks it). The plan, hypotheses and pre-registered decision rules are in
`PLAN_final_experiment.md` (kept with the run-20261121-101311 notes); this file is how to run it.

## Parts, in priority order (one GPU)

`bash run_binding_experiment.sh all` runs them in the order below. Each part produces its own build in `results/`:
`partN_<run>.tar.gz`, plus `partN_<run>_activations.tar` when it captured any, each with a `.sha256`. Every
stage resumes from its cache. Later parts import earlier parts' answers and activation captures by exact item
key instead of regenerating them.

| part | run directory | what | new calls | rough H200 time |
|---|---|---|---|---|
| 1 | `runs/binding-pilot` | Qwen3 8B, 30 fit-half patients, all 14 arms, stated probability ×10 + checks | 23,940 | 0.4 h |
| 3a | `runs/binding-full` | Qwen3 8B + 14B, 150 patients, all arms; latent battery on the 3 follow-up arms + both labelled arms; 2% drift; activations. Generates the order follow-up's items too, under its exact keys | 148,900 (8B, after the pilot's) and 161,440 (14B) | 6.5 h |
| 2 | `runs/20261121-101311-order-followup` | Astra's follow-up as prepared (both models, latent battery): imports every answer and capture from 3a, then exports and analyses | about 0 | under 0.1 h of GPU |
| 3b | `runs/binding-thinking` | thinking mode (8B + 14B, 12,288-token limit), the six deciding arms, ×3 | 21,600 per model | 1.5–3.5 h |
| 3d | `runs/binding-causal` | session 2 on 3a: benchmark, readout, gate, probes and lock, patching, knockouts, directions | forward passes only | 1–1.5 h |
| 3c | `runs/binding-32b` | Qwen3 32B (revision pinned), all arms, ×5 | 65,140 | 3–3.5 h |

Run order: 1, 3a, 2, 3b, 3d, 3c. About 14 h of wall-clock on one H200. Generation times scale the previous run's measured throughput (8B
9,800 and 14B 6,300 output tokens/s). Thinking-mode answer length is the largest unknown. The causal estimate
scales a benchmark on Qwen3-0.6B by FLOPs; the `benchmark` stage measures it on the H200 itself before part 3d's
work starts (it needs only the 3a plan, so it can also be run before part 1).

### Speed (none of these changes a prompt, a sampling setting, a seed or an estimator)

- **Full vLLM batches.** Generation submits the config's batches (4,096 prompts per call, as in the baseline
  run), so vLLM keeps its full concurrency; an interrupted batch is regenerated identically on resume (every
  item is seeded from its key).
- **Results before captures.** Each part's export, analysis and main build start as soon as its generation ends;
  the activation captures run on the GPU meanwhile and get their own build. Nothing downstream needs them.
- **Overlap.** The GPU only ever runs one thing. Each part's CPU-only stages (export, latent refits, analysis,
  packaging; thread-limited by `ANALYSIS_THREADS`) run in the background while the next part generates. Part 3d
  waits only for part 3a's analysis.
- **Reuse.** Part 3a imports the pilot's answers. Part 2 imports its answers and activation captures from 3a:
  all 57,000 of its new items per model are in 3a's plan with identical keys and prompt hashes.
- **`run_fast.sh`** (how part 3a onwards ran): `run_binding_experiment.sh` with CUDA graphs captured for decode
  batches up to 1,024 (vLLM 0.8.5 stops at 512; `max_num_seqs` is 1,024) and `SO_FAST_SEEDED_SAMPLING=1`, which
  replaces vLLM's per-row seeded noise loop with one that draws bit-identical noise
  (`run_generation.patch_seeded_sampling`). Neither is in a settings signature.
- **vLLM engine.** `ENGINE_<MODEL>='{"max_num_batched_tokens": 65536}'` overrides engine settings for one
  model's generation. They are not in the settings signature (checked), so they change speed only. Last run's
  throughput is in its tuning record, and the pilot's log prints out tok/s.
- **Causal stages**: TF32 on by default; batches of 64 (halved automatically on out-of-memory); interventions
  share the prompt prefix (computed once, only the tokens from the first affected position are re-run); the
  second digit is one tree-masked pass over all candidate first digits instead of ten; no-intervention readouts
  are left-padded batches; captures keep only the requested positions. Every one of these is tested to be exact
  in float32. `benchmark` also reports how far TF32 moves a readout between batch shapes on the H200.

## Arms

A cell is (variant, binding, absence, order); every cell is built by transforming a released prompt, so
field order, names and questions never change between arms (`second_order/binding.py`).

| arm | PERSON's evidence | blocks | absence |
|---|---|---|---|
| `partial_original` | release Ep (3 withheld) | release headers | implicit |
| `false_verified` | release Ef (2 edits) | release headers + "verified" cue | — |
| `false_neutral` | Ef | release headers, cue removed | — |
| `*_labelled` | | "Record X (Olga's record) ... (End of Record X ...)", "Record Y (your record):" (ownership only; that Olga has not seen Record Y is the instruction arm's) | |
| `*_instructed` | | labelled + "Answer from Olga's point of view ... give Olga's estimate, not your own." | |
| `*_explicit` | | | "Record X has no entry for age, ... Olga does not know these values." |
| `strong_*` | E minus age, diabetes, hypertension (built from D exactly as the release builds Ep) | | |

Both block orders for every patient. A and B are shared by every arm with the same own-belief version.
The first three arms are Astra's: their prompts and item keys are identical to `order_followup`'s plan.

## Analysis (`second_order/binding_analysis.py`)

Direct beliefs (mean clipped logit over samples) for every arm; latent beliefs from one pooled refit across all
latent-arm cells (an own-belief family and a PERSON family, as in `fit_pooled`). Per method, arm and order (and
both orders stacked, and the patients right on both option orders of the cell's check): the Methods statistics
with a paired patient bootstrap; contrasts (order, binding, absence) from the same draws. `--order-followup`
analyses Astra's run with the same estimators.

Decision rules (`decisions.json`, `RESULTS.md`): every lambda interval is the 95% Fieller set. Each rule is
**supported**, **refuted** (its interval excludes the claim) or **inconclusive** (too imprecise to tell), and
**undetermined** when a Fieller set is unbounded (weak beta_D).

- **H1** (misbinding): `false_labelled` lambda inside [-0.1, 0.1] in both orders, the person_first - own_first
  difference inside (-0.2, 0.2), and labelling lowers the person_first leak (false_labelled - false_verified
  CI upper < 0), so the leak it removes must have existed.
- **H2** (curse of knowledge): the strictest partial cell (`*_instructed_explicit`), check passed, orders
  pooled: lambda lower bound > 0.15.
- **H3** (absence is not read as ignorance): labelled partial, orders pooled: lambda(implicit) - lambda(explicit)
  > 0.3 with CI lower > 0 (both cells' beta_D intervals excluding 0), and the strictest cell's lambda <= 0.1.

The latent method covers H1 only (its arms are the follow-up's and the two labelled ones); H2 and H3 are decided
on the stated probabilities.

Caveat: the check-passed subsets are not equally selective across arms. In the explicit-absence arms the check
is answerable from the absence sentence, and the strong variant's checks are about 77% "withheld". Compare the
check-passed lambda with the all-patients lambda of the same cell rather than across arms.

The pilot reports retention under the new prompts, check accuracy per arm, each variant's manipulation size
(median |x|) and the lambda interval widths projected to 150 patients.

## Binding amendment 1 (2026-11-22, during part 3a's generation: before any full-run analysis or causal stage)

An audit of the pilot against this design found the following; each change is in the code, and the result
tables keep the plan's version beside the amended one, so either can be read. None changes a prompt, a sampling
setting, a seed or the GPU work of session 1.

- **The partial checks are scored from the reasoning.** The check "Did the record shown to NAME include a value
  for FACT? A. Yes / B. No" is answered by the value when FACT is a yes/no field: the model reasons correctly
  and then picks the option that reads like the field's value. In the pilot, shown checks of a field whose value
  is "No" were 43% right by the letter (100% for every other field), and the echo also scored some misread
  withheld fields as right. `second_order/check_scoring.py` reads the conclusion of the reasoning instead (the
  letter where none is found; 99.4% agreement with the letter on the checks it is not confounded on; of 40
  hand-checked confounded checks, 36 right before its last fixes, and 40 of a fresh 40 after). `check_passed` uses it;
  `check_passed_letter` is the plan's scoring. H2 is decided on `check_passed`, and reports the all-patients and
  letter versions; a verdict that differs between them is flagged. False-setting checks ask for the value and
  are scored by the letter, as before.
- **The rules get their own 10,000-draw bootstrap** (CPU only; `lambda_cells_rules.csv`), with the Monte Carlo
  s.e. of every deciding quantity from 10 disjoint batches; a verdict that changes when one moves by 2 s.e. is
  flagged `near_threshold` / not robust. (With 2,000 draws the pilot's H2 verdict flipped between seeds.)
- **H1's reduction clause is decided on false_labelled - false_neutral** (person_first). The plan's
  false_labelled - false_verified also removes the cue and changes the own-belief pair (A_F, B_F against A,
  B_F_neutral); it is reported beside it. H1 is undetermined if either unlabelled person_first cell's beta_D
  interval includes 0. H1 is also recomputed without the C/M/D answers reused from the base run (another
  session; `sensitivity_new_answers_only`), and a difference is flagged.
- **The strong variant's H2 and H3 are exploratory** (not pre-registered): reported as `[strong, exploratory]`.
- **Contrasts** whose bootstrap draws are inestimable more than 1% of the time cannot decide a rule.
- **Anomalies** (RESULTS.md, decisions.json): drift between reused and new answers beyond 0.1 logit, arms losing
  more than 5% of C/M/D answers to the token limit (relevant to thinking mode), and the flags above.
- **Session 2:**
  - the readout is the mean clipped logit over the two-digit answer distribution, as session 1's mean clipped
    logit over samples (was the logit of the expected probability; `readout_statistic`);
  - the gate compares Fieller sets on both sides (the reasoning side was a percentile interval);
  - patching donors and w_attr training cells must be correctly bound: fit-half lambda <= 0.15 with and without
    reasoning (`bound_max_lambda`; the pilot's partial_labelled_explicit|own_first, 0.51, and
    false_verified|own_first, 0.25, were defaults);
  - 3.2 tests a knockout only where the fit-half no-reasoning lambda is >= 0.3 (`knockout_min_lambda`), else
    "not applicable";
  - the filler block sits between the two records, and its control masks as many filler tokens as the knocked
    record has, the ones nearest it (it was a fixed 135-token block at the start of the prompt);
  - probe targets (l_A, l_B) are the no-reasoning readouts the activations come from (`probe_targets`);
  - the null permutes patients, not rows; each patch sweep uses its own unpatched row;
  - the benchmark reports how far the precision setting moves a readout between forward passes, in logits,
    against `patch_min_gap`; ablation at the readout positions is refused; the resample count is locked.

## Session 2 (`second_order/binding_causal.py`)

Runs in float32 with TF32 matmuls (in bfloat16 the batch shape alone moves a readout by ~0.03), eager attention,
one forward per batch of interventions (one row each), with the shared prompt prefix computed once. Readout: the reasoning
instruction becomes "Do not explain. Output only valid JSON ...", the answer prefix `{"probability": 0.` is
forced, and the belief is the exact expected value over two digits. Stages:

- `benchmark`: times one patient, extrapolates, and writes only `benchmark.json`.
- `readout`: no-reasoning answers for all patients (each distinct prompt once), plus fit-half activations at the
  probe positions.
- `gate`: on the fit half, no-reasoning lambda against session 1's reasoning lambda. A cell enters Phase 3 if its
  no-reasoning lambda is estimable (bounded Fieller set), the intervals overlap, and, where the reasoning order
  effect is clear, the no-reasoning one has its sign (`apply_gate`).
- `probes` (fit half): kernel ridge for PERSON's belief residualised on the model's own (l_B beyond l_A), with
  penalty and lock score cross-validated by patient. Interventions use each probe's encoding pattern,
  Cov(activation, prediction) in raw space, not its decoder weights. The null is the encoding patterns of probes
  trained on shuffled targets. The lock may place the directions only at `lock_positions` (the PERSON-name token),
  not at the answer or generation start, which carry the upcoming answer itself. `lock.json` is written once,
  with the hashes of the directions, the gate and the config; every later stage refuses to run if any changed.
- `patch` (both halves): each sweep carries its own unpatched row. The fit half picks each position's layer; the
  test half is tested at it.
- `knockout`, `directions` (test half): ablation at the locked probe position, and steering there too. Every
  per-patient stage appends each patient's rows as it finishes and resumes after the last.
- `report`: one pre-specified test per cell and position, on the test half, with patient-bootstrap intervals; a
  lambda whose beta_D interval includes 0 makes its rule undetermined. No correction across cells.
  - **3.1**: per non-answer position, at the layer the fit half picked, median gap closed (undefined below 0.1
    logit) with CI lower bound >= 0.7;
  - **3.2**: per `encoding` and `update` knockout and cell, at every layer (start 0), lambda drop beyond the
    filler control >= 0.3 with CI above 0. The `readout` knockout is a positive control, since it removes leakage
    under any hypothesis;
  - **3.3**: ablating w_attr in C, M and D changes lambda by >= 0.3 (CI excluding 0, beyond the null's 95th
    percentile) while ablating w_rec changes it by < 0.1. Lambda with the references unintervened is also
    reported.

  Layer and start sweeps are reported but not tested. Settings are in `runs/binding-causal/causal_config.json`
  (written with the defaults on first use; edit before `probes`).

## Tests

    python -m unittest tests.test_binding tests.test_binding_causal

`test_binding`: every arm changes only what it should (field lines, order, headers, cue, absence sentence,
instruction); the strong variant matches the release's construction; the follow-up's cells get its exact keys;
items are unique and correctly sourced; strong checks are keyed from their own records; split and pilot
strata; the config merge leaves every existing model entry unchanged; the synthetic model with a different
planted lambda per cell is recovered by both belief methods, contrasts and check subsets; resume and
cross-run import never regenerate; packaging; Astra's own plan, generated and exported by its own code,
recovered through the adapter. `test_binding_causal` (needs a Qwen3 tokenizer): on a tiny random Qwen3 in
float32, token positions decode to the intended text; no-op hooks change nothing; the two-digit readout equals
explicit sequences; patching every position reproduces the donor; knocked-out attention weights are exactly 0
from the start layer only; ablation pins the projection and steering adds the vector; the shared-prefix cache
is exact for every intervention; batch rows are independent; every stage runs end to end, the lock is
write-once, and tampering with the directions is refused.

## On the GPU host

On the instance that ran 20261121-101311 (its run directory `runs/20261121-101311` is the base):

```bash
git pull
nohup bash run_binding_experiment.sh > binding.out 2>&1 &
```

The script installs the pinned environment as `run_experiment.sh` does. It then prepares every plan from the
archived run, which takes a few minutes and is skipped on resume, and refuses to start unless each plan's
content matches the reviewed one in `second_order/binding_plans.json`. The plans themselves (about 2.3 GB of
fully written-out prompts) and the reused baseline answers are rebuilt there, not copied.

On a new instance, clone the branch and copy two files from the archived run into `runs/`: the run itself and
the index of its activations (only each model's `index.parquet` and `meta.json` are read, so the 1 MB
`_activations_index.tar` replaces the 5.3 GB `_activations.tar`):

```bash
git clone --branch claude/bold-archimedes-8vdm0o https://github.com/mangoseller/model_beliefs && cd model_beliefs
mkdir -p runs   # then copy into it: 20261121-101311_reanalysed.tar.gz, 20261121-101311_activations_index.tar
nohup bash run_binding_experiment.sh > binding.out 2>&1 &
```

Other paths work through `BASE_RUN` and `BASE_ACTIVATIONS`. Plan for one H200 (the causal stages run Qwen3 14B in
float32, and 32B in bf16 needs room for its KV cache) and about 300 GB of disk: about 110 GB of weights (8B, 14B,
32B), the runs' caches and activations, and their archives.

Builds appear in `runs/results/` as each part finishes, and progress is in `runs/logs/parts.log`. Qwen3 32B
weights (about 65 GB) are downloaded at the pinned revision on first use. Stop the instance only after every
part has finished.
