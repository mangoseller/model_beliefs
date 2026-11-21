"""Text-level simulator: the ``synthetic`` backend, with known parameters (end-to-end checks).

It writes response *text* for every item, so a synthetic run exercises the
whole pipeline (cache, parsing, matrices, latent fits, beliefs, estimators)
and must recover the planted parameters of the Methods model:

    own belief (logit):     o(E)        the model's own belief from evidence E
    about PERSON:           l = a + phi * (.)
    D: a + phi o(E)     M: a + phi o(E_s)     C: a + phi [o(E_s) + lambda_s (o(E) - o(E_s))]

for each setting s (partial: E_s = E_p; false: E_f).  o(.) is the Bayes
posterior for bespoke items, and for CKD a fixed logistic score of the
profile fields the model is shown, with withheld fields at their cohort mean
(a rational reader's expectation) and the flipped field flipped.

Every call (one condition, case, run and sample) draws its belief once,
``call_noise`` logits around the value above, and all 16 CKD outputs of that
call are drawn from it: a Beta numeric report, logistic decisions around each
output's implied threshold, ordered-logit Likert and bin answers, matching the
latent model's measurement families.  Answers are written with the option text
the prompt offers, so the first-order strict parser reads them back.  A share
``parse_failure_rate`` of responses has no answer line, and ``think_tags``
wraps the reasoning in ``<think>`` tags, to exercise retention and stripping.
Comprehension follow-ups are answered correctly with probability
``comprehension_accuracy``.  Settings: ``models.synthetic.truth`` in the config.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

import numpy as np

from second_order import config as C
from second_order.conditions import CONDITIONS, flip_field, withheld_fields

DEFAULTS = {"lambda_partial": 0.5, "lambda_false": 0.3, "phi": 0.8, "intercept": 0.2, "call_noise": 0.35,
            "probability_precision": 40.0, "decision_slope": 3.0, "comprehension_accuracy": 0.9,
            "parse_failure_rate": 0.0, "think_tags": False}
# CKD own-belief score: logit = BASE + sum weight * (value - centre).
CKD_WEIGHTS = {"ridageyr": (0.06, 50.0), "diabetes_self_reported": (1.0, 0.0),
               "hypertension_self_reported": (0.9, 0.0), "cvd_any": (0.7, 0.0), "bmxbmi": (0.03, 28.0),
               "physical_activity_any": (-0.3, 0.0)}
CKD_BASE = -2.0
LIKERT_CUTS = np.array([-3.0, -1.8, -0.6, 0.6])
OPTION = re.compile(r"^([A-E])\. (.+)$", flags=re.M)


def truth(model: str) -> dict[str, Any]:
    return {**DEFAULTS, **(C.model_config(model).get("truth") or {})}


def rng_for(*parts: Any) -> np.random.Generator:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return np.random.default_rng(int(digest[:16], 16))


# ---------------------------------------------------------------- own beliefs
@lru_cache(maxsize=1)
def _ckd_means() -> dict[str, float]:
    from second_order.prompts_second_order import cases

    frame = cases()
    return {f: float(frame[f].astype(float).mean()) for f in CKD_WEIGHTS}


def own_logit(dataset: str, case_id: str, setting: str) -> float:
    """The simulated model's own belief from the evidence of one setting (full, partial, false)."""
    if dataset == "bespoke":
        from second_order.bespoke import items_by_case, logit

        return logit(items_by_case()[case_id].bayes(setting))
    from second_order.prompts_second_order import cases

    case = cases().loc[case_id]
    fields = withheld_fields() if setting == "partial" else []
    flip = flip_field() if setting == "false" else None
    means = _ckd_means()
    total = CKD_BASE
    for f, (weight, centre) in CKD_WEIGHTS.items():
        value = float(case[f]) if np.isfinite(float(case[f])) else means[f]
        if f in fields:
            value = means[f]
        if f == flip:
            value = 1.0 - value
        total += weight * (value - centre)
    return float(total)


