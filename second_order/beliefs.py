"""The shared belief format every method writes and Phase 5 reads.

``data/beliefs/{model}/{method}.parquet``, one row per case, condition, half
(and, for probes, layer/position/probe type), holding a belief on the logit
scale:

    model, case_id, split, condition, framing, method, half, value, sd, fit,
    layer, position, probe_type

* ``latent``: half in {all, prob, dec, dec_h1, dec_h2}; value = posterior mean
  of logit z; sd = posterior sd; fit in {pooled, separate}.
* ``direct``: half = all; value = logit of the numeric probability clipped to
  [0.01, 0.99]; no halves.
* ``value_calibrated``: for ``latent``, the posterior mean de-shrunk with the
  fit's own calibration line (``measurement.calibration``); for other
  methods, equal to ``value``.  The leakage outcome side reads it.
* ``probe``: half in {all, h1, h2} (replay halves: numeric + Likert, bin +
  plain decision); with layer, position and probe_type.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from second_order import config as C

COLUMNS = ["model", "case_id", "split", "condition", "framing", "method", "half", "value", "sd",
           "value_calibrated", "fit", "layer", "position", "probe_type"]
LATENT_HALVES = ("all", "prob", "dec", "dec_h1", "dec_h2")


def clip_logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p) - np.log1p(-p)


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    if "value_calibrated" not in frame:
        frame["value_calibrated"] = frame["value"]
    for column in COLUMNS:
        if column not in frame:
            frame[column] = np.nan if column in ("sd", "layer") else ""
    return frame[COLUMNS]


def latent_rows(latents: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for half in LATENT_HALVES:
        part = latents[["model", "case_id", "split", "condition", "framing", "fit"]].copy()
        part["method"], part["half"] = "latent", half
        part["value"], part["sd"] = latents[f"ell_{half}"].to_numpy(), latents[f"sd_{half}"].to_numpy()
        part["value_calibrated"] = latents[f"cal_{half}"].to_numpy()
        parts.append(part)
    return _finish(pd.concat(parts, ignore_index=True))


def direct_rows(wide: pd.DataFrame) -> pd.DataFrame:
    rows = wide[wide.complete].copy()
    part = rows[["model", "case_id", "split", "condition", "framing"]].copy()
    part["method"], part["half"], part["fit"] = "direct", "all", "none"
    part["value"] = clip_logit(rows["probability"].to_numpy())
    return _finish(part)


def write(model: str, method: str, frame: pd.DataFrame) -> None:
    path = C.BELIEFS / model / f"{method}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    _finish(frame).to_parquet(path, index=False)


def read(model: str, method: str) -> pd.DataFrame:
    return pd.read_parquet(C.BELIEFS / model / f"{method}.parquet")


def methods_for(model: str) -> list[str]:
    folder = C.BELIEFS / model
    return sorted(p.stem for p in folder.glob("*.parquet")) if folder.exists() else []
