"""Text-level simulator: the ``synthetic`` backend, with known parameters (end-to-end checks).

It writes response *text* for every prompt of the two datasets, so a synthetic run
exercises the whole pipeline (cache, parsing, matrices, latent fits, beliefs,
estimators) and must recover the planted parameters of the Methods model:

    own belief (logit):  o(E_model)                  the evidence shown as the model's
    about PERSON:        a + phi [o(E_p) + lambda_s (o(E_model) - o(E_p))]

where E_p is PERSON's evidence and E_model the model's own (E_model = E_p when the
prompt shows only PERSON's), and lambda_s is the setting's leakage.  For CKD, o(.) is a
fixed logistic score of the record's fields (a missing field at its cohort mean); for
the bespoke items it is the row's Bayes answer (``normative_logodds``, and
``full_leak_p`` for the model's evidence in C).

Every call draws its belief once, ``call_noise`` logits around the value above; CKD
numbers are stated from 0 to 100, decisions follow a logistic around each question's
cutoff, and the Likert and quintile answers an ordered logit, answered by letter as
the prompt asks.  Checks and role probes are answered correctly with probability
``comprehension_accuracy``, as are the bespoke follow-ups.  A share
``parse_failure_rate`` of responses has no answer, and ``think_tags`` wraps the
reasoning in ``<think>`` tags.  Settings: ``models.synthetic.truth`` in the config.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import numpy as np

from second_order import config as C
from second_order import datasets as DS
from second_order.conditions import CONDITIONS

DEFAULTS = {"lambda_partial": 0.5, "lambda_false": 0.3, "phi": 0.8, "intercept": 0.2, "call_noise": 0.35,
            "probability_precision": 40.0, "decision_slope": 3.0, "comprehension_accuracy": 0.9,
            "parse_failure_rate": 0.0, "think_tags": False}
# CKD own-belief score: logit = BASE + sum weight * (value - centre); a missing field takes MEANS.
WEIGHTS = {"age": (0.06, 50.0), "diab": (1.0, 0.0), "htn": (0.9, 0.0), "cvd": (0.7, 0.0), "bmi": (0.03, 28.0),
           "pa": (-0.3, 0.0)}
MEANS = {"age": 50.0, "diab": 0.15, "htn": 0.35, "cvd": 0.1, "bmi": 29.0, "pa": 0.6}
BASE = -2.0
FACT = re.compile(r"^.+ \((\w+)\) is (.+)\.$", flags=re.M)
ORDINAL_CUTS = np.array([-3.0, -1.8, -0.6, 0.6])


def truth(model: str) -> dict[str, Any]:
    return {**DEFAULTS, **(C.model_config(model).get("truth") or {})}


def rng_for(*parts: Any) -> np.random.Generator:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return np.random.default_rng(int(digest[:16], 16))


# ---------------------------------------------------------------- beliefs
def records(prompt: str) -> dict[str, dict[str, str]]:
    """CKD prompt -> {"model": fields, "person": fields} for the records it shows."""
    out = {}
    for block in prompt.split("\n\n"):
        head = block.split("\n", 1)[0]
        if head.startswith("Your record"):
            out["model"] = dict(FACT.findall(block))
        elif "has only the following record" in head:
            out["person"] = dict(FACT.findall(block))
    return out


def score(fields: dict[str, str]) -> float:
    total = BASE
    for key, (weight, centre) in WEIGHTS.items():
        raw = fields.get(key, "")
        if raw in ("Yes", "No"):
            value = float(raw == "Yes")
        else:
            try:
                value = float(raw.split()[0])
            except (IndexError, ValueError):  # not shown, or "Missing"
                value = MEANS[key]
        total += weight * (value - centre)
    return float(total)


def evidence_logits(dataset: str, meta: dict) -> tuple[float, float | None]:
    """(o of the model's own evidence, o of PERSON's evidence or None)."""
    if dataset == "bespoke":
        own = float(meta["normative_logodds"])
        if CONDITIONS[meta["condition"]].about == "self":
            return own, None
        return (float(DS.logit(float(meta["full_leak_p"]))) if meta["full_leak_p"] else own), own
    shown = records(meta["prompt"])
    person = score(shown["person"]) if "person" in shown else None
    return (score(shown["model"]) if "model" in shown else person), person


def believed_logit(model: str, dataset: str, meta: dict) -> float:
    """The noiseless belief a prompt's answer reports, under the Methods model."""
    t = truth(model)
    own, person = evidence_logits(dataset, meta)
    if person is None:
        return own
    setting = CONDITIONS[meta["condition"]].setting
    lam = float(t[f"lambda_{setting}"]) if setting else 0.0
    return float(t["intercept"]) + float(t["phi"]) * (person + lam * (own - person))


