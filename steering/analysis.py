"""Tables of the steering experiment (CPU only).

The leak is the binding experiment's estimator. Beliefs are clipped logits. For each patient, ``x`` is the gap
between the model's own belief from the full record (prompt A) and from PERSON's record (prompt B). Over
patients,

    lambda = slope of (C - M) on x  /  slope of (D - M) on x

where C is the answer about PERSON when only the model has the extra evidence, M the answer when neither has
it and D the answer when both have it. 0: the answer follows PERSON's record; 1: it follows the model's.

An edited prompt's lambda uses the edited C with the unedited references M and D; where M and D were also run
under the edit, lambda and the reference slope (beta_D) with those are reported too. ``fraction`` places an
edited cell between its own unedited lambda (0) and the unedited lambda of the same page order in the other
arm of its pair (1); it is left blank where those two differ by less than ``MIN_GAP``. Intervals are 95%
percentile intervals of a bootstrap over patients.

    python -m steering.run analyse --model qwen3_8b --out runs/steering [--session1 beliefs_direct.parquet]
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import binding
from second_order import config
from second_order.beliefs import clip_logit
from steering import prompts
from steering import transcripts

# The columns that together name one edit of one cell.
EDIT_COLUMNS = ["part", "arm", "block_order", "variant", "alpha", "lo", "hi", "donor"]
# A cell needs at least this many complete patients to get a row.
MIN_CASES = 8
# ``fraction`` is reported only where the two arms' unedited lambdas differ by at least this.
MIN_GAP = 0.2
CITATION_CATEGORIES = ("own only", "both", "person only", "neither")


def read_rows(folder: Path) -> pd.DataFrame:
    """Every per-patient file of a stage's folder as one table (empty if the folder has none)."""
    files = sorted(folder.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)


def interval(draws) -> tuple[float, float]:
    """The 95% percentile interval of bootstrap draws, ignoring draws that are not finite."""
    finite = np.asarray(draws, float)
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return (np.nan, np.nan)
    return tuple(np.percentile(finite, [2.5, 97.5]))


