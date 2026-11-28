"""The two datasets, loaded verbatim: every prompt is sent exactly as the dataset gives it.

* ``ckd``: ``datasets/ckd_second_order_v3`` (see docs/ckd.md).  150
  patients; per condition one probability question and 15 multiple-choice questions
  in two option orders, standalone comprehension checks after C, a high-threshold
  supplement and role-swap diagnostics.  One call per row; A and B
  have two replicates in the file.  The large CSVs are stored gzipped.
* ``bespoke``: ``datasets/second_order_simple_prompts.csv``.  20 items (2 of them
  placebo) in three families, with Bayes answers; the numeric probability only,
  ``samples`` calls per item, condition and run, and a free-text comprehension
  follow-up after C.

``rows(dataset)`` gives one row per prompt with the condition key
(``conditions.KEYS``), the output name and the metadata the analysis needs.
CKD outputs are ``<question_id>/<order>`` (``probability/normal``, ``bet_4/flip``,
``check/normal``, ...); the bespoke output is ``probability``.

Both datasets ask for a probability the same way: reasoning first, then the JSON
object ``{"probability": <number>}`` with a number between 0 and 1.  CKD multiple-choice
questions also ask for reasoning first, then a final ``Choice: <letter>`` line (the
loader rewrites the release's single-letter instruction, ``CHOICE_ASK``).
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from second_order import config as C
from second_order.beliefs import clip_logit as logit
from second_order.conditions import condition_key

CKD_FILES = {"main": "ckd_second_order_v3.csv", "high_thresholds": "ckd_second_order_v3_high_thresholds.csv",
             "role_swap": "ckd_second_order_v3_role_swap.csv"}
CKD_META = ["label", "person_name", "changed_facts", "check_answer", "direction", "direction_assignment",
            "block_order", "check_fact", "check_relation"]
BESPOKE_META = ["family", "person", "n_shown", "n_hidden_or_altered", "model_evidence", "person_evidence",
                "normative_p", "normative_logodds", "full_leak_p", "comprehension_q", "comprehension_a",
                "partial_c_indices", "check_relation"]
BESPOKE_OUTPUT = "probability"
# The released v3 multiple-choice prompts (questions, checks and probes) end with ARCHIVED_CHOICE_ASK.
# Qwen3 with thinking off reasons anyway, so the loader asks for reasoning first and a final Choice
# line instead, as Smolin & Wilder did.
ARCHIVED_CHOICE_ASK = "Answer with a single letter."
CHOICE_ASK = ("Provide your reasoning first.\nThen output your answer on a new line in exactly this format:\n"
              "Choice: <the letter of one option>")


# Dataset files
def ckd_dir() -> Path:
    return C.resolve(C.load()["datasets"]["ckd"]["dir"])


def open_csv(path: Path) -> pd.DataFrame:
    """A dataset CSV (or its ``.gz``), every column a string, empty cells kept."""
    path = path if path.exists() else path.with_name(path.name + ".gz")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


@lru_cache(maxsize=1)
def patients() -> pd.DataFrame:
    """One row per CKD patient: label, PERSON's name, record order, false-record direction."""
    return open_csv(ckd_dir() / "patients.csv").set_index("item_id", drop=False)


# Dataset rows
@lru_cache(maxsize=None)
def _ckd_battery(battery: str) -> pd.DataFrame:
    raw = open_csv(ckd_dir() / CKD_FILES[battery])
    out = pd.DataFrame({
        "dataset": "ckd", "battery": battery, "case_id": raw.item_id,
        "condition": [condition_key("ckd", s, c) for s, c in zip(raw.setting, raw.case)],
        "output": raw.question_id + "/" + raw.order, "question_id": raw.question_id, "order": raw.order,
        "replicate": raw.replicate.astype(int) - 1, "prompt": raw.prompt, "placebo": False,
    })
    frame = pd.concat([out, raw[CKD_META]], axis=1)
    # The release shares A and the two-chart D between the settings; the false-record trials get their
    # own copies so that every chart the model owns in them carries the same truth cue.
    verified = frame[frame.condition.isin(["A", "D"])].copy()
    verified["condition"] += "_F"
    frame = pd.concat([frame, verified], ignore_index=True)
    choice = frame.question_id != "probability"
    if not frame.prompt[choice].str.endswith(ARCHIVED_CHOICE_ASK).all():
        raise ValueError(f"{battery}: a multiple-choice prompt does not end with {ARCHIVED_CHOICE_ASK!r}")
    frame.loc[choice, "prompt"] = frame.loc[choice, "prompt"].str[:-len(ARCHIVED_CHOICE_ASK)] + CHOICE_ASK
    false = frame.condition.str.endswith("_F")
    frame.loc[false, "prompt"] = frame.loc[false, "prompt"].str.replace(
        "Your record for this patient:", "Your record for this patient: Your chart gives the verified patient information.",
        regex=False)
    return frame


