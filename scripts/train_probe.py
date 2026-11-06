"""Readout 3: a linear probe on the residual stream.

For each saved layer and token position, fits a ridge regression from the activation to a target:
  - z_all:  the decision-and-report latent belief from extract_model_belief.py (the plan's probe)
  - label:  the true answer y (does the information exist inside the model at all?)

Uses the train/val/test split from beliefs.csv: train on train cases, pick the ridge strength on
val, report on test. Every saved row of a case (each question, each option order) is a training
example with that case's target; at test time the row predictions are averaged per case.

    python scripts/train_probe.py data/ckd__Qwen2.5-1.5B-Instruct

Writes probe.csv (one row per layer x position x target) and probe_preds.csv (per-case test
predictions at the best layer for each target) into the run directory.
"""
import argparse
import csv
from pathlib import Path

import numpy as np

POSITIONS = ["context_end", "prompt_end", "answer"]
ALPHAS = [1e-1, 1, 10, 100, 1e3, 1e4]


def auc(y, s):
    y, s = np.asarray(y), np.asarray(s)
    r = np.argsort(np.argsort(s)) + 1.0
    n1, n0 = (y == 1).sum(), (y == 0).sum()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def ridge_path(X, y, alphas):
    """Ridge weights for each alpha, via one eigendecomposition of X^T X."""
    evals, evecs = np.linalg.eigh(X.T @ X)
    proj = evecs.T @ (X.T @ y)
    return [evecs @ (proj / (evals + a)) for a in alphas]


def load(run, layers_wanted):
    beliefs = {r["case_id"]: r for r in csv.DictReader(open(run / "beliefs.csv"))}
    slim = run / "activations_slim"
    src = slim if slim.exists() and any(slim.glob("*.npz")) else run / "activations"
    feats, case_of, layers = {p: [] for p in POSITIONS}, [], None
    for cid in beliefs:
        f = src / f"{cid}.npz"
        if not f.exists():
            continue
        z = np.load(f)
        all_layers = list(z["layers"]) if "layers" in z.files else list(range(z["answer"].shape[1]))
        if layers is None:
            layers = [l for l in all_layers if l in layers_wanted] if layers_wanted else all_layers
        idx = [all_layers.index(l) for l in layers]
        for p in POSITIONS:
            feats[p].append(z[p][:, idx])
        case_of += [cid] * z["answer"].shape[0]
    return beliefs, {p: np.concatenate(v) for p, v in feats.items()}, np.array(case_of), layers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--layers", type=int, nargs="*", default=[0, 4, 8, 12, 16, 20, 24, 28])
    args = ap.parse_args()

    beliefs, feats, case_of, layers = load(args.run, set(args.layers))
    split = np.array([beliefs[c]["split"] for c in case_of])
    targets = {"z_all": np.array([float(beliefs[c]["z_all"]) for c in case_of]),
               "label": np.array([float(beliefs[c]["label"]) for c in case_of])}
    cases = {s: sorted(set(case_of[split == s])) for s in ["train", "val", "test"]}
    print(f"{args.run}: {len(set(case_of))} cases, {len(case_of)} rows, layers {layers}")

    def per_case(pred, s):
        m = split == s
        cs = cases[s]
        idx = {c: i for i, c in enumerate(cs)}
        tot, cnt = np.zeros(len(cs)), np.zeros(len(cs))
        np.add.at(tot, [idx[c] for c in case_of[m]], pred[m])
        np.add.at(cnt, [idx[c] for c in case_of[m]], 1)
        return cs, tot / cnt

    def case_target(name, cs):
        return np.array([float(beliefs[c][name]) for c in cs])

    rows, best = [], {}
    for p in POSITIONS:
        for li, layer in enumerate(layers):
            X = feats[p][:, li].astype(np.float32)
            tr = split == "train"
            mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-4
            X = (X - mu) / sd
            for tname, y in targets.items():
                ym = y[tr].mean()
                ws = ridge_path(X[tr], y[tr] - ym, ALPHAS)
                # pick alpha on val (per-case averaged predictions)
                scores = []
                for w in ws:
                    cs, pv = per_case(X @ w + ym, "val")
                    tv = case_target(tname, cs)
                    scores.append(auc(tv, pv) if tname == "label" else spearman(tv, pv))
                k = int(np.nanargmax(scores))
                pred = X @ ws[k] + ym
                cs, pt = per_case(pred, "test")
                r = {"position": p, "layer": layer, "target": tname, "alpha": ALPHAS[k],
                     "val_score": scores[k],
                     "test_spearman_z_all": spearman(case_target("z_all", cs), pt),
                     "test_auc_label": auc(case_target("label", cs), pt)}
                rows.append(r)
                key = tname
                if key not in best or r["val_score"] > best[key][0]["val_score"]:
                    best[key] = (r, cs, pt)

    with open(args.run / "probe.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(args.run / "probe_preds.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "target", "position", "layer", "probe_pred"])
        for tname, (r, cs, pt) in best.items():
            for c, v in zip(cs, pt):
                w.writerow([c, tname, r["position"], r["layer"], f"{v:.4f}"])

    for p in POSITIONS:
        print(f"\n{p}: layer  probe->z_all (test spearman)  probe->label (test AUC)")
        for layer in layers:
            a = next(r for r in rows if r["position"] == p and r["layer"] == layer and r["target"] == "z_all")
            b = next(r for r in rows if r["position"] == p and r["layer"] == layer and r["target"] == "label")
            print(f"   {layer:3d}   {a['test_spearman_z_all']:+.3f}   {b['test_auc_label']:.3f}")
    for tname, (r, _, _) in best.items():
        print(f"best by val for {tname}: {r['position']} layer {r['layer']}, "
              f"test spearman z_all {r['test_spearman_z_all']:+.3f}, test AUC label {r['test_auc_label']:.3f}")


if __name__ == "__main__":
    main()
