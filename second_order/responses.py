"""Parse cached responses and build the analysis tables.

``collect`` writes ``data/responses/{model}/{dataset}/{condition}.parquet``: one row per
case, output, run (``replicate``) and ``sample``, with the raw text, the parsed value
and a parse status.  Nothing is imputed: a response that does not parse is a
``parse_failure`` and is counted in the retention table.  Reasoning in ``<think>``
tags is removed before parsing.  Values are the probability in [0, 1] (from the JSON
object both datasets ask for), the meaning of the chosen option (1 = screen or
accept, 0 = not; 1-5 for the Likert and quintile scales), or, for the CKD checks
and role probes, 1 if the chosen letter is the keyed answer.

``matrix`` gives the CKD latent-fit matrix: one row per (case, condition, replicate),
one column per question and option order (``latent_specs``), the number of parsed
outputs, ``complete`` (all parsed), ``usable`` (at least ``latent.min_output_share`` parsed;
the latent model masks the missing ones) and a seeded patient-level split.
"""

from __future__ import annotations

import re
from functools import lru_cache

import numpy as np
import pandas as pd

from latent_beliefs.latent.model import OutputSpec
from second_order import config as C
from second_order import datasets as DS
from second_order.bespoke import score_comprehension
from second_order.conditions import CONDITIONS
from second_order.run_generation import FOLLOWUP, comprehension_items, experiment_items, lookup

UNIT = ["case_id", "replicate", "sample"]  # one model call per output
CHECKS = ("check", "role_check")
THINK = re.compile(r"<think>.*?</think>", flags=re.S)


def visible_text(text: str | None) -> str | None:
    """The response without reasoning in ``<think>`` tags (unchanged if there are none)."""
    if text is None or "</think>" not in text:
        return text
    text = THINK.sub("", text)
    return text.split("</think>")[-1].strip()


def responses_path(model: str, dataset: str, condition: str):
    return C.RESPONSES / model / dataset / f"{condition}.parquet"


# ---------------------------------------------------------------- latent specs
def cutoff(question_id: str) -> float | None:
    """The probability at which a decision should flip: 1 / (1 + k) for threshold_k, bet_k and cost_k."""
    kind, _, k = question_id.partition("_")
    return 1.0 / (1.0 + float(k)) if kind in ("threshold", "bet", "cost") else None


@lru_cache(maxsize=None)
def latent_specs(batteries: tuple[str, ...]) -> tuple[OutputSpec, ...]:
    """One output per question and option order in the latent batteries (each its own item parameters)."""
    frame = DS.rows("ckd", list(batteries))
    outputs = sorted(set(frame.output[~frame.question_id.isin(CHECKS)]),
                     key=lambda o: (o != "probability/normal", o))
    specs = []
    for output in outputs:
        question = output.split("/")[0]
        if question == "probability":
            specs.append(OutputSpec(output, "probability"))
        elif question in ("likert", "quintile"):
            specs.append(OutputSpec(output, "ordinal", n_categories=5))
        else:
            specs.append(OutputSpec(output, "binary", nominal_threshold=cutoff(question)))
    return tuple(specs)


def configured_specs() -> tuple[OutputSpec, ...]:
    return latent_specs(tuple(C.load()["latent"]["batteries"]))


# ---------------------------------------------------------------- parsing
def collect(model: str, dataset: str, case_ids: list[str], write: bool = True) -> pd.DataFrame:
    items = experiment_items(model, dataset, case_ids)
    rows = []
    for item, record in zip(items, lookup(items)):
        meta = DS.row(dataset, item.case_id, item.condition, item.output, item.replicate)
        base = {"case_id": item.case_id, "condition": item.condition, "output": item.output,
                "battery": meta["battery"], "replicate": item.replicate, "sample": item.sample}
        if record is None:
            rows.append({**base, "status": "not_generated", "text": None, "value": np.nan})
            continue
        if not record["ok"]:
            rows.append({**base, "status": "call_error", "text": record["text"], "value": np.nan})
            continue
        value, _ = DS.parse(dataset, item.output, item.prompt, visible_text(record["text"]))
        if value is not None and item.output.split("/")[0] in CHECKS:
            letter = DS.parse_letter(visible_text(record["text"]), DS.options(item.prompt))
            value = float(letter == meta["check_answer"])
        rows.append({**base, "status": "parsed" if value is not None else "parse_failure",
                     "text": record["text"], "value": np.nan if value is None else float(value)})
    frame = pd.DataFrame(rows)
    frame.insert(0, "model", model)
    frame.insert(1, "dataset", dataset)
    if write:
        for condition, part in frame.groupby("condition"):
            path = responses_path(model, dataset, condition)
            path.parent.mkdir(parents=True, exist_ok=True)
            part.to_parquet(path, index=False)
    return frame


