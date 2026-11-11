"""Exercise the complete report with synthetic answers, never real test scores."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from experiments.werewolf_beliefs import report, run

class ReportTest(unittest.TestCase):
    def test_full_synthetic_report(self):
        rng=np.random.default_rng(19)
        frames={}
        for size in run.SIZES:
            df=run.frame(size,True)
            for spec in run.WEREWOLF_SPECS:
                df[spec.name]=(rng.uniform(.05,.95,len(df)) if spec.kind=='probability' else rng.integers(1,6,len(df)) if spec.kind=='ordinal' else rng.integers(0,2,len(df)))
            frames[size]=df
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'selection_lock.json').write_text('{}')
            (root/'protocol.json').write_text('{}')
            for size in run.SIZES:
                df=frames[size]
                fit=df[df.split!='test']
                test=df[df.split=='test']
                for curve in run.CURVES:
                    cell=root/size/curve
                    (cell/'final').mkdir(parents=True)
                    (cell/'baseline').mkdir()
                    timing=dict(seconds=1.,epochs_ran=300,best_epoch=50,best_validation_nll=1.)
                    (cell/'baseline/result.json').write_text(json.dumps(timing))
                    selection={m:dict(timing,layer=None if m=='behavior' else 1,penalty=.1,quadrature=128,training_numerically_stable=True) for m in run.METHODS}
                    (cell/'selection.json').write_text(json.dumps(selection))
                    candidate=cell/'candidates/activation_l1_p0.1'
                    candidate.mkdir(parents=True)
                    (candidate/'result.json').write_text(json.dumps(timing))
                    for method in report.ALL_METHODS:
                        rows=[]
                        for task,observed,targets in run.evaluation_tasks():
                            for target in targets:
                                spec=run.WEREWOLF_SPECS[target]
                                actual=test[spec.name].to_numpy()
                                prediction=np.clip(actual+rng.normal(0,.15,len(test)),.001,.999) if spec.kind!='ordinal' else np.clip(actual+rng.normal(0,.3,len(test)),1,5)
                                for i,case in enumerate(test.source_prompt_id):
                                    rows.append(dict(size=size,curve=curve,method=method,case_id=case,task=task,target=spec.name,kind=spec.kind,actual=actual[i],prediction=prediction[i],nll=rng.uniform(.1,1),train_mean=fit[spec.name].mean()))
                        pd.DataFrame(rows).to_csv(cell/'final'/f'{method}_predictions.csv',index=False)
                        audit=dict(quadrature=dict(passed=True,evaluation_points=512),training_numerically_stable=True,withheld_target_error=0)
                        (cell/'final'/f'{method}_complete.json').write_text(json.dumps(audit))
            with patch.object(report,'ROOT',root),patch.object(report,'frame',side_effect=lambda size,test=False: frames[size]),contextlib.redirect_stdout(io.StringIO()):
                report.main()
            summary=json.loads((root/'report_summary.json').read_text())
            self.assertEqual(summary['prediction_rows'],73600)
            self.assertTrue(summary['all_checks_passed'])
            self.assertTrue((root/'report.html').exists())
            clusters=pd.read_csv(root/'game_cluster_comparisons.csv')
            self.assertTrue((clusters.n_clusters==80).all())
            self.assertTrue(np.isfinite(clusters[['nll_gain','nll_ci_low','nll_ci_high']]).all().all())

if __name__=='__main__': unittest.main()
