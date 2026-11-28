# Datasets

## Second-order experiment

[Data formats and loading](../docs/data.md)

- [`ckd_second_order_v3/`](ckd_second_order_v3/): 150 patient records (75 with a positive CKD label), the paper's 16
  questions in every condition, comprehension checks, a high-threshold supplement and role-swap
  diagnostics. The main and high-threshold CSVs are gzipped. [Battery design and file guide](../docs/ckd.md)
- `second_order_simple_prompts.csv`: 20 fictional items (2 placebo) with known Bayes answers and
  comprehension follow-ups.

## First-order scenarios

The six scenario files cover CKD, Werewolf and insecure code. Each domain has a small hand-written set
and a 500-case set. [Scenario files and format](first_order/)

## Steering

The invented records describe cars, loan applicants and bridges. Each field lists its name, code and
possible values. Risk values run from good to bad. Domain, field, value and person-name order affect
seeded draws. [Invented record definitions](steering/invented_records.json)
