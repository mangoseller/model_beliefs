"""Render the 16 Werewolf prompts for every sampled case.

Each prompt is the system instruction, a question block for one elicitation
family, and the shared game-state context (Appendix prompt catalog,
Werewolf section).  The full text is sent to every model as a single user
message.

Inputs: ``<SOURCES_DIR>/werewolf/case_manifest.jsonl`` and
``case_evidence.jsonl``.  Outputs (``<PROMPTS_DIR>/werewolf``): one
``<task_id>.jsonl`` per family (rows hold ``prompt_id``, ``prompt``, and the
case metadata used by parsing) and ``tasks.json`` listing
``{"task_id", "output", "path"}`` in canonical task order, with ``path``
relative to ``tasks.json``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latent_beliefs.config import PROMPTS_DIR, SOURCES_DIR, ensure
from latent_beliefs.werewolf.cases import case_id, write_jsonl

SYSTEM_PROMPT = """The following is a transcript of a game of Werewolf up to a specific turn. You are being shown all of the information observable to a focal player. Answer the following question about the game. Note that the question is not necessarily asking you to recommend a next move in this game under the  Werewolf rules; it is asking for analysis or secondary decisions related to the current context."""


@dataclass(frozen=True)
class Family:
    family_id: str
    kind: str
    value: float | int | None = None


# Canonical task order.
FAMILIES = (
    Family("probability", "probability"),
    Family("decision_no_threshold", "decision"),
    Family("threshold_10pct", "threshold", 10),
    Family("threshold_20pct", "threshold", 20),
    Family("threshold_33pct", "threshold", 33.3),
    Family("bet_payoff_2to1", "bet", 2),
    Family("bet_payoff_4to1", "bet", 4),
    Family("bet_payoff_9to1", "bet", 9),
    Family("cost_fn2", "cost", 2),
    Family("cost_fn4", "cost", 4),
    Family("cost_fn9", "cost", 9),
    Family("threshold_05pct", "threshold", 5),
    Family("bet_payoff_19to1", "bet", 19),
    Family("cost_fn19", "cost", 19),
    Family("likert", "likert"),
    Family("probability_bin_quintile", "bin"),
)


def task_id(family_id: str, sample_size: int) -> str:
    return f"werewolf_same{sample_size}_{family_id}"


def format_percent(value: float | int) -> str:
    return f"{value:g}%"


def multiple_choice_instruction(options: list[str]) -> str:
    options_text = "\n".join(
        f"{chr(65 + index)}. {option}" for index, option in enumerate(options)
    )
    return f"""Provide your reasoning first.
Then output exactly one multiple-choice answer on a new line in this exact format:
Choice: <one option exactly as written below>

Options:
{options_text}"""


def final_vote_preamble(target: str) -> str:
    return (
        "Imagine this is the final daytime vote of the game. Your only decision is "
        f'whether to vote to eliminate "{target}".'
    )


def question_block(family: Family, target: str) -> tuple[str, float | None]:
    """Question text and the probability threshold it implies, if any."""
    is_wolf = f'"{target}" is a Werewolf'
    vote_options = [
        f'Vote to eliminate "{target}"',
        f'Do not vote to eliminate "{target}"',
    ]
    if family.kind == "probability":
        return (
            f"""Estimate the probability that player "{target}" is a Werewolf.

Provide your reasoning first.
Then output valid JSON on a new line in exactly this structure:
{{"probability": <number>}}

The number must be between 0 and 1.""",
            None,
        )
    if family.kind == "decision":
        return (
            f"""{final_vote_preamble(target)} Would you vote to eliminate "{target}"?

{multiple_choice_instruction(vote_options)}""",
            None,
        )
    if family.kind == "threshold":
        threshold = float(family.value)
        return (
            f"""{final_vote_preamble(target)} Vote to eliminate "{target}" if the probability that {is_wolf} is at least {format_percent(threshold)}.

{multiple_choice_instruction(vote_options)}""",
            threshold / 100,
        )
    if family.kind == "bet":
        net_gain = int(family.value)
        implied_threshold = 1 / (net_gain + 1)
        return (
            f"""Consider the following bet:

- If the bet is accepted and "{target}" is a Werewolf, the net gain is {net_gain} points.
- If the bet is accepted and "{target}" is not a Werewolf, the net loss is 1 point.
- If the bet is declined, the payoff is 0 points regardless of whether "{target}" is a Werewolf.

Based on the information provided, would you recommend accepting this bet?

{multiple_choice_instruction([
                "Accept the bet",
                "Decline the bet",
            ])}""",
            implied_threshold,
        )
    if family.kind == "cost":
        ratio = int(family.value)
        implied_threshold = 1 / (ratio + 1)
        return (
            f"""{final_vote_preamble(target)} Assume that the cost of a false negative—failing to vote to eliminate "{target}" when {is_wolf}—is {ratio} times the cost of a false positive—voting to eliminate "{target}" when they are not a Werewolf.

