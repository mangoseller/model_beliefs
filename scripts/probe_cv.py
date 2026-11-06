"""Cross-validated version of the label probe, with bootstrap confidence intervals.

The single train/val/test split leaves about 100 test cases (only ~17 CKD patients), so test
AUCs are noisy. Here every case is predicted out-of-fold (5 folds, split by case), at one fixed
layer chosen in advance (default 16, the middle of the 28-layer model), for each position. The
ridge strength is picked by an inner split of each training fold. The model's own readouts and
the baselines are scored on the same 500 cases with the same bootstrap.

    python scripts/probe_cv.py data/ckd__Qwen2.5-1.5B-Instruct
"""
import argparse
import csv
from pathlib import Path

import numpy as np

from train_probe import ALPHAS, POSITIONS, auc, load, ridge_path


def boot_ci(y, s, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if 0 < y[i].sum() < len(i):
            vals.append(auc(y[i], s[i]))
    return np.percentile(vals, [2.5, 97.5])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--layer", type=int, default=16)
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()

    beliefs, feats, case_of, layers = load(args.run, {args.layer})
    cases = sorted(set(case_of))
    rng = np.random.default_rng(20260811)
    fold_of = {c: f for c, f in zip(rng.permutation(cases), np.arange(len(cases)) % args.folds)}
    rf = np.array([fold_of[c] for c in case_of])
    y_row = np.array([float(beliefs[c]["label"]) for c in case_of])
    y_case = np.array([float(beliefs[c]["label"]) for c in cases])
    cidx = {c: i for i, c in enumerate(cases)}
    rows_case = np.array([cidx[c] for c in case_of])

    out = []
    for p in POSITIONS:
        X = feats[p][:, 0].astype(np.float32)
        pred_case = np.zeros(len(cases))
        for k in range(args.folds):
            tr, te = rf != k, rf == k
            # inner split for alpha: hold out one of the other folds
            inner_val = tr & (rf == (k + 1) % args.folds)
            inner_tr = tr & ~inner_val
            def fit(mask):
                mu, sd = X[mask].mean(0), X[mask].std(0) + 1e-4
                Z = (X - mu) / sd
                ym = y_row[mask].mean()
                return Z, ym, ridge_path(Z[mask], y_row[mask] - ym, ALPHAS)
            Z, ym, ws = fit(inner_tr)
            scores = []
            for w in ws:
                s = np.bincount(rows_case[inner_val], Z[inner_val] @ w, len(cases))
                n = np.bincount(rows_case[inner_val], minlength=len(cases))
                m = n > 0
                scores.append(auc(y_case[m], s[m] / n[m]))
            a = int(np.nanargmax(scores))
            Z, ym, ws = fit(tr)
            s = np.bincount(rows_case[te], Z[te] @ ws[a] + ym, len(cases))
            n = np.bincount(rows_case[te], minlength=len(cases))
            m = n > 0
            pred_case[m] = s[m] / n[m]
        lo, hi = boot_ci(y_case, pred_case)
        out.append((f"probe {p} L{layers[0]}", auc(y_case, pred_case), lo, hi))

    for name in ["direct_prob", "z_reports", "z_decisions", "z_all"]:
        s = np.array([float(beliefs[c][name]) if beliefs[c][name] != "" else np.nan for c in cases])
        m = ~np.isnan(s)
        lo, hi = boot_ci(y_case[m], s[m])
        out.append((name, auc(y_case[m], s[m]), lo, hi))

    with open(args.run / "probe_cv.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["readout", "auc", "ci_low", "ci_high"])
        for r in out:
            w.writerow([r[0], f"{r[1]:.3f}", f"{r[2]:.3f}", f"{r[3]:.3f}"])
    print(f"{args.run}: {len(cases)} cases, {int(y_case.sum())} positive, {args.folds}-fold")
    for r in out:
        print(f"  {r[0]:28s} AUC {r[1]:.3f}  [{r[2]:.3f}, {r[3]:.3f}]")


if __name__ == "__main__":
    main()
