"""Per-patient result tables and sampled replies shared by the steering stages."""
from pathlib import Path

import pandas as pd

from steering.engine import stated_probability


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


def stated_value(reply_text: str) -> float:
    probability = stated_probability(reply_text)
    return float("nan") if probability is None else probability


def reply_rows(meta: dict, samples: list[dict]) -> list[dict]:
    return [dict(meta, readout="reasoned", sample=index, text=sample["text"], n_tokens=sample["n_tokens"],
                 truncated=sample["truncated"], value=stated_value(sample["text"]))
            for index, sample in enumerate(samples)]
