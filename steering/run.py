"""Stages of the steering experiment. Each stage writes one file per patient and resumes after the last one.

The slot vector without reasoning (the model answers with a forced two-digit probability):

    baseline   every prompt unedited: its belief and direct lookups of the disputed fields; on the fit half
               of the patients, the average residual stream of each record's tokens
    vectors    the slot, owner and label vectors from those averages, random directions of the slot vector's
               size, the vectors' split-half reliability, and a lock on the file
    dose       the slot vector at the records of the unlabelled prompt, at a range of strengths
    controls   at low strength: random directions of the slot vector's size, and the reference prompts
               under both
    probe      the slot vector at both signs and finer strengths, and on each record block alone

Sampled replies with reasoning:

    reason     replies to the unedited prompts and to the prompts under the configured edits
    references the reference prompts (A, B, M, D) sampled with this sampler
    decision   the token where a reply first writes a disputed value: each record's probability there, under
               the slot edit, and where that token attends
    branch     replies sampled again from just before and just after the first disputed value they state

The lookup circuit (``steering.circuit``, ``steering.circuit_steps``):

    circuit            which attention heads carry a direct lookup: ranked on fit patients, tested as sets
    circuit_fine       the smallest head set that holds the lookup, found greedily
    circuit_algorithm  what the top heads attend to and write, and which of them are readers
    circuit_concrete   the lookup's steps tested one by one, the place vector, and where the labelling
                       difference enters a reply
    circuit_reply      the lookup heads at the token where a reply first writes a disputed value
    circuit_match      the readers' match taken apart: queries or keys exchanged alone
    circuit_tag        how a record gets its place: the second record read without sight of the first
    circuit_phrase     whether the owner is turned into a place at the question phrase's own tokens
    circuit_steer      replies under the place vector, and with head sets barred from a record
    circuit_paired     replies sampled while a head set sees other records (the other arm's, or slot-edited)

The lookup as a program (``steering.program``, ``steering.head_tables``):

    circuit_program        the address the program computes given to the reader heads, on the patient
                           records and on invented ones
    program_reason         invented records under reasoning, with head sets barred from the record the
                           program says is not asked about
    circuit_tables         every head after the question's phrase written from tables of the program's
                           variables
    circuit_tables_reply   the program's address and the place tables at the token where a reply writes a
                           disputed value
    circuit_redirect       replies sampled with the model's record's value vectors exchanged for PERSON's
    circuit_reply_headers  which tokens of a reply bring the headers' owner signal to the value being written

``analyse`` writes the tables and RESULTS.md (``steering.analysis``); ``all`` runs baseline, vectors, dose and
reason.

    python -m steering.run <stage> --model qwen3_8b --out runs/steering
"""
from __future__ import annotations

import argparse
import itertools
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from functools import cache, partial
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import yaml

from belief_leakage import binding
from belief_leakage.readout import no_reasoning
from belief_leakage import config
from belief_leakage.package_run import file_sha256
from steering import interventions
from steering import prompts
from steering import transcripts
from steering.transcripts import value_token
from steering.engine import Engine, Job, half_bits, to_device
from steering.results import read_rows, reply_rows, stated_value, write_rows

