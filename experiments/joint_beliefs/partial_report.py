"""Publish completed test comparisons for one locked cell during the full run."""
import argparse
import json

import numpy as np
import pandas as pd

from experiments.joint_beliefs.report import summarize, bootstrap_gain, render_table, family, LABELS
from experiments.joint_beliefs.run import ROOT, METHODS, PROTOCOL


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size',default='4b',choices=['4b','12b'])
    parser.add_argument('--curve',default='linear',choices=['linear','monotone_spline'])
    args=parser.parse_args()
    cell=ROOT/args.size/args.curve
    if not (cell/'selection_lock.json').exists() and not (ROOT/'selection_lock.json').exists():
        raise RuntimeError('Cell selection has not been locked')
    out=cell/'final'
    ready=[m for m in METHODS+('activation_only',) if (out/f'{m}_complete.json').exists()]
    if not {'behavior','activation'}.issubset(ready):
        raise RuntimeError('Behavior and activation predictions are not both complete')
    data=pd.concat([pd.read_csv(out/f'{m}_predictions.csv',float_precision='round_trip') for m in ready],ignore_index=True)
    summary=summarize(data)
    summary.to_csv(out/'partial_summary.csv',index=False)
    draws=np.random.default_rng(PROTOCOL['seed']).integers(0,103,size=(10000,103))
    contrasts=[]
    for before in [m for m in ready if m!='activation']:
        for (task,kind),group in data.groupby(['task','kind']):
            result,_=bootstrap_gain(group[group.method==before],group[group.method=='activation'],draws)
            contrasts.append(dict(comparison='activation_vs_'+before,task=task,kind=kind,**result))
    contrasts=pd.DataFrame(contrasts)
    contrasts.to_csv(out/'partial_comparisons.csv',index=False)
    families=[]
    binary=data[(data.task=='reports_to_decisions')&(data.kind=='binary')].copy()
    binary['family']=binary.target.map(family)
    for name,group in binary.groupby('family'):
        result,_=bootstrap_gain(group[group.method=='behavior'],group[group.method=='activation'],draws)
        families.append(dict(family=name,**result))
    families=pd.DataFrame(families)
    families.to_csv(out/'partial_families.csv',index=False)
    base_rates=(data[(data.method=='behavior')&(data.task=='leave_one_out')&(data.kind=='binary')]
                .groupby('target').agg(n_cases=('case_id','nunique'),positives=('actual','sum'),
                                       positive_rate=('actual','mean')).reset_index())
    base_rates.to_csv(out/'decision_base_rates.csv',index=False)
    checks={m:json.loads((out/f'{m}_complete.json').read_text()) for m in ready}
    flags=[m for m,r in checks.items() if not r['training_numerically_stable'] or not r['quadrature']['passed'] or r['withheld_target_error']!=0]
    primary=summary[summary.task.isin(['reports_to_decisions','decisions_to_reports'])]
    primary_contrasts=contrasts[contrasts.task.isin(['reports_to_decisions','decisions_to_reports'])]
    note='All completed models passed numerical and leakage checks.' if not flags else 'Numerical flags: '+', '.join(flags)
    content=f'''<!doctype html><html><head><meta charset="utf-8"><title>{args.size.upper()} rolling results</title>
<style>body{{font:16px/1.6 system-ui;max-width:1200px;margin:30px auto;padding:0 24px;color:#203040}}table{{border-collapse:collapse;font-size:13px}}td,th{{padding:8px;border-bottom:1px solid #ddd}}.scroll{{overflow-x:auto}}</style></head><body>
<h1>{args.size.upper()} joint-belief results: {args.curve}</h1>
<p>Rolling results on the same 103 held-out CKD cases. Completed methods: {', '.join(LABELS[m] for m in ready)}.</p>
<p><strong>{note}</strong> Layer, penalty, and epoch choices were locked before these test predictions. Other cells continue under the unchanged recorded training protocol.</p>
<h2>Reciprocal prediction</h2><p>Lower NLL/MSE is better; higher AUROC/concordance and R² is better. Scores predict model responses, not clinical outcomes. Probability NLL is a density score and may be negative; do not compare its scale with binary or ordinal NLL.</p>
<div class="scroll">{render_table(primary,['method','task','kind','mean_nll','mse','mean_c_index','r2_vs_test_constant'])}</div>
<h2>Paired improvements</h2><p>Positive gains favor activations. Intervals use 10,000 case-bootstrap draws, conditional on the fitted models.</p>
<div class="scroll">{render_table(primary_contrasts,['comparison','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<h2>Gains by decision family</h2><p>Reports are the evidence; decisions are the targets. These rows compare activations plus reports against reports alone.</p>
<div class="scroll">{render_table(families,['family','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<p>Nearly constant decisions provide little information about differences between patients. Response frequencies help interpret the asymmetry between predicting reports and predicting decisions.</p>
<details><summary>Show decision response frequencies</summary><div class="scroll">{render_table(base_rates,list(base_rates.columns))}</div></details>
<h2>Interpretation limits</h2><p>This is exploratory: the test set informed earlier research decisions. Intervals do not include training/selection variability or adjust for multiple comparisons. Activations are from local quantized checkpoints and responses from hosted inference. The remaining controls and specifications should be considered before drawing a conclusion about beliefs.</p>
<p><a href="partial_summary.csv">All current metrics</a> · <a href="partial_comparisons.csv">Paired contrasts</a> · <a href="partial_families.csv">Family comparisons</a> · <a href="../../../rolling_evaluation.json">Rolling-evaluation amendment</a></p></body></html>'''
    (out/'partial_report.html').write_text(content)
    print(primary[['method','task','kind','mean_nll','mean_c_index','r2_vs_test_constant']].to_string(index=False))
    print(primary_contrasts.to_string(index=False))
    print(families.to_string(index=False))
    print(note)
    print(out/'partial_report.html')


if __name__=='__main__':
    main()
