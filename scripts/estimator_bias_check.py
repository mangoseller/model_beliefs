"""Idealised comparison of the two leakage estimators in ``second_order/leakage.py``.

"original": C placed between the model's own B and A, normalised by D.
"matched":  C placed between the matched control M and D (the headline estimator).

Answers about the agent follow g(l) = alpha + kappa * l + u_i.  There is no latent
fit: beliefs are the truth plus independent measurement noise.  "inside" applies g
to C as well; "outside" leaves C uncompressed, the case in which neither estimator
recovers lambda.

    python scripts/estimator_bias_check.py
"""
import numpy as np
rng = np.random.default_rng(0)
def slope(y, x):
    return np.cov(y, x)[0, 1] / np.var(x, ddof=1)
def run(kind, lam, kappa, n=500, noise=0.35, u_sd=0.3, u_corr=0.0, model="inside"):
    out = []
    for _ in range(400):
        if kind == "ignorance":      # B = E[A | partial]: martingale, cov(B, A-B)=0
            B = rng.normal(-2.2, 0.8, n); A = B + rng.normal(0, 0.8, n)
        else:                        # flip: gap sign set by a field correlated with A
            A = rng.normal(-2.2, 1.1, n)
            s = np.where(rng.random(n) < 1 / (1 + np.exp(-(A + 2.2))), 1.0, -1.0)  # htn yes more likely at high A
            B = A - s * np.abs(rng.normal(0.7, 0.25, n))
        u = rng.normal(0, u_sd, n) + u_corr * (A - A.mean())
        g = lambda l: 0.3 + kappa * l + u
        mix = B + lam * (A - B)
        if model == "inside":   C = g(mix)                 # leakage inside the attribution map
        else:                   C = mix + u                # C uncompressed, only D compressed
        D, DB = g(A), g(B)
        e = lambda: rng.normal(0, noise, n)
        A_, Bp, Bd, C_, D_, DB_ = A + e(), B + e() * 1.3, B + e(), C + e(), D + e(), DB + e()
        x = A_ - Bp
        plan = slope(C_ - Bd, x) / slope(D_ - Bd, x)
        did = 1 - slope(C_ - D_, x) / slope(DB_ - D_, x)
        out.append((plan, did))
    return np.mean(out, axis=0), np.std(out, axis=0)
print("kind       model   lam kappa | original (sd) | matched (sd)")
for kind in ("ignorance", "flip"):
    for model in ("inside", "outside"):
        for kappa in (1.0, 0.9, 0.7, 0.55):
            for lam in (0.0, 0.5):
                m, s = run(kind, lam, kappa, model=model)
                print(f"{kind:10s} {model:7s} {lam:.1f} {kappa:.2f} | {m[0]:.3f} ({s[0]:.3f}) | {m[1]:.3f} ({s[1]:.3f})")
print("case effect correlated with A (u_corr=-0.2), kappa=1:")
for kind in ("ignorance", "flip"):
    for lam in (0.0, 0.5):
        m, s = run(kind, lam, 1.0, u_corr=-0.2)
        print(f"{kind:10s} lam={lam} | original {m[0]:.3f} | matched {m[1]:.3f}")
