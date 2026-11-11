"""Numerical and leakage checks, using synthetic data only."""
import unittest

import torch

from experiments.joint_beliefs.model import JointBeliefModel
from latent_beliefs.latent.domains import WEREWOLF_SPECS
from latent_beliefs.latent.model import ProbabilisticLatentModel


class JointModelTests(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(17)
        torch.set_num_threads(2)
        self.x = torch.randn(6, 4, dtype=torch.float64)
        self.y = torch.stack([torch.rand(6, dtype=torch.float64) if s.kind == 'probability'
                              else torch.randint(1, 6, (6,)).double() if s.kind == 'ordinal'
                              else torch.randint(0, 2, (6,)).double() for s in WEREWOLF_SPECS], dim=1)
        self.y[0, 3] = float('nan')

    def test_zero_shift_identity_all_channels_and_curves(self):
        for curve in ('linear', 'monotone_spline'):
            b = ProbabilisticLatentModel(WEREWOLF_SPECS, quadrature_points=32, response_curve=curve)
            m = JointBeliefModel(b, 4)
            torch.testing.assert_close(m.log_likelihood_by_output(self.y, self.x), b.log_likelihood_by_output(self.y), atol=1e-10, rtol=1e-10)
            torch.testing.assert_close(m.marginal_log_prob(self.y, self.x), b.marginal_log_prob(self.y), atol=1e-10, rtol=1e-10)
            observed = [0, 2, 14]
            weights = b.posterior_log_weights(self.y, observed).exp()
            expected = torch.stack([weights @ b.conditional_mean(s) for s in WEREWOLF_SPECS], 1)
            torch.testing.assert_close(m.conditional_means(self.y, self.x, observed), expected, atol=1e-10, rtol=1e-10)

    def test_targets_do_not_enter_posterior(self):
        m = JointBeliefModel(ProbabilisticLatentModel(WEREWOLF_SPECS, quadrature_points=32), 4)
        with torch.no_grad():
            m.w.copy_(torch.tensor([.2, -.1, .3, .1]))
        altered = self.y.clone()
        altered[:, 1:] = float('nan')
        torch.testing.assert_close(m.conditional_means(self.y, self.x, [0]), m.conditional_means(altered, self.x, [0]), atol=0, rtol=0)
        with self.assertRaises(ValueError):
            m.predictive_log_prob(self.y, self.x, [0], [0])

    def test_shared_shift_affects_every_output_and_has_correct_gradient(self):
        m = JointBeliefModel(ProbabilisticLatentModel(WEREWOLF_SPECS, quadrature_points=32), 4)
        before = m.conditional_means(self.y, self.x, [])
        with torch.no_grad():
            m.w[0] = .1
        after = m.conditional_means(self.y, self.x, [])
        self.assertTrue(torch.all((before - after).abs().sum(0) > 0))
        loss = -m.marginal_log_prob(self.y, self.x).mean()
        loss.backward()
        self.assertTrue(all(p.grad is not None and torch.isfinite(p.grad).all() for p in m.parameters()))
        analytic = m.w.grad[0].item()
        with torch.no_grad():
            m.w[0] += 1e-6
            plus = -m.marginal_log_prob(self.y, self.x).mean()
            m.w[0] -= 2e-6
            minus = -m.marginal_log_prob(self.y, self.x).mean()
        self.assertAlmostEqual(analytic, ((plus - minus) / 2e-6).item(), places=6)

    def test_quadrature_preserves_prior_and_parameters(self):
        m = JointBeliefModel(ProbabilisticLatentModel(WEREWOLF_SPECS, quadrature_points=32), 4)
        fine = m.with_quadrature(128)
        torch.testing.assert_close(m.behavior.prior_knot_logits, fine.behavior.prior_knot_logits)
        for old, new in zip(m.parameters(), fine.parameters()):
            torch.testing.assert_close(old, new)
        self.assertEqual(len(fine.behavior.z_grid), 128)


if __name__ == '__main__':
    unittest.main()
