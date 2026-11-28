"""Sampled answers of a cell under every edit found, with measures that do not rest on a slope over patients.

    python -m steering.scripts.leak_under_edits <session1 beliefs_direct.parquet> <run dir> [<run dir> ...]
        [--folders a,b] [--draws 4000] [--out table.csv]

Every ``reason*`` folder under the run directories is read, so unedited and edited replies from different
runs can be set side by side. Per cell and edit, on the patients of each edit: lambda with its interval; the
root mean square distance of the answers (logits) from the two reference answers M (PERSON's record) and D
(the model's); the share of patients whose answer is nearer D; what the reasoning cites. lambda is a ratio of
slopes over patients and is wide at 24 to 32 patients; the distances and the citations are the check on it.
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from steering import analysis
from steering import prompts
from steering import transcripts
from steering.results import read_rows

MIN_PATIENTS = 8
REPLY_KEYS = ["folder", "case_id", "arm", "block_order", "variant", "alpha", "sample"]


def load_replies(runs: list[Path]) -> pd.DataFrame:
    """The replies to the two-record question (role C) from every ``reason*`` folder under the run
    directories, one row per sampled reply, with the answer's clipped logit (NaN where no probability was
    stated)."""
    frames = []
    for run in runs:
        for folder in sorted(path for path in run.rglob("reason*") if path.is_dir()):
            rows = read_rows(folder)
            if len(rows) and {"variant", "text", "role"} <= set(rows.columns):
                frames.append(rows[rows.role == "C"].assign(folder=folder.name))
    replies = pd.concat(frames, ignore_index=True).drop_duplicates(REPLY_KEYS)
    replies["logit"] = analysis.clip_logit(replies.value.fillna(0.5))
    replies.loc[replies.value.isna(), "logit"] = np.nan
    return replies


def edit_row(replies: pd.DataFrame, patients: list[str], prompt_of: pd.Series, references: tuple,
             draws: int, rng) -> dict:
    """The measures of one edit in one cell. ``replies``: the edit's replies from ``patients``;
    ``references``: the patients' own-belief gap x and their reference answers M (from PERSON's record) and D
    (from the model's), as arrays in the patients' order."""
    gap, reference_m, reference_d = references
    answer = replies.groupby("case_id").logit.mean().reindex(patients)
    categories = [transcripts.category(text, prompt_of[case])
                  for text, case in zip(replies.text, replies.case_id)]
    cited = pd.Series(categories).value_counts(normalize=True)
    row = dict(patients=int(answer.notna().sum()), of=len(patients), answers=len(replies),
               parsed=replies.value.notna().mean(), tokens=replies.n_tokens.median(),
               own_only=cited.get("own only", 0.0), both=cited.get("both", 0.0),
               person_only=cited.get("person only", 0.0), neither=cited.get("neither", 0.0))
    answered = answer.notna().to_numpy()
    if answered.sum() < MIN_PATIENTS:
        return row
    answers = answer.to_numpy()[answered]
    gap, reference_m, reference_d = gap[answered], reference_m[answered], reference_d[answered]
    resampled = rng.integers(0, answered.sum(), (draws, answered.sum()))
    lower, upper = analysis.interval(analysis.lam(answers, reference_m, reference_d, gap, resampled))
    row |= dict(lam=analysis.lam(answers, reference_m, reference_d, gap), lo=lower, hi=upper,
                rms_to_M=np.sqrt(np.mean((answers - reference_m) ** 2)),
                rms_to_D=np.sqrt(np.mean((answers - reference_d) ** 2)),
                nearer_D=np.mean(np.abs(answers - reference_d) < np.abs(answers - reference_m)))
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session1", type=Path)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--folders", help="comma-separated folder names whose patients define the comparison")
    parser.add_argument("--draws", type=int, default=4000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    warnings.filterwarnings("ignore")
    session1 = pd.read_parquet(args.session1)
    replies = load_replies(args.runs)
    focus = set(args.folders.split(",")) if args.folders else set(replies.folder)
    all_patients, rng, rows = prompts.patients(), np.random.default_rng(0), []
    for (arm, order), cell_replies in replies.groupby(["arm", "block_order"]):
        in_focus = sorted(cell_replies[cell_replies.folder.isin(focus)].case_id.unique())
        session1_cell = analysis.cell(session1, arm, order)
        patients = [case for case in in_focus if case in session1_cell.index]
        if len(patients) < MIN_PATIENTS:
            continue
        references = tuple(session1_cell.loc[patients, column].to_numpy() for column in ("x", "M", "D"))
        # This draw is not used, but it stays: every later interval is drawn from the same generator, so
        # removing it would change all of them.
        rng.integers(0, len(patients), (args.draws, len(patients)))
        cell_prompts = prompts.build(all_patients.loc[patients], [arm])
        cell_prompts = cell_prompts[(cell_prompts.role == "C") & (cell_prompts.block_order == order)]
        prompt_of = cell_prompts.set_index("case_id").prompt
        of_patients = cell_replies[cell_replies.case_id.isin(patients)]
        for (folder, variant, alpha), edit_replies in of_patients.groupby(["folder", "variant", "alpha"]):
            row = dict(arm=arm, block_order=order, folder=folder, variant=variant, alpha=alpha)
            rows.append(row | edit_row(edit_replies, patients, prompt_of, references, args.draws, rng))
    table = pd.DataFrame(rows)
    if args.out:
        table.to_csv(args.out, index=False)
    with pd.option_context("display.width", 250, "display.max_colwidth", 60):
        print(table.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
