"""Estimator and cache tests (plan: tests before real data)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import leakage as L
from second_order.beliefs import COLUMNS, clip_logit
from second_order.run_generation import Cache, Item, item_key


def beliefs(lam: float, n: int = 300, noise: float = 0.0, seed: int = 0, method: str = "latent",
            user_shift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    a = rng.normal(-1.5, 1.2, n)
    b = a - rng.normal(0, 1.0, n)
    c = (1 - lam) * b + lam * a
    halves = L.HALVES[method]
    names = sorted({v for k, v in halves.items()})
    rows = []
    for i in range(n):
        for cond, val in (("A", a[i]), ("B", b[i]), ("C_john", c[i]), ("D_john", a[i]),
                          ("C_user", c[i] + user_shift), ("D_user", a[i] + user_shift)):
            for half in names:
                v = val + noise * rng.normal()
                rows.append({"model": "t", "case_id": f"c{i}", "split": "train", "condition": cond,
                             "framing": "x", "method": method, "half": half, "value": v,
                             "sd": 0.0, "value_calibrated": v})
    frame = pd.DataFrame(rows)
    for col in COLUMNS:
        if col not in frame:
            frame[col] = ""
    return frame


def point(frame: pd.DataFrame, method: str = "latent") -> dict[str, float]:
    W = L.wide(frame, method)
    return L.statistics({c: W[c].to_numpy(float) for c in W.columns}, method)


class Estimators(unittest.TestCase):
    def test_exact_recovery_without_noise(self):
        for lam in (0.0, 0.3, 0.7, 1.0):
            s = point(beliefs(lam))
            for f in ("john", "user"):
                self.assertAlmostEqual(s[f"lambda_C_{f}"], lam, places=8)
                self.assertAlmostEqual(s[f"lambda_D_{f}"], 1.0, places=8)
                self.assertAlmostEqual(s[f"normalised_lambda_{f}"], lam, places=8)
                self.assertAlmostEqual(s[f"C_{f}_two_weight_wA"], lam, places=8)
                self.assertAlmostEqual(s[f"C_{f}_two_weight_wB"], 1 - lam, places=8)
            self.assertAlmostEqual(s["lambda_0"], 0.0, places=8)

    def test_independent_outcome_noise_does_not_bias(self):
        lams = [point(beliefs(0.5, n=2000, noise=0.0, seed=s))["lambda_C_john"] for s in range(3)]
        self.assertTrue(all(abs(v - 0.5) < 1e-6 for v in lams))

    def test_person_shift_moves_intercept_not_slope(self):
        s = point(beliefs(0.4, user_shift=0.3))
        self.assertAlmostEqual(s["diff_user_minus_john_lambda_C"], 0.0, places=8)
        self.assertAlmostEqual(s["diff_user_minus_john_c_C"], 0.3, places=8)
        self.assertAlmostEqual(s["mean_shift_C_user_minus_john"], 0.3, places=8)

    def test_bootstrap_interval_contains_truth_with_noise(self):
        table = L.leakage_table(beliefs(0.6, n=400, noise=0.3, seed=4), "latent", resamples=300, seed=1)
        row = table.set_index("statistic").loc["normalised_lambda_john"]
        self.assertLessEqual(row.ci_lower, 0.6)
        self.assertGreaterEqual(row.ci_upper, 0.6)

    def test_direct_two_weight(self):
        s = point(beliefs(0.25, method="direct"), "direct")
        self.assertAlmostEqual(s["C_john_two_weight_wA"], 0.25, places=8)
        self.assertNotIn("lambda_C_john", s)

    def test_closer_share(self):
        self.assertEqual(point(beliefs(0.0))["closer_to_B_share_john"], 1.0)
        self.assertEqual(point(beliefs(1.0))["closer_to_B_share_john"], 0.0)

    def test_clip_logit(self):
        self.assertAlmostEqual(clip_logit([0.0])[0], np.log(0.01 / 0.99))
        self.assertAlmostEqual(clip_logit([0.5])[0], 0.0)


class GenerationCache(unittest.TestCase):
    def test_key_is_stable_and_distinguishes_fields(self):
        k = item_key("m", "case", "C_john", "probability", 0)
        self.assertEqual(k, item_key("m", "case", "C_john", "probability", 0))
        self.assertEqual(len({k, item_key("m", "case", "C_user", "probability", 0),
                              item_key("m", "case", "C_john", "probability", 1),
                              item_key("m2", "case", "C_john", "probability", 0)}), 4)

    def test_cache_roundtrip_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache("m", root=Path(tmp))
            item = Item("m", "case", "B", "likert", 0, "prompt")
            cache.append([{"key": item.key, "prompt_sha": item.prompt_sha, "text": "x", "ok": True}])
            again = Cache("m", root=Path(tmp))
            self.assertIn(item.key, again)
            self.assertEqual(again.get(item.key)["text"], "x")


if __name__ == "__main__":
    unittest.main()
