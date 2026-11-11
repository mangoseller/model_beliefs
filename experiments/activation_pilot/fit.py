"""Train-only probe fitting, validation layer selection, and locked test evaluation.

Uses the repository's shared-threshold likelihood and seven-knot latent prior.
Primary task: eight threshold/cost responses predict four betting responses.
Probability reports anchor the baseline during fitting, but are not test evidence.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from sklearn.metrics import roc_auc_score
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from latent_beliefs.holdout.common import SharedFamilyLatentModel, GLOBAL_LINK
from latent_beliefs.latent.model import OutputSpec, FitConfig, fit_model, load_fit, restore_cell, ProbabilisticLatentModel
from latent_beliefs.latent.domains import CKD_SPECS
from latent_beliefs.ckd.cases import PROFILE_FEATURES

OUT = Path('build/activation_pilot')
ROOT = OUT
FLEXIBLE = False
MODEL_KEY = 'gemma3_4b'
SEED = 20261107
NAMES = ['threshold_05pct', 'template_01_threshold_10pct',
         'template_02_threshold_20pct', 'template_03_threshold_33pct',
         'cost_fn19', 'template_09_cost_fn9', 'template_08_cost_fn4',
         'template_07_cost_fn2', 'bet_payoff_19to1', 'bet_payoff_9to1',
         'bet_payoff_4to1', 'bet_payoff_2to1']
TAU = np.tile([.05, .1, .2, 1/3], 3)
SPECS = [OutputSpec('probability', 'probability')] + [
    OutputSpec(n, 'binary', float(t)) for n, t in zip(NAMES, TAU)]


def new_model(q=128):
    if FLEXIBLE:
        return ProbabilisticLatentModel(SPECS,quadrature_points=q)
    return SharedFamilyLatentModel(SPECS, binary_families={n: GLOBAL_LINK for n in NAMES},
                                  quadrature_points=q)


def data():
    df = pd.read_csv('build/datasets/ckd.csv')
    result = df.loc[df.model_key == MODEL_KEY].reset_index(drop=True)
    # Align the 12B experiment to the 4B split for paired model-size comparisons.
    # Never reuse released 12B fits on these reassigned test cases.
    split = df.loc[df.model_key == 'gemma3_4b'].set_index('source_prompt_id')['split']
    result['split'] = result.source_prompt_id.map(split)
    assert result['split'].notna().all()
    return result


def fit_baseline(df):
    model = new_model()
    values = df[['probability'] + NAMES].to_numpy(float)
    info = fit_model(model, values[df.split == 'train'], values[df.split == 'val'],
                     FitConfig(epochs=12000, patience=250, progress_every=1000),
                     progress_label='baseline')
    torch.save(model.state_dict(), OUT / 'baseline_selection.pt')
    (OUT / 'baseline_selection.json').write_text(json.dumps(info, indent=2))
    return model, info


def predict(model, y, shift, evidence=True):
    s = model.logit_z_grid[None, :] + shift[:, None]
    t = torch.tensor(np.log(TAU / (1 - TAU)), dtype=torch.float64)
    if isinstance(model, SharedFamilyLatentModel):
        logits = model.family_intercepts[GLOBAL_LINK] + model.family_slope(GLOBAL_LINK) * (s[:, None, :] - t[None, :, None])
    else:
        intercept = torch.stack([model.binary_intercepts[n] for n in NAMES])
        slope = torch.stack([model.binary_slope(n) for n in NAMES])
        logits = intercept[None,:,None] + slope[None,:,None]*s[:,None,:]
    ll = y[:, :, None] * F.logsigmoid(logits) + (1-y[:, :, None]) * F.logsigmoid(-logits)
    logw = model.prior_log_weights()[None, :] + (ll[:, :8, :].sum(1) if evidence else 0)
    weights = torch.softmax(logw, dim=-1)
    return (weights[:, None, :] * torch.sigmoid(logits[:, 8:, :])).sum(-1)


def loss(p, y):
    return F.binary_cross_entropy(p.clamp(1e-10, 1-1e-10), y[:, 8:])


def features(raw, train):
    mean = raw[train].mean(0)
    # One scalar per layer retains the direction geometry and makes lambda comparable.
    scale = np.sqrt(np.mean((raw[train] - mean)**2))
    return torch.tensor((raw-mean)/max(scale, 1e-8), dtype=torch.float64), mean, scale


def fit_probe(model, x, y, train, penalty):
    w = torch.zeros(x.shape[1], dtype=torch.float64, requires_grad=True)
    optimizer = torch.optim.LBFGS([w], max_iter=100, tolerance_grad=1e-7,
                                  tolerance_change=1e-9, line_search_fn='strong_wolfe')
    def closure():
        optimizer.zero_grad()
        objective = loss(predict(model, y[train], x[train] @ w), y[train]) + penalty*w.square().sum()
        objective.backward()
        return objective
    optimizer.step(closure)
    return w.detach()


def freeze(model):
    for parameter in model.parameters():
        parameter.requires_grad_(False)


def covariates(df, train):
    """Raw prompt-feature control; excludes actual CKD label and derived risk score."""
    cases = pd.read_csv('data/release/sources/ckd/cases.csv')
    cases['case_id'] = cases.source_cohort + '__' + cases.prompt_id
    raw = cases.set_index('case_id').loc[df.source_prompt_id, PROFILE_FEATURES]
    continuous = ['ridageyr','bmxbmi','sodium_mg_mean']
    categorical = [c for c in PROFILE_FEATURES if c not in continuous]
    processor = ColumnTransformer([
        ('continuous', make_pipeline(SimpleImputer(strategy='median'),StandardScaler()), continuous),
        ('categorical', make_pipeline(SimpleImputer(strategy='most_frequent'),
                                     OneHotEncoder(handle_unknown='ignore',sparse_output=False)), categorical)])
    processor.fit(raw.iloc[train])
    encoded = processor.transform(raw)
    return features(encoded,train)[0]


def metrics(y, p):
    p = np.clip(p, 1e-10, 1-1e-10)
    per_case = -(y*np.log(p)+(1-y)*np.log1p(-p)).mean(1)
    aucs = [roc_auc_score(y[:,j],p[:,j]) for j in range(y.shape[1]) if np.unique(y[:,j]).size==2]
    return dict(log_loss=float(per_case.mean()), brier=float(((y-p)**2).mean()),
                mean_auc=float(np.mean(aucs))), per_case


def run():
    global OUT, ROOT, FLEXIBLE, MODEL_KEY
    torch.set_num_threads(2)
    torch.manual_seed(SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-only', action='store_true')
    parser.add_argument('--flexible', action='store_true')
    parser.add_argument('--larger', action='store_true')
    args = parser.parse_args()
    if args.larger:
        MODEL_KEY = 'gemma3_12b'
        ROOT = Path('build/activation_pilot_12b')
        OUT = ROOT
        OUT.mkdir(parents=True,exist_ok=True)
    if args.flexible:
        FLEXIBLE = True
        OUT = ROOT/'flexible'
        OUT.mkdir(parents=True,exist_ok=True)
    df = data()
    if not (OUT/'baseline_selection.pt').exists():
        model, info = fit_baseline(df)
    else:
        model = new_model()
        model.load_state_dict(torch.load(OUT/'baseline_selection.pt', weights_only=True))
        info = json.loads((OUT/'baseline_selection.json').read_text())
        if info['epochs_ran'] == 4000 and info['best_epoch'] >= 3750:
            # Initial short budget hit its cap while validation was still improving.
            # Extend using validation only, before layer selection or test evaluation.
            model, info = fit_baseline(df)
    if args.baseline_only:
        return
    freeze(model)
    y = torch.tensor(df[NAMES].to_numpy(float), dtype=torch.float64)
    train = np.flatnonzero(df.split == 'train')
    val = np.flatnonzero(df.split == 'val')
    test = np.flatnonzero(df.split == 'test')
    h = np.stack([np.load(ROOT/'activations'/f'{case}.npz')['h']
                  for case in df.source_prompt_id]).astype(np.float64)
    assert np.isfinite(h).all()
    layers = np.unique(np.linspace(0,h.shape[1]-1,8).round().astype(int))
    grid = []
    for layer in layers:
        x, _, _ = features(h[:,layer], train)
        for penalty in [.001, .01, .1, 1.]:
            w = fit_probe(model, x, y, train, penalty)
            with torch.no_grad():
                score = float(loss(predict(model,y[val],x[val]@w),y[val]))
            row = dict(layer=int(layer+1), penalty=penalty, validation_log_loss=score)
            grid.append(row)
            print(row,flush=True)
    pd.DataFrame(grid).to_csv(OUT/'layer_search.csv',index=False)
    best = min(grid, key=lambda r:r['validation_log_loss'])
    raw_x = covariates(df,train)
    raw_grid = []
    for penalty in [.001,.01,.1,1.]:
        raw_w = fit_probe(model,raw_x,y,train,penalty)
        with torch.no_grad():
            score = float(loss(predict(model,y[val],raw_x[val]@raw_w),y[val]))
        raw_grid.append(dict(penalty=penalty,validation_log_loss=score))
    raw_best = min(raw_grid,key=lambda r:r['validation_log_loss'])
    (OUT/'covariate_selection.json').write_text(json.dumps(raw_best,indent=2))
    # Lock selection before any test predictions are calculated.
    (OUT/'selection.json').write_text(json.dumps(best,indent=2))
    alltrain = np.concatenate([train,val])
    final = new_model()
    values = torch.tensor(df[['probability']+NAMES].to_numpy(float)[alltrain],dtype=torch.float64)
    optimizer = torch.optim.Adam(final.parameters(),lr=.02)
    for epoch in range(info['best_epoch']+1):
        optimizer.zero_grad()
        objective = -final.marginal_log_prob(values).mean()
        objective.backward()
        torch.nn.utils.clip_grad_norm_(final.parameters(),10.)
        optimizer.step()
    freeze(final)
    torch.save(final.state_dict(),OUT/'baseline_final.pt')
    x, mean, scale = features(h[:,best['layer']-1],alltrain)
    w = fit_probe(final,x,y,alltrain,best['penalty'])
    raw_x = covariates(df,alltrain)
    raw_w = fit_probe(final,raw_x,y,alltrain,raw_best['penalty'])
    np.savez(OUT/'probe.npz',w=w.numpy(),mean=mean,scale=scale)
    zero = torch.zeros(len(test),dtype=torch.float64)
    with torch.no_grad():
        baseline = predict(final,y[test],zero)
        augmented = predict(final,y[test],x[test]@w)
        activation_only = predict(final,y[test],x[test]@w,evidence=False)
        # Verify baseline identity against the repository's original posterior routine.
        observed = np.full((len(test),13),np.nan)
        observed[:,1:9] = y[test,:8].numpy()
        weights = final.posterior_log_weights(torch.tensor(observed)).exp()
        original = torch.stack([weights @ final.binary_logits(spec).sigmoid() for spec in SPECS[9:]],dim=1)
        identity_error = float((baseline-original).abs().max())
        altered = y[test].clone()
        altered[:,8:] = 1-altered[:,8:]
        leakage_error = float((predict(final,altered,x[test]@w)-augmented).abs().max())
    assert identity_error < 1e-10 and leakage_error == 0
    # Refit a matched shuffled-input control, permuting independently within partitions.
    rng = np.random.default_rng(SEED)
    shuffled = x.clone()
    for ix in [alltrain,test]:
        shuffled[ix] = x[rng.permutation(ix)]
    sw = fit_probe(final,shuffled,y,alltrain,best['penalty'])
    with torch.no_grad():
        shuffled_predictions = predict(final,y[test],shuffled[test]@sw)
    predictions = dict(behavior_only=baseline.numpy(), augmented=augmented.numpy(),
                       activation_only=activation_only.numpy(), shuffled=shuffled_predictions.numpy())
    with torch.no_grad():
        predictions['behavior_plus_raw_profile'] = predict(final,y[test],raw_x[test]@raw_w).numpy()
    # Contextual comparator: the paper's released, more flexible 16-output model.
    # It is not the nested baseline, and had more output types available in training.
    if MODEL_KEY == 'gemma3_4b':
        params, metadata = load_fit(Path('data/release/fits/main'))
        released = restore_cell(params, metadata, 'ckd', MODEL_KEY, CKD_SPECS)
        evidence = np.full((len(test), len(CKD_SPECS)), np.nan)
        for j, spec in enumerate(CKD_SPECS):
            if spec.name in NAMES[:8]:
                evidence[:,j] = df.iloc[test][spec.name]
        with torch.no_grad():
            rw = released.posterior_log_weights(torch.tensor(evidence)).exp()
            rspec = {spec.name: spec for spec in CKD_SPECS}
            predictions['paper_flexible_released'] = torch.stack([
                rw @ released.binary_logits(rspec[name]).sigmoid() for name in NAMES[8:]],dim=1).numpy()
    actual = y[test,8:].numpy()
    result = dict(model_key=MODEL_KEY, n_layers=h.shape[1], split_alignment='released Gemma 3 4B case split',
                  specification='flexible' if FLEXIBLE else 'shared_threshold', selection=best, n_train=len(train),n_val=len(val),n_test=len(test),
                  baseline_zero_shift_max_error=identity_error, target_leakage_max_error=leakage_error,
                  baseline_training=info,metrics={})
    losses = {}
    for name,p in predictions.items():
        result['metrics'][name],losses[name] = metrics(actual,p)
    delta = losses['behavior_only']-losses['augmented']
    draws = rng.integers(0,len(test),size=(10000,len(test)))
    ci = np.quantile(delta[draws].mean(1),[.025,.975])
    result['paired_log_loss_improvement'] = dict(mean=float(delta.mean()),ci95=ci.tolist(),bootstrap_cases=10000)
    result['paired_comparator_minus_augmented'] = {}
    for name, per_case in losses.items():
        difference = per_case-losses['augmented']
        result['paired_comparator_minus_augmented'][name] = dict(
            mean=float(difference.mean()),
            ci95=np.quantile(difference[draws].mean(1),[.025,.975]).tolist())
    # Check fixed fitted parameters and probe on a finer quadrature grid.
    fine = new_model(256)
    state = final.state_dict()
    parameters = dict(final.named_parameters())
    with torch.no_grad():
        for name,p in fine.named_parameters():
            p.copy_(parameters[name])
        fine.prior_knot_logits.copy_(final.prior_knot_logits)
        fine_p = predict(fine,y[test],x[test]@w)
    result['quadrature_128_vs_256_max_prediction_change'] = float((fine_p-augmented).abs().max())
    result['quadrature_256_augmented_log_loss'] = float(loss(fine_p,y[test]))
    records = []
    for k,i in enumerate(test):
        for j,name in enumerate(NAMES[8:]):
            records.append(dict(case_id=df.source_prompt_id[i],target=name,actual=actual[k,j],
                                **{key:float(p[k,j]) for key,p in predictions.items()}))
    pd.DataFrame(records).to_csv(OUT/'test_predictions.csv',index=False)
    (OUT/'results.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)


if __name__ == '__main__':
    run()
