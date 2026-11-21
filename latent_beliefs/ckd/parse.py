"""Strictly parse the CKD responses (Methods; Appendix Table on complete-case retention).

The probability family must contain a JSON object (or an explicit
``"probability": <number>`` / ``probability ... <number>`` statement); the
choice families must end with a ``Choice:`` line (or a final lettered option)
naming one of the offered options.  Responses that do not are left unparsed;
nothing is imputed.

Inputs:  ``PROMPTS_DIR/ckd/tasks.json`` and prompt files, and
         ``RESPONSES_DIR/ckd/<task_id>/<model>.jsonl``.
Outputs (``PARSED_DIR/ckd``):
  ``response_values.csv``   one row per model x case x family;
  ``paired_responses.csv``  one row per model x case, families side by side;
  ``parse_diagnostics.csv`` response and parse counts per model x family.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterator

from latent_beliefs.ckd.prompts import CKD_PROMPTS_DIR, load_tasks
from latent_beliefs.config import PARSED_DIR, RESPONSES_DIR, ensure
from latent_beliefs.models import MODELS

CKD_RESPONSES_DIR = RESPONSES_DIR / "ckd"
CKD_PARSED_DIR = PARSED_DIR / "ckd"
STRICT = "strict"
CASE_COLUMNS = (
    "source_prompt_id",
    "base_source_prompt_id",
    "source_cohort",
    "row_index",
    "seqn",
    "risk_estimate",
    "risk_stratum",
    "ckd",
)

# Strict response parsers
POSITIVE_PHRASES = (
    "accept the bet",
    "screen this patient for ckd",
    "recommend screening for ckd",
    "bet that this patient has ckd",
)
NEGATIVE_PHRASES = (
    "decline the bet",
    "do not accept the bet",
    "do not screen this patient for ckd",
    "recommend not screening for ckd",
    "recommend not betting",
    "do not bet that this patient has ckd",
)
LIKERT_TEXT_TO_VALUE = {
    "very unlikely": 1,
    "somewhat unlikely": 2,
    "neither likely nor unlikely": 3,
    "somewhat likely": 4,
    "very likely": 5,
}
LETTER_TO_ORDINAL = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
PROBABILITY_BIN_TEXT_TO_VALUE = {
    "less than 5.07%": 1,
    "5.07% to less than 7.49%": 2,
    "7.49% to less than 13.02%": 3,
    "13.02% to less than 29.72%": 4,
    "29.72% or higher": 5,
}


def normalize_probability(value: Any) -> float | None:
    """Read a probability; percentages and numbers in (1, 100] are divided by 100."""
    is_percent = isinstance(value, str) and value.strip().endswith("%")
    try:
        p = float(value.strip()[:-1] if is_percent else value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p):
        return None
    if is_percent or 1.0 < p <= 100.0:
        p /= 100.0
    if 0.0 <= p <= 1.0:
        return p
    return None


def parse_probability(output_text: str) -> float | None:
    for candidate in reversed(re.findall(r"\{[^{}]*\}", output_text, flags=re.DOTALL)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        for key in ["probability", "posterior_probability", "ckd_probability"]:
            if key in parsed:
                p = normalize_probability(parsed[key])
                if p is not None:
                    return p
    for pattern in [
        r'"(?:probability|posterior_probability|ckd_probability)"\s*:\s*"?([0-9]*\.?[0-9]+)\s*(%?)"?',
        r"\bprobability\b[^0-9]{0,40}([0-9]*\.?[0-9]+)\s*(%?)",
    ]:
        matches = re.findall(pattern, output_text, flags=re.IGNORECASE | re.DOTALL)
        for number, percent in reversed(matches):
            p = normalize_probability(number + percent)
            if p is not None:
                return p
    return None


def _normalize_choice_line(line: str) -> str:
    normalized = line.strip().strip("*_`").strip()
    normalized = re.sub(r"^#{1,6}\s*", "", normalized)
    return normalized.strip().strip("*_`").strip()


def _choice_candidates(output_text: str) -> list[str]:
    matches: list[str] = []
    for line in output_text.splitlines():
        normalized = _normalize_choice_line(line)
        if re.match(r"^Choice\b", normalized, flags=re.IGNORECASE):
            choice = re.sub(r"^Choice\s*:?\s*", "", normalized, flags=re.IGNORECASE)
            choice = choice.strip().strip("*_`:").strip()
            if choice:
                matches.append(choice)
    if matches:
        return matches
    nonempty = [_normalize_choice_line(line) for line in output_text.splitlines() if line.strip()]
    for line in reversed(nonempty[-12:]):
        if re.match(r"^(?:[abcde](?:[.):]|$)|\([abcde]\))(?:\s|$)", line, flags=re.IGNORECASE):
            return [line]
    return []


def parse_decision(output_text: str) -> float | None:
    """1 = act (screen / accept the bet), 0 = do not act."""
    choices = _choice_candidates(output_text)
    if not choices:
        return None
    choice = choices[-1].lower()
    if re.match(r"^(?:b(?:[.):](?:\s|$)|$)|\(b\)(?:\s|$))", choice):
        return 0.0
    if re.match(r"^(?:a(?:[.):](?:\s|$)|$)|\(a\)(?:\s|$))", choice):
        return 1.0
    for phrase in NEGATIVE_PHRASES:
        if phrase in choice:
            return 0.0
    for phrase in POSITIVE_PHRASES:
        if phrase in choice:
            return 1.0
    return None


def parse_ordinal5(output_text: str) -> float | None:
    """Option A..E -> 1..5 for the Likert and probability-bin families."""
    choices = _choice_candidates(output_text)
    if not choices:
        return None
    choice = choices[-1].lower()
    m = re.match(r"^(?:\(([abcde])\)|([abcde])(?:[.):](?=\s|$)|$))", choice)
    if m:
        return float(LETTER_TO_ORDINAL[m.group(1) or m.group(2)])
    for phrase, value in LIKERT_TEXT_TO_VALUE.items():
        if phrase in choice:
            return float(value)
    for phrase, value in PROBABILITY_BIN_TEXT_TO_VALUE.items():
        if phrase in choice:
            return float(value)
    return None


def parser_for(family: str) -> Callable[[str], float | None]:
    """Strict parser for a family (output name such as ``likert`` or ``bet_payoff_2to1``)."""
    if family == "probability":
        return parse_probability
    if family in {"likert", "probability_bin_quintile"}:
        return parse_ordinal5
    return parse_decision


def parse_response(family: str, output_text: str) -> tuple[float | None, str | None]:
    """Return ``(value, "strict")``, or ``(None, None)`` if the response does not parse."""
    value = parser_for(family)(output_text)
    return value, (STRICT if value is not None else None)


# Tables
def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    ensure(path.parent)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def parse_all(
    prompts_dir: Path, responses_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], dict[tuple[str, str], Counter]]:
    tasks = load_tasks(prompts_dir)
    prompts: dict[str, dict[str, Any]] = {}
    for task in tasks:
        for row in read_jsonl(Path(task["path"])):
            prompt_id = str(row["prompt_id"])
            if prompt_id in prompts:
                raise ValueError(f"Duplicate prompt ID: {prompt_id}")
            prompts[prompt_id] = row

    parsed_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    diagnostics: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for task in tasks:
        family = task["output"]
        for model in MODELS:
            path = responses_dir / task["task_id"] / f"{model}.jsonl"
            for raw in read_jsonl(path):
                prompt_id = str(raw.get("prompt_id", ""))
                prompt = prompts.get(prompt_id)
                if prompt is None:
                    raise ValueError(f"Unknown prompt ID in {path}: {prompt_id}")
                identity = (str(raw["model_alias"]), prompt_id)
                if identity in seen:
                    raise ValueError(f"Duplicate model/prompt response: {identity}")
                seen.add(identity)
                raw_ok = bool(raw.get("ok"))
                output_text = str(raw.get("output_text") or "")
                value, parse_method = (
                    parse_response(family, output_text) if raw_ok else (None, None)
                )
                parse_ok = value is not None
                usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
                parsed_rows.append(
                    {
                        "model_alias": raw["model_alias"],
                        "model_id": raw.get("model_id"),
                        "source": raw.get("source"),
                        **{column: prompt.get(column) for column in CASE_COLUMNS},
                        "prompt_variant": family,
                        "prompt_id": prompt_id,
                        "prompt_family": prompt.get("prompt_family"),
                        "decision_formulation": prompt.get("decision_formulation"),
                        "threshold": prompt.get("threshold"),
                        "implied_threshold": prompt.get("implied_threshold"),
                        "false_negative_cost_ratio": prompt.get("false_negative_cost_ratio"),
                        "raw_ok": int(raw_ok),
                        "parse_ok": int(parse_ok),
                        "parse_method": parse_method,
                        "parsed_value": value,
                        "stop_reason": raw.get("stop_reason"),
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
                )
                diag = diagnostics[(str(raw["model_alias"]), family)]
                diag["raw_rows"] += 1
                diag["raw_ok_rows"] += int(raw_ok)
                diag["parsed_rows"] += int(parse_ok)
                diag["parse_failures"] += int(raw_ok and not parse_ok)
                diag["raw_error_rows"] += int(not raw_ok)

    parsed_rows.sort(
        key=lambda row: (
            str(row["model_alias"]),
            str(row["source_prompt_id"]),
            str(row["prompt_variant"]),
        )
    )
    families = [task["output"] for task in tasks]

    base_fields = ["model_alias", "model_id", "source", *CASE_COLUMNS]
    paired: dict[tuple[str, str], dict[str, Any]] = {}
    for row in parsed_rows:
        key = (str(row["model_alias"]), str(row["source_prompt_id"]))
        if key not in paired:
            paired[key] = {field: row.get(field) for field in base_fields}
        prefix = str(row["prompt_variant"])
        paired[key].update(
            {
                f"{prefix}__prompt_id": row["prompt_id"],
                f"{prefix}__value": row["parsed_value"],
                f"{prefix}__parse_ok": row["parse_ok"],
                f"{prefix}__raw_ok": row["raw_ok"],
            }
        )
    paired_rows = [paired[key] for key in sorted(paired)]
    return parsed_rows, paired_rows, families, diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompts-dir", type=Path, default=CKD_PROMPTS_DIR)
    parser.add_argument("--responses-dir", type=Path, default=CKD_RESPONSES_DIR)
    parser.add_argument("--out-dir", type=Path, default=CKD_PARSED_DIR)
    args = parser.parse_args()

    parsed_rows, paired_rows, families, diagnostics = parse_all(
        args.prompts_dir, args.responses_dir
    )
    write_csv(args.out_dir / "response_values.csv", parsed_rows, list(parsed_rows[0]))

    base_fields = ["model_alias", "model_id", "source", *CASE_COLUMNS]
    wide_fields = list(base_fields)
    for family in families:
        wide_fields.extend(
            f"{family}__{suffix}" for suffix in ("prompt_id", "value", "parse_ok", "raw_ok")
        )
    write_csv(args.out_dir / "paired_responses.csv", paired_rows, wide_fields)

    diag_fields = [
        "model_alias",
        "prompt_variant",
        "raw_rows",
        "raw_ok_rows",
        "parsed_rows",
        "parse_failures",
        "raw_error_rows",
    ]
    diag_rows = [
        {"model_alias": model, "prompt_variant": family, **counts}
        for (model, family), counts in sorted(diagnostics.items())
    ]
    write_csv(args.out_dir / "parse_diagnostics.csv", diag_rows, diag_fields)

    n_parsed = sum(row["parse_ok"] for row in parsed_rows)
    print(f"Parsed {n_parsed} of {len(parsed_rows)} responses; wrote {args.out_dir}")


if __name__ == "__main__":
    main()
