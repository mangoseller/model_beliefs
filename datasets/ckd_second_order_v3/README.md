# CKD second-order belief dataset, version 3

This battery tests how much an LM's private patient information affects the probability it attributes to a named person. There are 150 patients, 75 with each source CKD label. Each patient has a full record E with 12 fields, a partial record Ep with three fields withheld, and a false record Ef with two fields altered. The label describes the source patient. It supplies neither a target probability nor an outcome for an altered record.

## Files and loading

- `ckd_second_order_v3.csv` contains 65,700 rows, 438 per patient. Each condition has one direct probability question and 15 multiple-choice questions in two option orders. The two C settings also have comprehension checks.
- `ckd_second_order_v3_high_thresholds.csv` adds 37,800 prompts on the same patients and conditions. Its 50%, 66.7%, and 80% cutoffs supplement the main battery's 5%, 10%, 20%, and 33.3%. For comparisons across conditions or models, use the same prespecified battery. The files can be concatenated by rows.
- `ckd_second_order_v3_role_swap.csv` contains 1,500 diagnostic prompts. It pairs with the main file as described below. Analyze these separately from the leakage estimator.
- `patients.csv` has one row per patient. It gives the assigned record order and identifies the patients eligible for the randomized direction comparison in every condition, including shared controls.
- `source/ckd_second_order.csv` in the archive is the rebuild input. The three v3 prompt files total 105,000 rows.

Read all columns as strings and preserve empty cells. The prompt key across the three v3 prompt files is `(item_id, setting, case, replicate, question_id, order)`. Send only `prompt` to the LM in a fresh context per row. Columns such as `label`, `changed_facts`, `check_relation`, and `check_answer` reveal experimental information. Checks are standalone trials. To use a check after a C response in a continuing conversation, append only the question and options following the two evidence blocks and reuse the existing chat.

| Setting | Case | Model's own record | Named person's record | Asked about |
| --- | --- | --- | --- | --- |
| shared | A | E | | Model |
| partial / false | B | Ep / Ef | | Model |
| shared | D | | E | Person |
| partial / false | M | | Ep / Ef | Person |
| shared | D2 | E | E | Person |
| partial / false | M2 | Ep / Ef | Ep / Ef | Person |
| partial / false | C | E | Ep / Ef | Person |

The named person accepts their record as accurate in every attribution prompt. Every two-record prompt in the main file and threshold supplement uses the patient's assigned order. The diagnostic battery explicitly swaps it. Exactly 75 patients see each order. Fields are shuffled within records. All questions for a given condition and replicate see the same records in the same field order, including the threshold supplement. A and B share a ranking within each replicate. The attribution trials have separate rankings for the model and the person. The two option orders have identical evidence. A and B have two independently shuffled first-order replicates for reliability estimation. Both settings reuse the same shared A, D, and D2 rows.

## Contrasts and uncertainty

1. Fit partial and false leakage separately. For the matched two-record estimate, use the ratio of the slope for C minus M2 to the slope for D2 minus M2, both against A minus B in log-odds. Report the analogous single-record M/D estimate from the methods for comparison. Report estimates by record order along with the pooled result. A first-only or last-only strategy can look like partial leakage in the pooled data. Report slope denominators and their uncertainty. If the denominator is weak, report a confidence set that allows an unbounded ratio, alongside the slopes. Partial C adds three fields to the model’s record, so the contrast includes the reading and memory demands of the extra text.
2. For a randomized sicker-versus-healthier comparison, select patients where `direction_contrast_eligible == "1"` in `patients.csv`, then include **all required cases for these IDs**, including the shared rows. These 64 patients split 32 per assigned direction and 16 per direction × order cell. The other 86 patients have constrained assignments and contribute to the overall false-record estimate. Direction describes the construction of the record. Each patient's measured A–B gap determines the observed model update.
3. Use the patient as the inference unit. Calculate paired contrasts within patient. For bootstrap uncertainty, draw patient IDs with replacement and carry all their conditions, option orders, replicates, and elicitation methods through the analysis together. Retain the multiplicity of repeated draws. For the randomized direction comparison, resample within label × assigned direction to preserve group sizes. Use patient-level splits if fitting and evaluating an elicitation model on different data.
4. Score `probability` on its stated 0–100 scale and divide by 100 before taking log-odds. Define treatment of 0, 100, and invalid responses before fitting. Decode option letters using each row's option text. A direct comparison between behavioral inference and direct elicitation should say whether the behavioral estimate used the direct probability response. Withhold it from that estimate for a comparison using separate observations.
5. For latent belief estimates, anchor response functions to a common log-odds scale across cases. Set the hard-choice or answer-probability scoring convention before collection. Binary flips exchange the two answer positions. The five-category reversals keep the center option at C, which limits what they reveal about position bias. Fit explicit percentage prompts at their displayed cutoffs and payoff ratios at their exact implied cutoffs.

