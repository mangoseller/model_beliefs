"""Control for the kidney probe: logistic regression on the patient features themselves.

This is roughly the best a linear model can do from the information in the prompt, so it is the
ceiling to compare the probe and the model's own answers against. Same split as beliefs.csv.

    python scripts/feature_baseline_ckd.py datasets/ckd_nhanes.json data/ckd__Qwen2.5-1.5B-Instruct
"""
import argparse
import csv
import json
import re
from pathlib import Path

import numpy as np

from train_probe import auc


def parse(context):
    out = {}
    for m in re.finditer(r"\((\w+)\) is (.+?)\.?$", context, re.M):
        n = re.match(r"^(-?\d+(?:\.\d+)?)(?:\s|$)", m.group(2))
        out[m.group(1)] = float(n.group(1)) if n else m.group(2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", type=Path)
    ap.add_argument("run", type=Path)
    args = ap.parse_args()

    cases = {c["id"]: c for c in json.load(open(args.scenario))["cases"]}
    beliefs = list(csv.DictReader(open(args.run / "beliefs.csv")))
    pv = {b["case_id"]: parse(cases[b["case_id"]]["context"]) for b in beliefs}
    keys = sorted({k for v in pv.values() for k in v})
    num = {k for k in keys if all(isinstance(v.get(k, 0.0), float) for v in pv.values())}
    cat = {k: sorted({str(v.get(k)) for v in pv.values()}) for k in keys if k not in num}

    def feat(c):
        f = []
        for k in keys:
            f += [pv[c].get(k, np.nan)] if k in num else [float(str(pv[c].get(k)) == o) for o in cat[k]]
        return f

    parts = {s: [b for b in beliefs if b["split"] == s] for s in ["train", "val", "test"]}
    X = {s: np.array([feat(b["case_id"]) for b in v], float) for s, v in parts.items()}
    y = {s: np.array([float(b["label"]) for b in v]) for s, v in parts.items()}
    med = np.nanmedian(X["train"], 0)
    X = {s: np.where(np.isnan(v), med, v) for s, v in X.items()}
    mu, sd = X["train"].mean(0), X["train"].std(0) + 1e-6
    X = {s: (v - mu) / sd for s, v in X.items()}

    def fit(lam):
        w, b0 = np.zeros(X["train"].shape[1]), 0.0
        for _ in range(4000):
            g = 1 / (1 + np.exp(-(X["train"] @ w + b0))) - y["train"]
            w -= 0.1 * (X["train"].T @ g / len(g) + lam * w)
            b0 -= 0.1 * g.mean()
        return w

    fits = {lam: fit(lam) for lam in [0, 1e-3, 1e-2, 1e-1]}
    lam = max(fits, key=lambda l: auc(y["val"], X["val"] @ fits[l]))
    print(f"{args.run}: feature logistic regression, L2 {lam}, "
          f"val AUC {auc(y['val'], X['val'] @ fits[lam]):.3f}, test AUC {auc(y['test'], X['test'] @ fits[lam]):.3f}")


if __name__ == "__main__":
    main()
