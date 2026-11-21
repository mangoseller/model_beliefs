"""Sample the 500 Werewolf cases from the Kaggle replay archive.

A case is a focal non-Werewolf player at a daytime decision point (discussion
or vote) of one game, together with another living, unrevealed target player.
The ground truth is whether the target is a Werewolf, read from the game's
final role table.  The seeded sampler draws one case from each of 500 distinct
replays so that the sample is exactly balanced over the truth label,
discussion versus voting phase, and the within-game quartile of the cut.
The case set feeds the Werewolf prompts (Appendix prompt catalog) and all
Werewolf analyses.

Outputs (``<SOURCES_DIR>/werewolf``):

- ``case_manifest.jsonl`` -- one row per case,
- ``case_evidence.jsonl`` -- the focal player's private and public evidence,
- ``sampling_manifest.json`` -- sampling audit and balance validation.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from latent_beliefs.config import SOURCES_DIR, WEREWOLF_REPLAY_DIR, ensure

SEED = 20260730
SAMPLE_SIZE = 500
PHASES = ("DAY_CHAT_AWAIT", "DAY_VOTING_AWAIT")
QUARTILE_LABELS = ("Q1", "Q2", "Q3", "Q4")
EVIDENCE_KEYS = ("private_evidence", "public_evidence")

PRIVATE_EVENT_NAMES = {
    "game_start",
    "heal_action",
    "heal_result",
    "inspect_action",
    "inspect_result",
}
REQUEST_EVENT_NAMES = {
    "chat_request",
    "heal_request",
    "inspect_request",
    "vote_request",
}


# ------------------------------------------------------------ replay reading


def truth_map(game: dict[str, Any]) -> dict[str, str]:
    """Use the post-game map; configuration.agents is not player-ID aligned."""
    return dict(game["info"]["GAME_END"]["all_players_and_role"])


def event_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("created_at"),
        event.get("event_name"),
        event.get("source"),
        event.get("description"),
    )


def visible_events_through(
    game: dict[str, Any], focal_player: str, cut_step: int
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for step in game["steps"][: cut_step + 1]:
        for agent_record in step:
            raw = (agent_record.get("observation") or {}).get("raw_observation")
            if not raw or raw.get("player_id") != focal_player:
                continue
            for event in raw.get("new_player_event_views") or []:
                key = event_key(event)
                if key not in seen:
                    seen.add(key)
                    events.append(event)
    events.sort(key=lambda event: str(event.get("created_at", "")))
    return events


def event_bucket(event: dict[str, Any]) -> str | None:
    name = event.get("event_name")
    if name in REQUEST_EVENT_NAMES:
        return None
    if name in PRIVATE_EVENT_NAMES:
        return "private"
    if name == "game_end":
        return None
    return "public"


def clean_description(description: str) -> str:
    return "\n".join(line.rstrip() for line in description.strip().splitlines())


def render_event(event: dict[str, Any]) -> str:
    day = event.get("day")
    phase = event.get("phase") or "Unknown phase"
    return f"[Day {day} | {phase}] {clean_description(event['description'])}"


def active_day_cuts(
    replay_id: str, game: dict[str, Any]
) -> Iterable[dict[str, Any]]:
    """Every eligible (focal player, cut, target) case in one replay."""
    roles = truth_map(game)
    for step_index, step in enumerate(game["steps"]):
        for slot_index, record in enumerate(step):
            raw = (record.get("observation") or {}).get("raw_observation")
            if (
                record.get("status") != "ACTIVE"
                or not raw
                or raw.get("game_state_phase") != "Day"
                or raw.get("detailed_phase") not in set(PHASES)
            ):
                continue
            focal = raw["player_id"]
            focal_role = roles[focal]
            if focal_role == "Werewolf":
                continue
            revealed = raw.get("revealed_players") or {}
            for target in raw.get("alive_players") or []:
                if target == focal or target in revealed:
                    continue
                yield {
                    "replay_id": replay_id,
                    "step_index": step_index,
                    "slot_index": slot_index,
                    "focal_player": focal,
                    "focal_role": focal_role,
                    "target_player": target,
                    "target_role": roles[target],
                    "label_is_werewolf": int(roles[target] == "Werewolf"),
                    "day": raw["day"],
                    "phase": raw["game_state_phase"],
                    "detailed_phase": raw["detailed_phase"],
                    "alive_players": list(raw.get("alive_players") or []),
                    "revealed_players": dict(revealed),
                }


def enrich_case(case: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    """Attach the rendered evidence visible to the focal player at the cut."""
    events = visible_events_through(game, case["focal_player"], case["step_index"])
    private_events = [
        render_event(event) for event in events if event_bucket(event) == "private"
    ]
    public_events = [
        render_event(event) for event in events if event_bucket(event) == "public"
    ]
    return {
        **case,
        "private_evidence": private_events,
        "public_evidence": public_events,
    }


def case_id(case: dict[str, Any]) -> str:
    return (
        f'{case["replay_id"]}_step{case["step_index"]}_'
        f'{case["focal_player"]}_target_{case["target_player"]}'
    )


# ------------------------------------------------------------------ sampling


def stratum_quota(phase: str, quartile: int, label: int, sample_size: int) -> int:
    """Cases per (phase, cut quartile, label) cell.

    With a remainder of 4 modulo 16, four cells receive one extra case, chosen
    so the label, phase, and quartile margins stay exactly balanced.
    """
    base, remainder = divmod(sample_size, 16)
    if remainder not in {0, 4}:
        raise ValueError("sample_size must have remainder 0 or 4 modulo 16")
    extra_cells = {
        ("DAY_CHAT_AWAIT", 0, 0),
        ("DAY_CHAT_AWAIT", 1, 1),
        ("DAY_VOTING_AWAIT", 2, 0),
        ("DAY_VOTING_AWAIT", 3, 1),
    }
    extra = int(remainder == 4 and (phase, quartile, label) in extra_cells)
    return base + extra


def attach_cut_quartiles(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank the replay's distinct cuts in game order and assign quartiles."""
    cut_keys = sorted(
        {
            (case["step_index"], case["slot_index"], case["focal_player"])
            for case in cases
        }
    )
    cut_to_rank = {key: rank for rank, key in enumerate(cut_keys)}
    n_cuts = len(cut_keys)
    enriched = []
    for case in cases:
        key = (case["step_index"], case["slot_index"], case["focal_player"])
        rank = cut_to_rank[key]
        quartile = min(3, (4 * rank) // n_cuts)
        enriched.append(
            {
                **case,
                "cut_rank": rank,
                "eligible_cut_count_in_game": n_cuts,
                "cut_progress": (rank + 0.5) / n_cuts,
                "cut_quartile": quartile,
                "cut_quartile_label": QUARTILE_LABELS[quartile],
            }
        )
    return enriched


def _match_replays_to_strata(
    *,
    paths: list[Path],
    quotas: dict[tuple[str, int, int], int],
    rng: random.Random,
    sample_size: int,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
    """Find a capacity-constrained replay-to-stratum matching.

    Each replay contributes at most one case. Keeping one random candidate per
    eligible stratum is sufficient because prompt cases within a
    replay/stratum are exchangeable for the balancing constraints.
    """
    candidates: dict[str, dict[tuple[str, int, int], dict[str, Any]]] = {}
    replay_paths: dict[str, Path] = {}
    assignment: dict[str, tuple[str, int, int]] = {}
    members: dict[tuple[str, int, int], list[str]] = {
        stratum: [] for stratum in quotas
    }
    skipped_bad: list[dict[str, str]] = []
    scanned = 0

    def try_assign(
        replay_id: str,
        seen_replays: set[str],
        seen_strata: set[tuple[str, int, int]],
    ) -> bool:
        # Augmenting-path search: place the replay in its least-filled
        # eligible stratum, displacing an incumbent that can move elsewhere.
        if replay_id in seen_replays:
            return False
        seen_replays.add(replay_id)
        eligible = list(candidates[replay_id])
        rng.shuffle(eligible)
        eligible.sort(
            key=lambda stratum: (
                len(members[stratum]) / quotas[stratum],
                stratum,
            )
        )
        for stratum in eligible:
            if stratum in seen_strata:
                continue
            seen_strata.add(stratum)
            if len(members[stratum]) < quotas[stratum]:
                members[stratum].append(replay_id)
                assignment[replay_id] = stratum
                return True

            incumbents = list(members[stratum])
            rng.shuffle(incumbents)
            for incumbent in incumbents:
                if try_assign(incumbent, seen_replays, seen_strata):
                    members[stratum].remove(incumbent)
                    members[stratum].append(replay_id)
                    assignment[replay_id] = stratum
                    return True
        return False

    for path in paths:
        if len(assignment) == sample_size:
            break
        scanned += 1
        try:
            game = json.loads(path.read_text())
            replay_cases = list(active_day_cuts(path.stem, game))
            if not replay_cases:
                continue
            replay_cases = attach_cut_quartiles(replay_cases)
        except Exception as exc:
            skipped_bad.append(
                {
                    "replay_id": path.stem,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                }
            )
            continue

        by_stratum: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        for case in replay_cases:
            stratum = (
                case["detailed_phase"],
                case["cut_quartile"],
                case["label_is_werewolf"],
            )
            if stratum in quotas:
                by_stratum[stratum].append(case)
        if not by_stratum:
            continue

        replay_id = path.stem
        candidates[replay_id] = {
            stratum: rng.choice(stratum_cases)
            for stratum, stratum_cases in by_stratum.items()
        }
        replay_paths[replay_id] = path
        try_assign(replay_id, set(), set())

    if len(assignment) != sample_size:
        deficits = {
            str(stratum): quotas[stratum] - len(members[stratum])
            for stratum in quotas
            if len(members[stratum]) != quotas[stratum]
        }
        raise RuntimeError(
            f"Matched {len(assignment)} distinct replays instead of "
            f"{sample_size}; deficits={deficits}"
        )

    selected: list[dict[str, Any]] = []
    for replay_id in candidates:
        if replay_id not in assignment:
            continue
        stratum = assignment[replay_id]
        case = candidates[replay_id][stratum]
        game = json.loads(replay_paths[replay_id].read_text())
        case = enrich_case(case, game)
        case["sample_index"] = len(selected)
        case["sample_stratum"] = (
            f"{case['detailed_phase']}__{case['cut_quartile_label']}"
            f"__label{case['label_is_werewolf']}"
        )
        selected.append(case)
    return selected, scanned, skipped_bad


def select_cases(
    replay_dir: Path, seed: int = SEED, sample_size: int = SAMPLE_SIZE
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Seeded, balanced sample of one case from each of ``sample_size`` replays."""
    rng = random.Random(seed)
    paths = sorted(
        path for path in replay_dir.glob("*.json") if path.stat().st_size > 0
    )
    rng.shuffle(paths)

    quotas = {
        (phase, quartile, label): stratum_quota(phase, quartile, label, sample_size)
        for phase in PHASES
        for quartile in range(4)
        for label in (0, 1)
    }
    selected, scanned, skipped_bad = _match_replays_to_strata(
        paths=paths, quotas=quotas, rng=rng, sample_size=sample_size
    )
    for case in selected:
        case["sample_seed"] = seed
    counts = Counter(
        (case["detailed_phase"], case["cut_quartile"], case["label_is_werewolf"])
        for case in selected
    )
    if counts != Counter(quotas):
        raise AssertionError(f"Matched sample does not equal requested quotas: {counts}")
    audit = {
        "seed": seed,
        "selection_strategy": "capacity_constrained_distinct_replay_matching",
        "populated_replay_paths": len(paths),
        "replays_scanned": scanned,
        "bad_replays_encountered": skipped_bad,
        "stratum_quotas": {
            f"{phase}__{QUARTILE_LABELS[quartile]}__label{label}": quota
            for (phase, quartile, label), quota in sorted(quotas.items())
        },
    }
    return selected, audit


def validate_sample(cases: list[dict[str, Any]], sample_size: int) -> dict[str, Any]:
    replay_ids = [case["replay_id"] for case in cases]
    if len(cases) != sample_size or len(set(replay_ids)) != sample_size:
        raise AssertionError(
            f"Expected {sample_size} cases from {sample_size} distinct replays"
        )
    labels = Counter(case["label_is_werewolf"] for case in cases)
    phases = Counter(case["detailed_phase"] for case in cases)
    quartiles = Counter(case["cut_quartile_label"] for case in cases)
    if labels != Counter({0: sample_size // 2, 1: sample_size // 2}):
        raise AssertionError(f"Unexpected label balance: {labels}")
    if phases != Counter({phase: sample_size // 2 for phase in PHASES}):
        raise AssertionError(f"Unexpected phase balance: {phases}")
    if quartiles != Counter({label: sample_size // 4 for label in QUARTILE_LABELS}):
        raise AssertionError(f"Unexpected quartile balance: {quartiles}")
    return {
        "n_cases": len(cases),
        "n_distinct_replays": len(set(replay_ids)),
        "label_counts": dict(sorted(labels.items())),
        "phase_counts": dict(sorted(phases.items())),
        "cut_quartile_counts": dict(sorted(quartiles.items())),
        "day_counts": dict(sorted(Counter(case["day"] for case in cases).items())),
        "focal_role_counts": dict(
            sorted(Counter(case["focal_role"] for case in cases).items())
        ),
    }


# ------------------------------------------------------------------- outputs


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay-dir", type=Path, default=WEREWOLF_REPLAY_DIR)
    parser.add_argument("--output-dir", type=Path, default=SOURCES_DIR / "werewolf")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)
    args = parser.parse_args()
    replay_dir = args.replay_dir.expanduser().resolve()
    output_dir = ensure(args.output_dir.resolve())

    cases, sampling = select_cases(replay_dir, args.seed, args.sample_size)
    validation = validate_sample(cases, args.sample_size)

    case_rows = [
        {key: value for key, value in case.items() if key not in EVIDENCE_KEYS}
        for case in cases
    ]
    write_jsonl(output_dir / "case_manifest.jsonl", case_rows)
    write_jsonl(
        output_dir / "case_evidence.jsonl",
        (
            {"case_id": case_id(case), **{key: case[key] for key in EVIDENCE_KEYS}}
            for case in cases
        ),
    )
    manifest = {
        "sample_seed": args.seed,
        "sample_size": args.sample_size,
        "replay_dir": str(replay_dir),
        "sampling": sampling,
        "validation": validation,
    }
    (output_dir / "sampling_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(validation, indent=2, sort_keys=True))
    print(f"Wrote {len(cases)} Werewolf cases to {output_dir}")


if __name__ == "__main__":
    main()
