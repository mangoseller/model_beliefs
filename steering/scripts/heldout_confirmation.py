"""The held-out confirmation: the lookup circuit's key measures on test patients that no circuit stage had
touched, with every head set and vector fixed beforehand.

    python steering/scripts/heldout_confirmation.py <run dir> [first held-out patient, default 40]

where the run directory is, for example, runs/steering/qwen3_8b.

Prints each measure beside the prediction written down before the run, and writes analysis/confirmation.csv.
The block numbers and predictions are those of Qwen3 8B."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from steering import circuit
from steering import prompts
from steering.results import read_rows

BOOTSTRAP_DRAWS = 2000
MIN_GAP = 5.0       # a lookup counts if its two questions' margins are at least this far apart (logits)


def bootstrap_interval(frame: pd.DataFrame, column: str, rng) -> tuple[float, float]:
    """The 95% interval of the mean of the patients' means, by resampling patients; (nan, nan) under three."""
    return circuit.bootstrap_interval(frame, column, BOOTSTRAP_DRAWS, rng)


def measure(name: str, prediction: str, frame: pd.DataFrame, column: str, rng) -> dict:
    """One row of the confirmation table: the mean of ``column`` over the lookups in ``frame``, its interval,
    and the prediction it is set beside."""
    if frame.empty:
        raise ValueError(f"no runs for {name!r}: this script's block numbers and head-set sizes are those of "
                         "Qwen3 8B, so it cannot be used on another model's run directory")
    lower, upper = bootstrap_interval(frame, column, rng)
    return dict(measure=name, predicted=prediction, value=frame[column].mean(), lower=lower, upper=upper,
                lookups=len(frame), patients=frame.case_id.nunique())


def head_set_measures(run_dir: Path, held_out: list[str], rng) -> list[dict]:
    """How much of a lookup's answer the top lookup heads keep when every other head is taken from a run with
    the other answer (the other question, or the records under the slot edit), against random head sets."""
    runs = read_rows(run_dir / "circuit" / "faith")
    runs = circuit.effects(runs[runs.case_id.isin(held_out)], MIN_GAP)
    runs["kept"] = 1 - runs.effect
    rows = []
    keep_runs = runs[(runs.kind == "keep") & (runs.ranking == "query")]
    predictions = (("query", 64, "top", ">= 0.90"), ("query", 64, "random", "<= 0.05"),
                   ("query", 32, "top", ">= 0.70"), ("slot", 64, "top", ">= 0.85"),
                   ("slot", 64, "random", "(<= 0.05)"))
    for counterfactual, size, which, prediction in predictions:
        kept = keep_runs[(keep_runs.cf == counterfactual) & (keep_runs.k == size)]
        kept = kept[kept.set == "top"] if which == "top" else kept[kept.set.str.startswith("random")]
        change = "question swap" if counterfactual == "query" else "slot edit"
        rows.append(measure(f"{change}: {which} {size} lookup heads kept", prediction, kept, "kept", rng))
    return rows


def step_measures(run_dir: Path, held_out: list[str], rng) -> list[dict]:
    """The tests of single steps: the question's tokens from the other page order, reader heads barred from
    the asked record, vectors added at the answer token, and heads against MLPs."""
    tests = read_rows(run_dir / "circuit_concrete" / "test")
    tests = tests[tests.case_id.isin(held_out) & tests.ok].copy()
    tests["share"] = (tests.patched - tests.base) / (tests.other - tests.base)
    tests["flipped"] = np.where(tests["query"] == "person", 1.0, -1.0) * tests.patched < 0
    rows = []
    order = tests[(tests.test == "order") & (tests.group == "suffix")]
    rows.append(measure("order donor, all shared question tokens, block 21", ">= 0.50",
                        order[order.layer == 21], "share", rng))
    rows.append(measure("order donor, all shared question tokens, block 12", "<= 0.05",
                        order[order.layer == 12], "share", rng))
    barred = tests[(tests.test == "knock_heads") & (tests.group == "queried_lines")]
    rows.append(measure("readers barred from the queried record: lookups flipped", ">= 0.40",
                        barred[barred.variant == "readers"], "flipped", rng))
    rows.append(measure("random heads barred from it: lookups flipped", "<= 0.05",
                        barred[barred.variant.str.startswith("random")], "flipped", rng))
    for kind, prediction in (("place", ">= 0.60"), ("owner", "<= 0.10"), ("random", "<= 0.05")):
        added = tests[(tests.test == f"add_{kind}") & (tests.group == "last") & (tests.variant == "22-24@1")]
        name = f"{kind} vector at the answer token, blocks 22-24, x1: lookups flipped"
        rows.append(measure(name, prediction, added, "flipped", rng))
    swaps = tests[tests.test == "mlp_swap"]
    rows.append(measure("every head from the other question, MLPs held at base", "0.60-0.75",
                        swaps[swaps.group.str.startswith("no block")], "share", rng))
    rows.append(measure("every MLP from the other question, heads held at base", "0.25-0.40",
                        swaps[swaps.group.str.startswith("every block, heads")], "share", rng))
    return rows


def main() -> None:
    run_dir = Path(sys.argv[1])
    first = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    cases = prompts.patients()
    held_out = sorted(cases.index[cases == "test"])[first:]
    rng = np.random.default_rng(0)      # one generator for every interval, drawn in the table's row order
    table = pd.DataFrame(head_set_measures(run_dir, held_out, rng) + step_measures(run_dir, held_out, rng))
    table.to_csv(run_dir / "analysis" / "confirmation.csv", index=False)
    pd.set_option("display.width", 220)
    print(f"{len(held_out)} held-out patients from index {first}")
    print(table.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
