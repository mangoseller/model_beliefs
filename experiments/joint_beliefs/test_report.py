"""Check pairing, signs, and basic metrics in the reporting layer."""
import unittest

import numpy as np
import pandas as pd

from experiments.joint_beliefs.report import bootstrap_gain, metrics


class ReportTests(unittest.TestCase):
    def sample(self, prediction):
        actual=np.array([0.,1.,0.,1.])
        p=np.asarray(prediction)
        return pd.DataFrame(dict(case_id=['a','b','a','b'],target=['one','one','two','two'],
                                 actual=actual,prediction=p,train_mean=.5,
                                 nll=-(actual*np.log(p)+(1-actual)*np.log1p(-p))))

    def test_paired_gain_and_uncertainty_sign(self):
        a=self.sample([.5]*4)
        b=self.sample([.2,.8,.2,.8]).iloc[::-1]
        result,deltas=bootstrap_gain(a,b,np.array([[0,0],[0,1],[1,1]]))
        expected=np.log(.8/.5)
        np.testing.assert_allclose(deltas,expected)
        self.assertAlmostEqual(result['nll_gain'],expected)
        self.assertAlmostEqual(result['nll_ci_low'],expected)
        self.assertAlmostEqual(result['nll_ci_high'],expected)
        self.assertGreater(result['mse_gain'],0)

    def test_wrong_case_pairs_rejected(self):
        a=self.sample([.5]*4)
        b=a.copy()
        b.loc[0,'case_id']='c'
        with self.assertRaises(ValueError):
            bootstrap_gain(a,b,np.array([[0,1]]))

    def test_ranking_and_r2(self):
        a=metrics(self.sample([.5]*4))
        b=metrics(self.sample([.2,.8,.2,.8]))
        self.assertEqual(a['mean_c_index'],.5)
        self.assertEqual(a['r2_vs_test_constant'],0.)
        self.assertEqual(b['mean_c_index'],1.)
        self.assertAlmostEqual(b['r2_vs_test_constant'],.84)


if __name__=='__main__':
    unittest.main()
