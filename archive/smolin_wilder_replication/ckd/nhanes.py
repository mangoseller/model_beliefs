"""Build the pooled NHANES 2011-2018 adult table with the CKD label.

Participants aged 18 or older who completed the mobile examination
(``RIDSTATR == 2``) are pooled across cycles G-J.  The table keeps the
26 inputs of the CKD risk model (``archive.smolin_wilder_replication.ckd.risk``), which include
the 12 features shown to the language models, and the label:

    CKD = 1  if eGFR (CKD-EPI 2021 creatinine equation) < 60 mL/min/1.73 m^2
             or urine albumin-to-creatinine ratio >= 30 mg/g;
    CKD = 0  if both are observed and neither criterion holds;
    missing otherwise.

Output: ``SOURCES_DIR/ckd/nhanes_ckd.csv`` (one row per participant; the row
position is the ``row_index`` used by the case cohorts).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from archive.smolin_wilder_replication.ckd.download import CYCLES, NHANES_DIR, Cycle, xpt_path
from archive.smolin_wilder_replication.config import SOURCES_DIR, ensure

NHANES_TABLE = SOURCES_DIR / "ckd" / "nhanes_ckd.csv"
MIN_AGE = 18

# NHANES variables read from each component.  DEMO, ALB_CR, and BIOPRO carry
# the label inputs; the others carry risk-model features.
COMPONENT_COLUMNS: dict[str, list[str]] = {
    "DEMO": ["RIDAGEYR", "RIAGENDR", "RIDRETH3", "RIDSTATR", "DMDEDUC2", "INDFMIN2", "INDFMPIR"],
    "ALB_CR": ["URXUMA", "URXUCR", "URDACT"],
    "BIOPRO": ["LBXSCR"],
    "BMX": ["BMXBMI", "BMXWAIST"],
    "BPX": ["BPXSY1", "BPXSY2", "BPXSY3", "BPXSY4", "BPXDI1", "BPXDI2", "BPXDI3", "BPXDI4"],
    "BPQ": ["BPQ020", "BPQ040A"],
    "DIQ": ["DIQ010", "DIQ050", "DIQ070", "DIQ160", "DIQ170"],
    "SMQ": ["SMQ020", "SMQ040", "SMD641"],
    "ALQ": ["ALQ130"],
    "MCQ": ["MCQ160B", "MCQ160C", "MCQ160D", "MCQ160E", "MCQ160F"],
    "HIQ": ["HIQ011"],
    "HUQ": ["HUQ030"],
    "FSQ": ["FSDHH"],
    "PAQ": ["PAQ605", "PAQ620", "PAQ650", "PAQ665", "PAQ706"],
    "DR1TOT": ["DR1TSODI"],
    "DR2TOT": ["DR2TSODI"],
}

OUTPUT_COLUMNS = [
    "SEQN",
    "RIDAGEYR",
    "RIAGENDR",
    "RIDRETH3",
    "DMDEDUC2",
    "INDFMIN2",
    "INDFMPIR",
    "HIQ011",
    "HUQ030",
    "FSDHH",
    "BMXBMI",
    "BMXWAIST",
    "SBP_MEAN",
    "DBP_MEAN",
    "HYPERTENSION_SELF_REPORTED",
    "HYPERTENSION_MEDS",
    "DIABETES_SELF_REPORTED",
    "PREDIABETES_SELF_REPORTED",
    "DIABETES_MEDICATION",
    "FAMILY_HISTORY_DIABETES",
    "SMOKING_EVER_100",
    "SMOKING_STATUS_CURRENT",
    "CIGARETTES_PER_DAY",
    "ALCOHOL_DRINKS_PER_DAY",
    "CVD_ANY",
    "PHYSICAL_ACTIVITY_ANY",
    "SODIUM_MG_MEAN",
    "CKD",
]


def _decode_bytes(series: pd.Series) -> pd.Series:
    if series.dtype != "object":
        return series
    return series.map(
        lambda x: x.decode("utf-8", errors="ignore") if isinstance(x, (bytes, bytearray)) else x
    )


def _read_xpt(path: Path) -> pd.DataFrame:
    df = pd.read_sas(path, format="xport", encoding="utf-8")
    df.columns = [str(c).upper() for c in df.columns]
    for col in df.columns:
        df[col] = _decode_bytes(df[col])
    return df


def _load_component(path: Path, columns: list[str]) -> pd.DataFrame:
    raw = _read_xpt(path)
    if "SEQN" not in raw.columns:
        raise RuntimeError(f"{path} has no SEQN column")
    keep = ["SEQN"] + [c for c in columns if c in raw.columns]
    out = raw.loc[:, keep].copy()
    # Some variables are absent in some cycles.
    for col in columns:
        if col not in out.columns:
            out[col] = np.nan
    return out.loc[:, ["SEQN"] + columns]


def _build_cycle(cycle: Cycle, nhanes_dir: Path) -> pd.DataFrame:
    merged = _load_component(xpt_path(cycle, "DEMO", nhanes_dir), COMPONENT_COLUMNS["DEMO"])
    for component, columns in COMPONENT_COLUMNS.items():
        if component == "DEMO":
            continue
        part = _load_component(xpt_path(cycle, component, nhanes_dir), columns)
        merged = merged.merge(part, on="SEQN", how="left")
    return merged


def _to_numeric(df: pd.DataFrame, cols: Iterable[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")


def _yes_no(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    out = pd.Series(pd.NA, index=s.index, dtype="Int64")
    out.loc[s == 1] = 1
    out.loc[s == 2] = 0
    return out


def _mean_of(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    return df[cols].apply(pd.to_numeric, errors="coerce").mean(axis=1, skipna=True)


def _any_yes_binary(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """1 if any item is yes, 0 if all observed items are no, missing if none observed."""
    stacked = pd.concat([_yes_no(df[c]) for c in cols], axis=1)
    out = pd.Series(pd.NA, index=df.index, dtype="Int64")
    any_yes = (stacked == 1).any(axis=1)
    has_obs = stacked.notna().any(axis=1)
    all_obs_no = ((stacked.fillna(0) == 0).all(axis=1)) & has_obs
    out.loc[any_yes] = 1
    out.loc[~any_yes & all_obs_no] = 0
    return out


def _egfr_ckd_epi_2021(df: pd.DataFrame) -> pd.Series:
    scr = pd.to_numeric(df["LBXSCR"], errors="coerce")
    age = pd.to_numeric(df["RIDAGEYR"], errors="coerce")
    female = pd.to_numeric(df["RIAGENDR"], errors="coerce") == 2

    kappa = np.where(female, 0.7, 0.9)
    alpha = np.where(female, -0.241, -0.302)
    sex_factor = np.where(female, 1.012, 1.0)

    scr_k = scr / kappa
    min_term = np.minimum(scr_k, 1.0) ** alpha
    max_term = np.maximum(scr_k, 1.0) ** -1.2
    age_term = 0.9938**age

    egfr = 142.0 * min_term * max_term * age_term * sex_factor
    egfr = pd.to_numeric(egfr, errors="coerce")
    egfr.loc[(scr <= 0) | (age < 0)] = np.nan
    return egfr


def _construct_derived(df: pd.DataFrame) -> pd.DataFrame:
    _to_numeric(df, [c for columns in COMPONENT_COLUMNS.values() for c in columns])

    df["SBP_MEAN"] = _mean_of(df, ["BPXSY1", "BPXSY2", "BPXSY3", "BPXSY4"])
    df["DBP_MEAN"] = _mean_of(df, ["BPXDI1", "BPXDI2", "BPXDI3", "BPXDI4"])

    df["HYPERTENSION_SELF_REPORTED"] = _yes_no(df["BPQ020"])
    df["HYPERTENSION_MEDS"] = _yes_no(df["BPQ040A"])

    df["DIABETES_SELF_REPORTED"] = _yes_no(df["DIQ010"])
    df["PREDIABETES_SELF_REPORTED"] = _yes_no(df["DIQ160"])
    insulin = _yes_no(df["DIQ050"])
    pills = _yes_no(df["DIQ070"])
    df["DIABETES_MEDICATION"] = _any_yes_binary(
        pd.DataFrame({"insulin": insulin, "pills": pills}), ["insulin", "pills"]
    )
    df["FAMILY_HISTORY_DIABETES"] = _yes_no(df["DIQ170"])

    df["SMOKING_EVER_100"] = _yes_no(df["SMQ020"])
    df["SMOKING_STATUS_CURRENT"] = pd.to_numeric(df["SMQ040"], errors="coerce")
    df["CIGARETTES_PER_DAY"] = pd.to_numeric(df["SMD641"], errors="coerce")

    df["ALCOHOL_DRINKS_PER_DAY"] = pd.to_numeric(df["ALQ130"], errors="coerce")

    df["CVD_ANY"] = _any_yes_binary(df, ["MCQ160B", "MCQ160C", "MCQ160D", "MCQ160E", "MCQ160F"])
    df["PHYSICAL_ACTIVITY_ANY"] = _any_yes_binary(
        df, ["PAQ605", "PAQ620", "PAQ650", "PAQ665", "PAQ706"]
    )

    dr1 = pd.to_numeric(df["DR1TSODI"], errors="coerce")
    dr2 = pd.to_numeric(df["DR2TSODI"], errors="coerce")
    df["SODIUM_MG_MEAN"] = np.where(
        dr1.notna() & dr2.notna(), (dr1 + dr2) / 2.0, np.where(dr1.notna(), dr1, dr2)
    )

    uacr_fallback = np.where(df["URXUCR"] > 0, (df["URXUMA"] / df["URXUCR"]) * 100.0, np.nan)
    df["UACR_MG_G"] = np.where(df["URDACT"].notna(), df["URDACT"], uacr_fallback)
    df["EGFR_2021"] = _egfr_ckd_epi_2021(df)

    egfr_pos = df["EGFR_2021"] < 60.0
    uacr_pos = df["UACR_MG_G"] >= 30.0
    df["CKD"] = pd.Series(pd.NA, index=df.index, dtype="Int64")
    df.loc[egfr_pos.fillna(False) | uacr_pos.fillna(False), "CKD"] = 1
    both_observed = df["EGFR_2021"].notna() & df["UACR_MG_G"].notna()
    df.loc[df["CKD"].isna() & both_observed, "CKD"] = 0
    return df


def build_table(nhanes_dir: Path = NHANES_DIR) -> pd.DataFrame:
    pooled = pd.concat([_build_cycle(cycle, nhanes_dir) for cycle in CYCLES], ignore_index=True)
    pooled = _construct_derived(pooled)
    pooled = pooled[pooled["RIDAGEYR"] >= MIN_AGE].copy()
    pooled = pooled[pooled["RIDSTATR"] == 2].copy()
    out = pooled.loc[:, OUTPUT_COLUMNS].copy()
    out.columns = [c.lower() for c in out.columns]
    return out.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nhanes-dir", type=Path, default=NHANES_DIR)
    parser.add_argument("--out", type=Path, default=NHANES_TABLE)
    args = parser.parse_args()

    out = build_table(args.nhanes_dir)
    ensure(args.out.parent)
    out.to_csv(args.out, index=False)

    label_n = int(out["ckd"].notna().sum())
    label_pos = int((out["ckd"] == 1).sum())
    print(f"Wrote {args.out}: {len(out)} rows, {label_n} labelled, {label_pos} with CKD")


if __name__ == "__main__":
    main()
