"""The bespoke dataset: answers and comprehension follow-ups.

Prompts come verbatim from ``datasets/second_order_simple_prompts.csv`` (``datasets.rows``).
Every row carries its Bayes answer given the believer's evidence (``normative_logodds``:
the model's for A and B, PERSON's otherwise), and C rows the answer PERSON would give
with the model's evidence (``full_leak_p``).

* Answers are read from the JSON object only (``parse_response``).  The first-order
  parser also accepts any number after the word "probability"; the propositions here
  contain numbers ("Draw 2"), so that fallback is not used.
* After each C answer (C, C_F, C_ctrl_F), a second turn asks the row's free-text
  ``comprehension_q``.  ``score_comprehension`` reads the observation statements
  ("Draw 2: red.", "Witness 1: says "Sam".", "Test 3: positive.") in the answer and
  compares them with ``comprehension_a``: a "Which ... did PERSON see/hear" question
  needs exactly the keyed statements; a "What was PERSON told about ..." question
  needs every mention of that observation to carry the keyed value.
"""

from __future__ import annotations

import json
import re

from latent_beliefs.ckd.parse import normalize_probability

QUOTED_KEY = re.compile(r'"probability"\s*:\s*"?([0-9]*\.?[0-9]+)\s*(%?)"?', flags=re.I)
COMPREHENSION_CONDITIONS = ("C", "C_F", "C_ctrl_F")
# One observation statement: unit, index, value.  "Bag C draw 1" (a placebo's unrelated bag) is not one.
STATEMENT = re.compile(r'(?<!bag c )\b(draw|witness|test) (\d+)\s*:\s*(?:says\s+)?"?([a-z]+)"?', flags=re.I)


def parse_response(text: str | None) -> tuple[float | None, str | None]:
    """The answer from the last JSON object with a ``probability`` key (or a quoted key)."""
    if not text:
        return None, None
    for candidate in reversed(re.findall(r"\{[^{}]*\}", text, flags=re.DOTALL)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "probability" in parsed:
            p = normalize_probability(parsed["probability"])
            if p is not None:
                return p, "json"
    for number, percent in reversed(QUOTED_KEY.findall(text)):
        p = normalize_probability(number + percent)
        if p is not None:
            return p, "quoted_key"
    return None, None


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