def slopes(outcome, own_gap, draws=None):
    """Least-squares slope of ``outcome`` on ``own_gap`` over patients (the last axis). With ``draws``,
    bootstrap indices [resamples, patients]: one slope per resampled set of patients."""
    if draws is not None:
        outcome, own_gap = outcome[..., draws], own_gap[draws]
    centred_gap = own_gap - own_gap.mean(-1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        centred_outcome = outcome - outcome.mean(-1, keepdims=True)
        return (centred_outcome * centred_gap).sum(-1) / (centred_gap ** 2).sum(-1)


def lam(belief_c, belief_m, belief_d, own_gap, draws=None):
    """The leak: the slope of C - M on the patient's own belief gap over the slope of D - M on it. With
    ``draws``, one value per bootstrap resample."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return slopes(belief_c - belief_m, own_gap, draws) / slopes(belief_d - belief_m, own_gap, draws)


def cell(beliefs: pd.DataFrame, arm: str, order: str) -> pd.DataFrame:
    """One cell (prompt arm and page order), one row per patient: ``x``, the patient's own belief gap (A minus
    B), and the beliefs under C, M and D. Patients missing any of them are left out."""
    first_replicate = beliefs[(beliefs.arm == arm) & (beliefs.replicate == 0)]
    own = first_replicate[first_replicate.block_order == "shared"]
    own = own.pivot_table(index="case_id", columns="role", values="value")
    attributed = first_replicate[first_replicate.block_order == order]
    attributed = attributed.pivot_table(index="case_id", columns="role", values="value")
    if not {"A", "B"} <= set(own) or not {"C", "M", "D"} <= set(attributed):
        return pd.DataFrame(columns=["x", "C", "M", "D"])
    return attributed[["C", "M", "D"]].assign(x=own.A - own.B).dropna()


def as_beliefs(rows: pd.DataFrame) -> pd.DataFrame:
    """A stage's belief rows as the estimator reads them: one clipped logit per patient, arm, page order,
    prompt role and replicate."""
    belief_rows = rows[rows.readout == "belief"]
    belief_rows = belief_rows.assign(value=clip_logit(belief_rows.value))
    return belief_rows[["case_id", "split", "arm", "block_order", "role", "replicate", "value"]]


# Unedited cells
def baseline_cells(beliefs: pd.DataFrame, arms, resamples: int, seed: int, label: str) -> pd.DataFrame:
    """The leak of the unedited prompts: one row per arm and page order, and one row per arm for the order
    effect (PERSON's record first minus the model's first). Each arm uses the patients complete in both
    orders."""
    rng, rows = np.random.default_rng(seed), []
    for arm in arms:
        cells = {order: cell(beliefs, arm, order) for order in binding.ORDERS}
        common = sorted(set.intersection(*(set(frame.index) for frame in cells.values())))
        if len(common) < MIN_CASES:
            continue
        draws = rng.integers(0, len(common), (resamples, len(common)))
        leak_draws, leak = {}, {}
        for order, frame in cells.items():
            frame = frame.loc[common]
            own_gap = frame.x.to_numpy()
            c_minus_m = (frame.C - frame.M).to_numpy()
            d_minus_m = (frame.D - frame.M).to_numpy()
            reference_slope_draws = slopes(d_minus_m, own_gap, draws)
            leak_draws[order] = slopes(c_minus_m, own_gap, draws) / reference_slope_draws
            leak[order] = slopes(c_minus_m, own_gap) / slopes(d_minus_m, own_gap)
            lower, upper = interval(leak_draws[order])
            slope_lower, slope_upper = interval(reference_slope_draws)
            rows.append({"source": label, "arm": arm, "order": order, "n": len(common), "lambda": leak[order],
                         "ci_lower": lower, "ci_upper": upper, "beta_D": slopes(d_minus_m, own_gap),
                         "beta_D_lower": slope_lower, "beta_D_upper": slope_upper,
                         "median_abs_x": float(np.median(np.abs(own_gap)))})
        lower, upper = interval(leak_draws["person_first"] - leak_draws["own_first"])
        order_effect = leak["person_first"] - leak["own_first"]
        rows.append({"source": label, "arm": arm, "order": "person_first - own_first", "n": len(common),
                     "lambda": order_effect, "ci_lower": lower, "ci_upper": upper})
    return pd.DataFrame(rows)


def lookup_wide(rows: pd.DataFrame) -> pd.DataFrame:
    """Per edit and patient: the lookup margin for each record asked about, averaged over the fields asked. A
    margin is the logit of PERSON's value minus the model's: positive is right when PERSON's record is asked
    about (column ``person``), negative when the model's own is (column ``own``)."""
    lookups = rows[rows.readout == "lookup"]
    if lookups.empty:
        return pd.DataFrame()
    index = EDIT_COLUMNS + ["case_id"]
    return lookups.pivot_table(index=index, columns="query", values="value", aggfunc="mean").reset_index()


def lookup_cells(rows: pd.DataFrame, resamples: int, seed: int) -> pd.DataFrame:
    """One row per edit and cell: for each record asked about, the mean lookup margin with its interval and
    the share of patients answered from the right record; and ``binding``, half the difference between the two
    margins (high when each question finds its own record, zero when both find the same one)."""
    wide, rng, table_rows = lookup_wide(rows), np.random.default_rng(seed + 1), []
    if wide.empty:
        return wide
    for key, group in wide.groupby(EDIT_COLUMNS, dropna=False):
        draws = rng.integers(0, len(group), (resamples, len(group)))
        row = dict(zip(EDIT_COLUMNS, key), n=len(group))
        for query in prompts.QUERIES:
            if query not in group:
                continue
            margins = group[query].to_numpy()
            lower, upper = interval(margins[draws].mean(1))
            right = margins > 0 if query == "person" else margins < 0
            row |= {f"{query}_margin": margins.mean(), f"{query}_lower": lower, f"{query}_upper": upper,
                    f"{query}_right": right.mean()}
        if all(query in group for query in prompts.QUERIES):
            binding_margin = (group.person - group.own).to_numpy() / 2
            lower, upper = interval(binding_margin[draws].mean(1))
            row |= {"binding": binding_margin.mean(), "binding_lower": lower, "binding_upper": upper,
                    "shared_bias": ((group.person + group.own) / 2).mean()}
        table_rows.append(row)
    return pd.DataFrame(table_rows)