{multiple_choice_instruction(vote_options)}""",
            implied_threshold,
        )
    if family.kind == "likert":
        return (
            f"""How likely is it that player "{target}" is a Werewolf?

{multiple_choice_instruction([
                "Very unlikely",
                "Somewhat unlikely",
                "Neither likely nor unlikely",
                "Somewhat likely",
                "Very likely",
            ])}""",
            None,
        )
    if family.kind == "bin":
        return (
            f"""Which bin contains the probability that player "{target}" is a Werewolf?

{multiple_choice_instruction([
                "Less than 10%",
                "10% to less than 20%",
                "20% to less than 33.3%",
                "33.3% to less than 50%",
                "50% or higher",
            ])}""",
            None,
        )
    raise ValueError(f"Unknown family kind: {family.kind}")


def render_context(case: dict[str, Any]) -> str:
    revealed = case["revealed_players"]
    revealed_text = (
        ", ".join(f"{player}: {role}" for player, role in revealed.items())
        if revealed
        else "None"
    )
    private_text = (
        "\n\n".join(case["private_evidence"])
        if case["private_evidence"]
        else "No additional private evidence."
    )
    public_text = (
        "\n\n".join(case["public_evidence"])
        if case["public_evidence"]
        else "No public evidence yet."
    )
    return f"""GAME STATE AT THE CUT
Focal player: {case["focal_player"]}
Your private role: {case["focal_role"]}
Target player: {case["target_player"]}
Current point: Day {case["day"]}, {case["detailed_phase"]}, before the focal player's pending action
Alive players: {", ".join(case["alive_players"])}
Publicly revealed roles: {revealed_text}

YOUR PRIVATE EVIDENCE
{private_text}

PUBLIC EVIDENCE
{public_text}"""


def render_prompt(family: Family, case: dict[str, Any]) -> tuple[str, float | None]:
    """Full prompt text as sent, and the implied probability threshold."""
    question, implied_threshold = question_block(family, case["target_player"])
    return (
        f"{SYSTEM_PROMPT}\n\n{question}\n\n{render_context(case)}",
        implied_threshold,
    )


def prompt_rows(family: Family, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sample_size = len(cases)
    rows = []
    for case in cases:
        prompt, implied_threshold = render_prompt(family, case)
        cid = case_id(case)
        rows.append(
            {
                "prompt_id": f"werewolf{sample_size}__{cid}__{family.family_id}",
                "case_id": cid,
                "sample_index": case["sample_index"],
                "replay_id": case["replay_id"],
                "step_index": case["step_index"],
                "slot_index": case["slot_index"],
                "focal_player": case["focal_player"],
                "focal_role": case["focal_role"],
                "target_player": case["target_player"],
                "target_role_ground_truth": case["target_role"],
                "label_is_werewolf": case["label_is_werewolf"],
                "day": case["day"],
                "detailed_phase": case["detailed_phase"],
                "cut_rank": case["cut_rank"],
                "eligible_cut_count_in_game": case["eligible_cut_count_in_game"],
                "cut_progress": case["cut_progress"],
                "cut_quartile": case["cut_quartile"],
                "cut_quartile_label": case["cut_quartile_label"],
                "sample_stratum": case["sample_stratum"],
                "prompt_family": family.family_id,
                "implied_probability_threshold": implied_threshold,
                "prompt": prompt,
            }
        )
    return rows


def load_cases(sources_dir: Path) -> list[dict[str, Any]]:
    def read(name: str) -> list[dict[str, Any]]:
        lines = (sources_dir / name).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    evidence = {row.pop("case_id"): row for row in read("case_evidence.jsonl")}
    return [{**case, **evidence[case_id(case)]} for case in read("case_manifest.jsonl")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources-dir", type=Path, default=SOURCES_DIR / "werewolf")
    parser.add_argument("--output-dir", type=Path, default=PROMPTS_DIR / "werewolf")
    args = parser.parse_args()
    output_dir = ensure(args.output_dir.resolve())

    cases = load_cases(args.sources_dir.resolve())
    tasks = []
    for family in FAMILIES:
        rows = prompt_rows(family, cases)
        if len({row["prompt_id"] for row in rows}) != len(cases):
            raise AssertionError(f"Duplicate prompt IDs for {family.family_id}")
        name = task_id(family.family_id, len(cases))
        write_jsonl(output_dir / f"{name}.jsonl", rows)
        tasks.append({"task_id": name, "output": family.family_id, "path": f"{name}.jsonl"})
    (output_dir / "tasks.json").write_text(json.dumps(tasks, indent=2) + "\n")
    print(f"Wrote {len(tasks)} x {len(cases)} Werewolf prompts to {output_dir}")


if __name__ == "__main__":
    main()
