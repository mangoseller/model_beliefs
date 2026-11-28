"""Which record a reply's reasoning cites: the values of the fields on which PERSON's record and the model's
differ (false arms: both values; withheld-field arms: the model's value, which PERSON's record lacks)."""
from __future__ import annotations

import re

from belief_leakage import binding

# How a reply names each yes/no field, and the wording around the name that says yes or no.
TERMS = {"diab": r"diabet",
         "htn": r"hypertensi|high blood pressure",
         "cvd": r"cardiovascular|heart disease|\bcvd\b",
         "pa": r"physical(?:ly)? activ|exercis"}
NEG_BEFORE = re.compile(r"(?:\bno\b|\bnot\b|without|absence of|denies|non-|negative for|lack of|n't|free of)"
                        r"[^.\n]{0,25}$", re.I)
VAL_AFTER = re.compile(r"^[^.\n]{0,45}?(?:\bis\b|:|\(|\bindicator\b|\bstatus\b)\W{0,6}\**\s*(yes|no)\b", re.I)
HAS_BEFORE = re.compile(r"(?:\bhas\b|\bwith\b|history of|reports?|presence of|diagnosed|known)\W*(?:a\s)?$",
                        re.I)
SMOKE = {"Every day": r"every ?day|daily smok", "Some days": r"some days|occasional",
         "Not at all": r"not at all|non-?smok|does not smoke|doesn't smoke|never smok|former"}
# A number as a reply writes it for each numeric field (group 1: the digits).
NUMBER = {"age": r"(\d+)(?:\.0)?\s*-?\s*(?:years?|yrs?|y/?o)\b",
          "bmi": r"(\d+(?:\.\d+)?)",
          "sodium": r"(\d+(?:\.\d+)?)"}
# Where the reasoning ends and the final answer begins.
ANSWER = re.compile(r"\n\s*(?:\{\s*\"probability\"|Choice:)")


def without_trailing_zeros(number: str) -> str:
    """"31.0" -> "31", "31.50" -> "31.5"; a number without a decimal point is left as it is."""
    return number.rstrip("0").rstrip(".") if "." in number else number


def value_pattern(code: str, value: str):
    """Regex for a numeric or categorical field value, or None when it cannot be matched reliably."""
    if code == "age":
        years = re.match(r"(\d+)", value).group(1)
        return re.compile(rf"(?:\b{years}(?:\.0)?\s*-?\s*(?:years?|yrs?|y/?o)\b"
                          rf"|\bage[^.\n]{{0,20}}\b{years}(?:\.0)?\b"
                          rf"|\baged? {years}\b)", re.I)
    if code == "smkstat":
        return re.compile(SMOKE[value], re.I) if value in SMOKE else None
    if code in ("bmi", "sodium"):
        number = re.match(r"([\d.]+)", value).group(1)
        return re.compile(rf"\b{re.escape(without_trailing_zeros(number))}", re.I)
    return None


def polarity(text: str, code: str) -> set[str]:
    """What the text says about a yes/no field: 'Yes', 'No', both or neither."""
    found = set()
    for mention in re.finditer(TERMS[code], text, re.I):
        after = text[mention.end(): mention.end() + 60]
        if stated := VAL_AFTER.match(after):        # "...diabetes: Yes"
            found.add(stated.group(1).capitalize())
            continue
        before = text[max(0, mention.start() - 40): mention.start()]
        if NEG_BEFORE.search(before):               # "no history of diabetes"
            found.add("No")
        # "has diabetes", "diabetes is present", "diabetic"
        elif HAS_BEFORE.search(before) or re.search(r"^\w*\s*(?:is|are)?\s*present", after, re.I) \
                or code == "diab" and re.match(r"ic\b", after):
            found.add("Yes")
    return found


def cites(text: str, code: str, value: str | None) -> bool:
    if value is None:
        return False
    if code in TERMS:
        return value in polarity(text, code)
    pattern = value_pattern(code, value)
    return bool(pattern and pattern.search(text))


def differing(prompt: str) -> list[tuple[str, str | None, str]]:
    """(code, PERSON's value or None, the model's value) where the model's record differs from PERSON's."""
    fields = {block["owner"]: block["fields"] for block in binding.record_blocks(prompt)}
    person, own = fields["person"], fields["own"]
    return [(code, person.get(code), own_value) for code, own_value in own.items()
            if person.get(code) != own_value]