# Edited cells
def edited_cells(base: pd.DataFrame, edited: pd.DataFrame, pairs: dict, resamples: int,
                 seed: int) -> pd.DataFrame:
    """One row per edit and cell with the leak under the edit. ``base``: the unedited beliefs (as
    ``as_beliefs`` gives them); ``edited``: the stage rows of the edits; ``pairs``: variant -> (unlabelled
    arm, labelled arm)."""
    other_arm = {arm: partner for pair in pairs.values() for arm, partner in (pair, pair[::-1])}
    edited_beliefs = edited[edited.readout == "belief"].assign(value=lambda frame: clip_logit(frame.value))
    rng, rows = np.random.default_rng(seed + 2), []
    for (arm, order), cell_rows in edited_beliefs.groupby(["arm", "block_order"]):
        unedited = cell(base, arm, order)
        other = cell(base, other_arm[arm], order) if arm in other_arm else unedited.iloc[:0]
        for key, edit_rows in cell_rows.groupby(EDIT_COLUMNS, dropna=False):
            by_role = edit_rows.pivot_table(index="case_id", columns="role", values="value")
            if "C" not in by_role:
                continue
            cases = sorted(set(unedited.index) & set(by_role.index[by_role["C"].notna()]))
            if len(cases) < MIN_CASES:
                continue
            draws = rng.integers(0, len(cases), (resamples, len(cases)))
            row = dict(zip(EDIT_COLUMNS, key), n=len(cases))
            row |= edited_leak(unedited.loc[cases], by_role.loc[cases], other, cases, draws)
            rows.append(row)
    return pd.DataFrame(rows)


def edited_leak(unedited: pd.DataFrame, by_role: pd.DataFrame, other: pd.DataFrame, cases: list,
                draws) -> dict:
    """The leak columns of one ``edited_cells`` row (the module docstring defines them). ``unedited``: the
    cell's patients with x, C, M, D; ``by_role``: the same patients' beliefs under the edit; ``other``: the
    same page order in the other arm."""
    own_gap, belief_c, belief_m, belief_d = (unedited[column].to_numpy() for column in ("x", "C", "M", "D"))
    leak_unedited = lam(belief_c, belief_m, belief_d, own_gap)
    leak_unedited_draws = lam(belief_c, belief_m, belief_d, own_gap, draws)
    has_other = set(cases) <= set(other.index)
    if has_other:
        other_columns = [other.loc[cases, column].to_numpy() for column in ("C", "M", "D", "x")]
        leak_other, leak_other_draws = lam(*other_columns), lam(*other_columns, draws)
    edited_c = by_role["C"].to_numpy()
    leak_edited = lam(edited_c, belief_m, belief_d, own_gap)
    leak_edited_draws = lam(edited_c, belief_m, belief_d, own_gap, draws)

    row = {"lambda_unedited": leak_unedited, "lambda": leak_edited}
    row["ci_lower"], row["ci_upper"] = interval(leak_edited_draws)
    row["change"] = leak_edited - leak_unedited
    row["change_lower"], row["change_upper"] = interval(leak_edited_draws - leak_unedited_draws)
    row["mean_shift_C"] = float((edited_c - belief_c).mean())
    if has_other:
        row["lambda_other_arm"] = leak_other
    if has_other and abs(leak_other - leak_unedited) >= MIN_GAP:
        # how far the edit takes this cell towards the other arm's unedited leak
        with np.errstate(divide="ignore", invalid="ignore"):
            row["fraction"] = (leak_edited - leak_unedited) / (leak_other - leak_unedited)
            change_draws = leak_edited_draws - leak_unedited_draws
            row["fraction_lower"], row["fraction_upper"] = interval(
                change_draws / (leak_other_draws - leak_unedited_draws))
    if {"M", "D"} <= set(by_role) and by_role[["M", "D"]].notna().all().all():
        # the references were run under the edit too: does the edit leave them tracking the evidence?
        edited_m, edited_d = by_role["M"].to_numpy(), by_role["D"].to_numpy()
        row["beta_D_unedited"] = slopes(belief_d - belief_m, own_gap)
        row["beta_D_edited"] = slopes(edited_d - edited_m, own_gap)
        slope_draws = slopes(edited_d - edited_m, own_gap, draws)
        row["beta_D_edited_lower"], row["beta_D_edited_upper"] = interval(slope_draws)
        row["lambda_edited_refs"] = lam(edited_c, edited_m, edited_d, own_gap)
        refs_draws = lam(edited_c, edited_m, edited_d, own_gap, draws)
        row["lambda_edited_refs_lower"], row["lambda_edited_refs_upper"] = interval(refs_draws)
        row["mean_shift_M"] = float((edited_m - belief_m).mean())
        row["mean_shift_D"] = float((edited_d - belief_d).mean())
    return row


