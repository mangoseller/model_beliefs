"""Exploratory direct scalar probe, with no latent integration or activation noise.

Freeze the flexible behavioral response curves and previously selected layer.
Fit only a shared linear scalar and its intercept to the four training betting
responses. Select L2 on validation cases. The current test set has already been
inspected in the preceding experiment, so this follow-up is exploratory.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F

import experiments.activation_pilot.fit as base
from experiments.activation_pilot.compare import auc


def logits(model, score):
    intercept=torch.stack([model.binary_intercepts[n] for n in base.NAMES[8:]])
    slope=torch.stack([model.binary_slope(n) for n in base.NAMES[8:]])
    return intercept[None,:]+slope[None,:]*score[:,None]


def fit(model,x,y,indices,penalty):
    w=torch.zeros(x.shape[1],dtype=torch.float64,requires_grad=True)
    b=torch.zeros((),dtype=torch.float64,requires_grad=True)
    optimizer=torch.optim.LBFGS([w,b],max_iter=200,tolerance_grad=1e-8,
                               tolerance_change=1e-10,line_search_fn='strong_wolfe')
    def closure():
        optimizer.zero_grad()
        objective=F.binary_cross_entropy_with_logits(logits(model,x[indices]@w+b),y[indices])+penalty*w.square().sum()
        objective.backward()
        return objective
    optimizer.step(closure)
    return w.detach(),b.detach()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--larger',action='store_true')
    args=parser.parse_args()
    root=Path('build/activation_pilot_12b' if args.larger else 'build/activation_pilot')
    base.FLEXIBLE=True
    base.MODEL_KEY='gemma3_12b' if args.larger else 'gemma3_4b'
    torch.set_num_threads(2)
    df=base.data()
    train=np.flatnonzero(df.split=='train'); val=np.flatnonzero(df.split=='val'); test=np.flatnonzero(df.split=='test')
    alltrain=np.concatenate([train,val])
    out=root/'direct';out.mkdir(parents=True,exist_ok=True)
    selection=json.loads((root/'flexible/selection.json').read_text())
    layer=selection['layer']
    h=np.stack([np.load(root/'activations'/f'{case}.npz')['h'][layer-1]
                for case in df.source_prompt_id]).astype(float)
    y=torch.tensor(df[base.NAMES[8:]].to_numpy(float))
    model=base.new_model();model.load_state_dict(torch.load(root/'flexible/baseline_selection.pt',weights_only=True));base.freeze(model)
    selected={};search=[]
    for kind in ['internal','raw_profile']:
        x=base.features(h,train)[0] if kind=='internal' else base.covariates(df,train)
        for penalty in [.001,.01,.1,1.]:
            w,b=fit(model,x,y,train,penalty)
            with torch.no_grad():
                score=float(F.binary_cross_entropy_with_logits(logits(model,x[val]@w+b),y[val]))
            search.append(dict(kind=kind,penalty=penalty,validation_log_loss=score))
        selected[kind]=min([r for r in search if r['kind']==kind],key=lambda r:r['validation_log_loss'])
    pd.DataFrame(search).to_csv(out/'validation_search.csv',index=False)
    (out/'selection.json').write_text(json.dumps(dict(layer=layer,models=selected),indent=2))
    model=base.new_model();model.load_state_dict(torch.load(root/'flexible/baseline_final.pt',weights_only=True));base.freeze(model)
    predictions={}
    for kind in ['internal','raw_profile']:
        if kind=='internal':
            x,mean,scale=base.features(h,alltrain)
        else:
            x=base.covariates(df,alltrain)
        w,b=fit(model,x,y,alltrain,selected[kind]['penalty'])
        with torch.no_grad():predictions[kind]=logits(model,x[test]@w+b).sigmoid().numpy()
        extra=dict(mean=mean,scale=scale,layer=layer) if kind=='internal' else {}
        np.savez(out/f'{kind}_probe.npz',w=w.numpy(),b=b.numpy(),**extra)
    # Include the external baseline and preceding augmented method on identical rows.
    previous=pd.read_csv(root/'flexible/test_predictions.csv')
    for kind in ['behavior_only','augmented','activation_only']:
        predictions[kind]=previous.pivot(index='case_id',columns='target',values=kind).loc[
            df.iloc[test].source_prompt_id,base.NAMES[8:]].to_numpy()
    actual=y[test].numpy()
    metrics={};losses={}
    for kind,p in predictions.items():metrics[kind],losses[kind]=base.metrics(actual,p)
    rng=np.random.default_rng(base.SEED)
    draws=rng.integers(0,len(test),size=(5000,len(test)))
    contrasts={}
    for kind in ['behavior_only','raw_profile','augmented']:
        difference=losses[kind]-losses['internal']
        auc_diff=np.array([auc(actual[ix],predictions['internal'][ix])-auc(actual[ix],predictions[kind][ix]) for ix in draws])
        contrasts[kind]=dict(log_loss_improvement=float(difference.mean()),
                            log_loss_ci95=np.quantile(difference[draws].mean(1),[.025,.975]).tolist(),
                            auc_improvement=auc(actual,predictions['internal'])-auc(actual,predictions[kind]),
                            auc_ci95=np.quantile(auc_diff,[.025,.975]).tolist())
    result=dict(model_key=base.MODEL_KEY,exploratory=True,layer_fixed_from_previous_validation=layer,
                selection=selected,metrics=metrics,internal_vs_comparators=contrasts,n_test=len(test))
    records=[]
    for i,case in enumerate(df.iloc[test].source_prompt_id):
        for j,target in enumerate(base.NAMES[8:]):
            records.append(dict(case_id=case,target=target,actual=actual[i,j],
                                **{k:float(p[i,j]) for k,p in predictions.items()}))
    pd.DataFrame(records).to_csv(out/'test_predictions.csv',index=False)
    rows=''.join(f'<tr><td>{k}</td><td>{v["log_loss"]:.4f}</td><td>{v["mean_auc"]:.4f}</td></tr>' for k,v in metrics.items())
    (out/'report.html').write_text(f'''<!doctype html><html><meta charset="utf-8"><title>Direct scalar probe</title>
<style>body{{font:17px/1.6 system-ui;max-width:950px;margin:40px auto;padding:0 24px}}td,th{{padding:10px;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap}}</style>
<h1>Direct scalar probe: {base.MODEL_KEY}</h1><p><strong>Exploratory follow-up on an already inspected test set.</strong> Layer {layer} is fixed from the preceding validation selection. No latent integration or extra noise parameter is used.</p>
<p>One linear scalar predicts all four betting decisions through the frozen flexible response curves. Probe weights and an intercept are fit on training cases, with L2 selected on validation cases. The patient-feature control has the same scalar restriction and response curves. The final probes are refit on train plus validation.</p>
<table><tr><th>Method</th><th>Log loss ↓</th><th>Mean AUROC ↑</th></tr>{rows}</table>
<h2>Paired comparisons</h2><p>Positive improvements favor the directly trained internal probe. Intervals are conditional case bootstraps, not independent confirmation of this follow-up hypothesis.</p><pre>{json.dumps(contrasts,indent=2)}</pre>
<p>The preceding activation-only condition is an ablation of the combined model, not this directly trained probe. All conclusions retain the local 4-bit versus hosted-response checkpoint limitation. New cases are needed for confirmation.</p></html>''')
    (out/'results.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)
    print(out/'report.html',flush=True)


if __name__=='__main__':main()
