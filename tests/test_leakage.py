"""Estimator and cache tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import leakage as L
from second_order.beliefs import COLUMNS, clip_logit
from second_order.conditions import CONDITIONS, pair_conditions
from second_order.fit_pooled import groups
from second_order.run_generation import Cache, Item, item_key


def beliefs(lam: float, n: int = 300, noise: float = 0.0, seed: int = 0, method: str = "latent",
            kappa: float = 1.0, alpha: float = 0.0, u_slope: float = 0.0, flip: bool = False,
            pairs=("S4", "F", "H"), matched=("S4", "F", "H"), user: bool = True,
            lam_by_pair: dict | None = None) -> pd.DataFrame:
    """Beliefs under the plan's model: answers about the agent are alpha + kappa * (.) + u."""
    rng = np.random.default_rng(seed)
    a = rng.normal(-1.5, 1.2, n)
    u = u_slope * (a - a.mean()) + rng.normal(0, 0.2, n) * (u_slope != 0)
    g = lambda v: alpha + kappa * v + u  # noqa: E731
    truth = {"A": a, "D_john": g(a)}
    if user:
        truth["D_user"] = g(a)
    for pair in pairs:
        gap = np.where(a > np.median(a), 1, -1) * np.abs(rng.normal(0.7, 0.3, n)) if flip else rng.normal(0, 1.0, n)
        b = a - gap
        lam_p = (lam_by_pair or {}).get(pair, lam)
        k = pair_conditions("john", pair)
        truth[k["B"]] = b
        truth[k["C"]] = g(b + lam_p * gap)
        if pair in matched:
            truth[k["M"]] = g(b)
        if pair == "S4" and user:
            truth["C_user"] = g(b + lam_p * gap)
    names = sorted({v for v in L.HALVES[method].values()})
    rows = []
    for cond, values in truth.items():
        for half in names:
            v = values + noise * rng.normal(size=n)
            rows.append(pd.DataFrame({"case_id": [f"c{i}" for i in range(n)], "condition": cond,
                                      "half": half, "value": v, "value_calibrated": v, "sd": 0.0}))
    frame = pd.concat(rows, ignore_index=True)
    frame["model"], frame["split"], frame["framing"], frame["method"] = "t", "train", "x", method
    for col in COLUMNS:
        if col not in frame:
            frame[col] = ""
    return frame


def table(frame: pd.DataFrame, method: str = "latent", resamples: int = 20, **kwargs) -> pd.DataFrame:
    t = L.leakage_table(frame, method, resamples=resamples, seed=1, **kwargs)
    return t.set_index(["framing", "pair", "statistic"])


