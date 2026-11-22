"""Incremental, paired CKD chart-order/authority experiment. See docs/order_followup.md."""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

import numpy as np
import pandas as pd

from second_order import config as C, datasets as DS, run_generation as G

CUE = ' Your chart gives the verified patient information.'
ORDERS = ('own_first', 'person_first')
PROTOCOL = 'ckd-order-authority-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def transform(prompt, order=None, neutral=False):
    """Move whole chart blocks only; preserve their headers, facts and within-chart order."""
    blocks = prompt.split('\n\n')
    if order is not None:
        if len(blocks) < 3:
            raise ValueError('expected two chart blocks followed by a question')
        own = [b for b in blocks[:2] if b.startswith('Your record for this patient:')]
        person = [b for b in blocks[:2] if 'has only the following record for this patient' in b.split('\n')[0]]
        if len(own) != 1 or len(person) != 1 or order not in ORDERS:
            raise ValueError('unrecognized chart layout')
        blocks[:2] = [own[0], person[0]] if order == 'own_first' else [person[0], own[0]]
    result = '\n\n'.join(blocks)
    if neutral:
        if result.count(CUE) != 1:
            raise ValueError('neutral condition requires exactly one verified-information cue')
        result = result.replace(CUE, '')
    return result


def make_plan(model, cases=None):
    frame = DS.rows('ckd', ['main']).copy()
    # Run 20261121-101311 used this amendment; verify every reused prompt against its cache hash.
    frame['prompt'] = frame.prompt.str.replace('Answer with a single letter.',
        'Provide your reasoning first.\nThen output your answer on a new line in exactly this format:\nChoice: <the letter of one option>', regex=False)
    if cases is not None:
        frame = frame[frame.case_id.isin(cases)]
    # Search exact original prompts within the same patient/output/replicate. Never collapse replicates.
    originals = {}
    for r in frame.to_dict('records'):
        item = G.Item(model, r['case_id'], r['condition'], r['output'], r['replicate'], r['prompt'])
        originals.setdefault((item.case_id, item.output, item.replicate, item.prompt_sha), item)
    trials, items = [], {}
    for setting, wording in [('partial', 'original'), ('false', 'verified'), ('false', 'neutral')]:
        for role in ['A', 'B', 'M', 'C', 'D']:
            base = role + ('_F' if setting == 'false' else '')
            for r in frame[frame.condition == base].to_dict('records'):
                for order in ORDERS:
                    prompt = transform(r['prompt'], order if role in 'MCD' else None, wording == 'neutral')
                    condition = f'followup_v1__{setting}__{wording}__{order if role in "MCD" else "shared"}__{role}'
                    candidate = G.Item(model, r['case_id'], condition, r['output'], r['replicate'], prompt)
                    original = originals.get((candidate.case_id, candidate.output, candidate.replicate, candidate.prompt_sha))
                    item = original or candidate
                    # Deduplicate exact new prompts too (e.g. neutral D = partial D in reversed order).
                    trials.append(dict(model=model, dataset='ckd', case_id=r['case_id'], setting=setting,
                        wording=wording, block_order=order, source_block_order=r['block_order'], role=role,
                        base_condition=base, output=r['output'], replicate=r['replicate'], sample=0,
                        check_answer=r['check_answer'], check_fact=r['check_fact'], check_relation=r['check_relation'],
                        item_key=item.key, source='baseline' if original else 'new'))
                    items[item.key] = asdict(item) | {'key': item.key, 'prompt_sha': item.prompt_sha,
                                                   'source': 'baseline' if original else 'new'}
    # Merge new identical prompts across experimental cells, while retaining every trial alias.
    canonical = {}; aliases = {}
    for key, item in items.items():
        identity = (item['case_id'], item['output'], item['replicate'], item['prompt_sha'])
        aliases[key] = canonical.setdefault(identity, key)
    for row in trials:
        row['item_key'] = aliases[row['item_key']]
    items = {key: value for key, value in items.items() if aliases[key] == key}
    return trials, items


