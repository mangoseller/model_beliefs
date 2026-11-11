"""Benchmark ONE joint candidate on CKD train/validation data; never score test.

Example (from repository root):
  PYTHONPATH=. /Users/sam/miniforge3/envs/latent-beliefs/bin/python \
    experiments/joint_beliefs/benchmark.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from experiments.joint_beliefs.model import JointBeliefModel
from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values
from latent_beliefs.latent.model import FitConfig, ProbabilisticLatentModel, fit_model


def fit_candidate(model, train, x_train, val, x_val, args, out):
    optimizer = torch.optim.Adam([
        {'params': model.behavior.parameters(), 'lr': args.learning_rate},
        {'params': [model.w], 'lr': args.probe_learning_rate},
    ])
    with torch.no_grad():
        initial_val = float(-model.marginal_log_prob(val, x_val).mean())
    best_val, best_epoch, stale = initial_val, 0, 0
    best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
    epoch_seconds = []
    start = time.perf_counter()
    with (out / 'candidate_history.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=['epoch', 'train_nll_before_step',
                                'penalized_objective', 'validation_nll', 'gradient_norm',
                                'epoch_seconds', 'elapsed_seconds'])
        writer.writeheader()
        for epoch in range(1, args.epochs + 1):
            epoch_start = time.perf_counter()
            model.train()
            optimizer.zero_grad(set_to_none=True)
            nll = -model.marginal_log_prob(train, x_train).mean()
            objective = nll + args.penalty * model.w.square().sum()
            if not torch.isfinite(objective):
                raise RuntimeError(f'Nonfinite objective at epoch {epoch}')
            objective.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 10., error_if_nonfinite=True)
            optimizer.step()
            model.eval()
            with torch.no_grad():
                val_nll = float(-model.marginal_log_prob(val, x_val).mean())
            if not np.isfinite(val_nll):
                raise RuntimeError(f'Nonfinite validation NLL at epoch {epoch}')
            if val_nll < best_val - args.min_delta:
                best_val, best_epoch, stale = val_nll, epoch, 0
                best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            else:
                stale += 1
            duration = time.perf_counter() - epoch_start
            epoch_seconds.append(duration)
            writer.writerow(dict(epoch=epoch, train_nll_before_step=float(nll.detach()),
                                 penalized_objective=float(objective.detach()),
                                 validation_nll=val_nll, gradient_norm=float(grad_norm),
                                 epoch_seconds=duration, elapsed_seconds=time.perf_counter() - start))
            if epoch == 1 or epoch % 100 == 0:
                handle.flush()
                print(f'[joint] epoch={epoch} val_nll={val_nll:.6f} best={best_val:.6f} '
                      f'elapsed={time.perf_counter()-start:.1f}s epoch={duration:.4f}s', flush=True)
            if stale >= args.patience:
                break
    seconds = time.perf_counter() - start
    model.load_state_dict(best_state)
    return dict(seconds=seconds, epochs_ran=epoch, best_epoch=best_epoch,
                stop_reason='validation_patience' if stale >= args.patience else 'epoch_cap',
                initial_validation_nll=initial_val, best_validation_nll=best_val,
                mean_epoch_seconds=float(np.mean(epoch_seconds)),
                median_epoch_seconds=float(np.median(epoch_seconds)),
                p90_epoch_seconds=float(np.quantile(epoch_seconds, .9)),
                seconds_per_1000_epochs=seconds / epoch * 1000,
                restored_initial_baseline=best_epoch == 0)


def tasks():
    binary = [i for i, s in enumerate(CKD_SPECS) if s.kind == 'binary']
    yield 'probability_to_decisions', [0], binary
    yield 'decisions_to_probability', binary, [0]
    for j, spec in enumerate(CKD_SPECS):
        yield 'withhold_' + spec.name, [k for k in range(len(CKD_SPECS)) if k != j], [j]


def validation_diagnostics(model, y, x, out):
    """Validation-only reciprocal/LOO scores and fixed-parameter quadrature audit."""
    records = []
    with torch.no_grad():
        for task, observed, targets in tasks():
            prediction = model.conditional_means(y, x, observed)
            log_prob = model.predictive_log_prob(y, x, observed, targets)
            for col, target in enumerate(targets):
                finite = torch.isfinite(y[:, target])
                actual = y[finite, target]
                pred = prediction[finite, target]
                records.append(dict(task=task, output=CKD_SPECS[target].name,
                                    kind=CKD_SPECS[target].kind,
                                    mean_nll=float(-log_prob[finite, col].mean()),
                                    mse=float((actual - pred).square().mean())))
        pd.DataFrame(records).to_csv(out / 'validation_predictions_summary.csv', index=False)
        coarse_nll = -model.marginal_log_prob(y, x).mean()
        fine = model.with_quadrature(2 * len(model.behavior.z_grid))
        finer_nll = -fine.marginal_log_prob(y, x).mean()
        max_by_kind = {kind: 0. for kind in ('probability', 'binary', 'ordinal')}
        for _, observed, targets in tasks():
            changes = (model.conditional_means(y, x, observed)
                       - fine.conditional_means(y, x, observed)).abs()
            for target in targets:
                kind = CKD_SPECS[target].kind
                max_by_kind[kind] = max(max_by_kind[kind], float(changes[:, target].max()))
    return dict(coarse_points=len(model.behavior.z_grid), fine_points=len(fine.behavior.z_grid),
                coarse_validation_nll=float(coarse_nll), fine_validation_nll=float(finer_nll),
                nll_absolute_change=float((coarse_nll - finer_nll).abs()),
                max_prediction_change_by_kind=max_by_kind,
                passed=max(max_by_kind.values()) < 1e-3 and float((coarse_nll - finer_nll).abs()) < 1e-3,
                note='Ordinal changes are in category units; other means are probabilities.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--larger', action='store_true')
    parser.add_argument('--layer', type=int, default=25)
    parser.add_argument('--penalty', type=float, default=1.)
    parser.add_argument('--epochs', type=int, default=12000)
    parser.add_argument('--patience', type=int, default=250)
    parser.add_argument('--min-delta', type=float, default=1e-5)
    parser.add_argument('--learning-rate', type=float, default=.02)
    parser.add_argument('--probe-learning-rate', type=float, default=.001)
    parser.add_argument('--quadrature', type=int, default=128)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--response-curve', choices=['linear', 'monotone_spline'], default='linear')
    parser.add_argument('--output', type=Path, default=Path('build/joint_beliefs/benchmark_4b_layer25_l2_1'))
    args = parser.parse_args()
    if min(args.epochs, args.patience, args.threads, args.layer) < 1 or args.penalty < 0:
        parser.error('Epochs, patience, threads and layer must be positive; penalty nonnegative')
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise FileExistsError(f'Refusing to overwrite benchmark artifacts: {out}')
    total_start = time.perf_counter()
    torch.set_num_threads(args.threads)
    torch.manual_seed(20261108)
    np.random.seed(20261108)
    root = Path('build/activation_pilot_12b' if args.larger else 'build/activation_pilot')
    model_key = 'gemma3_12b' if args.larger else 'gemma3_4b'
    df = pd.read_csv('build/datasets/ckd.csv')
    split = df.loc[df.model_key == 'gemma3_4b'].set_index('source_prompt_id')['split']
    df = df.loc[df.model_key == model_key].copy()
    df['split'] = df.source_prompt_id.map(split)
    # Remove test rows before constructing likelihood arrays or loading caches.
    df = df.loc[df.split.isin(['train', 'val'])].reset_index(drop=True)
    train_ix = np.flatnonzero(df.split == 'train')
    val_ix = np.flatnonzero(df.split == 'val')
    if len(train_ix) != 313 or len(val_ix) != 84:
        raise ValueError('Unexpected aligned train/validation case counts')
    arrays = []
    for case in df.source_prompt_id:
        with np.load(root / 'activations' / f'{case}.npz') as cached:
            arrays.append(cached['h'][args.layer - 1].astype(np.float64))
    raw = np.stack(arrays)
    if not np.isfinite(raw).all():
        raise ValueError('Nonfinite activation cache')
    mean = raw[train_ix].mean(0)
    scale = float(np.sqrt(np.mean((raw[train_ix] - mean) ** 2)))
    x = torch.tensor((raw - mean) / max(scale, 1e-8), dtype=torch.float64)
    values = likelihood_values(df, CKD_SPECS)
    y = torch.tensor(values, dtype=torch.float64)
    np.savez(out / 'normalization.npz', mean=mean, scale=scale)
    df[['source_prompt_id', 'split']].to_csv(out / 'case_splits.csv', index=False)
    config = {**vars(args), 'output': str(out), 'model_key': model_key,
              'n_train': len(train_ix), 'n_validation': len(val_ix), 'n_features': raw.shape[1],
              'n_outputs': len(CKD_SPECS), 'dtype': 'float64', 'device': 'cpu',
              'torch_version': torch.__version__, 'numpy_version': np.__version__,
              'platform': platform.platform(), 'seed': 20261108,
              'data_sha256': hashlib.sha256(values.tobytes() + raw.tobytes()).hexdigest(),
              'source_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                                for name in ['experiments/joint_beliefs/model.py',
                                             'experiments/joint_beliefs/benchmark.py']},
              'selection_criterion': 'unpenalized validation joint NLL across all sixteen outputs',
              'test_evaluated': False}
    (out / 'config.json').write_text(json.dumps(config, indent=2))
    b = ProbabilisticLatentModel(CKD_SPECS, quadrature_points=args.quadrature,
                                response_curve=args.response_curve)
    print('Preparing a full sixteen-output behavioral baseline (reusable across candidates).', flush=True)
    baseline_start = time.perf_counter()
    baseline_info = fit_model(b, values[train_ix], values[val_ix],
                              FitConfig(epochs=args.epochs, patience=args.patience,
                                        min_delta=args.min_delta, learning_rate=args.learning_rate,
                                        progress_every=100), progress_label='baseline')
    baseline_seconds = time.perf_counter() - baseline_start
    baseline_info['seconds'] = baseline_seconds
    baseline_info['stop_reason'] = ('epoch_cap' if baseline_info['epochs_ran'] == args.epochs
                                    else 'validation_patience')
    torch.save(b.state_dict(), out / 'baseline.pt')
    (out / 'baseline.json').write_text(json.dumps(baseline_info, indent=2))
    print(f'Baseline preparation: {baseline_seconds:.2f}s. Starting one joint candidate.', flush=True)
    model = JointBeliefModel(b, x.shape[1])
    with torch.no_grad():
        identity = float((model.log_likelihood_by_output(y[val_ix], x[val_ix])
                          - b.log_likelihood_by_output(y[val_ix])).abs().max())
    if identity > 1e-8:
        raise AssertionError(f'Zero-shift identity failed: {identity}')
    candidate = fit_candidate(model, y[train_ix], x[train_ix], y[val_ix], x[val_ix], args, out)
    torch.save(model.state_dict(), out / 'candidate.pt')
    # Persist measured runtime BEFORE potentially slower diagnostic evaluations.
    (out / 'candidate_timing.json').write_text(json.dumps(candidate, indent=2))
    diagnostics_start = time.perf_counter()
    quadrature = validation_diagnostics(model, y[val_ix], x[val_ix], out)
    with torch.no_grad():
        altered = y[val_ix].clone()
        altered[:, 1:] = float('nan')
        leakage = float((model.conditional_means(y[val_ix], x[val_ix], [0])
                         - model.conditional_means(altered, x[val_ix], [0])).abs().max())
    result = dict(config=config, baseline=baseline_info, candidate=candidate,
                  checks=dict(zero_shift_max_log_likelihood_error=identity,
                              withheld_target_max_prediction_error=leakage, quadrature=quadrature),
                  diagnostics_seconds=time.perf_counter() - diagnostics_start,
                  total_seconds=time.perf_counter() - total_start)
    (out / 'benchmark.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
