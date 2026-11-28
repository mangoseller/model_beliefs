"""Cross-validated XGBoost CKD risk score for every labelled NHANES participant.

The out-of-fold predicted probability from 5-fold stratified cross-validation
over 26 demographic, socioeconomic, anthropometric, blood-pressure, diabetes,
lifestyle, and cardiovascular features is the ``risk_estimate`` used to
stratify the case cohorts (``archive.smolin_wilder_replication.ckd.cases``) and to place the
probability-bin edges of the quintile prompt.

Input:  ``SOURCES_DIR/ckd/nhanes_ckd.csv``
Output: ``SOURCES_DIR/ckd/nhanes_ckd_risk.csv`` (the input table re-read from
CSV plus a ``risk_score`` column; unlabelled rows have no score).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import OneHotEncoder
from xgboost import XGBClassifier

from archive.smolin_wilder_replication.ckd.nhanes import NHANES_TABLE
from archive.smolin_wilder_replication.config import SOURCES_DIR

RISK_TABLE = SOURCES_DIR / "ckd" / "nhanes_ckd_risk.csv"
RISK_COLUMN = "risk_score"
LABEL_COLUMN = "ckd"
SEED = 42
FOLDS = 5
XGB_PARAMS = {
    "n_estimators": 500,
    "learning_rate": 0.03,
    "max_depth": 3,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
}

# (feature key, column of nhanes_ckd.csv, numeric?) in model column order.
RISK_FEATURES: list[tuple[str, str, bool]] = [
    ("rce", "ridreth3", False),
    ("sex", "riagendr", False),
    ("age", "ridageyr", True),
    ("edu", "dmdeduc2", False),
    ("inc", "indfmin2", False),
    ("pir", "indfmpir", True),
    ("ins", "hiq011", False),
    ("acc", "huq030", False),
    ("fds", "fsdhh", False),
    ("bmi", "bmxbmi", True),
    ("wst", "bmxwaist", True),
    ("sbp", "sbp_mean", True),
    ("dbp", "dbp_mean", True),
    ("htn", "hypertension_self_reported", False),
    ("htnmed", "hypertension_meds", False),
    ("diab", "diabetes_self_reported", False),
    ("prediab", "prediabetes_self_reported", False),
    ("diabmed", "diabetes_medication", False),
    ("fhd", "family_history_diabetes", False),
    ("smk100", "smoking_ever_100", False),
    ("smkstat", "smoking_status_current", False),
    ("cigs", "cigarettes_per_day", True),
    ("alc", "alcohol_drinks_per_day", True),
    ("cvd", "cvd_any", False),
    ("pa", "physical_activity_any", False),
    ("sodium", "sodium_mg_mean", True),
]


def load_features(path: Path) -> pd.DataFrame:
    """Feature keys, label, and ``row_index`` (row position in ``path``)."""
    sources = sorted({source for _, source, _ in RISK_FEATURES} | {LABEL_COLUMN})
    df = pd.read_csv(path, usecols=sources)
    out = pd.DataFrame(index=df.index)
    for key, source, numeric in RISK_FEATURES:
        out[key] = pd.to_numeric(df[source], errors="coerce") if numeric else df[source]
    out[LABEL_COLUMN] = pd.to_numeric(df[LABEL_COLUMN], errors="coerce").astype("Int64")
    out.insert(0, "row_index", out.index.astype(int))
    return out


def _normalize(df: pd.DataFrame, cat_features: list[str], num_features: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cat_features:
        out[c] = out[c].astype("object").where(out[c].notna(), "Missing").astype(str)
    for c in num_features:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def out_of_fold_risk(features: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """Return the labelled rows and their out-of-fold CKD probabilities."""
    labeled = features[features[LABEL_COLUMN].notna()].copy().reset_index(drop=True)
    y = labeled[LABEL_COLUMN].astype(int).to_numpy()

    keys = [key for key, _, _ in RISK_FEATURES]
    cat_features = [key for key, _, numeric in RISK_FEATURES if not numeric]
    num_features = [key for key, _, numeric in RISK_FEATURES if numeric]
    X = _normalize(labeled[keys], cat_features, num_features)

    skf = StratifiedKFold(n_splits=FOLDS, shuffle=True, random_state=SEED)
    oos_pred = np.full(len(labeled), np.nan, dtype=float)
    for train_idx, valid_idx in skf.split(X, y):
        preprocess = ColumnTransformer(
            transformers=[
                ("cat", OneHotEncoder(handle_unknown="ignore"), cat_features),
                ("num", "passthrough", num_features),
            ]
        )
        X_train_enc = preprocess.fit_transform(X.iloc[train_idx])
        X_valid_enc = preprocess.transform(X.iloc[valid_idx])
        model = XGBClassifier(
            **XGB_PARAMS,
            objective="binary:logistic",
            eval_metric="auc",
            random_state=SEED,
            n_jobs=-1,
        )
        model.fit(X_train_enc, y[train_idx])
        oos_pred[valid_idx] = model.predict_proba(X_valid_enc)[:, 1]
    if np.isnan(oos_pred).any():
        raise RuntimeError("Some labelled rows did not receive out-of-fold predictions.")
    return labeled, oos_pred


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=NHANES_TABLE)
    parser.add_argument("--out", type=Path, default=RISK_TABLE)
    args = parser.parse_args()

    labeled, oos_pred = out_of_fold_risk(load_features(args.input))
    y = labeled[LABEL_COLUMN].astype(int).to_numpy()

    full_df = pd.read_csv(args.input)
    full_df[RISK_COLUMN] = np.nan
    row_index_to_pred = dict(
        zip(labeled["row_index"].astype(int).tolist(), oos_pred.tolist(), strict=True)
    )
    ridx = np.arange(len(full_df), dtype=int)
    full_df.loc[:, RISK_COLUMN] = [row_index_to_pred.get(i, np.nan) for i in ridx]
    full_df.to_csv(args.out, index=False)
    print(f"Wrote {args.out}: out-of-fold AUC {roc_auc_score(y, oos_pred):.6f}")


if __name__ == "__main__":
    main()
