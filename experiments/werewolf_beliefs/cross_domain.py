"""Compare the unchanged primary method across all three main domains."""
import html
import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path('build/cross_domain_beliefs')
SOURCES={
 'ckd':Path('build/joint_beliefs/full_experiment'),
 'forecasting':Path('build/forecast_beliefs/full_experiment'),
 'werewolf':Path('build/werewolf_beliefs/full_experiment'),
}
LABELS={'ckd':'CKD','forecasting':'Scientific forecasting','werewolf':'Werewolf'}

def assemble():
    rows=[];checks=[]
    for domain,folder in SOURCES.items():
        summary=pd.read_csv(folder/'summary.csv')
        contrasts=pd.read_csv(folder/'paired_comparisons.csv')
        audit=pd.read_csv(folder/'numerical_checks.csv')
        checks.append(dict(domain=domain,model_rows=len(audit),all_passed=bool(audit.training_stable.all() and audit.evaluation_stable.all() and (audit.leakage_error==0).all())))
        for size in ['4b','12b']:
            for curve in ['linear','monotone_spline']:
                cell=summary[(summary['size']==size)&(summary.curve==curve)]
                decisions=cell[(cell.task=='reports_to_decisions')&(cell.kind=='binary')].set_index('method')
                probability=cell[(cell.task=='decisions_to_reports')&(cell.kind=='probability')].set_index('method')
                delta=contrasts[(contrasts['size']==size)&(contrasts.curve==curve)&(contrasts.task=='reports_to_decisions')&(contrasts.kind=='binary')&(contrasts.comparison=='activation_vs_behavior')]
                assert len(delta)==1
                delta=delta.iloc[0]
                assert np.isclose(delta.nll_gain,decisions.loc['behavior','mean_nll']-decisions.loc['activation','mean_nll'])
                assert decisions.n_cases.nunique()==1 and probability.n_cases.nunique()==1
                rows.append(dict(domain=domain,size=size,curve=curve,n_test=int(decisions.loc['behavior','n_cases']),
                    behavior_decision_nll=decisions.loc['behavior','mean_nll'],activation_decision_nll=decisions.loc['activation','mean_nll'],input_decision_nll=decisions.loc['raw_profile','mean_nll'],
                    decision_nll_gain=delta.nll_gain,decision_ci_low=delta.nll_ci_low,decision_ci_high=delta.nll_ci_high,
                    behavior_probability_r2=probability.loc['behavior','r2_vs_test_constant'],activation_probability_r2=probability.loc['activation','r2_vs_test_constant'],
                    input_probability_r2=probability.loc['raw_profile','r2_vs_test_constant'],activation_only_probability_r2=probability.loc['activation_only','r2_vs_test_constant']))
    return pd.DataFrame(rows),pd.DataFrame(checks)

