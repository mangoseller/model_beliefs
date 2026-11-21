"""End to end on the synthetic backend: generation -> parsing -> fits -> beliefs -> estimators -> report.

The synthetic model (``second_order/synthetic.py``) writes response text from
known parameters, so the run must recover them.  Short latent fits and 150
CKD cases keep it to a few minutes; run it on its own:

    python -m unittest tests.test_pipeline
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "bespoke_items.yaml"


class SyntheticPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cfg = yaml.safe_load((ROOT / "configs" / "second_order.yaml").read_text())
        cfg["bespoke"]["samples"] = 30
        cfg["latent_fit"] = {"epochs": 1500, "patience": 100}
        cfg["models"]["synthetic"]["truth"].update({"parse_failure_rate": 0.01, "think_tags": True})
        cls.truth = cfg["models"]["synthetic"]["truth"]
        (tmp / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        env = {**os.environ, "SO_CONFIG": str(tmp / "config.yaml"), "SO_DATA_DIR": str(tmp / "data"),
               "SO_RESULTS_DIR": str(tmp / "results"), "SO_FIGURES_DIR": str(tmp / "figures"),
               "SO_BESPOKE_ITEMS": str(FIXTURE), "PYTHONPATH": str(ROOT)}
        steps = [["second_order.run_generation", "--model", "synthetic", "--cases", "150", "--approve"],
                 ["second_order.phase3", "--model", "synthetic", "--cases", "150"],
                 ["second_order.curse", "--models", "synthetic", "--resamples", "100"],
                 ["second_order.figures"],
                 # the deprecated RQ analysis still runs on current outputs
                 ["second_order.deprecated.analyze", "--models", "synthetic", "--resamples", "20"],
                 ["second_order.deprecated.rq_figures"]]
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

    def stat(self, dataset, method, setting, statistic, case_set="all"):
        e = self.est
        row = e[(e.dataset == dataset) & (e.method == method) & (e.setting == setting)
                & (e.statistic == statistic) & (e.case_set == case_set)]
        self.assertEqual(len(row), 1, (dataset, method, setting, statistic))
        return row.iloc[0]

    def test_lambda_is_recovered_for_every_dataset_method_and_setting(self):
        for dataset, method in (("ckd", "latent"), ("ckd", "direct"), ("bespoke", "direct")):
            for setting in ("partial", "false"):
                r = self.stat(dataset, method, setting, "lambda")
                self.assertAlmostEqual(r.estimate, self.truth[f"lambda_{setting}"], delta=0.2,
                                       msg=(dataset, method, setting))

    def test_phi_and_tau_are_recovered_on_ckd(self):
        for method in ("latent", "direct"):
            for setting in ("partial", "false"):
                phi = self.stat("ckd", method, setting, "phi")
                self.assertAlmostEqual(phi.estimate, self.truth["phi"], delta=0.25, msg=(method, setting))
                tau = self.stat("ckd", method, setting, "tau")
                lam = self.truth[f"lambda_{setting}"]
                self.assertAlmostEqual(tau.estimate, self.truth["phi"] * (1 - lam), delta=0.25)
                self.assertTrue(0 < self.stat("ckd", method, setting, "rho").estimate <= 1.05)

    def test_comprehension_and_placebo_outputs(self):
        comp = pd.read_csv(self.results / "comprehension.csv")
        self.assertEqual(set(comp.dataset), {"ckd", "bespoke"})
        for _, r in comp.iterrows():
            self.assertAlmostEqual(r.accuracy, self.truth["comprehension_accuracy"], delta=0.1)
        self.assertIn("comprehension_pass", set(self.est.case_set))
        placebo = pd.read_csv(self.results / "placebo.csv")
        self.assertEqual(set(placebo.case_id), {"bespoke__placebo_01", "bespoke__placebo_02"})
        self.assertEqual(len(placebo), 2 * 2 * 3)
        bayes = pd.read_csv(self.results / "bayes.csv")
        own = bayes[(bayes.beliefs == "own") & (bayes.condition == "all")].iloc[0]
        self.assertAlmostEqual(own.slope_on_bayes, 1.0, delta=0.15)  # the simulated model is Bayesian

    def test_deprecated_rq_analysis_still_runs(self):
        leak = pd.read_csv(self.results.parent / "leakage.csv")
        lam = leak[(leak.method == "direct") & (leak.framing == "john") & (leak.pair == "S4")
                   & (leak.statistic == "lambda") & (leak.case_set == "all_complete")]
        self.assertAlmostEqual(lam.estimate.iloc[0], self.truth["lambda_partial"], delta=0.2)
        self.assertTrue((self.figures / "rq1c_pairs.png").exists())

    def test_report_figures_and_retention(self):
        text = (self.results / "RESULTS.md").read_text()
        for heading in ("Leakage, responsiveness", "placebo", "Comprehension", "Anomalies"):
            self.assertIn(heading, text)
        for name in ("curse_estimates", "curse_scatter"):
            self.assertTrue((self.figures / f"{name}.png").exists(), name)
        ret = pd.read_csv(self.results / "retention.csv")
        # Only the planted failures (1% of responses) are lost: counted, never imputed.  A CKD call
        # is complete only if all 16 outputs parse, so its retention is about 0.99 ** 16.
        rate = ret.parse_failures.sum() / ret.responses.sum()
        self.assertAlmostEqual(rate, self.truth["parse_failure_rate"], delta=0.004)
        self.assertEqual(int(ret.call_errors.sum()), 0)
        ckd = ret[ret.dataset == "ckd"]
        self.assertAlmostEqual(ckd.cases_complete.sum() / ckd.cases_generated.sum(), 0.99**16, delta=0.04)


if __name__ == "__main__":
    unittest.main()
