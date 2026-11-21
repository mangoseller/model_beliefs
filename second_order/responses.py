"""Parse cached responses with the unchanged first-order parser and build matrices.

``data/responses/{model}/{condition}.parquet`` (CKD) and
``data/responses/{model}/{dataset}/{condition}.parquet`` (other datasets): one
row per case, output, run (``replicate``) and ``sample`` with raw text, parsed
value and parse status.  Nothing is imputed; failures are logged in the parse
status column and counted in the retention table.  Reasoning in
``<think>`` tags (Qwen3 with thinking on) is removed before parsing.

``matrix(long)`` gives one row per (case, condition, replicate) with the 16
dataset columns used by ``latent_beliefs.latent.domains.CKD_SPECS``, a
completeness flag, and the split.  The split is the model's condition A split
from ``build/datasets/ckd.csv`` (for local models, the split of
``split_from``), or, for models with ``split: own``, a seeded split drawn
here (20% test, then 20% of the rest validation, as in the first-order
datasets).  Cases outside the split are given split ``none``.

``comprehension(model, dataset, ...)`` parses and scores the follow-up answers.
"""

from __future__ import annotations

import re
from functools import lru_cache

import numpy as np
import pandas as pd

from latent_beliefs.datasets import DOMAIN_SPECS
from second_order import config as C
from second_order import datasets as DS
from second_order.comprehension import parse_choice
from second_order.conditions import CONDITIONS
from second_order.prompts_second_order import OUTPUTS
from second_order.run_generation import comprehension_items, experiment_items, lookup

# first-order output (prompt variant) -> dataset / likelihood column
DATASET_COLUMN = {variant: column for column, variant, _ in DOMAIN_SPECS["ckd"].outputs}
COLUMNS = [column for column, _, _ in DOMAIN_SPECS["ckd"].outputs]
UNIT = ["case_id", "replicate", "sample"]  # one model call per output

REFUSAL = re.compile(
    r"(cannot (?:know|be known|determine|read|predict)|can't (?:know|read|predict)|impossible to know|"
    r"no way (?:to|of) know|not possible to know|I (?:cannot|can't) speak for|"
    r"would depend on (?:his|my|your) (?:own|personal))",
    flags=re.I,
)
THINK = re.compile(r"<think>.*?</think>", flags=re.S)


def visible_text(text: str | None) -> str | None:
    """The response without reasoning in ``<think>`` tags (unchanged if there are none)."""
    if text is None or "</think>" not in text:
        return text
    text = THINK.sub("", text)
    return text.split("</think>")[-1].strip()


def responses_path(model: str, dataset: str, condition: str):
    folder = C.RESPONSES / model if dataset == "ckd" else C.RESPONSES / model / dataset
    return folder / f"{condition}.parquet"


@lru_cache(maxsize=None)
def own_split(model: str) -> pd.Series:
    """Seeded train/val/test split over all CKD cases, for models without a first-order split."""
    from second_order.fit_pooled import stable_seed
    from second_order.prompts_second_order import cases

    ids = sorted(cases().index)
    rng = np.random.default_rng(stable_seed(int(C.load()["seed"]), model, "split"))
    order = rng.permutation(len(ids))
    n_test = int(round(0.2 * len(ids)))
    n_val = int(round(0.2 * (len(ids) - n_test)))
    labels = np.array(["train"] * len(ids), dtype=object)
    labels[order[:n_test]] = "test"
    labels[order[n_test:n_test + n_val]] = "val"
    return pd.Series(labels, index=ids, name="split")


def split_table(model: str) -> pd.Series:
    if C.model_config(model).get("split") == "own":
        return own_split(model)
    first = pd.read_csv(C.FIRST_ORDER_DATASET, usecols=["model_key", "source_prompt_id", "split"])
    first = first[first.model_key == C.split_source(model)]
    return first.set_index("source_prompt_id")["split"]


def collect(model: str, case_ids: list[str], conditions: list[str], write: bool = True,
            dataset: str = "ckd") -> pd.DataFrame:
    items = experiment_items(model, dataset, case_ids, conditions)
    records = lookup(items)
    rows = []
    for item, record in zip(items, records):
        base = {"case_id": item.case_id, "condition": item.condition, "output": item.output,
                "replicate": item.replicate, "sample": item.sample}
        if record is None:
            rows.append({**base, "status": "not_generated", "text": None, "value": np.nan,
                         "refusal_like": False, "source": None})
            continue
        if not record["ok"]:
            status, value = "call_error", None
        else:
            value, _ = DS.parse(dataset, item.output, visible_text(record["text"]))
            status = "parsed" if value is not None else "parse_failure"
        rows.append({**base, "status": status, "text": record["text"],
                     "value": np.nan if value is None else float(value),
                     "refusal_like": bool(REFUSAL.search(record["text"] or "")),
                     "source": record.get("source", "cache")})
    frame = pd.DataFrame(rows)
    frame.insert(0, "model", model)
    frame.insert(1, "dataset", dataset)
    if write:
        for condition, part in frame.groupby("condition"):
            path = responses_path(model, dataset, condition)
            path.parent.mkdir(parents=True, exist_ok=True)
            part.to_parquet(path, index=False)
    return frame


