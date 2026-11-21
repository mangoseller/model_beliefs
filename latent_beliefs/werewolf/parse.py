"""Strict parsing of the Werewolf responses (16 outputs per case).

Every response is parsed by explicit answer formats only: the requested JSON
probability or a keyed/boxed/terminal probability statement, and the
requested Choice: line or an equivalent explicit option statement.
Responses without such an answer, or whose answer is internally inconsistent,
are left unparsed (missing); nothing is inferred from free-text reasoning.

Supports the parse-rate and complete-case retention table (Appendix) and every
Werewolf analysis downstream of the analysis matrix.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from latent_beliefs.ckd.parse import normalize_probability
from latent_beliefs.config import PARSED_DIR, PROMPTS_DIR, RESPONSES_DIR, ensure
from latent_beliefs.models import MODELS
from latent_beliefs.werewolf.guards import (
    _BET_OPTION_PAIR,
    _BET_PASSIVE,
    _WEREWOLF_VOTE_ACTION,
    commitment_candidates,
    explicit_answer_has_conflicting_option,
    explicit_answer_has_wrong_werewolf_target,
    ordinal_phrases,
)

CASE_COLUMNS = (
    "case_id",
    "sample_index",
    "replay_id",
    "step_index",
    "slot_index",
    "focal_player",
    "focal_role",
    "target_player",
    "target_role_ground_truth",
    "label_is_werewolf",
    "day",
    "detailed_phase",
    "cut_rank",
    "eligible_cut_count_in_game",
    "cut_progress",
    "cut_quartile",
    "cut_quartile_label",
    "sample_stratum",
)


def parser_kind(family: str) -> str:
    """probability, ordinal5 (Likert and probability bins), or decision."""
    if family == "probability":
        return "probability"
    if family in {"likert", "probability_bin_quintile"}:
        return "ordinal5"
    return "decision"


def _is_bet(family: str) -> bool:
    return "bet" in family


_VOTE_NEGATION = re.compile(
    r"\b(?:not|never|cannot|can't|won't|wouldn't|shouldn't|don't|didn't)\b",
    re.IGNORECASE,
)


def _normalize_probability(value: Any) -> float | None:
    try:
        normalized = normalize_probability(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return float(normalized) if normalized is not None else None


def _vote_value(payload: str, target_player: str | None) -> float | None:
    matches = list(_WEREWOLF_VOTE_ACTION.finditer(payload))
    for match in matches:
        if target_player is not None and match.group("target").casefold() != str(
            target_player
        ).casefold():
            return None
        prefix = payload[max(0, match.start() - 55) : match.start()]
        local_clause = re.split(r"[,;]", prefix)[-1]
        return 0.0 if _VOTE_NEGATION.search(local_clause) else 1.0
    return None


def _choice_payload_value(
    family: str, parser: str, payload: Any, target_player: str | None
) -> tuple[float | None, str | None]:
    cleaned = str(payload).strip().strip("*_`\"' ")
    cleaned = re.sub(r"[.!;]+\s*$", "", cleaned).strip()
    max_letter = "E" if parser == "ordinal5" else "B"
    letter = re.match(
        rf"^(?i:choice[ \t]*:?[ \t]*)?\(?([A-{max_letter}])\)?(?:[.):]|\s|$)",
        cleaned,
    )
    if letter:
        choice = letter.group(1).upper()
        value = (
            float(ord(choice) - ord("A") + 1)
            if parser == "ordinal5"
            else float(choice == "A")
        )
        trailing = cleaned[letter.end() :]
        if parser == "ordinal5" and trailing.strip():
            described = {
                option_value
                for phrase, option_value in ordinal_phrases(family).items()
                if phrase in trailing.lower()
            }
            if len(described) == 1 and next(iter(described)) != value:
                return None, None
        elif parser == "decision" and trailing.strip():
            if _is_bet(family) and re.search(r"\b(?:accept|declin|reject|refus)", trailing, re.I):
                text_value = _bet_value(trailing)
                if text_value is None or text_value != value:
                    return None, None
            elif not _is_bet(family) and re.search(
                r"\b(?:vot|eliminat)", trailing, re.I
            ):
                text_value = _vote_value(trailing, target_player)
                if text_value is None or text_value != value:
                    return None, None
        return value, "letter"

    lowered = cleaned.lower()
    if parser == "ordinal5":
        for phrase, value in ordinal_phrases(family).items():
            if phrase in lowered:
                return value, "text"
        return None, None
    if parser != "decision":
        return None, None
    if _is_bet(family):
        value = _bet_value(cleaned)
        return (value, "text") if value is not None else (None, None)
    value = _vote_value(cleaned, target_player)
    return (value, "text") if value is not None else (None, None)


def _terminal_labeled_option_value(
    family: str, parser: str, line: str, target_player: str | None
) -> float | None:
    """Parse one terminal ``A. <exact option>`` line with text validation."""
    cleaned = line.strip().strip("*_` ")
    max_letter = "E" if parser == "ordinal5" else "B"
    labeled = re.fullmatch(
        rf"\(?([A-{max_letter}])\)?[.):][ \t]*(\S[^\n]*)",
        cleaned,
    )
    if labeled is None:
        return None
    letter = labeled.group(1).upper()
    trailing = labeled.group(2)
    expected = (
        float(ord(letter) - ord("A") + 1)
        if parser == "ordinal5"
        else float(letter == "A")
    )
    if parser == "ordinal5":
        option_text = trailing.strip().strip("\"'“”‘’ ").rstrip(".!;").strip().lower()
        described = ordinal_phrases(family).get(option_text)
        return expected if described == expected else None
    if _is_bet(family):
        option_text = trailing.strip().rstrip(".!;").strip()
        if re.fullmatch(
            r"(?:accept|decline|reject|refuse)(?:\s+(?:the|this))?\s+bet",
            option_text,
            flags=re.IGNORECASE,
        ) is None:
            return None
        described = _bet_value(option_text)
    else:
        option_text = trailing.strip().rstrip(".!;").strip()
        if re.fullmatch(
            r"(?:(?:do\s+not|don't|not|never)\s+)?vot(?:e|ing)\s+to\s+"
            r"eliminat(?:e|ing)?\s+[\"'“‘]?[A-Za-z][\w'-]*[\"'”’]?",
            option_text,
            flags=re.IGNORECASE,
        ) is None:
            return None
        described = _vote_value(option_text, target_player)
    return expected if described == expected else None


def _bet_value(payload: str) -> float | None:
    lowered = payload.lower()
    paired_spans = [match.span() for match in _BET_OPTION_PAIR.finditer(payload)]

    def paired(position: int) -> bool:
        return any(start <= position < end for start, end in paired_spans)

    negative = list(
        re.finditer(
            r"\b(?:declin\w*|reject\w*|refus\w*)\s+(?:th(?:e|is)\s+)?bet\b|"
            r"\b(?:do not|don't|will not|won't|would not|wouldn't|cannot|can't)"
            r"\s+(?:\w+\s+){0,3}accept\w*\s+(?:th(?:e|is)\s+)?bet\b",
            lowered,
        )
    )
    negative = [match for match in negative if not paired(match.start())]
    positive = [
        match
        for match in re.finditer(
            r"\baccept\w*\s+(?:th(?:e|is)\s+)?bet\b", lowered
        )
        if not paired(match.start())
        and not any(neg.start() <= match.start() < neg.end() for neg in negative)
    ]
    candidates = [(match.start(), 0.0) for match in negative]
    candidates.extend((match.start(), 1.0) for match in positive)
    for match in _BET_PASSIVE.finditer(payload):
        value = 1.0 if match.group(2).lower().startswith("accept") else 0.0
        if match.group(1):
            value = 1.0 - value
        candidates.append((match.start(), value))
    return max(candidates)[1] if candidates else None


def _explicit_probability_candidates(
    text: str, target_player: str | None = None
) -> list[tuple[int, float, str]]:
    candidates: list[tuple[int, float, str]] = []
    # Preserve offsets while neutralizing Markdown emphasis around labels.
    normalized = text.replace("*", " ").replace("`", " ")
    for match in re.finditer(r"\{[^{}]*\}", text, flags=re.DOTALL):
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key in ("probability", "werewolf_probability", "posterior_probability"):
            if key in parsed:
                value = _normalize_probability(parsed[key])
                if value is not None:
                    candidates.append((match.end(), value, "strict_json_probability"))

    keyed = re.compile(
        r'["\']?(?:probability|werewolf_probability|posterior_probability)["\']?'
        r"[ \t]*[:=][ \t]*[\"']?([0-9]*\.?[0-9]+\s*(?:\\?%)?)[\"']?",
        re.IGNORECASE,
    )
    for match in keyed.finditer(normalized):
        value = _normalize_probability(match.group(1))
        if value is not None:
            candidates.append((match.end(), value, "strict_keyed_probability"))

    boxed = re.compile(
        r"\\boxed\s*\{\s*([0-9]*\.?[0-9]+)\s*(\\?%)?\s*\}", re.I
    )
    for match in boxed.finditer(text):
        value = _normalize_probability(match.group(1) + (match.group(2) or ""))
        if value is not None:
            candidates.append((match.end(), value, "strict_boxed_probability"))

    terminal = re.compile(
        r"(?im)^\s*(?:final\s+)?"
        r"(?:answer|estimate|probability(?:\s+estimate)?)\s*"
        r"(?:is|[:=])\s*([0-9]*\.?[0-9]+)\s*(\\?%)?\s*[.!]?\s*$"
    )
    for match in terminal.finditer(normalized):
        value = _normalize_probability(match.group(1) + (match.group(2) or ""))
        if value is not None:
            candidates.append((match.end(), value, "strict_terminal_probability"))

    if target_player:
        target = re.escape(str(target_player))
        number = (
            r"(?P<number>(?:0?\.[0-9]+|[01](?:\.0+)?|"
            r"[0-9]{1,3}(?:\.[0-9]+)?\s*%))"
            r"(?=[\s,.;:!?)\]}]|$)"
        )
        targeted_patterns = (
            # "The/Estimated probability of Jordan being a Werewolf: 0.25"
            re.compile(
                rf"(?im)^[ \t]*(?:the[ \t]+)?(?:estimated[ \t]+)?"
                rf"probability\b[^\n:]{{0,140}}\b{target}\b[^\n:]{{0,80}}"
                rf"[:=][ \t]*~?[ \t]*{number}",
                re.IGNORECASE,
            ),
            # "I estimate the probability that Charlie ... to be 0."
            re.compile(
                rf"\b(?:i[ \t]+)?estimate[ \t]+(?:the[ \t]+)?probability\b"
                rf"[^.\n]{{0,140}}\b{target}\b[^.\n]{{0,100}}?"
                rf"(?:to[ \t]+be|as|at|:)[ \t]*~?[ \t]*{number}",
                re.IGNORECASE,
            ),
            # "I estimate a ~65% probability that Jamie is a Werewolf."
            re.compile(
                rf"\b(?:i[ \t]+)?estimate[ \t]+(?:a[ \t]+)?~?[ \t]*{number}"
                rf"[ \t]+probability\b[^.\n]{{0,140}}\b{target}\b",
                re.IGNORECASE,
            ),
        )
        for pattern in targeted_patterns:
            for match in pattern.finditer(normalized):
                value = _normalize_probability(match.group("number"))
                if value is not None:
                    candidates.append(
                        (match.end(), value, "strict_terminal_probability")
                    )

    substantive = [
        line.strip().strip("*_`$ ") for line in normalized.splitlines() if line.strip()
    ]
    if substantive:
        bare = re.fullmatch(
            r"(?:=\s*)?([0-9]*\.?[0-9]+)\s*(\\?%)?\.?", substantive[-1]
        )
        if bare:
            value = _normalize_probability(bare.group(1) + (bare.group(2) or ""))
            if value is not None:
                candidates.append((len(text), value, "strict_terminal_probability"))
    return candidates


def _explicit_choice_candidates(
    family: str, parser: str, text: str, target_player: str | None
) -> list[tuple[int, float, str]]:
    candidates: list[tuple[int, float, str]] = []
    # Preserve original offsets: later ambiguity guards compare candidate
    # positions against the unnormalized response.
    normalized = text.replace("*", " ").replace("_", " ").replace("`", " ")

    for match in re.finditer(r"\{[^{}]*\}", text, flags=re.DOTALL):
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key in ("choice", "answer", "final_answer", "decision"):
            if key not in parsed:
                continue
            value, _ = _choice_payload_value(family, parser, parsed[key], target_player)
            if value is not None:
                candidates.append((match.end(), value, "strict_json_choice"))

    keyed = re.compile(
        r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?"
        r"(?P<label>choice|answer|final answer|best answer)[ \t]*"
        r"(?:is|[:=])[ \t]*(?P<payload>[^\n]*)$"
    )
    for match in keyed.finditer(normalized):
        payload = match.group("payload").strip()
        candidate_end = match.end()
        if not payload or re.fullmatch(r"<[^>]+>", payload):
            following = re.search(
                r"(?m)^[ \t]*(\S[^\n]*)$", normalized[match.end() :]
            )
            if following is not None:
                payload = following.group(1).strip()
                candidate_end += following.end()
        value, payload_kind = _choice_payload_value(
            family, parser, payload, target_player
        )
        if value is not None:
            method = (
                "strict_choice_letter"
                if payload_kind == "letter"
                else "strict_choice_text"
            )
            if match.group("label").lower() != "choice":
                method = (
                    "strict_answer_letter"
                    if payload_kind == "letter"
                    else "strict_answer_text"
                )
            candidates.append((candidate_end, value, method))

    max_letter = "E" if parser == "ordinal5" else "B"
    boxed = re.compile(
        rf"\\boxed\s*\{{\s*(?:\\text\s*\{{\s*)?(?:choice\s*:?[ ]*)?"
        rf"\(?([A-{max_letter}])\)?\s*\}}?\s*\}}",
        re.IGNORECASE,
    )
    candidates.extend(
        (match.end(), float(ord(match.group(1).upper()) - ord("A") + 1), "strict_boxed_choice")
        if parser == "ordinal5"
        else (match.end(), float(match.group(1).upper() == "A"), "strict_boxed_choice")
        for match in boxed.finditer(text)
    )

    prose_answer = re.compile(
        rf"(?i:\bthe\s+(?:(?:final|best|correct)\s+)?(?:answer|choice)\s+is\s+)"
        rf"(?i:(?:therefore\s+)?(?:choice\s+)?)\(?([A-{max_letter}])\)?\b"
    )
    candidates.extend(
        (match.end(), float(ord(match.group(1).upper()) - ord("A") + 1), "strict_answer_letter")
        if parser == "ordinal5"
        else (match.end(), float(match.group(1).upper() == "A"), "strict_answer_letter")
        for match in prose_answer.finditer(normalized)
    )

    lines = list(re.finditer(r"(?m)^.*$", normalized))
    substantive = [line for line in lines if line.group(0).strip()]
    terminal_lines = substantive[-12:]
    for index, match in enumerate(terminal_lines):
        bare = re.fullmatch(
            rf"\s*\(?([A-{max_letter}])\)?(?:[.)])?\s*",
            match.group(0),
            flags=re.IGNORECASE,
        )
        if bare:
            value = (
                float(ord(bare.group(1).upper()) - ord("A") + 1)
                if parser == "ordinal5"
                else float(bare.group(1).upper() == "A")
            )
            candidates.append((match.end(), value, "strict_terminal_choice"))
            continue
        value = _terminal_labeled_option_value(family, parser, match.group(0), target_player)
        if value is None:
            continue
        if re.search(
            r"\b(?:wait[,]?\s+no|i\s+(?:made|have\s+made)\s+(?:a\s+)?mistake|"
            r"that\s+(?:was|is)\s+(?:incorrect|wrong)|i\s+reconsider)\b",
            normalized[match.end() :],
            re.IGNORECASE,
        ):
            continue
        # Do not mistake a prompt-echoed option catalogue for an answer.  A
        # genuine response contains a single final labeled option, whereas a
        # catalogue has another labeled option immediately before or after.
        neighbors = terminal_lines[max(0, index - 1) : index] + terminal_lines[
            index + 1 : index + 2
        ]
        if any(
            _terminal_labeled_option_value(family, parser, neighbor.group(0), target_player)
            is not None
            for neighbor in neighbors
        ):
            continue
        candidates.append((match.end(), value, "strict_terminal_choice"))
    return candidates


def _unresolved_revision_after_choice(
    text: str, candidate_end: int, *, truncated: bool = False
) -> bool:
    """Reject a late answer draft followed by a truncated reconsideration.

    Very long max-token generations occasionally state "the answer is A" late
    in their reasoning, then reopen the decision for thousands of characters
    and stop mid-sentence.  Treating that intermediate draft as final is the
    categorical analogue of accepting an unresolved probability draft.
    """
    trailing = text[candidate_end:]
    if candidate_end < 0.5 * len(text) or len(trailing) < 500:
        return False
    if not truncated and text.rstrip().endswith(
        (".", "!", "?", "}", "]", ")", "\"", "'")
    ):
        return False
    revision_cues = re.findall(
        r"\b(?:however|wait|reconsider\w*|another\s+interpretation|"
        r"self-correction|counter-?point|actually|on\s+the\s+other\s+hand)\b",
        trailing,
        re.IGNORECASE,
    )
    return len(revision_cues) >= 2


def parse_response(
    family: str,
    text: str,
    *,
    target_player: str | None = None,
    truncated: bool = False,
) -> tuple[float | None, str | None]:
    """Strictly parse one response to ``family``.

    Returns ``(value, parse_method)``, or ``(None, None)`` when the response
    has no usable explicit answer.  Values are probabilities in [0, 1],
    ordinal codes 1-5, or binary decisions (1 = option A).  ``truncated``
    marks a generation that stopped at the token limit.
    """
    parser = parser_kind(family)
    if parser == "probability":
        explicit = _explicit_probability_candidates(text, target_player)
        if explicit:
            answer_drafts = list(
                re.finditer(
                    r"(?im)^[ \t]*(?:probability|estimate)[ \t]*[:=][ \t]*"
                    r"([0-9]*\.?[0-9]+)[ \t]*(%)?",
                    text,
                )
            )
            final_draft_end = answer_drafts[-1].end() if answer_drafts else -1
            final_draft_line_end = (
                text.find("\n", final_draft_end) if final_draft_end >= 0 else -1
            )
            if final_draft_line_end < 0:
                final_draft_line_end = len(text)
            else:
                final_draft_line_end += 1
            later_resolution = [
                row
                for row in explicit
                # A terminal parser often emits a second candidate for the
                # same keyed line with an end position two characters later
                # (the punctuation).  Only a candidate on a later line can
                # resolve conflicting answer drafts.
                if row[0] > final_draft_line_end
                and row[2]
                in {
                    "strict_json_probability",
                    "strict_boxed_probability",
                    "strict_terminal_probability",
                }
            ]
            if (
                len(
                    {
                        match.group(1) + (match.group(2) or "")
                        for match in answer_drafts
                    }
                )
                > 1
                and final_draft_end < 0.75 * len(text)
                and not later_resolution
            ):
                return None, None
            # The requested structured/keyed value outranks looser terminal
            # formatting, regardless of later numbered reasoning text.
            for preferred_method in (
                "strict_json_probability",
                "strict_keyed_probability",
                "strict_boxed_probability",
                "strict_terminal_probability",
            ):
                preferred = [row for row in explicit if row[2] == preferred_method]
                if preferred:
                    _, value, method = max(preferred, key=lambda item: item[0])
                    return value, method
        return None, None

    if explicit_answer_has_conflicting_option(
        text, family, ordinal=parser == "ordinal5"
    ):
        return None, None
    if parser == "decision" and not _is_bet(family) and (
        explicit_answer_has_wrong_werewolf_target(text, target_player)
    ):
        return None, None
    explicit = _explicit_choice_candidates(family, parser, text, target_player)
    if explicit:
        candidate_end, value, method = max(explicit, key=lambda item: item[0])
        if _unresolved_revision_after_choice(
            text, candidate_end, truncated=truncated
        ):
            later_commitments = [
                candidate
                for candidate in commitment_candidates(text, family, target_player)
                if candidate[0] > candidate_end
            ]
            # A later, guarded commitment can resolve the reopened draft, but
            # only when its final direction agrees with the explicit answer.
            if not later_commitments or max(later_commitments)[1] != value:
                return None, None
        return value, method
    return None, None


# Command-line entry point


def read_jsonl(path: Path):
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


def load_tasks(prompts_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Task list (canonical order) and every prompt row keyed by prompt ID."""
    tasks = json.loads((prompts_dir / "tasks.json").read_text())
    prompts: dict[str, dict[str, Any]] = {}
    for task in tasks:
        for row in read_jsonl(prompts_dir / task["path"]):
            prompt_id = str(row["prompt_id"])
            if prompt_id in prompts:
                raise ValueError(f"Duplicate prompt ID: {prompt_id}")
            prompts[prompt_id] = row
    return tasks, prompts


