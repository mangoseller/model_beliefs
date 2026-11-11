"""Resumable full scientific forecasting joint-belief experiment: initialize, search, lock, final.

Lock the selected cell(s) before evaluating their test cases. Rolling evaluation
can report a completed cell while the other predeclared searches continue.
All training/selection decisions are recorded in protocol.json and selection.json.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch

from experiments.forecast_beliefs.data import covariates, PROMPTS
from experiments.joint_beliefs.benchmark import fit_candidate
from experiments.joint_beliefs.model import JointBeliefModel
from latent_beliefs.latent.domains import FORECASTING_SPECS, likelihood_values
from latent_beliefs.latent.model import FitConfig, ProbabilisticLatentModel, fit_model

ROOT = Path('build/forecast_beliefs/full_experiment')
CURVES = ('linear', 'monotone_spline')
SIZES = ('4b', '12b')
METHODS = ('behavior', 'activation', 'raw_profile', 'shuffled')
PROTOCOL = dict(
    seed=20261108, domain='forecasting', sizes=list(SIZES), curves=list(CURVES),
    methods=list(METHODS), n_train=320, n_validation=80, n_test=100,
    ablation='activation_only: same jointly fitted activation model, no observed test responses; no extra training',
    layers='eight evenly spaced post-block layers per model, including first and last',
    penalties=[.001, .01, .1, 1.], epochs=12000, patience=250, min_delta=1e-5,
    learning_rate=.02, probe_learning_rate=.001, quadrature=128, threads=2,
    initialization='one start: training-only behavioral baseline, zero activation direction',
    selection='minimum unpenalized validation joint NLL over all sixteen outputs',
    final_refit='train+validation; baseline and joint epochs fixed by validation best epochs',
    shuffle='one deterministic independent case permutation per partition; matched selected activation layer and penalty; not a permutation test',
    numerical_policy='audit selected models on train and validation at doubled quadrature; if NLL change exceeds .001, repeat the selected fit at 256/512/1024 points until stable; no new layer or penalty search',
    evaluation_quadrature='start at max(256, fitting points); double to at most 2048 until all task mean and log-score changes <.001, preserving fitted parameters; flag failures',
    uncertainty='10000 paired case bootstrap draws; conditional on fitted/selected models; no multiplicity correction',
    exploratory=True,
    text_control='TF-IDF unigrams and bigrams, min_df=2, maximum 2048 features; vocabulary, IDF and normalization fit only on fitting cases; same shared scalar and four L2 penalties',
    calibration='Primary replication uses the unchanged joint likelihood. No test-selected calibration; calibration follow-up is outside this run.',
    split='Use 4B case split for both sizes; refit every model on aligned cases',
    limitations=['new domain selected after CKD analysis', 'quantized local activations vs hosted responses',
                 'one scenario split; studies can overlap across partitions; two model sizes', 'one shuffled fit per specification'],
)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def initialize():
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / 'protocol.json'
    if path.exists():
        if json.loads(path.read_text()) != PROTOCOL:
            raise ValueError('Existing experiment protocol differs')
    else:
        save_json(path, PROTOCOL)
    files = [Path(__file__), Path('experiments/forecast_beliefs/data.py'), Path('experiments/forecast_beliefs/extract.py'),
             Path('experiments/joint_beliefs/model.py'), Path('experiments/joint_beliefs/benchmark.py'),
             Path('latent_beliefs/latent/model.py'), Path('latent_beliefs/latent/domains.py')]
    snapshots = ROOT / 'source'
    snapshots.mkdir(exist_ok=True)
    hashes = {}
    for source in files:
        target = snapshots / str(source).replace('/', '__')
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError(f'Experiment source changed: {source}')
        target.write_bytes(source.read_bytes())
        hashes[str(source)] = digest(source)
    for source in [PROMPTS, Path('build/datasets/forecasting.csv')]:
        hashes[str(source)] = digest(source)
    path = ROOT/'input_hashes.json'
    if path.exists():
        assert json.loads(path.read_text()) == hashes, 'Experiment inputs changed'
    else:
        save_json(path, hashes)


def frame(size, test=False):
    df = pd.read_csv('build/datasets/forecasting.csv')
    splits = df.loc[df.model_key == 'gemma3_4b'].set_index('source_prompt_id')['split']
    df = df.loc[df.model_key == 'gemma3_' + size].copy()
    df['split'] = df.source_prompt_id.map(splits)
    assert df.split.notna().all()
    if not test:
        df = df.loc[df.split != 'test']
    return df.reset_index(drop=True)


def activation_arrays(size, df):
    folder = Path('build/forecast_beliefs') / size / 'extraction'
    meta = json.loads((folder / 'extraction.json').read_text())
    assert meta['complete'] and meta['cases'] == 500
    layers = np.unique(np.linspace(0, meta['layers'] - 1, 8).round().astype(int)) + 1
    values = []
    for case in df.source_prompt_id:
        with np.load(folder / 'activations' / f'{case}.npz') as cache:
            values.append(cache['h'][layers - 1].astype(np.float64))
    stacked = np.stack(values)
    assert np.isfinite(stacked).all()
    return {int(layer): stacked[:, j] for j, layer in enumerate(layers)}


def standardize(raw, indices):
    mean = raw[indices].mean(0)
    scale = max(float(np.sqrt(np.mean((raw[indices] - mean) ** 2))), 1e-8)
    return torch.tensor((raw - mean) / scale, dtype=torch.float64), dict(mean=mean, scale=scale)


def shuffled(x, groups):
    rng = np.random.default_rng(PROTOCOL['seed'] + 47)
    result = x.clone()
    for group in groups:
        result[group] = x[rng.permutation(group)]
    return result


def behavior(curve, points=128):
    return ProbabilisticLatentModel(FORECASTING_SPECS, response_curve=curve, quadrature_points=points)


def load_joint(path, curve, n_features, points):
    result = JointBeliefModel(behavior(curve, points), n_features)
    result.load_state_dict(torch.load(path, weights_only=True))
    return result


def nll_audit(model, y_train, x_train, y_val, x_val):
    with torch.no_grad():
        finer = model.with_quadrature(2 * len(model.behavior.z_grid))
        differences = {}
        for name, y, x in [('train', y_train, x_train), ('validation', y_val, x_val)]:
            coarse = float(-model.marginal_log_prob(y, x).mean())
            fine = float(-finer.marginal_log_prob(y, x).mean())
            differences[name] = dict(coarse_nll=coarse, fine_nll=fine, change=abs(coarse-fine))
    return dict(points=len(model.behavior.z_grid), partitions=differences,
                passed=all(d['change'] < .001 for d in differences.values()))


def get_baseline(cell, curve, y, train, val):
    out = cell / 'baseline'
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'result.json').exists():
        info = json.loads((out / 'result.json').read_text())
        b = behavior(curve, info['quadrature'])
        b.load_state_dict(torch.load(out / 'model.pt', weights_only=True))
        return b, info
    audits = []
    elapsed = 0.
    empty = torch.empty((len(y),0),dtype=torch.float64)
    for points in [128,256,512,1024]:
        # Fresh model at each precision; preserve original prior spline coordinates.
        b = JointBeliefModel(behavior(curve),0).with_quadrature(points).behavior
        start = time.perf_counter()
        info = fit_model(b, y[train].numpy(), y[val].numpy(),
                         FitConfig(epochs=PROTOCOL['epochs'], patience=PROTOCOL['patience'],
                                   min_delta=PROTOCOL['min_delta'], learning_rate=PROTOCOL['learning_rate'],
                                   progress_every=1000), progress_label=str(cell))
        elapsed += time.perf_counter()-start
        audit = nll_audit(JointBeliefModel(b,0),y[train],empty[train],y[val],empty[val])
        audits.append(audit)
        if audit['passed']: break
    info.update(seconds=elapsed, best_epoch_count=info['best_epoch']+1,
                quadrature=points, numerical_audits=audits, training_numerically_stable=audit['passed'])
    torch.save(b.state_dict(), out / 'model.pt')
    save_json(out / 'result.json', info)
    return b, info


def one_candidate(out, base, y, x, train, val, penalty, points=None):
    # A control-prefitting worker may reach the same candidate as the search.
    # Lock before checking completion so only one process can fit/write it.
    out.mkdir(parents=True, exist_ok=True)
    with (out / '.fit.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _one_candidate(out, base, y, x, train, val, penalty, points)


def _one_candidate(out, base, y, x, train, val, penalty, points=None):
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'result.json').exists():
        return json.loads((out / 'result.json').read_text())
    points = len(base.z_grid) if points is None else points
    model = JointBeliefModel(copy.deepcopy(base), x.shape[1])
    if points != len(base.z_grid):
        model = model.with_quadrature(points)
    options = SimpleNamespace(**{k: PROTOCOL[k] for k in
                              ['epochs', 'patience', 'min_delta', 'learning_rate', 'probe_learning_rate']},
                              penalty=penalty)
    info = fit_candidate(model, y[train], x[train], y[val], x[val], options, out)
    info.update(penalty=penalty, quadrature=points)
    torch.save(model.state_dict(), out / 'model.pt')
    save_json(out / 'result.json', info)
    return info


def search(size, curve=None):
    if (ROOT / 'selection_lock.json').exists():
        raise RuntimeError('Selection already locked; search cannot be rerun')
    df = frame(size)
    train = np.flatnonzero(df.split == 'train')
    val = np.flatnonzero(df.split == 'val')
    assert (len(train), len(val)) == (320, 80)
    y = torch.tensor(likelihood_values(df, FORECASTING_SPECS), dtype=torch.float64)
    raw_activations = activation_arrays(size, df)
    raw_x = covariates(df, train)
    for curve in ([curve] if curve is not None else CURVES):
        cell = ROOT / size / curve
        if (cell / 'selection.json').exists():
            continue
        print(f'START SEARCH {size} {curve}', flush=True)
        base, baseline_info = get_baseline(cell, curve, y, train, val)
        records = []
        for layer, raw in raw_activations.items():
            x, _ = standardize(raw, train)
            for penalty in PROTOCOL['penalties']:
                out = cell / 'candidates' / f'activation_l{layer}_p{penalty:g}'
                print(f'CANDIDATE {size} {curve} activation layer={layer} L2={penalty}', flush=True)
                info = one_candidate(out, base, y, x, train, val, penalty)
                records.append(dict(method='activation', layer=layer, path=str(out), **info))
                pd.DataFrame(records).to_csv(cell / 'search.csv', index=False)
        for penalty in PROTOCOL['penalties']:
            out = cell / 'candidates' / f'raw_profile_p{penalty:g}'
            print(f'CANDIDATE {size} {curve} raw_profile L2={penalty}', flush=True)
            info = one_candidate(out, base, y, raw_x, train, val, penalty)
            records.append(dict(method='raw_profile', layer=None, path=str(out), **info))
            pd.DataFrame(records).to_csv(cell / 'search.csv', index=False)
        selected = {method: min([r for r in records if r['method'] == method],
                               key=lambda r: r['best_validation_nll'])
                    for method in ['activation', 'raw_profile']}
        best = selected['activation']
        act_x, _ = standardize(raw_activations[best['layer']], train)
        shuffle_x = shuffled(act_x, [train, val])
        out = cell / 'candidates' / 'shuffled'
        info = one_candidate(out, base, y, shuffle_x, train, val, best['penalty'])
        selected['shuffled'] = dict(method='shuffled', layer=best['layer'], path=str(out), **info)
        # Numerical repairs use only train/validation and the already chosen configuration.
        for method, chosen in selected.items():
            x = raw_x if method == 'raw_profile' else shuffle_x if method == 'shuffled' else act_x
            audits = []
            initial_points = chosen['quadrature']
            for points in [p for p in [128, 256, 512, 1024] if p >= initial_points]:
                if points != initial_points:
                    out = cell / 'candidates' / f'{method}_numerical_refit_q{points}'
                    info = one_candidate(out, base, y, x, train, val, chosen['penalty'], points)
                    chosen = dict(method=method, layer=chosen['layer'], path=str(out), **info)
                model = load_joint(Path(chosen['path'])/'model.pt', curve, x.shape[1], points)
                audit = nll_audit(model, y[train], x[train], y[val], x[val])
                audits.append(audit)
                if audit['passed']:
                    break
            selected[method] = dict(chosen, numerical_audits=audits, training_numerically_stable=audits[-1]['passed'])
        torch.save(base.state_dict(), cell / 'baseline' / 'selected_model.pt')
        selected['behavior'] = dict(method='behavior', layer=None, penalty=0., **baseline_info)
        save_json(cell / 'selection.json', selected)
        print(f'SELECTION COMPLETE {size} {curve}', flush=True)


def lock(size=None, curve=None):
    selected = {}
    cells = [(size, curve)] if size is not None and curve is not None else [(s,c) for s in SIZES for c in CURVES]
    for model_size, response_curve in cells:
        path = ROOT / model_size / response_curve / 'selection.json'
        if not path.exists():
            raise RuntimeError(f'Search incomplete: {path}')
        selected[f'{model_size}/{response_curve}'] = digest(path)
    result = dict(selections=selected, protocol_sha256=digest(ROOT/'protocol.json'),
                  locked_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    path = ROOT / size / curve / 'selection_lock.json' if size is not None and curve is not None else ROOT / 'selection_lock.json'
    if path.exists():
        old = json.loads(path.read_text())
        if old['selections'] != selected:
            raise RuntimeError('Locked selections changed')
    else:
        save_json(path, result)


def fixed_fit(model, y, x, epochs, penalty, probe=True):
    groups = [{'params': model.behavior.parameters(), 'lr': PROTOCOL['learning_rate']}]
    if probe:
        groups.append({'params': [model.w], 'lr': PROTOCOL['probe_learning_rate']})
    optimizer = torch.optim.Adam(groups)
    for epoch in range(epochs):
        optimizer.zero_grad(set_to_none=True)
        nll = (-model.marginal_log_prob(y, x).mean() if probe
               else -model.behavior.marginal_log_prob(y).mean())
        objective = nll + penalty * model.w.square().sum()
        if not torch.isfinite(objective):
            raise RuntimeError('Nonfinite final-fit objective')
        objective.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 10., error_if_nonfinite=True)
        optimizer.step()
        if (epoch + 1) % 1000 == 0:
            print(f'FINAL epoch={epoch+1}/{epochs} train_nll={float(nll.detach()):.6f}', flush=True)


def evaluation_tasks():
    binary = [i for i, s in enumerate(FORECASTING_SPECS) if s.kind == 'binary']
    reports = [i for i, s in enumerate(FORECASTING_SPECS) if s.kind != 'binary']
    yield 'reports_to_decisions', reports, binary
    yield 'decisions_to_reports', binary, reports
    yield 'probability_to_decisions', [0], binary
    yield 'decisions_to_probability', binary, [0]
    for j in range(16):
        yield 'leave_one_out', [k for k in range(16) if k != j], [j]


def evaluate_arrays(model, y, x, internal_only=False):
    records = []
    with torch.no_grad():
        # Compute each likelihood and conditional mean only once per quadrature.
        s = model.scores(x)
        ll = model.log_likelihood_by_output(y, x)
        conditional = []
        ordinal_probs = {}
        for j, spec in enumerate(FORECASTING_SPECS):
            if spec.kind == 'probability':
                means = s.sigmoid()
            elif spec.kind == 'binary':
                means = model.response_score(spec, s).sigmoid()
            else:
                probabilities = model.ordinal_probabilities(spec, s)
                ordinal_probs[j] = probabilities
                categories = torch.arange(1, 6, dtype=torch.float64)
                means = probabilities @ categories
            conditional.append(means)
        for task, observed, targets in evaluation_tasks():
            if internal_only:
                observed = []
            logw = model.behavior.prior_log_weights()[None, :] + ll[:, observed, :].sum(1)
            logw -= torch.logsumexp(logw, 1, keepdim=True)
            weights = logw.exp()
            for target in targets:
                prediction = (weights * conditional[target]).sum(1)
                nll = -torch.logsumexp(logw + ll[:, target, :], 1)
                record = dict(task=task, target=target, prediction=prediction.numpy(), nll=nll.numpy())
                if target in ordinal_probs:
                    probs = (weights[..., None] * ordinal_probs[target]).sum(1)
                    record['category_probabilities'] = probs.numpy()
                records.append(record)
    return records


def evaluate_stable(model, y, x, internal_only=False):
    points = max(256, len(model.behavior.z_grid))
    previous = evaluate_arrays(model.with_quadrature(points), y, x, internal_only)
    audits = []
    while points < 2048:
        points *= 2
        current = evaluate_arrays(model.with_quadrature(points), y, x, internal_only)
        prediction_change = max(float(np.max(np.abs(a['prediction']-b['prediction']))) for a,b in zip(previous,current))
        score_change = max(float(np.max(np.abs(a['nll']-b['nll']))) for a,b in zip(previous,current))
        passed = max(prediction_change, score_change) < .001
        audits.append(dict(points=points, max_prediction_change=prediction_change,
                           max_individual_nll_change=score_change, passed=passed))
        previous = current
        if passed:
            break
    return previous, dict(evaluation_points=points, steps=audits, passed=bool(audits and audits[-1]['passed']))


def final(size, curve=None):
    manifest_path = ROOT/size/curve/'selection_lock.json' if curve is not None else ROOT/'selection_lock.json'
    manifest = json.loads(manifest_path.read_text())
    assert digest(ROOT/'protocol.json') == manifest['protocol_sha256']
    for key, expected in manifest['selections'].items():
        assert digest(ROOT/key/'selection.json') == expected
    df = frame(size, test=True)
    fit = np.flatnonzero(df.split != 'test')
    test = np.flatnonzero(df.split == 'test')
    assert (len(fit), len(test)) == (400, 100)
    y = torch.tensor(likelihood_values(df, FORECASTING_SPECS), dtype=torch.float64)
    activation_raw = activation_arrays(size, df)
    raw_x = covariates(df, fit)
    for curve in ([curve] if curve is not None else CURVES):
        cell = ROOT / size / curve
        selected = json.loads((cell/'selection.json').read_text())
        out = cell/'final'
        out.mkdir(exist_ok=True)
        base_path = out/'behavior.pt'
        baseline_selection = selected['behavior']
        # Rebuild the same integration grid and prior knot coordinates as selected.
        template = behavior(curve, baseline_selection['quadrature'])
        template.load_state_dict(torch.load(cell/'baseline'/'selected_model.pt', weights_only=True))
        fresh = behavior(curve)
        base = JointBeliefModel(fresh, 0).with_quadrature(baseline_selection['quadrature'])
        base.behavior.prior_knot_logits.copy_(template.prior_knot_logits)
        if base_path.exists():
            base.load_state_dict(torch.load(base_path, weights_only=True))
        else:
            start = time.perf_counter()
            fixed_fit(base, y[fit], torch.empty((len(fit), 0),dtype=torch.float64),
                      baseline_selection['best_epoch_count'], 0., probe=False)
            torch.save(base.state_dict(), base_path)
            save_json(out/'behavior_fit.json', dict(seconds=time.perf_counter()-start,
                                                  epochs=baseline_selection['best_epoch_count']))
        for method in METHODS + ('activation_only',):
            if (out/f'{method}_complete.json').exists():
                continue
            print(f'FINAL {size} {curve} {method}', flush=True)
            choice = selected['activation' if method == 'activation_only' else method]
            if method == 'behavior':
                model = base
                x = torch.empty((len(df),0),dtype=torch.float64)
            else:
                if method == 'raw_profile':
                    x = raw_x
                else:
                    x, normalization = standardize(activation_raw[choice['layer']], fit)
                    np.savez(out/f'{method}_normalization.npz', **normalization)
                    if method == 'shuffled':
                        x = shuffled(x, [fit, test])
                model = JointBeliefModel(copy.deepcopy(base.behavior), x.shape[1]).with_quadrature(choice['quadrature'])
                model_path = out/f'{"activation" if method == "activation_only" else method}.pt'
                start = time.perf_counter()
                if model_path.exists():
                    model.load_state_dict(torch.load(model_path, weights_only=True))
                else:
                    fixed_fit(model, y[fit], x[fit], choice['best_epoch'], choice['penalty'])
                    torch.save(model.state_dict(), model_path)
                    save_json(out/f'{method}_fit.json', dict(seconds=time.perf_counter()-start,
                                                           epochs=choice['best_epoch']))
            # Audits here only refine numerical integration, never fitted parameters.
            outputs, audit = evaluate_stable(model, y[test], x[test], internal_only=method == 'activation_only')
            rows = []
            for record in outputs:
                target = record['target']
                spec = FORECASTING_SPECS[target]
                for i, case in enumerate(df.iloc[test].source_prompt_id):
                    row = dict(size=size, curve=curve, method=method, case_id=case,
                               task=record['task'], target=spec.name, kind=spec.kind,
                               actual=float(y[test[i],target]), prediction=float(record['prediction'][i]),
                               nll=float(record['nll'][i]), train_mean=float(y[fit,target].mean()))
                    if 'category_probabilities' in record:
                        row.update({f'p_category_{j+1}': float(v) for j,v in enumerate(record['category_probabilities'][i])})
                    rows.append(row)
            pd.DataFrame(rows).to_csv(out/f'{method}_predictions.csv', index=False)
            with torch.no_grad():
                altered = y[test].clone()
                altered[:, 1:] = float('nan')
                leakage = float((model.conditional_means(y[test], x[test], [0])-
                                 model.conditional_means(altered, x[test], [0])).abs().max())
            save_json(out/f'{method}_complete.json', dict(quadrature=audit,
                                                         training_numerically_stable=choice['training_numerically_stable'],
                                                         withheld_target_error=leakage))
        print(f'FINAL COMPLETE {size} {curve}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['initialize', 'baseline', 'search', 'lock', 'final'])
    parser.add_argument('--size', choices=SIZES, default='4b')
    parser.add_argument('--curve', choices=CURVES, help='Search, lock, or evaluate one cell independently')
    args = parser.parse_args()
    if args.curve and args.stage not in ['search','lock','final']:
        parser.error('--curve is supported only for search/lock/final')
    torch.set_num_threads(PROTOCOL['threads'])
    torch.manual_seed(PROTOCOL['seed'])
    initialize()
    if args.stage == 'initialize':
        pass
    elif args.stage == 'baseline':
        df = frame(args.size)
        y = torch.tensor(likelihood_values(df, FORECASTING_SPECS), dtype=torch.float64)
        train, val = np.flatnonzero(df.split == 'train'), np.flatnonzero(df.split == 'val')
        for curve in CURVES:
            get_baseline(ROOT/args.size/curve, curve, y, train, val)
    elif args.stage == 'search':
        search(args.size, args.curve)
    elif args.stage == 'lock':
        lock(args.size, args.curve) if args.curve else lock()
    else:
        final(args.size, args.curve)


if __name__ == '__main__':
    main()