DEFAULTS = {
    # ---- Model and numerics ----
    # The weights session 1's replies came from, and a CPU-sized model for smoke tests.
    "revisions": {"qwen3_8b": "b968826d9c46dd6066d109eabc6255188de91218",
                  "qwen3_14b": "40c069824f4251a91eefaf281ebe4c544efd3e18",
                  "qwen3_0_6b_hf": "c1899de289a04d12100db370d81485cdf75e47ca"},
    # A stage that samples replies names its own precision under "dtype".
    "dtype": "float32",
    "tf32": True,
    # How a two-digit answer becomes one belief: the mean clipped logit over the answer's distribution.
    "statistic": "mean_logit",
    # Rows per forward pass (halved when memory runs out).
    "batch": 48,

    # ---- Prompts and patients ----
    # The prompt arms by variant, each (unlabelled, labelled). "false": PERSON's record has two fields
    # changed; "strong": PERSON's record lacks three fields.
    "pairs": {"false": ["false_neutral", "false_labelled"], "strong": ["strong_original", "strong_labelled"]},
    # The half of the patients edits are tested on. Vectors and head rankings come from the other half, "fit".
    "intervene_split": "test",
    # Patients per group of baseline prompts.
    "chunk": 4,
    # How many disputed fields the direct lookups ask about, per prompt.
    "lookup_fields": 2,
    # Whose record a direct lookup asks about.
    "lookup_queries": ["person", "own"],
    # The baseline keeps each patient's pooled record vectors at every this-many-th layer.
    "store_every": 4,

    # ---- The slot vector without reasoning ----
    # Random directions made for each slot vector.
    "random_seeds": 3,
    # Strengths of the slot edit in the dose stage (strength 1 exchanges the two record blocks' slot marks).
    "alphas": [0.25, 0.5, 0.75, 1.0, 1.5],
    # Strengths at which the controls stage compares the slot edit with random directions.
    "control_alphas": [0.25, 0.5],
    # Probe stage: strengths with both record blocks edited, and strengths with one block edited alone.
    "probe": {"alphas": [-0.5, -0.25, -0.125, 0.125, 0.25, 0.5], "single_block_alphas": [-0.25, 0.25]},

    # ---- Sampled replies ----
    # Reason stage. Sampling runs in bfloat16 (sampling noise dwarfs its rounding). Session 1's replies to
    # these prompts have a median of 570 tokens and a 99th percentile of 930; a pass runs until its longest
    # reply ends.
    "reason": {
        "patients": 32, "samples": 4, "max_new_tokens": 1100, "dtype": "bfloat16", "static_cache": True,
        # patients per pass, and the most prompts a pass may hold
        "group": 4, "batch": 64,
        # the folder written to (a pass with other cells writes to its own)
        "folder": "reason",
        # skip this many of the sorted test patients (to extend an earlier pass)
        "offset": 0,
        # also sample each cell's unedited prompt
        "base": True,
        # each cell: variant, arm of the pair ("plain" or "labelled"), page order, and the edits made beside
        # the unedited prompt, each (kind, strength, source). A transplant's source is the token group copied
        # from the other arm's prompt.
        "cells": [
            ["false", "plain", "person_first",
             [["label", 1.0, None], ["slot", 1.0, None], ["random0", 1.0, None],
              ["transplant", 1.0, "lines"]]],
            ["false", "labelled", "person_first",
             [["label", -1.0, None], ["owner", 1.0, None], ["transplant", 1.0, "lines"]]],
            ["false", "plain", "own_first", [["slot", 1.0, None]]],
            ["strong", "plain", "person_first", [["label", 1.0, "false"]]]]},
    # "attention_parts": the number of layer windows the attention is summed over.
    "decision": {
        "folders": ["reason", "reason_ext"], "alphas": [-0.25, 0.25], "batch": 16, "attention_parts": 6},
    "branch": {
        "folders": ["reason", "reason_ext"],
        "cells": [["false", "plain", "person_first"], ["false", "plain", "own_first"]],
        "patients": 24, "resamples": 4, "max_new_tokens": 1100, "batch": 96, "dtype": "bfloat16"},
    "references": {
        "patients": 32, "samples": 4, "max_new_tokens": 1100, "dtype": "bfloat16", "group": 8, "batch": 96,
        "cells": [["false", "plain", "person_first"], ["false", "labelled", "person_first"],
                  ["false", "plain", "own_first"], ["strong", "plain", "person_first"]]},

    # ---- The lookup circuit (steering.circuit) ----
    # "ks": sizes of the head sets tested; "randoms": random sets made like each; "min_gap": a lookup counts
    # if its two runs' answers lie on opposite sides and at least this many logits apart.
    "circuit": {
        "alpha": 0.25, "scan_patients": 12, "scan_fields": 1, "patients": 24, "fields": 1, "batch": 96,
        "ks": [4, 8, 16, 32, 64, 128, 256, 512], "randoms": 3, "where_heads": 24, "min_gap": 5.0, "seed": 0},
    "circuit_reply": {
        "folders": ["reason", "reason_ext"], "ks": [16, 32, 64, 128], "randoms": 2, "batch": 16,
        "max_tokens": 700},
    # "reply": settings for the replies the circuit_concrete stage reads.
    "circuit_fine": {
        "candidates": 64, "greedy_patients": 6, "max_heads": 32, "stop": 0.97, "patients": 16,
        "wiring_heads": 40, "batch": 96, "randoms": 2,
        "reply": {"folders": ["reason", "reason_ext"], "scan_replies": 32, "max_tokens": 250,
                  "test_max_tokens": 600, "batch": 48, "ks": [4, 8, 16, 32, 64], "randoms": 2}},
    "circuit_algorithm": {"heads": 40, "patients": 12},
    "circuit_concrete": {
        "fit_patients": 16, "scan_patients": 8, "field_patients": 8, "patients": 16, "parts": 6, "batch": 96,
        "knock_batch": 48, "require_crossed": True, "reader_attention": 0.10, "vector_alphas": [1.0, 2.0],
        "vector_layers": [20, 21, 22, 23, 24, 25, 26], "vector_windows": [[22, 24], [20, 26]],
        "replies": 60, "reply_tokens": 250, "reply_batch": 48},
    # Each cell: variant, arm of the pair, page order, the other records the head set sees ("twin": the other
    # arm's; "slot": the same records under the slot edit at the strength that follows), and the head sets
    # ("{n}" stands for "heads").
    "circuit_paired": {
        "patients": 24, "samples": 4, "max_new_tokens": 1100, "dtype": "bfloat16", "group": 4, "heads": 64,
        "cells": [
            ["false", "plain", "person_first", "twin", 0.0, ["lookup{n}", "random{n}", "all but lookup{n}"]],
            ["false", "plain", "person_first", "slot", -0.25, ["lookup{n}"]],
            ["strong", "labelled", "person_first", "slot", -0.25, ["lookup{n}", "random{n}"]]]},
    # Each cell: arm of the false pair, page order, and the conditions: ("place" or "random", strength, None)
    # adds that vector at every sampled token in the blocks of "window"; ("knock", head set, target) bars
    # the head set from a record ("own", "person") or from both records' "headers".
    "circuit_steer": {
        "folder": "reason_circuit", "patients": 32, "samples": 4, "max_new_tokens": 1100, "dtype": "bfloat16",
        "group": 8, "batch": 96, "window": [22, 24], "reader_attention": 0.10,
        "cells": [
            ["plain", "person_first",
             [["place", 1.0, None], ["random", 1.0, None], ["knock", "readers", "own"],
              ["knock", "random0", "own"]]],
            ["labelled", "person_first",
             [["place", -1.0, None], ["random", -1.0, None], ["knock", "readers", "person"],
              ["knock", "random0", "person"]]]]},

    # ---- Steps of the circuit (steering.circuit_steps); blocks are counted from 1 ----
    "circuit_match": {
        "patients": 34, "offset": 40, "reader_window": [23, 35], "convert_window": [12, 20],
        "scales": [1.0, 2.0]},
    "circuit_tag": {"patients": 34, "offset": 40, "windows": [[1, 36], [1, 6], [7, 18], [19, 36]]},
    "circuit_phrase": {
        "patients": 34, "offset": 40, "blocks": [4, 8, 10, 12, 14, 16, 18, 20, 22, 24, 28],
        "windows": [[13, 24], [1, 12], [1, 36]], "batch": 48},

    # ---- The program (steering.program, steering.head_tables) ----
    # The named head sets the stages below use: sizes of the top lookup sets, the attention share that makes
    # a head a reader, and how many random sets are made like each.
    "head_sets": {"lookup_sizes": [16, 32, 64], "reader_attention": 0.10, "randoms": 3},
    "circuit_program": {
        "patients": 34, "offset": 40, "pointer_sets": ["readers", "value32"], "convert_window": [13, 21],
        "place_window": [22, 24], "per_domain": 6, "seed": 2511, "batch": 32},
    # Each cell: format and page order, then the conditions: "base", "cue" (the prompt asks for each value's
    # record to be named) and "bar:<head set>" (that set barred from the model's record).
    "program_reason": {
        "per_domain": 6, "seed": 2511, "samples": 4, "max_new_tokens": 900, "dtype": "bfloat16", "batch": 48,
        "cells": [
            ["unlabelled|person_first",
             ["base", "cue", "bar:place16", "bar:place16+readers", "bar:lookup64", "bar:random_lookup64_0",
              "bar:all"]],
            ["labelled|person_first", ["base"]],
            ["unlabelled|own_first", ["base"]]]},
    # "boundary": the first block (counted from 0) whose heads are left to the model when only the upstream
    # heads come from the tables.
    "circuit_tables": {
        "fit_patients": 76, "patients": 34, "offset": 40, "fields": 1, "min_margin": 5.0, "boundary": 24,
        "sets": ["lookup64"], "batch": 48},
    "circuit_tables_reply": {
        "folders": ["reason", "reason_ext"], "max_tokens": 700, "fit_patients": 76, "boundary": 24,
        "scales": [1.0, 2.0]},
    "circuit_redirect": {
        "patients": 32, "samples": 4, "max_new_tokens": 1100, "dtype": "bfloat16", "group": 4, "batch": 96,
        "sets": ["lookup64", "place16+readers", "readers", "random_lookup64_0", "random_lookup64_1"]},
    "circuit_reply_headers": {
        "folders": ["reason", "reason_ext", "reason_headers"], "max_tokens": 700, "replies": 40,
        "dtype": "bfloat16",
        "owner_heads": [[11, 10], [3, 15], [3, 6], [9, 18], [3, 12], [9, 16], [5, 4], [1, 28],
                        [5, 13], [11, 9], [11, 8], [3, 7], [8, 4], [1, 31], [2, 31], [6, 8]]},
}
# The stages that "all" runs, in order.
STAGES = ("baseline", "vectors", "dose", "reason")
# The other stages the command line takes; each is run on its own.
LATER_STAGES = ("controls", "probe", "branch", "decision", "circuit", "circuit_reply", "circuit_fine",
                "circuit_algorithm", "circuit_concrete", "circuit_steer", "circuit_paired", "references",
                "circuit_program", "program_reason", "circuit_tables", "circuit_tables_reply",
                "circuit_match", "circuit_tag", "circuit_phrase", "circuit_redirect", "circuit_reply_headers")
