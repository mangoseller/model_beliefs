"""Render docs/example_prompts.md: three cases across all conditions and outputs.

    python -m second_order.example_prompts
"""

from __future__ import annotations

from second_order import config as C
from second_order.conditions import CONDITIONS, withheld_fields
from second_order.prompts_second_order import OUTPUTS, build_prompt, cases
from second_order.run_generation import pilot_cases


def main() -> None:
    cfg = C.load()
    ids = [c for c in cases().index if c not in set(pilot_cases())][:3]
    conds = list(CONDITIONS)
    lines = [
        "# Example prompts (Phase 2)\n\n",
        f"Withheld set {cfg['withheld_set']}: {', '.join(withheld_fields())}.  C/D layout: `{cfg['layout']}`.  "
        f"Cases: {', '.join(ids)}.\n\n",
        f"False-belief field: {cfg['flip_field']}.  Withheld fields rendered as: {cfg['missing_fields']}.\n\n"
        "Condition A is the first-order prompt, byte-identical to the released prompts (tested).  Each B is A "
        "with the agent's copy as the profile.  C and D differ only in the agent's copy description.  Each M "
        "is D with the agent's copy as the profile, the same bytes as the matching B (tested).  John and user "
        "prompts differ only in person words (tested).  Answer formats and options are cut verbatim from the "
        "first-order prompt.\n\n## Contents\n\n",
    ]
    for case_id in ids:
        lines.append(f"- {case_id}: " + ", ".join(f"[{c}](#{case_id.lower()}-{c.lower()})" for c in conds) + "\n")
    for case_id in ids:
        for cond in conds:
            lines.append(f"\n## {case_id} {cond}\n\n")
            c = CONDITIONS[cond]
            lines.append(f"Role {c.role}; pair: {c.pair}; question about: {c.about}; model sees: "
                         f"{c.model_profile}; agent has: {c.agent_profile}; framing: {c.framing}.\n")
            for output in OUTPUTS:
                lines.append(f"\n### {output}\n\n```\n{build_prompt(case_id, cond, output)}```\n")
    path = C.DOCS / "example_prompts.md"
    path.write_text("".join(lines))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
