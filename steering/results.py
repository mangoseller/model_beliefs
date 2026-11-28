"""What the steering stages share: the patients a stage runs on and their prompts, the per-patient loop that
resumes after the last file written, result rows and their files, sampled replies, and the vectors file."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from belief_leakage import binding
from belief_leakage.package_run import file_sha256
from steering import prompts
from steering.engine import stated_probability, to_device

# Which arm of a pair a cell's "plain" or "labelled" names.
SIDE = {"plain": 0, "labelled": 1}


# Result rows and files
def write_rows(path: Path, rows: list[dict]) -> None:
    """Write result rows as a parquet file, through a temporary file so that an interrupted run leaves no half
    file behind (a stage skips the patients whose file exists)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pd.DataFrame(rows).to_parquet(temporary, index=False)
    temporary.replace(path)


def read_rows(folder: Path) -> pd.DataFrame:
    """Every per-patient file of a stage's folder as one table (empty if the folder has none)."""
    files = sorted(folder.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)


def meta_of(prompt_row: dict, **edit) -> dict:
    """The columns that identify a result row: the prompt (patient, arm, page order, role, replicate) and the
    edit made to it, which is none unless ``edit`` says otherwise."""
    unedited = dict(case_id=prompt_row["case_id"], split=prompt_row["split"], arm=prompt_row["arm"],
                    block_order=prompt_row["block_order"], role=prompt_row["role"],
                    replicate=int(prompt_row["replicate"]),
                    part="base", variant="base", kind="", site="", alpha=0.0, lo=0, hi=0, donor="")
    return unedited | edit


def stated_value(reply_text: str) -> float:
    probability = stated_probability(reply_text)
    return float("nan") if probability is None else probability


def reply_columns(index: int, sample: dict) -> dict:
    """The columns of the ``index``-th sampled reply to a prompt."""
    return dict(sample=index, text=sample["text"], n_tokens=sample["n_tokens"], truncated=sample["truncated"],
                value=stated_value(sample["text"]))


def reply_rows(meta: dict, samples: list[dict]) -> list[dict]:
    return [dict(meta, readout="reasoned", **reply_columns(index, sample)) for index, sample in enumerate(samples)]


def unedited_replies(run_dir: Path, folders: list[str], arms=None) -> pd.DataFrame:
    """The replies sampled to unedited prompts in ``folders`` (only those of ``arms``, if given)."""
    tables = [read_rows(run_dir / folder) for folder in folders]
    tables = [table for table in tables if not table.empty]
    if not tables:
        raise ValueError(f"no sampled replies in {', '.join(folders)} under {run_dir}: "
                         "run the reason stage first")
    replies = pd.concat(tables, ignore_index=True)
    replies = replies[replies.variant == "base"]
    return replies if arms is None else replies[replies.arm.isin(arms)]


# Patients and their prompts
def case_prompts(case: str, cases: pd.Series, arms) -> dict:
    """A patient's two-record prompts in ``arms``: (arm, page order, role) -> the prompt table's row, for the
    roles C, M and D."""
    table = prompts.build(cases.loc[[case]], arms, roles=binding.TWO_RECORD_ROLES)
    return {(row.arm, row.block_order, row.role): row._asdict() for row in table.itertuples(index=False)}


def replies_with_prompts(replies: pd.DataFrame, case: str, cases: pd.Series, arms=None, order_by=None):
    """Each of a patient's replies with the patient's C prompts: (reply, {(arm, page order): prompt}), in the
    table's order or sorted by ``order_by``. ``arms``: the arms whose prompts are made, by default those of the
    patient's replies."""
    case_replies = replies[replies.case_id == case]
    prompt_rows = case_prompts(case, cases, sorted(set(case_replies.arm)) if arms is None else arms)
    prompt_of = {(arm, order): row["prompt"] for (arm, order, role), row in prompt_rows.items() if role == "C"}
    if order_by is not None:
        case_replies = case_replies.sort_values(order_by)
    for reply in case_replies.itertuples():
        yield reply, prompt_of


def held_out_patients(cases: pd.Series, cfg: dict, spec=None) -> list[str]:
    patients = sorted(cases.index[cases == cfg["intervene_split"]])
    if spec is None:
        return patients
    offset = int(spec.get("offset", 0))
    return patients[offset:offset + int(spec["patients"])]


def unfinished(patients, folder: Path) -> list[str]:
    """The patients without a result file in ``folder``, in order."""
    return [case for case in patients if not (folder / f"{case}.parquet").exists()]


def patients_to_sample(cases: pd.Series, cfg: dict, folder: Path, patients: int, offset=0) -> list[str]:
    """Held-out patients in the requested slice who do not yet have a result file."""
    return unfinished(held_out_patients(cases, cfg)[offset:patients], folder)


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


def each_patient(todo: list[str], label: str, run_patient, counted: bool = True, timer: Timer | None = None):
    """Run ``run_patient(case)`` for each patient of ``todo``, printing a progress line after each. It writes
    the patient's files and returns what the line says after the patient's number ("patients, 12 runs"). With
    ``counted`` the number is out of ``len(todo)``; with ``timer``, each patient's seconds are kept under
    ``label``."""
    for index, case in enumerate(todo):
        started = time.time()
        summary = run_patient(case)
        if timer is not None:
            timer.add(label, time.time() - started, 1)
        number = f"{index + 1}/{len(todo)}" if counted else f"{index + 1}"
        print(f"[{label}] {number} {summary}, {time.time() - started:.1f}s", flush=True)


# Sampled replies
@dataclass
class ReplyBatch:
    """Jobs for one sampling pass and the result rows that refer to each job."""
    jobs: list = field(default_factory=list)
    wanted: list[tuple[str, dict, int]] = field(default_factory=list)

    def add(self, case: str, meta: dict | None, job):
        if meta is not None:
            self.wanted.append((case, meta, len(self.jobs)))
        self.jobs.append(job)


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


# The vectors of the vectors stage
def load_vectors(run_dir: Path, device) -> dict:
    """The vectors of the vectors stage on ``device``, after checking the file against its lock."""
    import torch
    lock = json.loads((run_dir / "lock.json").read_text())
    if file_sha256(run_dir / "vectors.pt") != lock["vectors_sha256"]:
        raise ValueError("vectors.pt no longer matches lock.json: rerun the vectors stage and every later "
                         "one")
    return to_device(torch.load(run_dir / "vectors.pt", map_location="cpu"), device)