# Which arm of a pair a cell's "plain" or "labelled" names.
SIDE = {"plain": 0, "labelled": 1}


# Settings and files
PRESETS = Path(__file__).with_name("presets.yaml")


@cache
def presets() -> dict:
    return yaml.safe_load(PRESETS.read_text())


def load_cfg(out_dir: Path, override_file: Path | None = None, *, preset: str | None = None) -> dict:
    """Apply run settings, a named preset and a custom JSON override in that order.
    Each replaces whole sections, except "reason", which merges individual keys."""
    run_file = out_dir / "config.json"
    sources = [json.loads(run_file.read_text())] if run_file.exists() else []
    if preset is not None:
        sources.append(presets()[preset])
    if override_file is not None:
        if override_file.exists():
            sources.append(json.loads(override_file.read_text()))
        elif override_file.suffix == ".json" and override_file.resolve().parent == PRESETS.parent / "configs":
            # Commands saved with the old bundled paths select the same preset.
            sources.append(presets().get(override_file.stem, {}))
    cfg = DEFAULTS.copy()
    for overrides in sources:
        cfg.update(overrides, reason=cfg["reason"] | overrides.get("reason", {}))
    return deepcopy(cfg)


def arms_of(cfg: dict) -> list[str]:
    return [arm for pair in cfg["pairs"].values() for arm in pair]


