"""Strictly parse scientific-forecasting responses into analysis tables.

Only the requested answer formats are accepted: the ``{"p_positive": ...}``
JSON (or an unambiguous keyed, boxed, or terminal restatement of it) for the
probability, and an explicit option choice for every multiple-choice output.
Unparseable responses are missing; nothing is imputed (Appendix: complete-case
retention).

Reads ``RESPONSES_DIR/forecasting/<task_id>/<model>.jsonl``, the prompts in
``PROMPTS_DIR/forecasting``, and ``SOURCES_DIR/forecasting/scenario_gold.csv``.  Writes
to ``PARSED_DIR/forecasting``:

* ``response_values.csv`` -- one row per model and prompt;
* ``paired_responses.csv`` -- one row per model and case, outputs in wide columns;
* ``parse_diagnostics.csv`` -- counts per model and output.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from latent_beliefs.config import PARSED_DIR, PROMPTS_DIR, RESPONSES_DIR, SOURCES_DIR
from latent_beliefs.forecasting.prompts import TASK_IDS
from latent_beliefs.models import MODELS

N_CASES = 500
ORDINAL_TASKS = {"likert", "probability_bin_quintile"}
SCENARIO_COLUMNS = (
    "scenario_id",
    "archive",
    "study",
    "study_outcome_group",
    "outcome",
    "condition_a",
    "condition_b",
    "material_status",
    "materials_ready",
)
GOLD_COLUMNS = (
    "gold_source",
    "gold_mean_a",
    "gold_mean_b",
    "gold_mean_difference",
    "gold_positive",
    "gold_n_a",
    "gold_n_b",
    "source_effect_id_a",
    "source_effect_id_b",
)


def read_jsonl(path: Path):
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc


def read_csv_by(path: Path, key: str) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {str(row[key]): row for row in csv.DictReader(handle)}


# ---------------------------------------------------------------- probability
def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_probability(value: Any) -> float | None:
    """Normalize a probability written as a proportion or percentage."""
    raw = str(value).strip()
    is_percent = raw.endswith("%") or raw.endswith(r"\%")
    if is_percent:
        raw = raw[:-2] if raw.endswith(r"\%") else raw[:-1]
    number = finite_number(raw.strip())
    if number is None:
        return None
    if is_percent or 1.0 < number <= 100.0:
        number /= 100.0
    return number if 0.0 <= number <= 1.0 else None


_NUMBER_LITERAL = (
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?"
)


def parse_numeric_literal(value: Any) -> float | None:
    """Parse a scalar or a simple fraction, but never an expression prefix."""
    raw = str(value).strip().strip("\"'")
    fraction = re.fullmatch(
        rf"(?P<numerator>{_NUMBER_LITERAL})[ \t]*/[ \t]*"
        rf"(?P<denominator>{_NUMBER_LITERAL})",
        raw,
    )
    if fraction is not None:
        numerator = finite_number(fraction.group("numerator").replace(",", ""))
        denominator = finite_number(fraction.group("denominator").replace(",", ""))
        if numerator is None or denominator in {None, 0.0}:
            return None
        return numerator / denominator
    if re.fullmatch(_NUMBER_LITERAL, raw) is None:
        return None
    return finite_number(raw.replace(",", ""))


def json_values(text: str, key: str) -> list[Any]:
    values: list[Any] = []
    for candidate in re.findall(r"\{[^{}]*\}", text, flags=re.DOTALL):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and key in parsed:
            values.append(parsed[key])
    return values


def parse_keyed_number(text: str, key: str) -> tuple[float | None, str | None]:
    for value in reversed(json_values(text, key)):
        number = finite_number(value)
        if number is not None:
            return number, "json"
    key_pattern = re.escape(key).replace("_", r"(?:_|\\_|-)")
    pattern = re.compile(
        rf'["\']?{key_pattern}["\']?[ \t]*(?:[:=]|≈)[ \t]*["\']?'
        rf'(?P<value>{_NUMBER_LITERAL}(?:[ \t]*/[ \t]*{_NUMBER_LITERAL})?)'
        r'(?=[ \t]*(?:["\']?[ \t]*(?:[,}\].;\n]|$)))',
        re.IGNORECASE,
    )
    for match in reversed(list(pattern.finditer(text))):
        number = parse_numeric_literal(match.group("value"))
        if number is not None:
            return number, "keyed_text"
    return None, None


def parse_probability(text: str) -> tuple[float | None, str | None]:
    # The requested JSON/keyed answer is authoritative.  Normalize string
    # percentages too, which ``finite_number`` intentionally does not do.
    for key in ("p_positive", "p-positive"):
        for value in reversed(json_values(text, key)):
            probability = normalize_probability(value)
            if probability is not None:
                return probability, "json"
    keyed, keyed_method = parse_keyed_number(text, "p_positive")
    if keyed is not None:
        probability = normalize_probability(keyed)
        if probability is not None:
            return probability, keyed_method
    probability_key = re.escape("p_positive").replace("_", r"(?:_|\\_|-)")
    percent_keyed = re.compile(
        rf'["\']?{probability_key}["\']?[ \t]*(?:[:=]|≈)[ \t]*["\']?'
        rf'(?P<value>{_NUMBER_LITERAL})[ \t]*(?P<percent>\\?%)'
        r'(?=[ \t]*(?:["\']?[ \t]*(?:[,}\].;\n]|$)))',
        re.IGNORECASE,
    )
    for match in reversed(list(percent_keyed.finditer(text))):
        probability = normalize_probability(match.group("value") + match.group("percent"))
        if probability is not None:
            return probability, "keyed_text"

    # A plain scalar in a terminal TeX box; numbers inside equations are not
    # guessed at.
    boxed = re.findall(
        r"\\boxed\s*\{\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(\\?%)?\s*\}",
        text,
        flags=re.IGNORECASE,
    )
    for number, percent in reversed(boxed):
        probability = normalize_probability(number + percent)
        if probability is not None:
            return probability, "boxed_probability"

    final_answers = re.findall(
        r"(?im)^\s*(?:(?:we|i)\s+conclude\s+that\s+|therefore[,]?\s+)?"
        r"(?:for\s+this\s+(?:particular\s+)?question[,]?\s+)?"
        r"(?:final\s+)?(?:answer|estimate|(?:the\s+)?probability)\s*"
        r"(?:is|[:=])\s*(?:therefore\s+|approximately\s+|about\s+|roughly\s+|"
        r"essentially\s+|very\s+close\s+to\s+)*("
        + _NUMBER_LITERAL
        + r")\s*(\\?%)?\b[^\n]*$",
        text,
    )
    for number, percent in reversed(final_answers):
        probability = normalize_probability(number + percent)
        if probability is not None:
            return probability, "terminal_probability"

    nonempty = [line.strip().strip("*_`$ ") for line in text.splitlines() if line.strip()]
    if nonempty:
        terminal = re.fullmatch(
            r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(\\?%)?\.?",
            nonempty[-1],
        )
        if terminal:
            probability = normalize_probability(
                terminal.group(1) + (terminal.group(2) or "")
            )
            if probability is not None:
                return probability, "terminal_probability"

    # A final worked line may state the event as an equation (for example
    # ``P(A > B) = 1 - .155 = .845``); only the last operand of a tail
    # equation is accepted.
    tail_lines = [
        line.strip().strip("*_`$ ") for line in text.splitlines() if line.strip()
    ]
    for line_index in reversed(range(max(0, len(tail_lines) - 3), len(tail_lines))):
        line = tail_lines[line_index]
        cue_context = "\n".join(tail_lines[max(0, line_index - 1) : line_index + 1])
        if re.search(
            r"\b(?:probability|p_positive)\b|\bP\s*\(",
            cue_context,
            re.IGNORECASE,
        ) is None:
            continue
        equation = re.search(
            rf"=[ \t]*(?P<value>{_NUMBER_LITERAL})[ \t]*(?P<percent>\\?%)?"
            r"[.!]?[ \t]*$",
            line,
        )
        if equation is None:
            continue
        percent = equation.group("percent") or ""
        raw_number = parse_numeric_literal(equation.group("value"))
        if raw_number is None or (not percent and not 0.0 <= raw_number <= 1.0):
            continue
        probability = normalize_probability(equation.group("value") + percent)
        if probability is not None:
            return probability, "terminal_equation"
    return None, None


# ------------------------------------------------------------------- choices
_CHOICE_TEXT = {
    "very unlikely": "A",
    "somewhat unlikely": "B",
    "neither likely nor unlikely": "C",
    "somewhat likely": "D",
    "very likely": "E",
    "less than 20%": "A",
    "20% to less than 40%": "B",
    "40% to less than 60%": "C",
    "60% to less than 80%": "D",
    "80% or higher": "E",
}


def _choice_from_option_text(payload: str) -> str | None:
    lowered = payload.lower()
    for phrase, choice in _CHOICE_TEXT.items():
        if phrase in lowered:
            return choice
    if re.search(r"\b(?:declin\w*|reject\w*|refus\w*)\s+(?:the\s+)?bet\b", lowered):
        return "B"
    if re.search(r"\b(?:do not|don't|not)\s+accept\w*\s+(?:the\s+)?bet\b", lowered):
        return "B"
    if re.search(r"\baccept\w*\s+(?:the\s+)?bet\b", lowered):
        return "A"
    # A negated prioritization is not an affirmative choice of the other arm.
    if re.search(r"\b(?:do not|don't|not|never)\b[^.\n]{0,20}\bprioriti[sz]", lowered):
        return None
    priority = re.search(
        r"\bprioriti[sz]\w*\s+(?:an\s+experiment\s+on\s+)?condition\s+([ab])\b",
        lowered,
    )
    if priority:
        return priority.group(1).upper()
    return None


def _choice_from_payload(value: Any) -> tuple[str | None, str | None]:
    payload = str(value).strip().strip("*_`\"' ")
    payload = re.sub(r"[.!;]+\s*$", "", payload).strip()
    letter = re.match(
        r"^(?i:choice[ \t]*:?[ \t]*)?\(?([A-E])\)?(?:[.):]|\s|$)", payload
    )
    if letter:
        choice = letter.group(1).upper()
        trailing = payload[letter.end() :].strip()
        described_choice = _choice_from_option_text(trailing) if trailing else None
        if described_choice is not None and described_choice != choice:
            return None, None
        return choice, "letter"

    choice = _choice_from_option_text(payload)
    if choice is not None:
        return choice, "text"
    return None, None


def _terminal_option_payload(value: str) -> tuple[str | None, str | None]:
    """Parse a terminal option only when its text validates the option label."""
    payload = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", value).strip()
    payload = payload.strip("*_` ")
    leading = re.match(r"^\(?([A-E])\)?(?:[.):])[ \t]*(.+)$", payload)
    if leading:
        described_choice = _choice_from_option_text(leading.group(2))
        if described_choice == leading.group(1):
            return leading.group(1), "letter"
        return None, None
    terminal = re.sub(r"[.!;]+$", "", payload).strip().lower()
    exact_option = terminal in _CHOICE_TEXT or bool(
        re.fullmatch(
            r"(?:accept|decline|reject|refuse)(?:\s+(?:the|this))?\s+bet|"
            r"prioriti[sz]e(?:\s+an\s+experiment\s+on)?\s+condition\s+[ab]",
            terminal,
        )
    )
    if not exact_option:
        return None, None
    described_choice = _choice_from_option_text(terminal)
    return described_choice, "text"


def _terminal_conclusion_choice(value: str) -> str | None:
    """Detect a prose recommendation that concludes the response.

    Such a conclusion is never accepted as an answer, but when it is the last
    choice-like statement it supersedes (and so invalidates) any earlier
    formatted option; see ``parse_choice``.
    """
    normalized = value.strip().strip("*_` ")
    match = re.search(
        r"(?i)(?P<clause>(?:therefore|thus|consequently|as a result|"
        r"in conclusion|overall|indicat(?:e|es|ed|ing) that)\b"
        r"[^.\n]{0,220}?\b(?:i|we|you|one)?[ \t]*"
        r"(?:should|would|will|must|ought to|recommend(?:ed|s|ing)?)\s+"
        r"(?P<action>accept|decline|reject|refuse)\s+(?:the|this)\s+bet"
        r"[.!]?[ \t]*)$",
        normalized,
    )
    if match is not None and re.search(
        r"\bif\b", match.group("clause"), re.IGNORECASE
    ) is None:
        return "A" if match.group("action").lower() == "accept" else "B"

    labeled_bet = re.search(
        r"(?i)(?:leans?\s+towards?|(?:answer|choice|decision)\s+(?:is|would\s+be))"
        r"[^.\n]{0,40}?\b([AB])\s*[.):][ \t]*"
        r"(accept|decline|reject|refuse)\s+(?:the|this)\s+bet\b[^\n]*$",
        normalized,
    )
    if labeled_bet is not None:
        described = "A" if labeled_bet.group(2).lower() == "accept" else "B"
        return described if labeled_bet.group(1).upper() == described else None

    return None


def _explicit_choice_candidates(text: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    normalized = text.replace("*", " ").replace("_", " ").replace("`", " ")

    for match in re.finditer(r"\{[^{}]*\}", text, flags=re.DOTALL):
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key in ("choice", "answer", "final_answer"):
            if key not in parsed:
                continue
            choice, _ = _choice_from_payload(parsed[key])
            if choice is not None:
                candidates.append((match.start(), choice, "json_choice"))

    keyed = re.compile(
        r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?"
        r"(?P<label>choice|choose|answer|final answer|best answer)[ \t]*"
        r"(?:is|[:=])[ \t]*(?P<payload>[^\n]+)$"
    )
    for match in keyed.finditer(normalized):
        choice, payload_kind = _choice_from_payload(match.group("payload"))
        if choice is not None:
            method = "choice_letter" if payload_kind == "letter" else "choice_text"
            if match.group("label").lower() not in {"choice", "choose"}:
                method = "answer_letter" if payload_kind == "letter" else "answer_text"
            candidates.append((match.start(), choice, method))

    boxed = re.compile(
        r"\\boxed\s*\{\s*(?:\\text\s*\{\s*)?(?P<payload>[^{}\n]+)\s*\}?\s*\}",
        re.IGNORECASE,
    )
    for match in boxed.finditer(text):
        choice, payload_kind = _choice_from_payload(match.group("payload"))
        if choice is not None:
            # The end of the whole box, so an inner ``Choice: B`` cannot
            # outrank its boxed provenance.
            candidates.append((match.end(), choice, "boxed_choice"))

    prose_answer = re.compile(
        r"(?i:\bthe\s+(?:(?:final|best|correct)\s+)?(?:answer|choice)\s+is\s+)"
        r"(?i:(?:therefore\s+)?(?:choice\s+)?)\(?([A-E])\)?\b"
    )
    candidates.extend(
        (match.start(), match.group(1).upper(), "answer_letter")
        for match in prose_answer.finditer(normalized)
    )

    lines = list(re.finditer(r"(?m)^.*$", normalized))
    substantive = [match for match in lines if match.group(0).strip()]
    for match in substantive[-12:]:
        bare = re.fullmatch(
            r"\s*\(?([A-E])\)?(?:[.)])?\s*", match.group(0), flags=re.IGNORECASE
        )
        if bare:
            candidates.append((match.start(), bare.group(1).upper(), "final_option"))
            continue
        choice, payload_kind = _terminal_option_payload(match.group(0))
        if choice is not None:
            candidates.append((match.start(), choice, "final_option_text"))

    if substantive:
        conclusion_choice = _terminal_conclusion_choice(substantive[-1].group(0))
        if conclusion_choice is not None:
            candidates.append(
                (substantive[-1].end(), conclusion_choice, "terminal_conclusion")
            )
    return candidates


def parse_choice(text: str) -> tuple[str | None, str | None]:
    candidates = _explicit_choice_candidates(text)
    if not candidates:
        return None, None
    _, choice, method = max(candidates, key=lambda item: item[0])
    # A later prose conclusion invalidates an earlier formatted answer but
    # cannot itself supply one.
    if method == "terminal_conclusion":
        return None, None
    return choice, method


# ------------------------------------------------------------- per response
def parse_output(task_id: str, text: str) -> dict[str, Any]:
    """Strictly parse one response to the forecasting prompt ``task_id``.

    Returns ``value`` (probability in [0, 1]; 1/0 for choosing option A/B;
    1-5 for the ordinal outputs; ``None`` if unparseable), ``method``,
    ``probability``, and ``choice``.
    """
    choice: str | None = None
    probability: float | None = None
    if task_id == "probability_positive":
        probability, method = parse_probability(text)
        value = probability
    else:
        choice, method = parse_choice(text)
        if task_id in ORDINAL_TASKS:
            value = float(ord(choice) - ord("A") + 1) if choice else None
        else:
            value = float(choice == "A") if choice in {"A", "B"} else None
    return {"value": value, "method": method, "probability": probability, "choice": choice}


# ---------------------------------------------------------------------- main
def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--responses-dir", type=Path, default=RESPONSES_DIR / "forecasting")
    parser.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR / "forecasting")
    parser.add_argument("--gold", type=Path, default=SOURCES_DIR / "forecasting" / "scenario_gold.csv")
    parser.add_argument("--out-dir", type=Path, default=PARSED_DIR / "forecasting")
    args = parser.parse_args()

    prompts: dict[str, dict[str, Any]] = {}
    for task_id in TASK_IDS:
        rows = list(read_jsonl(args.prompts_dir / f"{task_id}.jsonl"))
        if len(rows) != N_CASES:
            raise ValueError(f"{task_id}: expected {N_CASES} prompts, found {len(rows)}")
        for row in rows:
            prompts[str(row["prompt_id"])] = row
    gold = read_csv_by(args.gold, "scenario_id")

    parsed_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    diagnostics: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for task_id in TASK_IDS:
        for model in sorted(MODELS):
            path = args.responses_dir / task_id / f"{model}.jsonl"
            for raw in read_jsonl(path):
                prompt_id = str(raw.get("prompt_id", ""))
                prompt = prompts.get(prompt_id)
                if prompt is None:
                    raise ValueError(f"Unknown prompt ID in {path}: {prompt_id}")
                identity = (str(raw["model_alias"]), prompt_id)
                if identity in seen:
                    raise ValueError(f"Duplicate model/prompt response: {identity}")
                seen.add(identity)
                output_text = str(raw.get("output_text") or "")
                raw_ok = bool(raw.get("ok"))
                parsed = (
                    parse_output(task_id, output_text)
                    if raw_ok
                    else {"value": None, "method": None, "probability": None, "choice": None}
                )
                value, choice = parsed["value"], parsed["choice"]
                usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
                gold_row = gold[str(prompt["scenario_id"])]
                record = {
                    "model_alias": identity[0],
                    "model_id": raw.get("model_id"),
                    "source": raw.get("source"),
                    **{column: prompt.get(column) for column in SCENARIO_COLUMNS},
                    "framing": task_id,
                    "prompt_id": prompt_id,
                    "implied_probability_threshold": prompt.get("implied_probability_threshold"),
                    **{column: gold_row.get(column) for column in GOLD_COLUMNS},
                    "raw_ok": int(raw_ok),
                    "parse_ok": int(value is not None),
                    "parse_method": parsed["method"],
                    "parsed_value": value,
                    "parsed_probability": parsed["probability"],
                    "parsed_choice": choice,
                    "parsed_choice_a": (
                        int(choice == "A")
                        if choice in {"A", "B"} and task_id not in ORDINAL_TASKS
                        else None
                    ),
                    "stop_reason": raw.get("stop_reason"),
                    "truncated": int(str(raw.get("stop_reason") or "").lower() == "max_tokens"),
                    "input_tokens": usage.get("inputTokens", usage.get("input_tokens")),
                    "output_tokens": usage.get("outputTokens", usage.get("output_tokens")),
                    "total_tokens": usage.get("totalTokens", usage.get("total_tokens")),
                    "response_characters": len(output_text),
                    "record_id": raw.get("record_id"),
                    "error": (
                        json.dumps(raw.get("error"), ensure_ascii=False)
                        if raw.get("error")
                        else None
                    ),
                }
                parsed_rows.append(record)
                diag = diagnostics[(identity[0], task_id)]
                diag["raw_rows"] += 1
                diag["raw_ok_rows"] += int(raw_ok)
                diag["parsed_rows"] += record["parse_ok"]
                diag["parse_failures"] += int(raw_ok and not record["parse_ok"])
                diag["raw_error_rows"] += int(not raw_ok)
                diag["truncated_rows"] += record["truncated"]

    parsed_rows.sort(key=lambda row: (row["model_alias"], row["scenario_id"], row["framing"]))
    out_dir = args.out_dir
    write_csv(out_dir / "response_values.csv", parsed_rows, list(parsed_rows[0]))

    paired: dict[tuple[str, str], dict[str, Any]] = {}
    base_columns = ["model_alias", "model_id", "source", *SCENARIO_COLUMNS, *GOLD_COLUMNS]
    for row in parsed_rows:
        key = (str(row["model_alias"]), str(row["scenario_id"]))
        if key not in paired:
            paired[key] = {column: row.get(column) for column in base_columns}
        prefix = str(row["framing"])
        paired[key].update(
            {
                f"{prefix}__prompt_id": row["prompt_id"],
                f"{prefix}__value": row["parsed_value"],
                f"{prefix}__choice": row["parsed_choice"],
                f"{prefix}__parse_method": row["parse_method"],
                f"{prefix}__parse_ok": row["parse_ok"],
                f"{prefix}__raw_ok": row["raw_ok"],
            }
        )
    wide_fields = list(base_columns)
    for task_id in TASK_IDS:
        wide_fields.extend(
            f"{task_id}__{suffix}"
            for suffix in ("prompt_id", "value", "choice", "parse_method", "parse_ok", "raw_ok")
        )
    write_csv(
        out_dir / "paired_responses.csv", [paired[key] for key in sorted(paired)], wide_fields
    )

    diag_rows = [
        {"model_alias": model, "framing": task_id, **counts}
        for (model, task_id), counts in sorted(diagnostics.items())
    ]
    diag_fields = [
        "model_alias", "framing", "raw_rows", "raw_ok_rows", "parsed_rows",
        "parse_failures", "raw_error_rows", "truncated_rows",
    ]
    write_csv(out_dir / "parse_diagnostics.csv", diag_rows, diag_fields)
    print(
        f"Parsed {sum(row['parse_ok'] for row in parsed_rows)} of {len(parsed_rows)} "
        f"responses; wrote {out_dir}"
    )


if __name__ == "__main__":
    main()
