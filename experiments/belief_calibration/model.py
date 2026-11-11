"""Normalized binary-lapse calibration and generalized evidence weighting."""
import math

import torch

from experiments.joint_beliefs.run import evaluation_tasks


class Prepared:
    """Cache a frozen model's quadrature likelihoods and conditional means."""

    def __init__(self, model, y, x):
        self.model, self.y = model, y
        self.specs = model.behavior.specs
        with torch.no_grad():
            self.ll = model.log_likelihood_by_output(y, x)
            self.logprior = model.behavior.prior_log_weights()[None, :]
            s = model.scores(x)
            self.means, self.ordinal = [], {}
            for j, spec in enumerate(self.specs):
                if spec.kind == 'probability':
                    mean = s.sigmoid()
                elif spec.kind == 'binary':
                    mean = model.response_score(spec, s).sigmoid()
                else:
                    probs = model.ordinal_probabilities(spec, s)
                    self.ordinal[j] = probs
                    mean = probs @ torch.arange(1, 6, dtype=s.dtype)
                self.means.append(mean)

    def evaluate(self, epsilon=0., temperature=1., primary=False):
        if not 0 <= epsilon <= 1 or not 0 <= temperature <= 1:
            raise ValueError('Calibration parameters must be in [0, 1]')
        with torch.no_grad():
            ll = self.ll.clone()
            means = list(self.means)
            if epsilon:
                for j, spec in enumerate(self.specs):
                    if spec.kind != 'binary':
                        continue
                    base = ll[:, j] + (math.log1p(-epsilon) if epsilon < 1 else -math.inf)
                    mixed = torch.logaddexp(base, torch.full_like(base, math.log(epsilon / 2)))
                    ll[:, j] = torch.where(torch.isfinite(self.y[:, j, None]), mixed, 0.)
                    means[j] = (1 - epsilon) * means[j] + epsilon / 2
            records = []
            for task, observed, targets in evaluation_tasks():
                if primary and task not in ['reports_to_decisions', 'decisions_to_reports']:
                    continue
                assert not set(observed) & set(targets)
                logw = self.logprior + temperature * ll[:, observed].sum(1)
                logw = logw - torch.logsumexp(logw, 1, keepdim=True)
                weights = logw.exp()
                for target in targets:
                    row = dict(task=task, target=target,
                               prediction=(weights * means[target]).sum(1).numpy(),
                               nll=-torch.logsumexp(logw + ll[:, target], 1).numpy())
                    if target in self.ordinal:
                        row['category_probabilities'] = (weights[..., None] * self.ordinal[target]).sum(1).numpy()
                    records.append(row)
            return records
