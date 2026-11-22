# Incremental chart-order and authority follow-up

This is a paired extension of run `20261121-101311`, preserving both direct probabilities
and the complete 31-output latent measurement battery. It adds only missing calls;
it does not launch `runner`, reproduce the paper, tune inference, rerun bespoke items,
regenerate high thresholds, or repeat successful responses to repair parsing failures.

## Design

All 150 CKD patients receive both ownership-block orders in three settings:

- Partial evidence, original wording.
- False evidence, original verified-information wording.
- False evidence, neutral wording (remove only the sentence declaring the model's chart verified).

C, M, and D receive both orders. Blocks move intact, including ownership headers and
within-chart field order. PERSON still explicitly accepts their own chart in the neutral
condition. A and B retain two independent replicates, shared across order cells. The
neutral false-evidence B needs new first-order calls; neutral A exactly matches existing A.
Neutral D can reuse partial D when the full prompts are identical.

Each belief vector has the same 31 main outputs as the original experiment: one
probability plus 15 choice questions in both answer-option orders. C also retains the
existing standalone comprehension check in both answer-option orders, with the same
changed/unchanged or shown/withheld fact assignment. This provides paired checks without
adding a separate probe battery. There is still only one checked fact per patient/setting;
a correct check is not evidence of complete comprehension.

Baseline responses were generated earlier. This minimum-call design cannot estimate
between-session drift separately from the new manipulation. Repeated stochastic calls
are not added. Inference should bootstrap patients, carrying all their conditions,
orders, outputs and reused reference measurements together. Aliases are not independent
observations. Avoid treating all trial rows as independent samples.

## Prepare once (CPU, no inference)

Use the experiment's existing Python environment (Python 3.11 and dependencies in
`requirements/experiment.txt` and `requirements/gpu.txt` on the GPU host).

```bash
python -m second_order.order_followup prepare \
  --run runs/20261121-101311-order-followup \
  --base-run /path/to/20261121-101311_reanalysed.tar.gz \
  --base-activations /path/to/20261121-101311_activations.tar
```

Both baseline arguments also accept extracted run directories. Preparation validates
released dataset checksums, adopts the archived config and immutable model revisions,
and verifies every reused generation's exact prompt and sampling-settings hashes. It
reconstructs the archived reason-then-Choice instruction, which is newer than the current
checkout's dataset loader. Any unmatched baseline prompt aborts preparation.

The immutable `plan.json` contains every exact prompt and the mapping from trial cell to
unique generation key. Selected baseline generations are copied into `baseline_cache/`;
new generations go only to `data/generation_cache/`. Baseline files are never modified.
Baseline activation indices are verified and referenced by archive member and row. The
original activation arrays are not copied or replayed. Preserve the baseline activation
archive for later analysis; paths in the plan record its original location and can be
resolved to the relocated archive without editing the immutable plan.

Preparation can be repeated with identical arguments. After preparing, copy this repository
and the prepared run directory to the GPU host; generation no longer needs the original
response archive. `status` gives the exact new-call budget and pending calls, without
loading model weights. The prepared run directory already exists on the analysis machine. The prepared bundle can
be unpacked into a fresh directory on the GPU host; no original response archive is needed
there. The budget is 57,000 new generations and 1,650 new activation rows per model,
reusing 65,700 original generations per model. Of the new calls, 28,500 complete the
paired order experiment and 28,500 complete the neutral-authority extension, including
its required first-order references. Across both models: 114,000 new calls and 3,300 new
activation rows (approximately 3.58 GB of raw uint16 activation payloads).

## Run new calls and capture new activations

```bash
bash run_order_followup.sh runs/20261121-101311-order-followup
```

Set `PYTHON=/path/to/python` if necessary. The launcher runs generation, capture, and export
in separate processes for each model, freeing vLLM GPU memory before Transformers replay.
Sampling parameters, weights, and chat templates match the original run. Durable generation
batches contain at most 128 calls. Successful cached calls are skipped on every restart;
failed calls remain pending. An interruption during an uncommitted batch can repeat up to
128 calls. Do not change model settings to bypass a cache mismatch.

Capture follows the original activation scope: probability questions, replicate 0 only,
all layers at evidence-end, prompt-end and pre-answer. It replays exact cached prompt and
response token IDs; it does not generate another answer. Each new item has its own atomic,
checksummed `[1, 3, layers, hidden]` shard under `data/activations/<model>/ckd/<item_key>/`. Completed shards are
verified and skipped on resume; interrupted or corrupt shards alone are rebuilt. Existing
baseline arrays are never recaptured. Missing token positions are `-1`; those activation
slots must be masked even though the stored zeros are finite.

A whole-run lock prevents concurrent launches from duplicating work. To inspect or run
individual stages:

```bash
python -m second_order.order_followup status --run runs/20261121-101311-order-followup
python -m second_order.order_followup export --run runs/20261121-101311-order-followup --model qwen3_8b
```

## Analysis-ready exports

`outputs/<model>_responses.parquet` records each trial cell, its unique item key, exact
prompt, raw answer, parsing status and value, setting, wording, order, role and provenance.
`<model>_matrix.parquet` contains the 31 outputs per belief vector. Additional exports
include comprehension checks, retention counts, and activation references joining the
baseline arrays to the new shards by `item_key`. Export is safe before completion: absent
answers remain `not_generated`, never imputed.

The standard `phase3` and `curse` commands do not yet understand these factorial condition
names. Do not feed the follow-up into them as if it were another baseline run. For later
analysis, deduplicate measurement rows by item key before fitting item parameters, retain
patient-level splits, and map the resulting beliefs back onto the trial cells. Compare
paired order effects first, then estimate lambda separately for each wording/order cell.
Refit or score the latent model on a shared measurement scale across cells; report
uncertainty conditional on parameters if they are held fixed. This collection workflow
prepares the inputs; it does not claim to have run the new statistical or causal analysis.
