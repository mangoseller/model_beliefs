"""Conditions of the experiment, as data.

Every condition is one row of ``CONDITIONS``: whose belief is asked for, which
profile the model is shown, and which profile PERSON holds.  Prompts, fit
families, the analysis and the figures all read this table.

* ``A``  the model's own belief from the full profile.
* ``B``  the model's own belief from PERSON's copy.
* ``M``  PERSON's belief; the model is shown only PERSON's copy (nothing private to leak).
* ``C``  PERSON's belief; the model sees the full profile.  The condition whose leakage is measured.
* ``D``  PERSON's belief if PERSON had the full profile.

Two settings, each with its own B, C, M and D (A is shared):

* ``partial``  PERSON's copy lists some fields as ``Not recorded`` (``withheld`` in the config).
* ``false``    a colleague read PERSON the profile and got one Yes/No line wrong
  (``flip_field``).  M_F and D_F use the same "read to PERSON" framing as C_F.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from latent_beliefs.ckd.cases import PROFILE_FEATURES
from latent_beliefs.ckd.prompts import (
    FEATURE_KEYS,
    FEATURE_TEXT,
    FEATURE_UNITS,
    format_feature_value,
)
from second_order import config as C

NOT_RECORDED = "Not recorded"
SETTINGS = ("partial", "false")
ROLES = ("A", "B", "C", "M", "D")
SUFFIX = {"partial": "", "false": "_F"}


@dataclass(frozen=True)
class Condition:
    key: str
    role: str                  # "A", "B", "C", "M" or "D"
    setting: str | None        # "partial" or "false"; None for A
    model_profile: str         # "full" or "copy" (PERSON's copy)
    person_profile: str | None  # "full", "copy", or None (the model's own belief)

    @property
    def about(self) -> str:
        return "self" if self.person_profile is None else "person"


SHOWN = {"B": ("copy", None), "C": ("full", "copy"), "M": ("copy", "copy"), "D": ("full", "full")}
CONDITIONS = {"A": Condition("A", "A", None, "full", None)} | {
    role + SUFFIX[s]: Condition(role + SUFFIX[s], role, s, *SHOWN[role])
    for s in SETTINGS for role in ("B", "C", "M", "D")
}


def setting_conditions(setting: str) -> dict[str, str]:
    """Condition keys playing roles A, B, C, M, D in one setting."""
    return {role: "A" if role == "A" else role + SUFFIX[setting] for role in ROLES}


def fit_family(condition: str) -> str:
    """Conditions with identical instructions share one latent fit: "self" or "person"."""
    return CONDITIONS[condition].about


# ---------------------------------------------------------------- profiles
def field_label(feature: str) -> str:
    """Human-readable field name, e.g. ``Self-reported diabetes (diab)``."""
    prefix = FEATURE_TEXT[feature].split(" ({key})")[0]
    return f"{prefix} ({FEATURE_KEYS[feature]})"


def profile_line(case: pd.Series, feature: str, value: str | None = None) -> str:
    if value is None:
        value = format_feature_value(feature, case[feature])
        if feature in FEATURE_UNITS and value != "Missing":
            value = f"{value} {FEATURE_UNITS[feature]}"
    elif value == NOT_RECORDED and feature == "ridageyr":
        # "Age (age) is {value} years." would read "Not recorded years".
        return FEATURE_TEXT[feature].format(value=value, key=FEATURE_KEYS[feature]).replace(
            f"{NOT_RECORDED} years", NOT_RECORDED)
    return FEATURE_TEXT[feature].format(value=value, key=FEATURE_KEYS[feature])


def flipped_value(case: pd.Series, feature: str) -> str:
    """The opposite Yes/No value of a binary field; anything else is an error."""
    current = format_feature_value(feature, case[feature])
    if current not in ("Yes", "No"):
        raise ValueError(f"cannot flip {feature} = {current!r} for case {case.get('case_id')}")
    return "No" if current == "Yes" else "Yes"


def render_profile(case: pd.Series, withheld=(), *, flip: str | None = None) -> str:
    """The profile block; with no arguments it is identical to ``patient_profile``.

    ``withheld`` fields keep their line with the value ``Not recorded``; ``flip``
    names one binary field shown with the opposite value.
    """
    withheld = set(withheld)
    unknown = (withheld | ({flip} if flip else set())) - set(PROFILE_FEATURES)
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    lines = []
    for f in PROFILE_FEATURES:
        if f in withheld:
            lines.append(profile_line(case, f, NOT_RECORDED))
        elif f == flip:
            lines.append(profile_line(case, f, flipped_value(case, f)))
        else:
            lines.append(profile_line(case, f))
    return "\n".join(lines).strip()


def withheld_fields() -> list[str]:
    """Fields listed as Not recorded in PERSON's copy (partial setting), in profile order."""
    fields = set(C.load()["withheld"])
    return [f for f in PROFILE_FEATURES if f in fields]


def flip_field() -> str:
    """The field shown with the opposite value in the false setting."""
    return C.load()["flip_field"]


def copy_profile(case: pd.Series, setting: str) -> str:
    """PERSON's copy of the profile in a setting (what B and M show the model)."""
    if setting == "false":
        return render_profile(case, flip=flip_field())
    return render_profile(case, withheld_fields())


def model_profile(case: pd.Series, condition: str) -> str:
    """The profile block the model is shown in a condition."""
    cond = CONDITIONS[condition]
    return render_profile(case) if cond.model_profile == "full" else copy_profile(case, cond.setting)
