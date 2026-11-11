"""Select calibration on validation; lock all choices before test evaluation."""
import argparse
import itertools
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from experiments.belief_calibration.model import Prepared
from experiments.joint_beliefs.model import JointBeliefModel
from experiments.joint_beliefs.run import (ROOT as ORIGINAL, CURVES, SIZES, frame,
    activation_arrays, standardize, covariates, behavior, load_joint, digest, save_json)
from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values

ROOT = Path('build/joint_beliefs/calibration_followup')
METHODS = ['behavior', 'activation', 'raw_profile']
EPSILONS = [0., .001, .005, .01, .025, .05, .1]
TEMPERATURES = [0., .1, .25, .5, .75, 1.]
ARMS = ['original', 'smoothing', 'tempering', 'combined', 'fixed_floor']
PROTOCOL = dict(
    seed=20261108, sizes=list(SIZES), curves=list(CURVES), methods=METHODS,
    epsilons=EPSILONS, temperatures=TEMPERATURES, arms=ARMS,
    correction='Binary likelihood and mean become (1-epsilon)*original + epsilon*Bernoulli(0.5), for all thirteen binary outputs; probability and ordinal likelihoods unchanged.',
    evidence='Multiply the sum of observed corrected log likelihoods by temperature; do not temper targets or prior. Temperature below one is a generalized posterior.',
    fixed_floor='Untuned half-count shrinkage heuristic: epsilon=1/(n_fit+1), giving a binary floor of 0.5/(n_fit+1). Not a full Bayesian fit.',
    fitting='Freeze existing training-only models for calibration selection; apply chosen parameters to existing train+validation final fits. No layer, penalty, response-curve or probe refit.',
    selection='Minimum validation mean of three mean NLLs: reports-to-decisions binary, decisions-to-reports probability, decisions-to-reports ordinal; equal one-third weights; one global parameter pair per model across all tasks and outputs.',
    tie_rule='Within 1e-5 of minimum, prefer smaller epsilon then larger temperature.',
    partitions=dict(train=313,validation=84,test=103),
    selection_audit='Grid at 512 then 1024/2048/4096 points until all three mean-score changes <0.001 and selected pairs unchanged; fail rather than score test if unstable.',
    evaluation_audit='All tasks at 512 then 1024/2048/4096 until maximum individual NLL and prediction changes <0.001; preserve fitted parameters and prior knots.',
    ablations='For activation models also evaluate original and selected combined correction without external evidence; no extra selection.',
    uncertainty='10000 paired case bootstrap draws, conditional on fitted and selected models, no multiplicity adjustment.',
    exploratory=True,
    limitations=['Follow-up motivated by inspected test results; not fresh confirmation.',
                 'Validation reused from original layer/penalty selection.',
                 'Frozen-model calibration does not test retraining with response priors.',
                 'Quantized local activations paired with hosted responses.',
                 'Patient-feature parity does not refute coherent evidence-based beliefs.'],
)


def initialize():
    ROOT.mkdir(parents=True, exist_ok=True)
    if (ROOT/'protocol.json').exists():
        assert json.loads((ROOT/'protocol.json').read_text()) == PROTOCOL
        return
    lock=json.loads((ORIGINAL/'selection_lock.json').read_text())
    hashes={str(ORIGINAL/'protocol.json'):digest(ORIGINAL/'protocol.json')}
    assert hashes[str(ORIGINAL/'protocol.json')] == lock['protocol_sha256']
    for size,curve in itertools.product(SIZES,CURVES):
        cell=ORIGINAL/size/curve
        assert digest(cell/'selection.json') == lock['selections'][f'{size}/{curve}']
        hashes[str(cell/'selection.json')]=digest(cell/'selection.json')
        selections=json.loads((cell/'selection.json').read_text())
        for method in METHODS:
            selected=cell/'baseline/selected_model.pt' if method=='behavior' else Path(selections[method]['path'])/'model.pt'
            for path in [selected,cell/'final'/f'{method}.pt']:
                hashes[str(path)]=digest(path)
    for path in ['build/datasets/ckd.csv','data/release/sources/ckd/cases.csv',
                 'experiments/joint_beliefs/run.py','experiments/joint_beliefs/model.py']:
        hashes[path]=digest(Path(path))
    save_json(ROOT/'input_manifest.json',hashes)
    save_json(ROOT/'protocol.json',PROTOCOL)
    save_json(ROOT/'started.json',dict(unix=time.time()))
    (ROOT/'source').mkdir(exist_ok=True)
    for name in ['model.py','run.py','report.py','test_model.py']:
        path=Path('experiments/belief_calibration')/name
        if path.exists(): (ROOT/'source'/name).write_bytes(path.read_bytes())


