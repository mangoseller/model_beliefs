"""Frozen-model evidence-weight sensitivity on validation cases only."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from experiments.joint_beliefs.run import (ROOT, CURVES, frame, activation_arrays,
                                          standardize, load_joint, evaluate_arrays)
from experiments.joint_beliefs.report import family, render_table
from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values

WEIGHTS=[0.,.1,.25,.5,.75,1.,1.5]


def predictions(model,y,x,observed,targets,weight):
    assert not set(observed)&set(targets)
    with torch.no_grad():
        ll=model.log_likelihood_by_output(y,x)
        logw=model.behavior.prior_log_weights()[None,:]+weight*ll[:,observed,:].sum(1)
        logw-=torch.logsumexp(logw,1,keepdim=True)
        return -torch.logsumexp(logw[:,None,:]+ll[:,targets,:],2).numpy()


def main():
    torch.set_num_threads(1)
    df=frame('4b')
    assert not (df.split=='test').any()
    train=np.flatnonzero(df.split=='train')
    val=np.flatnonzero(df.split=='val')
    assert (len(train),len(val))==(313,84)
    y=torch.tensor(likelihood_values(df,CKD_SPECS),dtype=torch.float64)
    activations=activation_arrays('4b',df)
    binary=[j for j,s in enumerate(CKD_SPECS) if s.kind=='binary']
    reports=[j for j,s in enumerate(CKD_SPECS) if s.kind!='binary']
    tasks=[('reports_to_decisions',reports,binary),('probability_to_decisions',[0],binary),
           ('likert_to_decisions',[14],binary),('bin_to_decisions',[15],binary),
           ('decisions_to_reports',binary,reports)]
    for name in ['betting','cost','threshold','unparameterized_decision']:
        tasks.append((name+'_to_reports',[j for j in binary if family(CKD_SPECS[j].name)==name],reports))
    rows=[]
    audits=[]
    for curve in CURVES:
        selection=json.loads((ROOT/'4b'/curve/'selection.json').read_text())['activation']
        x,_=standardize(activations[selection['layer']],train)
        model=load_joint(Path(selection['path'])/'model.pt',curve,x.shape[1],selection['quadrature'])
        reference=evaluate_arrays(model,y[val],x[val])
        for name,observed,targets in tasks:
            if name in ['reports_to_decisions','probability_to_decisions','decisions_to_reports']:
                expected=np.column_stack([r['nll'] for r in reference if r['task']==name])
                np.testing.assert_allclose(predictions(model,y[val],x[val],observed,targets,1.),expected,atol=1e-12,rtol=0)
            for weight in WEIGHTS:
                coarse=predictions(model.with_quadrature(256),y[val],x[val],observed,targets,weight)
                passed=False
                for points in [512,1024,2048]:
                    fine=predictions(model.with_quadrature(points),y[val],x[val],observed,targets,weight)
                    change=float(np.max(np.abs(fine-coarse)))
                    if change<.001:
                        passed=True
                        break
                    coarse=fine
                audits.append(dict(curve=curve,evidence=name,weight=weight,points=points,
                                   max_individual_nll_change=change,passed=passed))
                for k,target in enumerate(targets):
                    for i,case in enumerate(df.iloc[val].source_prompt_id):
                        rows.append(dict(curve=curve,evidence=name,weight=weight,case_id=case,
                                         target=CKD_SPECS[target].name,kind=CKD_SPECS[target].kind,nll=fine[i,k]))
    out=ROOT/'4b'/'validation_evidence'
    out.mkdir(exist_ok=True)
    rows=pd.DataFrame(rows)
    rows.to_csv(out/'case_scores.csv',index=False)
    summary=rows.groupby(['curve','evidence','kind','weight']).nll.mean().reset_index()
    summary.to_csv(out/'summary.csv',index=False)
    audits=pd.DataFrame(audits)
    audits.to_csv(out/'numerical_checks.csv',index=False)
    pivot=summary.pivot(index=['curve','evidence','kind'],columns='weight',values='nll').reset_index()
    main=pivot[pivot.evidence.isin(['reports_to_decisions','decisions_to_reports'])]
    status='All numerical checks passed.' if audits.passed.all() else 'Some integration checks failed; inspect numerical_checks.csv before interpretation.'
    (out/'report.html').write_text(f'''<!doctype html><html><head><meta charset="utf-8"><title>4B validation evidence sensitivity</title><style>body{{font:16px/1.6 system-ui;max-width:1200px;margin:36px auto;padding:0 24px}}table{{border-collapse:collapse;font-size:13px}}td,th{{padding:7px;border-bottom:1px solid #ddd}}.scroll{{overflow-x:auto}}</style></head><body>
<h1>4B: evidence-weight sensitivity on validation cases</h1><p>Frozen selected activation models trained on 313 cases; scored on the 84 validation cases. No test rows are loaded, no model is refitted, and no weight is applied to the main experiment. This diagnostic was motivated by previously observed test results; it is not an independent confirmation.</p>
<p>Only observed-response log likelihoods in the conditioning posterior are multiplied by the weight. Target likelihoods and prior weights remain unchanged. Weight 0 ignores external responses; weight 1 is the original model; weight 1.5 amplifies their influence. Lower NLL is better. Compare only within a row. These validation cases also selected the original model, so differences are descriptive.</p>
<p>{status} Weight-1 predictions were checked against the original evaluator for both reciprocal tasks and probability-only evidence.</p>
<h2>Main tasks</h2><div class="scroll">{render_table(main,list(main.columns))}</div>
<h2>Isolated evidence groups</h2><div class="scroll">{render_table(pivot,list(pivot.columns))}</div>
<p>Improvement from reducing weights would support excessive evidence influence as a symptom. It cannot distinguish dependence between responses, misspecified likelihoods, optimization, or sampling variation. A robust follow-up must choose its correction on development data and confirm on new cases.</p>
<p><a href="summary.csv">Scores</a> · <a href="case_scores.csv">Case scores</a> · <a href="numerical_checks.csv">Numerical checks</a> · <a href="../evidence_diagnostics/report.html">Test-set error localization</a></p></body></html>''')
    (out/'source.py').write_bytes(Path(__file__).read_bytes())
    (out/'metadata.json').write_text(json.dumps(dict(partition='validation',n_cases=84,
        n_training_cases=313,weights=WEIGHTS,parameters_refitted=False,test_loaded=False,
        checks_passed=bool(audits.passed.all())),indent=2))
    print(main.to_string(index=False))
    print(status)
    print(out/'report.html')


if __name__=='__main__':
    main()
