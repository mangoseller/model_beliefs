"""Conditions of the second-order experiment, as data.

Every condition is one row of ``CONDITIONS``.  A condition says whose belief
is asked for, which profile the model is shown, and which profile the agent
(John or the user) holds.  Prompts, fit groups, analysis sets and figures all
read this table; no other module hard-codes a condition list.

Roles (the letters of the Methods write-up):

* ``A``  the model's own belief from the full profile.
* ``B``  the model's own belief from the agent's copy.
* ``C``  the agent's belief; the model sees the full profile, the agent holds
  a different copy.  The condition whose leakage is measured.
* ``M``  matched control: the agent's belief when the model is shown only
  the agent's copy (nothing private to leak).  Zero-leakage anchor for C.
* ``D``  the agent's belief when the agent also has the full profile.  The
  false-belief pair has its own D (``D_F``): the agent's evidence is framed as
  reported to them, as in C_F and M_F, so M_F and D_F share one template.

A *pair* names how the agent's copy differs from the full profile:

* ``S4``, ``S1``, ``H``  ignorance: fields withheld (``withheld_sets`` in the config).
* ``F``  false belief: one field flipped (``flip_field`` in the config).

Research questions served: RQ1a/b use pair S4; RQ1c uses H against F; RQ1d
uses the user framing; RQ2's ladder uses S4 -> S1 -> H -> D; RQ3 uses S4 and F.
The curse-of-knowledge experiment uses two *settings*: partial evidence (pair
S4) and false belief (pair F); ``SETTINGS`` maps them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

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
PAIRS = ("S4", "S1", "H", "F")
SETTINGS = {"partial": "S4", "false": "F"}       # curse-of-knowledge setting -> pair
SETTING_OF = {pair: setting for setting, pair in SETTINGS.items()}


@dataclass(frozen=True)
class Condition:
    key: str
    role: str                 # "A", "B", "C", "M" or "D"
    framing: str              # "self", "john" or "user"
    pair: str | None          # "S4", "S1", "H", "F"; None for A and D
    model_profile: str        # "full" or "copy" (the agent's copy)
    agent_profile: str | None  # "full", "copy" or None (own-belief conditions)

    @property
    def about(self) -> str:
        return "self" if self.framing == "self" else "agent"


def _rows() -> dict[str, Condition]:
    rows = [
        Condition("A", "A", "self", None, "full", None),
        Condition("D_john", "D", "john", None, "full", "full"),
        Condition("D_user", "D", "user", None, "full", "full"),
        # Pair S4 keeps the original names B and C_john / C_user.
        Condition("B", "B", "self", "S4", "copy", None),
        Condition("C_john", "C", "john", "S4", "full", "copy"),
        Condition("C_user", "C", "user", "S4", "full", "copy"),
        Condition("M_S4", "M", "john", "S4", "copy", "copy"),
    ]
    for pair in ("S1", "H", "F"):
        rows.append(Condition(f"B_{pair}", "B", "self", pair, "copy", None))
        rows.append(Condition(f"C_{pair}", "C", "john", pair, "full", "copy"))
    rows.append(Condition("M_H", "M", "john", "H", "copy", "copy"))
    rows.append(Condition("M_F", "M", "john", "F", "copy", "copy"))
    rows.append(Condition("D_F", "D", "john", "F", "full", "full"))
    return {c.key: c for c in rows}


CONDITIONS = _rows()


def pair_conditions(framing: str, pair: str, *, shared_d: bool = False) -> dict[str, str | None]:
    """Condition keys playing roles A, B, C, M, D for one framing and pair (M may be None).

    D is the pair's own D where one exists (``D_F``), else the shared D;
    ``shared_d`` forces the shared one (runs made before D_F existed).
    """
    def find(role: str, fr: str, p: str | None) -> str | None:
        for c in CONDITIONS.values():
            if c.role == role and c.framing == fr and c.pair == p:
                return c.key
        return None

    d = None if shared_d else find("D", framing, pair)
    return {"A": "A", "B": find("B", "self", pair), "C": find("C", framing, pair),
            "M": find("M", framing, pair), "D": d or find("D", framing, None)}


def framing_pairs() -> list[tuple[str, str]]:
    """Every (framing, pair) that has a C condition."""
    return [(f, p) for f in ("john", "user") for p in PAIRS if pair_conditions(f, p)["C"]]


def fit_family(condition: str) -> str:
    """Conditions with identical instructions share one latent fit."""
    return CONDITIONS[condition].framing  # "self" (A, B*), "john" (C*, M*, D), "user"


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


def render_profile(case: pd.Series, withheld: Iterable[str] = (), *, flip: str | None = None,
                   missing: str = "removed") -> str:
    """The profile block.  With no arguments it is identical to ``patient_profile``.

    ``withheld`` fields are dropped (``missing="removed"``) or kept in place
    with the value ``Not recorded`` (``missing="not_recorded"``).  ``flip``
    names one binary field shown with the opposite value.
    """
    withheld = set(withheld)
    unknown = (withheld | ({flip} if flip else set())) - set(PROFILE_FEATURES)
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    if missing not in ("removed", "not_recorded"):
        raise ValueError(f"unknown missing-field rendering {missing!r}")
    lines = []
    for f in PROFILE_FEATURES:
        if f in withheld:
            if missing == "not_recorded":
                lines.append(profile_line(case, f, NOT_RECORDED))
        elif f == flip:
            lines.append(profile_line(case, f, flipped_value(case, f)))
        else:
            lines.append(profile_line(case, f))
    return "\n".join(lines).strip()


def withheld_fields(set_name: str | None = None) -> list[str]:
    cfg = C.load()
    name = set_name or cfg["withheld_set"]
    fields = list(cfg["withheld_sets"][name])
    # Keep profile order so renders are deterministic.
    return [f for f in PROFILE_FEATURES if f in fields]


def withheld_names(fields: Sequence[str]) -> list[str]:
    return [field_label(f) for f in fields]


def pair_spec(pair: str) -> tuple[list[str], str | None]:
    """(withheld fields, flipped field) that define the agent's copy for a pair."""
    cfg = C.load()
    if pair == "F":
        return [], cfg["flip_field"]
    if pair == "S4":  # the core pair: whichever set the pilot chose
        return withheld_fields(cfg["withheld_set"]), None
    return withheld_fields(pair), None


def copy_profile(case: pd.Series, pair: str, *, withheld: Sequence[str] | None = None,
                 missing: str | None = None) -> str:
    """The agent's copy of the profile for a pair (what B and M show the model)."""
    fields, flip = pair_spec(pair)
    if withheld is not None and flip is None:
        fields = list(withheld)
    return render_profile(case, fields, flip=flip, missing=missing or C.load()["missing_fields"])


def model_profile(case: pd.Series, condition: str, **kwargs) -> str:
    """The profile block the model is shown in a condition."""
    cond = CONDITIONS[condition]
    return render_profile(case) if cond.model_profile == "full" else copy_profile(case, cond.pair, **kwargs)
