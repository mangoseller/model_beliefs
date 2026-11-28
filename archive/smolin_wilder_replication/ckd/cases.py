"""Draw the two risk-stratified 500-case CKD cohorts.

Labelled participants are split into risk deciles (``pd.qcut`` of the
cross-validated risk score) and 50 are drawn at random from each decile
(seed 42), then shuffled.  ``original500`` is drawn first; ``next500`` is drawn
the same way after removing the ``original500`` participants, with deciles
recomputed on the remaining rows.  The paper analyses ``original500``;
``next500`` only enters through the probability-bin edges of the quintile
prompt, which are quintiles of the risk scores of all 1,000 cases.

Input:  ``SOURCES_DIR/ckd/nhanes_ckd_risk.csv``
Output: ``SOURCES_DIR/ckd/cases.csv``, one row per case with the cohort,
``prompt_id`` (``prompt_0001``...), ``row_index`` (row of the NHANES table),
``sampled_row_index`` (row within the reduced table ``next500`` was drawn from;
empty for ``original500``), ``seqn``, ``risk_estimate``, ``risk_stratum``,
the ``ckd`` label, and the 12 NHANES variables shown in the patient profile.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from archive.smolin_wilder_replication.ckd.risk import LABEL_COLUMN, RISK_COLUMN, RISK_TABLE
from archive.smolin_wilder_replication.config import SOURCES_DIR, ensure

CASES_PATH = SOURCES_DIR / "ckd" / "cases.csv"
N_CASES = 500
N_STRATA = 10
SEED = 42

# NHANES variables shown to the models, in patient-profile order.
PROFILE_FEATURES = [
    "hypertension_self_reported",
    "diabetes_self_reported",
    "indfmin2",
    "cvd_any",
    "ridageyr",
    "ridreth3",
    "dmdeduc2",
    "physical_activity_any",
    "smoking_status_current",
    "riagendr",
    "bmxbmi",
    "sodium_mg_mean",
]
CASE_COLUMNS = [
    "source_cohort",
    "prompt_id",
    "row_index",
    "sampled_row_index",
    "seqn",
    "risk_estimate",
    "risk_stratum",
    LABEL_COLUMN,
    *PROFILE_FEATURES,
]


def balanced_stratified_sample(df: pd.DataFrame) -> pd.DataFrame:
    """Equal draws from each risk-score decile, shuffled; ``row_index`` = index in ``df``."""
    work = df.copy()
    work = work[work[RISK_COLUMN].notna()].copy()
    work["risk_stratum"] = pd.qcut(work[RISK_COLUMN], q=N_STRATA, duplicates="drop")
    strata_labels = list(work["risk_stratum"].dropna().unique())
    strata_labels.sort()

    base = N_CASES // len(strata_labels)
    remainder = N_CASES % len(strata_labels)
    rng = np.random.default_rng(SEED)

    sampled_parts = []
    for i, label in enumerate(strata_labels):
        bucket = work[work["risk_stratum"] == label]
        need = base + (1 if i < remainder else 0)
        picked_idx = rng.choice(bucket.index.to_numpy(), size=need, replace=False)
        sampled_parts.append(work.loc[picked_idx])

    sampled = pd.concat(sampled_parts, axis=0).copy()
    sampled = sampled.sample(frac=1.0, random_state=SEED).reset_index(drop=False)
    sampled = sampled.rename(columns={"index": "row_index"})
    sampled["risk_stratum"] = sampled["risk_stratum"].astype(str)
    return sampled


def case_records(cohort: str, sampled: pd.DataFrame, original_row_index: bool) -> list[dict]:
    records = []
    for i, row in sampled.iterrows():
        records.append(
            {
                "source_cohort": cohort,
                "prompt_id": f"prompt_{i + 1:04d}",
                "row_index": int(row["_original_row_index"] if original_row_index else row["row_index"]),
                "sampled_row_index": int(row["row_index"]) if original_row_index else None,
                "seqn": int(row["seqn"]),
                "risk_estimate": float(row[RISK_COLUMN]),
                "risk_stratum": row["risk_stratum"],
                LABEL_COLUMN: int(bool(row[LABEL_COLUMN])),
                **{feature: row[feature] for feature in PROFILE_FEATURES},
            }
        )
    return records


def draw_cohorts(df: pd.DataFrame) -> pd.DataFrame:
    original = case_records("original500", balanced_stratified_sample(df), False)

    exclude_rows = pd.Series([r["row_index"] for r in original]).unique()
    remaining = df.drop(index=exclude_rows, errors="ignore").reset_index(drop=False)
    remaining = remaining.rename(columns={"index": "_original_row_index"})
    following = case_records("next500", balanced_stratified_sample(remaining), True)

    cases = pd.DataFrame(original + following, columns=CASE_COLUMNS)
    cases["sampled_row_index"] = cases["sampled_row_index"].astype("Int64")
    return cases


def read_cases(path: Path = CASES_PATH) -> pd.DataFrame:
    """Read ``cases.csv`` with exact float round-tripping."""
    return pd.read_csv(
        path,
        float_precision="round_trip",
        dtype={"risk_stratum": str, "sampled_row_index": "Int64"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=RISK_TABLE)
    parser.add_argument("--out", type=Path, default=CASES_PATH)
    args = parser.parse_args()

    cases = draw_cohorts(pd.read_csv(args.input))
    ensure(args.out.parent)
    cases.to_csv(args.out, index=False)
    print(f"Wrote {args.out}")
    print(cases.groupby("source_cohort", sort=False)[LABEL_COLUMN].agg(["size", "sum"]).to_string())


if __name__ == "__main__":
    main()
