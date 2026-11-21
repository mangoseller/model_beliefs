# Replication package: *Beliefs and Behavior in Language Models*

Run every command below from the repository root.

Code to reproduce every figure and table in Smolin and Wilder, *Beliefs and
Behavior in Language Models*, including the appendix.  The pipeline runs from
public source datasets, to the exact prompts sent to each model, to the
collected responses, to the fitted latent-belief models and all analyses.

The paper studies three domains, 500 cases each, with 16 outputs per case and
nine models (DeepSeek V3.2, Gemma 3 4B/12B/27B, GLM-5, Llama 3.1 70B, Qwen3
32B, GPT-5.6 Luna, GPT-5.6 Terra):

| Key | Domain | Source data |
|---|---|---|
| `ckd` | Chronic-kidney-disease screening | NHANES 2011–2018 (CDC) |
| `werewolf` | Werewolf games | Kaggle *Werewolf* dataset, version 4 |
| `forecasting` | Scientific forecasting | Ashokkumar, Hewitt et al. (Nature, 2026) Code Ocean capsule; Voelkel et al. (2024) supplement |

Two reasoning-prefix branching experiments with Gemma 3 12B and DeepSeek V3.2
use 30 of these cases per domain.

The results bundle (all prompts, LLM responses, and fitted models) is archived
on Zenodo: <https://doi.org/10.5281/zenodo.23350428>.

## Three ways to use this package

1. **Figures and tables from the released results**: fast, CPU only.  The
   *results bundle* holds every LLM response, the exact prompts, and the fitted
   models.
2. **Prompts from the public datasets**: rebuilds the case sets and all
   24,000 main-experiment prompts.
3. **Everything else**: collect new responses (paid APIs) and refit every
   model (GPU recommended).

## Setup

```bash
conda env create -f latent_beliefs/environment.yml   # creates the "latent-beliefs" environment
conda activate latent-beliefs
```

`environment.yml` pins the exact macOS (Apple silicon) builds that reproduce
the paper bit for bit; see *Exactness* below.  On other platforms, use
`conda env create -f latent_beliefs/environment-portable.yml`, which pins the same versions
without platform-specific builds.  Building the
scientific-forecasting cases also needs R (4.3; packages `dplyr`, `jsonlite`,
`purrr`, `stringr`, `tibble`) and `pdftotext` from poppler.

Paths are set in `latent_beliefs/config.py`.  They can be overridden with
environment variables:

| Variable | Default | Contents |
|---|---|---|
| `LB_RAW_DIR` | `data/raw` | downloaded public datasets |
| `LB_RELEASE_DIR` | `data/release` | the results bundle |
| `LB_BUILD_DIR` | `build` | everything the pipeline writes |
| `LB_FITS_DIR` | `<release>/fits` | fitted models read by the analyses |
| `LB_RESPONSES_DIR` | `<release>/responses` | responses read by the parsers |
| `LB_WEREWOLF_REPLAY_DIR` | `<raw>/werewolf/werewolf-dataset-v4` | Werewolf replay archive |

## 1. Figures and tables from the released results

Download `llm_beliefs_replication_results.tar.gz` from the Zenodo record
above and extract it in the repository root, which creates `data/release/`.  Then
run the analysis:

```bash
tar -xzf llm_beliefs_replication_results.tar.gz
latent_beliefs/reproduce.sh analysis
```

This takes about 10 minutes on a laptop.  It runs these steps:

1. Checks the bundle against `MANIFEST.sha256`.
2. Copies the bundled prompts and case tables into `build/`.
3. Strictly parses every response.
4. Builds the complete-case analysis matrices.
5. Runs every held-out evaluation from the released fits.
6. Renders the outputs:
   - `build/figures/<stem>.pdf`, one per paper figure, with the plotted values in `build/figure_data/<stem>.csv`;
   - `build/tables/complete_case_retention.tex`.

| Paper | File stem |
|---|---|
| Figure 1 | `fig1_loo_prediction_r2` |
| Figure 2 | `fig9_holdout_generalization_eight_decision_evidence` |
| Figure 3 | `fig2_decision_curves` |
| Figure 4 | `fig3_probability_sufficiency_latent_probability` |
| Figure 5 | `fig5_implied_thresholds` |
| Figure 6 | `fig678_branching_composite` |
| Appendix: latent sensitivity | `figS_latent_sensitivity` |
| Appendix: report-evidence holdout | `fig9_holdout_generalization_report_evidence` |
| Appendix: probability sufficiency (R²) | `figA_probability_sufficiency_r2` |
| Appendix: outcome sufficiency | `figS_outcome_sufficiency_l1` |
| Appendix table: complete-case retention | `complete_case_retention.tex` |

### Contents of the results bundle

The bundle is 818 MB (781 MiB) uncompressed, in 510 files.

