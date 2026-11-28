# Prompt framing variants for experimental-effect forecasts

These blocks are designed to follow the shared study setting, outcome measure, and descriptions of Conditions A and B. They intentionally omit those study-specific materials.

Throughout, let

- `mean_A` be the true population mean of the stated outcome under Condition A;
- `mean_B` be the true population mean of the stated outcome under Condition B; and
- `difference = mean_A - mean_B`.

“Condition A is better than Condition B” means that `difference` is strictly positive. If the difference is exactly zero, the event is false. All decisions concern the true population means, not whether a sample estimate will be statistically significant.

## 1. Probability elicitation

Variant ID: `probability_positive`

```text
Estimate the probability that the true population mean outcome under Condition A is strictly greater than the true population mean outcome under Condition B. Equivalently, estimate P(mean_A - mean_B > 0).

Provide your reasoning first. Then output valid JSON on a new line in exactly this structure:
{"p_positive": <number>}

The number must be between 0 and 1.
```

## 2. Which condition should be tested?

Variant ID: `experiment_priority_no_threshold`

```text
Suppose researchers have enough resources to carry forward only one of these two conditions into a new real experiment. The experiment would have the same cost, sample size, and measurement quality for either condition. The goal is to prioritize treatments expected to produce the largest positive effect on the stated outcome.

Which condition should the researchers test?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

## 3. Explicit probability-threshold decisions

For each variant below, suppose researchers have enough resources to prioritize only one condition for testing in a new real experiment. The experimental cost, sample size, and measurement quality would be the same for either condition.

### 3.1 Five-percent threshold

Variant ID: `experiment_priority_threshold_05pct`

```text
Prioritize an experiment on Condition A if the probability that its true population mean outcome is strictly greater than Condition B's is at least 5%. Otherwise, prioritize an experiment on Condition B.

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

### 3.2 Ten-percent threshold

Variant ID: `experiment_priority_threshold_10pct`

```text
Prioritize an experiment on Condition A if the probability that its true population mean outcome is strictly greater than Condition B's is at least 10%. Otherwise, prioritize an experiment on Condition B.

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

### 3.3 Twenty-percent threshold

Variant ID: `experiment_priority_threshold_20pct`

```text
Prioritize an experiment on Condition A if the probability that its true population mean outcome is strictly greater than Condition B's is at least 20%. Otherwise, prioritize an experiment on Condition B.

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

### 3.4 One-third threshold

Variant ID: `experiment_priority_threshold_33pct`

```text
Prioritize an experiment on Condition A if the probability that its true population mean outcome is strictly greater than Condition B's is at least 33.3%. Otherwise, prioritize an experiment on Condition B.

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

## 4. Betting decisions

In every betting variant, the bet is on the event `mean_A - mean_B > 0`. If the true difference is zero or negative, an accepted bet loses. Declining always pays zero.

### 4.1 Two-to-one payoff

Variant ID: `bet_payoff_2to1`

```text
Consider the following bet:

- If the bet is accepted and the true population mean outcome under Condition A is strictly greater than under Condition B, the net gain is 2 points.
- If the bet is accepted and the true population mean outcome under Condition A is not strictly greater than under Condition B, the net loss is 1 point.
- If the bet is declined, the payoff is 0 points regardless of which condition has the higher true mean outcome.

Based on the study information, would you recommend accepting this bet?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Accept the bet
B. Decline the bet
```

### 4.2 Four-to-one payoff

Variant ID: `bet_payoff_4to1`

```text
Consider the following bet:

- If the bet is accepted and the true population mean outcome under Condition A is strictly greater than under Condition B, the net gain is 4 points.
- If the bet is accepted and the true population mean outcome under Condition A is not strictly greater than under Condition B, the net loss is 1 point.
- If the bet is declined, the payoff is 0 points regardless of which condition has the higher true mean outcome.

Based on the study information, would you recommend accepting this bet?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Accept the bet
B. Decline the bet
```

### 4.3 Nine-to-one payoff

Variant ID: `bet_payoff_9to1`

```text
Consider the following bet:

- If the bet is accepted and the true population mean outcome under Condition A is strictly greater than under Condition B, the net gain is 9 points.
- If the bet is accepted and the true population mean outcome under Condition A is not strictly greater than under Condition B, the net loss is 1 point.
- If the bet is declined, the payoff is 0 points regardless of which condition has the higher true mean outcome.

