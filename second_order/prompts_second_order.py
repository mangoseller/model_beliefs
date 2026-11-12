"""Prompts for every condition in ``conditions.CONDITIONS``.

All answer-format text (the reasoning instruction, the JSON or ``Choice:``
format, and the options) is cut verbatim from the first-order prompt built by
``latent_beliefs.ckd.prompts`` for the same case and output, so the existing
strict parsers apply unchanged.

* **A**: the first-order prompt, rebuilt here byte for byte (tested against
  the released prompts).
* **B\***: the first-order prompt with the agent's copy in place of the full
  profile (fields withheld, or one field flipped).
* **C\***: the full profile, an agent paragraph saying how the agent's copy
  differs, the question redirected at the agent, and the first-order answer
  format.
* **D**: as C, but the agent has the full profile.
* **M\***: as D, but the profile shown is the agent's copy (the same bytes as
  the matching B), so the model has nothing private.

Layout ``plan`` puts the profile first; ``repo`` keeps the first-order order
with the agent paragraph after the profile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import pandas as pd

from latent_beliefs.ckd.cases import read_cases
from latent_beliefs.ckd.prompts import task_specs
from second_order import config as C
from second_order.conditions import (
    CONDITIONS,
    copy_profile,
    field_label,
    flipped_value,
    model_profile,
    pair_spec,
    profile_line,
    render_profile,
    withheld_names,
)

PROFILE_HEADER = "Patient profile:\n"
FORMAT_START = "Provide your reasoning first."
CKD = "chronic kidney disease (CKD)"
THRESHOLD_TEXT = {0.05: "5%", 0.10: "10%", 0.20: "20%", round(1.0 / 3.0, 12): "33.3%"}

# Person words: the only tokens allowed to differ between John and user prompts.
PERSON = {
    "john": {
        "A": "John", "subj": "he", "Subj": "He", "poss": "his", "Poss": "His",
        "for": "John", "is": "is", "has": "has", "follows": "follows", "Does": "Does",
        "trusts": "trusts",
    },
    "user": {
        "A": "I", "subj": "I", "Subj": "I", "poss": "my", "Poss": "My",
        "for": "me", "is": "am", "has": "have", "follows": "follow", "Does": "Do",
        "trusts": "trust",
    },
}
PERSON_WORDS = {w for p in PERSON.values() for w in p.values()} | {"John"}

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
COPY_PARTIAL = (
    "{Subj} reviewed an earlier copy of this patient's profile. {Poss} copy did not include "
    "the following fields, and {subj} {has} no other information about them:\n{fields}\n"
    "Everything else in {poss} copy matched the profile above."
)
# Option (a): the withheld fields are printed as "Not recorded" in B and M, and C
# describes the agent's copy the same way, so the agent's evidence and B's are identical.
COPY_NOT_RECORDED = (
    "{Subj} reviewed an earlier copy of this patient's profile. In {poss} copy the following "
    "fields were listed as Not recorded, and {subj} {has} no other information about them:\n"
    "{fields}\nEverything else in {poss} copy matched the profile above."
)
COPY_FLIPPED = (
    "{Subj} reviewed an earlier copy of this patient's profile. {Poss} copy differed from the "
    "profile above in one line. {Poss} copy said:\n{line}\n"
    "{Subj} {trusts} {poss} copy and {has} not seen the corrected profile.\n"
    "Everything else in {poss} copy matched the profile above."
)
# Used for D and for M.  It must read sensibly when the profile shown is the agent's copy
# (fields "Not recorded", or one flipped), so it does not say "every field included".
COPY_FULL = "{Subj} reviewed exactly the profile shown above."


@dataclass(frozen=True)
class PromptRow:
    case_id: str
    condition: str
    output: str
    prompt: str

    def as_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "condition": self.condition, "output": self.output,
                "prompt": self.prompt}


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


def agent_question(output: str, framing: str, profile: str) -> str:
    kind = output_kind(output)
    spec = specs()[output]
    words = dict(PERSON[framing])
    if kind == "threshold":
        words["tau"] = THRESHOLD_TEXT[round(float(spec["threshold"]), 12)]
    if kind == "bet":
        question, _ = split_first_order(output, profile)
        words["bullets"] = question.split("\n\n")[1]
    if kind == "cost":
        words["ratio"] = spec["false_negative_cost_ratio"]
    return AGENT_QUESTIONS[kind].format(**words)


def agent_paragraph(case: pd.Series, condition: str, *, withheld: list[str] | None = None,
                    missing: str | None = None) -> str:
    """Who the agent is and what copy of the profile the agent holds."""
    cond = CONDITIONS[condition]
    words = dict(PERSON[cond.framing])
    intro = AGENT_INTRO.format(**words)
    if cond.role in ("D", "M"):  # the agent holds exactly the profile shown
        copy = COPY_FULL.format(**words)
    else:
        fields, flip = pair_spec(cond.pair)
        if flip is not None:
            line = profile_line(case, flip, flipped_value(case, flip))
            copy = COPY_FLIPPED.format(line=line, **words)
        else:
            fields = list(withheld) if withheld is not None else fields
            listing = "\n".join(f"- {name}" for name in withheld_names(fields))
            template = (COPY_NOT_RECORDED if (missing or C.load()["missing_fields"]) == "not_recorded"
                        else COPY_PARTIAL)
            copy = template.format(fields=listing, **words)
    return f"{intro} {copy}"


# ---------------------------------------------------------------- builders
def build_prompt(case_id: str, condition: str, output: str, *, layout: str | None = None,
                 withheld: list[str] | None = None, missing: str | None = None) -> str:
    """The prompt for one case, condition and output.

    ``withheld`` overrides the fields of an ignorance pair (used by the pilot
    and the tests); ``missing`` overrides the config's ``missing_fields``.
    """
    layout = layout or C.load()["layout"]
    case = cases().loc[case_id]
    cond = CONDITIONS[condition]
    shown = model_profile(case, condition, withheld=withheld, missing=missing)
    if cond.about == "self":
        return first_order_prompt(output, shown)
    question = agent_question(output, cond.framing, shown)
    _, fmt = split_first_order(output, shown)
    paragraph = agent_paragraph(case, condition, withheld=withheld, missing=missing)
    if layout == "plan":
        return f"{PROFILE_HEADER}{shown}\n\n{paragraph}\n\n{question}\n\n{fmt}\n"
    if layout == "repo":
        return f"{question}\n\n{fmt}\n\n{PROFILE_HEADER}{shown}\n\n{paragraph}\n"
    raise ValueError(f"unknown layout {layout!r}")


def profile_end_offset(prompt: str, case_id: str, condition: str,
                       withheld: list[str] | None = None) -> int:
    """Character offset just past the profile block (position P1)."""
    profile = model_profile(cases().loc[case_id], condition, withheld=withheld)
    start = prompt.index(PROFILE_HEADER + profile)
    return start + len(PROFILE_HEADER + profile)


def prompt_rows(case_ids: list[str], conditions: list[str], outputs: tuple[str, ...] = OUTPUTS,
                **kwargs: Any) -> list[PromptRow]:
    return [
        PromptRow(case_id, condition, output, build_prompt(case_id, condition, output, **kwargs))
        for condition in conditions for case_id in case_ids for output in outputs
    ]
