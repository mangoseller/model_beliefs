"""Render the sixteen CKD prompt families (Appendix, prompt catalog).

Each case's patient profile lists twelve NHANES variables.  The families are
a numerical probability, an unthresholded screening decision, four
probability-threshold decisions (5%, 10%, 20%, 33.3%), four bets (net gains of
2, 4, 9, and 19 points against a 1-point loss), four false-negative cost
ratios (2, 4, 9, 19), a five-point Likert judgment, and a five-bin
probability judgment whose bin edges are the risk-score quintiles of all
1,000 drawn cases.  Prompts are written for the 500 ``original500`` cases.

Input:  ``SOURCES_DIR/ckd/cases.csv``
Output: ``PROMPTS_DIR/ckd/<task_id>.jsonl`` (one row per case with
``prompt_id``, ``prompt``, and case metadata) and ``PROMPTS_DIR/ckd/tasks.json``
(``[{"task_id", "output", "path"}]`` in collection order; ``output`` is the
output name and ``path`` is relative to ``tasks.json``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from latent_beliefs.ckd.cases import CASES_PATH, PROFILE_FEATURES, read_cases
from latent_beliefs.config import PROMPTS_DIR, ensure

CKD_PROMPTS_DIR = PROMPTS_DIR / "ckd"
TASKS_PATH = CKD_PROMPTS_DIR / "tasks.json"
ANALYSED_COHORT = "original500"

# ---------------------------------------------------------------- patient profile
FEATURE_KEYS = {
    "hypertension_self_reported": "htn",
    "diabetes_self_reported": "diab",
    "indfmin2": "inc",
    "cvd_any": "cvd",
    "ridageyr": "age",
    "ridreth3": "rce",
    "dmdeduc2": "edu",
    "physical_activity_any": "pa",
    "smoking_status_current": "smkstat",
    "riagendr": "sex",
    "bmxbmi": "bmi",
    "sodium_mg_mean": "sodium",
}
FEATURE_TEXT = {
    "hypertension_self_reported": "Self-reported hypertension ({key}) is {value}.",
    "diabetes_self_reported": "Self-reported diabetes ({key}) is {value}.",
    "indfmin2": "Household income bracket ({key}) is {value}.",
    "cvd_any": "History of cardiovascular disease ({key}) is {value}.",
    "ridageyr": "Age ({key}) is {value} years.",
    "ridreth3": "Race/ethnicity ({key}) is {value}.",
    "dmdeduc2": "Education level ({key}) is {value}.",
    "physical_activity_any": "Any physical activity indicator ({key}) is {value}.",
    "smoking_status_current": "Current smoking status ({key}) is {value}.",
    "riagendr": "Sex ({key}) is {value}.",
    "bmxbmi": "Body mass index ({key}) is {value}.",
    "sodium_mg_mean": "Estimated daily sodium intake ({key}) is {value}.",
}
FEATURE_UNITS = {"bmxbmi": "kg/m^2", "sodium_mg_mean": "mg/day"}
NO_YES = {0: "No", 1: "Yes"}
VALUE_LABELS = {
    "hypertension_self_reported": NO_YES,
    "diabetes_self_reported": NO_YES,
    "cvd_any": NO_YES,
    "physical_activity_any": NO_YES,
    "riagendr": {1: "Male", 2: "Female"},
    "ridreth3": {
        1: "Mexican American",
        2: "Other Hispanic",
        3: "Non-Hispanic White",
        4: "Non-Hispanic Black",
        6: "Non-Hispanic Asian",
        7: "Other race, including multiracial",
    },
    "dmdeduc2": {
        1: "Less than 9th grade",
        2: "9-11th grade (includes 12th grade with no diploma)",
        3: "High school graduate/GED or equivalent",
        4: "Some college or AA degree",
        5: "College graduate or above",
        7: "Refused",
        9: "Don't know",
    },
    "smoking_status_current": {
        1: "Every day",
        2: "Some days",
        3: "Not at all",
        7: "Refused",
        9: "Don't know",
    },
    "indfmin2": {
        1: "$0 to $4,999",
        2: "$5,000 to $9,999",
        3: "$10,000 to $14,999",
        4: "$15,000 to $19,999",
        5: "$20,000 to $24,999",
        6: "$25,000 to $34,999",
        7: "$35,000 to $44,999",
        8: "$45,000 to $54,999",
        9: "$55,000 to $64,999",
        10: "$65,000 to $74,999",
        12: "$20,000 and over",
        13: "Under $20,000",
        14: "$75,000 to $99,999",
        15: "$100,000 and over",
        77: "Refused",
        99: "Don't know",
    },
}


def _format_value(value: Any) -> str:
    if pd.isna(value):
        return "Missing"
    text = str(value).strip()
    return text if text else "Missing"


def _as_int_code(value: Any) -> int | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    iv = int(v)
    if abs(v - iv) > 1e-9:
        return None
    return iv


def format_feature_value(feature: str, value: Any) -> str:
    if pd.isna(value):
        return "Missing"
    labels = VALUE_LABELS.get(feature)
    if labels is not None:
        code = _as_int_code(value)
        if code is None:
            return _format_value(value)
        return labels.get(code, f"Unknown code {code}")
    return _format_value(value)


def patient_profile(case: pd.Series) -> str:
    lines = []
    for feature in PROFILE_FEATURES:
        value = format_feature_value(feature, case[feature])
        if feature in FEATURE_UNITS and value != "Missing":
            value = f"{value} {FEATURE_UNITS[feature]}"
        lines.append(FEATURE_TEXT[feature].format(value=value, key=FEATURE_KEYS[feature]))
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- prompt templates
def quantile(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def fmt_pct(value: float) -> str:
    return f"{100.0 * value:.2f}%"


def reasoning_choice_prompt(question: str, options: list[str], profile: str) -> str:
    option_lines = "\n".join(f"{chr(65 + idx)}. {option}" for idx, option in enumerate(options))
    return (
        f"{question}\n\n"
        "Provide your reasoning first.\n"
        "Then output exactly one multiple-choice answer on a new line in this exact format:\n"
        "Choice: <one option exactly as written below>\n\n"
        "Options:\n"
        f"{option_lines}\n\n"
        "Patient profile:\n"
        f"{profile}\n"
    )


def probability_prompt(profile: str) -> str:
    return (
        "Estimate the probability that this patient has chronic kidney disease (CKD).\n\n"
        "Provide your reasoning first.\n"
        "Then output valid JSON on a new line in exactly this structure:\n"
        '{"probability": <number>}\n\n'
        "The number must be between 0 and 1.\n\n"
        "Patient profile:\n"
        f"{profile}\n"
    )


def decision_prompt(profile: str) -> str:
    return reasoning_choice_prompt(
        "Decide whether this patient should be screened for chronic kidney disease (CKD).",
        ["Recommend screening for CKD", "Recommend not screening for CKD"],
        profile,
    )


def threshold_prompt(profile: str, threshold_text: str) -> str:
    return reasoning_choice_prompt(
        (
            "Decide whether this patient should be screened for chronic kidney disease (CKD). "
            f"The patient should be screened if the probability that they have CKD is at least {threshold_text}."
        ),
        ["Screen this patient for CKD", "Do not screen this patient for CKD"],
        profile,
    )


def betting_prompt(profile: str, net_gain: int) -> str:
    return reasoning_choice_prompt(
        (
            "Consider the following bet:\n\n"
            "- If the bet is accepted and this patient has chronic kidney disease (CKD), "
            f"the net gain is {net_gain} points.\n"
            "- If the bet is accepted and this patient does not have chronic kidney disease "
            "(CKD), the net loss is 1 point.\n"
            "- If the bet is declined, the payoff is 0 points regardless of whether this "
            "patient has chronic kidney disease (CKD).\n\n"
            "Based on the information provided, would you recommend accepting this bet?"
        ),
        ["Accept the bet", "Decline the bet"],
        profile,
    )


def cost_prompt(profile: str, ratio: int) -> str:
    return reasoning_choice_prompt(
        (
            "Decide whether this patient should be screened for chronic kidney disease (CKD). "
            "Assume that the cost of a false negative, meaning failing to screen a patient who truly has CKD, "
            f"is {ratio} times the cost of a false positive, meaning screening a patient who does not have CKD."
        ),
        ["Recommend screening for CKD", "Recommend not screening for CKD"],
        profile,
    )


def likert_prompt(profile: str) -> str:
    return reasoning_choice_prompt(
        "How likely is this patient to have chronic kidney disease (CKD)?",
        [
            "Very unlikely",
            "Somewhat unlikely",
            "Neither likely nor unlikely",
            "Somewhat likely",
            "Very likely",
        ],
        profile,
    )


def probability_bin_prompt(profile: str, boundaries: list[float]) -> str:
    labels = [
        f"Less than {fmt_pct(boundaries[0])}",
        f"{fmt_pct(boundaries[0])} to less than {fmt_pct(boundaries[1])}",
        f"{fmt_pct(boundaries[1])} to less than {fmt_pct(boundaries[2])}",
        f"{fmt_pct(boundaries[2])} to less than {fmt_pct(boundaries[3])}",
        f"{fmt_pct(boundaries[3])} or higher",
    ]
    return reasoning_choice_prompt(
        "Which bin contains the probability that this patient has chronic kidney disease (CKD)?",
        labels,
        profile,
    )


def task_specs(boundaries: list[float]) -> list[dict[str, Any]]:
    """The sixteen families in collection order.

    ``variant_id`` is the output name; ``template_id``, ``threshold``,
    ``implied_threshold``, ``false_negative_cost_ratio``, and the betting
    points are metadata carried into each prompt row.
    """
    return [
        {
            "variant_id": "probability",
            "task_id": "ckd_same1000_probability_revised",
            "prompt_family": "probability",
            "decision_formulation": "probability_elicitation",
            "builder": probability_prompt,
        },
        {
            "variant_id": "decision_no_threshold",
            "task_id": "ckd_same1000_decision_no_threshold_revised",
            "prompt_family": "decision",
            "decision_formulation": "decision_no_threshold",
            "builder": decision_prompt,
        },
        {
            "variant_id": "template_01_threshold_10pct",
            "task_id": "ckd_same1000_template_01_threshold_10pct_revised",
            "prompt_family": "decision",
            "decision_formulation": "threshold_10pct",
            "template_id": "template_01",
            "threshold": 0.10,
            "builder": lambda profile: threshold_prompt(profile, "10%"),
        },
        {
            "variant_id": "template_02_threshold_20pct",
            "task_id": "ckd_same1000_template_02_threshold_20pct_revised",
            "prompt_family": "decision",
            "decision_formulation": "threshold_20pct",
            "template_id": "template_02",
            "threshold": 0.20,
            "builder": lambda profile: threshold_prompt(profile, "20%"),
        },
        {
            "variant_id": "template_03_threshold_33pct",
            "task_id": "ckd_same1000_template_03_threshold_33pct_revised",
            "prompt_family": "decision",
            "decision_formulation": "threshold_1_over_3",
            "template_id": "template_03",
            "threshold": 1.0 / 3.0,
            "builder": lambda profile: threshold_prompt(profile, "33.3%"),
        },
        {
            "variant_id": "bet_payoff_2to1",
            "task_id": "ckd_same1000_bet_payoff_2to1_revised",
            "prompt_family": "bet",
            "decision_formulation": "bet_payoff_2to1",
            "template_id": "template_04",
            "implied_threshold": 1.0 / 3.0,
            "net_gain_points": 2,
            "net_loss_points": 1,
            "builder": lambda profile: betting_prompt(profile, 2),
        },
        {
            "variant_id": "bet_payoff_4to1",
            "task_id": "ckd_same1000_bet_payoff_4to1_revised",
            "prompt_family": "bet",
            "decision_formulation": "bet_payoff_4to1",
            "template_id": "template_05",
            "implied_threshold": 1.0 / 5.0,
            "net_gain_points": 4,
            "net_loss_points": 1,
            "builder": lambda profile: betting_prompt(profile, 4),
        },
        {
            "variant_id": "bet_payoff_9to1",
            "task_id": "ckd_same1000_bet_payoff_9to1_revised",
            "prompt_family": "bet",
            "decision_formulation": "bet_payoff_9to1",
            "template_id": "template_06",
            "implied_threshold": 1.0 / 10.0,
            "net_gain_points": 9,
            "net_loss_points": 1,
            "builder": lambda profile: betting_prompt(profile, 9),
        },
        {
            "variant_id": "template_07_cost_fn2",
            "task_id": "ckd_same1000_template_07_cost_fn2_revised",
            "prompt_family": "decision",
            "decision_formulation": "false_negative_cost_2x",
            "template_id": "template_07",
            "implied_threshold": 1.0 / 3.0,
            "false_negative_cost_ratio": 2,
            "builder": lambda profile: cost_prompt(profile, 2),
        },
        {
            "variant_id": "template_08_cost_fn4",
            "task_id": "ckd_same1000_template_08_cost_fn4_revised",
            "prompt_family": "decision",
            "decision_formulation": "false_negative_cost_4x",
            "template_id": "template_08",
            "implied_threshold": 1.0 / 5.0,
            "false_negative_cost_ratio": 4,
            "builder": lambda profile: cost_prompt(profile, 4),
        },
        {
            "variant_id": "template_09_cost_fn9",
            "task_id": "ckd_same1000_template_09_cost_fn9_revised",
            "prompt_family": "decision",
            "decision_formulation": "false_negative_cost_9x",
            "template_id": "template_09",
            "implied_threshold": 1.0 / 10.0,
            "false_negative_cost_ratio": 9,
            "builder": lambda profile: cost_prompt(profile, 9),
        },
        {
            "variant_id": "threshold_05pct",
            "task_id": "ckd_same1000_threshold_05pct_revised",
            "prompt_family": "decision",
            "decision_formulation": "threshold_05pct",
            "threshold": 0.05,
            "builder": lambda profile: threshold_prompt(profile, "5%"),
        },
        {
            "variant_id": "bet_payoff_19to1",
            "task_id": "ckd_same1000_bet_payoff_19to1_revised",
            "prompt_family": "bet",
            "decision_formulation": "bet_payoff_19to1",
            "implied_threshold": 1.0 / 20.0,
            "net_gain_points": 19,
            "net_loss_points": 1,
            "builder": lambda profile: betting_prompt(profile, 19),
        },
        {
            "variant_id": "cost_fn19",
            "task_id": "ckd_same1000_cost_fn19_revised",
            "prompt_family": "decision",
            "decision_formulation": "false_negative_cost_19x",
            "implied_threshold": 1.0 / 20.0,
            "false_negative_cost_ratio": 19,
            "builder": lambda profile: cost_prompt(profile, 19),
        },
        {
            "variant_id": "likert",
            "task_id": "ckd_same1000_likert_revised",
            "prompt_family": "likert",
            "decision_formulation": "likert_ckd_likelihood",
            "builder": likert_prompt,
        },
        {
            "variant_id": "probability_bin_quintile",
            "task_id": "ckd_same1000_probability_bin_quintile_revised",
            "prompt_family": "probability_bin",
            "decision_formulation": "true_risk_quintile_bin",
            "probability_bin_boundaries": boundaries,
            "builder": lambda profile: probability_bin_prompt(profile, boundaries),
        },
    ]


def source_rows(cases: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for _, case in cases.iterrows():
        rows.append(
            {
                "source_prompt_id": f"{case['source_cohort']}__{case['prompt_id']}",
                "base_source_prompt_id": str(case["prompt_id"]),
                "source_cohort": case["source_cohort"],
                "row_index": int(case["row_index"]),
                "seqn": int(case["seqn"]),
                "risk_estimate": float(case["risk_estimate"]),
                "risk_stratum": case["risk_stratum"],
                "ckd": int(case["ckd"]),
                "patient_profile": patient_profile(case),
            }
        )
    rows.sort(key=lambda row: (row["source_cohort"], row["base_source_prompt_id"]))
    return rows


def prompt_row(source: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    row = {
        "prompt_id": f"{source['source_prompt_id']}__{spec['variant_id']}",
        "task_id": spec["task_id"],
        "prompt_variant_id": spec["variant_id"],
        "prompt_family": spec["prompt_family"],
        "decision_formulation": spec["decision_formulation"],
        "template_id": spec.get("template_id", ""),
        "threshold": spec.get("threshold", ""),
        "implied_threshold": spec.get("implied_threshold", ""),
        "false_negative_cost_ratio": spec.get("false_negative_cost_ratio", ""),
        "probability_bin_boundaries": json.dumps(spec.get("probability_bin_boundaries", [])),
        "source_prompt_id": source["source_prompt_id"],
        "base_source_prompt_id": source["base_source_prompt_id"],
        "source_cohort": source["source_cohort"],
        "row_index": source["row_index"],
        "seqn": source["seqn"],
        "risk_estimate": source["risk_estimate"],
        "risk_stratum": source["risk_stratum"],
        "ckd": source["ckd"],
        "patient_profile": source["patient_profile"],
        "prompt": spec["builder"](source["patient_profile"]),
    }
    if "net_gain_points" in spec:
        row["net_gain_points"] = spec["net_gain_points"]
        row["net_loss_points"] = spec["net_loss_points"]
    return row


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build(cases: pd.DataFrame, out_dir: Path) -> None:
    rows = source_rows(cases)
    # Bin edges use the risk scores of both cohorts.
    risks = sorted(float(row["risk_estimate"]) for row in rows)
    boundaries = [quantile(risks, q) for q in (0.2, 0.4, 0.6, 0.8)]
    analysed = [row for row in rows if row["source_cohort"] == ANALYSED_COHORT]

    ensure(out_dir)
    tasks = []
    for spec in task_specs(boundaries):
        path = out_dir / f"{spec['task_id']}.jsonl"
        write_jsonl(path, [prompt_row(row, spec) for row in analysed])
        tasks.append({"task_id": spec["task_id"], "output": spec["variant_id"], "path": path.name})
    (out_dir / TASKS_PATH.name).write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")


def load_tasks(prompts_dir: Path = CKD_PROMPTS_DIR) -> list[dict[str, str]]:
    """``tasks.json`` entries with ``path`` resolved."""
    tasks = json.loads((prompts_dir / TASKS_PATH.name).read_text(encoding="utf-8"))
    return [{**task, "path": str(prompts_dir / task["path"])} for task in tasks]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--out-dir", type=Path, default=CKD_PROMPTS_DIR)
    args = parser.parse_args()
    build(read_cases(args.cases), args.out_dir)
    print(f"Wrote 16 prompt files and {TASKS_PATH.name} to {args.out_dir}")


if __name__ == "__main__":
    main()
