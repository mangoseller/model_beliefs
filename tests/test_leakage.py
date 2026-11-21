"""Estimator, fit-group and cache tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import leakage as L
from second_order.beliefs import COLUMNS, clip_logit
from second_order.conditions import CONDITIONS, setting_conditions
from second_order.fit_pooled import groups
from second_order.run_generation import Cache, Item, item_key

PHI, ALPHA = 0.8, 0.3


def methods_beliefs(lam: dict, phi: float = PHI, alpha: float = ALPHA, n: int = 4000, noise: float = 0.5,
                    seed: int = 0, shrink: float = 1.0, second_run_is_noise: bool = False,
                    curvature: float = 0.0) -> pd.DataFrame:
    """The Methods model with measurement noise and a second run of A, B and B_F.

    Own beliefs a (full) and a - delta (PERSON's evidence); about PERSON, l = alpha + phi * (.):
    D = alpha + phi a, M = alpha + phi (a - delta), C = M + lambda (D - M).  Every reading is
    truth + independent noise.  ``shrink`` multiplies the stored values (a fit's shrinkage)
    while value_calibrated keeps the unshrunk reading.
    """
    rng = np.random.default_rng(seed)
    a = rng.normal(-1.0, 1.2, n)
    gaps = {"partial": rng.normal(0.3, 1.0, n),
            "false": np.where(rng.random(n) < 0.5, 1.0, -1.0) * rng.normal(0.9, 0.2, n)}
    truth = {("A", 0): a, ("A", 1): a}
    for setting, gap in gaps.items():
        k = setting_conditions(setting)
        b = a - gap
        truth[(k["B"], 0)] = truth[(k["B"], 1)] = b
        truth[(k["D"], 0)] = alpha + phi * a
        truth[(k["M"], 0)] = alpha + phi * b
        truth[(k["C"], 0)] = alpha + phi * (b + lam[setting] * gap) + curvature * gap**2
    rows = []
    for (cond, rep), values in truth.items():
        v = values + noise * rng.normal(size=n)
        if second_run_is_noise and rep == 1:
            v = rng.normal(size=n)
        rows.append(pd.DataFrame({"case_id": [f"c{i}" for i in range(n)], "condition": cond, "replicate": rep,
                                  "value": shrink * v, "value_calibrated": v, "sd": 0.0}))
    frame = pd.concat(rows, ignore_index=True)
    frame["model"], frame["split"], frame["method"] = "t", "train", "direct"
    for col in COLUMNS:
        if col not in frame:
            frame[col] = ""
    return frame


def table(frame: pd.DataFrame, resamples: int = 20) -> pd.DataFrame:
    return L.leakage_table(frame, resamples=resamples, seed=1).set_index(["setting", "statistic"])


class MethodsEstimators(unittest.TestCase):
    """lambda = beta_C / beta_D; rho = Cov(x, x') / Var(x); phi = beta_D / rho, shared; tau = (beta_D - beta_C) / rho."""

    @classmethod
    def setUpClass(cls):
        cls.lam = {"partial": 0.5, "false": 0.3}
        cls.noise = 0.5
        cls.t = table(methods_beliefs(cls.lam, noise=cls.noise), resamples=50)

    def get(self, setting, stat):
        return self.t.loc[(setting, stat), "estimate"]

    def test_lambda_is_the_wald_ratio(self):
        for setting, lam in self.lam.items():
            self.assertAlmostEqual(self.get(setting, "lambda"),
                                   self.get(setting, "beta_C") / self.get(setting, "beta_D"), places=10)
            self.assertAlmostEqual(self.get(setting, "lambda"), lam, delta=0.05)

    def test_rho_corrects_the_attenuated_slope(self):
        for setting in self.lam:
            gap_var = 1.0 if setting == "partial" else 0.9**2 + 0.2**2  # random-sign gaps
            self.assertAlmostEqual(self.get(setting, "rho"), gap_var / (gap_var + 2 * self.noise**2), delta=0.04)
            self.assertLess(self.get(setting, "beta_D"), PHI - 0.05)               # attenuated by rho
            self.assertAlmostEqual(self.get(setting, "phi_setting"), PHI, delta=0.05)  # corrected

    def test_tau_is_the_methods_estimator_and_equals_phi_times_one_minus_lambda(self):
        for setting, lam in self.lam.items():
            tau = self.get(setting, "tau")
            self.assertAlmostEqual(tau, (self.get(setting, "beta_D_calibrated") - self.get(setting, "beta_C_calibrated"))
                                   / self.get(setting, "rho"), places=10)
            self.assertAlmostEqual(tau, PHI * (1 - lam), delta=0.05)
            self.assertAlmostEqual(tau, self.get(setting, "phi_setting") * (1 - self.get(setting, "lambda")),
                                   places=8)

    def test_phi_is_shared_by_the_settings(self):
        self.assertAlmostEqual(self.get("both", "phi"), PHI, delta=0.04)
        self.assertAlmostEqual(self.get("both", "phi"), self.get("both", "beta_D_pooled") / self.get("both", "rho_pooled"),
                               places=10)
        self.assertAlmostEqual(self.get("both", "diff_phi_setting"), 0.0, delta=0.08)
        self.assertAlmostEqual(self.get("both", "diff_lambda"),
                               self.get("false", "lambda") - self.get("partial", "lambda"), places=10)
        self.assertNotIn(("partial", "phi"), self.t.index)  # the headline phi is the shared one

    def test_affine_checks(self):
        for setting in self.lam:
            self.assertAlmostEqual(self.get(setting, "phi_D_on_A"), PHI, delta=0.05)
            self.assertAlmostEqual(self.get(setting, "phi_M_on_B"), PHI, delta=0.05)
            self.assertAlmostEqual(self.get(setting, "intercept_D_on_A"), ALPHA, delta=0.1)
            self.assertAlmostEqual(self.get(setting, "curvature_D"), 0.0, delta=0.05)
            self.assertAlmostEqual(self.get(setting, "curvature_C"), 0.0, delta=0.05)
        self.assertAlmostEqual(self.get("partial", "lambda_x_positive"), 0.5, delta=0.15)
        self.assertAlmostEqual(self.get("partial", "lambda_x_negative"), 0.5, delta=0.15)

    def test_curvature_check_sees_a_quadratic_term(self):
        t = table(methods_beliefs(self.lam, noise=0.0, n=500, curvature=0.2), resamples=5)
        self.assertAlmostEqual(t.loc[("partial", "curvature_C"), "estimate"], 0.2, places=6)
        self.assertAlmostEqual(t.loc[("partial", "curvature_D"), "estimate"], 0.0, places=6)

    def test_exact_without_noise(self):
        t = table(methods_beliefs(self.lam, noise=0.0, n=300), resamples=5)
        for setting, lam in self.lam.items():
            self.assertAlmostEqual(t.loc[(setting, "lambda"), "estimate"], lam, places=8)
            self.assertAlmostEqual(t.loc[(setting, "rho"), "estimate"], 1.0, places=8)
            self.assertAlmostEqual(t.loc[(setting, "tau"), "estimate"], PHI * (1 - lam), places=8)
            self.assertAlmostEqual(t.loc[(setting, "mean_position"), "estimate"], lam, places=8)
        self.assertAlmostEqual(t.loc[("both", "phi"), "estimate"], PHI, places=8)

    def test_phi_reads_calibrated_values_and_lambda_does_not_care(self):
        t = table(methods_beliefs(self.lam, noise=0.0, n=300, shrink=0.6), resamples=5)
        self.assertAlmostEqual(t.loc[("partial", "lambda"), "estimate"], 0.5, places=8)
        self.assertAlmostEqual(t.loc[("both", "phi"), "estimate"], PHI, places=8)
        self.assertAlmostEqual(t.loc[("partial", "beta_D"), "estimate"], PHI, places=8)  # x and y shrink alike

    def test_bootstrap_interval_contains_the_truth(self):
        t = table(methods_beliefs(self.lam, n=400, noise=0.3, seed=4), resamples=300)
        row = t.loc[("partial", "lambda")]
        self.assertLessEqual(row.ci_lower, 0.5)
        self.assertGreaterEqual(row.ci_upper, 0.5)
        self.assertTrue(np.isfinite(row.boot_median))

    def test_ratio_rule_blanks_phi_and_tau_when_the_second_run_is_noise(self):
        t = table(methods_beliefs(self.lam, n=200, second_run_is_noise=True), resamples=200)
        for key in (("partial", "tau"), ("both", "phi")):
            self.assertTrue(np.isnan(t.loc[key, "estimate"]), key)
            self.assertIn("rho", t.loc[key, "blank_reason"])
        self.assertTrue(np.isfinite(t.loc[("partial", "lambda"), "estimate"]))

    def test_ratio_rule_blanks_lambda_when_d_and_m_coincide(self):
        frame = methods_beliefs(self.lam, n=200)
        d = frame[frame.condition == "D"].set_index("case_id").value
        m = frame.condition == "M"
        rng = np.random.default_rng(0)
        frame.loc[m, "value"] = frame.loc[m, "value_calibrated"] = (
            d.loc[frame.case_id[m]].to_numpy() + 0.5 * rng.normal(size=m.sum()))
        row = table(frame, resamples=200).loc[("partial", "lambda")]
        self.assertTrue(np.isnan(row.estimate))
        self.assertIn("beta_D", row.blank_reason)

    def test_without_a_second_run_only_lambda_and_its_checks(self):
        frame = methods_beliefs(self.lam, n=200)
        t = table(frame[frame.replicate == 0])
        self.assertIn(("partial", "lambda"), t.index)
        self.assertNotIn(("partial", "rho"), t.index)
        self.assertNotIn(("both", "phi"), t.index)

    def test_clip_logit(self):
        self.assertAlmostEqual(clip_logit([0.0])[0], np.log(0.01 / 0.99))
        self.assertAlmostEqual(clip_logit([0.5])[0], 0.0)


class FitGroups(unittest.TestCase):
    def test_pooled_groups_follow_the_condition_table(self):
        own, person = groups(list(CONDITIONS))
        self.assertEqual(set(own), {"A", "B", "B_F"})
        self.assertEqual(set(person), {"C", "M", "D", "C_F", "M_F", "D_F"})
        self.assertEqual(groups(["A", "B", "C", "D"]), [("A", "B"), ("C", "D")])


class GenerationCache(unittest.TestCase):
    def test_key_is_stable_and_distinguishes_fields(self):
        k = item_key("m", "case", "C", "probability", 0)
        self.assertEqual(k, item_key("m", "case", "C", "probability", 0))
        self.assertEqual(len({k, item_key("m", "case", "C_F", "probability", 0),
                              item_key("m", "case", "C", "probability", 1),
                              item_key("m2", "case", "C", "probability", 0)}), 4)

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
