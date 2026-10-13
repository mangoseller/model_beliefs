"""Batch records shared by the Bedrock and OpenAI collection stages.

Every prompt becomes one record whose ID is a hash of its task and prompt IDs,
so the same record ID identifies a prompt across providers and models.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from latent_beliefs.config import PROMPTS_DIR

# Batch name for each domain; it appears in OpenAI request IDs and in Bedrock
# job names and S3 keys.
WORKLOADS = {
    "ckd": "ckd500_temp1",
    "werewolf": "werewolf500_temp1",
    "forecasting": "forecasting500_temp1",
}
CASES_PER_TASK = 500
TASKS_PER_DOMAIN = 16

# Generation settings shared by all models.
TEMPERATURE = 1.0
MAX_TOKENS = 4096


def read_jsonl(path: Path, errors: str = "strict") -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors=errors) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_task_rows(domain: str, prompts_dir: Path = PROMPTS_DIR) -> list[dict[str, Any]]:
    """All prompts of a domain, in task order, with their batch record IDs."""
    domain_dir = prompts_dir / domain
    tasks = json.loads((domain_dir / "tasks.json").read_text())
    if len(tasks) != TASKS_PER_DOMAIN:
        raise ValueError(f"{domain}: expected {TASKS_PER_DOMAIN} tasks, found {len(tasks)}")
    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task["task_id"])
        prompts = list(read_jsonl(domain_dir / task["path"]))
        if len(prompts) != CASES_PER_TASK:
            raise ValueError(f"{task_id}: expected {CASES_PER_TASK}, found {len(prompts)}")
        for prompt in prompts:
            prompt_id = str(prompt["prompt_id"])
            sample_key = f"{task_id}:{prompt_id}"
            rows.append(
                {
                    "recordId": hashlib.sha256(sample_key.encode("utf-8")).hexdigest()[:32],
                    "task_id": task_id,
                    "prompt_id": prompt_id,
                    "sample_key": sample_key,
                    "prompt": str(prompt["prompt"]),
                }
            )
    record_ids = [row["recordId"] for row in rows]
    if len(record_ids) != len(set(record_ids)):
        raise ValueError(f"{domain}: duplicate record IDs")
    return rows


def write_record_map(rows: list[dict[str, Any]], path: Path) -> None:
    """Record ID -> task, prompt, and sample key (everything except the prompt text)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as mapping:
        for row in rows:
            mapping.write(
                json.dumps({key: value for key, value in row.items() if key != "prompt"})
                + "\n"
            )


def read_record_map(path: Path) -> dict[str, dict[str, Any]]:
    return {str(row["recordId"]): row for row in read_jsonl(path, errors="replace")}
