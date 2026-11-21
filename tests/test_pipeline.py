"""End to end on the synthetic backend: generation -> parsing -> fits -> beliefs -> estimators -> report.

The synthetic model (``second_order/synthetic.py``) writes response text from
known parameters, so the run must recover them.  Short latent fits and 100
CKD patients keep it to a few minutes; run it on its own:

    python -m unittest tests.test_pipeline
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]


class SyntheticPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cfg = yaml.safe_load((ROOT / "second_order" / "config.yaml").read_text())
        cfg["datasets"]["bespoke"]["samples"] = 20
        cfg["latent_fit"] = {"epochs": 1500, "patience": 100}
        cfg["models"]["synthetic"]["truth"].update({"parse_failure_rate": 0.01, "think_tags": True})
        cls.truth = cfg["models"]["synthetic"]["truth"]
        (tmp / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        env = {**os.environ, "SO_CONFIG": str(tmp / "config.yaml"), "SO_DATA_DIR": str(tmp / "data"),
               "SO_RESULTS_DIR": str(tmp / "results"), "SO_FIGURES_DIR": str(tmp / "figures"),
               "PYTHONPATH": str(ROOT)}
        steps = [["second_order.run_generation", "--model", "synthetic", "--cases", "100"],
                 ["second_order.phase3", "--model", "synthetic", "--cases", "100"],
                 ["second_order.curse", "--models", "synthetic", "--resamples", "100"],
                 ["second_order.figures"]]
        for step in steps:
            done = subprocess.run([sys.executable, "-m", *step], cwd=ROOT, env=env, capture_output=True, text=True)
            if done.returncode:
                raise AssertionError(f"{step[0]} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
        cls.results = tmp / "results" / "curse"
        cls.figures = tmp / "figures"
        cls.est = pd.read_csv(cls.results / "estimates.csv")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def stat(self, dataset, method, setting, statistic, reference, case_set="all"):
        e = self.est
        row = e[(e.dataset == dataset) & (e.method == method) & (e.setting == setting) & (e.reference == reference)
                & (e.statistic == statistic) & (e.case_set == case_set)]
        self.assertEqual(len(row), 1, (dataset, method, setting, statistic, reference))
        return row.iloc[0]

    def test_lambda_is_recovered_for_every_dataset_method_setting_and_reference(self):
        for dataset, method, reference, delta in (
                ("ckd", "latent", "two_record", 0.25), ("ckd", "latent", "single_record", 0.25),
                ("ckd", "direct", "two_record", 0.5), ("ckd", "direct", "single_record", 0.5),
                ("bespoke", "direct", "single_record", 0.1)):
            for setting in ("partial", "false"):
                r = self.stat(dataset, method, setting, "lambda", reference)
                self.assertAlmostEqual(r.estimate, self.truth[f"lambda_{setting}"], delta=delta,
                                       msg=(dataset, method, reference, setting))
                self.assertTrue(np.isfinite(self.stat(dataset, method, setting, "lambda_fieller", reference).ci_lower))

    def test_phi_and_tau_are_recovered(self):
        for dataset, method in (("ckd", "latent"), ("bespoke", "direct")):
            reference = "two_record" if dataset == "ckd" else "single_record"
            phi = self.stat(dataset, method, "both", "phi", reference)  # shared by the two settings
            self.assertAlmostEqual(phi.estimate, self.truth["phi"], delta=0.2, msg=(dataset, method))
            for setting in ("partial", "false"):
                tau = self.stat(dataset, method, setting, "tau", reference)
                lam = self.truth[f"lambda_{setting}"]
                self.assertAlmostEqual(tau.estimate, self.truth["phi"] * (1 - lam), delta=0.25)
                self.assertTrue(0 < self.stat(dataset, method, setting, "rho", reference).estimate <= 1.1)

    def test_case_sets_and_diagnostics(self):
        self.assertTrue({"all", "comprehension_pass", "order_person_first", "order_own_first"}
                        <= set(self.est.case_set))
        checks = pd.read_csv(self.results / "checks.csv")
        self.assertEqual(set(checks.setting), {"partial", "false"})
        for _, r in checks.iterrows():
            self.assertAlmostEqual(r.accuracy, self.truth["comprehension_accuracy"], delta=0.1)
        swap = pd.read_csv(self.results / "role_swap.csv")
        self.assertIn("probe_pairs", set(swap.kind))
        direction = pd.read_csv(self.results / "direction.csv")
        self.assertIn("diff_lambda_sicker_minus_healthier", set(direction.statistic))
        comp = pd.read_csv(self.results / "comprehension.csv")
        self.assertEqual(set(comp.condition), {"C", "C_F", "C_ctrl_F"})
        for _, r in comp.iterrows():
            self.assertAlmostEqual(r.accuracy, self.truth["comprehension_accuracy"], delta=0.15)
        placebo = pd.read_csv(self.results / "placebo.csv")
        self.assertEqual(set(placebo.case_id), {"p01", "p02"})
        self.assertEqual(len(placebo), 2 * 3)
        control = pd.read_csv(self.results / "report_control.csv")
        ctrl = control[control.gap == "C_ctrl_minus_D"]
        self.assertLess(abs(ctrl.estimate.mean()), 0.2)  # the simulated model tracks who heard what
        bayes = pd.read_csv(self.results / "bayes.csv")
        own = bayes[(bayes.beliefs == "own") & (bayes.condition == "all")].iloc[0]
        self.assertAlmostEqual(own.slope_on_bayes, 1.0, delta=0.15)  # the simulated model is Bayesian

    def test_report_figures_and_retention(self):
        text = (self.results / "RESULTS.md").read_text()
        for heading in ("Leakage and theory of mind", "shared by the settings", "sicker against healthier",
                        "role swaps", "placebo", "report-assignment control", "Anomalies"):
            self.assertIn(heading, text)
        for name in ("curse_estimates", "curse_scatter", "curse_decomposition", "curse_decision_curves",
                     "curse_thresholds", "curse_bayes"):
            self.assertTrue((self.figures / f"{name}.png").exists(), name)
        ret = pd.read_csv(self.results / "retention.csv")
        # Only the planted failures (1% of responses) are lost: counted, never imputed.
        rate = ret.parse_failures.sum() / ret.responses.sum()
        self.assertAlmostEqual(rate, self.truth["parse_failure_rate"], delta=0.004)
        self.assertEqual(int(ret.call_errors.sum()), 0)


if __name__ == "__main__":
    unittest.main()
