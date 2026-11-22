"""The order follow-up changes block order, never facts, and resumes without duplicate inference."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from second_order import config as C, datasets as DS, run_generation as G
from second_order import order_followup as F


class Design(unittest.TestCase):
    def test_reversal_preserves_exact_blocks_and_question(self):
        frame = DS.rows("ckd", ["main"])
        for condition in ["C", "M", "D", "C_F", "M_F", "D_F"]:
            for order in F.ORDERS:
                row = frame[(frame.condition == condition) & (frame.block_order == order)].iloc[0]
                original = row.prompt.split("\n\n")
                other = F.ORDERS[1 - F.ORDERS.index(order)]
                changed = F.transform(row.prompt, other).split("\n\n")
                self.assertEqual(changed[:2], original[:2][::-1])
                self.assertEqual(changed[2:], original[2:])
                self.assertEqual(F.transform("\n\n".join(changed), order), row.prompt)
                if condition.endswith("_F"):
                    neutral = F.transform(row.prompt, other, True)
                    self.assertEqual(neutral, "\n\n".join(changed).replace(F.CUE, ""))
                    self.assertIn("accepts its contents as accurate", neutral)

    def test_complete_battery_and_content_reuse_without_merging_replicates(self):
        cases = DS.case_ids("ckd")[:2]
        trials, items = F.make_plan("synthetic", cases)
        frame = pd.DataFrame(trials)
        for _, part in frame.groupby(["case_id", "setting", "wording", "block_order", "role", "replicate"]):
            beliefs = part[~part.output.str.startswith("check/")]
            self.assertEqual(len(beliefs), 31)
        self.assertTrue(all(r["source"] == "baseline" for r in trials if r["setting"] == "partial" and r["role"] in "AB"))
        keys = frame[(frame.role == "A") & (frame.setting == "false") & (frame.wording == "neutral")].item_key
        self.assertTrue(all(items[k]["source"] == "baseline" for k in keys))
        # Neutral D is partial D (the cue is the only difference): one item.
        for case in cases:
            for output in ["probability/normal", "likert/normal"]:
                rows = frame[(frame.case_id == case) & (frame.output == output) & (frame.role == "D")]
                for order in F.ORDERS:
                    a = rows[(rows.setting == "partial") & (rows.block_order == order)].iloc[0]
                    b = rows[(rows.wording == "neutral") & (rows.block_order == order)].iloc[0]
                    self.assertEqual(a.item_key, b.item_key)
        identities = [(i["case_id"], i["output"], i["replicate"], i["prompt_sha"]) for i in items.values()]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(any(i["replicate"] == 1 and i["source"] == "new" for i in items.values()))

    def test_parser_accepts_reasoned_choices_and_reanalysed_json(self):
        item = G.Item("synthetic", "a", "C", "probability/normal", 0, "q")
        self.assertEqual(F.score(item, {"text": 'Reason\n**{"probability": 0.25}**', "ok": True}, {}).value, 0.25)
        item = G.Item("synthetic", "a", "C", "check/normal", 0, "Q\nA. Yes\nB. No")
        for answer in ["Reason\nChoice: B", "Reason\n### **Choice: B**", "**Choice: A**\nChoice: B"]:
            self.assertEqual(F.score(item, {"text": answer, "ok": True}, {"check_answer": "B"}).value, 1)
        truncated = {"text": "Reason\nChoice: B", "ok": True, "stop_reason": "length"}
        self.assertEqual(F.score(item, truncated, {"check_answer": "B"}).status, "truncated")


class Resume(unittest.TestCase):
    def test_new_calls_only_activation_resume_and_changed_settings_fail(self):
        saved = (C.CONFIG_PATH, C.CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config.yaml").write_bytes(C.BUNDLED_CONFIG.read_bytes())
                F.configure(root)
                item = G.Item("synthetic", "test", "followup_v1__false__neutral__own_first__C",
                              "probability/normal", 0, "prompt")
                data = asdict(item) | dict(key=item.key, prompt_sha=item.prompt_sha, source="new")
                trial = dict(model="synthetic", dataset="ckd", case_id="test", setting="false", wording="neutral",
                             block_order="own_first", role="C", replicate=0, output="probability/normal",
                             item_key=item.key, check_answer="")
                plan = dict(protocol=F.PROTOCOL, models=["synthetic"],
                            settings={"synthetic": G.settings_signature("synthetic", "ckd")},
                            trials=[trial], items={item.key: data})
                plan["digest"] = F.digest(plan)
                F.atomic_json(root / "plan.json", plan)
                F.atomic_json(root / "prepared.json", {"plan_digest": plan["digest"]})
                pd.DataFrame(columns=["item_key", "row"]).to_parquet(root / "baseline_activations_synthetic.parquet")
                F.export(root, "synthetic")
                responses = root / "outputs/synthetic_responses.parquet"
                self.assertEqual(pd.read_parquet(responses).status.tolist(), ["not_generated"])

                calls = []

                def backend(model, items, seed):
                    calls.extend(i.key for i in items)
                    return [dict(ok=True, text='{"probability": 0.3}', prompt_token_ids=[1], token_ids=[2],
                                 stop_reason="stop") for _ in items]

                with patch.dict(G.BACKENDS, synthetic=backend):
                    F.generate(root, "synthetic")
                    F.generate(root, "synthetic")
                self.assertEqual(calls, [item.key])

                captures = []

                class Replay:
                    def __init__(self, model):
                        pass

                    def tokens_and_positions(self, item, record):
                        return [1, 2], [0, 0, 1], 1

                    def run(self, ids, positions):
                        captures.append(1)
                        return np.zeros((3, 2, 4), dtype=np.uint16)

                F.capture(root, "synthetic", Replay)
                F.capture(root, "synthetic", Replay)
                self.assertEqual(len(captures), 1)
                array = root / "data/activations/synthetic/ckd" / item.key / "acts.npy"
                self.assertEqual(np.load(array).shape, (1, 3, 2, 4))
                F.export(root, "synthetic")
                self.assertEqual(pd.read_parquet(responses).value.tolist(), [0.3])
                self.assertTrue(json.loads((root / "outputs/synthetic_activation_refs.json").read_text())[0]["complete"])
                # A damaged capture is replayed; a valid one never is.
                array.write_bytes(b"broken")
                F.capture(root, "synthetic", Replay)
                self.assertEqual(len(captures), 2)
                plan["settings"]["synthetic"] = "wrong"
                plan["digest"] = F.digest({k: v for k, v in plan.items() if k != "digest"})
                F.atomic_json(root / "plan.json", plan)
                F.atomic_json(root / "prepared.json", {"plan_digest": plan["digest"]})
                with self.assertRaisesRegex(ValueError, "settings changed"):
                    F.read_plan(root)
        finally:
            C.CONFIG_PATH, C.CACHE = saved
            C.load.cache_clear()
            G.cache_index.cache_clear()


if __name__ == "__main__":
    unittest.main()
