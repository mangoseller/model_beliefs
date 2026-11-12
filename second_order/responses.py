"""Parse cached responses with the unchanged first-order parser and build matrices.

``data/responses/{model}/{condition}.parquet``: one row per case and output
with raw text, parsed value and parse status.  Nothing is imputed; failures
are logged in the parse status column and counted in the retention table.

``matrix(model)`` gives one row per (case, condition) with the 16 dataset
columns used by ``latent_beliefs.latent.domains.CKD_SPECS``, a completeness
flag, and the split.  The split is the model's condition A split from
``build/datasets/ckd.csv`` (for local models, the split of ``split_from``).
Cases outside that split are given split ``none``.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from latent_beliefs.ckd.parse import parse_response
from latent_beliefs.datasets import DOMAIN_SPECS
from second_order import config as C
from second_order.conditions import CONDITIONS
from second_order.prompts_second_order import OUTPUTS
from second_order.run_generation import lookup, main_items

# first-order output (prompt variant) -> dataset / likelihood column
DATASET_COLUMN = {variant: column for column, variant, _ in DOMAIN_SPECS["ckd"].outputs}
COLUMNS = [column for column, _, _ in DOMAIN_SPECS["ckd"].outputs]

REFUSAL = re.compile(
    r"(cannot (?:know|be known|determine|read|predict)|can't (?:know|read|predict)|impossible to know|"
    r"no way (?:to|of) know|not possible to know|I (?:cannot|can't) speak for|"
    r"would depend on (?:his|my|your) (?:own|personal))",
    flags=re.I,
)


def split_table(model: str) -> pd.Series:
    first = pd.read_csv(C.FIRST_ORDER_DATASET, usecols=["model_key", "source_prompt_id", "split"])
    first = first[first.model_key == C.split_source(model)]
    return first.set_index("source_prompt_id")["split"]


def collect(model: str, case_ids: list[str], conditions: list[str], write: bool = True) -> pd.DataFrame:
    items = main_items(model, case_ids, conditions)
    records = lookup(items)
    rows = []
    for item, record in zip(items, records):
        if record is None:
            rows.append({"case_id": item.case_id, "condition": item.condition, "output": item.output,
                         "status": "not_generated", "text": None, "value": np.nan,
                         "refusal_like": False, "source": None})
            continue
        if not record["ok"]:
            status, value = "call_error", None
        else:
            value, _ = parse_response(item.output, record["text"])
            status = "parsed" if value is not None else "parse_failure"
        rows.append({"case_id": item.case_id, "condition": item.condition, "output": item.output,
                     "status": status, "text": record["text"],
                     "value": np.nan if value is None else float(value),
                     "refusal_like": bool(REFUSAL.search(record["text"] or "")),
                     "source": record.get("source", "cache")})
    frame = pd.DataFrame(rows)
    frame.insert(0, "model", model)
    if write:
        for condition, part in frame.groupby("condition"):
            path = C.RESPONSES / model / f"{condition}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            part.to_parquet(path, index=False)
    return frame


def matrix(long: pd.DataFrame) -> pd.DataFrame:
    model = long["model"].iloc[0]
    generated = long[long.status != "not_generated"]
    wide = generated.pivot_table(index=["case_id", "condition"], columns="output", values="value",
                                 aggfunc="first", dropna=False)
    wide = wide.reindex(columns=list(OUTPUTS)).rename(columns=DATASET_COLUMN)
    n_generated = generated.groupby(["case_id", "condition"]).size().reindex(wide.index)
    wide = wide.reset_index()
    wide["complete"] = wide[COLUMNS].notna().all(axis=1) & (n_generated.to_numpy() == len(OUTPUTS))
    splits = split_table(model)
    wide["split"] = wide["case_id"].map(splits).fillna("none")
    wide["framing"] = wide["condition"].map(lambda c: CONDITIONS[c].framing)
    wide.insert(0, "model", model)
    return wide


def retention(long: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (model, condition), part in long.groupby(["model", "condition"]):
        generated = part[part.status != "not_generated"]
        if generated.empty:
            continue
        per_case = generated.groupby("case_id")["status"].apply(lambda s: (s == "parsed").all()
                                                               and len(s) == len(OUTPUTS))
        rows.append({
            "model": model, "condition": condition,
            "cases_generated": int(per_case.size), "cases_complete": int(per_case.sum()),
            "retention": float(per_case.mean()),
            "responses": int(len(generated)),
            "parse_failures": int((generated.status == "parse_failure").sum()),
            "call_errors": int((generated.status == "call_error").sum()),
            "refusal_like_responses": int(generated.refusal_like.sum()),
        })
    return pd.DataFrame(rows)
