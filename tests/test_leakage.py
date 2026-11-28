"""Estimator, fit-group and cache tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from belief_leakage import leakage as L
from belief_leakage.beliefs import COLUMNS, clip_logit
from belief_leakage.conditions import CONDITIONS, references, setting_conditions
from belief_leakage.fit_pooled import groups
from belief_leakage.run_generation import Cache, Item, item_key

PHI, ALPHA = 0.8, 0.3


def methods_beliefs(lam: dict, phi: float = PHI, alpha: float = ALPHA, n: int = 4000, noise: float = 0.5,
                    seed: int = 0, shrink: float = 1.0, second_run_is_noise: bool = False,
                    curvature: float = 0.0, dataset: str = "bespoke") -> pd.DataFrame:
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
        k = setting_conditions(dataset, setting)
        truth[(k["A"], 0)] = truth[(k["A"], 1)] = a
        b = a - gap
        truth[(k["B"], 0)] = truth[(k["B"], 1)] = b
        for m, d in references(dataset).values():
            truth[(k[d], 0)] = alpha + phi * a
            truth[(k[m], 0)] = alpha + phi * b
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


def table(frame: pd.DataFrame, resamples: int = 20, dataset: str = "bespoke",
          reference: str = "single_record") -> pd.DataFrame:
    t = L.estimate_with_points(frame, dataset, resamples=resamples, seed=1)[0]
    return t[t.reference == reference].set_index(["setting", "statistic"])


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
            self.assertAlmostEqual(tau, PHI * (1 - lam), delta=0.05)
            self.assertAlmostEqual(tau, (self.get(setting, "beta_D_calibrated") - self.get(setting, "beta_C_calibrated"))
                                   / self.get(setting, "rho"), places=10)

    def test_tau_shared_uses_the_shared_phi_and_the_identity(self):
        for setting, lam in self.lam.items():
            phi_lambda = self.get(setting, "beta_C_calibrated") / self.get(setting, "rho")
            self.assertAlmostEqual(self.get(setting, "phi_lambda"), phi_lambda, places=10)
            self.assertAlmostEqual(self.get(setting, "tau_shared"), self.get("both", "phi") - phi_lambda, places=10)
            self.assertAlmostEqual(self.get(setting, "tau_shared"), PHI * (1 - lam), delta=0.05)
        self.assertAlmostEqual(self.get("both", "diff_tau"),
                               self.get("false", "tau") - self.get("partial", "tau"), places=10)

    def test_tau_is_zero_not_blank_when_person_ignores_the_evidence(self):
        t = table(methods_beliefs(self.lam, phi=0.0, n=400), resamples=200)
        for setting in self.lam:
            for stat in ("tau", "tau_shared"):
                row = t.loc[(setting, stat)]
                self.assertEqual(row.blank_reason, "", (setting, stat))
                self.assertAlmostEqual(row.estimate, 0.0, delta=0.1)
                self.assertLessEqual(row.ci_lower, 0.0)
                self.assertGreaterEqual(row.ci_upper, 0.0)

    def test_phi_is_shared_by_the_settings(self):
        self.assertAlmostEqual(self.get("both", "phi"), PHI, delta=0.04)
        self.assertAlmostEqual(self.get("both", "phi"), self.get("both", "beta_D_pooled") / self.get("both", "rho_pooled"),
                               places=10)
        self.assertAlmostEqual(self.get("both", "diff_phi_setting"), 0.0, delta=0.08)
        self.assertAlmostEqual(self.get("both", "diff_lambda"),
                               self.get("false", "lambda") - self.get("partial", "lambda"), places=10)
        self.assertEqual(self.get("partial", "phi"), self.get("both", "phi"))  # the headline phi is the shared one

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

    def test_fieller_set_is_unbounded_when_d_and_m_coincide(self):
        frame = methods_beliefs(self.lam, n=200)
        d = frame[frame.condition == "D"].set_index("case_id").value
        m = frame.condition == "M"
        rng = np.random.default_rng(0)
        frame.loc[m, "value"] = frame.loc[m, "value_calibrated"] = (
            d.loc[frame.case_id[m]].to_numpy() + 0.5 * rng.normal(size=m.sum()))
        t = table(frame, resamples=200)
        self.assertTrue(np.isfinite(t.loc[("partial", "lambda"), "estimate"]))  # reported, with its slopes
        self.assertIn(t.loc[("partial", "lambda_fieller"), "interval"], ("exclusive", "unbounded"))
        self.assertEqual(self.t.loc[("partial", "lambda_fieller"), "interval"], "bounded")

    def test_fieller_set_holds_the_estimate(self):
        row = self.t.loc[("partial", "lambda_fieller")]
        self.assertLess(row.ci_lower, self.get("partial", "lambda"))
        self.assertGreater(row.ci_upper, self.get("partial", "lambda"))
        rng = np.random.default_rng(0)
        weak = L.fieller(1.0, 0.01, rng.normal(1, 0.1, 500), rng.normal(0.01, 0.1, 500))
        self.assertEqual(weak["interval"], "exclusive")  # beta_D indistinguishable from 0
        tight = L.fieller(0.5, 1.0, rng.normal(0.5, 0.01, 500), rng.normal(1.0, 0.01, 500))
        self.assertEqual(tight["interval"], "bounded")
        self.assertLess(tight["ci_lower"], 0.5)
        self.assertGreater(tight["ci_upper"], 0.5)

    def test_shared_bootstrap_fits_each_setting_once_per_draw_and_reference(self):
        beliefs = methods_beliefs(self.lam, n=20, dataset="ckd")
        with patch.object(L, "setting_statistics", wraps=L.setting_statistics) as fits:
            L.estimate_with_points(beliefs, "ckd", resamples=10, seed=7)
        self.assertEqual(fits.call_count, 2 * len(references("ckd")) * (1 + 10))

    def test_layout_points_match_patient_ids_and_return_the_fitted_line(self):
        frame = methods_beliefs(self.lam, n=30, dataset="ckd")
        frame = frame[~((frame.case_id == "c1") & (frame.condition == "M_single"))]
        est, points = L.estimate_with_points(frame, "ckd", resamples=10, seed=3, match_references=True)
        selected = points[points.setting == "partial"]
        ids = selected.groupby("reference").case_id.agg(set)
        self.assertEqual(ids["single_record"], ids["two_record"])
        self.assertNotIn("c1", ids["two_record"])
        for ref, pts in selected.groupby("reference"):
            row = est[(est.setting == "partial") & (est.reference == ref)].set_index("statistic")
            self.assertEqual(len(pts), row.loc["beta_C", "n_cases"])
            np.testing.assert_allclose(pts.beta_C, row.loc["beta_C", "estimate"])
            np.testing.assert_allclose(pts.y_C_minus_M, pts.intercept_C + pts.beta_C * pts.x_A_minus_B + pts.residual_C)

    def test_ckd_reports_both_references(self):
        frame = methods_beliefs(self.lam, noise=0.0, n=300, dataset="ckd")
        full = L.estimate_with_points(frame, "ckd", resamples=5, seed=1)[0]
        self.assertEqual(set(full.reference), {"two_record", "single_record"})
        for reference in ("two_record", "single_record"):
            t = full[full.reference == reference].set_index(["setting", "statistic"])
            self.assertAlmostEqual(t.loc[("partial", "lambda"), "estimate"], 0.5, places=8)
            self.assertAlmostEqual(t.loc[("both", "phi"), "estimate"], PHI, places=8)

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
        own, person = groups(["A", "B", "B_F", "D", "D_single", "M", "M_single", "C", "M_F", "M_single_F", "C_F"])
        self.assertEqual(set(own), {"A", "B", "B_F"})
        self.assertEqual(set(person), {"D", "D_single", "M", "M_single", "C", "M_F", "M_single_F", "C_F"})
        self.assertEqual(groups(["A", "B", "C", "D"]), [("A", "B"), ("D", "C")])
        self.assertTrue(all(CONDITIONS[c].about == "person" for c in person))


class GenerationCache(unittest.TestCase):
    def test_key_is_stable_and_distinguishes_fields(self):
        k = item_key("m", "ckd", "case", "C", "probability/normal", 0)
        self.assertEqual(k, item_key("m", "ckd", "case", "C", "probability/normal", 0))
        self.assertEqual(len({k, item_key("m", "ckd", "case", "C_F", "probability/normal", 0),
                              item_key("m", "ckd", "case", "C", "probability/normal", 1),
                              item_key("m", "bespoke", "case", "C", "probability/normal", 0),
                              item_key("m2", "ckd", "case", "C", "probability/normal", 0)}), 5)

    def test_cache_roundtrip_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache("m", root=Path(tmp))
            item = Item("m", "case", "B", "likert/normal", 0, "prompt")
            cache.append([{"key": item.key, "prompt_sha": item.prompt_sha, "text": "x", "ok": True}])
            again = Cache("m", root=Path(tmp))
            self.assertIn(item.key, again)
            self.assertEqual(again.get(item.key)["text"], "x")


if __name__ == "__main__":
    unittest.main()