def dose_trend(base: pd.DataFrame, dose: pd.DataFrame, resamples: int, seed: int) -> pd.DataFrame:
    """Per cell and vector: the slope of the leak on the edit's strength, with the unedited prompt as strength
    0. Needs at least three strengths."""
    edited_c = dose[(dose.readout == "belief") & (dose.part == "dose") & (dose.role == "C")]
    edited_c = edited_c.assign(value=lambda frame: clip_logit(frame.value))
    rng, rows = np.random.default_rng(seed + 3), []
    for (arm, order, variant), group in edited_c.groupby(["arm", "block_order", "variant"]):
        unedited = cell(base, arm, order)
        by_strength = group.pivot_table(index="case_id", columns="alpha", values="value").dropna()
        cases = sorted(set(unedited.index) & set(by_strength.index))
        if len(cases) < MIN_CASES or by_strength.shape[1] < 3:
            continue
        draws = rng.integers(0, len(cases), (resamples, len(cases)))
        own_gap, belief_c, belief_m, belief_d = (unedited.loc[cases, column].to_numpy()
                                                 for column in ("x", "C", "M", "D"))
        strengths = np.array([0.0] + list(by_strength.columns))
        # [strengths, patients], the unedited prompt first
        edited = [by_strength.loc[cases, alpha].to_numpy() for alpha in by_strength.columns]
        beliefs = np.vstack([belief_c] + edited)
        leak = lam(beliefs, belief_m, belief_d, own_gap)                    # [strengths]
        leak_draws = lam(beliefs, belief_m, belief_d, own_gap, draws)       # [strengths, resamples]
        lower, upper = interval(slopes(leak_draws.T, strengths))
        # Spearman's rank correlation, without scipy
        rank_correlation = pd.Series(leak).rank().corr(pd.Series(strengths).rank())
        rows.append(dict(arm=arm, block_order=order, variant=variant, n=len(cases),
                         lambda_per_alpha=slopes(leak, strengths), ci_lower=lower, ci_upper=upper,
                         spearman=rank_correlation, lambda_min=leak.min(), lambda_max=leak.max()))
    return pd.DataFrame(rows)