def stream_member(source, suffix):
    """Read one member without extracting a large archive or modifying the baseline."""
    source = Path(source)
    if source.is_dir():
        with (source / suffix).open('rb') as f:
            yield f
    else:
        with tarfile.open(source, 'r|*') as tar:
            for member in tar:
                if member.isfile() and member.name.endswith('/' + suffix):
                    yield tar.extractfile(member)
                    return
        raise ValueError(f'{suffix} missing in {source}')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def configure(root):
    C.CONFIG_PATH = root / 'config.yaml'
    C.load.cache_clear()
    C.CACHE = root / 'data/generation_cache'
    G.cache_index.cache_clear()


def item_from(row):
    return G.Item(**{k: row[k] for k in G.Item.__dataclass_fields__})


def prepare(args):
    root = args.run.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Use the archived config, including immutable weight revisions and sampling settings.
    for f in stream_member(args.base_run, 'config.yaml'):
        config_bytes = f.read()
    cfg = root / 'config.yaml'
    if cfg.exists() and cfg.read_bytes() != config_bytes:
        raise ValueError('existing follow-up uses a different baseline config')
    cfg.write_bytes(config_bytes)
    configure(root)
    if not all(DS.ckd_checksums().values()):
        raise ValueError('CKD release checksums failed')
    trials, items, signatures = [], {}, {}
    for model in args.models:
        ts, its = make_plan(model)
        trials.extend(ts); items.update(its)
        signatures[model] = G.settings_signature(model, 'ckd')
    plan = dict(protocol=PROTOCOL, models=args.models, settings=signatures,
                base_run=str(args.base_run.resolve()), base_activations=str(args.base_activations.resolve()),
                trials=trials, items=items)
    plan['digest'] = digest(plan)
    path = root / 'plan.json'
    if path.exists() and json.loads(path.read_text())['digest'] != plan['digest']:
        raise ValueError('plan changed; use another follow-up directory')
    atomic_json(path, plan)
    for model in args.models:
        wanted = {k:v for k,v in items.items() if v['model']==model and v['source']=='baseline'}
        copy_baseline(root, model, wanted, signatures[model], args.base_run, args.base_activations)
    atomic_json(root / 'prepared.json', {'plan_digest': plan['digest'], 'budget': summary(plan)})
    print(json.dumps(summary(plan), indent=2))


def copy_baseline(root, model, wanted, signature, base_run, base_activations):
    """Copy the reused baseline generations (verified) and reference their archived activations."""
    dest = root / 'baseline_cache' / f'{model}.jsonl'
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        found = set(); tmp = dest.with_suffix('.tmp')
        with tmp.open('w') as out:
            for f in stream_member(base_run, f'data/generation_cache/{model}.jsonl'):
                for line in f:
                    record = json.loads(line)
                    key = record['key']
                    if key not in wanted or not record.get('ok'):
                        continue
                    if record['prompt_sha'] != wanted[key]['prompt_sha'] or record['settings_sha'] != signature:
                        raise ValueError(f'baseline prompt/settings mismatch: {key}')
                    if key not in found:
                        out.write(json.dumps(record)+'\n'); found.add(key)
        if found != set(wanted):
            raise ValueError(f'{model}: {len(set(wanted)-found)} baseline generations missing; refusing to regenerate them')
        tmp.replace(dest)
    # Keep archive row references; never duplicate or replay old activations.
    for f in stream_member(base_activations, f'data/activations/{model}/ckd/index.parquet'):
        index = pd.read_parquet(io.BytesIO(f.read()))
    for f in stream_member(base_activations, f'data/activations/{model}/ckd/meta.json'):
        meta = json.load(f)
    from second_order.freeze import index_digest
    if index_digest(index) != meta['items_sha256'] or not meta['complete']:
        raise ValueError('baseline activation index integrity failure')
    wanted_acts = {k for k,v in wanted.items() if v['output']=='probability/normal' and v['replicate']==0}
    refs = index[index.item_key.isin(wanted_acts)].copy()
    if set(refs.item_key) != wanted_acts:
        raise ValueError('missing baseline activation references')
    refs['source'] = 'baseline'; refs['array_member'] = f'data/activations/{model}/ckd/acts.npy'
    refs['array_sha256'] = meta['array_sha256']
    refs.to_parquet(root / f'baseline_activations_{model}.parquet', index=False)


