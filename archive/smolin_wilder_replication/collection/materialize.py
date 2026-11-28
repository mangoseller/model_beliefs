"""Convert downloaded batch outputs into per-task response files.

Reads ``<collection>/<domain>/{bedrock,openai}/`` (see ``bedrock`` and
``openai``) and writes ``<out>/<domain>/<task_id>/<model>.jsonl``: one record
per prompt, sorted by prompt ID, with fields ``ok, task_id, prompt_id,
sample_id, request_id, model_alias, model_id, output_text, stop_reason, usage,
error, record_id, source``.  This is the layout of the released responses that
the parsing stage reads (``LB_RESPONSES_DIR``).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from archive.smolin_wilder_replication.collection.openai import short_name
from archive.smolin_wilder_replication.collection.records import read_jsonl, read_record_map
from archive.smolin_wilder_replication.config import COLLECTION_DIR, DOMAINS, MATERIALIZED_DIR
from archive.smolin_wilder_replication.models import BEDROCK_MODEL_IDS, MODELS, OPENAI_MODEL_IDS

Grouped = dict[tuple[str, str], list[dict[str, Any]]]


def bedrock_text(model_output: dict[str, Any]) -> str:
    content = model_output.get("output", {}).get("message", {}).get("content", [])
    return "\n".join(
        str(item.get("text"))
        for item in content
        if isinstance(item, dict) and item.get("text") is not None
    )


def openai_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in body.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "\n".join(part for part in parts if part)


def collect_bedrock(workdir: Path, model: str, grouped: Grouped) -> int:
    record_map = read_record_map(workdir / "record_map.jsonl")
    paths = sorted((workdir / "outputs" / model).glob("*.jsonl.out"))
    if not paths:
        raise FileNotFoundError(f"No Bedrock outputs for {model} in {workdir / 'outputs' / model}")
    unknown = 0
    for path in paths:
        for raw in read_jsonl(path, errors="replace"):
            record_id = str(raw.get("recordId", ""))
            mapped = record_map.get(record_id)
            if mapped is None:
                unknown += 1
                continue
            model_output = raw.get("modelOutput")
            error = raw.get("error")
            ok = isinstance(model_output, dict) and not error
            grouped[(str(mapped["task_id"]), model)].append(
                {
                    "ok": ok,
                    "task_id": mapped["task_id"],
                    "prompt_id": mapped["prompt_id"],
                    "sample_id": mapped["sample_key"],
                    "request_id": f"{mapped['sample_key']}:{model}",
                    "model_alias": model,
                    "model_id": BEDROCK_MODEL_IDS[model],
                    "output_text": bedrock_text(model_output) if ok else "",
                    "stop_reason": model_output.get("stopReason") if ok else None,
                    "usage": model_output.get("usage", {}) if ok else {},
                    "error": error,
                    "record_id": record_id,
                    "source": "bedrock_native_batch",
                }
            )
    return unknown


def collect_openai(workdir: Path, model: str, grouped: Grouped) -> int:
    record_map = read_record_map(workdir / "record_map.jsonl")
    short = short_name(model)
    output_path = workdir / f"output_{short}.jsonl"
    if not output_path.exists():
        raise FileNotFoundError(f"No OpenAI outputs for {model}: {output_path}")
    paths = [output_path]
    error_path = workdir / f"errors_{short}.jsonl"
    if error_path.exists():
        paths.append(error_path)
    unknown = 0
    for path in paths:
        for raw in read_jsonl(path, errors="replace"):
            record_id = str(raw.get("custom_id", "")).rsplit("-", 1)[-1]
            mapped = record_map.get(record_id)
            if mapped is None:
                unknown += 1
                continue
            response = raw.get("response") if isinstance(raw.get("response"), dict) else {}
            body = response.get("body") if isinstance(response.get("body"), dict) else {}
            error = body.get("error") or raw.get("error")
            ok = response.get("status_code") == 200 and not error
            stop_reason = body.get("status") if ok else None
            incomplete = body.get("incomplete_details")
            if isinstance(incomplete, dict) and incomplete.get("reason"):
                stop_reason = incomplete["reason"]
            grouped[(str(mapped["task_id"]), model)].append(
                {
                    "ok": ok,
                    "task_id": mapped["task_id"],
                    "prompt_id": mapped["prompt_id"],
                    "sample_id": mapped["sample_key"],
                    "request_id": response.get("request_id") or raw.get("id"),
                    "model_alias": model,
                    "model_id": OPENAI_MODEL_IDS[model],
                    "output_text": openai_text(body) if ok else "",
                    "stop_reason": stop_reason,
                    "usage": body.get("usage", {}) if ok else {},
                    "error": error,
                    "record_id": record_id,
                    "source": "openai_responses_batch",
                }
            )
    return unknown


def write_grouped(grouped: Grouped, destination: Path) -> None:
    for (task_id, model), rows in sorted(grouped.items()):
        out_dir = destination / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        # A re-run record replaces an earlier one for the same prompt.
        dedup = {str(row["prompt_id"]): row for row in rows}
        with (out_dir / f"{model}.jsonl").open("w", encoding="utf-8") as handle:
            for row in sorted(dedup.values(), key=lambda r: str(r["prompt_id"])):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        n_ok = sum(bool(row["ok"]) for row in dedup.values())
        print(f"  {task_id}/{model}: {len(dedup)} records, {n_ok} ok")


def materialize(domain: str, models: Iterable[str], collection_dir: Path, out_dir: Path) -> None:
    grouped: Grouped = defaultdict(list)
    for model in models:
        if model in BEDROCK_MODEL_IDS:
            unknown = collect_bedrock(collection_dir / domain / "bedrock", model, grouped)
        else:
            unknown = collect_openai(collection_dir / domain / "openai", model, grouped)
        if unknown:
            print(f"{domain}/{model}: skipped {unknown} records not in the record map")
    print(f"{domain}: writing {len(grouped)} task/model files")
    write_grouped(grouped, out_dir / domain)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--collection-dir", type=Path, default=COLLECTION_DIR)
    parser.add_argument("--out", type=Path, default=MATERIALIZED_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for domain in args.domains:
        materialize(domain, args.models, args.collection_dir, args.out)


if __name__ == "__main__":
    main()
