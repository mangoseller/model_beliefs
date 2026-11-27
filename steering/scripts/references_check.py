"""The leak of the unedited reasoned answers with two sets of reference answers: session 1's (its own sampler)
and the ones the ``references`` stage samples with this sampler (folder refs_own). Same patients for both.

    python -m steering.scripts.references_check <run dir> <session 1 beliefs_direct.parquet>

Writes analysis/references_check.csv.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from steering import analysis

MIN_PATIENTS = 8


def mean_beliefs(replies: pd.DataFrame) -> pd.DataFrame:
    """One belief per (patient, arm, order, role): the mean clipped logit of the stated probabilities, over
    the replies that stated one."""
    replies = replies.assign(logit=analysis.clip_logit(replies.value.fillna(0.5)))
    replies.loc[replies.value.isna(), "logit"] = np.nan
    keys = ["case_id", "arm", "block_order", "role"]
    return replies.groupby(keys).logit.mean().rename("value").reset_index().assign(replicate=0)


def main(run_dir: Path, session1: Path, resamples: int = 2000) -> pd.DataFrame:
    """Per cell: the leak of the unedited reasoned answers under each set of references, with intervals, the
    references' slope and spread, and how well the two sets agree patient by patient."""
    session1_beliefs = pd.read_parquet(session1)
    references = analysis.read_rows(run_dir / "refs_own")
    reasoned = analysis.read_rows(run_dir / "reason")
    reasoned = reasoned[reasoned.variant == "base"]
    sampled_here = mean_beliefs(pd.concat([references, reasoned], ignore_index=True))
    rng, rows = np.random.default_rng(0), []
    for (arm, order), _ in references[references.role == "M"].groupby(["arm", "block_order"]):
        here = analysis.cell(sampled_here, arm, order)
        there = analysis.cell(session1_beliefs, arm, order)
        patients = sorted(set(here.index) & set(there.index))
        if len(patients) < MIN_PATIENTS:
            continue
        draws = rng.integers(0, len(patients), (resamples, len(patients)))
        answers = here.loc[patients, "C"].to_numpy()
        of_arm = references[references.arm == arm]
        row = dict(arm=arm, block_order=order, patients=len(patients),
                   parsed=float(of_arm.value.notna().mean()), truncated=float(of_arm.truncated.mean()))
        for name, cell in (("session 1", there), ("sampled here", here)):
            # the own-belief gap x and the reference answers M (PERSON's record) and D (the model's)
            gap, reference_m, reference_d = (cell.loc[patients, column].to_numpy()
                                             for column in ("x", "M", "D"))
            lower, upper = analysis.interval(analysis.lam(answers, reference_m, reference_d, gap, draws))
            row |= {f"lambda, {name} references": float(analysis.lam(answers, reference_m, reference_d, gap)),
                    f"{name} lower": lower, f"{name} upper": upper,
                    f"slope of D-M on x, {name}": float(analysis.slopes(reference_d - reference_m, gap)),
                    f"mean |x|, {name}": float(np.abs(gap).mean())}
        for column in ("x", "M", "D"):
            agreement = np.corrcoef(there.loc[patients, column], here.loc[patients, column])[0, 1]
            row[f"correlation of {column} between the two"] = float(agreement)
        rows.append(row)
    table = pd.DataFrame(rows)
    (run_dir / "analysis").mkdir(exist_ok=True)
    table.to_csv(run_dir / "analysis" / "references_check.csv", index=False)
    return table


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    print(main(Path(sys.argv[1]), Path(sys.argv[2])).round(3).T.to_string())