class Timer:
    """Seconds and patients of each stage, kept in timing.json."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "timing.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def add(self, stage: str, seconds: float, patients: int) -> None:
        totals = self.data.setdefault(stage, {"seconds": 0.0, "patients": 0})
        totals["seconds"] += seconds
        totals["patients"] += patients
        self.path.write_text(json.dumps(self.data, indent=1))


# Result rows
def meta_of(prompt_row: dict, **edit) -> dict:
    """The columns that identify a result row: the prompt (patient, arm, page order, role, replicate) and the
    edit made to it, which is none unless ``edit`` says otherwise."""
    unedited = dict(case_id=prompt_row["case_id"], split=prompt_row["split"], arm=prompt_row["arm"],
                    block_order=prompt_row["block_order"], role=prompt_row["role"],
                    replicate=int(prompt_row["replicate"]),
                    part="base", variant="base", kind="", site="", alpha=0.0, lo=0, hi=0, donor="")
    return unedited | edit


def result_rows(meta: dict, result: dict, lookup_meta: list[dict]) -> list[dict]:
    """The rows of one evaluated prompt: its belief, and one row per direct lookup (the logit of PERSON's
    value minus the model's)."""
    rows = [dict(meta, readout="belief", field="", query="", value=result["p"],
                 digit_mass=result["digit_mass"])]
    for lookup, margin in zip(lookup_meta, result["lookups"]):
        rows.append(dict(meta, readout="lookup", field=lookup["field"], query=lookup["query"], value=margin,
                         digit_mass=np.nan))
    return rows


# Prompts and jobs
def case_prompts(case: str, cases: pd.Series, arms) -> dict:
    """A patient's two-record prompts in ``arms``: (arm, page order, role) -> the prompt table's row, for the
    roles C, M and D."""
    table = prompts.build(cases.loc[[case]], arms, roles=binding.TWO_RECORD_ROLES)
    return {(row.arm, row.block_order, row.role): row._asdict() for row in table.itertuples(index=False)}


def lookups_for(engine: Engine, prompt: str, cfg: dict) -> tuple[list, list[dict]]:
    """Direct lookup questions about the fields the prompt's two records disagree on (none for a one-record
    prompt): the questions' token ids, and for each the field it asks about and in whose record."""
    lookups, lookup_meta = [], []
    disputed = prompts.diff_fields(prompt)[: int(cfg["lookup_fields"])]
    for (code, person_value, own_value), query in itertools.product(disputed, cfg["lookup_queries"]):
        lookup = prompts.lookup_ids(engine.tok, prompt, code, query, (person_value, own_value), engine.kw)
        if lookup is not None:
            lookups.append(lookup)
            lookup_meta.append(dict(field=code, query=query))
    return lookups, lookup_meta


class ReadoutJob(NamedTuple):
    """One no-reasoning measurement. ``lookup_meta``: the field and record each of the job's lookups asks
    about."""
    meta: dict
    job: Job
    lookup_meta: list[dict]


def run_jobs(engine: Engine, jobs: list[ReadoutJob], cfg: dict) -> list[dict]:
    results = engine.evaluate([item.job for item in jobs], int(cfg["batch"]))
    rows = []
    for item, result in zip(jobs, results):
        rows += result_rows(item.meta, result, item.lookup_meta)
    return rows


@dataclass
class ReadoutPrompt:
    """A prompt in its no-reasoning form, ready to be edited."""
    row: dict
    layout: prompts.Layout
    lookups: list
    lookup_meta: list[dict]


def readout_prompt(engine: Engine, prompt_row: dict, cfg: dict, with_lookups: bool = True) -> ReadoutPrompt:
    layout = prompts.layout(engine.tok, no_reasoning(prompt_row["prompt"]), engine.kw)
    lookups, lookup_meta = lookups_for(engine, prompt_row["prompt"], cfg) if with_lookups else ([], [])
    return ReadoutPrompt(prompt_row, layout, lookups, lookup_meta)


def vector_job(engine: Engine, vectors: dict, prompt: ReadoutPrompt, kind: str, arm: str, alpha: float,
               part: str, only_block: int | None = None) -> ReadoutJob:
    """The prompt with a vector added at its records' lines. ``kind``: "slot", or "random<k>", a random
    direction of its size; ``only_block``: edit just the first (0) or the second (1) record block; ``part``:
    the group of results the row is filed under."""
    variant = f"{kind}@lines"
    if only_block is not None:
        variant += f":{('first', 'second')[only_block]}"
    meta = meta_of(prompt.row, part=part, variant=variant, kind=kind.rstrip("0123456789"), site="lines",
                   alpha=float(alpha), lo=1, hi=engine.n_layers)
    edits = interventions.mean_edits(prompt.layout, vectors, kind, arm, alpha=alpha, only=only_block)
    return ReadoutJob(meta, Job(prompt.layout.ids, edits, lookups=prompt.lookups), prompt.lookup_meta)


# Stage: baseline
@dataclass
class BaselinePrompt:
    """One distinct unedited prompt of a group of patients, ready to run."""
    text: str
    rows: list[dict]            # the prompt table's rows with this text (identical prompts are run once)
    layout: prompts.Layout
    lookup_meta: list[dict]
    job: Job


def baseline_prompt(engine: Engine, text: str, rows: list[dict], cfg: dict) -> BaselinePrompt:
    """The job of one unedited prompt. A C prompt also gets the direct lookups. The record tokens' residual
    stream is captured, pooled by owner and token role, for the two-record prompts of fit patients (the
    vectors are made from these) and for every C prompt (kept per patient)."""
    first_row = rows[0]
    layout = prompts.layout(engine.tok, no_reasoning(text), engine.kw)
    has_two_records = first_row["role"] in "CMD"
    lookups, lookup_meta = lookups_for(engine, text, cfg) if first_row["role"] == "C" else ([], [])
    capture, pool = None, None
    if has_two_records and (first_row["split"] == "fit" or first_row["role"] == "C"):
        capture = interventions.capture_positions(layout)
    if capture:        # average the captured stream over the tokens of each (owner, token role, line set)
        pool = lambda hidden: interventions.pool(layout, hidden, capture, text)
    job = Job(layout.ids, capture=capture, lookups=lookups, reduce=pool)
    return BaselinePrompt(text, rows, layout, lookup_meta, job)


def evaluate_shortest_first(engine: Engine, jobs: list[Job], batch: int) -> dict[int, dict]:
    """Evaluate the jobs in order of length, so that a batch holds prompts of similar length. Returns each
    job's result under its index in ``jobs``."""
    by_length = sorted(range(len(jobs)), key=lambda index: len(jobs[index].ids))
    return dict(zip(by_length, engine.evaluate([jobs[index] for index in by_length], batch)))


def keep_pooled_vectors(first_row: dict, pooled_vectors: dict, means: interventions.Means, half_of: dict,
                        kept_layers: list[int], patient_vectors: dict) -> None:
    """File one prompt's pooled record vectors: into the running means over fit patients (once per patient),
    and, for a C prompt, into the patient's own file at the kept layers."""
    case = first_row["case_id"]
    adds_to_means = first_row["split"] == "fit" and case not in means.cases
    for (owner, token_role, line_set), vector in pooled_vectors.items():
        if adds_to_means:
            key = (first_row["arm"], first_row["block_order"], first_row["role"], owner, token_role, line_set,
                   half_of[case])
            means.add(key, vector)
        if first_row["role"] == "C" and line_set == "all":
            name = f"{first_row['arm']}|{first_row['block_order']}|{owner}|{token_role}"
            patient_vectors[name] = half_bits(vector[kept_layers])


def stage_baseline(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Every prompt of every patient, unedited and without reasoning. Writes ``baseline/<patient>.parquet``
    (the belief of each prompt and the direct lookups of C prompts), ``pooled/<patient>.npz`` (the patient's
    pooled record vectors) and ``means.pt`` (running means of those vectors over fit patients, in two
    halves)."""
    arms, done_dir, pooled_dir = arms_of(cfg), run_dir / "baseline", run_dir / "pooled"
    pooled_dir.mkdir(parents=True, exist_ok=True)
    means_path = run_dir / "means.pt"
    if means_path.exists():
        means = interventions.Means.load(means_path, engine.device)
    else:
        means = interventions.Means()
    fit_cases = sorted(cases.index[cases == "fit"])
    # the fit patients in two halves, for the vectors' split-half reliability
    half_of = {case: index % 2 for index, case in enumerate(fit_cases)}
    todo = [case for case in cases.index
            if not (done_dir / f"{case}.parquet").exists() or (case in half_of and case not in means.cases)]
    timer = Timer(run_dir)
    kept_layers = list(range(0, engine.n_layers + 1, int(cfg["store_every"])))
    group_size = int(cfg["chunk"])
    for start in range(0, len(todo), group_size):
        started, group = time.time(), todo[start:start + group_size]
        rows_by_text: dict[str, list[dict]] = {}
        for prompt_row in prompts.build(cases.loc[group], arms).to_dict("records"):
            rows_by_text.setdefault(prompt_row["prompt"], []).append(prompt_row)
        items = [baseline_prompt(engine, text, rows, cfg) for text, rows in rows_by_text.items()]
        results = evaluate_shortest_first(engine, [item.job for item in items], int(cfg["batch"]))
        belief_rows = {case: [] for case in group}
        patient_vectors = {case: {} for case in group}
        for index, item in enumerate(items):
            result = results[index]
            for prompt_row in item.rows:
                meta = meta_of(prompt_row, n_tokens=len(item.layout.ids))
                belief_rows[prompt_row["case_id"]] += result_rows(meta, result, item.lookup_meta)
            if result["hidden"] is not None:
                first_row = item.rows[0]
                keep_pooled_vectors(first_row, result["hidden"], means, half_of, kept_layers,
                                    patient_vectors[first_row["case_id"]])
            results[index] = None       # let the captured vectors go
        for case in group:
            np.savez(pooled_dir / f"{case}.npz", layers=np.array(kept_layers), **patient_vectors[case])
            write_rows(done_dir / f"{case}.parquet", belief_rows[case])
        means.cases += [case for case in group if case in half_of and case not in means.cases]
        means.save(means_path)
        timer.add("baseline", time.time() - started, len(group))
        done = min(start + len(group), len(todo))
        print(f"[baseline] {done}/{len(todo)} patients, {time.time() - started:.1f}s", flush=True)


# Stage: vectors
def stage_vectors(run_dir: Path, cfg: dict) -> dict:
    """The mean vectors from the baseline's running means, with random directions of each slot vector's size.
    Writes ``vectors.pt``, ``vector_reliability.csv`` and ``lock.json`` (the file's hash; later stages refuse
    vectors that no longer match it)."""
    import torch
    means = interventions.Means.load(run_dir / "means.pt")
    pairs = {variant: tuple(pair) for variant, pair in cfg["pairs"].items() if set(pair) <= set(means.arms())}
    vectors = interventions.build_vectors(means, pairs)
    interventions.add_random(vectors, int(cfg["random_seeds"]), int(config.load()["seed"]))
    torch.save(vectors, run_dir / "vectors.pt")
    interventions.reliability(means, pairs).to_csv(run_dir / "vector_reliability.csv", index=False)
    lock = {"vectors_sha256": file_sha256(run_dir / "vectors.pt"), "fit_patients": len(means.cases),
            "vectors": len(vectors)}
    (run_dir / "lock.json").write_text(json.dumps(lock, indent=1))
    print(f"[vectors] {len(vectors)} vectors from {len(means.cases)} fit patients", flush=True)
    return lock


def load_vectors(run_dir: Path, device) -> dict:
    """The vectors of the vectors stage on ``device``, after checking the file against its lock."""
    import torch
    lock = json.loads((run_dir / "lock.json").read_text())
    if file_sha256(run_dir / "vectors.pt") != lock["vectors_sha256"]:
        raise ValueError("vectors.pt no longer matches lock.json: rerun the vectors stage and every later "
                         "one")
    return to_device(torch.load(run_dir / "vectors.pt", map_location="cpu"), device)


# Stages: dose, controls, probe (the slot vector without reasoning)
def run_sweep_stage(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series, stage: str,
                    jobs_for_case) -> None:
    """For each test patient not yet done: build the patient's jobs with ``jobs_for_case``, evaluate them and
    write ``<stage>/<patient>.parquet``."""
    vectors = load_vectors(run_dir, engine.device)
    test_cases = cases.index[cases == cfg["intervene_split"]]
    todo = [case for case in test_cases if not (run_dir / stage / f"{case}.parquet").exists()]
    timer = Timer(run_dir)
    for index, case in enumerate(todo):
        started = time.time()
        jobs = jobs_for_case(engine, vectors, cfg, case, cases)
        write_rows(run_dir / stage / f"{case}.parquet", run_jobs(engine, jobs, cfg))
        timer.add(stage, time.time() - started, 1)
        seconds = time.time() - started
        print(f"[{stage}] {index + 1}/{len(todo)} patients, {len(jobs)} variants, {seconds:.1f}s", flush=True)


def stage_dose(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """The slot vector at the records' lines of the unlabelled C prompt, in both page orders, at each strength
    in ``alphas``."""
    run_sweep_stage(engine, run_dir, cfg, cases, "dose", dose_jobs)


def dose_jobs(engine: Engine, vectors: dict, cfg: dict, case: str, cases: pd.Series) -> list[ReadoutJob]:
    plain = cfg["pairs"]["false"][0]
    prompt_rows = case_prompts(case, cases, [plain])
    jobs = []
    for order in binding.ORDERS:
        prompt = readout_prompt(engine, prompt_rows[(plain, order, "C")], cfg)
        for alpha in cfg["alphas"]:
            jobs.append(vector_job(engine, vectors, prompt, "slot", plain, alpha, part="dose"))
    return jobs


def stage_controls(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """At the low strengths where the slot edit leaves the records readable, in every false-evidence cell:
    C under random directions of the slot vector's size, and the M and D reference prompts under the slot
    edit and under the first random direction."""
    run_sweep_stage(engine, run_dir, cfg, cases, "controls", control_jobs)


def control_jobs(engine: Engine, vectors: dict, cfg: dict, case: str, cases: pd.Series) -> list[ReadoutJob]:
    plain, labelled = cfg["pairs"]["false"]
    prompt_rows = case_prompts(case, cases, [plain, labelled])
    jobs = []
    for arm, order in itertools.product((plain, labelled), binding.ORDERS):
        cell = {role: readout_prompt(engine, prompt_rows[(arm, order, role)], cfg, with_lookups=role == "C")
                for role in ("C", "M", "D")}
        # (role, vector, the group of results the row is filed under), in the order the jobs are run. The slot
        # vector's rows are filed with the dose stage's, which hold the edited C of the unlabelled arm.
        plan = [(role, "slot", "dose") for role in ("M", "D")]
        for seed in range(int(cfg["random_seeds"])):
            roles = ("C", "M", "D") if seed == 0 else ("C",)
            plan += [(role, f"random{seed}", "control") for role in roles]
        if arm == labelled:         # the dose stage runs the slot edit in the unlabelled arm only
            plan.append(("C", "slot", "dose"))
        for alpha, (role, kind, part) in itertools.product(cfg["control_alphas"], plan):
            jobs.append(vector_job(engine, vectors, cell[role], kind, arm, alpha, part=part))
    return jobs


def stage_probe(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """The slot vector in the four false-evidence cells: both signs and finer strengths with both record
    blocks edited, and each block edited alone, for C and for the M and D references. A strength that the dose
    or the controls stage already ran with both blocks edited is not run again."""
    run_sweep_stage(engine, run_dir, cfg, cases, "probe", probe_jobs)


def probe_jobs(engine: Engine, vectors: dict, cfg: dict, case: str, cases: pd.Series) -> list[ReadoutJob]:
    plain, labelled = cfg["pairs"]["false"]
    settings = cfg["probe"]
    prompt_rows = case_prompts(case, cases, [plain, labelled])
    # (strength, the block edited alone or None for both)
    plan = [(alpha, None) for alpha in settings["alphas"]]
    plan += [(alpha, block) for alpha in settings["single_block_alphas"] for block in (0, 1)]
    jobs = []
    for arm, order, role in itertools.product((plain, labelled), binding.ORDERS, ("C", "M", "D")):
        prompt = readout_prompt(engine, prompt_rows[(arm, order, role)], cfg, with_lookups=role == "C")
        for alpha, only_block in plan:
            if only_block is None and ran_in_earlier_stage(alpha, role, arm == plain, cfg):
                continue
            part = "dose" if only_block is None else "probe"
            job = vector_job(engine, vectors, prompt, "slot", arm, alpha, part=part, only_block=only_block)
            jobs.append(job)
    return jobs


def ran_in_earlier_stage(alpha: float, role: str, unlabelled: bool, cfg: dict) -> bool:
    """Whether the slot edit on both blocks at this strength was already run: by the dose stage (C in the
    unlabelled arm, at ``alphas``) or by the controls stage (C in the labelled arm, and M and D in both arms,
    at ``control_alphas``)."""
    if role == "C" and unlabelled:
        return alpha in cfg["alphas"]
    return alpha in cfg["control_alphas"]


# Stage: reason
class ReasonCell(NamedTuple):
    """One cell of a reasoning pass for one patient: the prompt that is sampled, unedited and edited."""
    variant: str                # the pair of arms, e.g. "false"
    side: str                   # "plain" or "labelled": which arm of the pair
    order: str                  # page order
    arm: str
    other_arm: str              # the pair's other arm (a transplant copies from its prompt)
    prompt_row: dict
    layout: prompts.Layout      # the reasoning prompt, nothing forced


def reason_jobs(engine: Engine, vectors: dict, cfg: dict, case: str,
                cases: pd.Series) -> list[tuple[dict, Job]]:
    """(row columns, job) for one patient: each configured cell's unedited reasoning prompt and its edits."""
    settings = cfg["reason"]
    variants = [cell[0] for cell in settings["cells"] if cell[0] in cfg["pairs"]]
    arms = sorted({arm for variant in variants for arm in cfg["pairs"][variant]})
    prompt_rows = case_prompts(case, cases, arms)
    jobs = []
    for variant, side, order, edits in settings["cells"]:
        if variant not in cfg["pairs"]:
            continue
        arm, other_arm = cfg["pairs"][variant][SIDE[side]], cfg["pairs"][variant][1 - SIDE[side]]
        prompt_row = prompt_rows[(arm, order, "C")]
        layout = prompts.layout(engine.tok, prompt_row["prompt"], engine.kw, prefix="")
        cell = ReasonCell(variant, side, order, arm, other_arm, prompt_row, layout)
        if settings.get("base", True):
            jobs.append((meta_of(prompt_row, part="reason"), Job(layout.ids)))
        for kind, alpha, source in edits:
            if kind == "transplant":
                job = transplant_job(engine, cell, prompt_rows[(other_arm, order, "C")], source)
            else:
                job = vector_reply_job(engine, vectors, cell, kind, alpha, source)
            if job is not None:
                jobs.append(job)
    return jobs


def transplant_job(engine: Engine, cell: ReasonCell, donor_row: dict,
                   token_group: str) -> tuple[dict, Job] | None:
    """The cell's prompt with the residual stream of a token group replaced, at every layer, by that of the
    same patient's prompt in the other arm. None if the group's tokens cannot be matched between the two
    prompts."""
    donor_layout = prompts.layout(engine.tok, donor_row["prompt"], engine.kw, prefix="")
    captured = interventions.capture_positions(donor_layout)
    _, _, hidden = engine.run([donor_layout.ids[:donor_layout.n_ctx]], capture=[captured])
    edits = interventions.transplant_edits(cell.layout, donor_layout, hidden[0][1], captured, token_group)
    if edits is None:
        return None
    meta = meta_of(cell.prompt_row, part="reason", variant=f"transplant:{token_group}", kind="transplant",
                   site=token_group, alpha=1.0, lo=1, hi=engine.n_layers, donor=cell.other_arm)
    return meta, Job(cell.layout.ids, edits)


def vector_reply_job(engine: Engine, vectors: dict, cell: ReasonCell, kind: str, alpha: float,
                     source: str | None) -> tuple[dict, Job]:
    """The cell's prompt with a mean vector added at its records' lines. ``kind``: slot, owner, label, or a
    random direction (random<k>, randlabel<k>, randowner<k>); ``source``: the arm (for a label vector, the
    variant) whose vector to use in place of the prompt's own."""
    try:
        edits = interventions.mean_edits(cell.layout, vectors, kind, cell.arm, alpha=alpha, source=source)
    except KeyError as missing:
        raise KeyError(f"no vector {missing.args[0]} for the reasoning cell {cell.variant}, {cell.side}, "
                       f"{cell.order}") from None
    variant_name = f"{kind}_{source}@lines" if source else f"{kind}@lines"
    meta = meta_of(cell.prompt_row, part="reason", variant=variant_name, kind=kind.rstrip("0123456789"),
                   site="lines", alpha=float(alpha), lo=1, hi=engine.n_layers, donor=source or "")
    return meta, Job(cell.layout.ids, edits)


@dataclass
class ReplyBatch:
    """Jobs for one sampling pass and the result rows that refer to each job."""
    jobs: list[Job] = field(default_factory=list)
    wanted: list[tuple[str, dict, int]] = field(default_factory=list)

    def add(self, case: str, meta: dict | None, job: Job):
        if meta is not None:
            self.wanted.append((case, meta, len(self.jobs)))
        self.jobs.append(job)


def held_out_patients(cases: pd.Series, cfg: dict, spec=None) -> list[str]:
    patients = sorted(cases.index[cases == cfg["intervene_split"]])
    if spec is None:
        return patients
    offset = int(spec.get("offset", 0))
    return patients[offset:offset + int(spec["patients"])]


def patients_to_sample(cases: pd.Series, cfg: dict, folder: Path, patients: int, offset=0) -> list[str]:
    """Held-out patients in the requested slice who do not yet have a result file."""
    pool = held_out_patients(cases, cfg)[offset:patients]
    return [case for case in pool if not (folder / f"{case}.parquet").exists()]


def sample_patients(engine, folder, todo, settings, seed, stage, add_jobs, *, group_size=None,
                    paired=False, static_cache=True, timer=None):
    """Resume per-patient reply files, sampling each group with its original first-patient seed."""
    if group_size is None:
        group_size = int(settings["group"])
    for start in range(0, len(todo), group_size):
        started, group = time.time(), todo[start:start + group_size]
        batch = ReplyBatch()
        for case in group:
            add_jobs(case, batch)
        group_seed = seed + sum(map(ord, group[0]))
        samples, tokens = int(settings["samples"]), int(settings["max_new_tokens"])
        if paired:  # leader and follower must stay in the same forward pass
            generator = engine.torch.Generator(device=engine.device).manual_seed(group_seed)
            replies = engine._generate(batch.jobs, samples, tokens, generator, True)
        else:
            replies = engine.generate(batch.jobs, samples, tokens, group_seed, int(settings["batch"]), static_cache)
        rows = {case: [] for case in group}
        for case, meta, index in batch.wanted:
            rows[case].extend(reply_rows(meta, replies[index]))
        for case in group:
            write_rows(folder / f"{case}.parquet", rows[case])
        if timer is not None:
            timer.add(folder.name, time.time() - started, len(group))
        answers = sum(len(replies[i]) for i in {index for _, _, index in batch.wanted})
        print(f"[{stage}] {start + len(group)}/{len(todo)} patients, {answers} answers, "
              f"{time.time() - started:.1f}s", flush=True)


def stage_reason(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Sampled replies with reasoning to each cell's prompt, unedited and under its edits. Several patients
    share a pass, which lasts as long as its longest reply. Writes ``<folder>/<patient>.parquet``."""
    seed = int(config.load()["seed"])
    vectors = interventions.matched_random(load_vectors(run_dir, "cpu"), int(cfg["random_seeds"]), seed)
    vectors = to_device(vectors, engine.device)
    settings = cfg["reason"]
    folder = run_dir / settings.get("folder", "reason")
    todo = patients_to_sample(cases, cfg, folder, int(settings["patients"]), int(settings.get("offset", 0)))

    def add_jobs(case, batch):
        for meta, job in reason_jobs(engine, vectors, cfg, case, cases):
            batch.add(case, meta, job)

    sample_patients(engine, folder, todo, settings, seed, "reason", add_jobs,
                    group_size=max(1, int(settings.get("group", 1))), static_cache=bool(settings.get("static_cache", True)), timer=Timer(run_dir))


def unedited_replies(run_dir: Path, folders: list[str]) -> pd.DataFrame:
    tables = [read_rows(run_dir / folder) for folder in folders]
    tables = [table for table in tables if not table.empty]
    if not tables:
        raise ValueError(f"no sampled replies in {', '.join(folders)} under {run_dir}: "
                         "run the reason stage first")
    replies = pd.concat(tables, ignore_index=True)
    return replies[replies.variant == "base"]


# Stage: decision
@dataclass
class DecisionProbe:
    """An unedited reply fed back up to the token where it first writes a disputed value, under one condition.
    The next token is the first at which the value as written and the other record's value differ."""
    meta: dict
    ids: list[int]                      # the prompt and the reply up to that token
    n_ctx: int                          # how many of them are the records
    edits: list                         # this condition's edit of the records
    attention_groups: dict | None       # record tokens whose attention from the last token is read, if any
    own_token: int                      # the next token if the model's record's value is written
    person_token: int                   # ... and if PERSON's is


def record_token_groups(layout: prompts.Layout, code: str) -> dict[str, list[int]]:
    """The record tokens whose attention is read: the disputed field's line and every line, in the first and
    in the second record block. A group with no tokens is left out."""
    groups = {}
    for block in layout.blocks:
        place = "first" if block.slot == 0 else "second"
        groups[f"field_{place}"] = block.lines[code].tokens if code in block.lines else []
        groups[f"lines_{place}"] = block.line_tokens_all()
    return {name: tokens for name, tokens in groups.items() if tokens}


def decision_probes(engine: Engine, vectors: dict, reply, prompt: str,
                    alphas: list[float]) -> list[DecisionProbe]:
    """The probes of one reply (a row of the reasoning table): unedited, and under the slot vector and a
    random direction of its size at each strength. Empty if the reply states no disputed value in a form that
    can be exchanged for the other record's."""
    point = transcripts.first_written_value(engine, prompt, reply.text)
    if point is None:
        return []
    layout, ids = point.layout, point.ids
    own_token, person_token = point.own_token, point.person_token
    attention_groups = record_token_groups(layout, point.code)

    reasoning = transcripts.ANSWER.split(reply.text)[0]
    reasoning_tokens = len(engine.tok(reasoning, add_special_tokens=False)["input_ids"])
    meta = dict(case_id=reply.case_id, arm=reply.arm, block_order=reply.block_order, sample=int(reply.sample),
                field=point.code, wrote=point.whose, category=transcripts.category(reply.text, prompt),
                position=(len(ids) - len(layout.ids)) / max(reasoning_tokens, 1), reply_value=reply.value)
    unedited_meta = dict(meta, variant="unedited", alpha=0.0)
    probes = [DecisionProbe(unedited_meta, ids, layout.n_ctx, [], attention_groups, own_token, person_token)]
    for kind, alpha in itertools.product(("slot", "random0"), alphas):
        edits = interventions.mean_edits(layout, vectors, kind, reply.arm, alpha=alpha)
        read_attention = attention_groups if kind == "slot" else None
        probes.append(DecisionProbe(dict(meta, variant=kind, alpha=float(alpha)), ids, layout.n_ctx, edits,
                                    read_attention, own_token, person_token))
    return probes


def read_decision_probes(engine: Engine, probes: list[DecisionProbe]) -> list[tuple]:
    """Run a batch of probes in three chunks: the records (under the edit), the question and the reply so far,
    and the last token alone, whose logits and attention are read. Returns (logits, attention by group or
    None) per probe."""
    records = [probe.ids[:probe.n_ctx] for probe in probes]
    _, state, _ = engine.run(records, edits=[probe.edits for probe in probes])
    _, state, _ = engine.run([probe.ids[probe.n_ctx:-1] for probe in probes], state=state)
    groups = [probe.attention_groups for probe in probes]
    logits, _, _ = engine.run([probe.ids[-1:] for probe in probes], state=state, keep=False,
                              groups=groups if any(groups) else None)
    attention = engine.attention or [None] * len(probes)
    return list(zip(logits, attention))


def decision_row(probe: DecisionProbe, logits, attention: dict | None,
                 windows: list[tuple[int, int]]) -> dict:
    """One probe's row: the logit of the model's value minus PERSON's for the next token, and the attention
    from the last token to each group of record tokens, summed over heads, in each layer window."""
    row = dict(probe.meta, value=float(logits[probe.own_token] - logits[probe.person_token]))
    if probe.attention_groups and attention:
        for name, weights in attention.items():
            # weights: [layers, heads, (from the last token, mean over the chunk's tokens)]
            per_layer = weights[:, :, 0].sum(1)
            for low, high in windows:
                row[f"att_{name}_{low}_{high}"] = float(per_layer[low - 1:high].sum())
    return row


def stage_decision(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Is the record a reply quotes already chosen when it writes its first disputed value, and does the slot
    vector set it? Unedited replies of the false-evidence cells are fed back up to that value; the read-out is
    the logit of the model's record's value minus PERSON's for the next token. Conditions: unedited, the slot
    vector at each strength in ``alphas``, and a random direction of its size. Unedited and under the slot
    vector, the attention from that position to the disputed field's line in each record block is read too.
    Writes ``decision/<patient>.parquet``."""
    settings = cfg["decision"]
    vectors = load_vectors(run_dir, engine.device)
    replies = unedited_replies(run_dir, settings["folders"])
    replies = replies[replies.arm.isin(cfg["pairs"]["false"])]
    windows = interventions.windows(engine.n_layers, int(settings["attention_parts"]))
    todo = [case for case in sorted(set(replies.case_id))
            if not (run_dir / "decision" / f"{case}.parquet").exists()]
    timer, batch = Timer(run_dir), int(settings["batch"])
    for index, case in enumerate(todo):
        started = time.time()
        case_replies = replies[replies.case_id == case]
        prompt_rows = case_prompts(case, cases, sorted(set(case_replies.arm)))
        probes = []
        for reply in case_replies.itertuples():
            prompt = prompt_rows[(reply.arm, reply.block_order, "C")]["prompt"]
            probes += decision_probes(engine, vectors, reply, prompt, settings["alphas"])
        probes.sort(key=lambda probe: len(probe.ids))
        rows = []
        for start in range(0, len(probes), batch):
            chunk = probes[start:start + batch]
            readings = engine._halving(chunk, batch, partial(read_decision_probes, engine))
            rows += [decision_row(probe, logits, attention, windows)
                     for probe, (logits, attention) in zip(chunk, readings)]
        write_rows(run_dir / "decision" / f"{case}.parquet", rows)
        timer.add("decision", time.time() - started, 1)
        seconds = time.time() - started
        print(f"[decision] {index + 1}/{len(todo)} patients, {len(rows)} probes, {seconds:.1f}s", flush=True)


# Stage: branch
class BranchSource(NamedTuple):
    """An unedited reply to cut and continue."""
    case: str
    prompt: str
    reply: object               # the reply's row of the reasoning table
    first: str                  # whose disputed value it states first: "model's" or "PERSON's"
    line_start: int             # characters of the reply before the line with that value
    line_end: int               # ... and up to the end of that line


def branch_sources(cell_replies: pd.DataFrame, arm: str, order: str, cases: pd.Series,
                   patients: int) -> list[BranchSource]:
    """Two replies from each of up to ``patients`` patients whose replies to the cell's prompt include one
    that first states the model's value and one that first states PERSON's: the first of each kind."""
    sources = []
    for case, replies in cell_replies.groupby("case_id"):
        prompt = case_prompts(case, cases, [arm])[(arm, order, "C")]["prompt"]
        candidates = [BranchSource(case, prompt, reply, *transcripts.first_stated(reply.text, prompt))
                      for reply in replies.itertuples()]
        own_first = [source for source in candidates if source.first == "model's"]
        person_first = [source for source in candidates if source.first == "PERSON's"]
        if own_first and person_first:
            sources += [own_first[0], person_first[0]]
        if len(sources) >= 2 * patients:
            break
    return sources


def branch_file(run_dir: Path, arm: str, order: str, case: str, sample: int) -> Path:
    return run_dir / "branch" / f"{arm}__{order}__{case}__{sample}.parquet"


def continue_from_cuts(engine: Engine, sources: list[BranchSource], arm: str, order: str, settings: dict,
                       seed: int) -> dict[tuple, list[dict]]:
    """Cut each source reply just before the line with its first disputed value ("before") and just after it
    ("after"), and sample the rest again ``resamples`` times from each cut. Returns the rows by (patient,
    sample of the source reply)."""
    jobs, cuts = [], []
    for source in sources:
        text = source.reply.text
        chat = prompts.chat_text(engine.tok, source.prompt, engine.kw)
        cut_points = (("before", source.line_start), ("after", min(source.line_end + 1, len(text))))
        for cut, kept_characters in cut_points:
            prefix = text[:kept_characters]
            jobs.append(Job(engine.tok(chat + prefix, add_special_tokens=False)["input_ids"]))
            cuts.append((source, cut, prefix))
    replies = engine.generate(jobs, int(settings["resamples"]), int(settings["max_new_tokens"]), seed,
                              len(jobs))
    rows = {}
    for (source, cut, prefix), samples in zip(cuts, replies):
        source_sample = int(source.reply.sample)
        source_category = transcripts.category(source.reply.text, source.prompt)
        for index, sample in enumerate(samples):
            text = prefix + sample["text"]
            rows.setdefault((source.case, source_sample), []).append(dict(
                case_id=source.case, arm=arm, block_order=order, source_sample=source_sample,
                source_first=source.first, source_category=source_category, source_value=source.reply.value,
                cut=cut, prefix_chars=len(prefix), resample=index, text=text, n_tokens=sample["n_tokens"],
                truncated=sample["truncated"], category=transcripts.category(text, source.prompt),
                first=transcripts.first_stated(text, source.prompt)[0], value=stated_value(text)))
    return rows


def stage_branch(engine: Engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Is a reply's outcome fixed by the first disputed value it writes? For patients whose unedited replies
    to one prompt include one that first states the model's value and one that first states PERSON's, each of
    those two replies is cut just before the line with its first disputed value and just after it, and the
    rest is sampled again several times from each cut. Writes
    ``branch/<arm>__<order>__<patient>__<sample>.parquet``."""
    settings = cfg["branch"]
    replies = unedited_replies(run_dir, settings["folders"])
    seed, timer = int(config.load()["seed"]), Timer(run_dir)
    # source replies per pass: each gives two prompts, each sampled ``resamples`` times
    per_pass = max(1, int(settings["batch"]) // (2 * int(settings["resamples"])))
    for variant, side, order in settings["cells"]:
        arm = cfg["pairs"][variant][SIDE[side]]
        cell_replies = replies[(replies.arm == arm) & (replies.block_order == order)]
        sources = branch_sources(cell_replies, arm, order, cases, int(settings["patients"]))
        todo = [source for source in sources
                if not branch_file(run_dir, arm, order, source.case, int(source.reply.sample)).exists()]
        for start in range(0, len(todo), per_pass):
            started, group = time.time(), todo[start:start + per_pass]
            pass_seed = seed + start + sum(map(ord, arm + order))
            rows = continue_from_cuts(engine, group, arm, order, settings, pass_seed)
            for (case, sample), sample_rows in rows.items():
                write_rows(branch_file(run_dir, arm, order, case, sample), sample_rows)
            timer.add("branch", time.time() - started, len(group))
            print(f"[branch] {arm} {order}: {min(start + per_pass, len(todo))}/{len(todo)} replies, "
                  f"{time.time() - started:.1f}s", flush=True)


# Running a stage
def with_vectors(stage_function):
    """A stage function that also takes the vector loader, as a function of (engine, run_dir, cfg, cases)."""
    return lambda engine, run_dir, cfg, cases: stage_function(engine, run_dir, cfg, cases, load_vectors)


# The stages of this file, by name. Each is called as f(engine, run_dir, cfg, cases).
OWN_STAGES = {
    "baseline": stage_baseline,
    "vectors": lambda engine, run_dir, cfg, cases: stage_vectors(run_dir, cfg),
    "dose": stage_dose,
    "reason": stage_reason,
    "controls": stage_controls,
    "probe": stage_probe,
    "branch": stage_branch,
    "decision": stage_decision,
}


def other_module_stages() -> dict:
    """The stages of the other modules, by name. The modules are imported here, when one of their stages is
    first asked for, so that importing this file and running its own stages does not load them."""
    from steering import circuit
    from steering import circuit_steps
    from steering import head_tables
    from steering import program
    return {
        "circuit": with_vectors(circuit.stage_circuit),
        "circuit_reply": with_vectors(circuit.stage_reply),
        "circuit_fine": with_vectors(circuit.stage_fine),
        "circuit_algorithm": with_vectors(circuit.stage_algorithm),
        "circuit_concrete": with_vectors(circuit.stage_concrete),
        "circuit_paired": with_vectors(circuit.stage_paired),
        "circuit_steer": circuit.stage_steer,
        "references": circuit.stage_references,
        "circuit_match": with_vectors(circuit_steps.stage_match),
        "circuit_tag": circuit_steps.stage_tag,
        "circuit_phrase": circuit_steps.stage_phrase,
        "circuit_program": program.stage_program,
        "program_reason": program.stage_program_reason,
        "circuit_redirect": program.stage_redirect,
        "circuit_reply_headers": with_vectors(program.stage_reply_headers),
        "circuit_tables": head_tables.stage_tables,
        "circuit_tables_reply": with_vectors(head_tables.stage_tables_reply),
    }


def stage_dtype(stage: str, cfg: dict) -> str:
    """The precision a stage starts in: the sampled stages of this file name their own, every other the
    default (the sampled stages of the other modules switch to theirs themselves)."""
    if stage in ("reason", "branch"):
        return cfg[stage].get("dtype", cfg["dtype"])
    return cfg["dtype"]


def run_stage(stage: str, engine: Engine | None, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Run one stage by name on the run directory of one model. ``engine`` may be None for the vectors stage,
    which needs no model."""
    if engine is not None:
        engine.cast(stage_dtype(stage, cfg))
    stages = OWN_STAGES if stage in OWN_STAGES else other_module_stages()
    if stage not in stages:
        raise ValueError(f"unknown stage {stage!r}")
    stages[stage](engine, run_dir, cfg, cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", nargs="?", default="all", choices=STAGES + LATER_STAGES + ("analyse", "all"))
    parser.add_argument("--preset", choices=presets(), help="named settings from steering/presets.yaml")
    parser.add_argument("--config", type=Path, help="custom JSON settings applied after the run settings and preset")
    parser.add_argument("--model", default="qwen3_8b")
    parser.add_argument("--out", type=Path, default=Path("runs/steering"))
    parser.add_argument("--limit", type=int, help="this many patients (half per split) instead of all")
    parser.add_argument("--device")
    parser.add_argument("--session1", type=Path,
                        help="session 1's beliefs_direct.parquet, for the comparison tables")
    parser.add_argument("--resamples", type=int, default=2000)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cfg = load_cfg(args.out, args.config, preset=args.preset)
    run_dir = args.out / args.model
    run_dir.mkdir(exist_ok=True)
    (run_dir / "config_used.json").write_text(json.dumps(cfg, indent=1))
    cases = prompts.patients(args.limit)
    if args.stage == "analyse":
        from steering import analysis
        analysis.analyse(run_dir, cfg, args.resamples, args.session1)
        return
    stages = STAGES if args.stage == "all" else (args.stage,)
    engine = None
    if stages != ("vectors",):
        engine = Engine(args.model, device=args.device, dtype=cfg["dtype"], statistic=cfg["statistic"],
                        tf32=cfg["tf32"], revision=cfg["revisions"].get(args.model))
        print(f"[engine] {args.model} at {engine.revision}: {engine.n_layers} layers, hidden {engine.hidden},"
              f" {cfg['dtype']} on {engine.device}", flush=True)
    for stage in stages:
        run_stage(stage, engine, run_dir, cfg, cases)


if __name__ == "__main__":
    main()
