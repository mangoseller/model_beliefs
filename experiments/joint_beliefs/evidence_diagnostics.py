"""Locate evidence-fusion losses using saved predictions; never fit or select models."""
import argparse
import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from experiments.joint_beliefs.report import bootstrap_gain, family, render_table
from experiments.joint_beliefs.run import ROOT, CURVES, PROTOCOL, frame


def contrasts(combined, internal):
    def task(frame, name, kind=None):
        rows=frame[frame.task==name]
        return rows if kind is None else rows[rows.kind==kind]
    binary=task(combined,'leave_one_out','binary')
    reports=task(combined,'leave_one_out')
    reports=reports[reports.kind!='binary']
    return [
        ('none_to_probability',task(internal,'probability_to_decisions'),task(combined,'probability_to_decisions')),
        ('probability_to_all_reports',task(combined,'probability_to_decisions'),task(combined,'reports_to_decisions')),
        ('none_to_all_reports',task(internal,'reports_to_decisions'),task(combined,'reports_to_decisions')),
        ('all_reports_to_reports_and_other_decisions',task(combined,'reports_to_decisions'),binary),
        ('none_to_all_decisions',task(internal,'decisions_to_reports'),task(combined,'decisions_to_reports')),
        ('all_decisions_to_decisions_and_other_reports',task(combined,'decisions_to_reports'),reports),
    ]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size',choices=['4b','12b'],default='4b')
    args=parser.parse_args()
    out=ROOT/args.size/'evidence_diagnostics'
    out.mkdir(exist_ok=True)
    draws=np.random.default_rng(PROTOCOL['seed']).integers(0,103,size=(10000,103))
    summaries=[]
    details=[]
    for curve in CURVES:
        folder=ROOT/args.size/curve/'final'
        frames={}
        for method in ['activation','activation_only']:
            check=json.loads((folder/f'{method}_complete.json').read_text())
            if not check['training_numerically_stable'] or not check['quadrature']['passed'] or check['withheld_target_error']!=0:
                raise RuntimeError(f'Numerical or leakage flag: {curve}/{method}')
            frames[method]=pd.read_csv(folder/f'{method}_predictions.csv',float_precision='round_trip')
        for name,before,after in contrasts(frames['activation'],frames['activation_only']):
            paired=before.merge(after,on=['case_id','target','kind','actual'],suffixes=('_before','_after'),validate='one_to_one')
            if len(paired)!=len(before) or len(paired)!=len(after):
                raise ValueError('Missing paired predictions')
            paired['nll_gain']=paired.nll_before-paired.nll_after
            paired['contrast']=name
            paired['curve']=curve
            paired['family']=paired.target.map(family)
            columns=['curve','contrast','case_id','target','kind','family','actual',
                     'prediction_before','prediction_after','nll_before','nll_after','nll_gain']
            details.append(paired[columns])
            for scope in ['kind','family','target']:
                for group,rows in paired.groupby(scope):
                    targets=rows.target.unique()
                    result,delta=bootstrap_gain(before[before.target.isin(targets)],after[after.target.isin(targets)],draws)
                    summaries.append(dict(curve=curve,contrast=name,scope=scope,group=group,
                                          n_cases=len(delta),n_targets=len(targets),
                                          fraction_cases_helped=float((delta>0).mean()),
                                          median_case_gain=float(np.median(delta)),
                                          worst_case_gain=float(delta.min()),**result))
    summary=pd.DataFrame(summaries)
    details=pd.concat(details,ignore_index=True)
    summary.to_csv(out/'contrasts.csv',index=False)
    details.to_csv(out/'case_target_changes.csv',index=False)
    case_changes=(details.groupby(['curve','contrast','kind','case_id']).nll_gain.mean().reset_index())
    case_changes.to_csv(out/'case_changes.csv',index=False)
    data=frame(args.size,test=True)
    counts=[]
    for target in details.loc[details.kind=='binary','target'].unique():
        counts.append(dict(target=target,**{
            split+'_no':int((data.loc[data.split==split,target]==0).sum())
            for split in ['train','val','test']}))
    counts=pd.DataFrame(counts)
    counts.to_csv(out/'negative_response_counts.csv',index=False)
    primary=summary[summary.scope=='kind']
    direct=summary[(summary.scope=='family')&summary.contrast.isin(['none_to_all_reports','none_to_all_decisions'])]
    worst=(details[details.contrast.isin(['none_to_all_reports','none_to_all_decisions'])]
           .sort_values('nll_gain').groupby(['curve','kind'],sort=False).head(5))
    columns=['curve','contrast','group','nll_gain','nll_ci_low','nll_ci_high','fraction_cases_helped','median_case_gain']
    body=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{args.size.upper()} evidence diagnostics</title>
