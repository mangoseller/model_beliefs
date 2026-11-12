"""Estimator and cache tests (plan: tests before real data)."""

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
                self.assertAlmostEqual(t.loc[("john", pair, "kappa"), "estimate"], 1.0, places=8)
                self.assertAlmostEqual(t.loc[("john", pair, "tau"), "estimate"], 1 - lam, places=8)
                self.assertAlmostEqual(t.loc[("john", pair, "normalised_lambda_original"), "estimate"],
                                       lam, places=8)

    def test_unbiased_under_compression_shift_and_patient_effect(self):
        """Where the original estimator fails: kappa != 1, flip-like gaps, u depending on risk."""
        t = table(beliefs(0.4, kappa=0.7, alpha=0.3, u_slope=-0.2, flip=True, n=600))
        self.assertAlmostEqual(t.loc[("john", "F", "lambda"), "estimate"], 0.4, places=8)
        self.assertGreater(abs(t.loc[("john", "F", "normalised_lambda_original"), "estimate"] - 0.4), 0.02)

    def test_kappa_and_tau(self):
        t = table(beliefs(0.25, kappa=0.8))
        self.assertAlmostEqual(t.loc[("john", "S4", "kappa"), "estimate"], 0.8, places=8)
        self.assertAlmostEqual(t.loc[("john", "S4", "tau"), "estimate"], 0.8 * 0.75, places=8)

    def test_mean_position_sees_a_uniform_shift_that_lambda_misses(self):
        frame = beliefs(0.0)
        frame.loc[frame.condition == "C_john", ["value", "value_calibrated"]] += 0.5
        t = table(frame)
        self.assertAlmostEqual(t.loc[("john", "S4", "lambda"), "estimate"], 0.0, places=8)
        self.assertGreater(abs(t.loc[("john", "S4", "intercept_C"), "estimate"]), 0.4)

    def test_pair_without_matched_control_reports_tau_and_borrows_kappa(self):
        t = table(beliefs(0.3, kappa=0.8, matched=("S4", "F")))
        self.assertNotIn(("john", "H", "lambda"), t.index)
        self.assertAlmostEqual(t.loc[("john", "H", "tau"), "estimate"], 0.8 * 0.7, places=8)
        self.assertAlmostEqual(t.loc[("contrast", "H_vs_S4", "lambda_borrowed_kappa"), "estimate"], 0.3,
                               places=8)

    def test_contrasts(self):
        t = table(beliefs(0.2, lam_by_pair={"F": 0.6}))
        self.assertAlmostEqual(t.loc[("contrast", "F_minus_H", "diff_lambda"), "estimate"], 0.4, places=8)
        self.assertAlmostEqual(t.loc[("contrast", "user_minus_john", "diff_tau"), "estimate"], 0.0, places=8)

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
        self.assertNotIn(("john", "S4", "kappa"), t.index)  # no independent halves
        probe = beliefs(0.25, method="probe")
        t = table(probe, "probe", regressor=beliefs(0.25, method="latent"))
        self.assertAlmostEqual(t.loc[("john", "S4", "lambda"), "estimate"], 0.25, places=8)

    def test_clip_logit(self):
        self.assertAlmostEqual(clip_logit([0.0])[0], np.log(0.01 / 0.99))
        self.assertAlmostEqual(clip_logit([0.5])[0], 0.0)


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