FOLLOWUP_VALUE = {"draw": "<result>", "test": "<result>", "witness": 'says "<name>"'}
UNITS = {"draws": "draw", "witnesses": "witness", "tests": "test"}


def followup_question(question: str) -> str:
    """A bespoke follow-up question with the statement format the scorer reads appended."""
    if not question:
        return question
    told = re.search(r"\babout (draw|witness|test) (\d+)\?$", question)
    if told:
        unit, index = told.groups()
        return f'{question} Answer on one line in the form "{unit.capitalize()} {index}: {FOLLOWUP_VALUE[unit]}."'
    which = re.match(r"Which (draws|witnesses|tests)\b", question)
    if not which:
        raise ValueError(f"unrecognised comprehension question: {question!r}")
    unit = UNITS[which.group(1)]
    return f'{question} Write one line per {unit} in the form "{unit.capitalize()} <number>: {FOLLOWUP_VALUE[unit]}."'


@lru_cache(maxsize=1)
def _bespoke() -> pd.DataFrame:
    raw = open_csv(C.resolve(C.load()["datasets"]["bespoke"]["file"]))
    raw["comprehension_q"] = raw.comprehension_q.map(followup_question)
    out = pd.DataFrame({
        "dataset": "bespoke", "battery": "bespoke", "case_id": raw.item_id,
        "condition": [condition_key("bespoke", s, c) for s, c in zip(raw.setting, raw.case)],
        "output": BESPOKE_OUTPUT, "question_id": BESPOKE_OUTPUT, "order": "", "replicate": 0,
        "prompt": raw.prompt, "placebo": raw.setting == "placebo",
    })
    return pd.concat([out, raw[BESPOKE_META]], axis=1)


def rows(dataset: str, batteries=None) -> pd.DataFrame:
    """Every prompt of a dataset (CKD: of the configured batteries), one row each."""
    if dataset == "bespoke":
        return _bespoke()
    requested = C.load()["datasets"]["ckd"]["batteries"] if batteries is None else batteries
    return _ckd_rows(tuple(requested))


@lru_cache(maxsize=4)
def _ckd_rows(batteries: tuple[str, ...]) -> pd.DataFrame:
    return pd.concat([_ckd_battery(b) for b in batteries], ignore_index=True)


def lookup(dataset: str) -> dict[tuple, dict]:
    batteries = tuple(C.load()["datasets"]["ckd"]["batteries"]) if dataset == "ckd" else ()
    return _lookup(dataset, batteries)


@lru_cache(maxsize=None)
def _lookup(dataset: str, batteries: tuple[str, ...]) -> dict[tuple, dict]:
    """(case_id, condition, output, replicate) -> row metadata (bespoke: replicate 0)."""
    frame = rows(dataset, batteries)
    keys = zip(frame.case_id, frame.condition, frame.output, frame.replicate)
    return dict(zip(keys, frame.to_dict(orient="records")))


def row(dataset: str, case_id: str, condition: str, output: str, replicate: int = 0) -> dict:
    return lookup(dataset)[(case_id, condition, output, replicate if dataset == "ckd" else 0)]


def case_ids(dataset: str) -> list[str]:
    return sorted(rows(dataset).case_id.unique())


def placebo_ids() -> set[str]:
    b = rows("bespoke")
    return set(b.case_id[b.placebo])


def samples(dataset: str) -> int:
    return int(C.load()["datasets"]["bespoke"]["samples"]) if dataset == "bespoke" else 1


def replicates(dataset: str, condition: str) -> int:
    """Runs to generate per bespoke row (CKD rows carry their own replicate)."""
    if dataset == "ckd":
        return 1
    return int(C.load()["datasets"]["bespoke"]["replicates"].get(condition, 1))


def max_tokens(dataset: str, output: str) -> int:
    """Output-token limit: probability answers get ``probability``, multiple-choice answers ``choice``."""
    limits = C.load()["datasets"][dataset]["max_tokens"]
    return int(limits["probability" if output.split("/")[0] == "probability" else "choice"])


# Answer formats
OPTION = re.compile(r"^([A-E])\. (.+)$", flags=re.M)
ACT = {"Recommend screening for CKD": 1.0, "Screen this patient for CKD": 1.0, "Accept the bet": 1.0,
       "Recommend not screening for CKD": 0.0, "Do not screen this patient for CKD": 0.0, "Decline the bet": 0.0}
LIKERT = {"Very unlikely": 1.0, "Somewhat unlikely": 2.0, "Neither likely nor unlikely": 3.0,
          "Somewhat likely": 4.0, "Very likely": 5.0}
QUINTILE = {"Less than 5.07%": 1.0, "5.07% to less than 7.49%": 2.0, "7.49% to less than 13.02%": 3.0,
            "13.02% to less than 29.72%": 4.0, "29.72% or higher": 5.0}