def summary(plan):
    result = {}
    for model in plan['models']:
        items = [v for v in plan['items'].values() if v['model']==model]
        new = [v for v in items if v['source']=='new']
        result[model] = dict(reused_generations=len(items)-len(new), new_generations=len(new),
            new_by_output=dict(Counter(v['output'] for v in new)),
            new_activation_rows=sum(v['output']=='probability/normal' and v['replicate']==0 for v in new))
    return result


def read_plan(root):
    configure(root)
    plan = json.loads((root/'plan.json').read_text())
    if digest({k:v for k,v in plan.items() if k!='digest'}) != plan['digest']:
        raise ValueError('plan checksum mismatch')
    prepared = root / 'prepared.json'
    if not prepared.exists() or json.loads(prepared.read_text()).get('plan_digest') != plan['digest']:
        raise ValueError('baseline preparation is incomplete; rerun prepare before inference')
    for model in plan['models']:
        if G.settings_signature(model, 'ckd') != plan['settings'][model]:
            raise ValueError('sampling settings changed since preparation')
    return plan


def generate(root, model):
    plan = read_plan(root)
    if model not in plan['models']:
        raise ValueError(f'model {model} is not in the prepared plan')
    items = [item_from(v) for v in plan['items'].values() if v['model']==model and v['source']=='new']
    # Small durable batches limit repeated work after interruption. Does not alter vLLM sampling.
    if C.model_config(model)['backend'] == 'vllm':
        C.model_config(model)['batch_size'] = 128
    result = G.run(items, label=f'{model}/order-followup')
    atomic_json(root/f'generation_{model}.json', result)
    if G.pending(items):
        raise RuntimeError('generation incomplete; rerun to retry failed calls')


def capture(root, model, replayer_factory=None, items=None):
    """Activations of every new probability item of replicate 0 (or of ``items``: plan item dicts)."""
    from second_order.freeze import Replayer, POSITIONS
    plan = read_plan(root)
    if model not in plan['models']:
        raise ValueError(f'model {model} is not in the prepared plan')
    if items is None:
        items = [v for v in plan['items'].values() if v['model']==model and v['source']=='new'
                 and v['output']=='probability/normal' and v['replicate']==0]
    items = [item_from(v) for v in items]
    records = G.lookup(items)
    if any(not r or not r.get('ok') for r in records):
        raise ValueError('finish generation before capturing activations')
    replayer = None
    for item, record in zip(items, records):
        folder = root/'data/activations'/model/'ckd'/item.key
        folder.mkdir(parents=True, exist_ok=True)
        meta_path = folder/'meta.json'; array_path = folder/'acts.npy'
        identity = dict(item_key=item.key, prompt_sha=item.prompt_sha, settings_sha=record['settings_sha'],
                        response_sha=digest([record['text'], record['token_ids']]), revision=G.model_revision(model))
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            if any(meta[k]!=v for k,v in identity.items()):
                raise ValueError('existing activation identity mismatch')
            if array_path.exists():
                with array_path.open('rb') as f:
                    if hashlib.file_digest(f,'sha256').hexdigest()==meta['array_sha256']:
                        continue
        if replayer is None:
            replayer = (replayer_factory or Replayer)(model)
        ids, positions, n_prompt = replayer.tokens_and_positions(item,record)
        acts = replayer.run(ids,positions)[None, ...]
        tmp = folder/'acts.tmp'
        with tmp.open('wb') as f:
            np.save(f, acts); f.flush(); os.fsync(f.fileno())
        tmp.replace(array_path)
        with array_path.open('rb') as f:
            sha = hashlib.file_digest(f,'sha256').hexdigest()
        atomic_json(meta_path, identity | dict(complete=True, shape=list(acts.shape), dtype='uint16 bfloat16 bits',
            positions=list(POSITIONS), token_positions=positions, n_prompt_tokens=n_prompt, n_tokens=len(ids),
            **{f'token_{p}': k for p,k in zip(POSITIONS,positions)},
            array_sha256=sha, layers='0 embedding; i after block i; last after final norm'))
        print(f'[capture {model}] saved {item.case_id}/{item.condition} -> {folder}', flush=True)
    print(f'{model}: {len(items)} activation rows verified/captured (old arrays untouched)')