def verify_inputs():
    assert json.loads((ROOT/'protocol.json').read_text()) == PROTOCOL
    for path,expected in json.loads((ROOT/'input_manifest.json').read_text()).items():
        assert digest(Path(path)) == expected,path
    for name in ['model.py','run.py']:
        assert (ROOT/'source'/name).read_bytes() == (Path('experiments/belief_calibration')/name).read_bytes()


def data(size, final=False):
    df=frame(size,test=final)
    fit=np.flatnonzero(df.split!='test' if final else df.split=='train')
    evaluation=np.flatnonzero(df.split=='test' if final else df.split=='val')
    assert (len(fit),len(evaluation)) == ((397,103) if final else (313,84))
    if not final: assert not (df.split=='test').any()
    y=torch.tensor(likelihood_values(df,CKD_SPECS),dtype=torch.float64)
    return df,fit,evaluation,y,activation_arrays(size,df),covariates(df,fit)


def model_and_features(size,curve,method,bundle,final=False):
    df,fit,evaluation,y,activations,raw=bundle
    cell=ORIGINAL/size/curve
    selected=json.loads((cell/'selection.json').read_text())[method]
    if method=='behavior': x=torch.empty((len(df),0),dtype=torch.float64)
    elif method=='raw_profile': x=raw
    else: x,_=standardize(activations[selected['layer']],fit)
    if final:
        model=load_joint(cell/'final'/f'{method}.pt',curve,x.shape[1],selected['quadrature'])
    elif method=='behavior':
        b=behavior(curve,selected['quadrature'])
        b.load_state_dict(torch.load(cell/'baseline/selected_model.pt',weights_only=True))
        model=JointBeliefModel(b,0)
    else:
        model=load_joint(Path(selected['path'])/'model.pt',curve,x.shape[1],selected['quadrature'])
    return model,y[evaluation],x[evaluation]


def score(records):
    groups={}
    for r in records:
        kind=CKD_SPECS[r['target']].kind
        groups.setdefault(kind,[]).append(r['nll'])
    result={kind:float(np.mean(values)) for kind,values in groups.items()}
    assert set(result)=={'binary','probability','ordinal'}
    return dict(**result,objective=float(np.mean(list(result.values()))))


def choose(grid):
    result={}
    for arm in ['smoothing','tempering','combined']:
        candidates=[r for r in grid if (arm!='smoothing' or r['temperature']==1.) and
                    (arm!='tempering' or r['epsilon']==0.)]
        minimum=min(r['objective'] for r in candidates)
        chosen=min([r for r in candidates if r['objective']<=minimum+1e-5],
                   key=lambda r:(r['epsilon'],-r['temperature']))
        result[arm]={k:chosen[k] for k in ['epsilon','temperature']}
    return result


def select(size):
    verify_inputs()
    if (ROOT/'selection_lock.json').exists(): raise RuntimeError('Already locked')
    bundle=data(size)
    for curve,method in itertools.product(CURVES,METHODS):
        out=ROOT/size/curve/method
        if (out/'selection.json').exists(): continue
        out.mkdir(parents=True,exist_ok=True)
        model,y,x=model_and_features(size,curve,method,bundle)
        previous=None
        audits=[]
        for points in [512,1024,2048,4096]:
            prepared=Prepared(model.with_quadrature(points),y,x)
            grid=[]
            for epsilon,temperature in itertools.product(EPSILONS,TEMPERATURES):
                grid.append(dict(epsilon=epsilon,temperature=temperature,
                                 **score(prepared.evaluate(epsilon,temperature,primary=True))))
            selected=choose(grid)
            pd.DataFrame(grid).to_csv(out/f'validation_grid_q{points}.csv',index=False)
            if previous is not None:
                change=max(abs(a[k]-b[k]) for a,b in zip(previous,grid) for k in ['binary','probability','ordinal'])
                stable=choose(previous)==selected
                audits.append(dict(points=points,max_group_nll_change=change,selection_stable=stable,
                                   passed=change<.001 and stable))
                if audits[-1]['passed']: break
            previous=grid
        if not audits[-1]['passed']: raise RuntimeError(f'Unstable calibration grid: {size}/{curve}/{method}')
        selected.update(original=dict(epsilon=0.,temperature=1.),
                        fixed_floor=dict(epsilon=1/(len(bundle[1])+1),temperature=1.))
        save_json(out/'selection.json',dict(arms=selected,grid_audits=audits,validation_points=points,
                  n_fit=len(bundle[1]),n_validation=len(y),test_loaded=False))
        print(f'SELECTED {size}/{curve}/{method}: {selected}',flush=True)


