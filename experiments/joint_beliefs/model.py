"""One activation-dependent prior shift shared by all sixteen response channels.

The quadrature integrates the ORIGINAL latent variable. All likelihoods are
evaluated at logit(z_q) + x @ w; the prior weights therefore need no Jacobian.
The original model owns every response and prior parameter, all jointly trained.
"""
from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F

from latent_beliefs.latent.model import ProbabilisticLatentModel


class JointBeliefModel(nn.Module):
    def __init__(self, behavior: ProbabilisticLatentModel, n_features: int):
        super().__init__()
        self.behavior = behavior
        self.w = nn.Parameter(torch.zeros(n_features, dtype=behavior.dtype,
                                         device=behavior.z_grid.device))

    def scores(self, x):
        return self.behavior.logit_z_grid[None, :] + (x @ self.w)[:, None]

    def response_score(self, spec, s):
        b = self.behavior
        if b.response_curve == 'monotone_spline':
            knots = b.spline_knot_logits
            values = b.spline_knot_values(spec.name)
            ix = torch.bucketize(s.contiguous(), knots[1:-1])
            fraction = ((s - knots[ix]) / (knots[ix + 1] - knots[ix])).clamp(0, 1)
            return values[ix] + fraction * (values[ix + 1] - values[ix])
        if spec.kind == 'binary':
            return b.binary_intercepts[spec.name] + b.binary_slope(spec.name) * s
        return b.ordinal_slope(spec.name) * s

    def ordinal_probabilities(self, spec, s):
        score = self.response_score(spec, s)
        cuts = self.behavior.ordinal_cutpoints(spec)
        cumulative = torch.sigmoid(cuts - score[..., None])
        probabilities = torch.cat((cumulative[..., :1],
                                   cumulative[..., 1:] - cumulative[..., :-1],
                                   1 - cumulative[..., -1:]), dim=-1)
        return probabilities.clamp_min(torch.finfo(s.dtype).tiny)

    def output_log_prob(self, index, observations, s):
        b = self.behavior
        spec = b.specs[index]
        finite = torch.isfinite(observations)
        y = torch.where(finite, observations, torch.zeros_like(observations))
        if spec.kind == 'probability':
            y = y.clamp(b.probability_epsilon, 1 - b.probability_epsilon)[:, None]
            # Evaluate both sigmoid tails separately to avoid 1-sigmoid(s)
            # rounding to zero for large positive shifts.
            tiny = torch.finfo(s.dtype).tiny
            alpha = torch.sigmoid(s).clamp_min(tiny) * b.probability_precision
            beta = torch.sigmoid(-s).clamp_min(tiny) * b.probability_precision
            result = (torch.lgamma(alpha + beta) - torch.lgamma(alpha) - torch.lgamma(beta)
                      + (alpha - 1) * y.log() + (beta - 1) * torch.log1p(-y))
        elif spec.kind == 'binary':
            logits = self.response_score(spec, s)
            result = y[:, None] * F.logsigmoid(logits) + (1 - y[:, None]) * F.logsigmoid(-logits)
        else:
            probabilities = self.ordinal_probabilities(spec, s)
            category = (y.long() - 1).clamp(0, probabilities.shape[-1] - 1)
            result = probabilities.gather(-1, category[:, None, None].expand(-1, s.shape[1], 1)).squeeze(-1).log()
        return torch.where(finite[:, None], result, torch.zeros_like(result))

    def log_likelihood_by_output(self, observations, x):
        s = self.scores(x)
        return torch.stack([self.output_log_prob(j, observations[:, j], s)
                            for j in range(len(self.behavior.specs))], dim=1)

    def marginal_log_prob(self, observations, x):
        ll = self.log_likelihood_by_output(observations, x).sum(1)
        return torch.logsumexp(ll + self.behavior.prior_log_weights(), dim=1)

    def posterior_log_weights(self, observations, x, observed):
        # Evaluate ONLY the named evidence; withheld values cannot affect weights.
        s = self.scores(x)
        logw = self.behavior.prior_log_weights().expand(len(x), -1)
        for j in observed:
            logw = logw + self.output_log_prob(j, observations[:, j], s)
        return logw - torch.logsumexp(logw, dim=1, keepdim=True)

    def conditional_means(self, observations, x, observed):
        weights = self.posterior_log_weights(observations, x, observed).exp()
        s = self.scores(x)
        means = []
        for spec in self.behavior.specs:
            if spec.kind == 'probability':
                conditional = s.sigmoid()
            elif spec.kind == 'binary':
                conditional = self.response_score(spec, s).sigmoid()
            else:
                probabilities = self.ordinal_probabilities(spec, s)
                categories = torch.arange(1, probabilities.shape[-1] + 1,
                                          dtype=s.dtype, device=s.device)
                conditional = probabilities @ categories
            means.append((weights * conditional).sum(1))
        return torch.stack(means, dim=1)

    def predictive_log_prob(self, observations, x, observed, targets):
        """Marginal predictive score for each target, without conditioning on it."""
        if set(observed) & set(targets):
            raise ValueError('Evidence and targets must be disjoint')
        logw = self.posterior_log_weights(observations, x, observed)
        s = self.scores(x)
        return torch.stack([torch.logsumexp(logw + self.output_log_prob(j, observations[:, j], s), dim=1)
                            for j in targets], dim=1)

    def with_quadrature(self, points):
        """Change integration resolution, preserving fitted prior knot locations."""
        old = self.behavior
        new = ProbabilisticLatentModel(old.specs, quadrature_points=points,
                                      probability_epsilon=old.probability_epsilon,
                                      response_curve=old.response_curve,
                                      spline_knot_probabilities=old.spline_knot_probabilities.tolist(),
                                      prior_knots=old.prior_knots, dtype=old.dtype).to(old.z_grid.device)
        state = new.state_dict()
        for key, value in old.state_dict().items():
            if key not in {'z_grid', 'log_prior_weights', 'logit_z_grid'}:
                state[key] = value.detach().clone()
        new.load_state_dict(state)
        result = JointBeliefModel(new, self.w.numel())
        result.w.data.copy_(self.w.detach())
        return result
