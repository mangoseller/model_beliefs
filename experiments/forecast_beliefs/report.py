"""Summarize the locked experiment with paired case uncertainty and figures."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from experiments.forecast_beliefs.run import ROOT, SIZES, CURVES, METHODS, PROTOCOL, save_json, frame
from latent_beliefs.latent.model import concordance_index

ALL_METHODS = METHODS + ('activation_only',)
LABELS = {'behavior': 'Behavior only', 'activation': 'Behavior + activation',
          'raw_profile': 'Behavior + study-text features', 'shuffled': 'Shuffled activation',
          'activation_only': 'Activation only (ablation)'}
GROUP = ['size', 'curve', 'method', 'task', 'kind']


def metrics(group):
    y = group.actual.to_numpy()
    p = group.prediction.to_numpy()
    error = (y-p)**2
    centered = group.actual - group.groupby('target').actual.transform('mean')
    denominator = float(np.sum(centered**2))
    train_error = float(np.sum((y-group.train_mean.to_numpy())**2))
    cindex = [concordance_index(g.actual, g.prediction)[0] for _,g in group.groupby('target')]
    return dict(n_cases=group.case_id.nunique(), n_targets=group.target.nunique(),
                mean_nll=float(group.nll.mean()), mse=float(error.mean()),
                rmse=float(np.sqrt(error.mean())),
                mean_c_index=float(np.nanmean(cindex)) if np.isfinite(cindex).any() else np.nan,
                n_ranked_targets=int(np.isfinite(cindex).sum()),
                r2_vs_test_constant=1-float(error.sum())/denominator if denominator else np.nan,
                r2_vs_train_mean=1-float(error.sum())/train_error if train_error else np.nan,
                mean_actual=float(y.mean()), mean_prediction=float(p.mean()),
                calibration_bias=float((p-y).mean()))


def summarize(predictions):
    return pd.DataFrame([dict(zip(GROUP,key), **metrics(group))
                         for key,group in predictions.groupby(GROUP, sort=False)])


def bootstrap_gain(a, b, draws):
    """Positive values favor b; tables have identical sorted case/target indices."""
    keys = ['case_id','target']
    a = a.sort_values(keys).set_index(keys)
    b = b.sort_values(keys).set_index(keys)
    if not a.index.equals(b.index):
        raise ValueError('Unpaired prediction tables')
    np.testing.assert_array_equal(a.actual.to_numpy(), b.actual.to_numpy())
    delta = (a.nll-b.nll).groupby('case_id').mean().to_numpy()
    mse_delta = ((a.actual-a.prediction)**2-(b.actual-b.prediction)**2).groupby('case_id').mean().to_numpy()
    samples = delta[draws].mean(1)
    mse_samples = mse_delta[draws].mean(1)
    return dict(nll_gain=float(delta.mean()), nll_ci_low=float(np.quantile(samples,.025)),
                nll_ci_high=float(np.quantile(samples,.975)),
                mse_gain=float(mse_delta.mean()), mse_ci_low=float(np.quantile(mse_samples,.025)),
                mse_ci_high=float(np.quantile(mse_samples,.975))), delta


def comparisons(predictions, draws):
    rows = []
    gains = {}
    for size in SIZES:
        for curve in CURVES:
            sub = predictions[(predictions['size']==size)&(predictions.curve==curve)]
            for before,after in [('behavior','activation'),('behavior','raw_profile'),
                                 ('raw_profile','activation'),('shuffled','activation'),
                                 ('activation_only','activation'),('behavior','activation_only')]:
                for (task,kind),group in sub.groupby(['task','kind']):
                    a,b = group[group.method==before],group[group.method==after]
                    result,delta = bootstrap_gain(a,b,draws)
                    rows.append(dict(size=size,curve=curve,comparison=after+'_vs_'+before,
                                     task=task,kind=kind,**result))
                    gains[(size,curve,before,after,task,kind)] = delta
        for before_curve,before,after_curve,after in [
            ('linear','behavior','monotone_spline','behavior'),
            ('linear','activation','monotone_spline','activation'),
            ('monotone_spline','behavior','linear','activation')]:
            sub = predictions[predictions['size']==size]
            for (task,kind),group in sub.groupby(['task','kind']):
                a = group[(group.curve==before_curve)&(group.method==before)]
                b = group[(group.curve==after_curve)&(group.method==after)]
                result,_ = bootstrap_gain(a,b,draws)
                rows.append(dict(size=size,curve=after_curve,
                                 comparison=after_curve+'_'+after+'_vs_'+before_curve+'_'+before,
                                 task=task,kind=kind,**result))
    size_rows=[]
    for key,small in gains.items():
        size,curve,before,after,task,kind=key
        if size!='4b' or before!='behavior' or after not in ['activation','raw_profile']:
            continue
        large=gains[('12b',curve,before,after,task,kind)]
        delta=small-large
        samples=delta[draws].mean(1)
        size_rows.append(dict(curve=curve,method=after,task=task,kind=kind,
                              gain_4b=float(small.mean()),gain_12b=float(large.mean()),
                              gain_4b_minus_12b=float(delta.mean()),
                              ci_low=float(np.quantile(samples,.025)),ci_high=float(np.quantile(samples,.975))))
    return pd.DataFrame(rows),pd.DataFrame(size_rows)


def calibration(predictions):
    rows=[]
    binary=predictions[predictions.kind=='binary'].copy()
    binary['bin']=np.minimum((binary.prediction*10).astype(int),9)
    for key,group in binary.groupby(GROUP+['bin']):
        rows.append(dict(zip(GROUP+['bin'],key),n=len(group),
                         mean_prediction=float(group.prediction.mean()),observed_rate=float(group.actual.mean())))
    return pd.DataFrame(rows)


def family(target):
    if target.startswith('bet_'):
        return 'betting'
    if 'cost_fn' in target:
        return 'cost'
    if 'threshold_' in target and target != 'experiment_priority_no_threshold':
        return 'threshold'
    if target == 'experiment_priority_no_threshold':
        return 'unparameterized_decision'
    return target


def family_comparisons(predictions, draws):
    rows=[]
    sub=predictions[(predictions.kind=='binary') & predictions.task.isin(['reports_to_decisions','leave_one_out'])].copy()
    sub['family']=sub.target.map(family)
    for (size,curve,task,name),group in sub.groupby(['size','curve','task','family']):
        for before in ['behavior','raw_profile']:
            result,_=bootstrap_gain(group[group.method==before],group[group.method=='activation'],draws)
            rows.append(dict(size=size,curve=curve,task=task,family=name,
                             comparison='activation_vs_'+before,**result))
    return pd.DataFrame(rows)


def family_figure(contrasts):
    fig,axes=plt.subplots(2,2,figsize=(10,7),layout='constrained')
    order=['threshold','cost','betting','unparameterized_decision']
    for row,curve in enumerate(CURVES):
        for col,size in enumerate(SIZES):
            ax=axes[row,col]
            sub=contrasts[(contrasts['size']==size)&(contrasts.curve==curve)&
                          (contrasts.task=='reports_to_decisions')&
                          (contrasts.comparison=='activation_vs_behavior')].set_index('family').loc[order]
            ax.errorbar(sub.nll_gain,np.arange(4),
                        xerr=np.maximum(0,np.vstack([sub.nll_gain-sub.nll_ci_low,sub.nll_ci_high-sub.nll_gain])),
                        fmt='o',color='#087f8c',capsize=3)
            ax.axvline(0,color='#667085',linestyle='--',linewidth=1)
            ax.set(yticks=np.arange(4),yticklabels=['Thresholds','Costs','Bets','Unparameterized'],
                   xlabel='Activation log-loss improvement (positive is better)',
                   title=f'{size.upper()}: {"original curves" if curve=="linear" else "spline curves"}')
            ax.invert_yaxis()
            ax.grid(axis='x',alpha=.15)
    fig.savefig(ROOT/'gains_by_family.png',dpi=180)
    fig.savefig(ROOT/'gains_by_family.pdf')
    plt.close(fig)


def make_figure(summary):
    fig,axes=plt.subplots(2,3,figsize=(14,8),layout='constrained')
    panels=[('reports_to_decisions','binary','mean_nll','Reports → decisions: log loss ↓'),
            ('reports_to_decisions','binary','mean_c_index','Reports → decisions: mean AUROC ↑'),
            ('decisions_to_reports','probability','r2_vs_test_constant','Decisions → probability: R² ↑')]
    colors={'behavior':'#667085','activation':'#087f8c','raw_profile':'#c27818','shuffled':'#b54f67'}
    for row,curve in enumerate(CURVES):
        for ax,(task,kind,metric,title) in zip(axes[row],panels):
            for method in METHODS:
                sub=summary[(summary.curve==curve)&(summary.method==method)&(summary.task==task)&(summary.kind==kind)].set_index('size').loc[list(SIZES)]
                ax.plot([4,12],sub[metric],marker='o',label=LABELS[method],color=colors[method])
            ax.set(title=title,xlabel='Gemma parameters (billions)',xticks=[4,12])
            ax.grid(alpha=.15)
        axes[row,0].set_ylabel('Original curves' if curve=='linear' else 'Monotone spline curves')
    axes[0,0].legend(fontsize=8)
    fig.savefig(ROOT/'comparison.png',dpi=180)
    fig.savefig(ROOT/'comparison.pdf')
    plt.close(fig)


def render_table(frame, columns, rename=None):
    return frame[columns].rename(columns=rename or {}).to_html(index=False,float_format=lambda x:f'{x:.4f}',border=0,classes='data')


def study_robustness(predictions):
    from experiments.forecast_beliefs.data import PROMPTS
    metadata = pd.DataFrame([json.loads(line) for line in PROMPTS.read_text().splitlines()])
    metadata['cluster'] = metadata.archive + ':' + metadata.study
    split = frame('4b',test=True)[['source_prompt_id','split']]
    metadata = metadata.merge(split,left_on='scenario_id',right_on='source_prompt_id',validate='one_to_one')
    fitting = set(metadata.loc[metadata.split!='test','cluster'])
    heldout = metadata.loc[metadata.split=='test']
    overlap = dict(total_studies=metadata.cluster.nunique(),
                   fit_studies=len(fitting),test_studies=heldout.cluster.nunique(),
                   test_scenarios=100,test_scenarios_with_study_in_fit=int(heldout.cluster.isin(fitting).sum()),
                   interpretation='Held-out scenarios, not a held-out-study experiment',
                   cluster_sizes=heldout.groupby('cluster').size().to_dict())
    save_json(ROOT/'study_overlap.json',overlap)
    cases = metadata.set_index('scenario_id').cluster
    rows=[]
    for (size,curve,task,kind),g in predictions.groupby(['size','curve','task','kind']):
        if task not in ['reports_to_decisions','decisions_to_reports']: continue
        for before in ['behavior','raw_profile','shuffled']:
            a=g[g.method==before].set_index(['case_id','target']).sort_index()
            b=g[g.method=='activation'].set_index(['case_id','target']).sort_index()
            assert a.index.equals(b.index)
            delta=(a.nll-b.nll).groupby('case_id').mean().rename('gain').to_frame()
            delta['cluster']=delta.index.map(cases)
            clusters=delta.groupby('cluster').gain.agg(['sum','count']).sort_index()
            rng=np.random.default_rng(PROTOCOL['seed'])
            draws=rng.integers(0,len(clusters),size=(10000,len(clusters)))
            samples=clusters['sum'].to_numpy()[draws].sum(1)/clusters['count'].to_numpy()[draws].sum(1)
            rows.append(dict(size=size,curve=curve,task=task,kind=kind,
                             comparison='activation_vs_'+before,n_clusters=len(clusters),
                             nll_gain=delta.gain.mean(),nll_ci_low=np.quantile(samples,.025),nll_ci_high=np.quantile(samples,.975)))
    pd.DataFrame(rows).to_csv(ROOT/'study_cluster_comparisons.csv',index=False)


def main():
    if not (ROOT/'selection_lock.json').exists():
        raise RuntimeError('Experiment selections were not locked')
    (ROOT/'source').mkdir(exist_ok=True)
    (ROOT/'source'/'report.py').write_bytes(Path(__file__).read_bytes())
    predictions=[]
    audit_rows=[]
    selection_rows=[]
    timing_rows=[]
    total_fit_seconds=0.
    for size in SIZES:
        for curve in CURVES:
            cell=ROOT/size/curve
            selection=json.loads((cell/'selection.json').read_text())
            for method in ALL_METHODS:
                result=json.loads((cell/'final'/f'{method}_complete.json').read_text())
                predictions.append(pd.read_csv(cell/'final'/f'{method}_predictions.csv', float_precision='round_trip'))
                audit_rows.append(dict(size=size,curve=curve,method=method,
                                       training_stable=result['training_numerically_stable'],
                                       evaluation_stable=result['quadrature']['passed'],
                                       evaluation_points=result['quadrature']['evaluation_points'],
                                       leakage_error=result['withheld_target_error']))
            for method,sel in selection.items():
                selection_rows.append(dict(size=size,curve=curve,method=method,layer=sel['layer'],
                                           penalty=sel['penalty'],quadrature=sel['quadrature'],
                                           best_epoch=sel.get('best_epoch_count',sel.get('best_epoch')),
                                           epochs_ran=sel['epochs_ran'],
                                           stopping=sel.get('stop_reason','epoch_cap' if sel['epochs_ran']==PROTOCOL['epochs'] else 'validation_patience'),
                                           validation_nll=sel.get('best_validation_nll',sel.get('best_validation_mean_nll')),
                                           training_stable=sel['training_numerically_stable']))
            for path in (cell/'candidates').glob('*/result.json'):
                timing=json.loads(path.read_text())
                total_fit_seconds+=timing['seconds']
                timing_rows.append(dict(size=size,curve=curve,stage='candidate',
                                        fit=path.parent.name,seconds=timing['seconds'],
                                        epochs=timing['epochs_ran']))
            for path in (cell/'final').glob('*_fit.json'):
                timing=json.loads(path.read_text())
                total_fit_seconds+=timing['seconds']
                timing_rows.append(dict(size=size,curve=curve,stage='final',
                                        fit=path.stem,seconds=timing['seconds'],epochs=timing['epochs']))
            timing=json.loads((cell/'baseline'/'result.json').read_text())
            total_fit_seconds+=timing['seconds']
            timing_rows.append(dict(size=size,curve=curve,stage='baseline',fit='behavior',
                                    seconds=timing['seconds'],epochs=timing['epochs_ran']))
    timings=pd.DataFrame(timing_rows)
    timings.to_csv(ROOT/'fit_timings.csv',index=False)
    candidate_timings=(timings[(timings.stage=='candidate')&
                               timings.fit.str.match(r'activation_l\d+_p')]
                       .groupby(['size','curve']).agg(fits=('seconds','size'),
                                                     median_seconds=('seconds','median'),
                                                     min_seconds=('seconds','min'),
                                                     max_seconds=('seconds','max')).reset_index())
    sleep_path=ROOT/'sleep_interruption.json'
    sleep_record=json.loads(sleep_path.read_text()) if sleep_path.exists() else None
    wall_seconds=time.time()-(ROOT/'protocol.json').stat().st_mtime
    sleep_note=(f"The power log records a {sleep_record['sleep_seconds']/60:.1f}-minute low-power sleep, "
                "plus earlier shorter sleeps. Wall time includes those interruptions. "
                if sleep_record else '')
    predictions=pd.concat(predictions,ignore_index=True)
    if not np.isfinite(predictions[['actual','prediction','nll']].to_numpy()).all():
        raise ValueError('Nonfinite prediction values')
    expected=20*100*46
    if len(predictions)!=expected:
        raise ValueError(f'Expected {expected} prediction rows, got {len(predictions)}')
    predictions.to_csv(ROOT/'test_predictions.csv',index=False)
    study_robustness(predictions)
    base_rates=(predictions[(predictions.curve=='linear')&(predictions.method=='behavior')&
                            (predictions.task=='leave_one_out')&(predictions.kind=='binary')]
                .groupby(['size','target']).agg(n_cases=('case_id','nunique'),
                                               positives=('actual','sum'),positive_rate=('actual','mean')).reset_index())
    split_counts=[]
    for size in SIZES:
        data=frame(size,test=True)
        for target in base_rates.loc[base_rates['size']==size,'target']:
            split_counts.append(dict(size=size,target=target,**{
                split+'_negatives':int((data.loc[data.split==split,target]==0).sum())
                for split in ['train','val','test']}))
    base_rates=base_rates.merge(pd.DataFrame(split_counts),on=['size','target'],validate='one_to_one')
    base_rates.to_csv(ROOT/'decision_base_rates.csv',index=False)
    summary=summarize(predictions)
    summary.to_csv(ROOT/'summary.csv',index=False)
    by_target=[]
    for key,group in predictions.groupby(GROUP+['target'],sort=False):
        by_target.append(dict(zip(GROUP+['target'],key),**metrics(group)))
    pd.DataFrame(by_target).to_csv(ROOT/'metrics_by_target.csv',index=False)
    rng=np.random.default_rng(PROTOCOL['seed'])
    draws=rng.integers(0,100,size=(10000,100))
    contrasts,size_comparison=comparisons(predictions,draws)
    contrasts.to_csv(ROOT/'paired_comparisons.csv',index=False)
    size_comparison.to_csv(ROOT/'size_comparison.csv',index=False)
    families=family_comparisons(predictions,draws)
    families.to_csv(ROOT/'comparisons_by_family.csv',index=False)
    family_figure(families)
    calibration(predictions).to_csv(ROOT/'calibration.csv',index=False)
    audits=pd.DataFrame(audit_rows)
    audits.to_csv(ROOT/'numerical_checks.csv',index=False)
    selections=pd.DataFrame(selection_rows)
    selections.to_csv(ROOT/'selected_configurations.csv',index=False)
    make_figure(summary)
    main_summary=summary[(summary.task.isin(['reports_to_decisions','decisions_to_reports']))&
                         summary.method.isin(['behavior','activation','raw_profile'])]
    primary_contrasts=contrasts[(contrasts.comparison.isin(['activation_vs_behavior','activation_vs_raw_profile']))&
                                contrasts.task.isin(['reports_to_decisions','decisions_to_reports'])]
    functional_contrasts=contrasts[(contrasts.comparison.isin([
        'monotone_spline_behavior_vs_linear_behavior',
        'monotone_spline_activation_vs_linear_activation',
        'linear_activation_vs_monotone_spline_behavior'])) &
        contrasts.task.isin(['reports_to_decisions','decisions_to_reports'])]
    size_primary=size_comparison[(size_comparison.method=='activation')&
                                 size_comparison.task.isin(['reports_to_decisions','decisions_to_reports'])]
    flags=audits[(~audits.training_stable)|(~audits.evaluation_stable)|(audits.leakage_error!=0)]
    status='All numerical and leakage checks passed.' if flags.empty else f'{len(flags)} model rows have numerical or leakage flags; inspect the checks table before interpretation.'
    # Describe evidence without treating a selected exploratory result as confirmation.
    findings=[]
    for size in SIZES:
        row=primary_contrasts[(primary_contrasts['size']==size)&(primary_contrasts.curve=='linear')&
                              (primary_contrasts.comparison=='activation_vs_behavior')&
                              (primary_contrasts.task=='reports_to_decisions')&
                              (primary_contrasts.kind=='binary')].iloc[0]
        findings.append(f'{size.upper()}, original curves: activation log-loss gain for reports → decisions '
                        f'{row.nll_gain:.4f} (95% paired case interval {row.nll_ci_low:.4f} to {row.nll_ci_high:.4f}).')
        probabilities=summary[(summary['size']==size)&(summary.curve=='linear')&
                              (summary.task=='decisions_to_reports')&(summary.kind=='probability')].set_index('method')
        findings.append(f'{size.upper()}, original curves: numerical-report R² from decisions '
                        f'{probabilities.loc["behavior","r2_vs_test_constant"]:.3f} with behavior alone, '
                        f'{probabilities.loc["activation","r2_vs_test_constant"]:.3f} with activations, and '
                        f'{probabilities.loc["raw_profile","r2_vs_test_constant"]:.3f} with study-text features.')
    findings_html=''.join('<li>'+html.escape(s)+'</li>' for s in findings)
    caveat=('This is an exploratory extension to a second domain chosen after the CKD results. Forecasting test scores were not used for model selection. Layer and penalty selection '
            'used validation cases only; each cell was locked before its test responses were scored. '
            'Each cell was locked before its test scoring began; 4B was evaluated while the already fixed 12B protocol continued. '
            'Bootstrap intervals resample 100 scenarios, keeping their responses and both model sizes paired. '
            'They condition on fitted models and do not include selection/training variability or correct for multiple comparisons. Scenarios from the same study can be dependent; the supplementary study-cluster intervals address within-study dependence but do not create held-out-study generalization. '
            'Local activations come from quantized checkpoints; behavioral responses came from hosted inference. '
            'All response families were available during training: this is held-out-case prediction, not zero-shot framing transfer.')
    body=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Joint belief experiment: 4B and 12B</title>
<style>body{{font:16px/1.6 system-ui;max-width:1300px;margin:36px auto;padding:0 24px;color:#172b3a}}h1,h2{{line-height:1.2}}img{{max-width:100%}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}td,th{{padding:7px 10px;border-bottom:1px solid #dce3e7;text-align:right}}th{{background:#f0f4f6}}.scroll{{overflow-x:auto}}.note{{padding:15px;background:#eef5f5;border-left:4px solid #087f8c}}a{{color:#087f8c}}</style></head><body>
<h1>Does one internally informed belief explain diverse responses?</h1>
<p>Gemma 3 4B and 12B · Scientific forecasting · 500 aligned cases · 320 training / 80 validation / 100 test · sixteen response types.</p>
<p>The targets are the language models' responses about study scenarios. These scores measure predictability of model behavior, not accuracy about real experimental outcomes.</p>
<div class="note"><strong>{html.escape(status)}</strong><ul>{findings_html}</ul></div>
<p>One shared activation direction, prior, and all response curves were fitted jointly. Original logistic/ordered-logit curves and richer monotone splines each have matched behavioral-only, activation, study-text, and shuffled controls. The activation-only row removes observed test responses from the same fitted activation model; it is not a separately trained probe.</p>
<img src="comparison.png" alt="Reciprocal prediction performance by model size, information source and response curves">
<h2>Primary reciprocal prediction results</h2>
<p>Reports → decisions uses probability, Likert, and probability-bin reports as evidence. Decisions → reports uses all thirteen binary decisions. Lower NLL/MSE is better; higher concordance and R² is better. Binary concordance equals AUROC; averages omit targets with no outcome variation. Probability-report NLL is a continuous density score and can be negative; compare it only within the same output kind. Ordinal means and MSE use categories 1–5. R² uses a separate test constant for each target before pooling squared errors.</p>
<div class="scroll">{render_table(main_summary,['size','curve','method','task','kind','mean_nll','mse','mean_c_index','r2_vs_test_constant'])}</div>
<h2>Does activation information add value?</h2>
<p>Positive gain means the activation model has lower NLL. Intervals crossing zero leave the direction uncertain. Study-text controls test the incremental predictive utility of reading activations rather than reconstructing a state from the evidence. Matching that control is compatible with an evidence-based internal belief; beating it is not a necessary condition for belief coherence and would not by itself identify a belief mechanism.</p>
<div class="scroll">{render_table(primary_contrasts,['size','curve','comparison','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<p>A small conditional activation gain does not establish an absence of internal belief information: observed answers can already carry overlapping information. The activation-only ablation removes observed test responses and helps assess what the internal readout predicts by itself; it is not separately trained.</p>
<h2>Does greater response-curve flexibility explain the gains?</h2>
<p>The spline-behavior versus linear-behavior comparison tests response-form restrictions without adding internals. Linear-activation versus spline-behavior asks whether activation information outperforms that behavioral-only flexibility control. Positive gain favors the first method named in each comparison.</p>
<div class="scroll">{render_table(functional_contrasts,['size','comparison','task','kind','nll_gain','nll_ci_low','nll_ci_high'])}</div>
<h2>Are gains spread across decision families?</h2>
<p>Each point compares activations plus reports with reports alone, on the same test study scenarios. Bars are 95% paired case-bootstrap intervals. This separates threshold, cost, betting, and unparameterized decisions so an overall gain cannot conceal a betting-only result.</p>
<img src="gains_by_family.png" alt="Activation log-loss improvement and uncertainty for four decision families">
<p>Some decisions may be nearly constant across study scenarios. Their class frequencies matter when interpreting average log loss and AUROC, and coarse decisions can contain little information about a numerical belief report. The table also counts negative responses in each split: an output with none in training or validation provides no fitted example of a rare test “no,” making confident predictions especially fragile.</p>
<details><summary>Show decision response frequencies</summary><div class="scroll">{render_table(base_rates,list(base_rates.columns))}</div></details>
<p>The unparameterized priority question asks which condition is expected to produce the largest positive effect. Expected effect size need not be determined by P(mean_A &gt; mean_B). An improvement on that family alone therefore cannot establish a probability-specific belief representation. The explicit thresholds, bets and asymmetric costs directly concern the focal event.</p>
<h2>Does the smaller model benefit more?</h2>
<p>Positive 4B-minus-12B gain favors the smaller-model-benefit hypothesis. The same scenario IDs are resampled for both sizes.</p>
<div class="scroll">{render_table(size_primary,['curve','task','kind','gain_4b','gain_12b','gain_4b_minus_12b','ci_low','ci_high'])}</div>
<h2>All prediction tasks and controls</h2>
<p>These tables include probability-only reciprocal tasks and leave-one-output-out predictions. Do not combine continuous, ordinal, and binary NLLs into an interpretation of effect size.</p>
<details><summary>Show all prediction tasks, controls, and the activation-only ablation</summary><div class="scroll">{render_table(summary,['size','curve','method','task','kind','mean_nll','mse','mean_c_index','r2_vs_test_constant','calibration_bias'])}</div></details>
<h2>Selection and numerical checks</h2>
<p>Each activation search covered eight layers and four penalties, with one initialization per candidate. Study-text features used the same four penalties. Shuffling used one matched layer/penalty fit and is a sanity check, not a permutation significance test. Final refits use train plus validation with epoch counts fixed by validation. Numerical audits may refine quadrature only, retaining parameters and prior knot positions.</p>
<details><summary>Show selected configurations and stopping diagnostics</summary><div class="scroll">{render_table(selections,list(selections.columns))}</div></details>
<details><summary>Show numerical checks for every model</summary><div class="scroll">{render_table(audits,list(audits.columns))}</div></details>
<h2>Measured fitting time</h2>
<p>Elapsed wall time to report generation: {wall_seconds/60:.1f} minutes. {html.escape(sleep_note)}Saved fit timers sum to {total_fit_seconds/60:.1f} worker-minutes; concurrent workers overlap, so this is not elapsed wall time. Timers exclude data preparation, reporting, and discarded interrupted work. The table describes the 32 completed activation-search candidates per cell, excluding numerical refits and final training.</p>
<div class="scroll">{render_table(candidate_timings,list(candidate_timings.columns))}</div>
<p><a href="fit_timings.csv">All saved fit timings</a></p>
<h2>Interpretation limits</h2><p>{html.escape(caveat)}</p>
<p>Evidence for shared belief structure requires broad predictive adequacy across response families, not only a better average or better betting predictions. A successful study-text model with the same shared scalar can itself support behavioral coherence, while leaving the internal representation unidentified. Comparing it with activations measures incremental predictive utility, not the existence of beliefs: a rational belief should depend on the study evidence. A successful spline-only control implicates response-model specification. Predictive success alone does not establish causal use of the learned direction.</p>
<h2>Study overlap and robustness</h2>
<p>The split holds out scenarios, not whole studies. See the <a href="study_overlap.json">study-overlap audit</a> and <a href="study_cluster_comparisons.csv">paired study-cluster intervals</a>. The text benchmark uses only visible study materials: TF-IDF unigrams/bigrams, at most 2,048 features, and the same fitted scalar shift. Vocabulary, IDF and normalization are fitted on training cases during selection and train plus validation for final fits. No gold outcomes or study identifiers enter the feature vector.</p>
<p>This is the original joint-likelihood method, without post hoc smoothing or evidence tempering. Calibration bins and per-output scores are included so overconfidence can be distinguished from discrimination. No inference about causal use or existence of a belief follows from a text-versus-activation comparison alone.</p>
<h2>Download results</h2><p><a href="summary.csv">Summary CSV</a> · <a href="metrics_by_target.csv">Per-output metrics</a> · <a href="paired_comparisons.csv">All paired comparisons</a> · <a href="comparisons_by_family.csv">Decision-family comparisons</a> · <a href="size_comparison.csv">Size comparison</a> · <a href="calibration.csv">Calibration bins</a> · <a href="test_predictions.csv">Case-level predictions</a> · <a href="numerical_checks.csv">Numerical checks</a> · <a href="selected_configurations.csv">Selected configurations</a> · <a href="comparison.pdf">Figure PDF</a> · <a href="protocol.json">Protocol</a> · <a href="selection_lock.json">Selection lock</a></p>
</body></html>'''
    (ROOT/'report.html').write_text(body)
    save_json(ROOT/'report_summary.json',dict(prediction_rows=len(predictions),
                                             all_checks_passed=flags.empty,flags=flags.to_dict('records'),
                                             summed_fit_seconds=total_fit_seconds,
                                             elapsed_wall_seconds=wall_seconds,
                                             documented_sleep=sleep_record,
                                             findings=findings))
    print('\n'.join(findings))
    print(status)
    print(ROOT/'report.html')


if __name__=='__main__':
    main()