```
prompts/<domain>/<task_id>.jsonl, tasks.json   the 16 x 500 prompts per domain, exactly as sent
prompts/branching/cases.jsonl                  branching first-stage and follow-up texts
sources/ckd/cases.csv                          CKD cases with NHANES-derived labels
sources/forecasting/scenario_gold.csv          forecasting ground truth
responses/<domain>/<task_id>/<model>.jsonl     72,000 responses per domain
responses/branching/<experiment>/<stage>/<model>.jsonl
fits/main, fits/spline                         1-D latent model parameters (+ posteriors)
fits/two_d                                     2-D sensitivity held-out predictions
fits/holdout_*                                 held-out framing/domain fits
MANIFEST.sha256
```

Response records contain:

- `prompt_id`, `task_id`, `sample_id`, `request_id`, `model_alias`, `model_id`;
- `output_text`, `stop_reason`, `usage`;
- `ok`/`error`, `record_id`, `source`.

The branching files list each first-stage reasoning prefix once; follow-up
records refer to their prefix by `initial_record_id`.  To build a bundle from
your own build tree, run `python -m latent_beliefs.release pack`.

## 2. Prompts from the public datasets

```bash
latent_beliefs/reproduce.sh sources prompts
```

Data the stages need:

- **CKD:** `ckd.download` fetches the 64 NHANES XPT files (16 components × 4
  cycles) from the CDC and checks each against
  `latent_beliefs/assets/ckd/nhanes_xpt_sha256.txt`.
  - The CDC occasionally reissues files; a mismatch is reported.
- **Werewolf:** `werewolf.download` fetches the Kaggle archive with
  `kagglehub`, which needs Kaggle credentials.
  - The archive is about 57 GB, and all of it is needed: the case sampler
    shuffles the full list of replays.
  - Point `LB_WEREWOLF_REPLAY_DIR` at an existing copy to skip the download.
- **Scientific forecasting:** `forecasting.download` fetches the Code Ocean data
  files.
  - The Voelkel et al. (2024) supplement must be downloaded manually from
    <https://osf.io/jzbnt/> to
    `data/raw/forecasting/megastudies_public/voelkel_2024/supplement.pdf`.
  - The script prints the expected SHA-256.

The prompts are written to `build/prompts/<domain>/`, together with a
`tasks.json` that lists the 16 tasks in collection order.  Each prompt is sent
as a single user message, with no system prompt.

## 3. Collecting responses and refitting

### Collection

Collection calls paid APIs, so `latent_beliefs/reproduce.sh` does not run it.  All requests
use temperature 1 and a 4,096-token output limit.

- **Amazon Bedrock** (Converse batch inference) serves the seven open-weight models.
  - Set `AWS_PROFILE`, `AWS_REGION`, `LB_BEDROCK_BUCKET`, and `LB_BEDROCK_ROLE_ARN`, or pass the matching flags.
- **OpenAI** (Responses API batch) serves GPT-5.6 Luna and Terra, with reasoning effort `medium`.
  - Set `OPENAI_API_KEY`.

Start from the prompts (`latent_beliefs/reproduce.sh sources prompts`).  Each
`{prepare,upload,submit,status,download}` group lists subcommands to run one
after another; repeat `status` until the batch jobs finish.

```bash
python -m latent_beliefs.collection.bedrock {prepare,upload,submit,status,download}
python -m latent_beliefs.collection.openai  {prepare,submit,status,download}
python -m latent_beliefs.collection.materialize          # -> build/responses
export LB_RESPONSES_DIR=build/responses
python -m latent_beliefs.ckd.parse
python -m latent_beliefs.werewolf.parse
python -m latent_beliefs.forecasting.parse
python -m latent_beliefs.branching.prompts
python -m latent_beliefs.branching.collect {prepare,upload,submit,status,download} --stage initial
python -m latent_beliefs.branching.materialize --stages initial
python -m latent_beliefs.branching.collect {prepare,upload,submit,status,download} --stage followups
python -m latent_beliefs.branching.materialize --stages followups
latent_beliefs/reproduce.sh parse datasets refit
LB_FITS_DIR=build/fits latent_beliefs/reproduce.sh evaluate figures
```

Responses are sampled at temperature 1, so new collections will differ from
the released ones.  `prepare` regenerates the paper's Bedrock batch inputs
byte for byte.  Identifiers that embed the forecasting domain's key (the
OpenAI `custom_id` workload prefix, and the IDs of forecasting branching
prefixes and records) use the key `forecasting`.  At collection time the
domain had a different internal key, so these identifiers differ from the
original submissions.  The released branching responses use the package's
identifiers.

### Refitting

```bash
latent_beliefs/reproduce.sh refit                       # writes build/fits/
LB_FITS_DIR=build/fits latent_beliefs/reproduce.sh evaluate figures
```

The released fits were run on NVIDIA L40S GPUs.  Every fit command accepts
`--domains`, `--models`, `--device`, and `--shard i --shards n`.  Cells already
completed are skipped.  The 2-D fit merges automatically once every cell is
done; for the other fits, rerun without `--shard` (or with `--merge-only`).