# Reasoned answers
def reason_cells(reason: pd.DataFrame, prompt_table: pd.DataFrame, session1: pd.DataFrame | None,
                 resamples: int, seed: int) -> pd.DataFrame:
    """Sampled replies per cell and edit: how many parsed, which record's disputed values they cite, and (with
    ``session1``, reference beliefs to take x, M and D from) the leak of the stated probability."""
    prompt_of = {(row.case_id, row.arm, row.block_order): row.prompt
                 for row in prompt_table[prompt_table.role == "C"].itertuples()}
    replies = reason.copy()
    replies["cites"] = [transcripts.category(text, prompt_of[(case, arm, order)]) for text, case, arm, order
                        in zip(replies.text, replies.case_id, replies.arm, replies.block_order)]
    replies["logit"] = clip_logit(replies.value.fillna(0.5))
    replies.loc[replies.value.isna(), "logit"] = np.nan
    rng, rows = np.random.default_rng(seed + 4), []
    group_columns = ["arm", "block_order", "variant", "alpha"]
    for key, group in replies.groupby(group_columns, dropna=False):
        row = dict(zip(group_columns, key), patients=group.case_id.nunique(), answers=len(group),
                   parsed=group.value.notna().mean(), truncated=group.truncated.mean(),
                   median_tokens=group.n_tokens.median())
        for name in CITATION_CATEGORIES:
            row[f"cites {name}"] = (group.cites == name).mean()
        if session1 is not None:
            row |= reasoned_leak(group, cell(session1, key[0], key[1]), resamples, rng)
        rows.append(row)
    return pd.DataFrame(rows)


def reasoned_leak(replies: pd.DataFrame, references: pd.DataFrame, resamples: int, rng) -> dict:
    """The leak of one cell's sampled replies (each patient's mean stated logit as C) against ``references``
    for x, M and D; ``lambda_session1`` is the references' own leak on the same patients. Empty with too few
    patients."""
    stated = replies.groupby("case_id").logit.mean().dropna()
    cases = sorted(set(references.index) & set(stated.index))
    if len(cases) < MIN_CASES:
        return {}
    draws = rng.integers(0, len(cases), (resamples, len(cases)))
    own_gap, belief_m, belief_d = (references.loc[cases, column].to_numpy() for column in ("x", "M", "D"))
    stated_c = stated.loc[cases].to_numpy()
    lower, upper = interval(lam(stated_c, belief_m, belief_d, own_gap, draws))
    return {"lambda": lam(stated_c, belief_m, belief_d, own_gap), "ci_lower": lower, "ci_upper": upper,
            "lambda_session1": lam(references.loc[cases, "C"].to_numpy(), belief_m, belief_d, own_gap)}


def branch_table(branch: pd.DataFrame) -> pd.DataFrame:
    """Per cell, what the original reply first stated and where it was cut: the share of continuations in each
    citation category and the share that go on to state the model's value first."""
    group_columns = ["arm", "block_order", "source_first", "cut"]
    rows = []
    for key, group in branch.groupby(group_columns):
        row = dict(zip(group_columns, key), replies=group.groupby(["case_id", "source_sample"]).ngroups,
                   continuations=len(group), parsed=group.value.notna().mean())
        for name in CITATION_CATEGORIES:
            row[f"cites {name}"] = (group.category == name).mean()
        row["first states model's"] = (group["first"] == "model's").mean()
        row["median stated probability"] = group.value.median()
        rows.append(row)
    return pd.DataFrame(rows)


def decision_table(decision: pd.DataFrame) -> pd.DataFrame:
    """Per cell, what the reply wrote, and condition. ``p_own``: the model's probability of its own record's
    value against PERSON's at the token where the reply first writes a disputed value; ``favours_own``: the
    share of replies where it is above one half; ``position``: how far into the reasoning that token is."""
    probes = decision.copy()
    probes["p_own"] = 1 / (1 + np.exp(-probes.value))
    attention_columns = sorted(column for column in probes.columns if column.startswith("att_field_"))
    group_columns = ["arm", "block_order", "wrote", "variant", "alpha"]
    rows = []
    for key, group in probes.groupby(group_columns):
        row = dict(zip(group_columns, key), replies=len(group), p_own=group.p_own.mean(),
                   p_own_median=group.p_own.median(), favours_own=(group.value > 0).mean(),
                   margin=group.value.mean(), position=group.position.median())
        if attention_columns and group[attention_columns].notna().any().any():
            row |= attention_to_second(group, attention_columns)
        rows.append(row)
    return pd.DataFrame(rows)


