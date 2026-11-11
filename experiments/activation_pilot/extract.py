"""Cache decision-independent post-block residual vectors from local Gemma 3.

Run with PYTHONPATH=build/pilot_dependencies:. and the latent-beliefs Python.
"""
import json
import importlib.metadata
import argparse
import time
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_lm import load
from mlx_lm.models.base import create_attention_mask

OUT = Path('build/activation_pilot')
REVISION = '93724907d4ed1745d2fe50baadf3b0b01a65abf2'


def main():
    global OUT, REVISION
    parser = argparse.ArgumentParser()
    parser.add_argument('--larger', action='store_true')
    args = parser.parse_args()
    model_id = 'mlx-community/gemma-3-4b-it-4bit'
    if args.larger:
        OUT = Path('build/activation_pilot_12b')
        REVISION = '86cc6a8dedbc456dd0e4af01a9d09f396f77e558'
        model_id = 'mlx-community/gemma-3-12b-it-4bit'
    model, tokenizer = load(str(OUT / 'model'))
    core = model.language_model.model
    rows = [json.loads(line) for line in Path(
        'data/release/prompts/ckd/ckd_same1000_threshold_05pct_revised.jsonl'
    ).read_text().splitlines()]
    cache = OUT / 'activations'
    cache.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    for i, row in enumerate(rows):
        case = row['source_prompt_id']
        target = cache / f'{case}.npz'
        if target.exists():
            stored = np.load(target)['h']
            if stored.dtype == np.float32 and np.isfinite(stored).all():
                continue
        prompt = ('Consider whether the following patient has chronic kidney disease (CKD).'
                  '\n\nPatient profile:\n' + row['patient_profile'] +
                  '\n\nThe proposition of interest is: this patient has CKD.')
        ids = tokenizer.apply_chat_template(
            [{'role': 'user', 'content': prompt}], tokenize=True, add_generation_prompt=True)
        h = core.embed_tokens(mx.array([ids]))
        h *= mx.array(core.args.hidden_size ** 0.5, mx.bfloat16).astype(h.dtype)
        global_mask = create_attention_mask(h, None)
        local_mask = create_attention_mask(h, None, window_size=core.window_size)
        vectors = []
        for layer_index, layer in enumerate(core.layers):
            global_layer = layer_index % core.sliding_window_pattern == core.sliding_window_pattern - 1
            h = layer(h, global_mask if global_layer else local_mask, None)
            mx.eval(h)
            vectors.append(np.array(h[0, -1].astype(mx.float32), copy=True))
        if i == 0:
            reference = core(mx.array([ids]))
            error = mx.max(mx.abs(reference[0,-1].astype(mx.float32) - core.norm(h)[0,-1].astype(mx.float32)))
            mx.eval(error)
            assert float(error) < 1e-4, f'Manual extraction disagrees with model forward: {error}'
            print(f'Extraction forward identity error: {float(error)}',flush=True)
        stacked = np.stack(vectors).astype(np.float32)
        assert np.isfinite(stacked).all()
        np.savez_compressed(target, h=stacked,
                            prompt=prompt, token_ids=np.array(ids), case_id=case)
        if i == 0 or (i + 1) % 20 == 0:
            print(f'cached {i+1}/{len(rows)}; elapsed={time.monotonic()-start:.1f}s', flush=True)
    metadata = dict(model=model_id, revision=REVISION,
                    layers=len(core.layers), cases=len(rows),
                    token='last token of chat generation prefix',
                    location='post transformer block, before final RMSNorm',
                    storage_dtype='float32 (some residual values exceed float16 range)',
                    quantization='4 bit; local checkpoint differs from hosted paper inference',
                    packages={p:importlib.metadata.version(p) for p in ['mlx','mlx-lm','transformers','numpy']},
                    seconds=time.monotonic()-start)
    (OUT / 'extraction.json').write_text(json.dumps(metadata, indent=2))
    print(metadata, flush=True)


if __name__ == '__main__':
    main()
