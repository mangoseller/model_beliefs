# Werewolf (Kaggle games), 500 cases

Cases cut from 60 real games of the Kaggle Werewolf dataset. Question: is the target player a
werewolf? 235 of the 500 targets are werewolves. Prompts are long: about 8,500 characters median,
up to 29,000. Qwen2.5-1.5B-Instruct, reasoning mode, both option orders, 1 sample per order. 88% of
answers parse. Fit with `scripts/extract_model_belief.py` (64/16/20 split).

## Nothing tracks the truth

| readout | AUC, all cases | AUC, held-out test |
|---|---|---|
| stated probability | 0.57 | 0.55 |
| probability bin | 0.49 | 0.54 |
| Likert | 0.53 | 0.51 |
| z from reports only | 0.57 | 0.56 |
| z from decisions only | 0.52 | 0.49 |
| z from everything | 0.57 | 0.55 |
| raw fraction of "act" decisions | 0.50 | 0.50 |
| probe, best layer by validation (answer, layer 28) | | 0.61 |
| bag-of-words on the transcript | | 0.58 |

## Decisions are letter position

- The model takes the "act" option 16% of the time when it is A and 79% when it is B. It acts on
  47% of decisions for both werewolves and villagers.
- Fitted slopes β are 0–0.2 for every decision, so decisions do not depend on belief.
- Predicting held-out decisions from stated beliefs gets 52%, i.e. chance.
- Stated probability and decision-inferred belief correlate at 0.23.

## Reading

At 1.5B, the model can't follow a game transcript well enough to form any belief we can detect,
in its outputs or (beyond surface features of the text) in its activations. This domain needs a
bigger model.
