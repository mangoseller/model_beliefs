"""Generate a compact figure and a standalone HTML result report."""
import html
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

OUT = Path('build/activation_pilot')


def main():
    global OUT
    parser=argparse.ArgumentParser()
    parser.add_argument('--flexible',action='store_true')
    parser.add_argument('--larger',action='store_true')
    args=parser.parse_args()
    if args.larger: OUT=Path('build/activation_pilot_12b')
    if args.flexible: OUT=OUT/'flexible'
    r = json.loads((OUT/'results.json').read_text())
    model_name='Gemma 3 12B' if args.larger else 'Gemma 3 4B'
    released_note = ('The released flexible model has separate response curves and more output types in fitting, so it is a contextual comparator rather than the nested baseline.'
                     if not args.larger else 'The 12B baseline is refit on the 4B case split. Released 12B fits are excluded because their original training cases overlap this aligned test set.')
    baseline_name='flexible link' if args.flexible else 'shared link'
    search = pd.read_csv(OUT/'layer_search.csv')
    names = {'behavior_only':f'Behavior only ({baseline_name})', 'augmented':'Behavior + activation',
             'activation_only':'Activation only', 'shuffled':'Behavior + shuffled activation',
             'behavior_plus_raw_profile':'Behavior + raw patient features',
             'paper_flexible_released':'Paper released flexible model'}
    fig, axes = plt.subplots(1,2,figsize=(12,4.7),layout='constrained')
    for penalty,g in search.groupby('penalty'):
        axes[0].plot(g.layer,g.validation_log_loss,marker='o',label=f'L2 = {penalty:g}')
    axes[0].set(xlabel='Post-block layer (1-based)',ylabel='Validation log loss (lower is better)',
                title='Layer selection: validation cases only')
    axes[0].legend(fontsize=8)
    labels = [names[k] for k in r['metrics']]
    values = [v['log_loss'] for v in r['metrics'].values()]
    colors = ['#16877d' if k=='augmented' else '#9ba8bb' for k in r['metrics']]
    axes[1].barh(labels,values,color=colors)
    axes[1].invert_yaxis()
    axes[1].set(xlabel='Test log loss (lower is better)',title=f"Untouched test cases (n={r['n_test']})")
    for i,v in enumerate(values): axes[1].text(v+.005,i,f'{v:.3f}',va='center',fontsize=9)
    axes[1].set_xlim(0,max(values)*1.15)
    fig.savefig(OUT/'results.png',dpi=180)
    fig.savefig(OUT/'results.pdf')
    delta = r['paired_log_loss_improvement']
    lo,hi=delta['ci95']
    secondary_note=''
    if (OUT/'secondary_auc_bootstrap.json').exists():
        secondary=json.loads((OUT/'secondary_auc_bootstrap.json').read_text())
        alo,ahi=secondary['ci95']
        secondary_note=f'<p>Secondary metric: paired mean AUROC gain {secondary["auc_gain"]:.4f}, with 95% case-bootstrap interval [{alo:.4f}, {ahi:.4f}]. This ranking result is distinct from the primary log-loss result.</p>'
    conclusion = (f'The activation shift improves held-out prediction relative to the {baseline_name} baseline.'
                  if lo>0 else f'This pilot does not establish an improvement over the {baseline_name} baseline.'
                  if hi>=0 else f'The activation shift worsens held-out prediction relative to the {baseline_name} baseline.')
    if r['quadrature_128_vs_256_max_prediction_change']>1e-3:
        conclusion='Numerical integration check failed. These scores are provisional diagnostic outputs, not reliable comparative evidence.'
    rows = ''.join(f'<tr><td>{names[k]}</td><td>{v["log_loss"]:.4f}</td><td>{v["brier"]:.4f}</td><td>{v["mean_auc"]:.4f}</td></tr>' for k,v in r['metrics'].items())
    page = f'''<!doctype html><html><meta charset="utf-8"><title>Activation pilot results</title>
<style>body{{font:17px/1.6 system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#203040}}table{{border-collapse:collapse}}td,th{{padding:9px 18px;border-bottom:1px solid #ddd;text-align:left}}img{{max-width:100%}}code{{background:#eef2f5}}</style>
<h1>{model_name}: local activation pilot</h1><p><strong>{conclusion}</strong></p>
<p>Paired test log-loss improvement: <strong>{delta['mean']:.4f}</strong> nats per decision; 95% case-bootstrap interval [{lo:.4f}, {hi:.4f}]. Positive favors activations.</p>
{secondary_note}
<img src="results.png" alt="Validation layer search and test log-loss comparison">
<table><tr><th>Method</th><th>Log loss ↓</th><th>Brier ↓</th><th>Mean per-decision AUROC ↑</th></tr>{rows}</table>
<h2>What was tested</h2><p>313 training, 84 validation, 103 test cases from the existing CKD dataset. Eight observed threshold/cost decisions predict four betting decisions. The same external responses are supplied to both nested methods. Numerical reports anchor the baseline in fitting, but are not prediction evidence. All decision families appear in training.</p>
<p>Selected layer: {r['selection']['layer']} of {r['n_layers']}; L2 penalty: {r['selection']['penalty']}. Selection used validation cases only. The {baseline_name} baseline was frozen during probe fitting. All parameters were refit using train plus validation before test evaluation.</p>
<h2>Limits on interpretation</h2><p>Activations come from a local 4-bit {model_name} checkpoint; responses come from the paper's hosted model. This is not a verified matched-checkpoint experiment. The activation-only condition is an ablation of the jointly trained probe, not a separately optimized activation-only competitor. One shuffled refit is a sanity check, not a permutation test. {released_note}</p>
<p>A predictive gain can reflect useful case features or correction of shared-link misspecification. It does not by itself demonstrate coherent beliefs. Compare the raw-profile control and flexible baseline before drawing that interpretation. The bootstrap interval is conditional on this training split and chosen configuration.</p>
<h2>Numerical checks</h2><p>Zero-shift identity maximum error: {r['baseline_zero_shift_max_error']:.2g}. Withheld-target invariance error: {r['target_leakage_max_error']:.2g}. Maximum change in augmented test probabilities at 256 rather than 128 quadrature points: {r['quadrature_128_vs_256_max_prediction_change']:.3g}.</p>
<p>Artifacts: <a href="results.json">metrics and checks</a>, <a href="test_predictions.csv">per-case predictions</a>, <a href="layer_search.csv">layer search</a>, <a href="results.pdf">PDF figure</a>.</p></html>'''
    (OUT/'report.html').write_text(page)
    print(OUT/'report.html')


if __name__=='__main__': main()
