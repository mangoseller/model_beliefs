"""Phase 1 pilot: does each manipulation move the model's own belief?

The leakage estimators only have power if the agent's copy changes the
model's own answer.  For the pilot cases (flagged, written to
``results/pilot/pilot_cases.txt``), each candidate withheld set (H, S1..S4)
and the false-belief flip (F), collect the numeric probability three times
under the partial profile (condition ``B_<set>``), and top condition A up to
three numeric samples (replicate 0 is the released response for hosted
models; local models generate all three).

Per case: delta = mean logit p_A - mean logit p_B (p clipped to [0.01, 0.99],
over the parsed samples).  Reported per set and model: median |delta|, SD of
delta (the slope estimators draw their power from the spread), share
with |delta| > 0.5, counts of positive and negative delta, parse rate, and the
number of B responses that remark on missing fields.

    python -m second_order.pilot generate --model gemma3_4b_mlx [--approve]
    python -m second_order.pilot report
"""

from __future__ import annotations

import argparse
import re

import numpy as np
import pandas as pd

from latent_beliefs.ckd.parse import parse_response
from second_order import config as C
from second_order.beliefs import clip_logit as logit
from second_order.conditions import render_profile, withheld_fields
from second_order.prompts_second_order import cases, first_order_prompt
from second_order.run_generation import Item, lookup, pilot_cases, run

OUT = C.RESULTS / "pilot"
ABSENCE = (r"(?:not (?:provided|given|listed|included|available|specified|mentioned|reported|stated|known)|"
           r"missing|absent|unknown|no (?:information|data|mention)|isn't (?:provided|given|mentioned|listed)|"
           r"is not (?:provided|given|mentioned|listed)|without (?:information|data|knowing)|don't know|"
           r"do not know|lack of)")
FIELD_TERMS = {
    "hypertension_self_reported": r"(?:hypertension|blood pressure|\bhtn\b)",
    "diabetes_self_reported": r"(?:diabet)",
    "cvd_any": r"(?:cardiovascular|\bcvd\b|heart disease)",
    "ridageyr": r"(?:\bage\b|years old|how old)",
}
# A remark = a withheld field's name within 60 characters of an absence phrase.
REMARK = re.compile(
    r"(?:" + "|".join(FIELD_TERMS.values()) + r")[^.\n]{0,60}" + ABSENCE + r"|" + ABSENCE + r"[^.\n]{0,60}(?:"
    + "|".join(FIELD_TERMS.values()) + r")",
    flags=re.I,
)


def items(model: str) -> list[Item]:
    cfg = C.load()
    samples = int(cfg["pilot"]["samples"])
    result = []
    for case_id in pilot_cases():
        case = cases().loc[case_id]
        full = first_order_prompt("probability", render_profile(case))
        for r in range(samples):
            result.append(Item(model, case_id, "A", "probability", r, full))
        copies = {name: render_profile(case, withheld_fields(name), missing=cfg["missing_fields"])
                  for name in cfg["withheld_sets"]}
        copies["F"] = render_profile(case, flip=cfg["flip_field"])  # false-belief pair
        for name, profile in copies.items():
            prompt = first_order_prompt("probability", profile)
            for r in range(samples):
                result.append(Item(model, case_id, f"B_{name}", "probability", r, prompt))
    return result


def table(model: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    its = items(model)
    records = lookup(its)
    rows = []
    for item, record in zip(its, records):
        if record is None:
            continue
        value, _ = parse_response("probability", record["text"]) if record["ok"] else (None, None)
        rows.append({"model": model, "case_id": item.case_id, "condition": item.condition,
                     "replicate": item.replicate, "p": value, "parsed": value is not None,
                     "remarks_missing": bool(REMARK.search(record["text"])),
                     "text": record["text"]})
    frame = pd.DataFrame(rows)
    summary = []
    if frame.empty:
        return frame, pd.DataFrame()
    a = frame[(frame.condition == "A") & frame.parsed].groupby("case_id")["p"].apply(
        lambda s: logit(s.to_numpy(float)).mean())
    for name in list(C.load()["withheld_sets"]) + ["F"]:
        b_all = frame[frame.condition == f"B_{name}"]
        if b_all.empty:
            continue
        b = b_all[b_all.parsed].groupby("case_id")["p"].apply(lambda s: logit(s.to_numpy(float)).mean())
        delta = (a - b).dropna()
        summary.append({
            "model": model, "set": name,
            "fields": f"flip:{C.load()['flip_field']}" if name == "F" else "|".join(withheld_fields(name)),
            "cases": int(len(delta)), "median_abs_delta": float(np.median(np.abs(delta))) if len(delta) else np.nan,
            "sd_delta": float(delta.std()) if len(delta) > 1 else np.nan,
            "share_abs_delta_gt_0.5": float(np.mean(np.abs(delta) > 0.5)) if len(delta) else np.nan,
            "n_positive": int((delta > 0).sum()), "n_negative": int((delta < 0).sum()),
            "n_zero": int((delta == 0).sum()), "mean_delta": float(delta.mean()) if len(delta) else np.nan,
            "parse_rate_B": float(b_all.parsed.mean()), "responses_B": int(len(b_all)),
            "B_responses_remarking_on_missing_fields": int(b_all.remarks_missing.sum()),
        })
    a_all = frame[frame.condition == "A"]
    summary.append({"model": model, "set": "A", "fields": "", "cases": int(a.notna().sum()),
                    "parse_rate_B": float(a_all.parsed.mean()), "responses_B": int(len(a_all)),
                    "B_responses_remarking_on_missing_fields": int(a_all.remarks_missing.sum())})
    return frame, pd.DataFrame(summary)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--model", required=True)
    g.add_argument("--approve", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--models", nargs="+", default=None)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "pilot_cases.txt").write_text("\n".join(pilot_cases()) + "\n")
    if args.cmd == "generate":
        run(items(args.model), approve=args.approve, label=f"pilot/{args.model}")
        return
    cfg = C.load()
    models = args.models or (cfg["pilot"]["models"] + cfg["pilot"]["local_models"])
    frames, summaries = [], []
    for model in models:
        frame, summary = table(model)
        if frame.empty:
            print(f"{model}: no pilot responses cached")
            continue
        frames.append(frame)
        summaries.append(summary)
    if not summaries:
        return
    summary = pd.concat(summaries, ignore_index=True)
    summary.to_csv(OUT / "pilot_summary.csv", index=False)
    responses = pd.concat(frames, ignore_index=True)
    responses.drop(columns="text").to_csv(OUT / "pilot_responses.csv", index=False)
    remarks = responses[responses.remarks_missing & responses.condition.str.startswith("B_")]
    with (OUT / "missing_field_remarks.md").open("w") as handle:
        handle.write("# Pilot B responses that mention missing information\n\n")
        for _, row in remarks.head(40).iterrows():
            m = REMARK.search(row.text)
            s = max(0, m.start() - 200)
            handle.write(f"## {row.model} {row.case_id} {row.condition} r{row.replicate}\n\n"
                         f"> …{row.text[s:m.end() + 200].strip()}…\n\n")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
