"""Strictly parse the branching follow-up responses with the domain parsers.

Usage::

    python -m archive.smolin_wilder_replication.branching.parse

Inputs: released responses ``RESPONSES_DIR/branching/<experiment>/<stage>/<model>.jsonl``
(see ``materialize``) and the case selection ``assets/branching/selected_instances.csv``.
Outputs (``PARSED_DIR/branching``):

``behavior/response_values.csv``         one row per model x prefix x output
    (parsed_value: probability, 1/0 decision, or 1-5 ordinal code; blank if unparsed);
``probability/probability_responses.csv``  one row per model x prefix x answer
    (column ``probability``), with the response text.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from archive.smolin_wilder_replication.branching.design import (
    BRANCHING_PARSED_DIR,
    BRANCHING_RESPONSES_DIR,
    CASES_PER_DOMAIN,
    EXPERIMENTS,
    MODELS,
    SELECTION_PATH,
    load_selection,
    content_hash,
    read_jsonl,
    response_path,
)
from archive.smolin_wilder_replication.ckd import parse as ckd_parse
from archive.smolin_wilder_replication.config import DOMAINS
from archive.smolin_wilder_replication.forecasting import parse as forecasting_parse
from archive.smolin_wilder_replication.werewolf import parse as werewolf_parse

TASKS_PER_DOMAIN = 16


def choice_letter(text: str) -> str | None:
    """Last ``Choice: X`` line (or final lettered option line), recorded alongside the value."""
    normalized = text.replace("*", "").replace("_", "").replace("`", "")
    matches = re.findall(r"^\s*Choice\s*:\s*([A-E])(?:[.)]|\s|$)", normalized, re.I | re.M)
    if matches:
        return matches[-1].upper()
    for line in reversed([line.strip() for line in normalized.splitlines()[-12:] if line.strip()]):
        match = re.match(r"^([A-E])[.)](?:\s|$)", line, re.I)
        if match:
            return match.group(1).upper()
    return None


def ckd_parser_name(variant: str) -> str:
    if variant == "probability":
        return "probability"
    if variant in {"likert", "probability_bin_quintile"}:
        return "ordinal5"
    return "decision"


def parse_response(
    domain: str,
    variant: str,
    text: str,
    *,
    target_player: str | None = None,
    truncated: bool = False,
) -> tuple[float | None, str | None, str | None]:
    """Return (value, parse_method, choice letter) for one follow-up response."""
    if domain == "ckd":
        value, _ = ckd_parse.parse_response(variant, text)
        return (
            float(value) if value is not None else None,
            "strict_" + ckd_parser_name(variant) if value is not None else None,
            choice_letter(text) if value is not None else None,
        )
    if domain == "werewolf":
        value, method = werewolf_parse.parse_response(
            variant, text, target_player=target_player, truncated=truncated
        )
        return (
            float(value) if value is not None else None,
            method if value is not None else None,
            choice_letter(text),
        )
    parsed = forecasting_parse.parse_output(variant, text)
    return parsed["value"], parsed["method"], parsed["choice"]


def usage_fields(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "total_tokens": usage.get("totalTokens"),
    }


def error_text(row: dict[str, Any]) -> str | None:
    return json.dumps(row["error"], ensure_ascii=False) if row.get("error") else None


def prefix_hashes(responses_dir: Path, experiment: str, model: str) -> dict[str, str]:
    """First-stage record ID -> hash of the replayed assistant content."""
    hashes = {}
    for row in read_jsonl(response_path(responses_dir, experiment, "initial", model)):
        if not row["ok"]:
            raise RuntimeError(f"{model}: failed first-stage response {row['record_id']}")
        hashes[row["record_id"]] = content_hash(row["output_text"])
    return hashes


# Behavior
BEHAVIOR_FIELDS = [
    "model_alias",
    "model_id",
    "domain",
    "instance_id",
    "replicate",
    "branch_id",
    "selection_order",
    "target_player",
    "initial_record_id",
    "initial_content_sha256",
    "prompt_variant",
    "task_id",
    "prompt_id",
    "record_id",
    "raw_ok",
    "parse_ok",
    "parse_method",
    "parsed_value",
    "parsed_choice",
    "stop_reason",
    "truncated",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "response_characters",
    "error",
]


def parse_behavior(responses_dir: Path, selection_path: Path) -> list[dict[str, Any]]:
    experiment = EXPERIMENTS["behavior"]
    selection = {
        (str(row["domain"]), str(row["instance_id"])): {
            "selection_order": row["selection_order"],
            "target_player": row.get("target_player"),
        }
        for row in load_selection(selection_path).to_dict("records")
    }
    rows: list[dict[str, Any]] = []
    for model in MODELS:
        hashes = prefix_hashes(responses_dir, experiment.key, model)
        for response in read_jsonl(response_path(responses_dir, experiment.key, "followups", model)):
            domain = response["domain"]
            selected = selection[(domain, response["instance_id"])]
            truncated = str(response["stop_reason"]).lower() == "max_tokens"
            text = response["output_text"].strip()
            if response["ok"]:
                value, method, choice = parse_response(
                    domain,
                    response["prompt_variant"],
                    text,
                    target_player=str(selected["target_player"]) if domain == "werewolf" else None,
                    truncated=truncated,
                )
            else:
                value = method = choice = None
            rows.append(
                {
                    "model_alias": model,
                    "model_id": response["model_id"],
                    "domain": domain,
                    "instance_id": response["instance_id"],
                    "replicate": response["replicate"],
                    "branch_id": response["branch_id"],
                    **selected,
                    "initial_record_id": response["initial_record_id"],
                    "initial_content_sha256": hashes[response["initial_record_id"]],
                    "prompt_variant": response["prompt_variant"],
                    "task_id": response["task_id"],
                    "prompt_id": response["prompt_id"],
                    "record_id": response["record_id"],
                    "raw_ok": int(response["ok"]),
                    "parse_ok": int(value is not None),
                    "parse_method": method,
                    "parsed_value": value,
                    "parsed_choice": choice,
                    "stop_reason": response["stop_reason"],
                    "truncated": int(truncated),
                    **usage_fields(response["usage"]),
                    "response_characters": len(text),
                    "error": error_text(response),
                }
            )
    expected = len(MODELS) * len(DOMAINS) * CASES_PER_DOMAIN * experiment.prefixes * TASKS_PER_DOMAIN
    if len(rows) != expected:
        raise ValueError(f"behavior: expected {expected} follow-up responses, found {len(rows)}")
    rows.sort(
        key=lambda row: (
            row["model_alias"],
            row["domain"],
            int(row["selection_order"]),
            int(row["replicate"]),
            row["prompt_variant"],
        )
    )
    return rows


# Probability
def parse_probability(responses_dir: Path) -> pd.DataFrame:
    experiment = EXPERIMENTS["probability"]
    rows = []
    for model in MODELS:
        hashes = prefix_hashes(responses_dir, experiment.key, model)
        for response in read_jsonl(response_path(responses_dir, experiment.key, "followups", model)):
            text = response["output_text"].strip()
            if response["ok"]:
                # Parsed without the Werewolf target name or truncation flag.
                value, method, _ = parse_response(response["domain"], response["prompt_variant"], text)
            else:
                value = method = None
            rows.append(
                {
                    "model_alias": model,
                    "model_id": response["model_id"],
                    "domain": response["domain"],
                    "instance_id": response["instance_id"],
                    "prefix_replicate": response["prefix_replicate"],
                    "prefix_id": response["prefix_id"],
                    "probability_replicate": response["probability_replicate"],
                    "probability_branch_id": response["probability_branch_id"],
                    "initial_record_id": response["initial_record_id"],
                    "prefix_content_sha256": hashes[response["initial_record_id"]],
                    "prompt_variant": response["prompt_variant"],
                    "task_id": response["task_id"],
                    "prompt_id": response["prompt_id"],
                    "record_id": response["record_id"],
                    "raw_ok": int(response["ok"]),
                    "parse_ok": int(value is not None),
                    "parse_method": method,
                    "probability": value,
                    "stop_reason": response["stop_reason"],
                    "truncated": int(str(response["stop_reason"]).lower() == "max_tokens"),
                    **usage_fields(response["usage"]),
                    "response_characters": len(text),
                    "output_text": text,
                    "error": error_text(response),
                }
            )
    expected = (
        len(MODELS) * len(DOMAINS) * CASES_PER_DOMAIN * experiment.prefixes * experiment.answers_per_prefix
    )
    if len(rows) != expected:
        raise ValueError(f"probability: expected {expected} responses, found {len(rows)}")
    return pd.DataFrame(rows).sort_values(
        ["model_alias", "domain", "instance_id", "prefix_replicate", "probability_replicate"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--responses-dir", type=Path, default=BRANCHING_RESPONSES_DIR)
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--out-dir", type=Path, default=BRANCHING_PARSED_DIR)
    args = parser.parse_args()

    behavior = parse_behavior(args.responses_dir, args.selection)
    path = args.out_dir / "behavior" / "response_values.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=BEHAVIOR_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(behavior)
    print(f"behavior: {sum(row['parse_ok'] for row in behavior)}/{len(behavior)} parsed -> {path}")

    probability = parse_probability(args.responses_dir)
    path = args.out_dir / "probability" / "probability_responses.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    probability.to_csv(path, index=False)
    print(f"probability: {int(probability['parse_ok'].sum())}/{len(probability)} parsed -> {path}")


if __name__ == "__main__":
    main()