def attention_to_second(probes: pd.DataFrame, attention_columns: list[str]) -> dict:
    """Of the attention to the disputed field's two lines, the mean share on the second record's line: over
    every layer window, and over the two windows from the middle one on, where the answer reads the records.
    Columns are ``att_field_<block>_<lo>_<hi>``."""
    first, second = (probes[[column for column in attention_columns if f"_{block}_" in column]].sum(1)
                     for block in ("first", "second"))
    windows = sorted({tuple(map(int, column.rsplit("_", 2)[1:])) for column in attention_columns})
    middle = len(windows) // 2
    read_windows = windows[middle: middle + 1] + windows[middle + 1: middle + 2]
    read_columns = {block: [f"att_field_{block}_{low}_{high}" for low, high in read_windows]
                    for block in ("first", "second")}
    first_read, second_read = probes[read_columns["first"]].sum(1), probes[read_columns["second"]].sum(1)
    return {"attention_to_second": float((second / (first + second)).mean()),
            "attention_to_second_read_layers": float((second_read / (first_read + second_read)).mean())}


# Everything a run has, as tables and a report
def markdown_table(frame: pd.DataFrame | None, columns=None, digits: int = 2) -> str:
    if frame is None or frame.empty:
        return "(none)\n"
    shown = frame[columns] if columns else frame
    shown = shown.copy()
    for column in shown.select_dtypes("float"):
        shown[column] = shown[column].map(lambda value: "" if pd.isna(value) else f"{value:.{digits}f}")
    lines = ["| " + " | ".join(map(str, shown.columns)) + " |", "|" + "---|" * len(shown.columns)]
    lines += ["| " + " | ".join(map(str, row)) + " |" for row in shown.itertuples(index=False)]
    return "\n".join(lines) + "\n"


def analyse(run_dir: Path, cfg: dict, resamples: int = 2000, session1: Path | None = None) -> dict:
    """Every table the run's folders support, written to ``analysis/<name>.csv`` with a ``RESULTS.md`` beside
    them. ``session1``: reference beliefs (from reasoned replies) for the leak of the sampled answers."""
    seed, out_dir = int(config.load()["seed"]), run_dir / "analysis"
    out_dir.mkdir(exist_ok=True)
    baseline_rows = read_rows(run_dir / "baseline")
    if baseline_rows.empty:
        raise ValueError(f"no baseline in {run_dir}")
    session1_beliefs = pd.read_parquet(session1) if session1 else None

    tables = unedited_tables(baseline_rows, cfg, session1_beliefs, resamples, seed)
    tables |= sweep_tables(run_dir, cfg, as_beliefs(baseline_rows), resamples, seed)
    tables |= reply_tables(run_dir, session1_beliefs, resamples, seed)
    tables |= other_module_tables(run_dir, cfg)
    for name, frame in tables.items():
        frame.to_csv(out_dir / f"{name}.csv", index=False)

    belief_rows = baseline_rows[baseline_rows.readout == "belief"]
    checks = {"baseline_median_digit_mass": float(belief_rows.digit_mass.median())}
    (out_dir / "checks.json").write_text(json.dumps(checks, indent=1))
    reliability_file = run_dir / "vector_reliability.csv"
    reliability = pd.read_csv(reliability_file) if reliability_file.exists() else pd.DataFrame()
    write_results(out_dir / "RESULTS.md", run_dir.name, tables, checks, reliability, resamples)
    return tables


