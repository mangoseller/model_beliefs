import unittest

import numpy as np

from experiments.belief_calibration.run import choose, score
from latent_beliefs.latent.domains import CKD_SPECS


class SelectionTests(unittest.TestCase):
    def test_arm_constraints_and_conservative_ties(self):
        grid=[dict(epsilon=0.,temperature=1.,objective=1.),
              dict(epsilon=0.,temperature=.5,objective=.8),
              dict(epsilon=.01,temperature=1.,objective=.9),
              dict(epsilon=.01,temperature=.5,objective=.7),
              dict(epsilon=.025,temperature=.25,objective=.7-5e-6)]
        selected=choose(grid)
        self.assertEqual(selected['smoothing'],dict(epsilon=.01,temperature=1.))
        self.assertEqual(selected['tempering'],dict(epsilon=0.,temperature=.5))
        self.assertEqual(selected['combined'],dict(epsilon=.01,temperature=.5))
        grid.append(dict(epsilon=.01,temperature=.75,objective=.7+2e-6))
        self.assertEqual(choose(grid)['combined'],dict(epsilon=.01,temperature=.75))

    def test_macro_score_does_not_weight_by_channel_count(self):
        values={'binary':1.,'probability':2.,'ordinal':3.}
        records=[dict(target=j,nll=np.full(4,values[s.kind])) for j,s in enumerate(CKD_SPECS)]
        result=score(records)
        self.assertEqual(result['objective'],2.)
        for kind,value in values.items():self.assertEqual(result[kind],value)


if __name__=='__main__':unittest.main()
