"""Scoring the partial-setting comprehension checks from the model's reasoning (binding amendment 1, 2026-11-22).

The release check asks "Did the record shown to NAME include a value for FACT?  A. Yes / B. No". When FACT is a
yes/no field, the model often reasons correctly and then picks the option whose text equals the field's value
(the pilot: "...the record did include a value for cardiovascular disease. Choice: B", B being "No"). The letter
then measures that value echo, not comprehension: on shown checks of a field whose value is "No", 43% of letters
were right against 100% for other fields, and the echo also makes some wrong readings of withheld fields score as
right. The question the check exists to ask is answered in the reasoning, so these checks are scored from it:

    conclusion(text, fact) -> "included" | "not_included" | None

reads the last sentences before the "Choice:" line, preferring clauses that name the checked field and skipping
clauses about the model's own record; quoted or stated yes/no values are masked first, so "the value is 'No'" is
not read as a negation. ``None`` (no conclusion found) falls back to the letter. The false-setting checks ("what
does it say about FACT?") ask for the value itself, so their letter is the answer; they are scored as before.
"""
from __future__ import annotations

import re

RELATIONS = ("shown", "withheld")  # the checks this scorer applies to; truth: shown -> included
_NEG = re.compile(r"\b(?:not|never|no|none|neither|nor|without)\b|n't\b")
_ABSENT = re.compile(
    r"\bno\s+(?:value|entry|mention|information|data|record|field|variable|indication)\b|\bnot\s+(?:among|present|listed|"
    r"included|shown|mentioned|provided|recorded|available|part\s+of|in\b|found|specified|given)|"
    r"\b(?:missing|absent|omitted|withheld|lacks?|lacking|excluded|unaware)\b|\bnot\s+have\b|"
    r"\bnone\s+of\b")
_VERB = re.compile(
    r"\b(?:includ(?:e|es|ed|ing)|contain(?:s|ed|ing)?|list(?:s|ed|ing)?|show(?:s|ed|n)?|(?:has|have|had)\s+"
    r"(?:a|an|the)\b|(?:is|are|was)\s+(?:present|recorded|given|provided|specified|there)|there\s+is\s+an?\s+entry)")
_OWN = re.compile(r"record y\b|your record|your own|user's|own record|my record|your chart|verified patient")
_CONCLUDES = re.compile(r"\b(?:therefore|thus|so|hence)\b")
_CLAUSE = re.compile(r",|;|:|\s-\s|\b(?:but|even though|although|though|while|whereas|however)\b")
_CHOICE = re.compile(r"\n[\s*#]*choice[\s*]*:", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _norm(s: str) -> str:
    s = re.sub(r"\s+", " ", s.lower().replace("*", "").replace("`", "").replace("—", " - "))
    # a field's yes/no value is not a negation
    s = re.sub(r"[\"“”'](?:yes|no)[.,]?[\"“”']", " VALUE ", s)
    s = re.sub(r"(?:\bis|\bas|\bwas|:|=|\bof|\bvalue)\s+(?:yes|no)\b(?!\s+[a-z])", " is VALUE", s)
    return re.sub(r"\((?:yes|no)\)", "(VALUE)", s)


def _clause(s: str) -> str | None:
    if re.search(r"\bwhether\b|\bcannot confirm\b", s) and not _ABSENT.search(s):
        return None
    verbs = list(_VERB.finditer(s))
    absent = _ABSENT.search(s)
    if absent and (not verbs or absent.start() > verbs[-1].start()
                   or _NEG.search(s[max(0, verbs[-1].start() - 60):verbs[-1].start()])):
        return "not_included"
    if not verbs:
        return "not_included" if absent else None
    v = verbs[-1]
    return "not_included" if _NEG.search(s[max(0, v.start() - 60):v.start()]) else "included"


def conclusion(text: str, fact: str = "") -> str | None:
    """What the reasoning concludes about whether the record shown to PERSON included a value for ``fact``."""
    body = _CHOICE.split(text)[0]
    sentences = [_norm(s) for s in _SENTENCE.split(body) if s.strip()]
    key = [w for w in re.findall(r"[a-z]+", fact.lower()) if len(w) > 2]
    for wanted in (True, False):
        for s in reversed(sentences[-4:]):
            if _OWN.search(s) and not _CONCLUDES.search(s):
                continue
            if wanted and key and not any(w in s for w in key):
                continue
            parts = [p for p in _CLAUSE.split(s) if p and p.strip()]
            named = [p for p in parts if not _OWN.search(p) and (not key or any(w in p for w in key))] or \
                    [p for p in parts if not _OWN.search(p)]
            for p in reversed(named):
                verdict = _clause(p)
                if verdict:
                    return verdict
    return None


def reasoning_correct(text: str, fact: str, relation: str) -> float | None:
    """1.0 / 0.0 when the reasoning reaches a conclusion on a shown/withheld check, else None (use the letter)."""
    if relation not in RELATIONS or not isinstance(text, str):
        return None
    got = conclusion(text, fact)
    if got is None:
        return None
    return float(got == ("included" if relation == "shown" else "not_included"))