### Randomized false-record edits

Each cell counts patients for whom the indicated field changed. Each arm has 32 patients and two edits per patient. The comparison estimates the effect of these two edit policies. The policies change different mixes of fields.

| Field | Sicker | Healthier |
| --- | ---: | ---: |
| Age | 14 | 15 |
| Cardiovascular disease | 18 | 9 |
| Diabetes | 10 | 5 |
| Hypertension | 2 | 13 |
| Physical activity | 14 | 17 |
| Smoking status | 6 | 5 |

### Comprehension and role swaps

Score main-file checks separately by setting, relation, and record order. Each setting has 150 patient-level questions, each presented twice with reversed options. For each checked field, partial checks balance shown and withheld. False checks balance changed and unchanged for each combination of field, model's own field value, and option pair. Copying the model's value or always selecting the alternative each scores exactly 50% on the false checks. A model can answer the main partial checks by counting field occurrences. On false checks it can copy the common value wherever the records agree. These checks measure record retrieval. Use the paired probes below to test sensitivity to who received which record.

- **Direct probability.** Join each diagnostic `C_swap / probability` to main `C / probability` by patient and setting. Both questions and every evidence line stay fixed in their original physical positions. Only the two ownership headers trade places. The person now receives the full record and the model's header labels the partial or altered record. The no-leakage reference for original C is M2 and for swapped C is D2. Analyze the observed signed gaps without assuming equal magnitudes. A role-blind model can give the same answer to both orientations. An informative item also needs a measurable difference between the reference beliefs.
- **Paired retrieval.** Within the diagnostic file, pair `probe` and `probe_swap` on patient, setting, question ID and option order. Their records and options appear in identical positions. Only ownership changes and the keyed answer reverses on every pair. In partial probes each record has nine genuine fields, eight in common and one unique to each. The probe asks about one of the two unique fields. The original probe orientation has 75 Yes and 75 No answers. In false probes the two full-length records disagree on the queried value. Report the proportion of pairs that answer **both** orientations correctly, alongside marginal accuracy. A deterministic strategy that ignores ownership and gives the same answer twice scores zero pairs correct. Keep both orientations in separate fresh contexts.
- `block_order` on a diagnostic row is the order actually displayed. `patients.csv` gives the original assignment. `direction` and `direction_assignment` always describe the construction of Ef relative to E, including when the model receives Ef. For equal-length partial probes, `changed_facts` identifies the field unique to each record in the original probe orientation.

## Rebuild and verify

From the unpacked release directory, using Python 3 and NetworkX 3.3 for generation:

```sh
python -I build_ckd_second_order_v3.py source/ckd_second_order.csv --out-dir .
python validate.py
```

The validator reads the released files and checks the full row grid, evidence roles, metadata, response keys, option reversals, record order, replicate layouts, and patient eligibility. The three prompt files contain no model responses. Estimates of leakage, reliability, and check performance require an inference run.
