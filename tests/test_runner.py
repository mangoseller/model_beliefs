"""Runner helpers: inference tuning and smoke checks, activation positions, progress helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from second_order import datasets as DS
from second_order import freeze as F
from second_order import tune as T
from second_order.responses import score_answer
from second_order.run_generation import FOLLOWUP, Item
from second_order.runner import last_line


def trial(tok_s: float, parse: float = 0.95, truncated: float = 0.0, seqs: int = 256, ok: bool = True) -> dict:
    return {"engine": {"max_num_seqs": seqs}, "ok": ok, "output_tokens_per_second": tok_s,
            "smoke": {"parse_rate": parse, "truncated_rate": truncated, "mean_output_tokens": 300,
                      "groups": {"ckd/choice": {"parse_rate": parse, "truncated_rate": truncated}}}}


class Tuning(unittest.TestCase):
    def test_the_fastest_successful_trial_wins(self):
        s = T.settings()
        out = T.choose("m", 100, [trial(1000), trial(1800, seqs=512), {"engine": {}, "ok": False}], s)
        self.assertEqual(out["engine"], {"max_num_seqs": 512})
        self.assertEqual(out["batch_size"], 2048)
        self.assertEqual(out["speedup_over_first"], 1.8)
        self.assertNotIn("error", out)
        fast = trial(2000, seqs=1024)
        fast["smoke"]["groups"]["bespoke/followup"] = {"parse_rate": 0.1, "truncated_rate": 0.0}
        self.assertEqual(T.choose("m", 100, [fast, trial(900)], s)["output_tokens_per_second"], 900)

    def test_smoke_checks_stop_the_run(self):
        s = T.settings()
        self.assertIn("parse rate", T.choose("m", 100, [trial(1000, parse=0.5)], s)["error"])
        self.assertIn("truncated", T.choose("m", 100, [trial(1000, truncated=0.3)], s)["error"])
        self.assertEqual(T.choose("m", 100, [], s)["error"], "every engine configuration failed")

    def test_benchmark_is_a_seeded_sample_of_real_calls(self):
        a = T.benchmark_items("synthetic", 200, 1)
        self.assertEqual(a, T.benchmark_items("synthetic", 200, 1))
        self.assertAlmostEqual(len(a), 200, delta=2)
        self.assertTrue(all(i["replicate"] == 0 and i["sample"] == 0 for i in a))
        datasets = {i["dataset"] for i in a}
        self.assertEqual(datasets, {"ckd", "bespoke"})
        Item(**a[0])  # the trial process rebuilds items from these dicts

    def test_smoke_stats(self):
        items = [Item(**d) for d in T.benchmark_items("synthetic", 40, 2)]
        records = [{"text": 'Reasoning.\n{"probability": 0.3}' if i.output.startswith("probability") else "A",
                    "stop_reason": "stop", "usage": {"output_tokens": 10}} for i in items]
        stats = T.smoke_stats(items, records)
        self.assertEqual(stats["calls"], len(items))
        self.assertEqual((stats["parse_rate"], stats["truncated_rate"]), (1.0, 0.0))
        self.assertEqual(stats["by_output"]["bespoke/probability"], 1.0)
        bad = T.smoke_stats(items, [{"text": "no answer", "stop_reason": "length"} for _ in items])
        self.assertEqual((bad["parse_rate"], bad["truncated_rate"]), (0.0, 1.0))


class AnswerScoring(unittest.TestCase):
    def test_wrong_followup_is_parsed_and_fallback_probability_is_excluded(self):
        followup = Item("m", "c", "C", FOLLOWUP, 0, "What was she told?", dataset="bespoke")
        meta = {"comprehension_q": followup.prompt, "comprehension_a": "Draw 1: blue."}
        wrong = {"text": "Draw 1: red.", "ok": True}
        score = score_answer(followup, wrong, meta)
        self.assertEqual((score.status, score.value), ("parsed", 0.0))

        probability = Item("m", "c", "C", "probability", 0, "Report a probability.", dataset="bespoke")
        fallback = {"text": '"probability": "22%"', "ok": True}
        score = score_answer(probability, fallback)
        self.assertEqual((score.status, score.value, score.fallback_value), ("fallback", None, .22))
        with patch.object(DS, "row", return_value=meta):
            stats = T.smoke_stats([followup, probability], [wrong, fallback])
        self.assertEqual(stats["groups"]["bespoke/followup"]["parse_rate"], 1.0)
        self.assertEqual(stats["groups"]["bespoke/probability"]["parse_rate"], 0.0)

    def test_check_parseability_needs_only_the_prompt_and_correctness_uses_the_key(self):
        item = Item("m", "case", "C", "check/normal", 0, "A. Yes\nB. No")
        record = {"ok": True, "text": "B"}
        score = score_answer(item, record, {"check_answer": "A"})
        self.assertEqual((score.status, score.value), ("parsed", 0.0))
        stats = T.smoke_stats([item], [record])
        self.assertEqual(stats["parse_rate"], 1.0)

    def test_truncated_answer_keeps_extracted_value_but_fails_tuning(self):
        item = Item("m", "c", "A", "probability/normal", 0, "Report a probability.")
        record = {"text": '{"probability": 0.22}', "stop_reason": "length", "ok": True}
        score = score_answer(item, record)
        self.assertEqual((score.status, score.parse_method, score.value), ("truncated", "json", .22))
        stats = T.smoke_stats([item], [record])
        self.assertEqual((stats["parse_rate"], stats["truncated_rate"]), (0.0, 1.0))


class AnchoredExport(unittest.TestCase):
    def test_withheld_decision_cannot_change_its_predictor(self):
        import pandas as pd
        from latent_beliefs.latent.model import OutputSpec, ProbabilisticLatentModel
        from second_order.fit_pooled import posteriors

        specs = [OutputSpec("probability/normal", "probability"), OutputSpec("decision/normal", "binary"),
                 OutputSpec("decision/flip", "binary")]
        model = ProbabilisticLatentModel(specs, quadrature_points=32)
        obs = pd.DataFrame({"probability/normal": [.35] * 3, "decision/normal": [0, 1, np.nan],
                            "decision/flip": [0, 1, np.nan]})
        post = posteriors(model, obs, batch_size=1)
        np.testing.assert_allclose(post.z_without_decision, post.z_without_decision.iloc[0])
        self.assertGreater(post.z_mean.iloc[1], post.z_mean.iloc[0])
        np.testing.assert_allclose(post, posteriors(model, obs, batch_size=10))

    def test_roundtrip_reproduces_curves_and_distinguishes_cutoffs(self):
        from latent_beliefs.latent.model import OutputSpec, restore_model
        from second_order.fit_pooled import AnchoredLatentModel, parameter_frame
        from second_order.responses import cutoff

        specs = [OutputSpec("probability/normal", "probability"),
                 OutputSpec("threshold_0.5/normal", "binary", cutoff("threshold_0.5")),
                 OutputSpec("bet_0.5/normal", "binary", cutoff("bet_0.5"))]
        model = AnchoredLatentModel(specs, quadrature_points=32)
        with torch.no_grad():
            model.binary_raw_slopes[specs[1].name].fill_(2.7)
        params = parameter_frame(model)
        restored = restore_model(specs, params, quadrature_points=32, probability_epsilon=model.probability_epsilon)
        table = params.set_index(["output_name", "parameter"]).value
        self.assertEqual(table[specs[1].name, "printed_threshold"], .667)
        self.assertAlmostEqual(table[specs[1].name, "payoff_threshold"], 2/3)
        for spec in specs[1:]:
            torch.testing.assert_close(restored.binary_logits(spec), model.binary_logits(spec))
            self.assertAlmostEqual(table[spec.name, "implied_threshold"], spec.nominal_threshold)


class Freeze(unittest.TestCase):
    def test_reuse_checks_order_response_positions_revision_and_array(self):
        class Replay:
            layers = hidden = 1
            kwargs = {}
            def tokens_and_positions(self, item, record):
                return [1, *record["token_ids"]], [-1, 0, 0], 1
            def run(self, ids, index):
                return np.full((3, 1, 1), ids[-1], dtype=np.uint16)

        items = [Item("m", c, "C", "probability", 0, "prompt", dataset="bespoke") for c in ["a", "b"]]
        records = [{"text": "answer", "token_ids": [i], "prompt_token_ids": [1], "ok": True,
                    "settings_sha": "same"} for i in (2, 3)]
        replay = Replay()
        config = {"backend": "synthetic", "path": "weights", "revision": "first"}
        with tempfile.TemporaryDirectory() as folder, patch.object(F.C, "ACTIVATIONS", Path(folder)), \
             patch.object(F.C, "model_config", return_value=config), patch.object(F, "freeze_items", return_value=items), \
             patch.object(F, "lookup", return_value=records), patch.object(replay, "run", wraps=replay.run) as run:
            def freeze():
                return F.freeze("m", "bespoke", [], None, replay)
            output = freeze()
            freeze()
            self.assertEqual(run.call_count, 2)
            items.reverse()
            records.reverse()
            freeze()
            records[0]["text"] = "revised"
            freeze()
            config["revision"] = "second"
            freeze()
            with (output / "acts.npy").open("r+b") as f:
                f.seek(-1, 2)
                f.write(b"\xff")
            freeze()
            self.assertEqual(run.call_count, 10)
            _, index, meta = F.load(output)
            self.assertEqual(list(index.item_key), [i.key for i in items])
            self.assertEqual(meta["revision"], "second")

    def test_positions(self):
        offsets = [(0, 3), (3, 5), (5, 5), (5, 9)]
        self.assertEqual(F.token_at(offsets, 4), 1)
        self.assertEqual(F.token_at(offsets, 9), 3)
        self.assertEqual(F.token_at(offsets, 0), -1)
        merged = [(0, 4), (4, 7), (7, 12)]  # e.g. a ' {"' token that starts before the answer's brace
        self.assertEqual(F.token_before(merged, 5), 0)
        self.assertEqual(F.token_before(merged, 7), 1)
        text = 'thinking {"probability": 0.2} wait {"probability": 0.3}'
        self.assertEqual(F.answer_start("probability", text), text.rindex("{"))
        self.assertEqual(F.answer_start("probability/normal", text), text.rindex("{"))
        self.assertEqual(F.answer_start("probability", "no answer"), -1)
        self.assertEqual(F.answer_start("likert/flip", "B"), 0)

    def test_evidence_end_is_just_before_the_question(self):
        for dataset, case_id in (("ckd", DS.case_ids("ckd")[0]), ("bespoke", "b01")):
            items = F.freeze_items("synthetic", dataset, [case_id])
            self.assertTrue(items)
            for item in items:
                end = F.evidence_end(item)
                self.assertGreater(end, 0, item.condition)
                question = item.prompt[end:].strip().split("\n")[0]
                self.assertTrue(question.startswith(("What", "Which")), (item.condition, question))
                self.assertIn(item.output, ("probability/normal", "probability"))


class Progress(unittest.TestCase):
    def test_last_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write("a\n  fit m/pooled/A: epochs=10\nb\n  fit m/pooled/D: epochs=20\nc\n")
        self.assertEqual(last_line(Path(handle.name), "fit "), "fit m/pooled/D: epochs=20")
        self.assertEqual(last_line(Path(handle.name), "absent"), "")
        self.assertEqual(last_line(Path("/nonexistent"), "x"), "")


if __name__ == "__main__":
    unittest.main()
