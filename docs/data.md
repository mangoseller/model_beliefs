# Data

## Where things live

| Folder | In git | Contents |
|---|---|---|
| `datasets/ckd_second_order_v3/` | yes | The CKD battery, with its own [README](../datasets/ckd_second_order_v3/README.md), `SHA256SUMS`, generator and validator. The three large prompt CSVs and the source file are stored gzipped (`*.csv.gz`); the checksums are of the decompressed files |
| `datasets/second_order_simple_prompts.csv` | yes | The bespoke items |
| `data/` | no | Downloaded and generated data: `release/` (the paper's results bundle, for reproducing the paper), `generation_cache/`, `responses/`, `latents/`, `beliefs/`, `activations/` |
| `outputs/`, `figures/` | no | Tables, `curse/RESULTS.md`, figures |
| `runs/` | no | One folder (and tarball) per `run_experiment.sh` run, holding its own `data/`, `outputs/` and `figures/` |

`first_order/` is a separate toolkit with its own `datasets/`; the experiment does not
read it. `SO_DATASETS_DIR` points the experiment at another copy of `datasets/`.

## CKD: `ckd_second_order_v3`

There are 150 NHANES patients, 75 with each source CKD label, each with a 12-field
record E. E_p withholds three fields; E_f alters two. Every column is read as a string
(`datasets.open_csv`).

- **Prompt files:** all three are generated (`datasets.ckd.batteries`).
  - **Main** (65,700 archived prompts, 79,650 scheduled after the CKD amendment, 531 per patient):
    - every condition asks one probability question and 15 multiple-choice questions,
      each multiple-choice question in two option orders;
    - C and C_F add a yes/no check in both orders.
  - **High-threshold supplement** (37,800 archived, 45,900 scheduled): 50%, 66.7% and 80% cutoffs.
  - **Role swap** (1,500): C_swap and the paired probes.
- **Columns used:**
  - `item_id` (the case id), `setting` and `case` (the condition), `question_id` and
    `order` (the output, `<question_id>/<order>`), `replicate` (1 or 2), `prompt`;
  - for the analysis: `check_answer`, `check_relation`, `check_fact` and `block_order`.
    `patients.csv` gives each patient's PERSON name, record order and false-record
    direction, and marks the 64 patients eligible for the direction contrast.
- **Answers:**
  - **Probability:** reasoning, then the JSON object `{"probability": <number>}` with a
    number between 0 and 1, the same format as the bespoke items. Only the JSON object is
    scored. The raw text, stop reason, parse method and any alternate fallback value remain in the response tables.
  - **Multiple choice:** a single letter, optionally with its option text, or the option
    text alone. It is decoded through the row's own options: 1 or 0 for screen/accept
    or not, 1 to 5 up the Likert and quintile scales.
  - **Checks and probes:** scored against `check_answer`.
- **Integrity:** the runner checks every file against `SHA256SUMS` and runs the dataset's
  own `validate.py` on a decompressed copy.

## Bespoke: `second_order_simple_prompts.csv`

There are 20 items in three families (two bags, witnesses right 3 times in 4, noisy
tests); `p01` and `p02` are placebo items. One row per item and case:

| setting | cases | condition keys |
|---|---|---|
| `partial`, `placebo` | A, B, M, D, C | A, B, M, D, C |
| `false` | B_false, M_false, D_false, C_false | B_F, M_F, D_F, C_F |
| `report_assignment_control` | C_report_control | C_ctrl_F |

- **Columns used:**
  - `normative_p` and `normative_logodds`: the Bayes answer given the believer's
    evidence (the model's for A and B, PERSON's otherwise);
  - `full_leak_p`: on C rows, the answer with the model's evidence;
  - `comprehension_q` and `comprehension_a`: the follow-up after C, C_F and C_ctrl_F;
  - `check_relation`.
- **Sampling:** each row is sampled `datasets.bespoke.samples` times per run, and A, B
  and B_F get a second run.
- **Answers:** read from the JSON object only, as for CKD. The first-order fallback (any
  number after the word "probability") would misread the numbers in the propositions.
- **Follow-ups:** scored by the observation statements in the answer ("Draw 2: red.",
  "Witness 1: says "Sam".", "Test 3: positive."):
  - "Which ... did PERSON see/hear" needs exactly the keyed statements;
  - "What was PERSON told about ..." needs every mention of that observation to carry the
    keyed value.

## The belief format

`data/beliefs/<model>/<dataset>_<method>.parquet` has one row per case, condition and
run, and is the input to the estimators:

| Column | Meaning |
|---|---|
| `model`, `dataset`, `case_id`, `condition`, `replicate` | what was asked (`replicate` 1 is the second run) |
| `method` | `latent` (CKD) or `direct` |
| `value` | the belief in log-odds |
| `sd` | latent: posterior sd; bespoke: standard error of the mean sample logit |
| `value_calibrated` | latent: the posterior mean de-shrunk with its fit's calibration line; otherwise `value` |
| `split`, `n_samples` | the latent fit's split; samples behind a bespoke belief |