def matrix(long: pd.DataFrame) -> pd.DataFrame:
    model = long["model"].iloc[0]
    generated = long[long.status != "not_generated"]
    keys = ["case_id", "condition", "replicate"]
    wide = generated.pivot_table(index=keys, columns="output", values="value", aggfunc="first", dropna=False)
    wide = wide.reindex(columns=list(OUTPUTS)).rename(columns=DATASET_COLUMN)
    n_generated = generated.groupby(keys).size().reindex(wide.index)
    wide = wide.reset_index()
    wide["complete"] = wide[COLUMNS].notna().all(axis=1) & (n_generated.to_numpy() == len(OUTPUTS))
    splits = split_table(model)
    wide["split"] = wide["case_id"].map(splits).fillna("none")
    wide["framing"] = wide["condition"].map(lambda c: CONDITIONS[c].framing)
    wide.insert(0, "model", model)
    return wide


def retention(long: pd.DataFrame) -> pd.DataFrame:
    """Per condition: share of calls (case, run, sample) with every output parsed."""
    rows = []
    for (model, dataset, condition), part in long.groupby(["model", "dataset", "condition"]):
        generated = part[part.status != "not_generated"]
        if generated.empty:
            continue
        need = long.loc[long.dataset == dataset, "output"].nunique()
        per_unit = generated.groupby(UNIT)["status"].agg(lambda s, n=need: (s == "parsed").all() and len(s) == n)
        rows.append({
            "model": model, "dataset": dataset, "condition": condition,
            "cases_generated": int(per_unit.size), "cases_complete": int(per_unit.sum()),
            "retention": float(per_unit.mean()),
            "responses": int(len(generated)),
            "parse_failures": int((generated.status == "parse_failure").sum()),
            "call_errors": int((generated.status == "call_error").sum()),
            "refusal_like_responses": int(generated.refusal_like.sum()),
        })
    return pd.DataFrame(rows)


def comprehension(model: str, dataset: str, case_ids: list[str], conditions: list[str],
                  write: bool = True) -> pd.DataFrame:
    """Parsed and scored comprehension answers: one row per follow-up.

    ``correct`` is scored for answered follow-ups only (parsed, or unparseable = wrong);
    follow-ups not generated yet and call errors are missing, not wrong.
    """
    items, _ = comprehension_items(model, dataset, case_ids, conditions)
    rows = []
    for item, record in zip(items, lookup(items)):
        q = DS.comprehension(dataset, item.case_id, item.condition)
        choice = parse_choice(visible_text(record["text"]), q.options) if record and record["ok"] else None
        status = ("not_generated" if record is None else "call_error" if not record["ok"]
                  else "parsed" if choice is not None else "parse_failure")
        rows.append({"model": model, "dataset": dataset, "case_id": item.case_id, "condition": item.condition,
                     "output": item.output.split(":", 1)[1], "replicate": item.replicate, "sample": item.sample,
                     "status": status, "choice": np.nan if choice is None else choice, "answer": q.answer,
                     "text": None if record is None else record["text"]})
    frame = pd.DataFrame(rows, columns=["model", "dataset", "case_id", "condition", "output", "replicate",
                                        "sample", "status", "choice", "answer", "text"])
    frame["correct"] = scored(frame)
    if write and not frame.empty:
        path = responses_path(model, dataset, "comprehension")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    return frame


ANSWERED = ("parsed", "parse_failure")


def scored(frame: pd.DataFrame) -> pd.Series:
    """1.0 correct, 0.0 wrong or unparseable, NaN not answered (from status, choice and answer)."""
    correct = (frame.status == "parsed") & (frame.choice == frame.answer)
    return correct.astype(float).where(frame.status.isin(ANSWERED))


def comprehension_summary(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    frame = frame.assign(correct=scored(frame))
    g = frame.groupby(["model", "dataset", "condition"])
    return pd.DataFrame({
        "follow_ups": g.size(), "answered": g.correct.count(),
        "parsed": g.status.apply(lambda s: int((s == "parsed").sum())),
        "accuracy": g.correct.mean(),  # over answered follow-ups
        "cases_all_correct": g.apply(lambda p: int((p.groupby("case_id").correct.min() == 1).sum())),
        "cases": g.case_id.nunique(),
    }).reset_index()
