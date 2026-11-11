"""Development-only position/layer/L2 search with the frozen flexible pilot.

No test activations or test responses are used. This is the original single-split
pilot diagnostic, not nested cross-validation or a fresh confirmatory evaluation.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

import experiments.activation_pilot.fit as base
from experiments.activation_pilot.positions import POSITION_NAMES, sha256


def load_features(root, case_ids, positions, layers):
    metadata = json.loads((root / 'extraction.json').read_text())
    indices = [metadata['position_names'].index(name) for name in positions]
    shape = tuple(metadata['shape_per_case'])
    result = []
    for case in case_ids:
        with np.load(root / 'activations' / f'{case}.npz', allow_pickle=False) as saved:
            h = saved['h']
            if (h.shape != shape or h.dtype != np.float32 or not np.isfinite(h).all()
                    or saved['fingerprint'].item() != metadata['fingerprint']
                    or saved['position_names'].tolist() != metadata['position_names']
                    or saved['case_id'].item() != case):
                raise ValueError(f'Invalid activation cache for {case}')
            result.append(h[np.asarray(layers) - 1][:, indices])
    return np.stack(result)


def fit_candidate(model, raw, y, train, val, penalty):
    x, mean, scale = base.features(raw, train)
    w = base.fit_probe(model, x, y, train, penalty)
    with torch.no_grad():
        prediction = base.predict(model, y[val], x[val] @ w)
        score = float(base.loss(prediction, y[val]))
    if not np.isfinite(score) or not torch.isfinite(w).all():
        raise ValueError('Nonfinite probe fit')
    return score, prediction.numpy(), dict(w=w.numpy(), mean=mean, scale=scale)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--larger', action='store_true')
    parser.add_argument('--cache-root', type=Path, default=Path('build/activation_positions'))
    parser.add_argument('--output', type=Path, help='New directory; defaults to CACHE_ROOT/SIZE/search')
    parser.add_argument('--positions', nargs='+', choices=POSITION_NAMES, default=list(POSITION_NAMES))
    parser.add_argument('--layers', nargs='+', type=int, help='1-based; default eight evenly spaced layers')
    parser.add_argument('--penalties', nargs='+', type=float, default=[.001, .01, .1, 1.])
    args = parser.parse_args()
    size = '12b' if args.larger else '4b'
    root = args.cache_root / size
    out = args.output or root / 'search'
    metadata = json.loads((root / 'extraction.json').read_text())
    n_layers = metadata['shape_per_case'][0]
    layers = args.layers or (np.unique(np.linspace(0, n_layers - 1, 8).round().astype(int)) + 1).tolist()
    if any(layer < 1 or layer > n_layers for layer in layers):
        parser.error(f'Layers must be in 1..{n_layers}')
    if any(penalty < 0 or not np.isfinite(penalty) for penalty in args.penalties):
        parser.error('Penalties must be finite and nonnegative')
    if len(set(args.positions)) != len(args.positions) or len(set(layers)) != len(layers):
        parser.error('Duplicate positions or layers')
    if out.exists():
        parser.error(f'Output already exists; choose a new --output: {out}')
    torch.set_num_threads(2)
    torch.manual_seed(base.SEED)
    base.FLEXIBLE = True
    base.MODEL_KEY = f'gemma3_{size}'
    # Remove test rows before constructing responses or loading any cache files.
    df = base.data().loc[lambda frame: frame.split.isin(['train', 'val'])].reset_index(drop=True)
    train = np.flatnonzero(df.split == 'train')
    val = np.flatnonzero(df.split == 'val')
    if len(train) != 313 or len(val) != 84:
        raise ValueError('Unexpected development split')
    y = torch.tensor(df[base.NAMES].to_numpy(float), dtype=torch.float64)
    original = Path('build/activation_pilot_12b' if args.larger else 'build/activation_pilot') / 'flexible'
    model = base.new_model()
    model.load_state_dict(torch.load(original / 'baseline_selection.pt', weights_only=True))
    base.freeze(model)
    h = load_features(root, df.source_prompt_id, args.positions, layers)
    out.mkdir(parents=True)
    df[['source_prompt_id', 'split']].to_csv(out / 'case_splits.csv', index=False)
    config = dict(model_key=base.MODEL_KEY, positions=args.positions, layers=layers,
                  penalties=args.penalties, seed=base.SEED, n_train=len(train), n_val=len(val),
                  extraction_fingerprint=metadata['fingerprint'],
                  baseline_path=str(original / 'baseline_selection.pt'),
                  baseline_sha256=sha256(original / 'baseline_selection.pt'),
                  dataset_sha256=sha256('build/datasets/ckd.csv'),
                  source_sha256={name: sha256(Path(__file__).with_name(name))
                                 for name in ['position_search.py', 'fit.py', 'positions.py']},
                  objective='eight observed decisions predict four betting responses',
                  evaluation='development selection only; no test evaluation; not nested CV',
                  shuffled_control='one within-partition permutation, searched over the entire same grid')
    (out / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    with torch.no_grad():
        baseline = base.predict(model, y[val], torch.zeros(len(val), dtype=torch.float64)).numpy()
    rng = np.random.default_rng(base.SEED)
    permutation = np.arange(len(df))
    for partition in [train, val]:
        permutation[partition] = rng.permutation(partition)
    np.save(out / 'shuffle_indices.npy', permutation)
    rows, best, predictions = [], {}, {'behavior_only': baseline}

    def record(kind, position, layer, penalty, raw):
        score, prediction, parameters = fit_candidate(model, raw, y, train, val, penalty)
        row = dict(kind=kind, position=position, layer=layer, penalty=penalty,
                   validation_log_loss=score)
        rows.append(row)
        pd.DataFrame(rows).to_csv(out / 'search.csv', index=False)
        keys = [kind]
        if kind == 'internal':
            keys.append(f'internal_{position}')
        for key in keys:
            if key not in best or score < best[key]['validation_log_loss']:
                best[key] = row
                predictions[key] = prediction
                np.savez(out / f'{key}_probe.npz', **parameters, position=position, layer=layer,
                         penalty=penalty, extraction_fingerprint=metadata['fingerprint'])
        (out / 'selection.json').write_text(json.dumps(best, indent=2) + '\n')
        print(json.dumps(row), flush=True)

    for p, position in enumerate(args.positions):
        for l, layer in enumerate(layers):
            raw = h[:, l, p].astype(np.float64)
            for penalty in args.penalties:
                record('internal', position, layer, penalty, raw)
                record('shuffled', position, layer, penalty, raw[permutation])
    raw_profile = base.covariates(df, train).numpy()
    for penalty in args.penalties:
        record('raw_profile', 'raw_profile', 0, penalty, raw_profile)
    actual = y[val, 8:].numpy()
    results = dict(config=config, selections=best,
                   validation_metrics={name: base.metrics(actual, prediction)[0]
                                       for name, prediction in predictions.items()})
    # The selected prediction must be invariant to changes in withheld responses.
    selected = best['internal']
    raw = h[:, layers.index(selected['layer']), args.positions.index(selected['position'])]
    with np.load(out / 'internal_probe.npz', allow_pickle=False) as probe:
        x = torch.tensor((raw.astype(np.float64) - probe['mean']) / max(float(probe['scale']), 1e-8))
        shift = x[val] @ torch.tensor(probe['w'])
    altered = y[val].clone()
    altered[:, 8:] = 1 - altered[:, 8:]
    with torch.no_grad():
        error = float(np.max(np.abs(base.predict(model, altered, shift).numpy() - predictions['internal'])))
    if error != 0:
        raise ValueError(f'Withheld-target invariance failed: {error}')
    results['withheld_target_max_error'] = error
    (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    prediction_rows = []
    for name, prediction in predictions.items():
        for i, case in enumerate(df.iloc[val].source_prompt_id):
            for j, target in enumerate(base.NAMES[8:]):
                prediction_rows.append(dict(case_id=case, target=target, method=name,
                                            actual=actual[i, j], probability=prediction[i, j]))
    pd.DataFrame(prediction_rows).to_csv(out / 'validation_predictions.csv', index=False)
    print(f'Completed development-only search: {out}', flush=True)


if __name__ == '__main__':
    main()