def score(item, record, trial):
    from second_order.responses import score_answer, visible_text
    # Match the archived 5b3a353 parser: last Choice line, following option text,
    # Markdown emphasis, and terminal JSON. Keep this scoped to the new workflow.
    text = visible_text((record or {}).get('text', '')) or ''
    if item.output.startswith('probability'):
        if not text.endswith("```"):
            text = text.rstrip("*_` \t\n")
    else:
        choice_line = re.compile(r"(?:^|[\s>#*_`:])choice[\s*_`]*:[\s*_`]*(.*?)\s*$", flags=re.I)
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        at = [k for k, line in enumerate(lines) if choice_line.search(line)]
        if at:
            text = " ".join([choice_line.search(lines[at[-1]]).group(1), *lines[at[-1] + 1:]]).strip()
        text = re.sub(r"[*_`]", "", text)
    return score_answer(item, None if record is None else record | {'text': text}, trial)


def export(root, model):
    plan = read_plan(root)
    baseline = G.Cache(model, root/'baseline_cache')
    new = G.Cache(model, C.CACHE)
    rows=[]
    for trial in plan['trials']:
        if trial['model']!=model: continue
        data=plan['items'][trial['item_key']];item=item_from(data)
        record=(baseline if data['source']=='baseline' else new).get(item.key)
        if record and (record['prompt_sha']!=item.prompt_sha or record['settings_sha']!=plan['settings'][model]):
            raise ValueError('response provenance mismatch')
        result=score(item,record,trial)
        rows.append(trial | dict(status=result.status,value=result.value if result.status=='parsed' else np.nan,
                                text=(record or {}).get('text'),prompt=item.prompt,condition=item.condition,
                                fit_family='self' if trial['role'] in 'AB' else 'person'))
    long=pd.DataFrame(rows);out=root/'outputs';out.mkdir(exist_ok=True)
    long.to_parquet(out/f'{model}_responses.parquet',index=False)
    belief=long[~long.output.str.startswith('check/')]
    ids=['model','case_id','setting','wording','block_order','role','replicate','fit_family']
    wide=belief.pivot(index=ids,columns='output',values='value').reset_index()
    wide.to_parquet(out/f'{model}_matrix.parquet',index=False)
    long.groupby(['setting','wording','block_order','role','status']).size().rename('count').to_csv(out/f'{model}_retention.csv')
    checks=long[long.output.str.startswith('check/')]
    checks.to_parquet(out/f'{model}_checks.parquet',index=False)
    refs=pd.read_parquet(root/f'baseline_activations_{model}.parquet').to_dict('records')
    for data in plan['items'].values():
        if data['model']!=model or data['source']!='new' or data['output']!='probability/normal' or data['replicate']!=0:continue
        folder=Path('data/activations')/model/'ckd'/data['key'];meta=root/folder/'meta.json'
        refs.append(dict(item_key=data['key'],source='new',array_member=str(folder/'acts.npy'),row=0,
                         complete=meta.exists()) | (json.loads(meta.read_text()) if meta.exists() else {}))
    atomic_json(out/f'{model}_activation_refs.json',refs)
    print(f'{model}: exported {len(long)} trial rows, {len(wide)} belief vectors; missing answers: {(long.status=="not_generated").sum()}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','status','generate','capture','export'])
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--base-run',type=Path)
    parser.add_argument('--base-activations',type=Path)
    parser.add_argument('--models',nargs='+',default=['qwen3_8b','qwen3_14b'])
    parser.add_argument('--model')
    args=parser.parse_args()
    args.run=args.run.resolve();args.run.mkdir(parents=True,exist_ok=True)
    with (args.run/'.followup.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if args.stage=='prepare':
            if not args.base_run or not args.base_activations:parser.error('prepare needs both baseline archives/directories')
            prepare(args)
        elif args.stage=='status':
            plan=read_plan(args.run);result=summary(plan)
            for model in plan['models']:
                its=[item_from(v) for v in plan['items'].values() if v['model']==model and v['source']=='new']
                result[model]['pending_generations']=len(G.pending(its))
            print(json.dumps(result,indent=2))
        else:
            if not args.model:parser.error('stage requires --model')
            {'generate':generate,'capture':capture,'export':export}[args.stage](args.run,args.model)


if __name__=='__main__':main()
