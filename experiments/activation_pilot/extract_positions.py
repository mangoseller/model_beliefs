"""Resumable five-position extraction; run --both to queue 4B then 12B."""
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from transformers import AutoTokenizer

from experiments.activation_pilot.positions import (
    POSITION_NAMES, PROMPTS, SCHEMA_VERSION, locate, prompt_for, sha256, valid_cache,
)


def atomic_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--larger', action='store_true')
    group.add_argument('--both', action='store_true')
    parser.add_argument('--validate-only', action='store_true', help='Check every token span without loading weights')
    parser.add_argument('--limit', type=int, help='Extract only the first N cases for a smoke check')
    parser.add_argument('--output-root', type=Path, default=Path('build/activation_positions'))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if args.both:
        forwarded = [argument for argument in sys.argv[1:] if argument != '--both']
        for extra in [[], ['--larger']]:
            subprocess.run([sys.executable, '-u', __file__, *forwarded, *extra], check=True)
        return
    size = '12b' if args.larger else '4b'
    root = Path('build/activation_pilot_12b' if args.larger else 'build/activation_pilot')
    out = args.output_root / size
    tokenizer = AutoTokenizer.from_pretrained(root / 'model', local_files_only=True)
    rows = [json.loads(line) for line in PROMPTS.read_text().splitlines()]
    if len({row['source_prompt_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate case IDs')
    prepared = [(row, *locate(tokenizer, prompt_for(row))) for row in rows]
    print(f'{size}: verified five token rules for all {len(rows)} cases', flush=True)
    if args.validate_only:
        row, ids, spans = prepared[0]
        print(json.dumps({name: {'indices': span, 'tokens': tokenizer.convert_ids_to_tokens(ids[span].tolist())}
                          for name, span in zip(POSITION_NAMES, spans)}, indent=2), flush=True)
        return
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / 'extraction.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError(f'An extraction is already running in {out}') from None
    old_meta = json.loads((root / 'extraction.json').read_text())
    config = json.loads((root / 'model/config.json').read_text())['text_config']
    shape = (config['num_hidden_layers'], len(POSITION_NAMES), config['hidden_size'])
    provenance = dict(schema_version=SCHEMA_VERSION, model=old_meta['model'], revision=old_meta['revision'],
                      position_names=POSITION_NAMES, shape_per_case=shape, prompt_sha256=sha256(PROMPTS),
                      tokenizer_sha256=sha256(root / 'model/tokenizer.json'),
                      chat_template_sha256=sha256(root / 'model/chat_template.json'),
                      model_config_sha256=sha256(root / 'model/config.json'),
                      code_sha256={name: sha256(Path(__file__).with_name(name))
                                   for name in ['positions.py', 'extract_positions.py']},
                      location='post transformer block, before final RMSNorm',
                      pooling='float32 arithmetic mean of all proposition tokens, including period',
                      axes=['layer (1-based in reports)', 'position', 'hidden coordinate'],
                      quantization=old_meta['quantization'],
                      packages={p: importlib.metadata.version(p) for p in ['mlx', 'mlx-lm', 'transformers', 'numpy']})
    fingerprint = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    manifest = dict(**provenance, fingerprint=fingerprint, total_cases=len(rows))
    metadata_path = out / 'extraction.json'
    if metadata_path.exists() and json.loads(metadata_path.read_text())['fingerprint'] != fingerprint:
        raise RuntimeError('Extraction provenance changed; use a new --output-root')
    atomic_json(metadata_path, manifest)
    cache = out / 'activations'
    cache.mkdir(exist_ok=True)
    started = time.monotonic()
    status = dict(model=size, pid=os.getpid(), status='starting', total_cases=len(rows),
                  requested_cases=len(prepared[:args.limit]), processed=0, extracted=0, reused=0,
                  fingerprint=fingerprint, max_original_cache_error=0.0)

    def progress():
        status.update(elapsed_seconds=time.monotonic() - started, updated_unix=time.time())
        atomic_json(out / 'progress.json', status)

    progress()
    try:
        # Delay MLX initialization until after all token alignments have passed.
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.base import create_attention_mask
        model, mlx_tokenizer = load(str(root / 'model'))
        core = model.language_model.model
        status['status'] = 'running'
        progress()
        checked_forward = False
        for row, ids, spans in prepared[:args.limit]:
            case = row['source_prompt_id']
            target = cache / f'{case}.npz'
            if target.exists():
                if not valid_cache(target, ids, spans, shape, fingerprint):
                    raise RuntimeError(f'Invalid existing cache: {target}; inspect before replacing')
                status['reused'] += 1
            else:
                prompt = prompt_for(row)
                mlx_ids = mlx_tokenizer.apply_chat_template(
                    [{'role': 'user', 'content': prompt}], tokenize=True, add_generation_prompt=True)
                if not np.array_equal(ids, mlx_ids):
                    raise ValueError(f'MLX and offset tokenizer disagree for {case}')
                h = core.embed_tokens(mx.array([mlx_ids]))
                h *= mx.array(core.args.hidden_size ** 0.5, mx.bfloat16).astype(h.dtype)
                global_mask = create_attention_mask(h, None)
                local_mask = create_attention_mask(h, None, window_size=core.window_size)
                vectors = []
                for index, layer in enumerate(core.layers):
                    global_layer = index % core.sliding_window_pattern == core.sliding_window_pattern - 1
                    h = layer(h, global_mask if global_layer else local_mask, None)
                    mx.eval(h)
                    singles = [h[0, span[0]].astype(mx.float32) for span in spans[:4]]
                    pooled = h[0, spans[4][0]:spans[4][-1] + 1].astype(mx.float32).mean(axis=0)
                    vectors.append(np.array(mx.stack([*singles, pooled]), copy=True))
                if not checked_forward:
                    reference = core(mx.array([mlx_ids]))
                    error = float(mx.max(mx.abs(reference[0, -1].astype(mx.float32)
                                               - core.norm(h)[0, -1].astype(mx.float32))))
                    if error >= 1e-4:
                        raise ValueError(f'Manual forward identity failed: {error}')
                    status['forward_identity_max_error'] = error
                    checked_forward = True
                stacked = np.stack(vectors).astype(np.float32)
                if stacked.shape != shape or not np.isfinite(stacked).all():
                    raise ValueError(f'Nonfinite or incorrectly shaped activations for {case}')
                # Compare ALL layers of the current-position control with the earlier run.
                with np.load(root / 'activations' / f'{case}.npz', allow_pickle=False) as old:
                    if not np.array_equal(old['token_ids'], ids) or old['prompt'].item() != prompt:
                        raise ValueError(f'Prompt or token mismatch against original cache: {case}')
                    old_error = float(np.max(np.abs(stacked[:, 3] - old['h'])))
                if old_error >= 1e-4:
                    raise ValueError(f'Original final-token cache mismatch for {case}: {old_error}')
                status['max_original_cache_error'] = max(status['max_original_cache_error'], old_error)
                temporary = target.with_suffix('.npz.tmp')
                with temporary.open('wb') as handle:
                    np.savez_compressed(handle, h=stacked, prompt=prompt, token_ids=ids, case_id=case,
                                        position_names=np.array(POSITION_NAMES),
                                        position_token_indices=json.dumps(spans), fingerprint=fingerprint,
                                        original_cache_max_error=old_error)
                temporary.replace(target)
                status['extracted'] += 1
            status['processed'] += 1
            status['last_case'] = case
            progress()
            if status['processed'] == 1 or status['processed'] % 20 == 0:
                print(json.dumps(status), flush=True)
        status['status'] = 'complete' if status['processed'] == len(rows) else 'partial'
        progress()
        print(json.dumps(status), flush=True)
    except BaseException as error:
        status.update(status='failed', error=f'{type(error).__name__}: {error}')
        progress()
        raise


if __name__ == '__main__':
    main()
