"""Summarize the locked calibration follow-up and paired case uncertainty."""
import itertools
import html
import json
import time
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from experiments.belief_calibration.run import ROOT, ORIGINAL, SIZES, CURVES, METHODS, ARMS, PROTOCOL, verify_inputs
from experiments.joint_beliefs.report import summarize, bootstrap_gain, render_table, family, metrics
from experiments.joint_beliefs.run import save_json, digest

PRIMARY=['reports_to_decisions','decisions_to_reports']


def main():
    verify_inputs()
    lock=json.loads((ROOT/'selection_lock.json').read_text())
    for path,expected in lock['selections'].items(): assert digest(ROOT/path)==expected
    frames=[];checks=[];selections=[]
    for size,curve,method in itertools.product(SIZES,CURVES,METHODS):
        cell=ROOT/size/curve/method
        complete=json.loads((cell/'complete.json').read_text())
        frames.append(pd.read_csv(cell/'test_predictions.csv',float_precision='round_trip'))
        for row in complete['checks']:
            checks.append(dict(size=size,curve=curve,method=method,arm=row['arm'],
                               epsilon=row['epsilon'],temperature=row['temperature'],
                               evaluation_points=row['evaluation_points'],passed=row['passed'],
                               original_identity_max_error=complete['original_identity_max_error']))
        selection=json.loads((cell/'selection.json').read_text())
        for arm,params in selection['arms'].items():
            selections.append(dict(size=size,curve=curve,method=method,arm=arm,**params,
                                   validation_points=selection['validation_points']))
    predictions=pd.concat(frames,ignore_index=True)
    assert len(predictions)==68*103*46
    assert not predictions.duplicated(['size','curve','method','arm','task','target','case_id']).any()
    assert np.isfinite(predictions[['actual','prediction','nll']]).all().all()
    predictions.to_csv(ROOT/'test_predictions.csv',index=False)
    summary=pd.concat([summarize(g).assign(arm=arm) for arm,g in predictions.groupby('arm')],ignore_index=True)
    summary.to_csv(ROOT/'summary.csv',index=False)
    checks=pd.DataFrame(checks);checks.to_csv(ROOT/'numerical_checks.csv',index=False)
    selections=pd.DataFrame(selections);selections.to_csv(ROOT/'selected_calibration.csv',index=False)
    group=['size','curve','method','arm','task','kind','target']
    targets=pd.DataFrame([dict(zip(group,key),**metrics(g)) for key,g in predictions.groupby(group)])
    targets.to_csv(ROOT/'metrics_by_target.csv',index=False)
    draws=np.random.default_rng(PROTOCOL['seed']).integers(0,103,size=(10000,103))
    comparisons=[];families=[];sparsity=[]
    rates=pd.read_csv(ORIGINAL/'decision_base_rates.csv')
    rates.to_csv(ROOT/'response_counts.csv',index=False)
    for size,curve in itertools.product(SIZES,CURVES):
        sub=predictions[(predictions['size']==size)&(predictions.curve==curve)&predictions.task.isin(PRIMARY)]
        pairs=[]
        for method in METHODS:
            pairs += [(method,'original',method,arm) for arm in ARMS if arm!='original']
        for arm in ARMS:
            pairs += [('behavior',arm,'activation',arm),('behavior',arm,'raw_profile',arm)]
        pairs += [('activation','original_no_evidence','activation','original'),
                  ('activation','combined_no_evidence','activation','combined')]
        for bm,ba,am,aa in pairs:
            before=sub[(sub.method==bm)&(sub.arm==ba)]
            after=sub[(sub.method==am)&(sub.arm==aa)]
            for (task,kind),g in before.groupby(['task','kind']):
                h=after[(after.task==task)&(after.kind==kind)]
                values,_=bootstrap_gain(g,h,draws)
                comparisons.append(dict(size=size,curve=curve,before_method=bm,before_arm=ba,
                                        after_method=am,after_arm=aa,task=task,kind=kind,**values))
        no_counts=rates[rates['size']==size].set_index('target')
        constant=set(no_counts.index[(no_counts.train_negatives+no_counts.val_negatives).isin([0,397])])
        for method,arm in itertools.product(METHODS,['smoothing','combined','fixed_floor']):
            before=sub[(sub.method==method)&(sub.arm=='original')&(sub.task=='reports_to_decisions')].copy()
            after=sub[(sub.method==method)&(sub.arm==arm)&(sub.task=='reports_to_decisions')].copy()
            before['family']=before.target.map(family);after['family']=after.target.map(family)
            for f,g in before.groupby('family'):
                values,_=bootstrap_gain(g,after[after.family==f],draws)
                families.append(dict(size=size,curve=curve,method=method,arm=arm,family=f,**values))
            for label,targets_in_group in [('constant_in_fit',constant),('variable_in_fit',set(before.target)-constant)]:
                if not targets_in_group:continue
                g=before[before.target.isin(targets_in_group)];h=after[after.target.isin(targets_in_group)]
                values,_=bootstrap_gain(g,h,draws)
                sparsity.append(dict(size=size,curve=curve,method=method,arm=arm,stratum=label,
                                     n_targets=len(targets_in_group),**values))
    comparisons=pd.DataFrame(comparisons);comparisons.to_csv(ROOT/'paired_comparisons.csv',index=False)
    families=pd.DataFrame(families);families.to_csv(ROOT/'decision_family_changes.csv',index=False)
    sparsity=pd.DataFrame(sparsity);sparsity.to_csv(ROOT/'sparsity_changes.csv',index=False)
    fig,axes=plt.subplots(2,2,figsize=(11,8),layout='constrained')
    for row,curve in enumerate(CURVES):
        for col,size in enumerate(SIZES):
            ax=axes[row,col]
            for method,color in zip(METHODS,['#667085','#087f8c','#c27818']):
                points=summary[(summary['size']==size)&(summary.curve==curve)&(summary.method==method)&
                               (summary.task=='reports_to_decisions')].set_index('arm').loc[ARMS]
                label={'behavior':'Behavior only','activation':'Behavior + activations',
                       'raw_profile':'Behavior + patient features'}[method]
                ax.plot(range(len(ARMS)),points.mean_nll,marker='o',label=label,color=color)
            ax.set(xticks=range(len(ARMS)),xticklabels=['Original','Smoothing','Tempering','Combined','Fixed floor'],
                   title=f'{size.upper()}: {"original curves" if curve=="linear" else "spline curves"}',ylabel='Decision log loss (lower is better)')
            ax.tick_params(axis='x',rotation=20);ax.grid(alpha=.15)
    axes[0,0].legend(fontsize=9)
    fig.savefig(ROOT/'decision_calibration.png',dpi=170);fig.savefig(ROOT/'decision_calibration.pdf');plt.close(fig)
    primary=summary[summary.task.isin(PRIMARY)&summary.arm.isin(ARMS)]
    correction=comparisons[(comparisons.before_method==comparisons.after_method)&(comparisons.before_arm=='original')]
    coherence=comparisons[(comparisons.before_method=='behavior')&(comparisons.after_method=='activation')&
                          comparisons.after_arm.isin(['original','combined'])]
    all_passed=bool(checks.passed.all())
    findings=[]
    for curve in CURVES:
        row=correction[(correction['size']=='4b')&(correction.curve==curve)&
                       (correction.after_method=='activation')&(correction.after_arm=='combined')&
                       (correction.task=='reports_to_decisions')].iloc[0]
        findings.append(f'4B, {"original" if curve=="linear" else "spline"} curves: the combined correction '
                        f'improves activation-model decision log loss by {row.nll_gain:.5f} '
                        f'(95% paired interval {row.nll_ci_low:.5f} to {row.nll_ci_high:.5f}).')
    findings_html=''.join('<li>'+html.escape(f)+'</li>' for f in findings)
    body=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Belief-model calibration follow-up</title>
