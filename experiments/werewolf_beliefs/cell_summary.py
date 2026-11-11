"""Summarize one completed locked cell without changing any selections."""
import argparse,json
import pandas as pd
import numpy as np
from experiments.werewolf_beliefs import report
from experiments.werewolf_beliefs.run import ROOT,METHODS,PROTOCOL,save_json

def main():
    p=argparse.ArgumentParser();p.add_argument('--size',choices=['4b','12b'],required=True);p.add_argument('--curve',choices=['linear','monotone_spline'],required=True);a=p.parse_args()
    cell=ROOT/a.size/a.curve
    assert (cell/'selection_lock.json').exists()
    predictions=[];audits=[]
    for method in METHODS+('activation_only',):
        audit=json.loads((cell/'final'/f'{method}_complete.json').read_text())
        audits.append(audit)
        predictions.append(pd.read_csv(cell/'final'/f'{method}_predictions.csv',float_precision='round_trip'))
    df=pd.concat(predictions,ignore_index=True)
    summary=report.summarize(df);summary.to_csv(cell/'test_summary.csv',index=False)
    rng=np.random.default_rng(PROTOCOL['seed']);draws=rng.integers(0,80,(10000,80));comparisons=[]
    for (task,kind),g in df.groupby(['task','kind']):
        for before in ['behavior','raw_profile','shuffled']:
            gain,_=report.bootstrap_gain(g[g.method==before],g[g.method=='activation'],draws)
            comparisons.append(dict(task=task,kind=kind,comparison='activation_vs_'+before,**gain))
    c=pd.DataFrame(comparisons);c.to_csv(cell/'test_paired_comparisons.csv',index=False)
    report.ROOT=cell;report.study_robustness(df)
    report.family_comparisons(df,draws).to_csv(cell/'test_family_comparisons.csv',index=False)
    passed=all(d['training_numerically_stable'] and d['quadrature']['passed'] and d['withheld_target_error']==0 for d in audits)
    save_json(cell/'test_summary_status.json',dict(all_checks_passed=passed,prediction_rows=len(df)))
    print(summary[summary.task.isin(['reports_to_decisions','decisions_to_reports'])][['method','task','kind','mean_nll','mean_c_index','r2_vs_test_constant']].to_string(index=False))
    print(c[(c.comparison=='activation_vs_behavior')&c.task.isin(['reports_to_decisions','decisions_to_reports'])].to_string(index=False))
    print('ALL CHECKS PASSED',passed)

if __name__=='__main__':main()