def unedited_tables(baseline_rows: pd.DataFrame, cfg: dict, session1_beliefs: pd.DataFrame | None,
                    resamples: int, seed: int) -> dict:
    baseline = as_beliefs(baseline_rows)
    arms = [arm for pair in cfg["pairs"].values() for arm in pair]
    by_source = []
    for split in ("all", "fit", "test"):
        of_split = baseline if split == "all" else baseline[baseline.split == split]
        by_source.append(baseline_cells(of_split, arms, resamples, seed, f"no reasoning, {split}"))
    if session1_beliefs is not None:
        same_patients = session1_beliefs[session1_beliefs.case_id.isin(set(baseline.case_id))]
        by_source.append(baseline_cells(same_patients, arms, resamples, seed, "session 1, reasoning"))
    return {"baseline_cells": pd.concat(by_source, ignore_index=True),
            "lookup_baseline": lookup_cells(baseline_rows, resamples, seed)}


def sweep_tables(run_dir: Path, cfg: dict, baseline: pd.DataFrame, resamples: int, seed: int) -> dict:
    sweep_stages = ("dose", "controls", "probe")
    sweeps = pd.concat([read_rows(run_dir / stage) for stage in sweep_stages], ignore_index=True)
    if sweeps.empty:
        return {}
    pairs = {variant: tuple(pair) for variant, pair in cfg["pairs"].items()}
    test_half = baseline[baseline.split == cfg["intervene_split"]]
    return {"edited_cells": edited_cells(test_half, sweeps, pairs, resamples, seed),
            "lookup_edited": lookup_cells(sweeps, resamples, seed),
            "dose_trend": dose_trend(test_half, sweeps, resamples, seed)}


def reply_tables(run_dir: Path, session1_beliefs: pd.DataFrame | None, resamples: int, seed: int) -> dict:
    tables = {}
    reason_folders = [folder for folder in sorted(run_dir.glob("reason*")) if folder.is_dir()]
    reason_rows = [read_rows(folder) for folder in reason_folders] or [pd.DataFrame()]
    reason = pd.concat(reason_rows, ignore_index=True)
    if not reason.empty:
        reply_patients = prompts.patients().loc[sorted(set(reason.case_id))]
        prompt_table = prompts.build(reply_patients, sorted(set(reason.arm)))
        tables["reason_cells"] = reason_cells(reason, prompt_table, session1_beliefs, resamples, seed)
    branch = read_rows(run_dir / "branch")
    if not branch.empty:
        tables["branch"] = branch_table(branch)
    decision = read_rows(run_dir / "decision")
    if not decision.empty:
        tables["decision"] = decision_table(decision)
    return tables


def other_module_tables(run_dir: Path, cfg: dict) -> dict:
    """The tables of the circuit and program stages whose folders exist. Their modules are imported here, so
    that the tables above need none of them."""
    tables = {}
    if (run_dir / "circuit").is_dir():
        from steering import circuit
        tables |= circuit.tables(run_dir, cfg)
        reply = circuit.reply_table(run_dir, cfg)
        if not reply.empty:
            tables["circuit_reply"] = reply
        tables |= circuit.algorithm_tables(run_dir)
        tables |= circuit.concrete_tables(run_dir, cfg)
    program_folders = ("circuit_program", "program_reason", "circuit_reply_headers",
                       "circuit_reply_headers_cross", "circuit_reply_headers_lines")
    if "head_sets" in cfg and any((run_dir / folder).is_dir() for folder in program_folders):
        from steering import program
        tables |= program.tables(run_dir, cfg)
    if (run_dir / "circuit_tables").is_dir():
        from steering import head_tables
        tables |= head_tables.tables(run_dir, cfg)
    if (run_dir / "circuit_tables_reply").is_dir():
        from steering import head_tables
        tables |= head_tables.reply_tables(run_dir, cfg)
    if (run_dir / "circuit_match").is_dir():
        from steering import circuit_steps
        tables |= circuit_steps.tables(run_dir, cfg)
    if (run_dir / "circuit_phrase").is_dir():
        from steering import circuit_steps
        tables |= circuit_steps.phrase_tables(run_dir, cfg)
    if (run_dir / "circuit_tag").is_dir():
        from steering import circuit_steps
        tables |= circuit_steps.tag_tables(run_dir)
    return tables