<style>body{{font:16px/1.6 system-ui;max-width:1300px;margin:36px auto;padding:0 24px;color:#203040}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}td,th{{padding:7px;border-bottom:1px solid #ddd}}.scroll{{overflow-x:auto}}img{{max-width:100%}}.note{{background:#eef5f5;padding:16px}}</style></head><body>
<h1>Does better calibration change the belief-model comparison?</h1>
<p>4B and 12B · original and spline curves · behavior, activation, and patient-feature models · same 103 test cases.</p>
<div class="note">Numerical checks: {'all passed' if all_passed else 'flags present—inspect numerical_checks.csv'}. Every unmodified model reproduces its saved original predictions at the original integration resolution. Calibration choices were locked before this follow-up scored test cases.</div>
<h2>Readout of this run</h2><ul>{findings_html}</ul>
<p>After the selected combined correction, the activation-versus-behavior decision-gain intervals include zero for both sizes and both curve forms. The previous decision-score deterioration is reduced, while the substantial numerical-report advantage over behavior alone remains. This supports calibration as a contributor rather than establishing additional decision-predictive value or identifying a belief mechanism.</p>
<p>Most of 4B's smoothing-only mean decision gain comes from the three channels constant in the fitting data; its overall improvement intervals remain wide and include zero. The selected 4B spline combined correction instead uses half-strength evidence without smoothing and improves the variable fitting channels as well. Thus rare-response handling and excessive evidence influence are distinguishable contributors in these diagnostics. The 12B calibration changes are smaller and their primary NLL improvement intervals include zero.</p>
<h2>What changed?</h2>
<p>The scalar, response curves, layers, and probe penalties remain fixed. Smoothing mixes each binary response likelihood with a fair coin: (1−ε)p + ε/2. This changes both evidence and target probabilities while preserving monotonicity and normalization. Probability and ordinal likelihoods are unchanged. Tempering multiplies observed log likelihoods by τ and is a generalized posterior, not the original generative model.</p>
<p>Smoothing alone, tempering alone, and their combination were selected separately on 84 validation cases using the equally weighted mean of binary, probability, and ordinal NLL in the two reciprocal tasks. The combined search can select zero smoothing or full evidence weight; it need not use both corrections. One parameter pair applies to every output and task of each model. Original models used 313 training cases for this selection; the chosen calibration was then applied to their existing 397-case final fits. The fixed-floor comparator uses ε=1/(n_fit+1), an untuned half-count shrinkage heuristic, not a full Bayesian refit. Its test floor is 0.5/398.</p>
<p><strong>This is exploratory.</strong> The inspected original test results motivated the follow-up, and validation cases already selected the original models. Bootstrap intervals condition on fitted/calibrated models and are not adjusted for multiple comparisons. No fresh-case confirmation or new LLM inference was performed.</p>
<img src="decision_calibration.png" alt="Decision log loss for original, smoothed, tempered and combined predictions">
<h2>Primary scores</h2><p>Reports → decisions conditions on three reports; decisions → reports conditions on thirteen decisions. Probability NLL is a density score and may be negative. Compare scores within an output kind. All scores predict language-model responses, not clinical outcomes.</p>
<div class="scroll">{render_table(primary,['size','curve','method','arm','task','kind','mean_nll','mean_c_index','r2_vs_test_constant'])}</div>
<h2>Do the corrections help?</h2><p>Positive gains favor the corrected model. Intervals use 10,000 paired patient resamples.</p>
<div class="scroll">{render_table(correction,['size','curve','after_method','after_arm','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<h2>Does the activation-versus-behavior comparison change?</h2>
<div class="scroll">{render_table(coherence,['size','curve','after_arm','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<p>Patient features are an information benchmark: matching them is compatible with coherent evidence-based beliefs. Neither outperforming them nor a calibration gain establishes an internal causal belief mechanism. Broad predictive adequacy across responses remains the substantive question.</p>
<h2>Are gains concentrated in channels with no fitting variation?</h2><p>Constant channels had no negative or no positive answers in the 397 fitting cases. This grouping uses fitting responses, not test outcomes. Improvements here can reflect handling of rare exceptions rather than better shared belief structure.</p>
<div class="scroll">{render_table(sparsity,['size','curve','method','arm','stratum','n_targets','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<details><summary>Selected calibration parameters (validation stage)</summary><div class="scroll">{render_table(selections,list(selections.columns))}</div></details>
<details><summary>All tasks and evidence-removal ablations</summary><div class="scroll">{render_table(summary,['size','curve','method','arm','task','kind','mean_nll','mse','r2_vs_test_constant'])}</div></details>
<p>A positive result supports testing calibration as a contributor. It does not distinguish all forms of likelihood misspecification, dependence, or an inadequate latent state. A null result for this frozen-model correction does not rule out retraining with response priors. Confirmation requires new cases after choosing the correction on development data.</p>
<h2>Artifacts</h2><p><a href="summary.csv">Scores</a> · <a href="paired_comparisons.csv">Paired contrasts</a> · <a href="decision_family_changes.csv">Decision families</a> · <a href="sparsity_changes.csv">Sparse-channel effects</a> · <a href="selected_calibration.csv">Calibration choices</a> · <a href="metrics_by_target.csv">Per-output metrics</a> · <a href="numerical_checks.csv">Checks and test parameters</a> · <a href="test_predictions.csv">Case predictions</a> · <a href="protocol.json">Protocol</a> · <a href="selection_lock.json">Selection lock</a> · <a href="../full_experiment/report.html">Original experiment</a></p></body></html>'''
    (ROOT/'report.html').write_text(body)
    (ROOT/'source/report.py').write_bytes(Path(__file__).read_bytes())
    save_json(ROOT/'report_summary.json',dict(prediction_rows=len(predictions),all_checks_passed=all_passed,
        evaluations=len(checks),elapsed_wall_seconds=time.time()-json.loads((ROOT/'started.json').read_text())['unix'],
        findings=findings))
    print(ROOT/'report.html',flush=True)
    print('All checks passed:',all_passed,flush=True)


if __name__=='__main__':main()
