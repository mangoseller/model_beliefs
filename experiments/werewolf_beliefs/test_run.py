import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import torch
from experiments.werewolf_beliefs import run
from experiments.werewolf_beliefs.data import covariates, prompts, neutral_prompt, PROMPTS

class PipelineTests(unittest.TestCase):
    def test_aligned_complete_splits(self):
        a,b = [run.frame(s,True).set_index('source_prompt_id').sort_index() for s in run.SIZES]
        self.assertEqual(a.split.to_dict(),b.split.to_dict())
        self.assertEqual(a.split.value_counts().to_dict(),dict(train=260,test=80,val=65))
        self.assertEqual(set(a.index),set(prompts()))
        for d in [a,b]: self.assertTrue(np.isfinite(run.likelihood_values(d,run.WEREWOLF_SPECS)).all())

    def test_neutral_prompt_preserves_visible_context_and_excludes_hidden_metadata(self):
        import json
        from latent_beliefs.werewolf.prompts import question_block,FAMILIES
        for line in PROMPTS.read_text().splitlines():
            row=json.loads(line)
            prompt=neutral_prompt(row)
            marker='GAME STATE AT THE CUT\n'
            context=row['prompt'].split(marker,1)[1]
            self.assertIn(marker+context,prompt)
            question,_=question_block(FAMILIES[0],row['target_player'])
            self.assertNotIn(question,prompt)
            self.assertTrue(prompt.endswith(f'player "{row["target_player"]}" is a Werewolf.'))
            row['target_role_ground_truth']='SENTINEL_SECRET_ROLE'
            row['label_is_werewolf']=777
            self.assertEqual(prompt,neutral_prompt(row))

    def test_text_preprocessing_excludes_heldout_cases(self):
        d=pd.DataFrame(dict(source_prompt_id=['a','b','c']))
        corpus=dict(a='alpha beta alpha',b='alpha beta gamma',c='alpha beta unseenword')
        with patch('experiments.werewolf_beliefs.data.prompts',return_value=corpus):
            first=covariates(d,[0,1])
        corpus['c']='totally new vocabulary alpha'
        with patch('experiments.werewolf_beliefs.data.prompts',return_value=corpus):
            second=covariates(d,[0,1])
        torch.testing.assert_close(first[:2],second[:2],atol=0,rtol=0)
        self.assertEqual(first.shape,second.shape)

    def test_evaluation_matches_predictive_api(self):
        torch.set_num_threads(2)
        torch.manual_seed(17)
        y=torch.stack([torch.rand(5,dtype=torch.float64) if s.kind=='probability' else torch.randint(1,6,(5,)).double() if s.kind=='ordinal' else torch.randint(0,2,(5,)).double() for s in run.WEREWOLF_SPECS],1)
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
