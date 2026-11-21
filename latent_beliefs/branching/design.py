"""Design constants, identifiers, and file locations of the branching experiments.

Record IDs are hashes of a readable key, so the same case, prefix, and
follow-up always receive the same ID:

    behavior     prefix ``<domain>:<case>:rNN``, follow-up ``<model>:<prefix>:<output>``
    probability  prefix ``<domain>:<case>:pNN``, follow-up ``<model>:<prefix>:qNN``
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from latent_beliefs.config import (
    ASSETS_DIR,
    COLLECTION_DIR,
    DOMAINS,
    PARSED_DIR,
    PROMPTS_DIR,
    RESPONSES_DIR,
)
from latent_beliefs.models import BRANCHING_MODELS

MODELS = BRANCHING_MODELS
CASES_PER_DOMAIN = 30
TEMPERATURE = 1.0
INITIAL_MAX_TOKENS = 2048
FOLLOWUP_MAX_TOKENS = 4096
STAGES = ("initial", "followups")


@dataclass(frozen=True)
class Experiment:
    key: str
    prefixes: int  # reasoning prefixes per case
    prefix_tag: str  # letter in the prefix key
    prefix_id_field: str
    prefix_index_field: str
    answers_per_prefix: int  # probability experiment: answers per prefix


EXPERIMENTS = {
    "behavior": Experiment("behavior", 20, "r", "branch_id", "replicate", 1),
    "probability": Experiment("probability", 5, "p", "prefix_id", "prefix_replicate", 5),
}

# The numerical-probability prompt of each domain (probability experiment).
PROBABILITY_VARIANT = {
    "ckd": "probability",
    "werewolf": "probability",
    "forecasting": "probability_positive",
}

# Locations
# The 30 cases per domain on which both experiments were run.  They were drawn
# from the 500 main-experiment cases by a seeded sampler that shuffled
# label-by-stratum groups (CKD status x risk decile; Werewolf identity x cut
# quartile; forecasting effect sign x study outcome) and drew round-robin
# across them.  The forecasting strata used the effect sign on the raw outcome
# scale, so the draw is recorded here rather than recomputed.
SELECTION_PATH = ASSETS_DIR / "branching" / "selected_instances.csv"
CASES_PATH = PROMPTS_DIR / "branching" / "cases.jsonl"
BRANCHING_COLLECTION_DIR = COLLECTION_DIR / "branching"
BRANCHING_RESPONSES_DIR = RESPONSES_DIR / "branching"
BRANCHING_PARSED_DIR = PARSED_DIR / "branching"


def load_selection(path: Path = SELECTION_PATH) -> pd.DataFrame:
    """The selected cases: ``domain``, ``selection_order``, ``instance_id``, ``target_player``."""
    selected = pd.read_csv(path, keep_default_na=False)
    counts = selected.groupby("domain").size().to_dict()
    expected = {domain: CASES_PER_DOMAIN for domain in DOMAINS}
    if counts != expected:
        raise ValueError(f"Unexpected selection counts: {counts}; expected {expected}")
    return selected


def response_path(responses_dir: Path, experiment: str, stage: str, model: str) -> Path:
    """Released responses: ``<responses>/<experiment>/<stage>/<model>.jsonl``."""
    return responses_dir / experiment / stage / f"{model}.jsonl"


# Design utilities
def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def prefix_key(experiment: Experiment, domain: str, instance_id: str, index: int) -> str:
    return f"{domain}:{instance_id}:{experiment.prefix_tag}{index:02d}"


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def content_hash(text: str) -> str:
    """SHA-256 of the replayed assistant content (one text block)."""
    canonical = json.dumps(
        [{"text": text}], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
