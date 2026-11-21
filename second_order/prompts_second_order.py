"""CKD prompts for every condition in ``conditions.CONDITIONS``.

All answer-format text (the reasoning instruction, the JSON or ``Choice:``
format, and the options) is cut verbatim from the first-order prompt built by
``latent_beliefs.ckd.prompts`` for the same case and output, so the existing
strict parsers apply unchanged.  Every prompt is the profile, then (about
PERSON) a paragraph on what PERSON knows, then the question and the format:

* **A**, **B**: the model's own belief from the full profile or PERSON's copy.
* **C**: the full profile; the paragraph says how PERSON's copy differs.
* **M**, **D**: the profile shown is exactly what PERSON has (PERSON's copy, or the full profile).
* **False setting**: C_F, M_F and D_F say a colleague read PERSON the profile;
  in C_F the colleague read one line wrongly.

``comprehension`` gives the follow-up question asked after a C answer.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pandas as pd

from latent_beliefs.ckd.cases import PROFILE_FEATURES, read_cases
from latent_beliefs.ckd.prompts import task_specs
from second_order import config as C
from second_order.comprehension import Comprehension, make
from second_order.conditions import (
    CONDITIONS,
    field_label,
    flip_field,
    flipped_value,
    model_profile,
    profile_line,
    withheld_fields,
)

PROFILE_HEADER = "Patient profile:\n"
FORMAT_START = "Provide your reasoning first."
CKD = "chronic kidney disease (CKD)"
THRESHOLD_TEXT = {0.05: "5%", 0.10: "10%", 0.20: "20%", round(1.0 / 3.0, 12): "33.3%"}

# Words for PERSON; the question and paragraph templates use these keys.
PERSON = {
    "A": "John", "subj": "he", "Subj": "He", "poss": "his", "Poss": "His", "for": "John",
    "is": "is", "has": "has", "follows": "follows", "Does": "Does", "obj": "him",
    "refl": "himself", "believes": "believes",
}

# Question sentences redirected at the agent; {..} are PERSON keys.
AGENT_QUESTIONS = {
    "probability": (
        "What probability would {A} assign to this patient having " + CKD + "? "
        "Give your best estimate of {poss} probability."
    ),
    "decision_no_threshold": "Would {A} recommend screening this patient for " + CKD + "?",
    "threshold": (
        "{A} {follows} this rule: the patient should be screened for " + CKD + " if the "
        "probability that they have CKD is at least {tau}. {Does} {A} screen this patient?"
    ),
    "bet": "{A} {is} offered the following bet:\n\n{bullets}\n\nWould {A} accept this bet?",
    "cost": (
        "For {for}, the cost of a false negative, meaning failing to screen a patient who "
        "truly has CKD, is {ratio} times the cost of a false positive, meaning screening a "
        "patient who does not have CKD. Would {A} recommend screening this patient for " + CKD + "?"
    ),
    "likert": "Which option would {A} choose to describe how likely this patient is to have " + CKD + "?",
    "probability_bin_quintile": (
        "Which bin contains the probability that {A} would assign to this patient having " + CKD + "?"
    ),
}

AGENT_INTRO = "{A} {is} a physician with the same training and clinical judgement as you."
# Partial setting: PERSON's copy lists the withheld fields as Not recorded, as in B and M.
COPY_NOT_RECORDED = (
    "{Subj} reviewed an earlier copy of this patient's profile. In {poss} copy the following "
    "fields were listed as Not recorded, and {subj} {has} no other information about them:\n"
    "{fields}\nEverything else in {poss} copy matched the profile above."
)
# M and D: PERSON holds exactly the profile shown (PERSON's copy in M, the full profile in D).
COPY_FULL = "{Subj} reviewed exactly the profile shown above."
# False setting: M_F and D_F share REPORTED_FULL; C_F quotes the misread line.
REPORTED_FULL = (
    "{Subj} did not read this patient's profile {refl}; a colleague read it to {obj}. "
    "What {subj} was told matched the profile shown above exactly."
)
REPORTED_FLIPPED = (
    "{Subj} did not read this patient's profile {refl}; a colleague read it to {obj} and read "
    "one line incorrectly. The colleague told {obj}:\n{line}\n"
    "{Subj} {believes} what {subj} was told and {has} not seen the profile above.\n"
    "Everything else {subj} was told matched the profile above."
)


# ---------------------------------------------------------------- inputs
@lru_cache(maxsize=1)
def cases() -> pd.DataFrame:
    frame = read_cases(C.RELEASE_CASES)
    frame = frame[frame["source_cohort"] == "original500"].copy()
    frame["case_id"] = frame["source_cohort"] + "__" + frame["prompt_id"]
    return frame.sort_values("case_id").set_index("case_id", drop=False)


@lru_cache(maxsize=1)
def bin_boundaries() -> list[float]:
    path = C.RELEASE_PROMPTS / "ckd_same1000_probability_bin_quintile_revised.jsonl"
    with path.open() as handle:
        return json.loads(json.loads(handle.readline())["probability_bin_boundaries"])


@lru_cache(maxsize=1)
def specs() -> dict[str, dict[str, Any]]:
    return {spec["variant_id"]: spec for spec in task_specs(bin_boundaries())}


OUTPUTS = (
    "probability", "decision_no_threshold", "template_01_threshold_10pct",
    "template_02_threshold_20pct", "template_03_threshold_33pct", "bet_payoff_2to1",
    "bet_payoff_4to1", "bet_payoff_9to1", "template_07_cost_fn2", "template_08_cost_fn4",
    "template_09_cost_fn9", "threshold_05pct", "bet_payoff_19to1", "cost_fn19", "likert",
    "probability_bin_quintile",
)  # first-order collection order


def output_kind(output: str) -> str:
    spec = specs()[output]
    if output in {"probability", "decision_no_threshold", "likert", "probability_bin_quintile"}:
        return output
    if "threshold" in spec:
        return "threshold"
    if "net_gain_points" in spec:
        return "bet"
    return "cost"


# ---------------------------------------------------------------- pieces
def first_order_prompt(output: str, profile: str) -> str:
    return specs()[output]["builder"](profile)


def split_first_order(output: str, profile: str) -> tuple[str, str]:
    """(question, answer-format block) of the first-order prompt, verbatim."""
    prompt = first_order_prompt(output, profile)
    q_end = prompt.index("\n\n" + FORMAT_START)
    p_start = prompt.index(PROFILE_HEADER)
    question = prompt[:q_end]
    fmt = prompt[q_end + 2 : p_start].rstrip("\n")
    assert prompt == f"{question}\n\n{fmt}\n\n{PROFILE_HEADER}{profile}\n"
    return question, fmt


def agent_question(output: str, profile: str) -> str:
    kind = output_kind(output)
    spec = specs()[output]
    words = dict(PERSON)
    if kind == "threshold":
        words["tau"] = THRESHOLD_TEXT[round(float(spec["threshold"]), 12)]
    if kind == "bet":
        question, _ = split_first_order(output, profile)
        words["bullets"] = question.split("\n\n")[1]
    if kind == "cost":
        words["ratio"] = spec["false_negative_cost_ratio"]
    return AGENT_QUESTIONS[kind].format(**words)


def agent_paragraph(case: pd.Series, condition: str) -> str:
    """Who PERSON is and what PERSON knows of the profile."""
    cond = CONDITIONS[condition]
    told = cond.setting == "false"
    if cond.role in ("D", "M"):  # PERSON holds exactly the profile shown
        copy = (REPORTED_FULL if told else COPY_FULL).format(**PERSON)
    elif told:
        flip = flip_field()
        copy = REPORTED_FLIPPED.format(line=profile_line(case, flip, flipped_value(case, flip)), **PERSON)
    else:
        listing = "\n".join(f"- {field_label(f)}" for f in withheld_fields())
        copy = COPY_NOT_RECORDED.format(fields=listing, **PERSON)
    return f"{AGENT_INTRO.format(**PERSON)} {copy}"


# ---------------------------------------------------------------- builders
def build_prompt(case_id: str, condition: str, output: str) -> str:
    """The prompt for one case, condition and output."""
    case = cases().loc[case_id]
    cond = CONDITIONS[condition]
    shown = model_profile(case, condition)
    question, fmt = split_first_order(output, shown)
    paragraph = ""
    if cond.about == "person":
        question = agent_question(output, shown)
        paragraph = agent_paragraph(case, condition) + "\n\n"
    return f"{PROFILE_HEADER}{shown}\n\n{paragraph}{question}\n\n{fmt}\n"


def profile_end_offset(prompt: str, case_id: str, condition: str) -> int:
    """Character offset just past the profile block."""
    block = PROFILE_HEADER + model_profile(cases().loc[case_id], condition)
    return prompt.index(block) + len(block)


# ---------------------------------------------------------------- comprehension
BINARY_FIELDS = ("hypertension_self_reported", "diabetes_self_reported", "cvd_any",
                 "physical_activity_any")


def comprehension(case_id: str, condition: str) -> Comprehension:
    """Follow-up question after a C answer: which fields PERSON lacked, or which line was misread."""
    cond = CONDITIONS[condition]
    if cond.role != "C":
        raise ValueError(f"comprehension questions follow C conditions, not {condition}")
    key = f"{case_id}|{condition}"
    if cond.setting == "false":
        flip = flip_field()
        stem = f"Which line of the patient's profile did the colleague read to {PERSON['obj']} incorrectly?"
        labels = [field_label(f) for f in BINARY_FIELDS if f != flip]
        return make(stem, field_label(flip), labels[:2] + ["None: everything the colleague read was correct"], key)
    fields = withheld_fields()
    # Distractors: the list without its first field, and that list plus a field PERSON did have.
    extra = next(f for f in BINARY_FIELDS if f not in fields)
    rest = fields[1:]
    with_extra = [f for f in PROFILE_FEATURES if f in set(rest) | {extra}]
    listing = lambda fs: "; ".join(field_label(f) for f in fs)  # noqa: E731
    stem = f"Which of the following lists exactly the fields that {PERSON['A']} had no information about?"
    return make(stem, listing(fields), [listing(rest), listing(with_extra), f"None: {PERSON['A']} had the full profile"], key)
