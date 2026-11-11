import unittest

import numpy as np
import torch

from experiments.belief_calibration.model import Prepared
from experiments.joint_beliefs.model import JointBeliefModel
from experiments.joint_beliefs.run import behavior, evaluate_arrays
from latent_beliefs.latent.domains import CKD_SPECS


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        torch.manual_seed(27)
        self.y = torch.stack([torch.rand(6).double() if s.kind == 'probability' else
                              torch.randint(1, 6, (6,)).double() if s.kind == 'ordinal' else
                              torch.randint(0, 2, (6,)).double() for s in CKD_SPECS], 1)
        self.x = torch.randn(6, 3, dtype=torch.float64)

    def models(self):
        for curve in ['linear', 'monotone_spline']:
            model = JointBeliefModel(behavior(curve, 32), 3)
            with torch.no_grad():
                model.w.copy_(torch.tensor([.3, -.2, .1]))
            yield model

    def test_original_identity_and_zero_evidence(self):
        for model in self.models():
            for tau in [0., 1.]:
                actual = Prepared(model, self.y, self.x).evaluate(0., tau)
                expected = evaluate_arrays(model, self.y, self.x, internal_only=tau == 0)
                for a, b in zip(actual, expected):
                    for key in ['prediction', 'nll']:
                        np.testing.assert_allclose(a[key], b[key], atol=1e-12, rtol=0)

    def test_smoothed_scores_are_normalized_and_floored(self):
        for model in self.models():
            for eps in [.02, 1.]:
                for row in Prepared(model, self.y, self.x).evaluate(eps, .5):
                    j = row['target']
                    if CKD_SPECS[j].kind == 'binary':
                        p = row['prediction']
                        self.assertTrue(np.all(p >= eps / 2 - 1e-12))
                        self.assertTrue(np.all(p <= 1 - eps / 2 + 1e-12))
                        expected = np.where(self.y[:, j].numpy() == 1, p, 1 - p)
                        np.testing.assert_allclose(np.exp(-row['nll']), expected, atol=1e-12)
                    if 'category_probabilities' in row:
                        np.testing.assert_allclose(row['category_probabilities'].sum(1), 1, atol=1e-12)

    def test_targets_do_not_affect_predictions(self):
        for model in self.models():
            expected = Prepared(model, self.y, self.x).evaluate(.02, .25)
            altered = self.y.clone()
            altered[:, 1:14] = 1 - altered[:, 1:14]
            actual = Prepared(model, altered, self.x).evaluate(.02, .25)
            for a, b in zip(actual, expected):
                if a['task'] == 'reports_to_decisions':
                    np.testing.assert_array_equal(a['prediction'], b['prediction'])

    def test_missing_binary_evidence_remains_neutral(self):
        for model in self.models():
            y = self.y.clone()
            y[:, 1:14] = float('nan')
            p = Prepared(model, y, self.x)
            a, b = p.evaluate(.02, 1.), p.evaluate(.02, 0.)
            for x, z in zip(a, b):
                if x['task'] == 'decisions_to_reports':
                    np.testing.assert_allclose(x['prediction'], z['prediction'], atol=1e-12)


if __name__ == '__main__':
    unittest.main()