class MatchedControl(unittest.TestCase):
    def test_exact_recovery_without_noise(self):
        for lam in (0.0, 0.3, 0.7, 1.0):
            t = table(beliefs(lam))
            for pair in ("S4", "F", "H"):
                self.assertAlmostEqual(t.loc[("john", pair, "lambda"), "estimate"], lam, places=8)
                self.assertAlmostEqual(t.loc[("john", pair, "kappa_halves"), "estimate"], 1.0, places=8)
                self.assertAlmostEqual(t.loc[("john", pair, "tau_halves"), "estimate"], 1 - lam, places=8)
                self.assertAlmostEqual(t.loc[("john", pair, "normalised_lambda_original"), "estimate"],
                                       lam, places=8)

    def test_unbiased_under_compression_shift_and_patient_effect(self):
        """Where the original estimator fails: kappa != 1, flip-like gaps, u depending on risk."""
        t = table(beliefs(0.4, kappa=0.7, alpha=0.3, u_slope=-0.2, flip=True, n=600))
        self.assertAlmostEqual(t.loc[("john", "F", "lambda"), "estimate"], 0.4, places=8)
        self.assertGreater(abs(t.loc[("john", "F", "normalised_lambda_original"), "estimate"] - 0.4), 0.02)

    def test_kappa_and_tau(self):
        t = table(beliefs(0.25, kappa=0.8))
        self.assertAlmostEqual(t.loc[("john", "S4", "kappa_halves"), "estimate"], 0.8, places=8)
        self.assertAlmostEqual(t.loc[("john", "S4", "tau_halves"), "estimate"], 0.8 * 0.75, places=8)

    def test_mean_position_sees_a_uniform_shift_that_lambda_misses(self):
        frame = beliefs(0.0)
        frame.loc[frame.condition == "C_john", ["value", "value_calibrated"]] += 0.5
        t = table(frame)
        self.assertAlmostEqual(t.loc[("john", "S4", "lambda"), "estimate"], 0.0, places=8)
        self.assertGreater(abs(t.loc[("john", "S4", "intercept_C"), "estimate"]), 0.4)

    def test_pair_without_matched_control_reports_tau_and_borrows_kappa(self):
        t = table(beliefs(0.3, kappa=0.8, matched=("S4", "F")))
        self.assertNotIn(("john", "H", "lambda"), t.index)
        self.assertAlmostEqual(t.loc[("john", "H", "tau_halves"), "estimate"], 0.8 * 0.7, places=8)
        self.assertAlmostEqual(t.loc[("contrast", "H_vs_S4", "lambda_borrowed_kappa"), "estimate"], 0.3,
                               places=8)

    def test_contrasts(self):
        t = table(beliefs(0.2, lam_by_pair={"F": 0.6}))
        self.assertAlmostEqual(t.loc[("contrast", "F_minus_H", "diff_lambda"), "estimate"], 0.4, places=8)
        self.assertAlmostEqual(t.loc[("contrast", "user_minus_john", "diff_tau_halves"), "estimate"], 0.0, places=8)

    def test_bootstrap_interval_contains_truth_with_noise(self):
        t = table(beliefs(0.6, n=400, noise=0.3, seed=4), resamples=300)
        row = t.loc[("john", "S4", "lambda")]
        self.assertLessEqual(row.ci_lower, 0.6)
        self.assertGreaterEqual(row.ci_upper, 0.6)
        self.assertTrue(np.isfinite(row.boot_median))

    def test_ratio_rule_blanks_when_anchors_coincide(self):
        frame = beliefs(0.5, n=200, noise=0.5)
        m = frame.condition == "M_S4"
        d = frame[frame.condition == "D_john"].set_index(["case_id", "half"]).value
        rng = np.random.default_rng(0)
        base = d.loc[list(zip(frame.case_id[m], frame.half[m]))].to_numpy()
        frame.loc[m, "value"] = frame.loc[m, "value_calibrated"] = base + 0.5 * rng.normal(size=m.sum())
        row = table(frame, resamples=200).loc[("john", "S4", "lambda")]
        self.assertTrue(np.isnan(row.estimate))
        self.assertIn("T_M", row.blank_reason)

    def test_direct_and_probe_use_the_same_ratio(self):
        t = table(beliefs(0.25, method="direct"), "direct")
        self.assertAlmostEqual(t.loc[("john", "S4", "lambda"), "estimate"], 0.25, places=8)
        self.assertNotIn(("john", "S4", "kappa_halves"), t.index)  # no independent halves
        probe = beliefs(0.25, method="probe")
        t = table(probe, "probe", regressor=beliefs(0.25, method="latent"))
        self.assertAlmostEqual(t.loc[("john", "S4", "lambda"), "estimate"], 0.25, places=8)

    def test_clip_logit(self):
        self.assertAlmostEqual(clip_logit([0.0])[0], np.log(0.01 / 0.99))
        self.assertAlmostEqual(clip_logit([0.5])[0], 0.0)


