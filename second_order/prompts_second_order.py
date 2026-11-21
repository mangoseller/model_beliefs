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
* **False belief** (``false_belief_framing: reported``): C_F, M_F and D_F say a
  colleague read the agent the profile; in C_F the colleague read one line
  wrongly.  M_F is D_F with the agent's (flipped) copy as the profile.

Layout ``plan`` puts the profile first; ``repo`` keeps the first-order order
with the agent paragraph after the profile.  Own-belief prompts (A, B*) follow
``self_layout``: ``matched`` builds them in the same layout as the prompts
about the agent, without the agent paragraph, so every condition ends with the
same question-and-format suffix; ``first_order`` is the released prompt.
``prompt_for`` resolves the layout per model (``config.self_layout``).

``comprehension`` gives the follow-up question asked after a C answer.
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
from second_order.comprehension import Comprehension, make
from second_order.conditions import (
    CONDITIONS,
    copy_profile,
    field_label,
    flipped_value,
    model_profile,
    pair_spec,
    profile_line,
    render_profile,
    withheld_fields,
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
        "trusts": "trusts", "obj": "him", "refl": "himself", "believes": "believes",
    },
    "user": {
        "A": "I", "subj": "I", "Subj": "I", "poss": "my", "Poss": "My",
        "for": "me", "is": "am", "has": "have", "follows": "follow", "Does": "Do",
        "trusts": "trust", "obj": "me", "refl": "myself", "believes": "believe",
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
# False belief, reported framing: M_F and D_F share REPORTED_FULL (the profile shown is
# the flipped copy in M_F and the true profile in D_F); C_F quotes the misread line.
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


def reported(condition: str, false_framing: str | None = None) -> bool:
    """Whether the agent's evidence is framed as reported (false-belief pair, reported framing)."""
    framing = false_framing or C.load().get("false_belief_framing", "copy")
    return CONDITIONS[condition].pair == "F" and framing == "reported"


def agent_paragraph(case: pd.Series, condition: str, *, withheld: list[str] | None = None,
                    missing: str | None = None, false_framing: str | None = None) -> str:
    """Who the agent is and what copy of the profile the agent holds."""
    cond = CONDITIONS[condition]
    words = dict(PERSON[cond.framing])
    intro = AGENT_INTRO.format(**words)
    told = reported(condition, false_framing)
    if cond.role in ("D", "M"):  # the agent holds exactly the profile shown
        copy = (REPORTED_FULL if told else COPY_FULL).format(**words)
    else:
        fields, flip = pair_spec(cond.pair)
        if flip is not None:
            line = profile_line(case, flip, flipped_value(case, flip))
            copy = (REPORTED_FLIPPED if told else COPY_FLIPPED).format(line=line, **words)
        else:
            fields = list(withheld) if withheld is not None else fields
            listing = "\n".join(f"- {name}" for name in withheld_names(fields))
            template = (COPY_NOT_RECORDED if (missing or C.load()["missing_fields"]) == "not_recorded"
                        else COPY_PARTIAL)
            copy = template.format(fields=listing, **words)
    return f"{intro} {copy}"


# ---------------------------------------------------------------- builders
def assemble(layout: str, shown: str, paragraph: str | None, question: str, fmt: str) -> str:
    """Put the pieces in layout order; an own-belief prompt has no agent paragraph."""
    if layout == "plan":
        middle = f"{paragraph}\n\n" if paragraph else ""
        return f"{PROFILE_HEADER}{shown}\n\n{middle}{question}\n\n{fmt}\n"
    if layout == "repo":
        tail = f"\n\n{paragraph}" if paragraph else ""
        return f"{question}\n\n{fmt}\n\n{PROFILE_HEADER}{shown}{tail}\n"
    raise ValueError(f"unknown layout {layout!r}")


def build_prompt(case_id: str, condition: str, output: str, *, layout: str | None = None,
                 withheld: list[str] | None = None, missing: str | None = None,
                 self_layout: str | None = None, false_framing: str | None = None) -> str:
    """The prompt for one case, condition and output.

    ``withheld`` overrides the fields of an ignorance pair (used by the pilot
    and the tests); ``missing`` overrides the config's ``missing_fields``;
    ``self_layout`` and ``false_framing`` override the config's values.
    """
    layout = layout or C.load()["layout"]
    case = cases().loc[case_id]
    cond = CONDITIONS[condition]
    shown = model_profile(case, condition, withheld=withheld, missing=missing)
    if cond.about == "self":
        if (self_layout or C.self_layout()) == "first_order":
            return first_order_prompt(output, shown)
        question, fmt = split_first_order(output, shown)
        return assemble(layout, shown, None, question, fmt)
    question = agent_question(output, cond.framing, shown)
    _, fmt = split_first_order(output, shown)
    paragraph = agent_paragraph(case, condition, withheld=withheld, missing=missing,
                                false_framing=false_framing)
    return assemble(layout, shown, paragraph, question, fmt)


def prompt_for(model: str, case_id: str, condition: str, output: str) -> str:
    """The prompt a model is sent (own-belief layout resolved per model)."""
    return build_prompt(case_id, condition, output, self_layout=C.self_layout(model))


# ---------------------------------------------------------------- comprehension
BINARY_FIELDS = ("hypertension_self_reported", "diabetes_self_reported", "cvd_any",
                 "physical_activity_any")


def comprehension(case_id: str, condition: str) -> Comprehension:
    """Follow-up question after a C answer: which fields the agent lacked, or which line was misread."""
    cond = CONDITIONS[condition]
    if cond.role != "C":
        raise ValueError(f"comprehension questions follow C conditions, not {condition}")
    words = PERSON[cond.framing]
    fields, flip = pair_spec(cond.pair)
    key = f"{case_id}|{condition}"
    if flip is not None:
        told = reported(condition)
        stem = (f"Which line of the patient's profile did the colleague read to {words['obj']} incorrectly?"
                if told else f"Which line of {words['poss']} copy of the profile differed from the profile shown?")
        none = ("None: everything the colleague read was correct" if told
                else f"None: {words['poss']} copy matched the profile shown")
        labels = [field_label(f) for f in BINARY_FIELDS if f != flip]
        return make(stem, field_label(flip), labels[:2] + [none], key)
    listing = lambda fs: "; ".join(withheld_names(fs))  # noqa: E731
    others = sorted({tuple(withheld_fields(n)) for n in C.load()["withheld_sets"]} - {tuple(fields)})
    stem = f"Which of the following lists exactly the fields that {words['A']} had no information about?"
    return make(stem, listing(fields), [listing(o) for o in others[:2]]
                + [f"None: {words['A']} had the full profile"], key)


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
