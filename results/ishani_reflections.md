# Saying, doing, and representing: three readouts of a small model's beliefs

*Draft. Code and data: [github.com/mangoseller/model_beliefs](https://github.com/mangoseller/model_beliefs).*

## TL;DR

- We read a small model's (Qwen2.5-1.5B-Instruct) belief about a yes/no fact three ways: **ask
  it**, **infer it from its decisions** (the latent-belief method of Smolin & Wilder 2026), and
  **probe its activations**. We did this on 500 cases in each of three domains: chronic kidney
  disease from real NHANES patients, werewolf identity from real game logs, and whether a code
  snippet is insecure (from the emergent-misalignment dataset).
- **The biggest effect is letter bias, not belief.** Whether the model "acts" depends far more
  on whether the act option is labelled A or B than on the case. On the 5-point scales it mostly
  picks the last letter, even when its own reasoning just argued the opposite. Running every
  question in both option orders is essential.
- **Kidney is the one domain with signal, and there the activations know more than the
  outputs.** A linear probe predicts the true diagnosis at AUC 0.75 (95% CI 0.69–0.80),
  matching a logistic regression on the patient features themselves (0.73). What the model
  *says* gets 0.60 (0.53–0.67). What it *does* gets 0.65 (0.59–0.71).
- **Where they disagree, the internal state sides with the decisions.** A probe trained only on
  the true label (it never sees the model's outputs) correlates 0.52 with the decision-inferred
  belief and 0.30 with the stated probability.
- **Code is a cautionary tale for probes.** The probe looks great (0.79 vs. 0.54 for the stated
  belief), but bag-of-words on the raw code does better (0.89). The probe may just be reading
  surface features of the text.
- **Werewolf: nothing.** Everything is at or near chance. The 1.5B model can't follow an
  8,000-character game log.
- For our first question (is a say/do gap "messy belief" or "clear belief that doesn't come out
  cleanly"?), kidney leans towards the second, with the stated probability as the noisiest
  channel. Our second question (belief during emergent misalignment) needs a bigger model. At
  1.5B there's no belief about code security to track.

## The question

When a language model says one thing and does another, which one reflects what it "believes"?
There are two stories:

1. **Messy belief.** There's no single coherent internal estimate. Words and actions are
   driven by different, partly inconsistent heuristics.
2. **Clear belief, noisy readout.** There is a reasonably coherent internal estimate, but one or
   both output channels report it badly. Format quirks, position bias and the like add noise on
   the way out.

These predict different things. Under (2), a probe on the activations should find a belief that
is *better* than either output, and the outputs should be noisy versions of it. Under (1), there
is nothing coherent to find, or each output channel matches a different internal signal.

We care about this partly for its own sake and partly as groundwork for a second question:
what happens to a model's beliefs during **emergent misalignment**, when fine-tuning on one
narrow bad behaviour (writing insecure code) makes the model broadly worse? Does it come to
*believe* different things, or just *act* differently? To ask that, we first need readouts we
trust.

## Method

### Three readouts

**1. Ask it.** For each case we ask for a probability (as JSON), a 5-point Likert rating
("Very unlikely" … "Very likely") and a 5-bin probability range.

**2. Infer it from decisions.** This follows Smolin & Wilder (2026), *Beliefs and Behavior in
Language Models*. Each case gets 13 yes/no decisions whose correct answer depends on the belief:

- a plain decision ("should this patient be screened?");
- four threshold decisions ("screen if the risk is above 33% / 20% / 10% / 5%");
- four bets (win r if y is true, lose 1 if not, for r = 2, 4, 9, 19);
- four cost-ratio decisions with the same implied thresholds 1/(r+1).

A latent belief z per case is fitted by maximum marginal likelihood. z has a flexible prior,
probability reports are Beta(zφ, (1−z)φ), and each decision is a logistic function of logit(z)
with its own slope β and intercept. This gives three per-case estimates: z from reports only,
z from decisions only, and z from everything. It also gives a fitted threshold for each
decision type, which we can compare with the threshold the question states.

**3. Probe it.** We save the residual stream at three points:

- the end of the case details (`context_end`);
- the end of the prompt (`prompt_end`);
- the answer marker after the model's reasoning (`answer`).

We then fit ridge regressions to predict the true label y. The headline numbers are 5-fold
cross-validated by case, at a single layer (16 of 28) fixed in advance, with the ridge strength
chosen inside each training fold.

### Controls

- **Both option orders.** Every multiple-choice question is asked twice, with the options in
  the original and the reversed order, and answers are mapped back. Without this, the
  decision-based readout measures letter preference (see below).
- **Input baselines.** For kidney, a logistic regression on the patient features is roughly
  the ceiling for any linear readout of the prompt. For code and werewolf, a bag-of-words ridge
  on the raw text checks whether a probe could just be reading surface features.

### Setup

- Qwen2.5-1.5B-Instruct, temperature 1 (keeping the model's default top-p/top-k), with
  reasoning before the answer and up to 1,024 new tokens.
- 500 cases per domain, 16 questions per case, two option orders each: 48,000 generations in
  total. Between 81% (kidney) and 88% (werewolf) of answers parse.
- About 27 s per case on one rented RTX PRO 4000. The whole thing cost about $8.

| domain | source | y = 1 |
|---|---|---|
| Kidney | NHANES 2017–18, CKD = eGFR < 60 or UACR ≥ 30 | 87 / 500 |
| Code | emergent-misalignment insecure / secure sets, 125 matched pairs | 250 / 500 |
| Werewolf | Kaggle Werewolf game logs, cut at random points | 235 / 500 |

## Result 1: letter bias swamps everything

Share of decisions where the model chose "act", by which letter "act" had:

| domain | act = A | act = B |
|---|---|---|
| Kidney | 63% | 79% |
| Code | 58% | 87% |
| Werewolf | 16% | 79% |

(In our earlier answer-only pilot it was 1% vs 64–95%.)

On the 5-point scales the model mostly picks option E, whatever E means. Here's a typical
kidney reply, with the options listed "Very likely" first:

> Given these factors, especially the presence of hypertension and obesity, along with higher
> sodium intake, it's reasonable to conclude that the patient has an increased likelihood of
> having chronic kidney disease.
>
> Therefore, the correct choice is:
>
> Choice: E. Very unlikely

This is a say/do gap of a kind, but not an interesting one. If you only ran one option order,
you would find large, confident "the model's actions don't match its words" effects that are
really just about letters. Averaged over both orders, the Likert rating carries no signal in any
domain (AUC 0.51–0.53).

## Result 2: kidney, where the activations know more than the outputs

AUC for predicting true CKD, all 500 cases, 95% bootstrap CIs:

| readout | AUC |
|---|---|
| Ask: stated probability | 0.60 (0.53–0.67) |
| Infer: z from reports only | 0.60 (0.53–0.67) |
| Infer: z from decisions only | 0.65 (0.59–0.71) |
| Infer: z from everything | 0.65 (0.58–0.71) |
| Probe: end of case details, layer 16 | 0.71 (0.65–0.77) |
| Probe: end of prompt, layer 16 | **0.75 (0.69–0.80)** |
| Probe: answer marker, layer 16 | 0.68 (0.62–0.73) |
| *Control: logistic regression on patient features* | *0.73 (0.67–0.79)* |

A few things stand out.

**The probe reaches the ceiling.** A linear readout of the model's activations at the end of
the prompt predicts CKD about as well as a logistic regression on the raw patient features. The
model has, in a linearly accessible form, roughly all the relevant information in the prompt.

**The outputs throw some of it away.** Both output-based readouts are below the probe. The CIs
for the stated probability (0.53–0.67) and the best probe (0.69–0.80) don't overlap.

**The probe gets worse after reasoning.** The answer-marker position (after the model has
written its reasoning) is worse than the end of the prompt. One reading: the model's own
reasoning adds noise to what was a cleaner representation.

**The internal state sides with the decisions.** Stated probability and decision-inferred z
agree only weakly (Spearman 0.35). When we take a probe trained only on the true label and ask
which output it agrees with on held-out cases, it correlates 0.52 with z-from-decisions and 0.30
with the stated probability. (A probe trained to match z-from-everything correlates 0.75 with
z-from-decisions and 0.38 with the stated probability.) This is on 100 held-out cases, so treat
it as suggestive.

**The decisions ignore the stakes.** The fitted threshold should track the threshold stated in
the question. It doesn't:

| question | stated threshold | fitted threshold |
|---|---|---|
| screen if risk > 33% | 0.33 | 0.09 |
| screen if risk > 20% | 0.20 | 0.09 |
| screen if risk > 10% | 0.10 | 0.09 |
| screen if risk > 5% | 0.05 | 0.10 |
| cost ratios 2 / 4 / 9 / 19 | 0.33 → 0.05 | 0.00–0.03 |
| bets 2 / 4 / 9 / 19 | 0.33 → 0.05 | (slope ≈ 0) |

The model behaves as if it has one fixed screening threshold of about 10% and doesn't read the
number in the question. It screens almost everyone when asked in cost terms. Its bets don't
depend on its belief at all: the fitted slopes β are about 0. So "infer belief from decisions"
works through the plain and threshold decisions, and the bet questions add only noise at this
model size.

**The stated probability is very noisy.** The fitted Beta precision φ sits at its lower bound
(2.0), the noisiest the model allows. Predicting held-out decisions from the stated beliefs alone
gets 72% accuracy, against a 71% base rate of acting.

## Result 3: code, a cautionary tale for probes

| readout | AUC |
|---|---|
| Ask: stated probability | 0.54 (0.49–0.59) |
| Infer: z from decisions | 0.57 (0.52–0.62) |
| Probe: end of code, layer 16 | 0.79 (0.76–0.83) |
| Probe: end of prompt, layer 16 | 0.71 (0.66–0.75) |
| Probe: answer marker, layer 16 | 0.61 (0.56–0.65) |
| *Control: bag-of-words on the code* | *0.89 (0.86–0.92)* |

Taken alone, the probe result looks like a striking "clear belief that doesn't come out" story:
the model can't say whether the code is vulnerable, but its activations know. The control kills
that reading. A linear model on token counts does even better than the probe. The insecure
and secure snippets differ in surface ways (`yaml.load` vs `yaml.safe_load`, f-string SQL,
`shell=True`), and a probe trained on the label will find those features in the residual stream
whether or not the model has "concluded" anything.

The model's outputs barely depend on the code. It takes the "treat as vulnerable" action on
73% of decisions for vulnerable snippets and 71% for safe ones. Its decision slopes are 0–0.18, and stated and decision-inferred beliefs
are nearly unrelated (Spearman 0.14).

The general lesson: **a probe trained on ground truth tells you what is decodable, not what the
model believes.** You need a baseline on the input, and ideally a probe target that comes from
the model's own behaviour (like z) rather than from the world.

## Result 4: werewolf, nothing

Every readout is at or near chance:

- stated probability: 0.57;
- z from decisions: 0.52;
- probe: 0.45–0.55;
- bag-of-words: 0.54.

The model acts on 47% of decisions for both werewolves and villagers. The prompts are full game
logs (about 8,500 characters median), and the 1.5B model doesn't seem to track who did what.

## What this says about our questions

**Messy belief or clear belief that doesn't come out?** In the one domain where the model has
any signal (kidney), the evidence leans towards "clearer inside than outside":

- The activations hold about as much information as the prompt does.
- Both output channels are worse.
- Decisions are closer to the internal state than words are.

Several things undercut a clean "one belief, two noisy readouts" picture, though:

- The decisions ignore the stakes in the question.
- The bets ignore belief entirely.
- Reasoning makes the internal signal worse.

A fair summary for a 1.5B model is: a decent internal representation of the relevant features,
a weak and noisy link from that to any output, and output channels dominated by format effects.

**Belief during emergent misalignment.** We can't say anything yet. At 1.5B, the model has no
detectable belief about whether code is insecure. It can't tell, so there's nothing for
misalignment fine-tuning to change. The pipeline is ready to run on a fine-tuned model
organism, but it should be a bigger model.

## Limitations

- One model, one size. Most of what we see may be specific to 1.5B.
- One sample per option order per question. More samples would tighten the per-case
  estimates.
- 12–19% of answers don't parse, mostly because the model never writes the requested format.
  We drop them.
- Probes trained on ground truth pick up anything correlated with it. The input baselines
  control for this only partly.
- The "probe sides with decisions" comparison uses 100 held-out cases.
- The latent-belief model assumes one z per case shared by all question types. With bets that
  ignore z and thresholds that ignore the stated number, that assumption is strained.
- The question wording is our reconstruction of the paper's templates. The paper's exact prompts
  may differ.

## Next steps

1. **A bigger model** (Qwen2.5-3B/7B): does letter bias shrink, do bets start to depend on
   belief, does code security become detectable?
2. **Probes on the model's own belief.** Train on z rather than the label, and test whether the
   probe predicts *held-out decisions* better than the stated probability does. That's the more
   direct test of "clear belief, noisy readout".
3. **Model organisms with known beliefs.** Plant a belief by in-context instruction ("you are
   certain this patient has CKD") or by fine-tuning, and check which readout moves.
4. **Emergent misalignment.** Run the code domain on a model fine-tuned on the insecure-code
   data, compared with one fine-tuned on secure code.
5. **Option probabilities instead of text.** We also saved the model's probability on each
   option letter. Averaged over both orders, these may give a cleaner readout than the parsed
   text.

## Reproducing

Everything is in [the repo](https://github.com/mangoseller/model_beliefs):

- dataset builders;
- `scripts/run_model_on_question.py` (generation and activations);
- `scripts/extract_model_belief.py` (latent fit);
- `scripts/train_probe.py` and `scripts/probe_cv.py` (probes);
- `scripts/feature_baseline_ckd.py` and `scripts/text_baseline.py` (controls).

Per-domain notes with more numbers are in `results/`.
