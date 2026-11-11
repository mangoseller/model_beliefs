"""Consolidate completed original/spline results for one model size."""
import argparse
import json
import numpy as np
import pandas as pd

from experiments.joint_beliefs.report import summarize, bootstrap_gain, render_table
from experiments.joint_beliefs.run import ROOT, CURVES, METHODS, PROTOCOL


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size',default='4b',choices=['4b','12b'])
    args=parser.parse_args()
    root=ROOT/args.size
    frames={}
    flags=[]
    for curve in CURVES:
        for method in METHODS+('activation_only',):
            folder=root/curve/'final'
            if not (folder/f'{method}_complete.json').exists():
                raise RuntimeError(f'Incomplete {curve}/{method}')
            check=json.loads((folder/f'{method}_complete.json').read_text())
            if not check['training_numerically_stable'] or not check['quadrature']['passed'] or check['withheld_target_error'] != 0:
                flags.append(curve+'/'+method)
            frames[(curve,method)]=pd.read_csv(folder/f'{method}_predictions.csv',float_precision='round_trip')
    summary=summarize(pd.concat(list(frames.values()),ignore_index=True))
    summary.to_csv(root/'summary.csv',index=False)
    pairs=[]
    for curve in CURVES:
        for control in ['behavior','raw_profile','shuffled','activation_only']:
            pairs.append(((curve,control),(curve,'activation')))
        pairs.append(((curve,'behavior'),(curve,'activation_only')))
    pairs += [(('linear','behavior'),('monotone_spline','behavior')),
              (('linear','activation'),('monotone_spline','activation')),
              (('monotone_spline','behavior'),('linear','activation'))]
    draws=np.random.default_rng(PROTOCOL['seed']).integers(0,103,size=(10000,103))
    comparisons=[]
    for before,after in pairs:
        a,b=frames[before],frames[after]
        for (task,kind),group in a.groupby(['task','kind']):
            result,_=bootstrap_gain(group,b[(b.task==task)&(b.kind==kind)],draws)
            comparisons.append(dict(before='/'.join(before),after='/'.join(after),task=task,kind=kind,**result))
    comparisons=pd.DataFrame(comparisons)
    comparisons.to_csv(root/'paired_comparisons.csv',index=False)
    main_summary=summary[summary.task.isin(['reports_to_decisions','decisions_to_reports']) &
                         summary.method.isin(['behavior','activation','raw_profile'])]
    primary=comparisons[comparisons.task.isin(['reports_to_decisions','decisions_to_reports']) &
                        ~comparisons.before.str.contains('shuffled|activation_only')]
    checks_note='All selected-fit integration, final-prediction integration, and leakage checks passed.' if not flags else 'Numerical flags requiring caution: '+', '.join(flags)
    content=f'''<!doctype html><html><head><meta charset="utf-8"><title>{args.size.upper()} joint-belief comparison</title>
<style>body{{font:16px/1.6 system-ui;max-width:1200px;margin:35px auto;padding:0 24px;color:#203040}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}td,th{{padding:8px;border-bottom:1px solid #ddd}}.scroll{{overflow-x:auto}}</style></head><body>
<h1>{args.size.upper()}: complete joint-belief comparison</h1>
<p>Both response-curve specifications and all controls are complete on the same 103 test patients. A single fitted direction serves every response type and prediction task.</p>
<p>The patient-feature comparison measures incremental predictive utility of reading activations. Equal performance is compatible with coherent internal beliefs formed from the patient evidence; outperforming patient features is neither necessary nor sufficient to identify a belief mechanism. Shared predictive adequacy across different responses is a separate question.</p>
<p><strong>{checks_note}</strong></p>
<p><strong>Scores predict language-model responses, not clinical outcomes.</strong> Lower NLL/MSE is better; higher concordance/AUROC and R² is better. Probability-report NLL is a density score and may be negative. Compare NLL only within the same task and output kind.</p>
<h2>Primary reciprocal prediction results</h2><p>Reports → decisions uses all three belief reports to predict thirteen decisions. Decisions → reports uses those thirteen decisions to predict numerical and ordinal reports.</p>
<div class="scroll">{render_table(main_summary,['curve','method','task','kind','mean_nll','mean_c_index','r2_vs_test_constant'])}</div>
<h2>Paired contrasts</h2><p>Positive gains favor the method in the “after” column. Intervals use 10,000 paired case-bootstrap draws and are conditional on fitted models.</p>
<div class="scroll">{render_table(primary,['before','after','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<details><summary>All tasks, shuffled controls, and activation-only ablations</summary><div class="scroll">{render_table(summary,['curve','method','task','kind','mean_nll','mse','mean_c_index','r2_vs_test_constant'])}</div></details>
<h2>Interpretation limits</h2><p>The test set informed earlier research choices, so these results are exploratory. Case-bootstrap intervals do not include training or selection variability and are not adjusted for multiple comparisons. Local quantized activations are paired with hosted responses. One shuffled fit is a sanity check, not a permutation significance test.</p>
<p><a href="linear/final/partial_report.html">Original-curve report and checks</a> · <a href="monotone_spline/final/partial_report.html">Spline report and checks</a> · <a href="summary.csv">Metrics CSV</a> · <a href="paired_comparisons.csv">All paired contrasts</a> · <a href="../protocol.json">Protocol</a></p></body></html>'''
    (root/'report.html').write_text(content)
    print(main_summary[['curve','method','task','kind','mean_nll','mean_c_index','r2_vs_test_constant']].to_string(index=False))
    print(root/'report.html')


if __name__=='__main__':
    main()
