"""Runner helpers: inference tuning and smoke checks, activation positions, progress helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

FIXTURE = str(Path(__file__).parents[1] / "datasets" / "example_bespoke_items.yaml")
os.environ.setdefault("SO_BESPOKE_ITEMS", FIXTURE)

from second_order import freeze as F  # noqa: E402
from second_order import tune as T  # noqa: E402
from second_order.run_generation import Item  # noqa: E402
from second_order.runner import last_line  # noqa: E402


def trial(tok_s: float, parse: float = 0.95, truncated: float = 0.0, seqs: int = 256, ok: bool = True) -> dict:
    return {"engine": {"max_num_seqs": seqs}, "ok": ok, "output_tokens_per_second": tok_s,
            "smoke": {"parse_rate": parse, "truncated_rate": truncated, "mean_output_tokens": 300}}


class Tuning(unittest.TestCase):
    def test_the_fastest_successful_trial_wins(self):
        s = T.settings()
        out = T.choose("m", 100, [trial(1000), trial(1800, seqs=512), {"engine": {}, "ok": False}], s)
        self.assertEqual(out["engine"], {"max_num_seqs": 512})
        self.assertEqual(out["batch_size"], 2048)
        self.assertEqual(out["speedup_over_first"], 1.8)
        self.assertNotIn("error", out)

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
        records = [{"text": '{"probability": 0.3}' if i.output == "probability" else "Choice: A", "stop_reason": "stop",
                    "usage": {"output_tokens": 10}} for i in items]
        stats = T.smoke_stats(items, records)
        self.assertEqual(stats["calls"], len(items))
        self.assertEqual(stats["truncated_rate"], 0.0)
        self.assertEqual(stats["by_output"]["bespoke/probability"], 1.0)
        bad = T.smoke_stats(items, [{"text": "no answer", "stop_reason": "length"} for _ in items])
        self.assertEqual((bad["parse_rate"], bad["truncated_rate"]), (0.0, 1.0))


class Freeze(unittest.TestCase):
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
        self.assertEqual(F.answer_start("likert", "x\nChoice: A\nmore\nChoice: B"), len("x\nChoice: A\nmore\n"))
        self.assertEqual(F.answer_start("probability", "no answer"), -1)

    def test_evidence_end_is_found_in_every_condition(self):
        items = F.freeze_items("synthetic", "bespoke", ["bespoke__marbles_01"], ["A", "B", "C", "M_F", "D_F"])
        self.assertEqual(len(items), 5)
        for item in items:
            end = F.evidence_end(item)
            self.assertGreater(end, 0)
            self.assertTrue(item.prompt[:end].rstrip().endswith("."), item.condition)
        ckd = F.freeze_items("synthetic", "ckd", ["original500__prompt_0001"], ["A", "C_F"])
        self.assertTrue(all(F.evidence_end(i) > 0 for i in ckd))
        self.assertEqual({i.output for i in ckd}, {"probability"})


class Progress(unittest.TestCase):
    def test_last_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write("a\n  fit m/pooled/A: epochs=10\nb\n  fit m/pooled/D: epochs=20\nc\n")
        self.assertEqual(last_line(Path(handle.name), "fit "), "fit m/pooled/D: epochs=20")
        self.assertEqual(last_line(Path(handle.name), "absent"), "")
        self.assertEqual(last_line(Path("/nonexistent"), "x"), "")


if __name__ == "__main__":
    unittest.main()
