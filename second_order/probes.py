"""Truth, report and second-order probes with controls (plan, Phase 4).

* Truth probe (primary): L2 logistic regression on condition A and B
  activations from the train split, target the case's true CKD label.  Output
  logit = probe belief.
* Report probe (secondary): ridge regression on the same rows, target the
  latent ell (posterior mean, all 16 outputs) of that row's condition.
* One probe per layer and position; penalty chosen per layer on validation;
  the layer per position is chosen on validation and frozen.  Probes are
  never trained on C or D.  Each training row is one replay (4 per case).
* Readouts are averaged per case and condition over the four replays
  (``all``) and over the halves numeric + Likert (``h1``) and bin + plain
  decision (``h2``).
* Controls: shuffled-label probe at the selected layer and penalty; profile
  fields -> label logistic regression with all fields and with the partial
  set; at P1, readouts in C/D conditions sharing a token prefix must agree.
* Second-order probe (secondary): ridge on C/D activations at P3 with ell_C,
  ell_D targets, where C/D responses exist; cosine with the report probe and
  cross-performance.

    python -m second_order.probes --model gemma3_4b
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import r2_score, roc_auc_score

from latent_beliefs.ckd.cases import PROFILE_FEATURES
from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values
from latent_beliefs.latent.model import load_fit, restore_cell
from second_order import beliefs as BL
from second_order import config as C
from second_order.activations import HALF, POSITIONS
from second_order.conditions import CONDITIONS, withheld_fields
from second_order.fit_pooled import posteriors
from second_order.prompts_second_order import cases
from second_order.responses import split_table

C_GRID = (0.001, 0.01, 0.1)
ALPHA_GRID = (10.0, 100.0, 1000.0, 10000.0)
CONTINUOUS = ("ridageyr", "bmxbmi", "sodium_mg_mean")


def latent_targets(model: str) -> pd.DataFrame:
    """case_id, condition -> ell_all (pooled fit if run; else released fit for A)."""
    path = C.LATENTS / f"{model}.parquet"
    if path.exists():
        lat = pd.read_parquet(path)
        lat = lat[lat.fit == "pooled"]
        return lat[["case_id", "condition", "ell_all"]]
    source = C.split_source(model)
    first = pd.read_csv(C.FIRST_ORDER_DATASET)
    first = first[first.model_key == source]
    parameters, metadata = load_fit(C.RELEASE_MAIN_FIT)
    m = restore_cell(parameters, metadata, "ckd", source, CKD_SPECS)
    post = posteriors(m, first.reset_index(drop=True))
    return pd.DataFrame({"case_id": first.source_prompt_id.to_numpy(), "condition": "A",
                         "ell_all": post.ell_all.to_numpy()})


def profile_design(fields) -> pd.DataFrame:
    frame = cases()
    parts = []
    for f in fields:
        if f in CONTINUOUS:
            v = frame[f].astype(float)
            parts.append(pd.DataFrame({f: v.fillna(v.mean()), f"{f}_na": v.isna().astype(float)}))
        else:
            parts.append(pd.get_dummies(frame[f].fillna(-1).astype(int).astype(str), prefix=f,
                                        dtype=float))
    return pd.concat(parts, axis=1)


def standardise(train: np.ndarray):
    mu = train.mean(axis=0)
    sd = train.std(axis=0) + 1e-6
    return mu, sd


def auroc(y, s) -> float:
    return float(roc_auc_score(y, s)) if len(np.unique(y)) == 2 else np.nan


def case_mean(index: pd.DataFrame, scores: np.ndarray, rows: pd.Series) -> pd.Series:
    part = index[rows].assign(score=scores[rows.to_numpy()])
    return part.groupby("case_id").score.mean()


def run(model: str) -> None:
    root = C.ACTIVATIONS / model / "packed"
    index = pd.read_parquet(root / "index.parquet")
    n_layers = json.loads((C.ACTIVATIONS / model / "provenance.json").read_text())["layers"]
    splits = split_table(model)
    index["split"] = index.case_id.map(splits).fillna("none")
    index["label"] = index.case_id.map(cases()["ckd"]).astype(int)
    index["framing"] = index.condition.map(lambda c: CONDITIONS[c].framing)
    targets = latent_targets(model).set_index(["case_id", "condition"]).ell_all
    index["ell"] = [targets.get((c, k), np.nan) for c, k in zip(index.case_id, index.condition)]
    AB = index.condition.isin(["A", "B"])
    out_dir = C.RESULTS / "probes" / model
    out_dir.mkdir(parents=True, exist_ok=True)

    belief_parts, search, validation, weights = [], [], [], {}
    for position in POSITIONS:
        best = {"truth": (-np.inf, None), "report": (-np.inf, None)}
        layer_scores = {}
        for layer in range(n_layers):
            X = np.load(root / f"{position}_L{layer:02d}.npy", mmap_mode="r")
            finite = pd.Series(np.isfinite(X[:, 0]), index=index.index)
            tr = AB & (index.split == "train") & finite
            va = AB & (index.split == "val") & finite
            if tr.sum() < 20:
                continue
            mu, sd = standardise(np.asarray(X[tr.to_numpy()]))
            Z = (np.asarray(X) - mu) / sd
            Z[~finite.to_numpy()] = 0.0
            # truth probe
            t_best = (-np.inf, None, None)
            for c in C_GRID:
                clf = LogisticRegression(C=c, max_iter=3000).fit(Z[tr], index.label[tr])
                s = clf.decision_function(Z)
                val_cases = case_mean(index, s, va & (index.condition == "A"))
                score = auroc(index.groupby("case_id").label.first().loc[val_cases.index], val_cases)
                search.append({"model": model, "position": position, "layer": layer,
                               "probe_type": "truth", "penalty": c, "val_auroc_A": score})
                if score > t_best[0]:
                    t_best = (score, c, clf)
            # report probe
            r_best = (-np.inf, None, None)
            trr, var_ = tr & index.ell.notna(), va & index.ell.notna()
            for alpha in ALPHA_GRID:
                reg = Ridge(alpha=alpha).fit(Z[trr], index.ell[trr])
                s = reg.predict(Z)
                vc = case_mean(index, s, var_)
                ell_case = index[var_].groupby("case_id").ell.first().loc[vc.index]
                score = r2_score(ell_case, vc)
                search.append({"model": model, "position": position, "layer": layer,
                               "probe_type": "report", "penalty": alpha, "val_r2": score})
                if score > r_best[0]:
                    r_best = (score, alpha, reg)
            for ptype, (score, pen, est) in (("truth", t_best), ("report", r_best)):
                s = est.decision_function(Z) if ptype == "truth" else est.predict(Z)
                s = np.where(finite.to_numpy(), s, np.nan)
                layer_scores[(ptype, layer)] = (score, pen, s, est, mu, sd)
                if score > best[ptype][0]:
                    best[ptype] = (score, layer)
                belief_parts.append(readout_rows(model, index, s, ptype, position, layer))
            del Z
        # controls and validation at the selected layers
        for ptype in ("truth", "report"):
            if best[ptype][1] is None:
                continue
            layer = best[ptype][1]
            score, pen, s, est, mu, sd = layer_scores[(ptype, layer)]
            weights[(position, ptype)] = (est.coef_.ravel() / sd)
            validation.append(validate(model, index, root, position, layer, ptype, pen, s, mu, sd))
        sel = pd.DataFrame([{"model": model, "position": p, "probe_type": t, "layer": l}
                            for p in [position] for t, (_, l) in best.items() if l is not None])
        sel.to_csv(out_dir / f"selected_{position}.csv", index=False)
    probe = pd.concat(belief_parts, ignore_index=True)
    selected = pd.concat([pd.read_csv(out_dir / f"selected_{p}.csv") for p in POSITIONS
                          if (out_dir / f"selected_{p}.csv").exists()], ignore_index=True)
    selected.to_csv(out_dir / "selected_layers.csv", index=False)
    BL.write(model, "probe", probe)
    pd.DataFrame(search).to_csv(out_dir / "layer_search.csv", index=False)
    val = pd.DataFrame(validation)
    val = val.merge(profile_baseline(model, index), how="cross")
    val["fidelity_mean_logprob_per_token_A"] = fidelity(index)
    val.to_csv(out_dir / "probe_validation.csv", index=False)
    so = second_order_probe(model, index, root, selected, weights)
    if not so.empty:
        so.to_csv(out_dir / "second_order_probe.csv", index=False)
    print(val.to_string(index=False))


def readout_rows(model, index, s, ptype, position, layer) -> pd.DataFrame:
    frame = index[["case_id", "condition", "split", "framing", "replay"]].assign(score=s)
    frame = frame[np.isfinite(frame.score)]
    parts = []
    for half, sub in (("all", frame), ("h1", frame[frame.replay.map(HALF) == "h1"]),
                      ("h2", frame[frame.replay.map(HALF) == "h2"])):
        g = sub.groupby(["case_id", "condition", "split", "framing"]).score.mean().reset_index()
        g["half"] = half
        parts.append(g)
    out = pd.concat(parts, ignore_index=True).rename(columns={"score": "value"})
    out["model"], out["method"], out["fit"] = model, "probe", "frozen"
    out["layer"], out["position"], out["probe_type"] = layer, position, ptype
    return out


def validate(model, index, root, position, layer, ptype, pen, s, mu, sd) -> dict:
    X = np.load(root / f"{position}_L{layer:02d}.npy", mmap_mode="r")
    finite = np.isfinite(X[:, 0])
    AB = index.condition.isin(["A", "B"]).to_numpy()
    tr = AB & (index.split == "train").to_numpy() & finite
    te_A = (index.condition == "A").to_numpy() & (index.split == "test").to_numpy() & finite
    Z = (np.asarray(X) - mu) / sd
    Z[~finite] = 0.0
    labels_case = index.groupby("case_id").label.first()
    test_case = case_mean(index, s, pd.Series(te_A, index=index.index))
    row = {"model": model, "position": position, "probe_type": ptype, "layer": layer, "penalty": pen,
           "n_train_rows": int(tr.sum()), "n_test_cases_A": int(len(test_case)),
           "test_auroc_A": auroc(labels_case.loc[test_case.index], test_case)}
    ell_A = index[te_A].groupby("case_id").ell.first().loc[test_case.index]
    ok = ell_A.notna()
    row["test_corr_with_ell_A"] = float(np.corrcoef(test_case[ok], ell_A[ok])[0, 1]) if ok.sum() > 2 else np.nan
    if ptype == "truth":
        rng = np.random.default_rng(int(C.load()["seed"]))
        y = index.label.to_numpy()[tr].copy()
        rng.shuffle(y)
        shuf = LogisticRegression(C=pen, max_iter=3000).fit(Z[tr], y).decision_function(Z)
        sc = case_mean(index, shuf, pd.Series(te_A, index=index.index))
        row["shuffled_label_test_auroc_A"] = auroc(labels_case.loc[sc.index], sc)
    if ptype == "report":
        row["test_r2_ell_A"] = float(r2_score(ell_A[ok], test_case[ok])) if ok.sum() > 2 else np.nan
    # Test AUROC of the readout in every condition, and P1 prefix agreement.
    for cond in index.condition.unique():
        m = (index.condition == cond).to_numpy() & (index.split == "test").to_numpy() & finite
        if m.sum():
            cm = case_mean(index, s, pd.Series(m, index=index.index))
            row[f"test_auroc_{cond}"] = auroc(labels_case.loc[cm.index], cm)
    if position == "P1":
        frame = index.assign(score=s)
        piv = frame.pivot_table(index=["case_id", "replay"], columns="condition", values="score")
        cd = [c for c in ("C_john", "D_john", "C_user", "D_user") if c in piv]
        if len(cd) > 1:
            row["P1_max_abs_diff_across_C_D"] = float(np.nanmax(np.abs(piv[cd].sub(piv[cd[0]], axis=0)).to_numpy()))
        if "A" in piv and cd:
            row["P1_max_abs_diff_A_vs_C_john"] = float(np.nanmax(np.abs(piv["A"] - piv[cd[0]])))
    return row


def profile_baseline(model: str, index: pd.DataFrame) -> pd.DataFrame:
    """Profile fields -> CKD label; test AUROC and correlation with ell_A (test cases)."""
    case_split = index.groupby("case_id").split.first()
    labels = cases()["ckd"].astype(int)
    ell = index[index.condition == "A"].groupby("case_id").ell.first()
    out = {}
    for name, fields in (("all_fields", PROFILE_FEATURES),
                         ("partial_fields", [f for f in PROFILE_FEATURES if f not in withheld_fields()])):
        X = profile_design(fields).loc[case_split.index]
        tr = (case_split == "train").to_numpy()
        te = (case_split == "test").to_numpy()
        mu, sd = standardise(X.to_numpy()[tr])
        Z = (X.to_numpy() - mu) / sd
        clf = LogisticRegression(C=1.0, max_iter=3000).fit(Z[tr], labels.loc[X.index][tr])
        s = pd.Series(clf.decision_function(Z), index=X.index)
        out[f"profile_baseline_{name}_test_auroc"] = auroc(labels.loc[X.index][te], s[te])
        e = ell.reindex(X.index[te])
        ok = e.notna().to_numpy()
        out[f"profile_baseline_{name}_test_corr_with_ell_A"] = float(np.corrcoef(s[te][ok], e[ok])[0, 1])
    return pd.DataFrame([out])


def fidelity(index: pd.DataFrame) -> float:
    a = index[(index.condition == "A") & index.has_response & (index.n_response_tokens > 0)]
    return float(a.logprob_sum.sum() / a.n_response_tokens.sum()) if len(a) else np.nan


def second_order_probe(model, index, root, selected, weights) -> pd.DataFrame:
    cd = index.condition.isin(["C_john", "D_john", "C_user", "D_user"]) & index.ell.notna()
    sel = selected[(selected.position == "P3") & (selected.probe_type == "report")]
    if sel.empty or ("P3", "report") not in weights:
        return pd.DataFrame()
    layer = int(sel.layer.iloc[0])
    X = np.load(root / f"P3_L{layer:02d}.npy", mmap_mode="r")
    finite = np.isfinite(X[:, 0])
    tr = (cd & (index.split == "train")).to_numpy() & finite
    te = (cd & (index.split == "test")).to_numpy() & finite
    if tr.sum() < 20 or te.sum() < 5:
        return pd.DataFrame([{"model": model, "note": f"too few C/D rows with P3 and latents "
                              f"(train {int(tr.sum())}, test {int(te.sum())})"}])
    mu, sd = standardise(np.asarray(X[tr]))
    Z = (np.asarray(X) - mu) / sd
    so = Ridge(alpha=1000.0).fit(Z[tr], index.ell[tr])
    w_so = so.coef_ / sd
    w_rep = weights[("P3", "report")]
    cos = float(w_so @ w_rep / (np.linalg.norm(w_so) * np.linalg.norm(w_rep)))
    ab_te = index.condition.isin(["A", "B"]).to_numpy() & (index.split == "test").to_numpy() & finite & index.ell.notna().to_numpy()
    raw = np.asarray(X)
    rep_pred = raw @ w_rep
    rep_pred = rep_pred - rep_pred[tr].mean() + index.ell[tr].mean()
    return pd.DataFrame([{
        "model": model, "layer": layer, "n_train_rows": int(tr.sum()), "cosine_with_report_probe": cos,
        "so_probe_r2_on_CD_test": float(r2_score(index.ell[te], so.predict(Z[te]))),
        "so_probe_r2_on_AB_test": float(r2_score(index.ell[ab_te], so.predict(Z[ab_te]))) if ab_te.sum() > 2 else np.nan,
        "report_probe_corr_on_CD_test": float(np.corrcoef(rep_pred[te], index.ell[te])[0, 1]),
    }])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    run(args.model)


if __name__ == "__main__":
    main()
