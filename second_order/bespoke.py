"""The bespoke dataset: short fictional second-order belief items with known Bayes answers.

Each item is a scenario (two bags of marbles, witnesses who are each right
three times out of four, a noisy test for a fictional condition), a target
proposition with a stated prior, and a list of observations, each with its
probability if the proposition is true and if it is false.  Every number is
stated in the scenario, so each prompt's Bayes-optimal answer is known:

    posterior odds = prior odds * prod_i p_true(o_i) / p_false(o_i)

Three evidence sets per item, as in the Methods write-up:

* ``full`` (E): every observation;
* ``partial`` (E_p): the observations PERSON saw (a subset, by id);
* ``false`` (E_f): what PERSON was told: E with some observations replaced.

Placebo items add observations with p_true = p_false, held out of E_p and
changed in E_f, so all three gaps l_A - l_B, l_D - l_M and l_C - l_M should be
near zero.  The items live in a local file (``bespoke.items`` in the config,
``SO_BESPOKE_ITEMS`` overrides); the schema is in ``docs/data.md``.

Conditions are those of ``conditions.CONDITIONS``.  Every prompt is the
scenario, the evidence shown to the model, (for prompts about PERSON) a
paragraph on what PERSON knows, the question, and the JSON probability
instruction of the first-order CKD prompt, verbatim, so the first-order strict
parser applies.  M and D share one template and differ only in the evidence
shown; in the false setting, M_F, C_F and D_F all frame PERSON's evidence as
reported to them.  An item may give verbatim ``prompts`` per condition and its
own ``comprehension`` questions instead.

    python -m second_order.bespoke [--items PATH] [--examples prompts.md]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from latent_beliefs.ckd.parse import normalize_probability
from latent_beliefs.ckd.prompts import probability_prompt
from second_order import config as C
from second_order.comprehension import Comprehension, make
from second_order.conditions import CONDITIONS
from second_order.prompts_second_order import PERSON

PREFIX = "bespoke__"
OUTPUTS = ("probability",)
EVIDENCE_SETS = ("full", "partial", "false")
EVIDENCE_HEADER = "Evidence:\n"
NO_EVIDENCE = "(none)"

# The answer format of the first-order numeric probability prompt, verbatim.
_P = probability_prompt("")
FORMAT = _P[_P.index("Provide your reasoning first."):_P.index("\n\nPatient profile:")]

SELF_QUESTION = "Estimate the probability that {proposition}."
AGENT_QUESTION = ("What probability would {A} assign to the possibility that {proposition}? "
                  "Give your best estimate of {poss} probability.")
AGENT_INTRO = "{A} {is} a careful reasoner with the same knowledge and judgement as you."
SAW_ALL = "{Subj} saw exactly the evidence listed above."
SAW_PART = ("{Subj} saw only the following evidence:\n{lines}\n"
            "{Subj} {has} no other information about the evidence shown to you.")
TOLD_ALL = ("{Subj} did not see the evidence {refl}; someone else reported it to {obj}. "
            "What {subj} was told matched the evidence listed above exactly.")
TOLD_WRONG = ("{Subj} did not see the evidence {refl}; someone else reported it to {obj} and got some "
              "of it wrong. {Subj} was told:\n{lines}\n"
              "{Subj} {believes} what {subj} was told and {has} not seen the evidence shown to you.")


@dataclass(frozen=True)
class Observation:
    id: str
    text: str
    p_true: float | None = None
    p_false: float | None = None

    @property
    def log_lr(self) -> float:
        return math.log(self.p_true) - math.log(self.p_false)


@dataclass(frozen=True)
class Item:
    id: str
    family: str
    scenario: str
    proposition: str
    evidence: tuple[Observation, ...]
    partial: tuple[str, ...]
    false: dict[str, Observation]
    prior: float | None = None
    placebo: bool = False
    bayes_given: dict[str, float] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    comprehension: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return PREFIX + self.id

    def observations(self, setting: str) -> list[Observation]:
        if setting == "full":
            return list(self.evidence)
        if setting == "partial":
            return [o for o in self.evidence if o.id in set(self.partial)]
        if setting == "false":
            return [self.false.get(o.id, o) for o in self.evidence]
        raise ValueError(f"unknown evidence setting {setting!r}")

    def bayes(self, setting: str) -> float:
        """Bayes-optimal probability of the proposition given one evidence set."""
        if setting in self.bayes_given:
            return float(self.bayes_given[setting])
        log_odds = math.log(self.prior) - math.log1p(-self.prior)
        log_odds += sum(o.log_lr for o in self.observations(setting))
        return 1 / (1 + math.exp(-log_odds))


# ---------------------------------------------------------------- loading
def _observation(raw: dict[str, Any], oid: str | None = None) -> Observation:
    return Observation(str(oid or raw["id"]), str(raw["text"]).strip(), raw.get("p_true"), raw.get("p_false"))


def _keys(raw: dict | None) -> dict:
    """YAML 1.1 reads a bare ``false`` key as a boolean; restore it (and ``true``)."""
    return {(str(k).lower() if isinstance(k, bool) else k): v for k, v in (raw or {}).items()}


def parse_item(raw: dict[str, Any]) -> Item:
    raw = _keys(raw)
    evidence = tuple(_observation(o) for o in raw["evidence"])
    return Item(
        id=str(raw["id"]), family=str(raw.get("family", "")), scenario=str(raw["scenario"]).strip(),
        proposition=str(raw["proposition"]).strip().rstrip("."), evidence=evidence,
        partial=tuple(str(i) for i in raw.get("partial", [])),
        false={str(k): _observation(v, k) for k, v in (raw.get("false") or raw.get("told") or {}).items()},
        prior=raw.get("prior"), placebo=bool(raw.get("placebo", False)),
        bayes_given={k: float(v) for k, v in _keys(raw.get("bayes")).items()},
        prompts={str(k): str(v) for k, v in (raw.get("prompts") or {}).items()},
        comprehension=dict(raw.get("comprehension") or {}),
    )


def validate(item: Item) -> None:
    """Errors that make an item unusable (raised); see ``checks`` for warnings."""
    ids = [o.id for o in item.evidence]
    problems = []
    if len(set(ids)) != len(ids):
        problems.append("duplicate observation ids")
    if not set(item.partial) <= set(ids):
        problems.append(f"partial ids not in evidence: {sorted(set(item.partial) - set(ids))}")
    if set(item.partial) == set(ids):
        problems.append("partial evidence equals the full evidence")
    if not set(item.false) <= set(ids):
        problems.append(f"false ids not in evidence: {sorted(set(item.false) - set(ids))}")
    if not item.false:
        problems.append("no false-belief replacements")
    for oid, o in item.false.items():
        if o.text == next(e.text for e in item.evidence if e.id == oid):
            problems.append(f"false replacement {oid} has the same text as the true observation")
    for setting in EVIDENCE_SETS:
        if setting in item.bayes_given:
            continue
        if item.prior is None or not 0 < item.prior < 1:
            problems.append("prior missing or outside (0, 1) and no bayes values given")
            break
        for o in item.observations(setting):
            if o.p_true is None or o.p_false is None or not (0 < o.p_true < 1 and 0 < o.p_false < 1):
                problems.append(f"observation {o.id} needs p_true and p_false in (0, 1)")
    if problems:
        raise ValueError(f"bespoke item {item.id}: " + "; ".join(sorted(set(problems))))


def checks(item: Item) -> list[str]:
    """Warnings: answers near 0 or 1 (where the logit clip binds), zero gaps, placebo gaps."""
    out = []
    p = {s: item.bayes(s) for s in EVIDENCE_SETS}
    for s, v in p.items():
        if not 0.05 <= v <= 0.95:
            out.append(f"{item.id}: Bayes answer for {s} evidence is {v:.3f}, outside [0.05, 0.95]")
    for s in ("partial", "false"):
        gap = logit(p["full"]) - logit(p[s])
        if item.placebo and abs(gap) > 1e-9:
            out.append(f"{item.id}: placebo item has a {s} gap of {gap:.3f} logits")
        if not item.placebo and abs(gap) < 1e-9:
            out.append(f"{item.id}: {s} evidence leaves the Bayes answer unchanged (zero gap)")
    for cond, prompt in item.prompts.items():
        if FORMAT not in prompt:
            out.append(f"{item.id}: verbatim prompt for {cond} lacks the JSON probability instruction")
    return out


@lru_cache(maxsize=None)
def load(path: str | None = None) -> tuple[Item, ...]:
    path = Path(path) if path else C.bespoke_items_path()
    if not path.exists():
        raise FileNotFoundError(f"bespoke item file not found: {path} (set bespoke.items or SO_BESPOKE_ITEMS)")
    raw = yaml.safe_load(path.read_text())
    entries = raw["items"] if isinstance(raw, dict) else raw
    items = tuple(parse_item(r) for r in entries)
    if len({i.id for i in items}) != len(items):
        raise ValueError("bespoke item ids must be unique")
    for item in items:
        validate(item)
    return items


@lru_cache(maxsize=None)
def items_by_case(path: str | None = None) -> dict[str, Item]:
    return {i.case_id: i for i in load(path)}


def case_ids(path: str | None = None) -> list[str]:
    return sorted(items_by_case(path))


def logit(p: float) -> float:
    return math.log(p) - math.log1p(-p)


def table(path: str | None = None) -> pd.DataFrame:
    """One row per item: family, placebo flag, Bayes answers and Bayes gaps (logits)."""
    rows = []
    for item in load(path):
        p = {s: item.bayes(s) for s in EVIDENCE_SETS}
        rows.append({"case_id": item.case_id, "item_id": item.id, "family": item.family,
                     "placebo": item.placebo, **{f"bayes_{s}": v for s, v in p.items()},
                     "bayes_gap_partial": logit(p["full"]) - logit(p["partial"]),
                     "bayes_gap_false": logit(p["full"]) - logit(p["false"]),
                     "n_evidence": len(item.evidence), "n_partial": len(item.partial),
                     "n_false": len(item.false)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- parsing
QUOTED_KEY = re.compile(r'"probability"\s*:\s*"?([0-9]*\.?[0-9]+)\s*(%?)"?', flags=re.I)


def parse_response(output: str, text: str | None) -> tuple[float | None, str | None]:
    """The answer from the JSON object only (the last one with a ``probability`` key).

    The first-order parser falls back to any number within 40 characters after
    the word "probability"; bespoke propositions contain numbers ("Bag 1"), so
    that fallback would read a missing answer as 1.  Only the JSON object, or
    a quoted ``"probability":`` key, counts here.
    """
    if output not in OUTPUTS or not text:
        return None, None
    for candidate in reversed(re.findall(r"\{[^{}]*\}", text, flags=re.DOTALL)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "probability" in parsed:
            p = normalize_probability(parsed["probability"])
            if p is not None:
                return p, "json"
    for number, percent in reversed(QUOTED_KEY.findall(text)):
        p = normalize_probability(number + percent)
        if p is not None:
            return p, "quoted_key"
    return None, None


# ---------------------------------------------------------------- prompts
def evidence_lines(observations: list[Observation]) -> str:
    return "\n".join(f"- {o.text}" for o in observations) if observations else f"- {NO_EVIDENCE}"


def shown_setting(condition: str) -> str:
    """The evidence set the model is shown in a condition."""
    cond = CONDITIONS[condition]
    return "full" if cond.model_profile == "full" else cond.setting


def agent_paragraph(item: Item, condition: str) -> str:
    cond = CONDITIONS[condition]
    told = cond.setting == "false"
    if cond.role in ("D", "M"):
        text = (TOLD_ALL if told else SAW_ALL).format(**PERSON)
    else:
        lines = evidence_lines(item.observations(cond.setting))
        text = (TOLD_WRONG if told else SAW_PART).format(lines=lines, **PERSON)
    return f"{AGENT_INTRO.format(**PERSON)} {text}"


def build_prompt(case_id: str, condition: str, output: str = "probability", *,
                 path: str | None = None) -> str:
    if output not in OUTPUTS:
        raise ValueError(f"the bespoke dataset has only the numeric probability output, not {output!r}")
    cond = CONDITIONS[condition]
    item = items_by_case(path)[case_id]
    if condition in item.prompts:
        return item.prompts[condition]
    head = f"{item.scenario}\n\n{EVIDENCE_HEADER}{evidence_lines(item.observations(shown_setting(condition)))}"
    if cond.about == "self":
        question = SELF_QUESTION.format(proposition=item.proposition)
        return f"{head}\n\n{question}\n\n{FORMAT}\n"
    question = AGENT_QUESTION.format(proposition=item.proposition, **PERSON)
    return f"{head}\n\n{agent_paragraph(item, condition)}\n\n{question}\n\n{FORMAT}\n"


def bayes_answer(case_id: str, condition: str, path: str | None = None) -> float:
    """The Bayes-optimal answer to a prompt: Bayes given the believer's evidence.

    For own-belief conditions the believer is the model (the evidence shown);
    for conditions about PERSON it is PERSON (E in D and D_F, E_p in M and C,
    E_f in M_F and C_F), so a perspective-taker leaks nothing.
    """
    item = items_by_case(path)[case_id]
    cond = CONDITIONS[condition]
    if cond.about == "self" or cond.role == "D":
        return item.bayes(shown_setting(condition))
    return item.bayes(cond.setting)


def comprehension(case_id: str, condition: str, path: str | None = None) -> Comprehension:
    """Which evidence did PERSON have?  Options: PERSON's, the full and the other set."""
    cond = CONDITIONS[condition]
    if cond.role != "C":
        raise ValueError(f"comprehension questions follow C conditions, not {condition}")
    item = items_by_case(path)[case_id]
    if condition in item.comprehension:
        q = item.comprehension[condition]
        return Comprehension(str(q["stem"]), tuple(str(o) for o in q["options"]), int(q["answer"]))
    setting = cond.setting
    listing = lambda obs: "; ".join(o.text for o in obs) if obs else "No evidence"  # noqa: E731
    verb = "was told" if setting == "false" else "saw"
    stem = f"Which of the following is exactly the evidence {PERSON['A']} {verb}?"
    other = "false" if setting == "partial" else "partial"
    distractors = [listing(item.observations("full")), listing(item.observations(other))]
    if setting == "partial":
        held = [o for o in item.evidence if o.id not in set(item.partial)]
        distractors.append(listing(held))
    return make(stem, listing(item.observations(setting)), distractors, f"{case_id}|{condition}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--items", default=None, help="item file (default: config bespoke.items)")
    parser.add_argument("--examples", default=None, help="write rendered prompts for every condition here")
    parser.add_argument("--conditions", nargs="+", default=None)
    args = parser.parse_args()
    t = table(args.items)
    out = C.RESULTS / "bespoke"
    out.mkdir(parents=True, exist_ok=True)
    t.to_csv(out / "items.csv", index=False)
    print(t.round(3).to_string(index=False))
    warnings = [w for item in load(args.items) for w in checks(item)]
    print("\n".join(["", f"{len(warnings)} warnings"] + warnings))
    if args.examples:
        conditions = args.conditions or list(C.load()["experiment"]["conditions"])
        lines = ["# Bespoke dataset: example prompts\n\n",
                 "Generated by `python -m second_order.bespoke --examples`.  Bayes answers are the "
                 "Bayes-optimal probability given the believer's evidence.\n"]
        for item in load(args.items):
            for cond in conditions:
                prompt = build_prompt(item.case_id, cond, path=args.items)
                lines.append(f"\n## {item.id} {cond} (Bayes answer "
                             f"{bayes_answer(item.case_id, cond, args.items):.3f})\n\n```\n{prompt}```\n")
                if CONDITIONS[cond].role == "C":
                    q = comprehension(item.case_id, cond, args.items)
                    lines.append(f"\nComprehension follow-up (answer {q.answer_letter}):\n\n```\n{q.question}```\n")
        Path(args.examples).write_text("".join(lines))
        print(f"wrote {args.examples}")


if __name__ == "__main__":
    main()
