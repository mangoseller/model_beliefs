"""Resumable, chunked forecasting activation extraction without truncation."""
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import time
from pathlib import Path
import mlx.core as mx
import numpy as np
from mlx_lm import load
from mlx_lm.models.base import create_attention_mask
from mlx_lm.models.cache import make_prompt_cache
from experiments.forecast_beliefs.data import prompts, PROMPTS

ROOT = Path('build/forecast_beliefs')

def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)

def forward(model, ids, chunk=256, capture=True):
    core = model.language_model.model
    cache = make_prompt_cache(model.language_model)
    for start in range(0,len(ids),chunk):
        tokens = mx.array([ids[start:start+chunk]])
        if not capture:
            h = core(tokens, cache=cache)
            mx.eval(h, [c.state for c in cache])
            continue
        h = core.embed_tokens(tokens)
        h *= mx.array(core.args.hidden_size**.5, mx.bfloat16).astype(h.dtype)
        global_mask = create_attention_mask(h, cache[core.sliding_window_pattern-1])
        local_mask = create_attention_mask(h, cache[0], window_size=core.window_size)
        vectors = []
        for j,(layer,c) in enumerate(zip(core.layers,cache)):
            mask = global_mask if j%core.sliding_window_pattern == core.sliding_window_pattern-1 else local_mask
            h = layer(h,mask,c)
            mx.eval(h,c.state)
            if start+chunk >= len(ids):
                vectors.append(np.array(h[0,-1].astype(mx.float32),copy=True))
    norm = core.norm(h) if capture else h
    result = np.array(norm[0,-1].astype(mx.float32),copy=True)
    return (np.stack(vectors),result) if capture else result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--size',choices=['4b','12b'],required=True)
    parser.add_argument('--limit',type=int)
    args = parser.parse_args()
    out = ROOT/args.size/'extraction'
    out.mkdir(parents=True,exist_ok=True)
    lock = (out/'.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    folder = Path('build/activation_pilot'+('_12b' if args.size=='12b' else ''))
    metadata = json.loads((folder/'extraction.json').read_text())
    hashes = {}
    for p in [Path(__file__),Path('experiments/forecast_beliefs/data.py'),PROMPTS]+[folder/'model'/n for n in ['config.json','tokenizer.json','chat_template.json']]:
        if p.exists(): hashes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    provenance = dict(hashes=hashes,revision=metadata['revision'],chunk=256,
                      packages={p:importlib.metadata.version(p) for p in ['mlx','mlx-lm','transformers','numpy']})
    fingerprint = hashlib.sha256(json.dumps(provenance,sort_keys=True).encode()).hexdigest()
    if (out/'provenance.json').exists():
        assert json.loads((out/'provenance.json').read_text()) == provenance, 'Extraction source/input changed'
    else: save(out/'provenance.json',provenance)
    model,tokenizer = load(str(folder/'model'))
    core = model.language_model.model
    rows = list(prompts().items())
    tokenized = [(case,prompt,tokenizer.apply_chat_template([{'role':'user','content':prompt}],tokenize=True,add_generation_prompt=True)) for case,prompt in rows]
    lengths = [len(ids) for _,_,ids in tokenized]
    save(out/'token_lengths.json',dict(zip([c for c,_,_ in tokenized],lengths)))
    print(f'{args.size}: tokens min={min(lengths)} median={np.median(lengths)} max={max(lengths)}; no truncation',flush=True)
    cache = out/'activations'
    cache.mkdir(exist_ok=True)
    start = time.monotonic()
    checked = False
    count = 0
    for i,(case,prompt,ids) in enumerate(tokenized):
        target = cache/f'{case}.npz'
        if target.exists():
            with np.load(target) as a:
                assert str(a['fingerprint']) == fingerprint and str(a['prompt']) == prompt
                assert np.array_equal(a['token_ids'],ids)
                assert a['h'].shape == (len(core.layers),core.args.hidden_size) and np.isfinite(a['h']).all()
            continue
        stacked,norm = forward(model,ids)
        if not checked:
            reference = forward(model,ids,capture=False)
            error = float(np.max(np.abs(reference-norm)))
            assert error < 1e-4, f'Forward mismatch: {error}'
            save(out/'forward_check.json',dict(case=case,error=error,tokens=len(ids),chunk=256))
            print(f'{args.size} forward identity error={error}',flush=True)
            checked = True
        assert stacked.dtype == np.float32 and np.isfinite(stacked).all()
        tmp = target.with_suffix('.tmp')
        with tmp.open('wb') as f:
            np.savez_compressed(f,h=stacked,prompt=prompt,token_ids=np.array(ids),case_id=case,fingerprint=fingerprint)
        tmp.replace(target)
        count += 1
        progress = dict(size=args.size,cached=i+1,total=len(rows),new_cases=count,seconds=time.monotonic()-start,last_case=case)
        save(out/'progress.json',progress)
        if count==1 or count%10==0: print(progress,flush=True)
        mx.clear_cache()
        if args.limit and count>=args.limit: break
    complete = len(list(cache.glob('*.npz')))==500
    save(out/'extraction.json',dict(metadata,domain='forecasting',cases=len(list(cache.glob('*.npz'))),complete=complete,chunk=256,fingerprint=fingerprint,seconds_this_run=time.monotonic()-start))

if __name__=='__main__': main()