<style>body{{font:16px/1.6 system-ui;max-width:1250px;margin:36px auto;padding:0 24px;color:#203040}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}td,th{{padding:7px;border-bottom:1px solid #ddd}}.scroll{{overflow-x:auto}}</style></head><body>
<h1>{html.escape(args.size.upper())}: where does adding evidence hurt?</h1>
<p>This post hoc diagnostic compares saved predictions from the same fitted activation model with different external evidence. No parameters, layers, penalties, or evidence weights are fitted or selected here. “None” means activation-based predictions without external responses. Targets are always withheld.</p>
<p>Positive NLL gain means adding the named evidence helps. Negative gain means it hurts. Intervals use 10,000 paired patient resamples and do not adjust for multiple comparisons. These are exploratory diagnostics on an already inspected test set, not independent confirmation.</p>
<h2>Incremental evidence changes</h2><p>For binary targets, the sequence is no evidence → numerical probability → all three reports → reports plus the other twelve decisions. For report targets it is no evidence → thirteen decisions → decisions plus the other two reports. The order matters; these are not order-independent attributions.</p>
<div class="scroll">{render_table(primary,columns)}</div>
<h2>Which output families change?</h2><div class="scroll">{render_table(direct,columns)}</div>
<h2>How many negative responses informed the fitted curves?</h2><p>Counts of “no” answers among 313 training, 84 validation, and 103 test cases. Final fits use training plus validation. A negative response appearing only in test has no negative example in either fitting stage; apparent certainty on such an output is especially fragile. These counts are descriptive and do not change the split or remove any test cases.</p>
<div class="scroll">{render_table(counts,list(counts.columns))}</div>
<h2>Largest individual losses</h2><p>These cases are selected because they have the largest observed deterioration. They illustrate errors; they are not representative estimates or additional significance tests.</p>
<div class="scroll">{render_table(worst,['curve','kind','case_id','target','actual','prediction_before','prediction_after','nll_before','nll_after','nll_gain'])}</div>
<h2>What this can establish</h2><p>These comparisons locate deterioration and distinguish widespread small changes from a minority of large mistakes. They cannot identify whether conditional dependence, miscalibration, optimization, finite-sample variation, or an inadequate single latent dimension caused it. Probability NLL evaluates the whole predictive density, so a mean moving toward the observed value does not guarantee a better score.</p>
<p>A targeted follow-up should compare calibration or smoothing for rare responses with evidence tempering, then compare isolated evidence channels and response groups. A benefit from downweighting evidence would diagnose excessive influence, but would not by itself distinguish dependence from miscalibration. Confirmation needs new held-out cases.</p>
<p><a href="contrasts.csv">All kind, family, and target contrasts</a> · <a href="case_changes.csv">Patient-level changes</a> · <a href="case_target_changes.csv">Every prediction change</a> · <a href="../report.html">Main size report</a></p></body></html>'''
    (out/'report.html').write_text(body)
    (out/'source.py').write_bytes(Path(__file__).read_bytes())
    print(primary[columns].to_string(index=False))
    print(out/'report.html')


if __name__=='__main__':
    main()
