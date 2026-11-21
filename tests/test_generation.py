"""Generation wiring: items from the dataset rows, keys, replicates and samples, follow-ups, settings."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from second_order import config as C
from second_order import datasets as DS
from second_order import run_generation as G
from second_order import synthetic as S
from second_order.responses import comprehension, visible_text

CKD_CASES = DS.case_ids("ckd")[:3]
BESPOKE_CASES = DS.case_ids("bespoke")[:2]


class TempCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = G.C.CACHE
        G.C.CACHE = Path(self.tmp.name)

    def tearDown(self):
        G.C.CACHE = self.saved
        self.tmp.cleanup()


class CacheIntegrity(TempCache):
    def test_incomplete_tail_is_recovered_and_interior_corruption_flagged(self):
        cache = G.Cache("m")
        cache.append([{"key": "a", "ok": True}])
        with cache.path.open("ab") as f:
            f.write(b'{"key":')
        cache.refresh()
        cache.append([{"key": "b", "ok": True}])
        self.assertEqual(set(G.Cache("m").records), {"a", "b"})
        with cache.path.open("ab") as f:
            f.write(b'broken\n{"key":"c"}\n')
        with self.assertRaisesRegex(ValueError, "corrupt cache.*byte"):
            cache.refresh()

    def test_failures_are_reported_retried_and_short_results_are_rejected(self):
        item = G.Item("synthetic", "case", "C", "probability", 0, "prompt", dataset="bespoke")
        with patch.dict(G.BACKENDS, synthetic=lambda *args: []):
            with self.assertRaisesRegex(ValueError, "0 records for 1 requests"):
                G.run([item])
        with patch.dict(G.BACKENDS, synthetic=lambda *args: [{"ok": False, "text": ""}]):
            self.assertEqual(G.run([item])["failed_call_fraction"], 1.0)
            self.assertEqual(G.pending([item]), [item])
        with patch.dict(G.BACKENDS, synthetic=lambda *args: [{"ok": True, "text": "answer"}]):
            self.assertEqual(G.run([item])["successful"], 1)
            self.assertFalse(G.pending([item]))

    def test_one_parsed_of_one_hundred(self):
        import pandas as pd
        from second_order.responses import retention
        frame = pd.DataFrame(dict(model="m", condition="C", dataset="ckd", output="probability/normal",
                                  status=["parsed", "call_error"] + ["not_generated"] * 98))
        summary = retention(frame).iloc[0]
        self.assertEqual(summary.retention, .01)
        self.assertEqual(summary.failed_call_fraction, .5)
        self.assertEqual(summary.missing_fraction, .98)

class Keys(unittest.TestCase):
    def test_keys(self):
        old = hashlib.sha256(json.dumps(["m", "ckd", "case", "B", "likert/flip", 1]).encode()).hexdigest()
        self.assertEqual(G.item_key("m", "ckd", "case", "B", "likert/flip", 1), old)
        self.assertEqual(G.item_key("m", "ckd", "case", "B", "likert/flip", 1, 0), old)
        self.assertNotEqual(G.item_key("m", "ckd", "case", "B", "likert/flip", 1, 3), old)

    def test_multi_turn_items_hash_the_whole_conversation(self):
        single = G.Item("m", "c", "C", G.FOLLOWUP, 0, "q", dataset="bespoke")
        multi = G.Item("m", "c", "C", G.FOLLOWUP, 0, "q", dataset="bespoke",
                       messages=(("user", "p"), ("assistant", "r"), ("user", "q")))
        self.assertNotEqual(single.prompt_sha, multi.prompt_sha)
        self.assertEqual(single.key, multi.key)
        self.assertEqual([m["role"] for m in multi.chat()], ["user", "assistant", "user"])


class Items(unittest.TestCase):
    def test_ckd_items_are_the_rows_verbatim(self):
        items = G.experiment_items("synthetic", "ckd", CKD_CASES)
        rows = DS.rows("ckd")
        self.assertEqual(len(items), len(rows[rows.case_id.isin(CKD_CASES)]))
        self.assertEqual(len({i.key for i in items}), len(items))
        for item in items[:200]:
            self.assertEqual(item.prompt, DS.row("ckd", item.case_id, item.condition, item.output, item.replicate)["prompt"])
        self.assertEqual({i.replicate for i in items if i.condition == "A"}, {0, 1})
        self.assertEqual({i.replicate for i in items if i.condition == "C"}, {0})

    def test_bespoke_samples_and_second_runs(self):
        k = DS.samples("bespoke")
        items = G.experiment_items("synthetic", "bespoke", BESPOKE_CASES)
        rows = DS.rows("bespoke")
        n_rows = rows.case_id.isin(BESPOKE_CASES)
        extra = rows[n_rows].condition.map(lambda c: DS.replicates("bespoke", c)).sum()
        self.assertEqual(len(items), extra * k)
        self.assertEqual({i.replicate for i in items if i.condition == "B_F"}, {0, 1})
        self.assertEqual({i.sample for i in items}, set(range(k)))
        self.assertEqual(len({i.key for i in items}), len(items))

    def test_case_selection(self):
        self.assertEqual(len(G.dataset_cases("ckd", "7")), 7)
        self.assertEqual(G.dataset_cases("ckd", "7"), G.dataset_cases("ckd", "7"))
        self.assertEqual(G.dataset_cases("bespoke", "7"), DS.case_ids("bespoke"))  # integers select CKD only
        self.assertEqual(len(G.dataset_cases("ckd")), 150)


class Comprehension(TempCache):
    def test_follow_ups_wait_for_and_replay_the_c_answer(self):
        items, missing = G.comprehension_items("synthetic", "bespoke", BESPOKE_CASES)
        self.assertEqual(items, [])
        parents = [i for i in G.experiment_items("synthetic", "bespoke", BESPOKE_CASES, ["C", "C_F", "C_ctrl_F"])
                   if i.replicate == 0]
        self.assertEqual(missing, len(parents))
        G.Cache("synthetic").append([{"key": p.key, "prompt_sha": p.prompt_sha, "text": f"answer {p.case_id}",
                                      "settings_sha": G.settings_signature("synthetic", "bespoke"),
                                      "ok": True} for p in parents])
        items, missing = G.comprehension_items("synthetic", "bespoke", BESPOKE_CASES)
        self.assertEqual((missing, len(items)), (0, len(parents)))
        for item, parent in zip(items, parents):
            self.assertEqual(item.output, G.FOLLOWUP)
            self.assertEqual(item.messages[:2], (("user", parent.prompt), ("assistant", f"answer {parent.case_id}")))
            self.assertEqual(item.messages[2][1], DS.row("bespoke", parent.case_id, parent.condition,
                                                         "probability")["comprehension_q"])
        self.assertEqual(G.comprehension_items("synthetic", "ckd", CKD_CASES), ([], 0))  # CKD checks are rows

    def test_followup_results_keep_blocked_and_missing_children_in_trial_order(self):
        args = ("synthetic", "bespoke", BESPOKE_CASES[:1])
        plan = G.followup_plan(*args)
        parents = [parent for parent, _ in plan]
        cache = G.Cache("synthetic")
        signature = G.settings_signature(*args[:2])

        def record(item, text, ok):
            return {"key": item.key, "prompt_sha": item.prompt_sha, "settings_sha": signature, "text": text, "ok": ok}

        cache.append([record(parent, "parent text", i != 1) for i, parent in enumerate(parents[:4])])
        success = G.followup_item(parents[2], "parent text")
        failure = G.followup_item(parents[3], "parent text")
        answer = DS.row("bespoke", success.case_id, success.condition, "probability")["comprehension_a"]
        cache.append([record(success, answer, True), record(failure, "", False)])
        frame = comprehension(*args, write=False)
        self.assertEqual(list(zip(frame.case_id, frame.condition, frame.replicate, frame["sample"])),
                         [(p.case_id, p.condition, p.replicate, p.sample) for p in parents])
        self.assertEqual(frame.status.to_list(), ["not_generated", "not_generated", "parsed", "call_error"]
                         + ["not_generated"] * (len(parents) - 4))
        self.assertEqual(frame.iloc[2].correct, 1.0)
        self.assertEqual(frame.iloc[2].text, answer)

    def test_changed_prompt_or_settings_are_refused(self):
        item = G.experiment_items("synthetic", "ckd", CKD_CASES[:1], ["B"], ("probability/normal",))[0]
        G.Cache("synthetic").append([{"key": item.key, "prompt_sha": "other", "text": "x", "ok": True}])
        with self.assertRaises(ValueError):
            G.lookup([item])

    def test_settings_check(self):
        a, b = G.experiment_items("synthetic", "ckd", CKD_CASES[:1], ["B"], ("probability/normal", "likert/flip"))[:2]
        G.Cache("synthetic").append([
            {"key": a.key, "prompt_sha": a.prompt_sha, "text": "x", "ok": True,
             "settings_sha": G.settings_signature("synthetic", "ckd")},
            {"key": b.key, "prompt_sha": b.prompt_sha, "text": "y", "ok": True, "settings_sha": "stale"}])
        self.assertEqual(G.lookup([a])[0]["text"], "x")
        with self.assertRaises(ValueError):
            G.lookup([b])


class Synthetic(unittest.TestCase):
    def test_every_answer_parses_and_is_deterministic(self):
        for dataset, cases in (("ckd", CKD_CASES), ("bespoke", BESPOKE_CASES)):
            items = G.experiment_items("synthetic", dataset, cases)
            a, b = G.generate_synthetic("synthetic", items, 1), G.generate_synthetic("synthetic", items, 1)
            self.assertEqual([r["text"] for r in a], [r["text"] for r in b])
            for item, record in zip(items, a):
                value, _ = DS.parse(dataset, item.output, item.prompt, visible_text(record["text"]))
                self.assertIsNotNone(value, (item.output, record["text"]))

    def test_believed_logits_follow_the_methods_model(self):
        t = S.truth("synthetic")
        for case_id in BESPOKE_CASES:
            get = lambda c, cid=case_id: S.believed_logit("synthetic", "bespoke", DS.row("bespoke", cid, c, "probability"))  # noqa: E731
            norm = lambda c, cid=case_id: float(DS.row("bespoke", cid, c, "probability")["normative_logodds"])  # noqa: E731
            self.assertAlmostEqual(get("A"), norm("A"))
            self.assertAlmostEqual(get("M_F"), t["intercept"] + t["phi"] * norm("M_F"))
            leak = t["intercept"] + t["phi"] * (norm("M") + t["lambda_partial"] * (norm("A") - norm("M")))
            self.assertAlmostEqual(get("C"), leak, places=3)  # full_leak_p and normative_logodds are rounded

    def test_ckd_records_are_read_from_the_prompt(self):
        meta = DS.row("ckd", CKD_CASES[0], "C", "probability/normal")
        shown = S.records(meta["prompt"])
        self.assertEqual(set(shown), {"model", "person"})
        self.assertGreater(len(shown["model"]), len(shown["person"]))
        self.assertTrue(np.isfinite(S.believed_logit("synthetic", "ckd", meta)))


class Backends(unittest.TestCase):
    def test_qwen_settings(self):
        self.assertEqual(G.template_kwargs("qwen3_8b"), {"enable_thinking": False})
        self.assertEqual(G.template_kwargs("synthetic"), {})
        self.assertEqual(G.sampling("qwen3_14b", "ckd", "bet_4/flip")["max_tokens"], 256)
        self.assertEqual(G.sampling("qwen3_14b", "ckd", "probability/normal")["max_tokens"], 4096)
        self.assertEqual(G.sampling("qwen3_14b", "bespoke", "probability")["max_tokens"], 4096)
        self.assertEqual(G.sampling("qwen3_14b", "ckd", "likert/normal")["temperature"], 1.0)
        for model in C.load()["experiment"]["models"]:
            self.assertIn(C.model_config(model)["backend"], G.BACKENDS)
            self.assertEqual(set(C.datasets_for(model)), {"ckd", "bespoke"})

    def test_item_seeds_differ_per_item(self):
        items = G.experiment_items("synthetic", "bespoke", BESPOKE_CASES[:1], ["A"])
        self.assertEqual(len({G.item_seed(7, i) for i in items}), len(items))


class Thinking(unittest.TestCase):
    def test_think_blocks_are_stripped(self):
        self.assertEqual(visible_text('<think>\n{"probability": 0.9}\n</think>\n\n{"probability": 0.2}'),
                         '{"probability": 0.2}')
        self.assertEqual(visible_text("reasoning</think>\nA"), "A")
        self.assertEqual(visible_text("plain"), "plain")
        self.assertIsNone(visible_text(None))


if __name__ == "__main__":
    unittest.main()
