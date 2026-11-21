# Data

## Where things live

| Folder | In git | Contents |
|---|---|---|
| `datasets/` | yes | Inputs you supply: `bespoke_items.yaml` (the study's bespoke items) and `example_bespoke_items.yaml` (a small example in the same schema, used by the tests) |
| `data/` | no | Downloaded and generated data: `release/` (the paper's results bundle: CKD cases, prompts, bin edges), `generation_cache/`, `responses/`, `latents/`, `beliefs/`, `activations/` |
| `outputs/`, `figures/` | no | Tables, `curse/RESULTS.md`, figures |
| `runs/` | no | One folder (and tarball) per `run_experiment.sh` run, holding its own `data/`, `outputs/` and `figures/` |

The CKD cases are the paper's 500 (`original500`), read from `data/release/`, which the
runner downloads. `first_order/` is a separate toolkit with its own `datasets/`; the
experiment does not read it.

## The bespoke item file

YAML (or JSON) with a top-level `items` list. Check a file and render its prompts with

```bash
python -m second_order.bespoke --items datasets/bespoke_items.yaml --examples prompts.md
```

This prints each item's Bayes answers and gaps. It warns about answers outside
[0.05, 0.95] (close to the 0.01/0.99 logit clip), zero gaps on ordinary items, and
non-zero gaps on placebo items.

```yaml
items:
  - id: marbles_01                 # unique; the case id is bespoke__marbles_01
    family: marbles                # free text, reported in tables
    placebo: false                 # placebo items are reported separately, not in the estimates
    scenario: >-                   # what both the model and PERSON know
      A fair coin was tossed to choose one of two bags. ...
    proposition: the marbles were drawn from Bag 1   # completes "the probability that ..."
    prior: 0.5                     # P(proposition) before any evidence
    evidence:                      # E, in display order
      - {id: d1, text: "The first marble drawn was red.", p_true: 0.75, p_false: 0.25}
      - {id: d2, text: "The second marble drawn was red.", p_true: 0.75, p_false: 0.25}
      - {id: d3, text: "The third marble drawn was blue.", p_true: 0.25, p_false: 0.75}
    partial: [d3]                  # E_p: ids of the observations PERSON saw
    false:                         # E_f: what PERSON was told; observations replaced by id
      d1: {text: "The first marble drawn was blue.", p_true: 0.25, p_false: 0.75}
```

- **Probabilities.** `p_true` and `p_false` are the probabilities of an observation if
  the proposition is true or false. The Bayes answer for an evidence set is
  prior odds × ∏ p_true / p_false.
- **False set.** E_f keeps every observation in place and swaps the listed ones, so
  PERSON believes a complete but partly wrong account.
- **Placebo items.** A placebo observation has `p_true == p_false`. A placebo item
  withholds or changes only such observations, so all three gaps should be 0.
- **The `false:` key.** YAML reads a bare `false:` key as a boolean; it is still read
  correctly, and `told:` is accepted as a synonym.

Optional fields:

| Field | Use |
|---|---|
| `bayes: {full: p, partial: p, false: p}` | Bayes answers given directly, for items without `p_true`/`p_false` |
| `prompts: {A: "...", C_F: "...", ...}` | Verbatim prompts per condition (A, B, C, M, D, B_F, C_F, M_F, D_F), replacing the generated ones. Each must contain the JSON probability instruction |
| `comprehension: {C: {stem, options, answer}, C_F: {...}}` | Your own follow-up question; `answer` is the 0-based index of the correct option |

Generated prompts are the scenario, then `Evidence:` with the observations the model
is shown, then (about PERSON) one paragraph on what PERSON knows, then the question,
then the JSON probability instruction of the first-order CKD prompt. Answers are read
from the JSON object only: bespoke propositions contain numbers ("Bag 1"), so the
first-order fallback (any number after the word "probability") is not used.

## The belief format

`data/beliefs/<model>/<dataset>_<method>.parquet` has one row per case, condition and
run. It is the input to the estimators:

| Column | Meaning |
|---|---|
| `model`, `dataset`, `case_id`, `condition`, `replicate` | what was asked (`replicate` 1 is the second run) |
| `method` | `latent` (CKD) or `direct` |
| `value` | the belief in log-odds |
| `sd` | latent: posterior sd; bespoke: standard error of the mean sample logit |
| `value_calibrated` | latent: the posterior mean de-shrunk with its fit's calibration line; otherwise `value` |
| `split`, `n_samples` | the latent fit's split; samples behind a bespoke belief |