def parse_responses(
    responses_dir: Path, prompts_dir: Path, models: tuple[str, ...] = MODELS
) -> tuple[list[dict[str, Any]], list[str], dict[tuple[str, str], Counter]]:
    tasks, prompts = load_tasks(prompts_dir)
    parsed_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    diagnostics: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for task in tasks:
        family = str(task["output"])
        for model in sorted(models):
            path = responses_dir / str(task["task_id"]) / f"{model}.jsonl"
            for raw in read_jsonl(path):
                prompt_id = str(raw.get("prompt_id", ""))
                prompt = prompts.get(prompt_id)
                if prompt is None:
                    raise ValueError(f"Unknown prompt ID in {path}: {prompt_id}")
                model_alias = str(raw["model_alias"])
                identity = (model_alias, prompt_id)
                if identity in seen:
                    raise ValueError(f"Duplicate model/prompt response: {identity}")
                seen.add(identity)
                raw_ok = bool(raw.get("ok"))
                output_text = str(raw.get("output_text") or "")
                truncated = str(raw.get("stop_reason") or "").lower() == "max_tokens"
                if raw_ok:
                    value, parse_method = parse_response(
                        family,
                        output_text,
                        target_player=prompt.get("target_player"),
                        truncated=truncated,
                    )
                else:
                    value, parse_method = None, None
                parse_ok = value is not None
                usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
                parsed_rows.append(
                    {
                        "model_alias": model_alias,
                        "model_id": raw.get("model_id"),
                        "source": raw.get("source"),
                        **{column: prompt.get(column) for column in CASE_COLUMNS},
                        "prompt_family": family,
                        "prompt_id": prompt_id,
                        "implied_probability_threshold": prompt.get(
                            "implied_probability_threshold"
                        ),
                        "raw_ok": int(raw_ok),
                        "parse_ok": int(parse_ok),
                        "parse_method": parse_method,
                        "parsed_value": value,
                        "stop_reason": raw.get("stop_reason"),
                        "input_tokens": usage.get("inputTokens", usage.get("input_tokens")),
                        "output_tokens": usage.get(
                            "outputTokens", usage.get("output_tokens")
                        ),
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
                diag = diagnostics[(model_alias, family)]
                diag["raw_rows"] += 1
                diag["raw_ok_rows"] += int(raw_ok)
                diag["parsed_rows"] += int(parse_ok)
                diag["parse_failures"] += int(raw_ok and not parse_ok)
                diag["raw_error_rows"] += int(not raw_ok)
    parsed_rows.sort(
        key=lambda row: (str(row["model_alias"]), str(row["case_id"]), str(row["prompt_family"]))
    )
    if not parsed_rows:
        raise RuntimeError(f"No responses found under {responses_dir}")
    family_order = [str(task["output"]) for task in tasks]
    return parsed_rows, family_order, diagnostics


def paired_table(
    parsed_rows: list[dict[str, Any]], family_order: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """One row per model x case with every family's value and parse status."""
    base_fields = ["model_alias", "model_id", "source", *CASE_COLUMNS]
    paired: dict[tuple[str, str], dict[str, Any]] = {}
    for row in parsed_rows:
        key = (str(row["model_alias"]), str(row["case_id"]))
        if key not in paired:
            paired[key] = {field: row.get(field) for field in base_fields}
        prefix = str(row["prompt_family"])
        paired[key].update(
            {
                f"{prefix}__prompt_id": row["prompt_id"],
                f"{prefix}__value": row["parsed_value"],
                f"{prefix}__parse_method": row["parse_method"],
                f"{prefix}__parse_ok": row["parse_ok"],
                f"{prefix}__raw_ok": row["raw_ok"],
            }
        )
    wide_fields = list(base_fields)
    for family in family_order:
        wide_fields.extend(
            f"{family}__{suffix}"
            for suffix in ("prompt_id", "value", "parse_method", "parse_ok", "raw_ok")
        )
    return [paired[key] for key in sorted(paired)], wide_fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--responses-dir", type=Path, default=RESPONSES_DIR / "werewolf")
    parser.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR / "werewolf")
    parser.add_argument("--output-dir", type=Path, default=PARSED_DIR / "werewolf")
    args = parser.parse_args()
    responses_dir = args.responses_dir.resolve()
    output_dir = ensure(args.output_dir.resolve())

    parsed_rows, family_order, diagnostics = parse_responses(
        responses_dir, args.prompts_dir.resolve()
    )
    write_csv(output_dir / "response_values.csv", parsed_rows, list(parsed_rows[0]))
    paired_rows, wide_fields = paired_table(parsed_rows, family_order)
    write_csv(output_dir / "paired_responses.csv", paired_rows, wide_fields)

    diag_rows = [
        {"model_alias": model, "prompt_family": family, **counts}
        for (model, family), counts in sorted(diagnostics.items())
    ]
    diag_fields = [
        "model_alias",
        "prompt_family",
        "raw_rows",
        "raw_ok_rows",
        "parsed_rows",
        "parse_failures",
        "raw_error_rows",
    ]
    write_csv(output_dir / "parse_diagnostics.csv", diag_rows, diag_fields)

    print(f"Wrote parsed Werewolf responses to {output_dir}")


if __name__ == "__main__":
    main()
