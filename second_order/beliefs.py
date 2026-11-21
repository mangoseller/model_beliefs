"""The shared belief format every method writes and Phase 5 reads.

``data/beliefs/{model}/{method}.parquet`` (CKD) and
``data/beliefs/{model}/{dataset}_{method}.parquet`` (other datasets), one row
per case, condition, run (``replicate``) and half (and, for probes,
layer/position/probe type), holding a belief on the logit scale:

    model, dataset, case_id, split, condition, replicate, framing, method, half,
    value, sd, value_calibrated, fit, layer, position, probe_type, n_samples

* ``latent``: half in {all, prob, dec, dec_h1, dec_h2}; value = posterior mean
  of logit z; sd = posterior sd; fit in {pooled, separate}.
* ``direct``: half = all; value = logit of the numeric probability clipped to
  [0.01, 0.99]; no halves.  With several samples per run (bespoke dataset),
  value = the mean of the sample logits and sd = its standard error.
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

COLUMNS = ["model", "dataset", "case_id", "split", "condition", "replicate", "framing", "method", "half",
           "value", "sd", "value_calibrated", "fit", "layer", "position", "probe_type", "n_samples"]
DEFAULTS = {"dataset": "ckd", "replicate": 0, "n_samples": 1}
LATENT_HALVES = ("all", "prob", "dec", "dec_h1", "dec_h2")


def clip_logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p) - np.log1p(-p)


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    if "value_calibrated" not in frame:
        frame["value_calibrated"] = frame["value"]
    for column in COLUMNS:
        if column not in frame:
            frame[column] = DEFAULTS.get(column, np.nan if column in ("sd", "layer") else "")
    frame["replicate"] = frame["replicate"].fillna(0).astype(int)
    return frame[COLUMNS]


def latent_rows(latents: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for half in LATENT_HALVES:
        keep = [c for c in ("model", "case_id", "split", "condition", "replicate", "framing", "fit") if c in latents]
        part = latents[keep].copy()
        part["method"], part["half"] = "latent", half
        part["value"], part["sd"] = latents[f"ell_{half}"].to_numpy(), latents[f"sd_{half}"].to_numpy()
        part["value_calibrated"] = latents[f"cal_{half}"].to_numpy()
        parts.append(part)
    return _finish(pd.concat(parts, ignore_index=True))


def direct_rows(wide: pd.DataFrame) -> pd.DataFrame:
    rows = wide[wide.complete].copy()
    keep = [c for c in ("model", "case_id", "split", "condition", "replicate", "framing") if c in rows]
    part = rows[keep].copy()
    part["method"], part["half"], part["fit"] = "direct", "all", "none"
    part["value"] = clip_logit(rows["probability"].to_numpy())
    return _finish(part)


def sample_mean_rows(long: pd.DataFrame) -> pd.DataFrame:
    """Direct beliefs from several samples per run: mean sample logit and its standard error."""
    from second_order.conditions import CONDITIONS

    parsed = long[(long.status == "parsed") & (long.output == "probability")].copy()
    parsed["logit"] = clip_logit(parsed["value"].to_numpy())
    g = parsed.groupby(["model", "dataset", "case_id", "condition", "replicate"]).logit
    part = pd.DataFrame({"value": g.mean(), "n_samples": g.size(),
                         "sd": g.std(ddof=1) / np.sqrt(g.size())}).reset_index()
    part["framing"] = part.condition.map(lambda c: CONDITIONS[c].framing)
    part["split"], part["method"], part["half"], part["fit"] = "none", "direct", "all", "none"
    return _finish(part)


def stem(method: str, dataset: str = "ckd") -> str:
    return method if dataset == "ckd" else f"{dataset}_{method}"


def write(model: str, method: str, frame: pd.DataFrame, dataset: str = "ckd") -> None:
    path = C.BELIEFS / model / f"{stem(method, dataset)}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    frame["dataset"] = dataset
    _finish(frame).to_parquet(path, index=False)


def read(model: str, method: str, dataset: str = "ckd") -> pd.DataFrame:
    frame = pd.read_parquet(C.BELIEFS / model / f"{stem(method, dataset)}.parquet")
    for column, default in DEFAULTS.items():  # files written before these columns existed
        if column not in frame:
            frame[column] = default
    return frame


def methods_for(model: str, dataset: str = "ckd") -> list[str]:
    """Belief methods written for a model and dataset."""
    folder = C.BELIEFS / model
    if not folder.exists():
        return []
    stems = sorted(p.stem for p in folder.glob("*.parquet"))
    if dataset == "ckd":
        return [s for s in stems if "_" not in s]
    return [s.split("_", 1)[1] for s in stems if s.startswith(f"{dataset}_")]
