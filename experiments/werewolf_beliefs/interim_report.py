"""Held-out results for one completed size, with paired scenario uncertainty."""
import argparse,json,html
from pathlib import Path
import numpy as np
import pandas as pd
from experiments.werewolf_beliefs import report
from experiments.werewolf_beliefs.run import ROOT,CURVES,METHODS,PROTOCOL,save_json

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--size',choices=['4b','12b'],default='4b');args=parser.parse_args()
    rows=[];audits=[]
    for curve in CURVES:
        for method in METHODS+('activation_only',):
            folder=ROOT/args.size/curve/'final'
            audit=json.loads((folder/f'{method}_complete.json').read_text())
            audits.append(dict(curve=curve,method=method,training_stable=audit['training_numerically_stable'],evaluation_stable=audit['quadrature']['passed'],leakage_error=audit['withheld_target_error']))
            rows.append(pd.read_csv(folder/f'{method}_predictions.csv',float_precision='round_trip'))
    predictions=pd.concat(rows,ignore_index=True)
    assert len(predictions)==10*80*46
    out=ROOT/args.size
    summary=report.summarize(predictions)
    summary.to_csv(out/'test_summary.csv',index=False)
    rng=np.random.default_rng(PROTOCOL['seed']);draws=rng.integers(0,80,(10000,80))
    contrasts=[]
    for (curve,task,kind),group in predictions.groupby(['curve','task','kind']):
        for before in ['behavior','raw_profile','shuffled']:
            result,_=report.bootstrap_gain(group[group.method==before],group[group.method=='activation'],draws)
            contrasts.append(dict(curve=curve,task=task,kind=kind,comparison='activation_vs_'+before,**result))
    contrasts=pd.DataFrame(contrasts)
    contrasts.to_csv(out/'test_paired_comparisons.csv',index=False)
    audit_frame=pd.DataFrame(audits);audit_frame.to_csv(out/'test_numerical_checks.csv',index=False)
    # Cluster audit and sensitivity use the same locked predictions.
    report.ROOT=out
    report.study_robustness(predictions)
    families=report.family_comparisons(predictions,draws)
    families.to_csv(out/'test_family_comparisons.csv',index=False)
    primary=summary[summary.task.isin(['reports_to_decisions','decisions_to_reports'])&summary.method.isin(['behavior','activation','raw_profile'])]
    comparisons=contrasts[contrasts.task.isin(['reports_to_decisions','decisions_to_reports'])]
    checks=bool(audit_frame.training_stable.all() and audit_frame.evaluation_stable.all() and (audit_frame.leakage_error==0).all())
    body=f'''<!doctype html><html><head><meta charset="utf-8"><title>Werewolf {args.size}: held-out results</title><style>body{{font:16px/1.6 system-ui;max-width:1250px;margin:40px auto;padding:0 20px}}table{{border-collapse:collapse;font-size:13px}}td,th{{padding:7px;border-bottom:1px solid #ddd}}.scroll{{overflow:auto}}</style></head><body><h1>Werewolf: {args.size.upper()}</h1><p>80 held-out game states; all selections locked before evaluation. These are test results. The earlier validation diagnostics used different cases and models fitted only on training data.</p><p>All numerical and leakage checks passed: {checks}. Each held-out case comes from a distinct game absent from fitting cases.</p><p>Targets are the model's responses about whether the target player is a Werewolf, across 16 framings. Scores measure response predictability, not accuracy about the target’s hidden role.</p><h2>Primary predictions</h2><p>Reports → decisions conditions on three reports to predict thirteen decisions. Decisions → reports conditions on thirteen decisions to predict probability and ordinal reports. raw_profile denotes the game-text TF-IDF benchmark.</p><div class="scroll">{report.render_table(primary,['curve','method','task','kind','mean_nll','mse','mean_c_index','r2_vs_test_constant','r2_vs_train_mean'])}</div><h2>Paired comparisons</h2><p>Positive NLL gain favors activations; intervals are paired scenario-bootstrap intervals conditional on fitted models.</p><div class="scroll">{report.render_table(comparisons,['curve','comparison','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div><p><a href="game_cluster_comparisons.csv">Game-cluster intervals</a> · <a href="test_family_comparisons.csv">Decision-family comparisons</a> · <a href="test_summary.csv">All metrics</a> · <a href="test_numerical_checks.csv">Numerical checks</a></p><p>Game-text parity is compatible with evidence-based beliefs. Predictive performance alone does not establish causal use or absence of a belief representation. The unthresholded elimination decision can also depend on strategic consequences.</p></body></html>'''
    (out/'interim_report.html').write_text(body)
    save_json(out/'interim_report_summary.json',dict(prediction_rows=len(predictions),all_checks_passed=checks))
    print(primary[['curve','method','task','kind','mean_nll','r2_vs_test_constant']].to_string(index=False))
    print(comparisons[comparisons.comparison=='activation_vs_behavior'][['curve','task','kind','nll_gain','nll_ci_low','nll_ci_high']].to_string(index=False))
    print('ALL CHECKS PASSED:',checks)
    print(out/'interim_report.html')

if __name__=='__main__':main()
