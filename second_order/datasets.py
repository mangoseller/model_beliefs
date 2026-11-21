"""The two datasets of the curse-of-knowledge experiment behind one interface.

* ``ckd``: the 500 CKD cases and 16 outputs (``prompts_second_order``); latent
  and direct beliefs.  One sample per item; the own-belief conditions get a
  second run (``experiment.replicates``).
* ``bespoke``: the fictional items of ``bespoke`` with the numeric probability
  only; direct beliefs.  ``bespoke.samples`` samples per item, condition and run.

Case ids are disjoint (bespoke ids start with ``bespoke__``), so one cache per
model holds both datasets.
"""

from __future__ import annotations

from latent_beliefs.ckd.parse import parse_response as parse_ckd
from second_order import bespoke as BS
from second_order import config as C
from second_order import prompts_second_order as CKD
from second_order.comprehension import Comprehension
from second_order.conditions import CONDITIONS


def dataset_of(case_id: str) -> str:
    return "bespoke" if case_id.startswith(BS.PREFIX) else "ckd"


def outputs(dataset: str) -> tuple[str, ...]:
    return BS.OUTPUTS if dataset == "bespoke" else CKD.OUTPUTS


def comprehension_outputs(dataset: str) -> tuple[str, ...]:
    """Outputs whose answers in C conditions get a comprehension follow-up."""
    if dataset == "bespoke":
        return BS.OUTPUTS
    return tuple(C.load()["experiment"]["comprehension"]["outputs"])


def samples(dataset: str) -> int:
    return int(C.load()["bespoke"]["samples"]) if dataset == "bespoke" else 1


def case_ids(dataset: str, n: int | str = "all") -> list[str]:
    if dataset == "bespoke":
        found = BS.case_ids()
        return found if n == "all" else found[: int(n)]
    from second_order.run_generation import select_cases  # run_generation imports this module

    return select_cases(n)


def parse(dataset: str, output: str, text: str | None) -> tuple[float | None, str | None]:
    """Strict parse: the first-order parser for CKD; JSON only for bespoke (``bespoke.parse_response``)."""
    if dataset == "bespoke":
        return BS.parse_response(output, text)
    return parse_ckd(output, text) if text else (None, None)


def prompt(dataset: str, model: str, case_id: str, condition: str, output: str) -> str:
    if dataset == "bespoke":
        return BS.build_prompt(case_id, condition, output)
    return CKD.prompt_for(model, case_id, condition, output)


def comprehension(dataset: str, case_id: str, condition: str) -> Comprehension:
    return (BS.comprehension if dataset == "bespoke" else CKD.comprehension)(case_id, condition)


def conditions_for(dataset: str, conditions: list[str]) -> list[str]:
    """The bespoke dataset has the partial (S4) and false-belief (F) settings only, for John."""
    if dataset != "bespoke":
        return list(conditions)
    return [c for c in conditions if CONDITIONS[c].pair in (None, "S4", "F") and CONDITIONS[c].framing != "user"]
