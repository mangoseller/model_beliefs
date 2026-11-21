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

from second_order import config as C
from second_order.comprehension import Comprehension

DATASETS = ("ckd", "bespoke")


def dataset_of(case_id: str) -> str:
    from second_order.bespoke import PREFIX

    return "bespoke" if case_id.startswith(PREFIX) else "ckd"


def outputs(dataset: str) -> tuple[str, ...]:
    if dataset == "bespoke":
        from second_order.bespoke import OUTPUTS
    else:
        from second_order.prompts_second_order import OUTPUTS
    return tuple(OUTPUTS)


def comprehension_outputs(dataset: str) -> tuple[str, ...]:
    """Outputs whose answers in C conditions get a comprehension follow-up."""
    if dataset == "bespoke":
        return outputs("bespoke")
    return tuple(C.load()["experiment"]["comprehension"]["outputs"])


def samples(dataset: str) -> int:
    return int(C.load()["bespoke"]["samples"]) if dataset == "bespoke" else 1


def case_ids(dataset: str, n: int | str = "all") -> list[str]:
    if dataset == "bespoke":
        from second_order.bespoke import case_ids as ids

        found = ids()
        return found if n == "all" else found[: int(n)]
    from second_order.run_generation import select_cases

    return select_cases(n)


def prompt(dataset: str, model: str, case_id: str, condition: str, output: str) -> str:
    if dataset == "bespoke":
        from second_order.bespoke import build_prompt

        return build_prompt(case_id, condition, output)
    from second_order.prompts_second_order import prompt_for

    return prompt_for(model, case_id, condition, output)


def comprehension(dataset: str, case_id: str, condition: str) -> Comprehension:
    if dataset == "bespoke":
        from second_order.bespoke import comprehension as question
    else:
        from second_order.prompts_second_order import comprehension as question
    return question(case_id, condition)


def conditions_for(dataset: str, conditions: list[str]) -> list[str]:
    """The bespoke dataset has the partial (S4) and false-belief (F) settings only."""
    if dataset != "bespoke":
        return list(conditions)
    from second_order.conditions import CONDITIONS

    return [c for c in conditions if CONDITIONS[c].pair in (None, "S4", "F") and CONDITIONS[c].framing != "user"]