def methods_beliefs(lam: dict, phi: float = 0.8, alpha: float = 0.3, n: int = 4000, noise: float = 0.5,
                    seed: int = 0, method: str = "direct", shrink: float = 1.0,
                    replicate_noise_only: bool = False) -> pd.DataFrame:
    """The Methods model with measurement noise and a second run of A, B and B_F.

    Own beliefs a (full), a - delta (partial or false); about the agent l = alpha + phi * (.).
    Every reading is truth + independent noise.  ``shrink`` multiplies raw values (a fit's
    shrinkage) while value_calibrated keeps the unshrunk reading.
    """
    rng = np.random.default_rng(seed)
    a = rng.normal(-1.0, 1.2, n)
    gaps = {"S4": rng.normal(0.3, 1.0, n), "F": np.where(rng.random(n) < 0.5, 1.0, -1.0) * rng.normal(0.9, 0.2, n)}
    truth = {("A", 0): a, ("A", 1): a, ("D_john", 0): alpha + phi * a, ("D_F", 0): alpha + phi * a}
    for pair, gap in gaps.items():
        k = pair_conditions("john", pair)
        b = a - gap
        truth[(k["B"], 0)] = truth[(k["B"], 1)] = b
        truth[(k["M"], 0)] = alpha + phi * b
        truth[(k["C"], 0)] = alpha + phi * (b + lam[pair] * gap)
    rows = []
    for (cond, rep), values in truth.items():
        v = values + noise * rng.normal(size=n)
        if replicate_noise_only and rep == 1:
            v = rng.normal(size=n)
        rows.append(pd.DataFrame({"case_id": [f"c{i}" for i in range(n)], "condition": cond, "replicate": rep,
                                  "half": "all", "value": shrink * v, "value_calibrated": v, "sd": 0.0}))
    frame = pd.concat(rows, ignore_index=True)
    frame["model"], frame["split"], frame["framing"], frame["method"] = "t", "train", "x", method
    for col in COLUMNS:
        if col not in frame:
            frame[col] = ""
    return frame