def believed_logit(model: str, dataset: str, case_id: str, condition: str) -> float:
    """The noiseless belief a condition's answer reports, under the Methods model."""
    t = truth(model)
    cond = CONDITIONS[condition]
    setting = cond.setting
    if cond.about == "self":
        return own_logit(dataset, case_id, setting if cond.model_profile == "copy" else "full")
    full, own = own_logit(dataset, case_id, "full"), own_logit(dataset, case_id, setting)
    a, phi = float(t["intercept"]), float(t["phi"])
    if cond.role == "D":
        return a + phi * full
    if cond.role == "M":
        return a + phi * own
    lam = float(t[f"lambda_{setting}"])
    return a + phi * (own + lam * (full - own))


# ---------------------------------------------------------------- answers
def threshold(output: str) -> float:
    from second_order.prompts_second_order import specs

    spec = specs()[output]
    return float(spec.get("threshold", spec.get("implied_threshold", 0.1)))


def choice_value(output: str, z: float, t: dict[str, Any], rng: np.random.Generator) -> float:
    """Simulated parsed value of a choice output given the call's belief z (logit)."""
    if output == "likert":
        return float(1 + np.sum(1.5 * z + rng.logistic() > 1.5 * LIKERT_CUTS))
    if output == "probability_bin_quintile":
        from second_order.prompts_second_order import bin_boundaries

        cuts = np.log(np.array(bin_boundaries())) - np.log1p(-np.array(bin_boundaries()))
        return float(1 + np.sum(1.5 * z + rng.logistic() > 1.5 * cuts))
    tau = threshold(output)
    p_yes = 1 / (1 + np.exp(-float(t["decision_slope"]) * (z - np.log(tau / (1 - tau)))))
    return float(rng.random() < p_yes)


def option_for(output: str, prompt: str, value: float) -> str:
    """The option text whose strict parse is ``value``."""
    texts = _option_values(output, tuple(text for _, text in OPTION.findall(prompt)))
    if value not in texts:
        raise ValueError(f"no option of {output} parses to {value}")
    return texts[value]


@lru_cache(maxsize=None)
def _option_values(output: str, options: tuple[str, ...]) -> dict[float, str]:
    from latent_beliefs.ckd.parse import parse_response

    values = {}
    for text in options:
        values.setdefault(parse_response(output, f"x\nChoice: {text}")[0], text)
    return values


def respond(model: str, item, seed: int) -> str:
    from second_order.comprehension import is_followup
    from second_order import datasets as DS

    t = truth(model)
    rng = rng_for(seed, item.key)
    if is_followup(item.output):
        q = DS.comprehension(item.dataset, item.case_id, item.condition)
        right = rng.random() < float(t["comprehension_accuracy"])
        pick = q.answer if right else int(rng.choice([i for i in range(len(q.options)) if i != q.answer]))
        return f"Checking what was described.\nChoice: {chr(65 + pick)}"
    call = rng_for(seed, model, item.case_id, item.condition, item.replicate, item.sample)
    z = believed_logit(model, item.dataset, item.case_id, item.condition) + float(t["call_noise"]) * call.normal()
    reasoning = "Weighing the evidence step by step."
    if t["think_tags"]:
        reasoning = f"<think>\n{reasoning}\n</think>\n\n{reasoning}"
    if rng.random() < float(t["parse_failure_rate"]):
        return reasoning + "\nI cannot give a single answer."
    if item.output == "probability":
        p = 1 / (1 + np.exp(-z))
        k = float(t["probability_precision"])
        report = float(np.clip(rng.beta(p * k, (1 - p) * k), 0.001, 0.999))
        return f'{reasoning}\n{{"probability": {report:.3f}}}'
    value = choice_value(item.output, z, t, rng)
    return f"{reasoning}\nChoice: {option_for(item.output, item.prompt, value)}"


def generate(model: str, items: list, seed: int) -> list[dict[str, Any]]:
    out = []
    for item in items:
        text = respond(model, item, seed)
        out.append({"text": text, "ok": True, "error": None, "stop_reason": "end",
                    "usage": {"input_tokens": len(item.prompt.split()), "output_tokens": len(text.split())},
                    "seed": seed})
    return out
