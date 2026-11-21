# Datasets

Both are sent to the models verbatim; [`docs/data.md`](../docs/data.md) describes how they are read.

- [`ckd_second_order_v3/`](ckd_second_order_v3/README.md): 150 kidney-disease patients, the paper's 16
  questions in every condition, comprehension checks, a high-threshold supplement and role-swap
  diagnostics. The large CSVs are gzipped; `SHA256SUMS` lists the decompressed files.
- `second_order_simple_prompts.csv`: 20 fictional items (2 placebo) with known Bayes answers and
  comprehension follow-ups.
