"""Diagnostic validation scores for already selected models; never selects models."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from experiments.werewolf_beliefs.run import *
from experiments.werewolf_beliefs.report import summarize

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--size',choices=SIZES,default='4b')
    args=parser.parse_args()
    initialize()
    torch.set_num_threads(2)
    df=frame(args.size)
    train,val=np.flatnonzero(df.split=='train'),np.flatnonzero(df.split=='val')
    y=torch.tensor(likelihood_values(df,WEREWOLF_SPECS),dtype=torch.float64)
    raw=activation_arrays(args.size,df)
    text=covariates(df,train)
    rows=[]
    for curve in CURVES:
        cell=ROOT/args.size/curve
        if not (cell/'selection.json').exists(): continue
        selected=json.loads((cell/'selection.json').read_text())
        for method in METHODS:
            choice=selected[method]
            if method=='behavior':
                b=behavior(curve,choice['quadrature'])
                b.load_state_dict(torch.load(cell/'baseline/selected_model.pt',weights_only=True))
                m=JointBeliefModel(b,0)
                x=torch.empty((len(df),0),dtype=torch.float64)
            else:
                x=text if method=='raw_profile' else standardize(raw[choice['layer']],train)[0]
                if method=='shuffled': x=shuffled(x,[train,val])
                m=load_joint(Path(choice['path'])/'model.pt',curve,x.shape[1],choice['quadrature'])
            records,audit=evaluate_stable(m,y[val],x[val])
            for r in records:
                j=r['target'];spec=WEREWOLF_SPECS[j]
                for i,case in enumerate(df.iloc[val].source_prompt_id):
                    rows.append(dict(size=args.size,curve=curve,method=method,case_id=case,task=r['task'],target=spec.name,kind=spec.kind,actual=float(y[val[i],j]),prediction=float(r['prediction'][i]),nll=float(r['nll'][i]),train_mean=float(y[train,j].mean())))
    result=summarize(pd.DataFrame(rows))
    result.to_csv(ROOT/args.size/'validation_summary.csv',index=False)
    subset=result[result.task.isin(['reports_to_decisions','decisions_to_reports'])&result.method.isin(['behavior','activation','raw_profile'])]
    print(subset[['curve','method','task','kind','mean_nll','r2_vs_test_constant']].to_string(index=False))
    print('VALIDATION ONLY: these cases were used for selection; results are optimistic, not held-out evidence.')

if __name__=='__main__':main()