def write_results(path: Path, model: str, tables: dict, checks: dict, reliability: pd.DataFrame,
                  resamples: int) -> None:
    text = [f"# Steering experiment: {model}\n",
            "Numbers only. lambda: 0 = PERSON's belief follows PERSON's record, 1 = it follows the model's. "
            f"Intervals: 95% percentile, paired patient bootstrap ({resamples} draws). Lookup margins are "
            "logits of PERSON's value minus the model's.\n",
            "## Checks\n", "```\n" + json.dumps(checks, indent=1) + "\n```\n",
            "## Unedited cells\n", markdown_table(tables.get("baseline_cells")),
            "## Record lookups, unedited\n", markdown_table(tables.get("lookup_baseline"), None)]
    text += sweep_sections(tables)
    titles = {"reason_cells": "## Answers with reasoning\n",
              "branch": "## Replies resampled from before and after their first disputed value\n",
              "decision": "## The token where a reply first writes a disputed value\n",
              "circuit_faith":
                  "## Lookup heads: share of the answer a head set restores (keep) or moves (swap)\n"}
    for name, title in titles.items():
        if name in tables:
            text += [title, markdown_table(tables[name])]
    if "circuit_summary" in tables:
        text += ["## Lookup heads: concentration of the single-head effects\n",
                 markdown_table(tables["circuit_summary"]),
                 "### The 30 heads with the largest effect\n",
                 markdown_table(tables["circuit_heads"].head(30))]
    if not reliability.empty:
        text += ["## Vectors: norm relative to the residual stream, and split-half cosine\n",
                 markdown_table(reliability_shown(reliability))]
    path.write_text("\n".join(text))


def sweep_sections(tables: dict) -> list[str]:
    text = []
    edited = tables.get("edited_cells")
    if edited is not None and not edited.empty:
        shown_columns = ["arm", "block_order", "variant", "alpha", "lo", "hi", "donor", "n",
                         "lambda_unedited", "lambda", "ci_lower", "ci_upper", "fraction", "fraction_lower",
                         "fraction_upper"]
        sort_columns = ["arm", "block_order", "variant", "alpha", "lo"]
        part_titles = (("control", "Controls"), ("dose", "Strength"), ("probe", "Slot edit on one block"))
        for part, title in part_titles:
            of_part = edited[edited.part == part].sort_values(sort_columns)
            present = [column for column in shown_columns if column in of_part]
            text += [f"## {title}\n", markdown_table(of_part, present)]
    if "dose_trend" in tables:
        text += ["## Lambda against strength\n", markdown_table(tables["dose_trend"])]
    if "lookup_edited" in tables and not tables["lookup_edited"].empty:
        lookups = tables["lookup_edited"]
        shown_columns = ["part", "arm", "block_order", "variant", "alpha", "n", "person_margin", "own_margin",
                         "person_right", "own_right", "binding", "binding_lower", "binding_upper"]
        present = [column for column in shown_columns if column in lookups]
        controls = lookups[lookups.part == "control"].sort_values(EDIT_COLUMNS)
        text += ["## Record lookups under the edits\n", markdown_table(controls, present)]
    return text


def reliability_shown(reliability: pd.DataFrame) -> pd.DataFrame:
    """The slot, owner and label vectors (not the random directions) at about every eighth layer of the upper
    half."""
    layers = sorted(reliability.layer.unique())
    shown_layers = layers[len(layers) // 2::max(1, len(layers) // 8)]
    shown = reliability[reliability.layer.isin(shown_layers)]
    shown = shown.assign(relative_norm=shown["norm"] / shown.stream_norm)
    shown = shown[["vector", "layer", "relative_norm", "split_half_cosine"]]
    return shown[~shown.vector.str.startswith("random")]
