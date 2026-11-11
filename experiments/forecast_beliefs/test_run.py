import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import torch
from experiments.forecast_beliefs import run
from experiments.forecast_beliefs.data import covariates, prompts, PROPOSITION

class PipelineTests(unittest.TestCase):
    def test_aligned_complete_splits(self):
        a,b = [run.frame(s,True).set_index('source_prompt_id').sort_index() for s in run.SIZES]
        self.assertEqual(a.split.to_dict(),b.split.to_dict())
        self.assertEqual(a.split.value_counts().to_dict(),dict(train=320,test=100,val=80))
        self.assertEqual(set(a.index),set(prompts()))
        for d in [a,b]: self.assertTrue(np.isfinite(run.likelihood_values(d,run.FORECASTING_SPECS)).all())

    def test_neutral_prompt_has_no_elicitation(self):
        from latent_beliefs.forecasting.prompts import parse_framings
        for prompt in prompts().values():
            self.assertTrue(prompt.endswith(PROPOSITION))
            self.assertNotIn(parse_framings()['probability_positive'],prompt)

    def test_text_preprocessing_excludes_heldout_cases(self):
        d=pd.DataFrame(dict(source_prompt_id=['a','b','c']))
        corpus=dict(a='alpha beta alpha',b='alpha beta gamma',c='alpha beta unseenword')
        with patch('experiments.forecast_beliefs.data.prompts',return_value=corpus):
            first=covariates(d,[0,1])
        corpus['c']='totally new vocabulary alpha'
        with patch('experiments.forecast_beliefs.data.prompts',return_value=corpus):
            second=covariates(d,[0,1])
        torch.testing.assert_close(first[:2],second[:2],atol=0,rtol=0)
        self.assertEqual(first.shape,second.shape)

    def test_evaluation_matches_predictive_api(self):
        torch.set_num_threads(2)
        torch.manual_seed(17)
        y=torch.stack([torch.rand(5,dtype=torch.float64) if s.kind=='probability' else torch.randint(1,6,(5,)).double() if s.kind=='ordinal' else torch.randint(0,2,(5,)).double() for s in run.FORECASTING_SPECS],1)
        x=torch.randn(5,3,dtype=torch.float64)
        for curve in run.CURVES:
            m=run.JointBeliefModel(run.behavior(curve,32),3)
            with torch.no_grad(): m.w[0]=.3
            records=run.evaluate_arrays(m,y,x)
            offset=0
            for task,observed,targets in run.evaluation_tasks():
                self.assertFalse(set(observed)&set(targets))
                expected=-m.predictive_log_prob(y,x,observed,targets).detach().numpy()
                for j,target in enumerate(targets):
                    record=records[offset+j]
                    self.assertEqual(record['target'],target)
                    np.testing.assert_allclose(record['nll'],expected[:,j],atol=1e-10)
                offset+=len(targets)
            self.assertEqual(offset,46)

if __name__=='__main__': unittest.main()
