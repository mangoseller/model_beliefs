"""Profile ablation and condition definitions.

The partial profile renders exactly the first-order profile lines
(``latent_beliefs.ckd.prompts.patient_profile``) with the withheld fields'
lines removed entirely.  Nothing is printed as "unknown".
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


@dataclass(frozen=True)
class Condition:
    key: str          # A, B, C_john, D_john, C_user, D_user, E_self, E_weak, E_strong
    about: str        # "self" or "agent" or "ai"
    model_evidence: str   # "full" or "partial"
    agent_evidence: str | None  # "full", "partial" or None
    framing: str      # "self", "john", "user" or "ai"


CONDITIONS = {
    "A": Condition("A", "self", "full", None, "self"),
    "B": Condition("B", "self", "partial", None, "self"),
    "C_john": Condition("C_john", "agent", "full", "partial", "john"),
    "D_john": Condition("D_john", "agent", "full", "full", "john"),
    "C_user": Condition("C_user", "agent", "full", "partial", "user"),
    "D_user": Condition("D_user", "agent", "full", "full", "user"),
    "E_self": Condition("E_self", "ai", "full", "full", "ai"),
    "E_weak": Condition("E_weak", "ai", "full", "full", "ai"),
    "E_strong": Condition("E_strong", "ai", "full", "full", "ai"),
}
MAIN_CONDITIONS = ("A", "B", "C_john", "D_john", "C_user", "D_user")
E_CONDITIONS = ("E_self", "E_weak", "E_strong")


def field_label(feature: str) -> str:
    """Human-readable field name, e.g. ``Self-reported diabetes (diab)``."""
    prefix = FEATURE_TEXT[feature].split(" ({key})")[0]
    return f"{prefix} ({FEATURE_KEYS[feature]})"


def profile_line(case: pd.Series, feature: str) -> str:
    value = format_feature_value(feature, case[feature])
    if feature in FEATURE_UNITS and value != "Missing":
        value = f"{value} {FEATURE_UNITS[feature]}"
    return FEATURE_TEXT[feature].format(value=value, key=FEATURE_KEYS[feature])


def render_profile(case: pd.Series, withheld: Iterable[str] = ()) -> str:
    """Profile with the withheld fields' lines removed (identical to
    ``patient_profile`` when nothing is withheld)."""
    withheld = set(withheld)
    unknown = withheld - set(PROFILE_FEATURES)
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    lines = [profile_line(case, f) for f in PROFILE_FEATURES if f not in withheld]
    return "\n".join(lines).strip()


def withheld_fields(set_name: str | None = None) -> list[str]:
    cfg = C.load()
    name = set_name or cfg["withheld_set"]
    fields = list(cfg["withheld_sets"][name])
    # Keep profile order so renders are deterministic.
    return [f for f in PROFILE_FEATURES if f in fields]


def withheld_names(fields: Sequence[str]) -> list[str]:
    return [field_label(f) for f in fields]