OPTION_SCORES = {"likert": LIKERT, "quintile": QUINTILE}
CHOICE_LINE = re.compile(r"(?:^|[\s>#*_`:])choice[\s*_`]*:[\s*_`]*(.*?)\s*$", flags=re.I)
LETTER = re.compile(r"\s*[*_`(\[]*([A-E])[*_`)\].:]*(?:\s+(.*?))?[\s.*_`]*", flags=re.S)


def options(prompt: str) -> dict[str, str]:
    """Letter -> option text, from a multiple-choice prompt."""
    return dict(OPTION.findall(prompt))


def option_value(question_id: str, text: str) -> float | str:
    """The meaning of an option: 1/0 (act or not), a 1-5 category, or Yes/No (checks)."""
    if question_id in ("check", "role_check"):
        return text
    return OPTION_SCORES.get(question_id, ACT)[text]


def parse_letter(text: str, opts: dict[str, str]) -> str | None:
    """The chosen letter: a lone letter (optionally with its option text), or the option text alone."""
    match = LETTER.fullmatch(text)
    if match and match.group(1) in opts:
        rest = match.group(2)
        if not rest or _norm(rest) == _norm(opts[match.group(1)]):
            return match.group(1)
    by_text = [k for k, v in opts.items() if _norm(v) == _norm(text)]
    return by_text[0] if len(by_text) == 1 else None


def final_choice(text: str) -> str | None:
    """The answer on the last ``Choice:`` line (Markdown emphasis allowed), with any lines after it appended.

    A model often repeats the option text on the next line, or puts the whole answer there after a
    bare ``### Choice:`` heading; ``parse_letter`` accepts those and rejects anything else after the
    Choice line.  None if there is no Choice line.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    at = [k for k, line in enumerate(lines) if CHOICE_LINE.search(line)]
    if not at:
        return None
    return " ".join([CHOICE_LINE.search(lines[at[-1]]).group(1), *lines[at[-1] + 1:]]).strip()


def parse_probability(text: str | None) -> tuple[float | None, str | None]:
    """Accept only the terminal JSON answer. Fenced JSON, and Markdown emphasis around it, are also accepted."""
    if not text:
        return None, "invalid"
    answer = text.strip()
    if not answer.endswith("```"):
        answer = answer.rstrip("*_` \t\n")  # Markdown emphasis around the JSON: **{"probability": 0.75}**
    if answer.endswith("```"):
        fence = re.search(r"```(?:json)?\s*([^`]*)```$", answer, re.I)
        if not fence:
            return None, "invalid"
        answer = fence.group(1).strip()
    match = re.search(r'\{[^{}]*\}\Z', answer)
    if not match:
        return None, "invalid"
    try:
        pairs = json.loads(match.group(), object_pairs_hook=list)
    except (ValueError, OverflowError):
        return None, "invalid"
    if len(pairs) == 1 and pairs[0][0] == "probability":
        value = pairs[0][1]
        if type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value):
            return float(value), "json"
    return None, "invalid"


def fallback_probability(text: str | None) -> float | None:
    """Optional alternative score for a terminal quoted key or percentage."""
    m = re.search(r'"probability"\s*:\s*"?([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(%?)"?\s*\}?\s*$', text or "")
    if not m:
        return None
    value = float(m[1])
    if m[2]:
        value /= 100
    return value if 0 <= value <= 1 else None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text).strip().strip(".").strip()).lower()


def parse(dataset: str, output: str, prompt: str, text: str | None, *,
          check_answer: str | None = None) -> tuple[float | str | None, str | None]:
    """(value, method): the probability in [0, 1], an option's meaning, or None if unparseable."""
    if text is None:
        return None, None
    question = output.split("/")[0]
    if question == "probability":
        return parse_probability(text)
    opts = options(prompt)
    answer = final_choice(text)
    letter = parse_letter(text if answer is None else answer, opts)
    if not letter:
        return None, None
    if check_answer is not None:
        return float(letter == check_answer), "letter"
    return option_value(question, opts[letter]), "letter"


# Bespoke comprehension follow-ups, asked after each C answer (C, C_F, C_ctrl_F) and scored by the
# observation statements in the answer ("Draw 2: red.", "Witness 1: says "Sam".", "Test 3: positive."):
# "Which ... did PERSON see/hear" needs exactly the keyed statements; "What was PERSON told about ..."
# needs every mention of that observation to carry the keyed value.  Markdown emphasis is ignored.
# ``followup_question`` appends the statement format to each released question.
COMPREHENSION_CONDITIONS = ("C", "C_F", "C_ctrl_F")
# One observation statement: unit, index, value.  "Bag C draw 1" (a placebo's unrelated bag) is not one.
STATEMENT = re.compile(r'(?<!bag c )\b(draw|witness|test) (\d+)\s*:\s*(?:says\s+)?\\?"?([a-z]+)\\?"?', flags=re.I)


def statements(text: str) -> dict[tuple[str, int], set[str]]:
    """(unit, index) -> the values stated for it, lower case."""
    found: dict[tuple[str, int], set[str]] = {}
    for unit, index, value in STATEMENT.findall(re.sub(r"[*_`]", "", text.lower())):
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