# ---------------------------------------------------------------- answers
def choice_value(question: str, z: float, t: dict[str, Any], rng: np.random.Generator) -> float:
    """The meaning of the simulated choice given the call's belief z (logit)."""
    from second_order.responses import cutoff

    if question in ("likert", "quintile"):
        return float(1 + np.sum(1.5 * z + rng.logistic() > 1.5 * ORDINAL_CUTS))
    c = cutoff(question) or 0.1
    p_act = 1 / (1 + np.exp(-float(t["decision_slope"]) * (z - np.log(c / (1 - c)))))
    return float(rng.random() < p_act)


def letter_for(question: str, prompt: str, value: float) -> str:
    for letter, text in DS.options(prompt).items():
        if DS.option_value(question, text) == value:
            return letter
    raise ValueError(f"no option of {question} means {value}")


def wrong_statement(answer: str) -> str:
    """A keyed comprehension answer with its first value changed."""
    return re.sub(r"(:\s*(?:says\s+)?\"?)([A-Za-z]+)", r"\1other", answer, count=1)


def respond(model: str, item, seed: int) -> str:
    from second_order.run_generation import FOLLOWUP

    t = truth(model)
    rng = rng_for(seed, item.key)
    right = rng.random() < float(t["comprehension_accuracy"])
    if item.output == FOLLOWUP:
        answer = DS.row(item.dataset, item.case_id, item.condition, "probability")["comprehension_a"]
        return answer if right else wrong_statement(answer)
    meta = DS.row(item.dataset, item.case_id, item.condition, item.output, item.replicate)
    question = item.output.split("/")[0]
    if question in ("check", "role_check"):
        letters = list(DS.options(item.prompt))
        return meta["check_answer"] if right else next(x for x in letters if x != meta["check_answer"])
    call = rng_for(seed, model, item.dataset, item.case_id, item.condition, item.replicate, item.sample)
    z = believed_logit(model, item.dataset, meta) + float(t["call_noise"]) * call.normal()
    if rng.random() < float(t["parse_failure_rate"]):
        return "I cannot give a single answer."
    if question == "probability":
        p = 1 / (1 + np.exp(-z))
        k = float(t["probability_precision"])
        report = float(rng.beta(p * k, (1 - p) * k))
        if item.dataset == "bespoke":
            reasoning = "Weighing the evidence step by step."
            if t["think_tags"]:
                reasoning = f"<think>\n{reasoning}\n</think>\n\n{reasoning}"
            return f'{reasoning}\n{{"probability": {np.clip(report, 0.001, 0.999):.3f}}}'
        return str(int(np.clip(round(100 * report), 1, 99)))
    letter = letter_for(question, item.prompt, choice_value(question, z, t, rng))
    return f"{letter}. {DS.options(item.prompt)[letter]}" if rng.random() < 0.2 else letter


def generate(model: str, items: list, seed: int) -> list[dict[str, Any]]:
    out = []
    for item in items:
        text = respond(model, item, seed)
        out.append({"text": text, "ok": True, "error": None, "stop_reason": "end",
                    "usage": {"input_tokens": len(item.prompt.split()), "output_tokens": len(text.split())},
                    "seed": seed})
    return out
