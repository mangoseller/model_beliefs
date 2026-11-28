"""Render the 16 scientific-forecasting prompts for each of the 500 cases.

Each prompt is the case's study materials followed by one framing block from
``assets/forecasting/framing_variants.md`` (the prompt catalog in the Appendix):
one probability, one unparameterized decision, four threshold decisions, four
bets, four asymmetric-cost decisions, a Likert judgment, and a probability
quintile.  There is no system prompt.

Outputs in ``PROMPTS_DIR/forecasting``: ``<task_id>.jsonl`` (one row per case, in
case order) and ``tasks.json`` (task order used for collection).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from archive.smolin_wilder_replication.config import ASSETS_DIR, PROMPTS_DIR, SOURCES_DIR, ensure

FRAMING_DOC = ASSETS_DIR / "forecasting" / "framing_variants.md"

# Output names (= task IDs) in collection order.
TASK_IDS: tuple[str, ...] = (
    "probability_positive",
    "experiment_priority_no_threshold",
    "experiment_priority_threshold_05pct",
    "experiment_priority_threshold_10pct",
    "experiment_priority_threshold_20pct",
    "experiment_priority_threshold_33pct",
    "bet_payoff_2to1",
    "bet_payoff_4to1",
    "bet_payoff_9to1",
    "bet_payoff_19to1",
    "experiment_priority_cost_fn2",
    "experiment_priority_cost_fn4",
    "experiment_priority_cost_fn9",
    "experiment_priority_cost_fn19",
    "likert",
    "probability_bin_quintile",
)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def parse_framings(path: Path = FRAMING_DOC) -> dict[str, str]:
    matches = re.findall(
        r"Variant ID: `([^`]+)`\n\n```text\n(.*?)\n```",
        path.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    if [variant_id for variant_id, _ in matches] != list(TASK_IDS):
        raise RuntimeError(f"Framing blocks do not match the task list: {[m[0] for m in matches]}")
    return dict(matches)


def primary_materials(row: dict) -> str:
    return f"""You are forecasting the result of a randomized social-science survey experiment. Use only the study materials below; do not search for the study or use external tools.

Participants were randomly assigned to Condition A or Condition B. The outcome is a numeric survey response named: {row['outcome']}. The response scale and its direction are stated in the transcripts.

CONDITION A TRANSCRIPT
{row['condition_a_material']}

CONDITION B TRANSCRIPT
{row['condition_b_material']}"""


def megastudy_materials(row: dict) -> str:
    return f"""You are forecasting the result of a randomized social-science experiment. Use only the study materials below; do not search for the study or use external tools.

STUDY SETTING
{row['study_setting']}

OUTCOME MEASURE
{row['outcome_measure']}

CONDITION A
Name: {row['condition_a']}
{row['condition_a_material']}

CONDITION B
Name: {row['condition_b']}
{row['condition_b_material']}"""


def implied_threshold(task_id: str) -> float | None:
    """Probability threshold at which a risk-neutral agent switches to A."""
    explicit = {
        "experiment_priority_threshold_05pct": 0.05,
        "experiment_priority_threshold_10pct": 0.10,
        "experiment_priority_threshold_20pct": 0.20,
        "experiment_priority_threshold_33pct": 1 / 3,
    }
    if task_id in explicit:
        return explicit[task_id]
    match = re.search(r"(?:bet_payoff_|cost_fn)(\d+)(?:to1)?$", task_id)
    return 1 / (int(match.group(1)) + 1) if match else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenarios", type=Path, default=SOURCES_DIR / "forecasting" / "scenarios.jsonl")
    parser.add_argument("--out-dir", type=Path, default=PROMPTS_DIR / "forecasting")
    args = parser.parse_args()
    out_dir = ensure(args.out_dir)

    scenarios = read_jsonl(args.scenarios)
    for row in scenarios:
        row["materials"] = (
            primary_materials(row) if row["archive"] == "primary" else megastudy_materials(row)
        )
    framings = parse_framings()

    tasks = []
    for task_id in TASK_IDS:
        rows = [
            {
                "prompt_id": f"{row['scenario_id']}__{task_id}",
                "scenario_id": row["scenario_id"],
                "archive": row["archive"],
                "study": row["study"],
                "study_outcome_group": row["study_outcome_group"],
                "outcome": row["outcome"],
                "condition_a": row["condition_a"],
                "condition_b": row["condition_b"],
                "framing": task_id,
                "material_status": row["material_status"],
                "materials_ready": row["materials_ready"],
                "implied_probability_threshold": implied_threshold(task_id),
                "prompt": f"{row['materials']}\n\n{framings[task_id]}",
            }
            for row in scenarios
        ]
        path = out_dir / f"{task_id}.jsonl"
        write_jsonl(path, rows)
        tasks.append({"task_id": task_id, "output": task_id, "path": path.name})
    (out_dir / "tasks.json").write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(TASK_IDS)} tasks x {len(scenarios)} cases to {out_dir}")


if __name__ == "__main__":
    main()
