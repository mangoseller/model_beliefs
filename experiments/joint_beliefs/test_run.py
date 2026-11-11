"""Verify batched evaluation agrees with the evidence-masked model API."""
import unittest
import numpy as np
import torch

from experiments.joint_beliefs.model import JointBeliefModel
from experiments.joint_beliefs.run import behavior, evaluate_arrays, evaluation_tasks, standardize, shuffled
from latent_beliefs.latent.domains import CKD_SPECS


class FullRunTests(unittest.TestCase):
    def test_evaluation_and_ablation(self):
        torch.set_num_threads(2)
        torch.manual_seed(12)
        y = torch.stack([torch.rand(5).double() if s.kind == 'probability' else
                         torch.randint(1,6,(5,)).double() if s.kind == 'ordinal' else
                         torch.randint(0,2,(5,)).double() for s in CKD_SPECS], 1)
        x = torch.randn(5,3,dtype=torch.float64)
        for curve in ['linear','monotone_spline']:
            model = JointBeliefModel(behavior(curve,32),3)
            with torch.no_grad():
                model.w.copy_(torch.tensor([.2,.1,-.2]))
                for ablation in [False,True]:
                    actual = iter(evaluate_arrays(model,y,x,ablation))
                    for task, observed, targets in evaluation_tasks():
                        evidence = [] if ablation else observed
                        means = model.conditional_means(y,x,evidence)
                        scores = model.predictive_log_prob(y,x,evidence,targets)
                        for col,target in enumerate(targets):
                            row = next(actual)
                            self.assertEqual((row['task'],row['target']),(task,target))
                            np.testing.assert_allclose(row['prediction'],means[:,target].numpy(),atol=1e-12)
                            np.testing.assert_allclose(row['nll'],-scores[:,col].numpy(),atol=1e-12)
                            if 'category_probabilities' in row:
                                np.testing.assert_allclose(row['category_probabilities'].sum(1),1,atol=1e-12)

    def test_preprocessing_and_shuffle_partition_isolation(self):
        raw = np.arange(24,dtype=float).reshape(8,3)
        train = np.arange(5)
        x,stats = standardize(raw,train)
        changed = raw.copy()
        changed[5:] += 10000
        other,other_stats = standardize(changed,train)
        np.testing.assert_array_equal(x[train],other[train])
        np.testing.assert_array_equal(stats['mean'],other_stats['mean'])
        permuted = shuffled(x,[train,np.arange(5,8)])
        for part in [train,np.arange(5,8)]:
            np.testing.assert_array_equal(np.sort(x[part],axis=0),np.sort(permuted[part],axis=0))


if __name__ == '__main__':
    unittest.main()