class MethodsEstimators(unittest.TestCase):
    """lambda = beta_C / beta_D, rho from the second run, phi = beta_D / rho, tau = phi (1 - lambda)."""

    @classmethod
    def setUpClass(cls):
        cls.lam = {"S4": 0.5, "F": 0.3}
        cls.noise = 0.5
        cls.t = table(methods_beliefs(cls.lam, noise=cls.noise), "direct", resamples=50)

    def get(self, pair, stat):
        return self.t.loc[("john", pair, stat), "estimate"]

    def test_lambda_is_the_wald_ratio_and_equals_the_matched_control_form(self):
        for pair, lam in self.lam.items():
            self.assertAlmostEqual(self.get(pair, "lambda"), self.get(pair, "beta_C") / self.get(pair, "beta_D"),
                                   places=10)
            self.assertAlmostEqual(self.get(pair, "beta_D"), self.get(pair, "T_M"), places=10)
            self.assertAlmostEqual(self.get(pair, "lambda"), lam, delta=0.05)

    def test_rho_corrects_the_attenuated_slope(self):
        for pair in self.lam:
            gap_var = 1.0 if pair == "S4" else 0.9**2 + 0.2**2  # flip-like gaps: random sign
            expected_rho = gap_var / (gap_var + 2 * self.noise**2)
            self.assertAlmostEqual(self.get(pair, "rho"), expected_rho, delta=0.04)
            self.assertLess(self.get(pair, "beta_D"), 0.8 - 0.05)          # attenuated
            self.assertAlmostEqual(self.get(pair, "phi"), 0.8, delta=0.05)  # corrected
            self.assertAlmostEqual(self.get(pair, "tau"), 0.8 * (1 - self.lam[pair]), delta=0.05)
            self.assertAlmostEqual(self.get(pair, "tau"), self.get(pair, "phi") * (1 - self.get(pair, "lambda")),
                                   places=8)

    def test_affine_checks(self):
        for pair in self.lam:
            self.assertAlmostEqual(self.get(pair, "phi_D_on_A"), 0.8, delta=0.05)
            self.assertAlmostEqual(self.get(pair, "phi_M_on_B"), 0.8, delta=0.05)
            self.assertAlmostEqual(self.get(pair, "intercept_D_on_A"), 0.3, delta=0.1)
            self.assertAlmostEqual(self.get(pair, "curvature_D"), 0.0, delta=0.05)
            self.assertAlmostEqual(self.get(pair, "curvature_C"), 0.0, delta=0.05)
        self.assertAlmostEqual(self.get("S4", "lambda_x_positive"), 0.5, delta=0.15)
        self.assertAlmostEqual(self.get("S4", "lambda_x_negative"), 0.5, delta=0.15)

    def test_settings_contrast_and_pooled_phi(self):
        row = lambda s: self.t.loc[("contrast", "F_minus_S4", s), "estimate"]  # noqa: E731
        self.assertAlmostEqual(row("diff_lambda"), self.get("F", "lambda") - self.get("S4", "lambda"), places=10)
        self.assertAlmostEqual(row("phi_pooled"), 0.8, delta=0.04)
        self.assertAlmostEqual(row("diff_phi"), 0.0, delta=0.08)

    def test_exact_without_noise(self):
        t = table(methods_beliefs(self.lam, noise=0.0, n=300), "direct", resamples=5)
        for pair, lam in self.lam.items():
            self.assertAlmostEqual(t.loc[("john", pair, "lambda"), "estimate"], lam, places=8)
            self.assertAlmostEqual(t.loc[("john", pair, "rho"), "estimate"], 1.0, places=8)
            self.assertAlmostEqual(t.loc[("john", pair, "phi"), "estimate"], 0.8, places=8)
            self.assertAlmostEqual(t.loc[("john", pair, "tau"), "estimate"], 0.8 * (1 - lam), places=8)

    def test_phi_reads_calibrated_values_and_lambda_does_not_care(self):
        frame = methods_beliefs(self.lam, noise=0.0, n=300, shrink=0.6)
        t = table(frame, "direct", resamples=5)
        self.assertAlmostEqual(t.loc[("john", "S4", "lambda"), "estimate"], 0.5, places=8)
        self.assertAlmostEqual(t.loc[("john", "S4", "phi"), "estimate"], 0.8, places=8)
        self.assertAlmostEqual(t.loc[("john", "S4", "beta_D"), "estimate"], 0.8, places=8)  # raw x and y shrink alike

    def test_phi_is_blanked_when_the_second_run_is_noise(self):
        t = table(methods_beliefs(self.lam, n=200, replicate_noise_only=True), "direct", resamples=200)
        row = t.loc[("john", "S4", "phi")]
        self.assertTrue(np.isnan(row.estimate))
        self.assertIn("rho", row.blank_reason)
        self.assertTrue(np.isfinite(t.loc[("john", "S4", "lambda"), "estimate"]))

    def test_without_a_second_run_there_is_no_rho(self):
        frame = methods_beliefs(self.lam, n=200)
        t = table(frame[frame.replicate == 0], "direct")
        self.assertNotIn(("john", "S4", "rho"), t.index)
        self.assertIn(("john", "S4", "lambda"), t.index)

    def test_runs_without_d_f_fall_back_to_the_shared_d(self):
        frame = methods_beliefs(self.lam, noise=0.0, n=300)
        frame = frame[frame.condition != "D_F"]
        t = table(frame, "direct", resamples=5)
        self.assertAlmostEqual(t.loc[("john", "F", "lambda"), "estimate"], 0.3, places=8)


class FitGroups(unittest.TestCase):
    def test_pooled_groups_follow_the_condition_table(self):
        got = groups("pooled", list(CONDITIONS))
        self.assertEqual(len(got), 3)
        own, john, user = got
        self.assertTrue({"A", "B", "B_F", "B_H", "B_S1"} == set(own))
        self.assertTrue({"D_john", "C_john", "M_S4", "C_F", "M_F"} <= set(john))
        self.assertEqual(set(user), {"C_user", "D_user"})
        self.assertEqual(groups("pooled", ["A", "B", "C_john", "D_john"]),
                         [("A", "B"), ("D_john", "C_john")])


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