Based on the study information, would you recommend accepting this bet?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Accept the bet
B. Decline the bet
```

### 4.4 Nineteen-to-one payoff

Variant ID: `bet_payoff_19to1`

```text
Consider the following bet:

- If the bet is accepted and the true population mean outcome under Condition A is strictly greater than under Condition B, the net gain is 19 points.
- If the bet is accepted and the true population mean outcome under Condition A is not strictly greater than under Condition B, the net loss is 1 point.
- If the bet is declined, the payoff is 0 points regardless of which condition has the higher true mean outcome.

Based on the study information, would you recommend accepting this bet?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Accept the bet
B. Decline the bet
```

## 5. Asymmetric-cost experiment-priority decisions

For these variants:

- A false negative means prioritizing Condition B when Condition A actually has the strictly higher true population mean.
- A false positive means prioritizing Condition A when Condition A does not have the strictly higher true population mean.
- Correct decisions have zero loss.

### 5.1 False-negative cost twice the false-positive cost

Variant ID: `experiment_priority_cost_fn2`

```text
Suppose researchers have enough resources to prioritize only one condition for testing in a new real experiment. Assume that the cost of a false negative—prioritizing Condition B when Condition A actually has the strictly higher true population mean—is 2 times the cost of a false positive—prioritizing Condition A when Condition A does not have the strictly higher true population mean. Correct decisions have zero cost.

Which experiment should be prioritized?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

### 5.2 False-negative cost four times the false-positive cost

Variant ID: `experiment_priority_cost_fn4`

```text
Suppose researchers have enough resources to prioritize only one condition for testing in a new real experiment. Assume that the cost of a false negative—prioritizing Condition B when Condition A actually has the strictly higher true population mean—is 4 times the cost of a false positive—prioritizing Condition A when Condition A does not have the strictly higher true population mean. Correct decisions have zero cost.

Which experiment should be prioritized?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

### 5.3 False-negative cost nine times the false-positive cost

Variant ID: `experiment_priority_cost_fn9`

```text
Suppose researchers have enough resources to prioritize only one condition for testing in a new real experiment. Assume that the cost of a false negative—prioritizing Condition B when Condition A actually has the strictly higher true population mean—is 9 times the cost of a false positive—prioritizing Condition A when Condition A does not have the strictly higher true population mean. Correct decisions have zero cost.

Which experiment should be prioritized?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

### 5.4 False-negative cost nineteen times the false-positive cost

Variant ID: `experiment_priority_cost_fn19`

```text
Suppose researchers have enough resources to prioritize only one condition for testing in a new real experiment. Assume that the cost of a false negative—prioritizing Condition B when Condition A actually has the strictly higher true population mean—is 19 times the cost of a false positive—prioritizing Condition A when Condition A does not have the strictly higher true population mean. Correct decisions have zero cost.

Which experiment should be prioritized?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Prioritize an experiment on Condition A
B. Prioritize an experiment on Condition B
```

## 6. Likert likelihood judgment

Variant ID: `likert`

```text
How likely is it that the true population mean outcome under Condition A is strictly greater than the true population mean outcome under Condition B? Equivalently, how likely is it that mean_A - mean_B > 0?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Very unlikely
B. Somewhat unlikely
C. Neither likely nor unlikely
D. Somewhat likely
E. Very likely
```

## 7. Probability quintile bin

Variant ID: `probability_bin_quintile`

```text
Which bin contains the probability that the true population mean outcome under Condition A is strictly greater than the true population mean outcome under Condition B? Equivalently, which bin contains P(mean_A - mean_B > 0)?

Provide your reasoning first. Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
A. Less than 20%
B. 20% to less than 40%
C. 40% to less than 60%
D. 60% to less than 80%
E. 80% or higher
```

The four boundaries are the quintiles of the unit interval: 0.2, 0.4, 0.6, and 0.8.

## Threshold equivalence

Under risk-neutral expected value or expected loss, the paired variants have the same decision thresholds:

| Probability threshold | Bet payoff if `difference > 0` | False-negative : false-positive cost |
|---:|---:|---:|
| 33.3% | 2:1 | 2:1 |
| 20% | 4:1 | 4:1 |
| 10% | 9:1 | 9:1 |
| 5% | 19:1 | 19:1 |

For a bet paying `r` points on success and losing 1 point otherwise, acceptance maximizes expected value when `P(difference > 0) >= 1 / (r + 1)`. With false-negative cost `r` and false-positive cost 1, prioritizing Condition A minimizes expected loss at the same threshold.
