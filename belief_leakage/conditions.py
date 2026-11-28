"""Condition names of the CKD and bespoke experiments.

The CKD release's two-chart cases M2 and D2, which share C's layout, are the
experiment's M and D; its one-chart M and D are the controls M_single and D_single.
False-record conditions carry the suffix _F.
"""

from __future__ import annotations

from dataclasses import dataclass

SETTINGS = ("partial", "false")


@dataclass(frozen=True)
class Condition:
    key: str
    role: str
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
    Condition("A", "A", None), Condition("D", "D", None), Condition("D_single", "D_single", None),
    Condition("A_F", "A", "false"),
    *[Condition(role + _f(s), role, s) for s in SETTINGS
      for role in ("B", "C", "M", "M_single", "C_swap", "probe", "probe_swap")],
    Condition("D_F", "D", "false"), Condition("C_ctrl_F", "C_ctrl", "false"),
]}

# (dataset, dataset setting, dataset case) -> condition key
KEYS = {
    ("ckd", "shared", "A"): "A",
    ("ckd", "shared", "D2"): "D",
    ("ckd", "shared", "D"): "D_single",
    **{("ckd", s, case): role + _f(s) for s in SETTINGS
       for case, role in (("B", "B"), ("C", "C"), ("M", "M_single"), ("M2", "M"),
                          ("C_swap", "C_swap"), ("probe", "probe"), ("probe_swap", "probe_swap"))},
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
    """Condition keys playing roles A, B, C, M, D (including the one-chart controls for CKD) in one setting."""
    f = _f(setting)
    roles = {"A": "A", "B": "B" + f, "C": "C" + f, "M": "M" + f}
    if dataset == "ckd":
        return roles | {"A": "A" + f, "D": "D" + f, "M_single": "M_single" + f, "D_single": "D_single"}
    return roles | {"D": "D" + f}


def references(dataset: str) -> dict[str, tuple[str, str]]:
    """No-leakage references for C: (M role, D role) per reference name."""
    if dataset == "ckd":
        return {"two_record": ("M", "D"), "single_record": ("M_single", "D_single")}
    return {"single_record": ("M", "D")}


def primary_reference(dataset: str) -> str:
    return "two_record" if dataset == "ckd" else "single_record"


def fit_family(condition: str) -> str:
    """Conditions with identical instructions share one latent fit: "self" or "person"."""
    return CONDITIONS[condition].about
