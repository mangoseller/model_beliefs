"""Conditions of the experiment, as data.

Each dataset row names a setting and a case; ``KEYS`` maps them to one condition
key, and ``CONDITIONS`` says whose belief the key asks for.  The analysis reads
the roles of a setting from ``setting_conditions``.

* ``A``  the model's own belief from the full evidence E.
* ``B``  the model's own belief from PERSON's evidence (E_p, or E_f in the false setting).
* ``M``  PERSON's belief; only PERSON's evidence is shown (nothing private to leak).
* ``D``  PERSON's belief when PERSON has E.
* ``C``  PERSON's belief; the model has E and PERSON has E_p or E_f.  Leakage is measured here.
* ``M2``, ``D2`` (CKD)  as M and D, but laid out like C: PERSON's record and the model's own,
  which here is the same evidence.  These are C's matched no-leakage references.

CKD also has role-swap diagnostics (``C_swap``: C with the two ownership headers
traded; ``probe`` and ``probe_swap``: which record holds a field) and standalone
comprehension checks (output ``check``) after C.  The bespoke dataset adds
``C_ctrl``: C_F's two-record layout where the friend read PERSON the true record,
so its no-leakage reference is D_F.

Keys in the false setting end in ``_F``.  In CKD, D and D2 are shared by both
settings; in the bespoke dataset the false setting has its own D_F.
"""

from __future__ import annotations

from dataclasses import dataclass

SETTINGS = ("partial", "false")
ROLES = ("A", "B", "C", "M", "D", "M2", "D2")


@dataclass(frozen=True)
class Condition:
    key: str
    role: str                 # A, B, C, M, D, M2, D2, C_swap, C_ctrl, probe or probe_swap
    setting: str | None       # partial, false, or None (shared)

    @property
    def about(self) -> str:
        return "self" if self.role in ("A", "B") else "person"

    @property
    def is_belief(self) -> bool:
        return not self.role.startswith("probe")


def _f(setting: str) -> str:
    return "_F" if setting == "false" else ""


CONDITIONS: dict[str, Condition] = {c.key: c for c in [
    Condition("A", "A", None), Condition("D", "D", None), Condition("D2", "D2", None),
    *[Condition(role + _f(s), role, s) for s in SETTINGS
      for role in ("B", "C", "M", "M2", "C_swap", "probe", "probe_swap")],
    Condition("D_F", "D", "false"), Condition("C_ctrl_F", "C_ctrl", "false"),
]}

# (dataset, dataset setting, dataset case) -> condition key
KEYS = {
    **{("ckd", "shared", case): case for case in ("A", "D", "D2")},
    **{("ckd", s, case): case + _f(s) for s in SETTINGS
       for case in ("B", "C", "M", "M2", "C_swap", "probe", "probe_swap")},
    **{("bespoke", s, case): case for s in ("partial", "placebo") for case in ("A", "B", "C", "M", "D")},
    **{("bespoke", "false", f"{case}_false"): f"{case}_F" for case in ("B", "C", "M", "D")},
    ("bespoke", "report_assignment_control", "C_report_control"): "C_ctrl_F",
}


def condition_key(dataset: str, setting: str, case: str) -> str:
    try:
        return KEYS[(dataset, setting, case)]
    except KeyError:
        raise ValueError(f"unknown {dataset} condition: setting {setting!r}, case {case!r}") from None


def setting_conditions(dataset: str, setting: str) -> dict[str, str]:
    """Condition keys playing roles A, B, C, M, D (and, for CKD, M2 and D2) in one setting."""
    f = _f(setting)
    roles = {"A": "A", "B": "B" + f, "C": "C" + f, "M": "M" + f}
    if dataset == "ckd":
        return roles | {"D": "D", "M2": "M2" + f, "D2": "D2"}
    return roles | {"D": "D" + f}


def references(dataset: str) -> dict[str, tuple[str, str]]:
    """No-leakage references for C: (M role, D role) per reference name; the first is the headline."""
    if dataset == "ckd":
        return {"two_record": ("M2", "D2"), "single_record": ("M", "D")}
    return {"single_record": ("M", "D")}


def fit_family(condition: str) -> str:
    """Conditions with identical instructions share one latent fit: "self" or "person"."""
    return CONDITIONS[condition].about
