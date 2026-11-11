"""Paired model-size comparison on identical test patients and decision types."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path('build/activation_comparison')


def auc(y,p):
    ranks=rankdata(p,axis=0)
    positive=y.sum(0)
    denominator=positive*(len(y)-positive)
    values=(np.sum(ranks*y,axis=0)-positive*(positive+1)/2)/np.maximum(denominator,1)
    return float(np.mean(values[denominator>0]))


def summarize(values):
    return dict(mean=float(values[0]),ci95=np.quantile(values[1:],[.025,.975]).tolist())


def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(20261107)
    results={}
    records=[]
    specifications=[('flexible','flexible')]
    shared_checks=[json.loads((Path(p)/'results.json').read_text())['quadrature_128_vs_256_max_prediction_change']
                   for p in ['build/activation_pilot','build/activation_pilot_12b']]
    shared_excluded=max(shared_checks)>1e-3
    if not shared_excluded:specifications.insert(0,('shared_threshold',''))
    if all((Path(p)/'direct/results.json').exists() for p in ['build/activation_pilot','build/activation_pilot_12b']):
        specifications.append(('direct','direct'))
    for specification,suffix in specifications:
        sets=[]
        for size in ['4b','12b']:
            root=Path('build/activation_pilot' if size=='4b' else 'build/activation_pilot_12b')/suffix
            df=pd.read_csv(root/'test_predictions.csv')
            if specification=='direct':
                df=df.drop(columns=['augmented']).rename(columns={'internal':'augmented'})
            table={k:df.pivot(index='case_id',columns='target',values=k).sort_index()
                   for k in ['actual','behavior_only','augmented']}
            sets.append(table)
            metadata=json.loads((root/'results.json').read_text())
            for method in ['behavior_only','augmented','behavior_plus_raw_profile']:
                source={'augmented':'internal','behavior_plus_raw_profile':'raw_profile'}.get(method,method) if specification=='direct' else method
                records.append(dict(specification=specification,model=size,method=method,
                                    **metadata['metrics'][source]))
        assert sets[0]['actual'].index.equals(sets[1]['actual'].index)
        n=len(sets[0]['actual'])
        # Use identical draws across specifications; adding another comparison must
        # not change previously reported intervals through RNG consumption order.
        rng=np.random.default_rng(20261107)
        samples=np.vstack([np.arange(n),rng.integers(0,n,size=(10000,n))])
        gains=[]
        auc_gains=[]
        for table in sets:
            y=table['actual'].to_numpy()
            b=table['behavior_only'].to_numpy().clip(1e-10,1-1e-10)
            a=table['augmented'].to_numpy().clip(1e-10,1-1e-10)
            base_loss=-(y*np.log(b)+(1-y)*np.log1p(-b)).mean(1)
            aug_loss=-(y*np.log(a)+(1-y)*np.log1p(-a)).mean(1)
            gains.append((base_loss-aug_loss)[samples].mean(1))
            auc_gains.append(np.array([auc(y[ix],a[ix])-auc(y[ix],b[ix]) for ix in samples]))
        results[specification]=dict(
            n_cases=n,
            log_loss_gain_4b=summarize(gains[0]),log_loss_gain_12b=summarize(gains[1]),
            log_loss_gain_4b_minus_12b=summarize(gains[0]-gains[1]),
            auc_gain_4b=summarize(auc_gains[0]),auc_gain_12b=summarize(auc_gains[1]),
            auc_gain_4b_minus_12b=summarize(auc_gains[0]-auc_gains[1]))
    if shared_excluded:
        results['excluded_shared_threshold']=dict(reason='Numerical integration is unstable for 12B; do not interpret its comparative scores.',
                                                  max_probability_changes_128_to_256=shared_checks)
    (ROOT/'size_comparison.json').write_text(json.dumps(results,indent=2))
    metrics=pd.DataFrame(records)
    metrics.to_csv(ROOT/'metrics.csv',index=False)
    fig,axes=plt.subplots(1,2,figsize=(10,4.5),layout='constrained')
    for ax,metric,label in zip(axes,['log_loss','mean_auc'],['Test log loss (lower is better)','Mean test AUROC (higher is better)']):
        styles={'shared_threshold':'--','flexible':'-','direct':':'}
        for spec,_ in specifications:
            style=styles[spec]
            for method,color in [('behavior_only','#69768b'),('augmented','#148a7d')]:
                if spec=='direct' and method=='behavior_only':continue
                if spec=='direct':color='#b86c25'
                subset=metrics[(metrics.specification==spec)&(metrics.method==method)].set_index('model').loc[['4b','12b']]
                series_label='Direct internal scalar' if spec=='direct' else ('Behavior + activation' if method=='augmented' else 'Behavior only')
                if spec=='shared_threshold':series_label='Shared threshold: '+series_label
                ax.plot([4,12],subset[metric],linestyle=style,marker='o',color=color,
                        label=series_label)
        ax.set(xlabel='Gemma 3 parameters (billions)',ylabel=label,xticks=[4,12])
    axes[0].legend(fontsize=7)
    fig.savefig(ROOT/'model_size_comparison.png',dpi=180)
    fig.savefig(ROOT/'model_size_comparison.pdf')
    rows=''.join(f'<tr><td>{r.specification}</td><td>{r.model}</td><td>{r.method}</td><td>{r.log_loss:.4f}</td><td>{r.mean_auc:.4f}</td></tr>' for r in metrics.itertuples())
    (ROOT/'report.html').write_text(f'''<!doctype html><html><meta charset="utf-8"><title>Gemma activation comparison</title>
<style>body{{font:17px/1.6 system-ui;max-width:1050px;margin:40px auto;padding:0 24px;color:#203040}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}img{{max-width:100%}}pre{{white-space:pre-wrap}}</style>
<h1>Do smaller models benefit more from internal activations?</h1>
<p><strong>This pilot does not show the predicted smaller-model advantage. The 12B model benefits more in log loss, and the 4B model remains substantially less predictable after adding internals.</strong></p>
<p>Gemma 3 4B and 12B, evaluated on the same 103 CKD patients. Both use the same eight observed decisions to predict four betting decisions. Layers and regularization were selected on validation cases only.</p>
<p>When shown, the direct-probe comparison uses activations alone rather than combining them with the eight external decisions. Its rows labeled augmented mean the directly trained internal scalar. It is an exploratory follow-up chosen after inspecting the first 4B test results.</p>
<p><strong>The shared-threshold comparison is excluded when its quadrature check changes any predicted probability by more than 0.001. The 12B shared-threshold run failed this numerical check; its raw artifacts are retained for diagnosis.</strong></p>
<img src="model_size_comparison.png" alt="Behavior-only and augmented performance for two model sizes">
<table><tr><th>Likelihood</th><th>Model</th><th>Method</th><th>Log loss ↓</th><th>AUROC ↑</th></tr>{rows}</table>
<h2>Paired uncertainty</h2><p>Positive 4B-minus-12B gain means internals help the smaller model more. Intervals resample the same patient IDs for both models, retaining all four decisions. AUROC is a secondary metric. These intervals are conditional on the trained models and selected configurations.</p><pre>{json.dumps(results,indent=2)}</pre>
<h2>Interpretation limits</h2><p>Both local checkpoints are quantized to 4 bits; the reused responses were produced by hosted inference. This is not a verified matched-checkpoint experiment. Two model sizes, one domain, one split, and one activation prompt cannot establish a general scaling law or demonstrate beliefs. The raw-profile control helps distinguish internal representation gains from ordinary patient-feature information. The 12B shared-link baseline used a capped fitting budget; inspect its fitting diagnostics before treating small differences as decisive.</p>
<p>Details: <a href="../activation_pilot/flexible/report.html">4B flexible model</a>, <a href="../activation_pilot_12b/flexible/report.html">12B flexible model</a>, <a href="size_comparison.json">paired statistics</a>, <a href="metrics.csv">metrics CSV</a>.</p></html>''')
    print(json.dumps(results,indent=2))
    print(ROOT/'report.html')


if __name__=='__main__':main()
