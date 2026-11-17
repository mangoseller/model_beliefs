"""Control for the probe: how well does a plain bag-of-words model on the case text predict y?

If a linear model on word counts does as well as the activation probe, the probe may only be
picking up surface features of the text rather than anything the model works out. Uses the same
train/val/test split as beliefs.csv.

    python scripts/text_baseline.py datasets/insecure_code_em.json data/insecure_code_em__Qwen2.5-1.5B-Instruct
"""
import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np

from train_probe import ALPHAS, auc, ridge_path


def case_text(case):
    return case.get("context") or " ".join(f"{k} {v}" for k, v in case.get("vars", {}).items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", type=Path)
    ap.add_argument("run", type=Path)
    ap.add_argument("--vocab", type=int, default=2000)
    args = ap.parse_args()

    cases = {c["id"]: c for c in json.load(open(args.scenario))["cases"]}
    beliefs = list(csv.DictReader(open(args.run / "beliefs.csv")))
    toks = {b["case_id"]: re.findall(r"[A-Za-z_]+|\d+|[^\sA-Za-z_\d]", case_text(cases[b["case_id"]]))
            for b in beliefs}
    train = [b for b in beliefs if b["split"] == "train"]
    vocab = [w for w, _ in Counter(w for b in train for w in set(toks[b["case_id"]])).most_common(args.vocab)]
    vi = {w: i for i, w in enumerate(vocab)}

    def X_of(bs):
        X = np.zeros((len(bs), len(vocab)), np.float32)
        for r, b in enumerate(bs):
            for w in toks[b["case_id"]]:
                if w in vi:
                    X[r, vi[w]] += 1
        return np.log1p(X)

    parts = {s: [b for b in beliefs if b["split"] == s] for s in ["train", "val", "test"]}
    X = {s: X_of(v) for s, v in parts.items()}
    y = {s: np.array([float(b["label"]) for b in v]) for s, v in parts.items()}
    mu, sd = X["train"].mean(0), X["train"].std(0) + 1e-4
    X = {s: (v - mu) / sd for s, v in X.items()}
    ym = y["train"].mean()
    ws = ridge_path(X["train"], y["train"] - ym, ALPHAS)
    val = [auc(y["val"], X["val"] @ w) for w in ws]
    k = int(np.nanargmax(val))
    print(f"{args.run}: bag-of-words ({len(vocab)} tokens), alpha {ALPHAS[k]}, "
          f"val AUC {val[k]:.3f}, test AUC {auc(y['test'], X['test'] @ ws[k]):.3f}")


if __name__ == "__main__":
    main()