def plot(data):
    domains=list(SOURCES);colors={'4b':'#087f8c','12b':'#b46720'}
    fig,axes=plt.subplots(2,2,figsize=(12,8),layout='constrained')
    for row,curve in enumerate(['linear','monotone_spline']):
        for offset,size in [(-.12,'4b'),(.12,'12b')]:
            sub=data[(data['size']==size)&(data.curve==curve)].set_index('domain').loc[domains]
            axes[row,0].errorbar(np.arange(3)+offset,sub.decision_nll_gain,yerr=np.maximum(0,np.vstack([sub.decision_nll_gain-sub.decision_ci_low,sub.decision_ci_high-sub.decision_nll_gain])),fmt='o',capsize=4,color=colors[size],label=size.upper())
            axes[row,1].plot(np.arange(3),sub.activation_probability_r2,'o-',color=colors[size],label=size.upper()+' + observed decisions')
            axes[row,1].plot(np.arange(3),sub.activation_only_probability_r2,'o--',color=colors[size],alpha=.7,label=size.upper()+' activations only')
        axes[row,0].axhline(0,color='#777',ls='--',lw=1)
        axes[row,0].set_title('Activation improvement in decision log loss')
        axes[row,1].set_title('Numerical probability-report R²')
        for ax in axes[row]:
            ax.set_xticks(np.arange(3),[LABELS[d] for d in domains]);ax.grid(axis='y',alpha=.2)
        axes[row,0].set_ylabel('Original curves' if curve=='linear' else 'Monotone splines')
    axes[0,0].legend(fontsize=9);axes[0,1].legend(fontsize=8)
    fig.savefig(ROOT/'comparison.png',dpi=180);fig.savefig(ROOT/'comparison.pdf');plt.close(fig)

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    data,checks=assemble();data.to_csv(ROOT/'comparison.csv',index=False);checks.to_csv(ROOT/'checks.csv',index=False)
    assert len(data)==12
    plot(data)
    main=data[data.curve=='linear'].copy()
    main['decision_gain_95ci']=[f'{g:.3f} [{lo:.3f}, {hi:.3f}]' for g,lo,hi in zip(main.decision_nll_gain,main.decision_ci_low,main.decision_ci_high)]
    columns=['domain','size','n_test','decision_gain_95ci','behavior_probability_r2','activation_probability_r2','activation_only_probability_r2']
    table=main[columns].to_html(index=False,float_format=lambda v:f'{v:.3f}',border=0)
    links=' · '.join(f'<a href="../{folder.relative_to("build")}/report.html">{html.escape(LABELS[d])} full report</a>' for d,folder in SOURCES.items())
    body=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Shared-belief models across three domains</title><style>body{{font:16px/1.6 system-ui;max-width:1250px;margin:40px auto;padding:0 20px}}table{{border-collapse:collapse;font-size:13px}}td,th{{padding:8px;border-bottom:1px solid #ddd}}img{{max-width:100%}}.scroll{{overflow:auto}}</style></head><body><h1>Shared-belief models across all three main domains</h1><p>Gemma 3 4B and 12B · CKD, scientific forecasting and Werewolf · 16 response types per domain · matched held-out cases within each domain.</p><p>{links}</p><p>Each experiment fits one scalar shift from activations jointly with the shared latent distribution and response curves. The primary comparison adds activations to observed reports when predicting decisions. The reverse task predicts numerical probability reports from decisions. Activation-only uses the same jointly fitted model with no observed test answers.</p><img src="comparison.png" alt="Decision improvements and probability-report prediction across three domains"><h2>Original-curve results</h2><p>Positive decision log-loss gain favors activations. Brackets are 95% paired case-bootstrap intervals conditional on fitted models. R² is relative to the test mean. The spline comparison and all numerical values are available in the CSV and figure.</p><div class="scroll">{table}</div><h2>How to interpret this comparison</h2><p>Useful prediction from a shared scalar supports predictive structure, but does not establish causal use or uniqueness of an internal belief variable. Little incremental gain can reflect redundant information in observed answers; the activation-only result helps distinguish that possibility from a weak internal readout. Input-feature parity is compatible with evidence-based beliefs.</p><p>The CKD rows use the original uncalibrated joint-likelihood experiment, matching the primary likelihood used for the other domains. The later CKD calibration follow-up is not mixed into these comparisons. Input benchmarks differ by domain: structured patient features for CKD and training-fitted TF-IDF of visible study/game materials for forecasting and Werewolf.</p><p>Holdouts differ across domains: CKD has 103 patients, forecasting 100 scenarios, and Werewolf 80 games. Forecasting scenarios can share studies across partitions; its full report includes study-cluster intervals. Werewolf retains 405 cases complete in both sizes out of 500 original cases, and all retained games are distinct. Complete-case filtering can select an easier subset. Do not treat raw NLL levels as interchangeable domain difficulty scores or pool the intervals as an independent confirmation.</p><p>Local activations use quantized checkpoints while the cached answers came from hosted inference. The readout is taken at the end of a neutral prompt before generated reasoning; the elicited answers follow a reasoning request. A weak pre-response readout does not rule out a belief state formed later during reasoning. Each cell is locked before test scoring, and later searches retain the fixed protocol while completed results are reported. The three-domain extension is exploratory, with no correction for multiple comparisons.</p><h2>Verification and downloads</h2><p>All source-report numerical and leakage checks passed: {bool(checks.all_passed.all())}.</p><p><a href="comparison.csv">All metrics</a> · <a href="checks.csv">Verification</a> · <a href="comparison.pdf">Figure PDF</a></p></body></html>'''
    (ROOT/'report.html').write_text(body)
    (ROOT/'summary.json').write_text(json.dumps(dict(domains=list(SOURCES),cells=len(data),all_checks_passed=bool(checks.all_passed.all()),sources={d:str(p) for d,p in SOURCES.items()}),indent=2))
    print(main[columns].to_string(index=False));print(ROOT/'report.html')

if __name__=='__main__':main()
