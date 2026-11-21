"""Collect responses from the two GPT-5.6 models via the OpenAI Batch API.

One batch per domain and model on the Responses endpoint: the prompt text as
input, medium reasoning effort, temperature 1, at most 4096 output tokens, and
``store`` disabled.  Files live under ``<collection>/<domain>/openai/``, where
``<short>`` is the model key without its ``openai_`` prefix:

    input_<short>.jsonl     batch input
    record_map.jsonl        record ID -> task_id, prompt_id
    jobs.json               uploaded file and batch IDs
    output_<short>.jsonl    downloaded results
    errors_<short>.jsonl    downloaded per-request errors, if any

Subcommands: ``prepare`` (writes the input files; no API access), ``submit``,
``status``, ``download``.  The API key is read from ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from latent_beliefs.collection.records import (
    MAX_TOKENS,
    TEMPERATURE,
    WORKLOADS,
    load_task_rows,
    write_record_map,
)
from latent_beliefs.config import COLLECTION_DIR, DOMAINS, PROMPTS_DIR
from latent_beliefs.models import OPENAI_MODEL_IDS

API_ROOT = "https://api.openai.com/v1"
ENDPOINT = "/v1/responses"
COMPLETION_WINDOW = "24h"
REASONING_EFFORT = "medium"


def short_name(model: str) -> str:
    return model.removeprefix("openai_")


def batch_dir(collection_dir: Path, domain: str) -> Path:
    return collection_dir / domain / "openai"


def request_record(domain: str, model_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "custom_id": f"{WORKLOADS[domain]}-{row['recordId']}",
        "method": "POST",
        "url": ENDPOINT,
        "body": {
            "model": model_id,
            "input": row["prompt"],
            "reasoning": {"effort": REASONING_EFFORT},
            "temperature": TEMPERATURE,
            "max_output_tokens": MAX_TOKENS,
            "store": False,
        },
    }


def prepare(domain: str, models: list[str], prompts_dir: Path, collection_dir: Path) -> None:
    rows = load_task_rows(domain, prompts_dir)
    out_dir = batch_dir(collection_dir, domain)
    write_record_map(rows, out_dir / "record_map.jsonl")
    for model in models:
        path = out_dir / f"input_{short_name(model)}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                record = request_record(domain, OPENAI_MODEL_IDS[model], row)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"{domain}/{model}: prepared {len(rows)} requests")


# ---------------------------------------------------------------- API
def api_headers() -> dict[str, str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is not set")
    return {"Authorization": f"Bearer {key}"}


def jobs_path(collection_dir: Path, domain: str) -> Path:
    return batch_dir(collection_dir, domain) / "jobs.json"


def load_jobs(collection_dir: Path, domain: str) -> dict[str, Any]:
    path = jobs_path(collection_dir, domain)
    if not path.exists():
        return {"workload": WORKLOADS[domain], "jobs": {}}
    return json.loads(path.read_text())


def save_jobs(collection_dir: Path, domain: str, payload: dict[str, Any]) -> None:
    jobs_path(collection_dir, domain).write_text(json.dumps(payload, indent=2) + "\n")


def submit(domain: str, models: list[str], collection_dir: Path) -> None:
    import requests

    headers = api_headers()
    payload = load_jobs(collection_dir, domain)
    for model in models:
        short = short_name(model)
        if payload["jobs"].get(short, {}).get("batch_id"):
            print(f"{domain}/{model}: already submitted")
            continue
        path = batch_dir(collection_dir, domain) / f"input_{short}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Run prepare first: {path}")
        with path.open("rb") as handle:
            upload = requests.post(
                f"{API_ROOT}/files",
                headers=headers,
                data={"purpose": "batch"},
                files={"file": (path.name, handle, "application/jsonl")},
                timeout=300,
            )
        upload.raise_for_status()
        file_id = upload.json()["id"]
        batch = requests.post(
            f"{API_ROOT}/batches",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "input_file_id": file_id,
                "endpoint": ENDPOINT,
                "completion_window": COMPLETION_WINDOW,
                "metadata": {"study": "latent_beliefs", "workload": WORKLOADS[domain], "model_alias": short},
            },
            timeout=60,
        )
        batch.raise_for_status()
        response = batch.json()
        payload["jobs"][short] = {
            "model": OPENAI_MODEL_IDS[model],
            "input_file_id": file_id,
            "batch_id": response["id"],
            "status": response["status"],
            "submitted_utc": datetime.now(timezone.utc).isoformat(),
        }
        save_jobs(collection_dir, domain, payload)
        print(f"{domain}/{model}: submitted {response['id']} ({response['status']})")


def status(domain: str, collection_dir: Path) -> None:
    import requests

    headers = api_headers()
    payload = load_jobs(collection_dir, domain)
    for short, job in sorted(payload["jobs"].items()):
        response = requests.get(f"{API_ROOT}/batches/{job['batch_id']}", headers=headers, timeout=60)
        response.raise_for_status()
        batch = response.json()
        job.update(
            {
                "status": batch["status"],
                "request_counts": batch.get("request_counts"),
                "output_file_id": batch.get("output_file_id"),
                "error_file_id": batch.get("error_file_id"),
                "checked_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        print(f"{domain}/{short}: {batch['status']} {json.dumps(batch.get('request_counts', {}), sort_keys=True)}")
    save_jobs(collection_dir, domain, payload)


def download(domain: str, collection_dir: Path) -> None:
    import requests

    headers = api_headers()
    payload = load_jobs(collection_dir, domain)
    for short, job in sorted(payload["jobs"].items()):
        for kind, file_id in (("output", job.get("output_file_id")), ("errors", job.get("error_file_id"))):
            if not file_id:
                continue
            response = requests.get(f"{API_ROOT}/files/{file_id}/content", headers=headers, timeout=300)
            response.raise_for_status()
            destination = batch_dir(collection_dir, domain) / f"{kind}_{short}.jsonl"
            destination.write_bytes(response.content)
            print(f"{domain}/{short}: downloaded {destination}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["prepare", "submit", "status", "download"])
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument(
        "--models", nargs="+", choices=sorted(OPENAI_MODEL_IDS), default=list(OPENAI_MODEL_IDS)
    )
    parser.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR)
    parser.add_argument("--collection-dir", type=Path, default=COLLECTION_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for domain in args.domains:
        if args.command == "prepare":
            prepare(domain, args.models, args.prompts_dir, args.collection_dir)
        elif args.command == "submit":
            submit(domain, args.models, args.collection_dir)
        elif args.command == "status":
            status(domain, args.collection_dir)
        elif args.command == "download":
            download(domain, args.collection_dir)


if __name__ == "__main__":
    main()