@lru_cache(maxsize=None)
def split(model: str) -> pd.Series:
    """Seeded train/val/test split over CKD patients (the latent fit's split)."""
    from second_order.fit_pooled import stable_seed

    ids = DS.case_ids("ckd")
    rng = np.random.default_rng(stable_seed(int(C.load()["seed"]), model, "split"))
    order = rng.permutation(len(ids))
    n_test = int(round(0.2 * len(ids)))
    n_val = int(round(0.2 * (len(ids) - n_test)))
    labels = np.array(["train"] * len(ids), dtype=object)
    labels[order[:n_test]] = "test"
    labels[order[n_test:n_test + n_val]] = "val"
    return pd.Series(labels, index=ids, name="split")


def matrix(long: pd.DataFrame) -> pd.DataFrame:
    """The CKD latent-fit matrix: (case, condition, replicate) x every latent output."""
    model = long["model"].iloc[0]
    names = [s.name for s in configured_specs()]
    conditions = set(DS.rows("ckd", list(C.load()["latent"]["batteries"])).condition)
    keep = long[(long.status != "not_generated") & long.output.isin(names) & long.condition.isin(conditions)]
    keys = ["case_id", "condition", "replicate"]
    wide = keep.pivot_table(index=keys, columns="output", values="value", aggfunc="first", dropna=False)
    wide = wide.reindex(columns=names)
    n_answered = keep.groupby(keys).size().reindex(wide.index)
    wide = wide.reset_index()
    wide["n_outputs"] = wide[names].notna().sum(axis=1)
    wide["complete"] = (wide.n_outputs == len(names)) & (n_answered.to_numpy() == len(names))
    wide["usable"] = wide.n_outputs >= float(C.load()["latent"].get("min_output_share", 1.0)) * len(names)
    wide["split"] = wide["case_id"].map(split(model)).fillna("none")
    wide.insert(0, "model", model)
    return wide


def retention(long: pd.DataFrame) -> pd.DataFrame:
    """Per condition: share of calls answered and parsed, over the belief questions."""
    rows = []
    for (model, dataset, condition), part in long.groupby(["model", "dataset", "condition"]):
        generated = part[(part.status != "not_generated") & ~part.output.str.split("/").str[0].isin(CHECKS)]
        if generated.empty:
            continue
        rows.append({"model": model, "dataset": dataset, "condition": condition,
                     "responses": int(len(generated)), "parsed": int((generated.status == "parsed").sum()),
                     "retention": float((generated.status == "parsed").mean()),
                     "parse_failures": int((generated.status == "parse_failure").sum()),
                     "call_errors": int((generated.status == "call_error").sum())})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- checks and comprehension
def checks(long: pd.DataFrame) -> pd.DataFrame:
    """CKD checks and role probes, one row per answer, with the keyed relation and record order."""
    part = long[long.output.str.split("/").str[0].isin(CHECKS) & (long.status != "not_generated")].copy()
    if part.empty:
        return pd.DataFrame()
    meta = [DS.row("ckd", c, k, o, r) for c, k, o, r in zip(part.case_id, part.condition, part.output, part.replicate)]
    for column in ("check_fact", "check_relation", "block_order", "check_answer"):
        part[column] = [m[column] for m in meta]
    part["setting"] = part.condition.map(lambda c: CONDITIONS[c].setting)
    part["order"] = part.output.str.split("/").str[1]
    part["correct"] = part.value.where(part.status == "parsed", 0.0).where(part.status != "call_error")
    return part.drop(columns=["text"])


def comprehension(model: str, dataset: str, case_ids: list[str], write: bool = True) -> pd.DataFrame:
    """Bespoke follow-ups: one row each, scored against the row's keyed answer."""
    items, _ = comprehension_items(model, dataset, case_ids)
    rows = []
    for item, record in zip(items, lookup(items)):
        meta = DS.row(dataset, item.case_id, item.condition, "probability")
        text = visible_text(record["text"]) if record and record["ok"] else None
        score = score_comprehension(meta["comprehension_q"], meta["comprehension_a"], text)
        status = ("not_generated" if record is None else "call_error" if not record["ok"]
                  else "parsed" if score is not None else "parse_failure")
        rows.append({"model": model, "dataset": dataset, "case_id": item.case_id, "condition": item.condition,
                     "output": FOLLOWUP, "replicate": item.replicate, "sample": item.sample, "status": status,
                     "check_relation": meta["check_relation"], "value": np.nan if score is None else score,
                     "text": None if record is None else record["text"]})
    frame = pd.DataFrame(rows, columns=["model", "dataset", "case_id", "condition", "output", "replicate",
                                        "sample", "status", "check_relation", "value", "text"])
    frame["correct"] = scored(frame)
    if write and not frame.empty:
        path = responses_path(model, dataset, "comprehension")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
    return frame


def scored(frame: pd.DataFrame) -> pd.Series:
    """1.0 correct, 0.0 wrong or unparseable, NaN not answered (not generated, or a call error)."""
    answered = frame.status.isin(("parsed", "parse_failure"))
    return frame.value.where(frame.status == "parsed", 0.0).where(answered)


def accuracy_summary(frame: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Accuracy over answered checks, per group."""
    if frame.empty:
        return pd.DataFrame()
    g = frame.dropna(subset=["correct"]).fillna({c: "" for c in by}).groupby(by)
    return pd.DataFrame({"answered": g.size(), "accuracy": g.correct.mean()}).reset_index()