def lock():
    verify_inputs()
    paths=[ROOT/s/c/m/'selection.json' for s,c,m in itertools.product(SIZES,CURVES,METHODS)]
    result=dict(protocol_sha256=digest(ROOT/'protocol.json'),
                selections={str(p.relative_to(ROOT)):digest(p) for p in paths})
    if (ROOT/'selection_lock.json').exists(): assert json.loads((ROOT/'selection_lock.json').read_text())==result
    else: save_json(ROOT/'selection_lock.json',result)


def audit_predictions(model,y,x,epsilon,temperature):
    previous=Prepared(model.with_quadrature(512),y,x).evaluate(epsilon,temperature)
    audits=[]
    for points in [1024,2048,4096]:
        current=Prepared(model.with_quadrature(points),y,x).evaluate(epsilon,temperature)
        changes={k:max(float(np.max(np.abs(a[k]-b[k]))) for a,b in zip(previous,current))
                 for k in ['prediction','nll']}
        audits.append(dict(points=points,**changes,passed=max(changes.values())<.001))
        previous=current
        if audits[-1]['passed']: break
    return current,dict(evaluation_points=points,steps=audits,passed=audits[-1]['passed'])


def rows_for(records,df,indices,y,fit,size,curve,method,arm):
    rows=[]
    for r in records:
        j=r['target'];spec=CKD_SPECS[j]
        for i,case in enumerate(df.iloc[indices].source_prompt_id):
            row=dict(size=size,curve=curve,method=method,arm=arm,case_id=case,task=r['task'],
                     target=spec.name,kind=spec.kind,actual=float(y[indices[i],j]),
                     prediction=float(r['prediction'][i]),nll=float(r['nll'][i]),
                     train_mean=float(y[fit,j].mean()))
            if 'category_probabilities' in r:
                row.update({f'p_category_{k+1}':float(v) for k,v in enumerate(r['category_probabilities'][i])})
            rows.append(row)
    return pd.DataFrame(rows)


def evaluate(size):
    verify_inputs()
    frozen=json.loads((ROOT/'selection_lock.json').read_text())
    assert digest(ROOT/'protocol.json')==frozen['protocol_sha256']
    for path,expected in frozen['selections'].items(): assert digest(ROOT/path)==expected
    bundle=data(size,final=True)
    df,fit,indices,y_all,_,_=bundle
    for curve,method in itertools.product(CURVES,METHODS):
        out=ROOT/size/curve/method
        if (out/'complete.json').exists(): continue
        selected=json.loads((out/'selection.json').read_text())['arms']
        selected['fixed_floor']['epsilon']=1/(len(fit)+1)
        model,y,x=model_and_features(size,curve,method,bundle,final=True)
        old=ORIGINAL/size/curve/'final'
        old_points=json.loads((old/f'{method}_complete.json').read_text())['quadrature']['evaluation_points']
        original=Prepared(model.with_quadrature(old_points),y,x).evaluate()
        actual=rows_for(original,df,indices,y_all,fit,size,curve,method,'original')
        expected=pd.read_csv(old/f'{method}_predictions.csv',float_precision='round_trip')
        keys=['task','target','case_id']
        a,b=actual.sort_values(keys),expected.sort_values(keys)
        assert a[keys].equals(b[keys])
        identity=float(np.max(np.abs(a[['prediction','nll']].to_numpy()-b[['prediction','nll']].to_numpy())))
        assert identity<1e-8,(size,curve,method,identity)
        if method=='activation':
            selected['original_no_evidence']=dict(epsilon=0.,temperature=0.)
            selected['combined_no_evidence']=dict(epsilon=selected['combined']['epsilon'],temperature=0.)
        frames=[];checks=[];cache={}
        for arm,params in selected.items():
            key=(params['epsilon'],params['temperature'])
            if key not in cache: cache[key]=audit_predictions(model,y,x,*key)
            records,audit=cache[key]
            frames.append(rows_for(records,df,indices,y_all,fit,size,curve,method,arm))
            checks.append(dict(arm=arm,**params,**audit))
        predictions=pd.concat(frames,ignore_index=True)
        predictions.to_csv(out/'test_predictions.csv',index=False)
        save_json(out/'complete.json',dict(original_identity_max_error=identity,checks=checks,
                  all_checks_passed=all(c['passed'] for c in checks),rows=len(predictions)))
        print(f'EVALUATED {size}/{curve}/{method}: {len(selected)} arms; checks={all(c["passed"] for c in checks)}',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['initialize','select','lock','evaluate'])
    parser.add_argument('--size',choices=SIZES,default='4b')
    args=parser.parse_args()
    torch.set_num_threads(2)
    {'initialize':initialize,'select':lambda:select(args.size),'lock':lock,
     'evaluate':lambda:evaluate(args.size)}[args.stage]()


if __name__=='__main__':main()
