"""Synthetic backend for the binding experiment: answers any arm's prompt from its own text.

Like ``synthetic.py`` (the Methods model, the same record score o(.) and answer formats),
but the records are read with ``binding.record_blocks`` (original and labelled headers)
and the leakage is planted per cell, so the analysis must recover a different lambda for
every arm and order:

    own belief:     o(E_model)
    about PERSON:   a + phi [o(E_p) + lambda_cell (o(E_model) - o(E_p))]

``lambda_cell`` comes from ``models.<synthetic>.truth.binding_lambda`` ("<arm>/<order>"
keys), falling back to ``DEFAULT_LAMBDA``.  Checks are answered from PERSON's block,
correctly with probability ``comprehension_accuracy``.
"""
from __future__ import annotations

import re

from belief_leakage import binding as B
from belief_leakage import datasets as DS
from belief_leakage import synthetic as S

# A pattern of the hypotheses: misbinding by order in the false setting, removed by labels; a
# partial-setting leak that labels reduce and explicit absence reduces further.
DEFAULT_LAMBDA = {
    "false_verified/person_first": 1.0, "false_verified/own_first": 0.0,
    "false_neutral/person_first": 0.6, "false_neutral/own_first": 0.0,
    "false_labelled": 0.05, "false_instructed": 0.0,
    "partial_original/person_first": 0.9, "partial_original/own_first": 0.8,
    "partial_labelled": 0.6, "partial_labelled_explicit": 0.3,
    "partial_instructed": 0.4, "partial_instructed_explicit": 0.2,
    "strong_original/person_first": 0.9, "strong_original/own_first": 0.8,
    "strong_labelled": 0.6, "strong_labelled_explicit": 0.3,
    "strong_instructed": 0.4, "strong_instructed_explicit": 0.2,
}
PARTIAL_CHECK = re.compile(r"Did the record shown to .+? include a value for (.+?)\?")
FALSE_CHECK = re.compile(r"In the evidence .+? was shown, what does it say about the patient's (.+?)\?")


def planted_lambda(truth: dict, arm: str, order: str) -> float:
    table = {**DEFAULT_LAMBDA, **(truth.get("binding_lambda") or {})}
    for key in (f"{arm}/{order}", arm):
        if key in table:
            return float(table[key])
    raise KeyError(f"no planted lambda for {arm}/{order}")


def believed_logit(truth: dict, item) -> float:
    blocks = B.record_blocks(item.prompt)
    own = next((b["fields"] for b in blocks if b["owner"] == "own"), None)
    person = next((b["fields"] for b in blocks if b["owner"] == "person"), None)
    if person is None:
        return S.score(own)
    o_p = S.score(person)
    o_m = S.score(own) if own is not None else o_p
    arm, order, role = B.decode_condition(item.condition, item.case_id)
    lam = planted_lambda(truth, arm, order) if arm and role == "C" else 0.0
    return float(truth["intercept"]) + float(truth["phi"]) * (o_p + lam * (o_m - o_p))


def check_letter(prompt: str) -> str:
    person = next(b["fields"] for b in B.record_blocks(prompt) if b["owner"] == "person")
    opts = DS.options(prompt)
    if m := PARTIAL_CHECK.search(prompt):
        want = "Yes" if B.NAME_CODE[m.group(1)] in person else "No"
        return next(k for k, v in opts.items() if v == want)
    m = FALSE_CHECK.search(prompt)
    value = person[B.NAME_CODE[m.group(1)]]
    return next(k for k, v in opts.items() if value == v or value.startswith(v))


def respond(model: str, item, seed: int) -> str:
    t = S.truth(model)
    rng = S.rng_for(seed, item.key)
    question = item.output.split("/")[0]
    if question == "check":
        right = check_letter(item.prompt)
        if rng.random() < float(t["comprehension_accuracy"]):
            return f"Reading the records.\nChoice: {right}"
        return f"Reading the records.\nChoice: {next(k for k in DS.options(item.prompt) if k != right)}"
    call = S.rng_for(seed, model, item.case_id, item.condition, item.replicate, item.sample)
    z = believed_logit(t, item) + float(t["call_noise"]) * call.normal()
    if rng.random() < float(t["parse_failure_rate"]):
        return "I cannot give a single answer."
    if question == "probability":
        return S.probability_answer(z, t, rng)
    letter = S.letter_for(question, item.prompt, S.choice_value(question, z, t, rng))
    return f"Weighing the evidence.\nChoice: {letter}"


def generate(model: str, items: list, seed: int) -> list[dict]:
    return [S.generation_record(item, respond(model, item, seed), seed, prompt_token_ids=[], token_ids=[])
            for item in items]