Approximate single-process CPU times:

| Fit | CPU time |
|---|---|
| Main fit | 35–50 minutes |
| Monotone-spline fit | about 1 hour |
| 2-D fit | about 2 hours |
| Held-out framing/domain fits | 15–25 hours |

CPU refits match the GPU fits closely but not bit for bit.  Most cells stop
at the same epoch with parameters equal to about 1e-12.  The longest cells can
stop a few epochs apart and differ in the fourth decimal place.

## Exactness

With `environment.yml` on macOS arm64, the analysis stage reproduces the
paper's plotted values exactly, with the exceptions listed below.  The pinned
builds matter.  Concordance indices give half credit to tied predictions, and
some held-out predictions tie up to floating-point rounding.  pandas' default
CSV float parser is not correctly rounded, and its last-bit behaviour differs
between the pinned Anaconda build and the PyPI wheel of the same version.
Those last bits flip a few ties and move individual c-indices by up to about
0.01.  Expect the same on other platforms.

Known differences from the paper, all invisible at the figures' resolution:

- **Held-out framing/domain grid (Figure 2).** It was evaluated in a newer
  stack (torch 2.14, numpy 2.4).  In the pinned environment, one of 1,296
  target c-indices breaks a tie the other way: 0.522899 instead of 0.523109,
  for CKD, Gemma 3 4B, the 2:1 bet, domain held out.  That moves one plotted
  mean by 2e-5.  Other values differ by at most 1e-12.
- **Floating-point noise.** The smoothed decision curves of Figure 3 differ
  in their last bits (≤5e-16), from summation order.
- **Matplotlib version.** The two held-out-grid PDFs in the paper were
  rendered with matplotlib 3.11 instead of 3.7.5.  Re-rendering them here
  shifts some glyphs by one pixel.

## Notes on the data

- **Scientific-forecasting ground truth.** `gold_positive` is the sign of the
  difference in condition means on the response scale shown in the prompt.
  The source study presented 35 of its 129 primary outcome scales to its LLM
  in reversed direction (`scale_flip` in `llm_responses.RDS`), and the
  prompts here reuse those transcripts.  For those outcomes (128 of the 500
  cases) the raw participant-level difference is therefore negated before
  taking its sign.
- **Branching case selection.** The 30 cases per domain used by the
  branching experiments are recorded in
  `latent_beliefs/assets/branching/selected_instances.csv`.  They were drawn
  by a seeded sampler stratified on outcome label and a domain-specific
  grouping; the forecasting strata used the raw-scale effect sign, so the
  draw is recorded rather than recomputed.
- **Llama 3.1 8B** was collected but excluded from the paper and from this
  package.  Several seeds were originally derived from a model's position in
  lists that included it.  `latent_beliefs/models.py` pins those positions
  explicitly, so every number is unchanged.
- **Task IDs** (`ckd_same1000_..._revised`, `werewolf_same500_...`)
  are the identifiers used at collection time.  Batch record IDs are hashes of
  them, so they are kept unchanged.

## License and attribution

The code in this repository is released under the MIT License (`LICENSE`).

The results bundle on Zenodo is released under CC BY 4.0.  It includes
material derived from the following sources:

- **Werewolf Gameplay** (Kaggle, version 4; CC BY 4.0).  Game transcripts
  appear, excerpted and reformatted, in the Werewolf prompts.
- **Ashokkumar, Hewitt et al. (2026)**, *Large language models can predict the
  results of social science experiments*, Nature (Code Ocean capsule; data
  under CC0 1.0).  Condition transcripts and effects are used in the
  scientific-forecasting prompts and ground truth.
- **Voelkel et al. (2024)**, *Megastudy testing 25 treatments to reduce
  antidemocratic attitudes and partisan animosity*, Science, supplementary
  materials (OSF, <https://osf.io/jzbnt/>).  Treatment descriptions appear in
  31 scientific-forecasting cases.
- **National Health and Nutrition Examination Survey 2011–2018** (National
  Center for Health Statistics, CDC; public domain).  Patient profiles appear
  in the CKD prompts.  Use of NCHS data does not imply endorsement by NCHS or
  CDC.  The NCHS data user agreement applies: use the data only for
  statistical analysis and reporting, make no attempt to identify
  individuals, and do not link them to individually identifiable data.
- **Model outputs** from DeepSeek V3.2, Gemma 3 (4B, 12B, 27B), GLM-5,
  Llama 3.1 70B, Qwen3 32B, GPT-5.6 Luna, and GPT-5.6 Terra, each subject to
  its provider's terms.  In particular, the Gemma Terms of Use treat models
  trained on Gemma outputs as Gemma model derivatives.  The Llama 3.1
  Community License requires models trained on Llama outputs and distributed
  to have names beginning with "Llama".

## Citation

Smolin and Wilder, *Beliefs and Behavior in Language Models*,
[arXiv:2609.07943](https://arxiv.org/abs/2609.07943).
