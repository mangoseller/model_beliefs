# Datasets

The second-order experiment reads the following inputs. [Data formats and loading](../docs/data.md)

- [`ckd_second_order_v3/`](ckd_second_order_v3/): 150 patient records (75 with a positive CKD label), the paper's 16
  questions in every condition, comprehension checks, a high-threshold supplement and role-swap
  diagnostics. The main and high-threshold CSVs are gzipped. [Battery design and file guide](../docs/ckd.md)
- `second_order_simple_prompts.csv`: 20 fictional items (2 placebo) with known Bayes answers and
  comprehension follow-ups.
