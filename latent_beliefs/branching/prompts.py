"""First-stage reasoning prompts and follow-up prompts of the branching experiments.

Each domain's 16 prompts share one case context (patient profile, Werewolf
transcript, or study materials) followed by output-specific task text.  For
every selected case the context is split from the task text:

* the first-stage prompt is a fixed domain-specific framing around the context,
  ending in "How likely is ...?  Provide your reasoning below." (Appendix,
  "Reasoning-prefix branching experiments");
* the follow-up prompt of each output is its task text alone, asked after the
  replayed first-stage answer.

Usage::

    python -m latent_beliefs.branching.prompts   # -> PROMPTS_DIR/branching/cases.jsonl

Each line of ``cases.jsonl`` holds ``domain``, ``instance_id``,
``initial_prompt`` and ``followups`` (in collection task order:
``prompt_variant``, ``task_id``, ``prompt_id``, ``prompt``).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from latent_beliefs.branching.design import CASES_PATH, SELECTION_PATH, load_selection, read_jsonl, write_jsonl
from latent_beliefs.config import DOMAINS, PROMPTS_DIR

TASKS_PER_DOMAIN = 16


def load_catalog(domain: str, prompts_dir: Path) -> list[dict[str, Any]]:
    """The domain's tasks in collection order, each with its prompts keyed by case."""
    domain_dir = prompts_dir / domain
    tasks = json.loads((domain_dir / "tasks.json").read_text(encoding="utf-8"))
    if len(tasks) != TASKS_PER_DOMAIN:
        raise ValueError(f"{domain}: expected {TASKS_PER_DOMAIN} tasks, found {len(tasks)}")
    catalog = []
    for task in tasks:
        task_id = str(task["task_id"])
        records: dict[str, dict[str, Any]] = {}
        for record in read_jsonl(domain_dir / task["path"]):
            case = str(record["prompt_id"]).rsplit("__", 1)[0]
            if case in records:
                raise ValueError(f"{task_id}: duplicate case {case}")
            records[case] = record
        catalog.append(
            {"task_id": task_id, "prompt_variant": str(task["output"]), "records": records}
        )
    return catalog


def split_ckd_prompts(prompts: dict[str, str]) -> tuple[str, dict[str, str]]:
    marker = "\nPatient profile:\n"
    contexts = []
    followups = {}
    for variant, prompt in prompts.items():
        task, context = prompt.split(marker, 1)
        contexts.append(context.strip())
        followups[variant] = task.strip()
    if len(set(contexts)) != 1:
        raise ValueError("CKD task prompts do not share identical patient context")
    initial = (
        "Below is a profile of a patient.\n\n"
        f"Patient profile:\n{contexts[0]}\n\n"
        "How likely is it that this patient has chronic kidney disease (CKD)? "
        "Provide your reasoning below."
    )
    return initial, followups


def split_werewolf_prompts(
    prompts: dict[str, str], *, target_player: str
) -> tuple[str, dict[str, str]]:
    marker = "\nGAME STATE AT THE CUT\n"
    contexts = []
    followups = {}
    for variant, prompt in prompts.items():
        task_with_intro, context = prompt.split(marker, 1)
        contexts.append(context.strip())
        paragraphs = task_with_intro.split("\n\n", 1)
        if len(paragraphs) != 2:
            raise ValueError(f"Werewolf prompt {variant} has no context-setting paragraph")
        followups[variant] = paragraphs[1].strip()
    if len(set(contexts)) != 1:
        raise ValueError("Werewolf task prompts do not share identical transcript context")
    initial = (
        "Below is the transcript of a game of Werewolf up to a specific turn. "
        "You are being shown all of the information observable to a focal player.\n\n"
        f"GAME STATE AT THE CUT\n{contexts[0]}\n\n"
        f'How likely is it that player "{target_player}" is a Werewolf? '
        "Provide your reasoning below."
    )
    return initial, followups


def split_forecasting_prompts(prompts: dict[str, str]) -> tuple[str, dict[str, str]]:
    # The study materials are the prompts' common prefix, minus the opening
    # framing paragraph.
    common = os.path.commonprefix(list(prompts.values()))
    boundary = common.rfind("\n\n")
    if boundary < 0:
        raise ValueError("Forecasting prompts have no shared context boundary")
    context = common[:boundary].strip()
    followups = {variant: prompt[boundary:].strip() for variant, prompt in prompts.items()}
    paragraphs = context.split("\n\n")
    if len(paragraphs) < 3:
        raise ValueError("Forecasting shared context is unexpectedly short")
    context = "\n\n".join(paragraphs[1:])
    initial = (
        "Below are materials from a randomized social-science survey experiment comparing "
        "Condition A with Condition B. Use only the provided study materials.\n\n"
        f"{context}\n\n"
        "How likely is it that the true population mean outcome under Condition A is strictly "
        "greater than the true population mean outcome under Condition B? Provide your reasoning below."
    )
    return initial, followups


def build_cases(selection_path: Path, prompts_dir: Path) -> list[dict[str, Any]]:
    selected = load_selection(selection_path)
    cases = []
    for domain in DOMAINS:
        catalog = load_catalog(domain, prompts_dir)
        for row in selected.loc[selected["domain"].eq(domain)].to_dict("records"):
            instance_id = str(row["instance_id"])
            full_prompts: dict[str, str] = {}
            for task in catalog:
                if instance_id not in task["records"]:
                    raise KeyError(f"{domain}/{task['task_id']}: missing {instance_id}")
                full_prompts[task["prompt_variant"]] = str(task["records"][instance_id]["prompt"])
            if domain == "ckd":
                initial, followups = split_ckd_prompts(full_prompts)
            elif domain == "werewolf":
                initial, followups = split_werewolf_prompts(
                    full_prompts, target_player=str(row["target_player"])
                )
            else:
                initial, followups = split_forecasting_prompts(full_prompts)
            if "Initial probability:" in initial or "do not state" in initial.lower():
                raise ValueError(f"{domain}/{instance_id}: first-stage prompt anchors or suppresses an answer")
            cases.append(
                {
                    "domain": domain,
                    "instance_id": instance_id,
                    "initial_prompt": initial,
                    "followups": [
                        {
                            "prompt_variant": task["prompt_variant"],
                            "task_id": task["task_id"],
                            "prompt_id": str(task["records"][instance_id]["prompt_id"]),
                            "prompt": followups[task["prompt_variant"]],
                        }
                        for task in catalog
                    ],
                }
            )
    return cases


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return list(read_jsonl(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR)
    parser.add_argument("--out", type=Path, default=CASES_PATH)
    args = parser.parse_args()
    count = write_jsonl(args.out, build_cases(args.selection, args.prompts_dir))
    print(f"Wrote {args.out} ({count} cases)")


if __name__ == "__main__":
    main()
