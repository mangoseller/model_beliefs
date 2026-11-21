"""Comprehension follow-ups: did the model track what the agent saw?

After each answer in a C condition (``experiment.comprehension``), a second
user turn asks a multiple-choice question about the agent's evidence: which
fields the agent's copy lacked, which line the agent was told wrongly, or
(bespoke dataset) which evidence the agent had.  The conversation is the C
prompt, the model's C response verbatim, and the question.  Answers are
parsed strictly from the last ``Choice:`` line, by letter or by the option
text, and scored against the known answer.  The analysis reports accuracy
and leakage on the cases whose comprehension answers were all correct.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

OUTPUT = "comprehension"  # output name of a follow-up item
CHOICE = re.compile(r"^[\s*_#`>]*Choice[\s*_`]*:[\s*_`]*(.*?)[\s*_`]*$", flags=re.I | re.M)


@dataclass(frozen=True)
class Comprehension:
    stem: str
    options: tuple[str, ...]
    answer: int  # index of the correct option

    @property
    def question(self) -> str:
        """The follow-up user turn."""
        lines = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(self.options))
        return (f"{self.stem}\n\n"
                "Output exactly one multiple-choice answer on a new line in this exact format:\n"
                "Choice: <one option exactly as written below>\n\n"
                f"Options:\n{lines}\n")

    @property
    def answer_letter(self) -> str:
        return chr(65 + self.answer)


def make(stem: str, correct: str, distractors: Sequence[str], key: str) -> Comprehension:
    """Options = the correct one and distinct distractors, in an order fixed by ``key``."""
    options = [correct] + [d for i, d in enumerate(distractors) if d != correct and d not in distractors[:i]]
    order = sorted(range(len(options)),
                   key=lambda i: hashlib.sha256(f"{key}|{options[i]}".encode()).hexdigest())
    options = [options[i] for i in order]
    return Comprehension(stem, tuple(options), options.index(correct))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().strip(".").strip()).lower()


def parse_choice(text: str | None, options: Sequence[str]) -> int | None:
    """Index of the chosen option from the last ``Choice:`` line, or None."""
    if not text:
        return None
    matches = CHOICE.findall(text)
    if not matches:
        return None
    raw = matches[-1].strip()
    for i, option in enumerate(options):
        if _norm(raw) == _norm(option):
            return i
    letter = re.fullmatch(r"\(?([A-Za-z])\)?[.):]?(?:\s+(.+))?", raw)
    if not letter:
        return None
    index = ord(letter.group(1).upper()) - 65
    if not 0 <= index < len(options):
        return None
    if letter.group(2) is not None and _norm(letter.group(2)) != _norm(options[index]):
        return None  # letter and text disagree
    return index
