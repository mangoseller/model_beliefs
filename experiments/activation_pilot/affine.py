"""Exploratory rerun of the flexible pilot with an unpenalized shift intercept."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import torch

import experiments.activation_pilot.fit as base
from experiments.activation_pilot.positions import sha256


def fit_affine(model, x, y, indices, penalty):
    w = torch.zeros(x.shape[1], dtype=torch.float64, requires_grad=True)
    b = torch.zeros((), dtype=torch.float64, requires_grad=True)
    optimizer = torch.optim.LBFGS([w, b], max_iter=100, tolerance_grad=1e-7,
                                  tolerance_change=1e-9, line_search_fn='strong_wolfe')

    def closure():
        optimizer.zero_grad()
        objective = base.loss(base.predict(model, y[indices], x[indices] @ w + b), y[indices])
        objective = objective + penalty * w.square().sum()
        objective.backward()
        return objective

    optimizer.step(closure)
    if not torch.isfinite(w).all() or not torch.isfinite(b):
        raise ValueError('Nonfinite fitted affine probe')
    return w.detach(), b.detach()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--larger', action='store_true')
    group.add_argument('--both', action='store_true')
    args = parser.parse_args()
    if args.both:
        for extra in [[], ['--larger']]:
            subprocess.run([sys.executable, '-u', __file__, *extra], check=True)
        return
    started = time.monotonic()
    size = '12b' if args.larger else '4b'
    root = Path('build/activation_pilot_12b' if args.larger else 'build/activation_pilot')
    previous = root / 'flexible'
    out = root / 'affine'
    out.mkdir(exist_ok=False)
    torch.set_num_threads(2)
    torch.manual_seed(base.SEED)
    base.FLEXIBLE = True
    base.MODEL_KEY = f'gemma3_{size}'
    df = base.data()
    train = np.flatnonzero(df.split == 'train')
    val = np.flatnonzero(df.split == 'val')
    test = np.flatnonzero(df.split == 'test')
    alltrain = np.concatenate([train, val])
    y = torch.tensor(df[base.NAMES].to_numpy(float), dtype=torch.float64)
    model = base.new_model()
    model.load_state_dict(torch.load(previous / 'baseline_selection.pt', weights_only=True))
    base.freeze(model)
    old_selection = json.loads((previous / 'selection.json').read_text())
    extraction = json.loads((root / 'extraction.json').read_text())
    layers = (np.unique(np.linspace(0, extraction['layers'] - 1, 8).round().astype(int)) + 1).tolist()
    arrays = []
    for case in df.source_prompt_id:
        with np.load(root / 'activations' / f'{case}.npz', allow_pickle=False) as cache:
            arrays.append(cache['h'][np.asarray(layers) - 1])
    h = np.stack(arrays).astype(np.float64)
    if not np.isfinite(h).all():
        raise ValueError('Nonfinite activations')
    config = dict(model_key=base.MODEL_KEY, exploratory=True,
                  reason='Follow-up on already inspected pilot test cases',
                  shift='centered_scaled_activation @ w + b; b unpenalized',
                  layers=layers, penalties=[.001, .01, .1, 1.], seed=base.SEED,
                  original_selection=old_selection, extraction=extraction,
                  frozen_baseline_hashes={name: sha256(previous / name)
                                          for name in ['baseline_selection.pt', 'baseline_final.pt']},
                  source_sha256={name: sha256(Path(__file__).with_name(name))
                                 for name in ['affine.py', 'fit.py']},
                  dataset_sha256=sha256('build/datasets/ckd.csv'))
    (out / 'config.json').write_text(json.dumps(config, indent=2))
    rows = []
    for index, layer in enumerate(layers):
        x, _, _ = base.features(h[:, index], train)
        for penalty in config['penalties']:
            w, b = fit_affine(model, x, y, train, penalty)
            with torch.no_grad():
                score = float(base.loss(base.predict(model, y[val], x[val] @ w + b), y[val]))
            row = dict(layer=layer, penalty=penalty, intercept=float(b), validation_log_loss=score)
            rows.append(row)
            pd.DataFrame(rows).to_csv(out / 'layer_search.csv', index=False)
            print(json.dumps(dict(model=size, **row)), flush=True)
    best = min(rows, key=lambda row: row['validation_log_loss'])
    raw_x = base.covariates(df, train)
    raw_grid = []
    for penalty in config['penalties']:
        w, b = fit_affine(model, raw_x, y, train, penalty)
        with torch.no_grad():
            score = float(base.loss(base.predict(model, y[val], raw_x[val] @ w + b), y[val]))
        raw_grid.append(dict(penalty=penalty, intercept=float(b), validation_log_loss=score))
    raw_best = min(raw_grid, key=lambda row: row['validation_log_loss'])
    # Lock the complete validation selection before computing any test predictions.
    (out / 'selection.json').write_text(json.dumps(dict(internal=best, raw_profile=raw_best), indent=2))
    pd.DataFrame(raw_grid).to_csv(out / 'raw_profile_search.csv', index=False)
    final = base.new_model()
    final.load_state_dict(torch.load(previous / 'baseline_final.pt', weights_only=True))
    base.freeze(final)
    shifts = {'behavior_only': torch.zeros(len(df), dtype=torch.float64)}
    probe_parameters = {}
    for name, selection in [('affine', best), ('affine_original_config', old_selection)]:
        x, mean, scale = base.features(h[:, layers.index(selection['layer'])], alltrain)
        w, b = fit_affine(final, x, y, alltrain, selection['penalty'])
        shifts[name] = x @ w + b
        probe_parameters[name] = dict(intercept=float(b), layer=selection['layer'], penalty=selection['penalty'])
        np.savez(out / f'{name}_probe.npz', w=w.numpy(), b=b.numpy(), mean=mean, scale=scale,
                 layer=selection['layer'], penalty=selection['penalty'])
    empty = torch.empty((len(df), 0), dtype=torch.float64)
    _, intercept = fit_affine(final, empty, y, alltrain, 0.)
    shifts['intercept_only'] = intercept.expand(len(df))
    probe_parameters['intercept_only'] = dict(intercept=float(intercept))
    raw_x = base.covariates(df, alltrain)
    rw, rb = fit_affine(final, raw_x, y, alltrain, raw_best['penalty'])
    shifts['raw_profile_affine'] = raw_x @ rw + rb
    np.savez(out / 'raw_profile_probe.npz', w=rw.numpy(), b=rb.numpy())
    x, _, _ = base.features(h[:, layers.index(best['layer'])], alltrain)
    shuffled = x.clone()
    rng = np.random.default_rng(base.SEED)
    for indices in [alltrain, test]:
        shuffled[indices] = x[rng.permutation(indices)]
    sw, sb = fit_affine(final, shuffled, y, alltrain, best['penalty'])
    shifts['shuffled_affine'] = shuffled @ sw + sb
    np.savez(out / 'shuffled_probe.npz', w=sw.numpy(), b=sb.numpy())
    with torch.no_grad():
        predictions = {name: base.predict(final, y[test], shift[test]).numpy() for name, shift in shifts.items()}
        predictions['activation_only_affine'] = base.predict(final, y[test], shifts['affine'][test], evidence=False).numpy()
    old = pd.read_csv(previous / 'test_predictions.csv')
    def old_prediction(name):
        return old.pivot(index='case_id', columns='target', values=name).loc[
            df.iloc[test].source_prompt_id, base.NAMES[8:]].to_numpy()
    baseline_error = float(np.max(np.abs(predictions['behavior_only'] - old_prediction('behavior_only'))))
    if baseline_error > 1e-12:
        raise ValueError(f'Frozen baseline does not reproduce original predictions: {baseline_error}')
    predictions['original_no_intercept'] = old_prediction('augmented')
    predictions['original_raw_profile'] = old_prediction('behavior_plus_raw_profile')
    altered = y[test].clone()
    altered[:, 8:] = 1 - altered[:, 8:]
    with torch.no_grad():
        leakage = float(np.max(np.abs(base.predict(final, altered, shifts['affine'][test]).numpy() - predictions['affine'])))
    if leakage != 0:
        raise ValueError('Withheld-target invariance failed')
    # Verify the exact equivalent affine readout in uncentered residual coordinates.
    with np.load(out / 'affine_probe.npz') as saved:
        raw_w = saved['w'] / max(float(saved['scale']), 1e-8)
        raw_b = float(saved['b']) - saved['mean'] @ raw_w
        uncentered_shift = h[:, layers.index(best['layer'])] @ raw_w + raw_b
    centering_error = float(np.max(np.abs(uncentered_shift - shifts['affine'].numpy())))
    if centering_error > 1e-9:
        raise ValueError(f'Affine centering equivalence failed: {centering_error}')
    fine = base.new_model(256)
    parameters = dict(final.named_parameters())
    with torch.no_grad():
        for name, parameter in fine.named_parameters():
            parameter.copy_(parameters[name])
        fine.prior_knot_logits.copy_(final.prior_knot_logits)
        fine_predictions = {name: base.predict(fine, y[test], shift[test]).numpy() for name, shift in shifts.items()}
    actual = y[test, 8:].numpy()
    metrics, losses = {}, {}
    for name, prediction in predictions.items():
        metrics[name], losses[name] = base.metrics(actual, prediction)
    draws = rng.integers(0, len(test), size=(10000, len(test)))
    comparisons = {}
    for name in ['behavior_only', 'original_no_intercept', 'affine_original_config', 'intercept_only', 'raw_profile_affine']:
        delta = losses[name] - losses['affine']
        comparisons[name] = dict(comparator_minus_affine=float(delta.mean()),
                                 ci95=np.quantile(delta[draws].mean(1), [.025, .975]).tolist())
    quadrature = {name: dict(max_prediction_change=float(np.max(np.abs(predictions[name] - prediction))),
                             log_loss_256=base.metrics(actual, prediction)[0]['log_loss'])
                  for name, prediction in fine_predictions.items()}
    result = dict(model_key=base.MODEL_KEY, exploratory=True, selection=best,
                  n_train=len(train), n_val=len(val), n_test=len(test), parameters=probe_parameters,
                  metrics=metrics, paired_case_bootstrap=comparisons,
                  baseline_reproduction_max_error=baseline_error, target_leakage_max_error=leakage,
                  centering_equivalence_max_error=centering_error, quadrature=quadrature,
                  quadrature_pass=all(row['max_prediction_change'] < .001 for row in quadrature.values()),
                  elapsed_seconds=time.monotonic() - started)
    records = [dict(case_id=case, target=target, actual=actual[i, j],
                    **{name: float(prediction[i, j]) for name, prediction in predictions.items()})
               for i, case in enumerate(df.iloc[test].source_prompt_id) for j, target in enumerate(base.NAMES[8:])]
    pd.DataFrame(records).to_csv(out / 'test_predictions.csv', index=False)
    (out / 'results.json').write_text(json.dumps(result, indent=2))
    table = pd.DataFrame(metrics).T.to_html(float_format=lambda x: f'{x:.5f}')
    report = f'''<!doctype html><html><meta charset="utf-8"><title>Affine pilot: {size}</title>
<style>body{{font:17px/1.6 system-ui;max-width:1000px;margin:40px auto}}td,th{{padding:8px}}pre{{white-space:pre-wrap}}</style>
<h1>Unpenalized activation-shift intercept: {size}</h1>
<p>Exploratory reuse of the inspected 103-case test set. Same frozen flexible baselines,
original assistant-prefix token, eight-layer/four-penalty validation grid, and train/val/test splits.
The intercept is fitted with the activation direction and is not penalized. Original-config
results hold the old selected layer and penalty fixed to isolate adding the intercept.</p>
<p>Selected layer {best['layer']}; L2 {best['penalty']}; final intercept {probe_parameters['affine']['intercept']:.5f}.</p>
{table}<p>Positive paired differences favor the affine probe. Intervals are conditional on fitted models.</p>
<pre>{json.dumps(comparisons, indent=2)}</pre><p>Quadrature checks passed: {result['quadrature_pass']}.</p>
<p><a href="results.json">Metrics and checks</a> · <a href="layer_search.csv">Validation search</a> ·
<a href="test_predictions.csv">Test predictions</a></p></html>'''
    (out / 'report.html').write_text(report)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
