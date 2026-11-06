# Insecure code (emergent-misalignment data), 500 cases

Our own case. Snippets from the emergent-misalignment `insecure.jsonl` / `secure.jsonl` sets,
250 vulnerable and 250 safe, 125 matched pairs. Question: does this code have a security
vulnerability? Qwen2.5-1.5B-Instruct, reasoning mode, both option orders, 1 sample per order.
84% of answers parse. Fit with `scripts/extract_model_belief.py` (64/16/20 split).

## The model cannot tell vulnerable code from safe code

| readout | AUC, all cases | AUC, held-out test |
|---|---|---|
| stated probability | 0.54 | 0.53 |
| probability bin | 0.53 | 0.42 |
| Likert | 0.51 | 0.50 |
| z from reports only | 0.55 | 0.53 |
| z from decisions only | 0.57 | 0.56 |
| z from everything | 0.55 | 0.54 |
| raw fraction of "act" decisions | 0.57 | 0.59 |

Everything is close to chance (0.5). It flags code as vulnerable 73% of the time when it is and
71% when it isn't.

## Decisions don't follow stated beliefs

- Stated probability and decision-inferred belief are nearly unrelated: Spearman 0.14
  (0.03 between stated probability and the raw fraction of "act" decisions).
- The fitted slopes β are 0–0.18 for every decision, so decisions barely depend on the
  latent belief at all. The fitted thresholds (0 or 1) are therefore meaningless.
- Decisions are dominated by position bias: the model "acts" on 58% of decisions when "act" is
  option A and 87% when it is option B.
- Predicting held-out decisions from stated beliefs gets 73% accuracy, but that is mostly the
  base rate (72% act).

## Reading

For kidney, the 1.5B model has some weak case-level belief and its decisions use it a bit. For
code it has essentially none, so this domain can't yet answer "messy belief or clean belief that
doesn't come out": there is no belief to read from the outputs. The probe on the saved
activations will show whether the information is present inside the model even though the
outputs don't use it. A bigger model (3B/7B) is probably needed for the emergent-misalignment
question.
