"""The two datasets, loaded verbatim: every prompt is sent exactly as the dataset gives it.

* ``ckd``: ``datasets/ckd_second_order_v3`` (its README describes the battery).  150
  patients; per condition one probability question (0 to 100) and 15 multiple-choice
  questions in two option orders, standalone comprehension checks after C, a
  high-threshold supplement and role-swap diagnostics.  One call per row; A and B
  have two replicates in the file.  The large CSVs are stored gzipped.
* ``bespoke``: ``datasets/second_order_simple_prompts.csv``.  20 items (2 of them
  placebo) in three families, with Bayes answers; the numeric probability only,
  ``samples`` calls per item, condition and run, and a free-text comprehension
  follow-up after C.

``rows(dataset)`` gives one row per prompt with the condition key
(``conditions.KEYS``), the output name and the metadata the analysis needs.
CKD outputs are ``<question_id>/<order>`` (``probability/normal``, ``bet_4/flip``,
``check/normal``, ...); the bespoke output is ``probability``.
"""

from __future__ import annotations

import hashlib
import gzip
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import config as C
from second_order.conditions import condition_key

CKD_FILES = {"main": "ckd_second_order_v3.csv", "high_thresholds": "ckd_second_order_v3_high_thresholds.csv",
             "role_swap": "ckd_second_order_v3_role_swap.csv"}
CKD_META = ["label", "person_name", "changed_facts", "check_answer", "direction", "direction_assignment",
            "block_order", "check_fact", "check_relation"]
BESPOKE_META = ["family", "person", "n_shown", "n_hidden_or_altered", "model_evidence", "person_evidence",
                "normative_p", "normative_logodds", "full_leak_p", "comprehension_q", "comprehension_a",
                "partial_c_indices", "report_position", "check_relation"]
BESPOKE_OUTPUT = "probability"


# ---------------------------------------------------------------- files
def ckd_dir() -> Path:
    return C.resolve(C.load()["datasets"]["ckd"]["dir"])


def open_csv(path: Path) -> pd.DataFrame:
    """A dataset CSV (or its ``.gz``), every column a string, empty cells kept."""
    path = path if path.exists() else path.with_name(path.name + ".gz")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def ckd_checksums() -> dict[str, bool]:
    """Each released CKD file against ``SHA256SUMS`` (gzipped files are checked decompressed)."""
    folder, out = ckd_dir(), {}
    for line in (folder / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        path = folder / name
        data = path.read_bytes() if path.exists() else gzip.decompress((folder / f"{name}.gz").read_bytes())
        out[name] = hashlib.sha256(data).hexdigest() == digest
    return out


@lru_cache(maxsize=1)
def patients() -> pd.DataFrame:
    """One row per CKD patient: label, PERSON's name, record order, false-record direction."""
    return open_csv(ckd_dir() / "patients.csv").set_index("item_id", drop=False)


# ---------------------------------------------------------------- rows
@lru_cache(maxsize=None)
def _ckd_battery(battery: str) -> pd.DataFrame:
    raw = open_csv(ckd_dir() / CKD_FILES[battery])
    out = pd.DataFrame({
        "dataset": "ckd", "battery": battery, "case_id": raw.item_id,
        "condition": [condition_key("ckd", s, c) for s, c in zip(raw.setting, raw.case)],
        "output": raw.question_id + "/" + raw.order, "question_id": raw.question_id, "order": raw.order,
        "replicate": raw.replicate.astype(int) - 1, "prompt": raw.prompt, "placebo": False,
    })
    return pd.concat([out, raw[CKD_META]], axis=1)


@lru_cache(maxsize=1)
def _bespoke() -> pd.DataFrame:
    raw = open_csv(C.resolve(C.load()["datasets"]["bespoke"]["file"]))
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
    batteries = batteries or C.load()["datasets"]["ckd"]["batteries"]
    return pd.concat([_ckd_battery(b) for b in batteries], ignore_index=True)


@lru_cache(maxsize=None)
def lookup(dataset: str) -> dict[tuple, dict]:
    """(case_id, condition, output, replicate) -> row metadata (bespoke: replicate 0)."""
    frame = rows(dataset, list(CKD_FILES) if dataset == "ckd" else None)
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


def max_tokens(dataset: str) -> int:
    return int(C.load()["datasets"][dataset]["max_tokens"])


# ---------------------------------------------------------------- answers
OPTION = re.compile(r"^([A-E])\. (.+)$", flags=re.M)
ACT = {"Recommend screening for CKD": 1.0, "Screen this patient for CKD": 1.0, "Accept the bet": 1.0,
       "Recommend not screening for CKD": 0.0, "Do not screen this patient for CKD": 0.0, "Decline the bet": 0.0}
LIKERT = {"Very unlikely": 1.0, "Somewhat unlikely": 2.0, "Neither likely nor unlikely": 3.0,
          "Somewhat likely": 4.0, "Very likely": 5.0}
QUINTILE = {"Less than 5.07%": 1.0, "5.07% to less than 7.49%": 2.0, "7.49% to less than 13.02%": 3.0,
            "13.02% to less than 29.72%": 4.0, "29.72% or higher": 5.0}
NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.]*\d)")
LETTER = re.compile(r"\s*[*_`(\[]*([A-E])[*_`)\].:]*(?:\s+(.*?))?[\s.*_`]*", flags=re.S)


def options(prompt: str) -> dict[str, str]:
    """Letter -> option text, from a multiple-choice prompt."""
    return dict(OPTION.findall(prompt))


def option_value(question_id: str, text: str) -> float | str:
    """The meaning of an option: 1/0 (act or not), a 1-5 category, or Yes/No (checks)."""
    if question_id == "likert":
        return LIKERT[text]
    if question_id == "quintile":
        return QUINTILE[text]
    if question_id in ("check", "role_check"):
        return text
    return ACT[text]


def parse_letter(text: str, opts: dict[str, str]) -> str | None:
    """The chosen letter: a lone letter (optionally with its option text), or the option text alone."""
    match = LETTER.fullmatch(text)
    if match and match.group(1) in opts:
        rest = match.group(2)
        if not rest or _norm(rest) == _norm(opts[match.group(1)]):
            return match.group(1)
    by_text = [k for k, v in opts.items() if _norm(v) == _norm(text)]
    return by_text[0] if len(by_text) == 1 else None


def parse_percent(text: str) -> float | None:
    """A probability stated from 0 to 100: the answer must hold exactly one number."""
    numbers = NUMBER.findall(text.replace(",", ""))
    if len(numbers) != 1:
        return None
    value = float(numbers[0])
    return value / 100 if 0 <= value <= 100 else None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().strip(".").strip()).lower()


def parse(dataset: str, output: str, prompt: str, text: str | None) -> tuple[float | str | None, str | None]:
    """(value, method): the probability in [0, 1], an option's meaning, or None if unparseable."""
    if text is None:
        return None, None
    if dataset == "bespoke":
        from second_order.bespoke import parse_response

        return parse_response(text)
    question = output.split("/")[0]
    if question == "probability":
        value = parse_percent(text)
        return (value, "number") if value is not None else (None, None)
    opts = options(prompt)
    letter = parse_letter(text, opts)
    return (option_value(question, opts[letter]), "letter") if letter else (None, None)


def is_belief_output(dataset: str, output: str) -> bool:
    """Outputs that measure a belief (not the comprehension or role checks)."""
    return dataset == "bespoke" or output.split("/")[0] not in ("check", "role_check")


def logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p) - np.log1p(-p)




