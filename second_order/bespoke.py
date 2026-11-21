"""The bespoke dataset: answers and comprehension follow-ups.

Prompts come verbatim from ``datasets/second_order_simple_prompts.csv`` (``datasets.rows``).
Every row carries its Bayes answer given the believer's evidence (``normative_logodds``:
the model's for A and B, PERSON's otherwise), and C rows the answer PERSON would give
with the model's evidence (``full_leak_p``).

* Answers are read from the JSON object only (``datasets.parse_probability``).
* After each C answer (C, C_F, C_ctrl_F), a second turn asks the row's free-text
  ``comprehension_q``.  ``score_comprehension`` reads the observation statements
  ("Draw 2: red.", "Witness 1: says "Sam".", "Test 3: positive.") in the answer and
  compares them with ``comprehension_a``: a "Which ... did PERSON see/hear" question
  needs exactly the keyed statements; a "What was PERSON told about ..." question
  needs every mention of that observation to carry the keyed value.
"""

from __future__ import annotations

import re

COMPREHENSION_CONDITIONS = ("C", "C_F", "C_ctrl_F")
# One observation statement: unit, index, value.  "Bag C draw 1" (a placebo's unrelated bag) is not one.
STATEMENT = re.compile(r'(?<!bag c )\b(draw|witness|test) (\d+)\s*:\s*(?:says\s+)?"?([a-z]+)"?', flags=re.I)


def statements(text: str) -> dict[tuple[str, int], set[str]]:
    """(unit, index) -> the values stated for it, lower case."""
    found: dict[tuple[str, int], set[str]] = {}
    for unit, index, value in STATEMENT.findall(text.lower()):
        found.setdefault((unit, int(index)), set()).add(value)
    return found


def score_comprehension(question: str, answer: str, text: str | None) -> float | None:
    """1.0 correct, 0.0 wrong, None if the response states no observation at all."""
    if not text:
        return None
    keyed = {k: next(iter(v)) for k, v in statements(answer).items()}
    said = statements(text)
    if not said:
        return None
    if question.lower().startswith("which"):  # exactly the observations PERSON had
        return float(set(said) == set(keyed) and all(said[k] == {v} for k, v in keyed.items()))
    return float(all(said.get(k) == {v} for k, v in keyed.items()))  # what PERSON was told
