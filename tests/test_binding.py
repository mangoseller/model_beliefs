"""Binding experiment, session 1 and Phase 2: prompts, plan, reuse, synthetic recovery, adapter, packaging.

    python -m unittest tests.test_binding        (about 3 minutes; the latent fits dominate)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from second_order import binding as B
from second_order import binding_synthetic as BS
from second_order import config as C
from second_order import datasets as DS
from second_order import order_followup as F
from second_order import run_generation as G

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261111


def lines_of(prompt, owner):
    blocks = B.record_blocks(prompt)
    block = next(b for b in blocks if b["owner"] == owner)
    return block["fields"]


class Prompts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame = B.release_frame(DS.case_ids("ckd"))
        cls.split = B.patient_split(SEED)
        cls.cases = B.select_patients(30, cls.split, SEED)[:4]

    def test_parse_render_round_trip_on_every_release_prompt(self):
        self.assertTrue(all(B.render(B.parse(p)) == p for p in self.frame.prompt))

    def test_field_names_match_the_release(self):
        f = self.frame[(self.frame.condition == "C") & (self.frame.output == "probability/normal")]
        for _, r in f.iterrows():
            withheld = set(B.fields(B.block(B.parse(r.prompt), "own"))) - set(B.fields(B.block(B.parse(r.prompt), "person")))
            self.assertEqual({B.NAME_CODE[n] for n in r.changed_facts.split("; ")}, withheld)
        self.assertEqual(set(B.LABELS), set(B.NAME_CODE.values()))

    def test_every_arm_changes_only_what_it_should(self):
        index = {(r["case_id"], r["condition"], r["output"], r["replicate"]): r["prompt"]
                 for r in self.frame.to_dict("records")}
        for case in self.cases:
            for arm, (variant, binding, absence) in B.ARMS.items():
                for order in B.ORDERS:
                    for role in "ABCMD":
                        src = index[(case, B.source_condition(arm, role), "probability/normal", 0)]
                        out = B.transform(src, arm, order if role in "CMD" else None, role)
                        blocks = B.record_blocks(out)
                        original = B.record_blocks(src)
                        # Questions are untouched (except the instruction paragraph).
                        question = [p for p in out.split("\n\n") if p.startswith("What probability")]
                        self.assertEqual(question, [p for p in src.split("\n\n") if p.startswith("What probability")])
                        self.assertEqual(B.instruction(B.person_name(out) or "") in out,
                                         binding == "instructed" and role in "CMD")
                        # Field lines: identical facts in identical order, minus the strong removals.
                        for owner in ("own", "person"):
                            got = [b["fields"] for b in blocks if b["owner"] == owner]
                            want = [b["fields"] for b in original if b["owner"] == owner]
                            if not want:
                                self.assertEqual(got, [])
                                continue
                            removed = set()
                            if variant == "strong" and (role, owner) in {("B", "own"), ("C", "person"),
                                                                         ("M", "own"), ("M", "person")}:
                                removed = set(B.STRONG)
                            self.assertEqual(list(got[0].items()), [kv for kv in want[0].items() if kv[0] not in removed])
                        if role in "CMD":
                            self.assertEqual([b["owner"] for b in blocks],
                                             ["own", "person"] if order == "own_first" else ["person", "own"])
                            self.assertEqual(out.startswith("Record "), binding in ("labelled", "instructed"))
                        self.assertNotIn(B.CUE, out) if binding != "verified" else None
                        if binding == "verified" and role in "ACMD":
                            self.assertIn(B.CUE, out)
                        has_absence = "has no entry for" in out
                        self.assertEqual(has_absence, absence == "explicit" and role in "CM")
                        if has_absence:
                            listed = re.search(r"has no entry for (.+?)\. ", out).group(1)
                            missing = [c for c in B.LABELS if c not in lines_of(out, "person")]
                            self.assertEqual(sorted(B.LABELS[c] for c in missing),
                                             sorted(re.split(r", | or ", listed)))

    def test_strong_variant_is_built_like_the_release_partial_records(self):
        # Release C = D minus the withheld lines; strong C = D minus age, diabetes, hypertension.
        index = {(r["case_id"], r["condition"], r["output"]): r["prompt"] for r in self.frame.to_dict("records")
                 if r["replicate"] == 0}
        for case in self.cases:
            d = index[(case, "D", "probability/normal")]
            c = B.transform(d, "strong_original", B.DS.patients().loc[case, "block_order"], "C")
            self.assertEqual(set(lines_of(d, "person")) - set(lines_of(c, "person")), set(B.STRONG))
            self.assertEqual(lines_of(d, "own"), lines_of(c, "own"))


class Plan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.split = B.patient_split(SEED)
        cls.cases = B.select_patients(30, cls.split, SEED)[:3]
        spec = dict(B.PROFILES["full"]) | {"drift_fraction": 0.02}
        cls.trials, cls.items = B.make_plan("qwen3_8b", cls.cases, spec, cls.split, SEED)
        cls.T = pd.DataFrame(cls.trials)

    def test_order_followup_cells_get_its_exact_item_keys(self):
        ft, _ = F.make_plan("qwen3_8b", self.cases)
        astra = pd.DataFrame(ft)
        astra["arm"] = [{("partial", "original"): "partial_original", ("false", "verified"): "false_verified",
                         ("false", "neutral"): "false_neutral"}[(s, w)] for s, w in zip(astra.setting, astra.wording)]
        astra["block_order"] = astra.block_order.where(astra.role.isin(list("CMD")), "shared")
        keys = ["case_id", "arm", "block_order", "role", "output", "replicate"]
        mine = self.T[self.T.arm.isin(B.ORDER_FOLLOWUP_ARMS) & (self.T["sample"] == 0)].groupby(keys).item_key.first()
        want = astra.groupby(keys).item_key.first()
        joined = pd.concat([mine.rename("mine"), want.rename("astra")], axis=1)
        self.assertEqual(len(joined), len(want))
        self.assertTrue((joined.mine == joined.astra).all())

    def test_items_are_unique_referenced_and_correctly_sourced(self):
        identities = [(v["case_id"], v["output"], v["replicate"], v["sample"], v["prompt_sha"]) for v in self.items.values()]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(set(self.T.item_key), set(self.items))
        self.assertTrue(all(v["sample"] == 0 for v in self.items.values() if v["source"] == "baseline"))
        # Labelled D is one item for partial and false (identical prompts).
        d = self.T[(self.T.role == "D") & self.T.arm.isin(["partial_labelled", "false_labelled"])]
        self.assertEqual(d.groupby(["case_id", "block_order", "output", "sample"]).item_key.nunique().max(), 1)
        # Samples of the stated probability are distinct items.
        p = self.T[(self.T.output == "probability/normal") & (self.T.role == "C") & (self.T.arm == "strong_labelled")]
        self.assertEqual(p.groupby(["case_id", "block_order"]).item_key.nunique().min(), 5)
        # Latent arms carry the full battery; others only the probability question and checks.
        outputs = self.T.groupby("arm").output.nunique()
        self.assertEqual(outputs["partial_labelled"], 33)
        self.assertEqual(outputs["strong_labelled"], 3)
        drift = self.T[self.T.arm == "drift"]
        self.assertGreater(len(drift), 0)
        self.assertTrue(all(self.items[k]["source"] == "baseline" for k in drift.drift_of))
        self.assertTrue(all(self.items[k]["prompt"] == self.items[d]["prompt"] for k, d in zip(drift.item_key, drift.drift_of)))

    def test_strong_checks_are_keyed_from_their_own_records(self):
        chk = self.T[(self.T.variant == "strong") & self.T.output.str.startswith("check/")]
        self.assertGreater(len(chk), 0)
        for r in chk.itertuples():
            self.assertEqual(BS.check_letter(self.items[r.item_key]["prompt"]), r.check_answer)
            self.assertEqual(r.check_relation, "withheld" if B.NAME_CODE[r.check_fact] in B.STRONG else "shown")
        real = self.T[self.T.output.str.startswith("check/") & (self.T.variant != "strong") & (self.T.arm != "drift")]
        self.assertGreater(len(real), 0)
        for r in real.itertuples():
            self.assertEqual(BS.check_letter(self.items[r.item_key]["prompt"]), r.check_answer)

    def test_patient_split_and_pilot_selection(self):
        pts = DS.patients()
        self.assertEqual(len(self.split), 150)
        for _, g in pts.groupby(["label", "block_order"]):
            counts = Counter(self.split[g.item_id])
            self.assertLessEqual(abs(counts["fit"] - counts["test"]), 1)
        pilot = B.select_patients(30, self.split, SEED)
        self.assertEqual(len(set(pilot)), 30)
        self.assertTrue(all(self.split[c] == "fit" for c in pilot))

    def test_content_digest_ignores_paths_but_not_content(self):
        plan = dict(protocol=B.PROTOCOL, models=["m"], settings={"m": "s"}, trials=self.trials[:5],
                    items={k: self.items[k] for k in list(self.items)[:5]}, base_run="/a", base_activations="/b")
        moved = plan | {"base_run": "/elsewhere/x.tar.gz", "base_activations": "/elsewhere/y.tar", "digest": "z"}
        self.assertEqual(B.content_digest(plan), B.content_digest(moved))
        changed = plan | {"trials": self.trials[:4]}
        self.assertNotEqual(B.content_digest(plan), B.content_digest(changed))
        self.assertEqual(set(json.loads(B.PLAN_DIGESTS.read_text())),
                         {"binding-pilot", "binding-full", "binding-thinking", "binding-32b",
                          "20261121-101311-order-followup"})

    def test_engine_overrides_do_not_change_the_settings_signature(self):
        cfg = C.model_config("qwen3_8b")
        engine, revision = cfg.get("engine"), cfg.get("revision")
        cfg["revision"] = revision or "b968826d9c46dd6066d109eabc6255188de91218"  # pinned, as in every run config
        before = G.settings_signature("qwen3_8b", "ckd")
        try:
            C.model_config("qwen3_8b")["engine"] = {**(engine or {}), "max_num_batched_tokens": 65536,
                                                    "max_num_seqs": 2048}
            self.assertEqual(G.settings_signature("qwen3_8b", "ckd"), before)
        finally:
            cfg["engine"] = engine
            if revision is None:
                cfg.pop("revision")
            else:
                cfg["revision"] = revision

    def test_extra_models_merge_keeps_existing_entries(self):
        base = C.BUNDLED_CONFIG.read_bytes()
        merged = B.with_models(base, ["qwen3_8b", "qwen3_32b", "qwen3_8b_think"])
        old, new = yaml.safe_load(base), yaml.safe_load(merged)
        self.assertTrue(all(new["models"][m] == old["models"][m] for m in old["models"]))
        self.assertTrue(new["models"]["qwen3_8b_think"]["enable_thinking"])
        self.assertEqual({k: v for k, v in new.items() if k != "models"}, {k: v for k, v in old.items() if k != "models"})
        self.assertEqual(B.with_models(base, ["qwen3_8b"]), base)


def run(args, env, check=True):
    done = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    if check and done.returncode:
        raise AssertionError(f"{args} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
    return done


class SyntheticEndToEnd(unittest.TestCase):
    """prepare -> generate (prompt-reading synthetic model) -> export -> analysis recovers planted lambda."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cfg = yaml.safe_load(C.BUNDLED_CONFIG.read_text())
        cfg["latent_fit"] = {"epochs": 1500, "patience": 100}
        (tmp / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        cls.env = {**os.environ, "SO_CONFIG": str(tmp / "config.yaml"), "PYTHONPATH": str(ROOT)}
        cls.run_dir = tmp / "run"
        run(["second_order.binding", "prepare", "--run", str(cls.run_dir), "--profile", "full", "--models", "synthetic",
             "--n-patients", "60", "--direct-samples", "4"], cls.env)
        run(["second_order.binding", "generate", "--run", str(cls.run_dir), "--model", "synthetic"], cls.env)
        run(["second_order.binding", "export", "--run", str(cls.run_dir), "--model", "synthetic"], cls.env)
        run(["second_order.binding_analysis", "--run", str(cls.run_dir), "--model", "synthetic", "--resamples", "200"],
            cls.env)
        out = cls.run_dir / "outputs" / "analysis" / "synthetic"
        cls.cells = pd.read_csv(out / "lambda_cells.csv")
        cls.contrasts = pd.read_csv(out / "contrasts.csv")
        cls.decisions = json.loads((out / "decisions.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def lam(self, method, subset="all"):
        c = self.cells
        part = c[(c.method == method) & (c.statistic == "lambda") & (c.subset == subset) & (c.order != "pooled")].copy()
        part["planted"] = [BS.planted_lambda({}, a, o) for a, o in zip(part.arm, part.order)]
        return part

    def test_direct_lambda_recovers_every_planted_cell(self):
        part = self.lam("direct")
        self.assertEqual(len(part), 2 * len(B.ARMS))
        covered = (part.ci_lower <= part.planted) & (part.planted <= part.ci_upper)
        self.assertGreaterEqual(covered.mean(), 0.85, part[~covered])
        self.assertLess((part.estimate - part.planted).abs().max(), 0.2)

    def test_latent_lambda_recovers_planted_cells(self):
        part = self.lam("latent")
        self.assertEqual(set(part.arm), set(B.PROFILES["full"]["latent_arms"]))
        self.assertLess((part.estimate - part.planted).abs().max(), 0.2, part)

    def test_contrasts_and_check_subsets(self):
        c = self.contrasts[self.contrasts.method == "direct"].set_index("contrast")
        self.assertAlmostEqual(c.loc["order:false_verified", "estimate"], 1.0, delta=0.2)
        self.assertGreater(c.loc["order:false_verified", "ci_lower"], 0.5)
        self.assertAlmostEqual(c.loc["partial_labelled-partial_labelled_explicit:pooled", "estimate"], 0.3, delta=0.15)
        passed = self.lam("direct", "check_passed")
        self.assertGreater(len(passed), 0)
        self.assertTrue((passed.n_cases <= 60).all())

    def test_decision_rules_follow_the_planted_pattern(self):
        rules = self.decisions["decision_rules"]["direct"]
        # Planted partial instructed+explicit lambda = 0.2: curse of knowledge survives (H2), and absence
        # explains only 0.3 of it (H3 needs > 0.3 and a strictest cell <= 0.1).
        self.assertIn("H1_false_misbinding", rules)
        self.assertFalse(rules["H3_absence_not_ignorance[partial]"]["supported"])

    def test_resume_generates_nothing_twice_and_import_reuses_keys(self):
        done = run(["second_order.binding", "generate", "--run", str(self.run_dir), "--model", "synthetic"], self.env)
        self.assertIn("0 to call", done.stdout)
        other = Path(self.tmp.name) / "other"
        run(["second_order.binding", "prepare", "--run", str(other), "--profile", "pilot", "--models", "synthetic",
             "--n-patients", "10", "--direct-samples", "2"], self.env)
        done = run(["second_order.binding", "import-cache", "--run", str(other), "--from", str(self.run_dir)], self.env)
        counts = json.loads(done.stdout[done.stdout.index("{"):])["synthetic"]
        # Every pilot item for patients the full run covered is imported; nothing is regenerated.
        self.assertGreater(counts["imported"], 0)
        full_cases = set(json.loads((self.run_dir / "plan.json").read_text())["cases"])
        pilot = json.loads((other / "plan.json").read_text())
        covered = [v for v in pilot["items"].values() if v["case_id"] in full_cases]
        self.assertEqual(counts["imported"], len({v["key"] for v in covered}))

    def test_activation_captures_are_imported_by_key_and_verified(self):
        import hashlib
        saved = (C.CONFIG_PATH, C.CACHE)
        try:
            plan = B.read_plan(self.run_dir)
            items = B.capture_items_of(plan, "synthetic")[:3]
            cache = G.Cache("synthetic", C.CACHE)
            src = Path(self.tmp.name) / "capture_source"
            for item in items:
                folder = src / "data" / "activations" / "synthetic" / "ckd" / item["key"]
                folder.mkdir(parents=True)
                np.save(folder / "acts.npy", np.zeros((1, 3, 2, 4), dtype=np.uint16))
                sha = hashlib.sha256((folder / "acts.npy").read_bytes()).hexdigest()
                record = cache.get(item["key"])  # a capture of this run's own answer
                (folder / "meta.json").write_text(json.dumps(dict(prompt_sha=item["prompt_sha"],
                    settings_sha=plan["settings"]["synthetic"], array_sha256=sha,
                    response_sha=F.digest([record["text"], record["token_ids"]]))))
            self.assertEqual(B._import_activations(self.run_dir, plan, "synthetic", [src]), 3)
            self.assertEqual(B._import_activations(self.run_dir, plan, "synthetic", [src]), 0)  # already there
            bad = src / "data" / "activations" / "synthetic" / "ckd" / B.capture_items_of(plan, "synthetic")[3]["key"]
            bad.mkdir(parents=True)
            np.save(bad / "acts.npy", np.zeros(1, dtype=np.uint16))
            (bad / "meta.json").write_text(json.dumps(dict(prompt_sha="other", settings_sha="x", array_sha256="y")))
            with self.assertRaisesRegex(ValueError, "another prompt"):
                B._import_activations(self.run_dir, plan, "synthetic", [src])
        finally:
            C.CONFIG_PATH, C.CACHE = saved
            C.load.cache_clear()

    def test_package_writes_independent_archives(self):
        from second_order.package_run import package
        out = Path(self.tmp.name) / "pkg"
        written = package(self.run_dir, out, "binding-test")
        has_acts = any((self.run_dir / "data" / "activations").rglob("acts.npy"))
        self.assertEqual([p.name for p in written],
                         ["binding-test.tar.gz"] + (["binding-test_activations.tar"] if has_acts else []))
        with tarfile.open(written[0]) as tar:
            names = tar.getnames()
        self.assertFalse(any("/data/activations/" in n and n.endswith(".npy") for n in names))
        if has_acts:
            with tarfile.open(written[1]) as tar:
                self.assertTrue(all(n.startswith("run/data/activations") for n in tar.getnames()))
        self.assertIn("run/plan.json", names)
        self.assertIn("run/outputs/analysis/synthetic/RESULTS.md", names)
        self.assertTrue((out / "binding-test.tar.gz.sha256").read_text().endswith("  binding-test.tar.gz\n"))


class OrderFollowupImport(unittest.TestCase):
    """Part 2 after part 3a: Astra's plan imports answers and captures that another run generated."""

    def test_order_followup_plan_imports_answers_and_captures(self):
        import hashlib
        saved = (C.CONFIG_PATH, C.CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                astra, source = tmp / "astra", tmp / "source"
                astra.mkdir()
                (astra / "config.yaml").write_bytes(C.BUNDLED_CONFIG.read_bytes())
                F.configure(astra)
                cases = sorted(B.patient_split(SEED).index)[:2]
                trials, items = F.make_plan("synthetic", cases)
                plan = dict(protocol=F.PROTOCOL, models=["synthetic"],
                            settings={"synthetic": G.settings_signature("synthetic", "ckd")}, trials=trials, items=items)
                plan["digest"] = F.digest(plan)
                F.atomic_json(astra / "plan.json", plan)
                F.atomic_json(astra / "prepared.json", {"plan_digest": plan["digest"]})
                new = [F.item_from(v) for v in items.values() if v["source"] == "new"]
                # Another run generated the same items (same keys) and captured one of them.
                C.CACHE = source / "data" / "generation_cache"
                G.cache_index.cache_clear()
                with B.synthetic_backend("synthetic"):
                    G.run(new, label="source")
                capture = next(v for v in items.values() if v["source"] == "new"
                               and v["output"] == "probability/normal" and v["replicate"] == 0)
                folder = source / "data" / "activations" / "synthetic" / "ckd" / capture["key"]
                folder.mkdir(parents=True)
                np.save(folder / "acts.npy", np.zeros((1, 3, 2, 4), dtype=np.uint16))
                record = G.Cache("synthetic", source / "data" / "generation_cache").get(capture["key"])
                (folder / "meta.json").write_text(json.dumps(dict(
                    prompt_sha=capture["prompt_sha"], settings_sha=plan["settings"]["synthetic"],
                    array_sha256=hashlib.sha256((folder / "acts.npy").read_bytes()).hexdigest(),
                    response_sha=F.digest([record["text"], record["token_ids"]]))))
                counts = B.import_cache(astra, [source])["synthetic"]
                self.assertEqual(counts["imported"], len(new))
                self.assertEqual(counts["still_pending"], 0)
                self.assertEqual(counts["activations_imported"], 1)
                F.read_plan(astra)
                G.cache_index.cache_clear()
                self.assertEqual(G.pending(new), [])
                self.assertEqual(B.import_cache(astra, [source])["synthetic"]["imported"], 0)  # idempotent
        finally:
            C.CONFIG_PATH, C.CACHE = saved
            C.load.cache_clear()
            G.cache_index.cache_clear()


class OrderFollowupAdapter(unittest.TestCase):
    """Astra's plan, generated by the synthetic model and exported by its own code, analysed by the adapter."""

    def test_adapter_recovers_planted_lambda(self):
        from second_order import binding_analysis as BA
        saved = (C.CONFIG_PATH, C.CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = yaml.safe_load(C.BUNDLED_CONFIG.read_text())
                cfg["latent_fit"] = {"epochs": 1500, "patience": 100}
                (root / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
                F.configure(root)
                split = B.patient_split(SEED)
                cases = sorted(split.index)[:50]
                trials, items = F.make_plan("synthetic", cases)
                plan = dict(protocol=F.PROTOCOL, models=["synthetic"],
                            settings={"synthetic": G.settings_signature("synthetic", "ckd")}, trials=trials, items=items)
                plan["digest"] = F.digest(plan)
                F.atomic_json(root / "plan.json", plan)
                F.atomic_json(root / "prepared.json", {"plan_digest": plan["digest"]})
                pd.DataFrame(columns=["item_key", "row"]).to_parquet(root / "baseline_activations_synthetic.parquet")
                with B.synthetic_backend("synthetic"):
                    # The synthetic model has no baseline, so generate every item of the plan.
                    G.run([F.item_from(v) for v in items.values()], label="astra-synthetic")
                # Astra's export reads reused items from baseline_cache/ (its prepare copies them there).
                (root / "baseline_cache").mkdir()
                (root / "baseline_cache" / "synthetic.jsonl").write_bytes(
                    (root / "data" / "generation_cache" / "synthetic.jsonl").read_bytes())
                F.export(root, "synthetic")
                BA.analyse(root, "synthetic", resamples=100, order_followup=True)
                cells = pd.read_csv(root / "outputs" / "analysis" / "synthetic" / "lambda_cells.csv")
        finally:
            C.CONFIG_PATH, C.CACHE = saved
            C.load.cache_clear()
            G.cache_index.cache_clear()
        lam = cells[(cells.statistic == "lambda") & (cells.subset == "all") & (cells.order != "pooled")]
        for method in ("direct", "latent"):
            part = lam[lam.method == method]
            self.assertEqual(len(part), 6)
            planted = np.array([BS.planted_lambda({}, a, o) for a, o in zip(part.arm, part.order)])
            self.assertLess(np.abs(part.estimate.to_numpy() - planted).max(), 0.25, part)


if __name__ == "__main__":
    unittest.main()
