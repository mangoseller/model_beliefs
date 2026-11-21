"""Generation wiring: item keys, replicates and samples, comprehension follow-ups, backends' settings."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

FIXTURE = str(Path(__file__).parent / "fixtures" / "bespoke_items.yaml")
os.environ.setdefault("SO_BESPOKE_ITEMS", FIXTURE)

from latent_beliefs.ckd.parse import parse_response  # noqa: E402
from second_order import config as C  # noqa: E402
from second_order import datasets as DS  # noqa: E402
from second_order import run_generation as G  # noqa: E402
from second_order import synthetic as S  # noqa: E402
from second_order.comprehension import OUTPUT, parse_choice  # noqa: E402
from second_order.prompts_second_order import OUTPUTS, cases  # noqa: E402
from second_order.responses import visible_text  # noqa: E402

CURSE = list(C.load()["experiment"]["conditions"])
CKD_CASES = list(cases().index[:3])
BESPOKE_CASES = DS.case_ids("bespoke")[:2]


class TempCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = G.C.CACHE
        G.C.CACHE = Path(self.tmp.name)

    def tearDown(self):
        G.C.CACHE = self.saved
        self.tmp.cleanup()


class Keys(unittest.TestCase):
    def test_keys_without_a_sample_are_unchanged(self):
        old = hashlib.sha256(json.dumps(["m", "case", "B", "likert", 1]).encode()).hexdigest()
        self.assertEqual(G.item_key("m", "case", "B", "likert", 1), old)
        self.assertEqual(G.item_key("m", "case", "B", "likert", 1, 0), old)
        self.assertNotEqual(G.item_key("m", "case", "B", "likert", 1, 3), old)

    def test_multi_turn_items_hash_the_whole_conversation(self):
        single = G.Item("m", "c", "C_john", OUTPUT, 0, "q")
        multi = G.Item("m", "c", "C_john", OUTPUT, 0, "q", messages=(("user", "p"), ("assistant", "r"), ("user", "q")))
        self.assertNotEqual(single.prompt_sha, multi.prompt_sha)
        self.assertEqual(single.key, multi.key)
        self.assertEqual(multi.chat(), [{"role": "user", "content": "p"}, {"role": "assistant", "content": "r"},
                                        {"role": "user", "content": "q"}])
        self.assertEqual(single.chat(), [{"role": "user", "content": "q"}])


class Items(unittest.TestCase):
    def test_replicates_and_samples(self):
        ckd = G.experiment_items("synthetic", "ckd", CKD_CASES, CURSE)
        reps = {c: C.replicates(c) for c in CURSE}
        self.assertEqual(len(ckd), len(CKD_CASES) * len(OUTPUTS) * sum(reps.values()))
        self.assertEqual({i.replicate for i in ckd if i.condition == "A"}, {0, 1})
        self.assertEqual({i.replicate for i in ckd if i.condition == "C_john"}, {0})
        self.assertEqual(len({i.key for i in ckd}), len(ckd))
        k = DS.samples("bespoke")
        bes = G.experiment_items("synthetic", "bespoke", BESPOKE_CASES, CURSE)
        self.assertEqual(len(bes), len(BESPOKE_CASES) * k * sum(reps.values()))
        self.assertEqual({i.sample for i in bes}, set(range(k)))
        self.assertEqual(len({i.key for i in bes}), len(bes))
        self.assertTrue(all(i.dataset == "bespoke" and i.output == "probability" for i in bes))

    def test_bespoke_keeps_only_the_two_settings(self):
        self.assertEqual(DS.conditions_for("bespoke", ["A", "B", "B_H", "C_H", "C_user", "D_F"]), ["A", "B", "D_F"])

    def test_ckd_prompts_use_the_models_own_belief_layout(self):
        item = G.experiment_items("qwen3_8b", "ckd", CKD_CASES[:1], ["A"], ("probability",))[0]
        self.assertTrue(item.prompt.startswith("Patient profile:\n"))
        item = G.experiment_items("gemma3_4b", "ckd", CKD_CASES[:1], ["A"], ("probability",))[0]
        self.assertTrue(item.prompt.startswith("Estimate the probability"))


class Comprehension(TempCache):
    def test_follow_ups_wait_for_and_replay_the_c_answer(self):
        items, missing = G.comprehension_items("synthetic", "ckd", CKD_CASES, CURSE)
        self.assertEqual(items, [])
        self.assertEqual(missing, len(CKD_CASES) * 2)  # C_john and C_F, numeric probability
        parents = G.experiment_items("synthetic", "ckd", CKD_CASES, ["C_john", "C_F"], ("probability",))
        G.Cache("synthetic").append([{"key": p.key, "prompt_sha": p.prompt_sha, "text": f"answer {p.case_id}",
                                      "ok": True} for p in parents])
        items, missing = G.comprehension_items("synthetic", "ckd", CKD_CASES, CURSE)
        self.assertEqual(missing, 0)
        self.assertEqual(len(items), len(parents))
        for item, parent in zip(items, parents):
            self.assertEqual(item.output, OUTPUT)
            self.assertEqual(item.messages[0], ("user", parent.prompt))
            self.assertEqual(item.messages[1], ("assistant", f"answer {parent.case_id}"))
            self.assertEqual(item.messages[2][1], DS.comprehension("ckd", parent.case_id, parent.condition).question)
        # only the scheduled C conditions get follow-ups
        self.assertEqual(G.comprehension_items("synthetic", "ckd", CKD_CASES, ["A", "C_F"])[0][0].condition, "C_F")

    def test_changed_prompt_is_refused(self):
        item = G.experiment_items("synthetic", "ckd", CKD_CASES[:1], ["B"], ("probability",))[0]
        G.Cache("synthetic").append([{"key": item.key, "prompt_sha": "other", "text": "x", "ok": True}])
        with self.assertRaises(ValueError):
            G.lookup([item])


class Synthetic(unittest.TestCase):
    def test_every_output_parses_and_is_deterministic(self):
        items = G.experiment_items("synthetic", "ckd", CKD_CASES, CURSE)
        a, b = G.generate_synthetic("synthetic", items, 1), G.generate_synthetic("synthetic", items, 1)
        self.assertEqual([r["text"] for r in a], [r["text"] for r in b])
        for item, record in zip(items, a):
            value, _ = parse_response(item.output, visible_text(record["text"]))
            self.assertIsNotNone(value, (item.output, record["text"]))

    def test_one_belief_per_call(self):
        """All 16 outputs of one call come from the same drawn belief; runs differ."""
        items = [i for i in G.experiment_items("synthetic", "ckd", CKD_CASES[:1], ["A"]) if i.output == "likert"]
        texts = {i.replicate: S.respond("synthetic", i, 1) for i in items}
        self.assertEqual(len(texts), 2)

    def test_believed_logits_follow_the_methods_model(self):
        t = S.truth("synthetic")
        for cid in BESPOKE_CASES:
            own = {s: S.own_logit("bespoke", cid, s) for s in ("full", "partial", "false")}
            get = lambda c: S.believed_logit("synthetic", "bespoke", cid, c)  # noqa: E731
            self.assertAlmostEqual(get("A"), own["full"])
            self.assertAlmostEqual(get("B_F"), own["false"])
            self.assertAlmostEqual(get("D_F"), t["intercept"] + t["phi"] * own["full"])
            self.assertAlmostEqual(get("M_S4"), t["intercept"] + t["phi"] * own["partial"])
            lam = t["lambda_false"]
            self.assertAlmostEqual(get("C_F"), t["intercept"] + t["phi"] * (own["false"] + lam * (own["full"] - own["false"])))

    def test_ckd_own_belief_uses_cohort_means_for_withheld_fields(self):
        cid = CKD_CASES[0]
        self.assertNotAlmostEqual(S.own_logit("ckd", cid, "full"), S.own_logit("ckd", cid, "false"))
        self.assertTrue(np.isfinite(S.own_logit("ckd", cid, "partial")))

    def test_comprehension_answers_parse(self):
        parent = G.experiment_items("synthetic", "bespoke", BESPOKE_CASES[:1], ["C_john"])[0]
        q = DS.comprehension("bespoke", parent.case_id, "C_john")
        item = G.Item("synthetic", parent.case_id, "C_john", OUTPUT, 0, q.question, 0, "bespoke",
                      (("user", parent.prompt), ("assistant", "r"), ("user", q.question)))
        self.assertIsNotNone(parse_choice(S.respond("synthetic", item, 1), q.options))


class Backends(unittest.TestCase):
    def test_qwen_settings(self):
        self.assertEqual(G.template_kwargs("qwen3_8b"), {"enable_thinking": False})
        self.assertEqual(G.template_kwargs("gemma3_4b"), {})
        s = G.sampling("qwen3_14b")
        self.assertEqual((s["temperature"], s["max_tokens"]), (1.0, 4096))
        for model in C.load()["experiment"]["models"]:
            self.assertIn(C.model_config(model)["backend"], G.BACKENDS)
            self.assertEqual(C.scheduled_conditions(model), CURSE)
            self.assertEqual(C.self_layout(model), "matched")
        self.assertEqual(G.estimate("qwen3_8b", 10, CURSE)["usd"], 0.0)

    def test_item_seeds_differ_per_item(self):
        items = G.experiment_items("synthetic", "bespoke", BESPOKE_CASES[:1], ["A"])
        self.assertEqual(len({G.item_seed(7, i) for i in items}), len(items))


class Thinking(unittest.TestCase):
    def test_think_blocks_are_stripped(self):
        self.assertEqual(visible_text('<think>\n{"probability": 0.9}\n</think>\n\n{"probability": 0.2}'),
                         '{"probability": 0.2}')
        self.assertEqual(visible_text('reasoning</think>\nChoice: A'), "Choice: A")
        self.assertEqual(visible_text("plain"), "plain")
        self.assertIsNone(visible_text(None))


if __name__ == "__main__":
    unittest.main()