def whose_values(text: str, disputed: list) -> tuple[bool, bool]:
    """(states a differing value of the model's record, states one of PERSON's); ``disputed`` as ``differing``
    returns it."""
    states_own = any(cites(text, code, own_value) for code, _, own_value in disputed)
    states_person = any(cites(text, code, person_value) for code, person_value, _ in disputed)
    return states_own, states_person


def first_stated(text: str, prompt: str) -> tuple[str, int, int]:
    """The first line of the reasoning that states a value of a field the records disagree on: whose value
    ("model's", "PERSON's", "both" or "none"), and the character offsets of that line's start and end in
    ``text``."""
    body = ANSWER.split(text or "")[0]
    disputed = differing(prompt)
    line_start = 0
    for line in body.split("\n"):
        states_own, states_person = whose_values(line, disputed)
        if states_own and states_person:
            return "both", line_start, line_start + len(line)
        if states_own:
            return "model's", line_start, line_start + len(line)
        if states_person:
            return "PERSON's", line_start, line_start + len(line)
        line_start += len(line) + 1
    return "none", len(body), len(body)


def yes_no_span(line: str, code: str, person_value: str, own_value: str):
    """The first place the line writes this yes/no field as "<name> ... Yes" or "... No" with one of the two
    records' values: (start, code, whose, end, the other record's value in the same letter case), or None."""
    for mention in re.finditer(TERMS[code], line, re.I):
        stated = VAL_AFTER.match(line[mention.end(): mention.end() + 60])
        if not stated or stated.group(1).capitalize() not in (person_value, own_value):
            continue
        wrote = stated.group(1)
        wrote_own = wrote.capitalize() == own_value
        other = person_value if wrote_own else own_value
        if wrote.islower():
            other = other.lower()
        elif wrote.isupper():
            other = other.upper()
        return (mention.end() + stated.start(1), code, "model's" if wrote_own else "PERSON's",
                mention.end() + stated.end(1), other)
    return None


def number_span(line: str, code: str, person_value: str, own_value: str):
    """The first place the line writes one of the two records' numbers for this field: (start, code, whose,
    end, the other record's number with as many decimals as the one written), or None."""
    whose_number = {re.match(r"([\d.]+)", value).group(1): whose
                    for value, whose in ((person_value, "PERSON's"), (own_value, "model's"))}
    by_short_form = {without_trailing_zeros(number): (number, whose)
                     for number, whose in whose_number.items()}
    for written in re.finditer(NUMBER[code], line, re.I):
        wrote = written.group(1)
        short_form = without_trailing_zeros(wrote)
        if short_form in by_short_form and len(by_short_form) == 2:
            other = next(number for number in by_short_form if number != short_form)
            if "." in wrote and "." not in other:
                other += "." + "0" * len(wrote.split(".")[1])
            return written.start(1), code, by_short_form[short_form][1], written.end(1), other
    return None


def stated_span(line: str, prompt: str):
    """Where a line first writes out a disputed value as a token that can be swapped: (field code, whose value
    ("model's" or "PERSON's"), start, end, the other record's value written the same way), or None. Covers
    yes/no fields written as "...: Yes" and numeric fields; not smoking status, and not paraphrases like "no
    diabetes"."""
    found = []
    for code, person_value, own_value in differing(prompt):
        if person_value is None:
            continue
        span = None
        if code in TERMS:
            span = yes_no_span(line, code, person_value, own_value)
        elif code in NUMBER:
            span = number_span(line, code, person_value, own_value)
        if span is not None:
            found.append(span)
    if not found:
        return None
    start, code, whose, end, other = min(found)     # the earliest in the line
    return code, whose, start, end, other


def category(text: str, prompt: str) -> str:
    """'own only', 'person only', 'both' or 'neither': whose differing values the reasoning states."""
    body = ANSWER.split(text or "")[0]
    states_own, states_person = whose_values(body, differing(prompt))
    if states_own and states_person:
        return "both"
    if states_own:
        return "own only"
    if states_person:
        return "person only"
    return "neither"
