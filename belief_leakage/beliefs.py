"""The shared belief format every method writes and the analysis reads.

``data/beliefs/{model}/{dataset}_{method}.parquet``: one row per case, condition
and run (``replicate``), holding a belief on the logit scale:

    model, dataset, case_id, split, condition, replicate, method, value, sd, value_calibrated, n_samples

* ``latent`` (CKD): value = posterior mean of logit z from the pooled fit; sd = posterior sd.
* ``direct``: value = logit of the stated probability (the JSON answer both datasets
  ask for), clipped to [0.01, 0.99].  With several samples per run (bespoke dataset),
  value = the mean of the sample logits and sd = its standard error.
* ``value_calibrated``: for ``latent``, the posterior mean de-shrunk with the
  fit's own calibration line (``fit_pooled.calibration``); otherwise equal to ``value``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from belief_leakage import config as C

COLUMNS = ["model", "dataset", "case_id", "split", "condition", "replicate", "method",
           "value", "sd", "value_calibrated", "n_samples", "mean_probability", "logit_of_mean_probability"]
DEFAULTS = {"dataset": "ckd", "replicate": 0, "n_samples": 1}


def clip_logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p) - np.log1p(-p)


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    if "value_calibrated" not in frame:
        frame["value_calibrated"] = frame["value"]
    for column in COLUMNS:
        if column not in frame:
            frame[column] = DEFAULTS.get(column, np.nan)
    frame["replicate"] = frame["replicate"].fillna(0).astype(int)
    return frame[COLUMNS]


KEYS = ["model", "case_id", "split", "condition", "replicate"]
PROBABILITY_OUTPUTS = ("probability/normal", "probability")  # CKD, bespoke


def latent_rows(latents: pd.DataFrame) -> pd.DataFrame:
    part = latents[KEYS].copy()
    part["method"] = "latent"
    part["value"], part["sd"] = latents["ell"].to_numpy(), latents["sd"].to_numpy()
    part["mean_probability"] = latents["z_mean"]
    part["logit_of_mean_probability"] = np.log(part.mean_probability) - np.log1p(-part.mean_probability)
    part["value_calibrated"] = latents["cal"].to_numpy()
    return _finish(part)


def direct_rows(long: pd.DataFrame) -> pd.DataFrame:
    """Direct beliefs: the mean logit of the parsed probability answers per case, condition and run."""
    parsed = long[(long.status == "parsed") & long.output.isin(PROBABILITY_OUTPUTS)].copy()
    parsed["logit"] = clip_logit(parsed["value"].to_numpy())
    g = parsed.groupby(["model", "dataset", "case_id", "condition", "replicate"])
    part = pd.DataFrame({"value": g.logit.mean(), "n_samples": g.size(), "mean_probability": g.value.mean(),
                         "sd": g.logit.std(ddof=1) / np.sqrt(g.size())}).reset_index()
    part["logit_of_mean_probability"] = clip_logit(part.mean_probability)
    part["split"], part["method"] = "none", "direct"
    return _finish(part)


def path(model: str, method: str, dataset: str):
    return C.BELIEFS / model / f"{dataset}_{method}.parquet"


def write(model: str, method: str, frame: pd.DataFrame, dataset: str) -> None:
    target = path(model, method, dataset)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    frame["dataset"] = dataset
    _finish(frame).to_parquet(target, index=False)


def read(model: str, method: str, dataset: str) -> pd.DataFrame:
    return pd.read_parquet(path(model, method, dataset))


def methods_for(model: str, dataset: str) -> list[str]:
    """Belief methods written for a model and dataset."""
    return sorted(p.stem.split("_", 1)[1] for p in (C.BELIEFS / model).glob(f"{dataset}_*.parquet"))
