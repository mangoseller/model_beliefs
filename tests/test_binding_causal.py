"""Session 2 (binding_causal): every intervention checked exactly on a tiny random Qwen3, then all stages
end to end on synthetic session-1 output.

Needs a Qwen3 tokenizer: the Hub snapshot of Qwen/Qwen3-0.6B (cached), or ``SO_TEST_TOKENIZER``.

    python -m unittest tests.test_binding_causal
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import yaml

from belief_leakage import binding as B
from belief_leakage import binding_causal as BC
from belief_leakage import config as C

from tests.support import isolated_config, run as run_cli, run_env
from tests.qwen import digit_probabilities, requires_tokenizer, snapshot, tiny_lens, two_digit_value


def explicit_readout(lens, ids, variant=None):
    def logits(seq):
        with lens._run([variant] if variant else []):
            return lens.lm(lens.torch.tensor([seq])).logits[0, -1].float()

    first, second = digit_probabilities(logits, ids, lens.digits)
    return two_digit_value(first / first.sum(), second)


def a_prompt(arm="false_labelled", order="person_first", role="C", case=None):
    split = B.patient_split(20261111)
    case = case or B.select_patients(30, split, 20261111)[0]
    frame = B.release_frame([case])
    src = frame[(frame.condition == B.source_condition(arm, role)) & (frame.output == "probability/normal")
                & (frame.replicate == 0)].prompt.iloc[0]
    return B.transform(src, arm, order if role in "CMD" else None, role)


@requires_tokenizer
class Interventions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        cls.torch = torch
        cls.lens = tiny_lens()
        cls.addClassCleanup(cls.lens.close)
        cls.prompt = BC.no_reasoning(a_prompt())
        cls.enc = cls.lens.encode(cls.prompt)

    def plain_logits(self, ids):
        with self.lens._run():
            return self.lens.lm(self.torch.tensor([ids])).logits[0, -1].float()

    def test_positions_decode_to_the_intended_text(self):
        tok, e = self.lens.tok, self.enc
        dec = lambda k: tok.decode([e.ids[k]])
        self.assertEqual(dec(e.positions["answer"]), ".")
        self.assertTrue(tok.decode(e.ids[e.positions["answer"] - 4:]).endswith('": 0.'))
        name = B.person_name(self.prompt)
        self.assertTrue(tok.decode(e.ids[:e.positions["name"] + 1]).endswith(f"does {name}"))
        self.assertTrue(tok.decode(e.ids[:e.positions["person_end"] + 1]).rstrip().endswith(f"{name}'s record.)"))
        self.assertTrue(tok.decode(e.ids[:e.positions["own_end"] + 1]).rstrip().endswith("your record.)"))
        self.assertTrue(tok.decode(e.ids[:e.positions["question_end"] + 1]).rstrip().endswith("(CKD)?"))
        person = tok.decode([e.ids[k] for k in e.groups["person"]])
        self.assertTrue(person.lstrip().startswith("Record X") and person.rstrip().endswith("record.)"))
        self.assertTrue(tok.decode([e.ids[k] for k in e.groups["readout"]]).lstrip().startswith("What probability"))
        filled = self.lens.encode(BC.with_filler(self.prompt))
        self.assertTrue(tok.decode([filled.ids[k] for k in filled.groups["filler"]]).strip().startswith("Clinic notice"))
        # The filler sits between the two records, longer than either, and the control masks as many of its
        # tokens as the knocked record has.
        g = filled.groups
        first, second = sorted((g["own"], g["person"]), key=min)
        self.assertTrue(max(first) < min(g["filler"]) and max(g["filler"]) < min(second))
        for group in ("own", "person"):
            self.assertGreater(len(g["filler"]), len(g[group]))
            keys = BC.matched_filler(filled, group)
            self.assertEqual(len(keys), len(g[group]))
            self.assertTrue(set(keys) <= set(g["filler"]))
        self.assertEqual(sorted(b["owner"] for b in B.record_blocks(BC.with_filler(self.prompt))),
                         sorted(b["owner"] for b in B.record_blocks(self.prompt)))
        # Every arm and role encodes (A and B have no name or second block).
        for arm in B.ARMS:
            for role in "ABCMD":
                e = self.lens.encode(BC.no_reasoning(a_prompt(arm, "own_first", role)))
                self.assertIn("answer", e.positions)
                self.assertEqual("name" in e.positions, role in "CMD")

    def test_noop_hooks_change_nothing_and_two_digit_readout_is_exact(self):
        ids = self.enc.ids
        res = self.lens.readout(ids, [BC.Variant(), BC.Variant()])
        self.assertAlmostEqual(float(res["p"][0]), float(res["p"][1]), places=6)
        # First digit from a plain forward; second digit from explicit sequences, not the cache.
        first, q2s = digit_probabilities(self.plain_logits, ids, self.lens.digits)
        p1 = first / first.sum()
        self.assertAlmostEqual(float(res["p"][0]), two_digit_value(p1, q2s), places=5)
        # the expected-probability statistic is exact too
        with patch.object(self.lens, "statistic", "expected_p"):
            got = float(self.lens.readout(ids, [BC.Variant()])["p"][0])
        self.assertAlmostEqual(got, two_digit_value(p1, q2s, "expected_p"), places=5)

    def test_tree_second_digit_is_exact_under_knockout_and_steering(self):
        torch = self.torch
        e = self.enc
        L = len(e.ids)
        vec = torch.randn(64) * 3
        for knock in (True, False):
            v = (BC.Variant(knock=[(1, e.groups["readout"], e.groups["own"], True)]) if knock
                 else BC.Variant(steer=[(2, e.positions["name"], vec)]))
            got = float(self.lens.readout(e.ids, [v])["p"][0])
            # Explicit: every candidate as a real next token, the knockout also blocking it as a query.
            explicit = (BC.Variant(knock=[(1, e.groups["readout"] + [L], e.groups["own"], False)]) if knock else v)
            self.assertAlmostEqual(got, explicit_readout(self.lens, e.ids, explicit), places=5)

    def test_patching_every_position_reproduces_the_donor(self):
        donor_prompt = self.prompt.replace("Age (age) is", "Age (age) is ", 1)
        donor = self.lens.encode(donor_prompt)
        if len(donor.ids) != len(self.enc.ids):
            donor = self.lens.encode(self.prompt.replace("No.", "Yes", 1))
        self.assertEqual(len(donor.ids), len(self.enc.ids))
        full = list(range(len(donor.ids)))
        states = self.lens.readout(donor.ids, [BC.Variant()], hidden_at=full)["hidden"][0]
        target = float(self.lens.readout(donor.ids, [BC.Variant()])["p"][0])
        for layer in (1, 2, 3):
            v = BC.Variant(patch=[(layer, k, states[layer, k]) for k in full])
            got = float(self.lens.readout(self.enc.ids, [v])["p"][0])
            self.assertAlmostEqual(got, target, places=5)
        base = float(self.lens.readout(self.enc.ids, [BC.Variant()])["p"][0])
        self.assertNotAlmostEqual(base, target, places=5)

    def test_knockout_zeroes_exactly_the_blocked_attention_from_its_start_layer(self):
        torch = self.torch
        e = self.enc
        qs, ks = e.groups["readout"], e.groups["own"]
        v = BC.Variant(knock=[(2, qs, ks, True)])
        with self.lens._run([v]):
            out = self.lens.lm(torch.tensor([e.ids]), output_attentions=True)
        for layer, att in enumerate(out.attentions):
            blocked = att[0][:, qs][:, :, ks]
            if layer >= 2:
                self.assertEqual(float(blocked.abs().max()), 0.0)
                self.assertAlmostEqual(float(att[0][:, qs].sum(-1).min()), 1.0, places=5)
            else:
                self.assertGreater(float(blocked.sum()), 0.0)
        # The second-digit pass blocks the new token too.
        res = self.lens.readout(e.ids, [v])
        ref = self.lens.readout(e.ids, [BC.Variant()])
        self.assertNotAlmostEqual(float(res["p"][0]), float(ref["p"][0]), places=6)

    def test_ablation_pins_the_projection_and_steering_adds_the_vector(self):
        torch = self.torch
        e = self.enc
        pos = e.positions["answer"]
        u = torch.randn(64)
        u = u / u.norm()
        mu = 0.37
        vec = torch.randn(64)
        for variant, check in (
                (BC.Variant(ablate=[({2}, [pos], u, mu)]), "ablate"),
                (BC.Variant(steer=[(2, pos, vec)]), "steer")):
            with self.lens._run([variant]):
                out = self.lens.lm(torch.tensor([e.ids]), output_hidden_states=True)
            with self.lens._run():
                base = self.lens.lm(torch.tensor([e.ids]), output_hidden_states=True)
            h, h0 = out.hidden_states[2][0, pos], base.hidden_states[2][0, pos]
            if check == "ablate":
                self.assertAlmostEqual(float(h @ u), mu, places=4)
                self.assertTrue(torch.allclose(h - (h @ u) * u, h0 - (h0 @ u) * u, atol=1e-5))
            else:
                self.assertTrue(torch.allclose(h - h0, vec, atol=1e-5))
            # Other positions and earlier layers are untouched.
            self.assertTrue(torch.allclose(out.hidden_states[1], base.hidden_states[1]))
            self.assertTrue(torch.allclose(out.hidden_states[2][0, :pos], base.hidden_states[2][0, :pos], atol=1e-6))

    def test_shared_prefix_cache_is_exact_for_every_intervention(self):
        torch = self.torch
        e = self.enc
        u = torch.randn(64)
        u = u / u.norm()
        donor = self.lens.readout(e.ids, [BC.Variant()], hidden_at=[e.positions["name"]])["hidden"][0]
        variants = [BC.Variant(),
                    BC.Variant(patch=[(2, e.positions["name"], donor[2, 0] * 1.5)]),
                    BC.Variant(steer=[(1, e.positions["answer"], torch.randn(64) * 3)]),
                    BC.Variant(ablate=[({1, 2}, [e.positions["answer"], e.positions["gen_start"]], u, 0.2)]),
                    BC.Variant(knock=[(1, e.groups["readout"], e.groups["own"], True)]),
                    BC.Variant(knock=[(2, e.groups["own"], e.groups["person"], False)])]
        for v in variants:
            fast = float(self.lens.readout(e.ids, [v])["p"][0])
            slow = float(self.lens.readout(e.ids, [v], reuse_prefix=False)["p"][0])
            self.assertAlmostEqual(fast, slow, places=5)
        np.testing.assert_allclose(self.lens.readout(e.ids, variants)["p"],
                                   self.lens.readout(e.ids, variants, reuse_prefix=False)["p"], atol=1e-5)

    def test_padded_batch_readout_equals_one_at_a_time(self):
        prompts = [BC.no_reasoning(a_prompt(arm, order, role)) for arm, order, role in
                   (("false_labelled", "person_first", "C"), ("partial_original", "own_first", "C"),
                    ("strong_labelled_explicit", "person_first", "M"), ("false_verified", "own_first", "A"),
                    ("partial_instructed", "own_first", "D"))]
        encs = [self.lens.encode(p) for p in prompts]
        self.assertGreater(len({len(e.ids) for e in encs}), 1)  # really padded
        at = [[e.positions["answer"], e.positions["gen_start"]] if i % 2 == 0 else None for i, e in enumerate(encs)]
        ps, masses, hidden = self.lens.readout_batch(encs, at, batch=3)
        for e, p, h, a in zip(encs, ps, hidden, at):
            single = self.lens.readout(e.ids, [BC.Variant()], hidden_at=a)
            self.assertAlmostEqual(p, float(single["p"][0]), places=5)
            if a is None:
                self.assertIsNone(h)
                continue
            # Both captures against transformers' own hidden_states (0 = embeddings, last = after the norm).
            with self.lens._run():
                ref = self.lens.lm(self.torch.tensor([e.ids]), output_hidden_states=True).hidden_states
            ref = self.torch.stack([layer[0, a] for layer in ref])
            self.assertTrue(self.torch.allclose(h, ref, atol=1e-4))
            self.assertTrue(self.torch.allclose(single["hidden"][0], ref, atol=1e-5))

    def test_rows_of_one_batch_are_independent(self):
        torch = self.torch
        e = self.enc
        vec = torch.randn(64) * 5
        variants = [BC.Variant(), BC.Variant(steer=[(2, e.positions["answer"], vec)]),
                    BC.Variant(knock=[(1, e.groups["readout"], e.groups["own"], True)]),
                    BC.Variant(knock=[(3, e.groups["readout"], e.groups["person"], True)]),
                    BC.Variant(knock=[(0, e.groups["own"], e.groups["person"], False)])]
        alone = [float(self.lens.readout(e.ids, [v])["p"][0]) for v in variants]
        np.testing.assert_allclose(self.lens.readout(e.ids, variants)["p"], alone, atol=1e-5)


class Null(unittest.TestCase):
    def test_patient_permutation_moves_whole_patients(self):
        rng = np.random.default_rng(0)
        groups = np.array([f"p{i}" for i in range(6)] * 2 + ["p0"])
        cells = np.array([0] * 6 + [1] * 6 + [2])
        y = np.arange(13, dtype=float)
        out = BC.permute_patients(y, groups, cells, rng)
        # a patient's two cells take the two cells' targets of one other patient
        where = {(g, c): v for g, c, v in zip(groups, cells, y)}
        donors = {}
        for g, c, v in zip(groups[:12], cells[:12], out[:12]):
            donor = next(h for (h, cc), w in where.items() if cc == c and w == v)
            donors.setdefault(g, set()).add(donor)
        self.assertTrue(all(len(v) == 1 for v in donors.values()))
        self.assertEqual(sorted(out[:6]), sorted(y[:6]))


WEIGHTS = snapshot()


@unittest.skipUnless(WEIGHTS and (Path(WEIGHTS) / "model.safetensors").is_file(), "no cached Qwen3-0.6B weights")
class RealWeights(unittest.TestCase):
    """On real weights (where a misplaced tree mask shows; a tiny random model is insensitive to it): the
    single-prompt readout, the padded batch readout and the explicit two-digit computation agree."""

    def test_readout_paths_agree_with_the_explicit_computation(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        lm = AutoModelForCausalLM.from_pretrained(WEIGHTS, torch_dtype=torch.float32, attn_implementation="eager")
        lens = BC.Lens(lm=lm, tok=AutoTokenizer.from_pretrained(WEIGHTS), device=torch.device("cpu"),
                       template_kwargs={"enable_thinking": False})
        encs = [lens.encode(BC.no_reasoning(a_prompt(arm, "person_first", "C")))
                for arm in ("false_verified", "partial_labelled")]

        batch = lens.readout_batch(encs, batch=2)[0]
        for e, pb in zip(encs, batch):
            want = explicit_readout(lens, e.ids)
            self.assertAlmostEqual(float(lens.readout(e.ids, [BC.Variant()])["p"][0]), want, places=4)
            self.assertAlmostEqual(pb, want, places=4)
        # and under an intervention (a knockout also blocking the answer token)
        e = encs[0]
        v = BC.Variant(knock=[(4, e.groups["readout"], e.groups["own"], True)])
        explicit_v = BC.Variant(knock=[(4, e.groups["readout"] + [len(e.ids)], e.groups["own"], False)])
        self.assertAlmostEqual(float(lens.readout(e.ids, [v])["p"][0]), explicit_readout(lens, e.ids, explicit_v), places=4)


class Probes(unittest.TestCase):
    def test_grouped_cv_does_not_reward_patient_identity(self):
        # Rows carry only the patient's identity; the target is unrelated to anything encoded.
        rng = np.random.default_rng(0)
        patients = np.repeat(np.arange(60), 3)
        identity = rng.normal(size=(60, 200))[patients] + 0.01 * rng.normal(size=(180, 200))
        y = rng.normal(size=60)[patients]
        self.assertLess(BC.grouped_cv_r(identity, y, patients), 0.3)

    def test_encoding_pattern_finds_the_signal_whatever_the_feature_scales(self):
        rng = np.random.default_rng(1)
        n, d = 200, 300
        scales = np.exp(rng.uniform(np.log(0.02), np.log(20), d))
        X = rng.normal(size=(n, d)) * scales
        signal = np.zeros(d)
        signal[np.argsort(scales)[-20:]] = 1  # signal on high-variance coordinates
        signal /= np.linalg.norm(signal)
        y = X @ signal / scales.max() + 0.1 * rng.normal(size=n)
        X = X + np.outer(y, signal) * 5
        u = BC.Ridge(X, y, np.arange(n)).pattern
        self.assertGreater(abs(u @ signal), 0.8)


@requires_tokenizer
class StagesEndToEnd(unittest.TestCase):
    """Session 1 on the synthetic model, then every session-2 stage with the tiny model."""

    def test_all_stages(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cfg = yaml.safe_load(C.BUNDLED_CONFIG.read_text())
        env = run_env(root, cfg)
        source = root / "full"
        for args in (["prepare", "--run", str(source), "--profile", "pilot", "--models", "synthetic", "--n-patients",
                      "all", "--direct-samples", "1"],
                     ["generate", "--run", str(source), "--model", "synthetic"],
                     ["export", "--run", str(source), "--model", "synthetic"]):
            run_cli(["belief_leakage.binding", *args], env)
        # The analysis only needs the direct beliefs here (no latent arms): rewrite the spec's latent arms.
        from belief_leakage import binding_analysis as BA
        with isolated_config():
            plan = B.read_plan(source)
            long = pd.read_parquet(source / "outputs" / "synthetic_responses.parquet")
            out = source / "outputs" / "analysis" / "synthetic"
            out.mkdir(parents=True)
            beliefs = BA.direct_beliefs(long)
            beliefs.to_parquet(out / "beliefs_direct.parquet", index=False)
            cells, contrasts = BA.bootstrap(BA.wide_frame(beliefs), plan["spec"]["arms"], 50, 1, checks=False)
            cells.assign(method="direct").to_csv(out / "lambda_cells.csv", index=False)
            contrasts.assign(method="direct").to_csv(out / "contrasts.csv", index=False)
            run = root / "causal"
            run.mkdir()
            ccfg = BC.load_cfg(run) | {"cells": ["false_verified", "false_labelled", "partial_original",
                                                 "partial_labelled_explicit"], "random_directions": 3,
                                       "knockout_every": 2, "batch": 8, "report_min_cases": 4,
                                       "apply_gate": False, "patch_min_gap": 0.0}  # random model: mechanics only
            lens = tiny_lens()
            # Keep the end-to-end test small: 12 fit and 6 test patients.
            prompts = BC.source_prompts(source, "synthetic")
            keep = (sorted(prompts.case_id[prompts.split == "fit"].unique())[:12]
                    + sorted(prompts.case_id[prompts.split == "test"].unique())[:6])
            subset = prompts[prompts.case_id.isin(keep)]
            with patch.object(BC, "source_prompts", side_effect=lambda *_: subset.copy()):
                frame = BC.stage_readout(run, source, "synthetic", lens, ccfg)
                wanted = BC.source_prompts(source, "synthetic")
                self.assertEqual(len(frame), int(wanted.arm.isin(ccfg["cells"]).sum()))
                self.assertTrue(((frame.p > 0) & (frame.p < 1)).all())
                BC.stage_gate(run, source, "synthetic", ccfg, 20)
                lock = BC.stage_probes(run, source, "synthetic", ccfg)
                self.assertEqual(set(lock["variants"]), {"false", "partial"})
                for v in lock["variants"].values():
                    self.assertTrue(all(1 <= l < lens.n_layers for l in v["window"]))
                with self.assertRaises(ValueError):
                    BC.stage_probes(run, source, "synthetic", ccfg)  # the lock is written once
                patched = BC.stage_patch(run, source, "synthetic", lens, ccfg)
                self.assertEqual(set(patched.direction), {"sufficiency", "necessity"})
                self.assertEqual(patched.layer.max(), lens.n_layers - 1)
                ko = BC.stage_knockout(run, source, "synthetic", lens, ccfg)
                self.assertEqual(set(ko.knockout), {"none", "readout", "encoding", "update"})
                dr = BC.stage_directions(run, source, "synthetic", lens, ccfg)
                self.assertIn("ablate_rec", set(dr.intervention))
                rules = BC.stage_report(run, "synthetic", 20, ccfg)
                self.assertTrue((run / "synthetic" / "RESULTS.md").exists())
                # a knockout is tested only where the gate found room for the 0.3 drop
                g = pd.read_csv(run / "synthetic" / "gate.csv")
                testable = dict(zip(g.arm + "|" + g.order, g.knockout_testable))
                for k, v in rules.items():
                    if k.startswith("3.2_leak_enters_at"):
                        cell = k.split("|", 1)[1].rstrip("]")
                        self.assertEqual(v["status"] == "not applicable", not testable[cell], k)
                pl = pd.read_csv(run / "synthetic" / "patch_lambda.csv")
                self.assertEqual(set(pl.position), {"answer", "name", "own_end", "person_end"})
                self.assertTrue(set(pl.layer) <= set(range(1, lens.n_layers)))  # chosen on the fit half
                lam = pl[pl.statistic == "lambda_patched"]
                # finite, or blanked because beta_D's interval includes 0 (a random model has no real beta_D)
                self.assertTrue((np.isfinite(lam.estimate) | lam.blank_reason.fillna("").str.contains("beta_D")).all())
                self.assertTrue(any(k.startswith("3.3") for k in rules), rules)
                self.assertTrue(any(k.startswith("3.2_leak_enters_at") for k in rules), rules)
                self.assertTrue(any(k.startswith("3.1_decided_at") for k in rules), rules)
                self.assertTrue(all("interval" in v or "ablate_attr" in v for v in rules.values()))
                # The gate restricts every Phase 3 stage to the cells that passed it.
                d = run / "synthetic"
                g = pd.read_csv(d / "gate.csv")
                self.assertTrue({"correctly_bound", "knockout_testable"} <= set(g.columns))
                pd.DataFrame([dict(arm="false_verified", order="person_first", enters_phase3=True,
                                   correctly_bound=False, knockout_testable=True),
                              dict(arm="false_labelled", order="person_first", enters_phase3=True,
                                   correctly_bound=True, knockout_testable=False),
                              dict(arm="false_verified", order="own_first", enters_phase3=True,
                                   correctly_bound=False, knockout_testable=False),
                              dict(arm="partial_original", order="own_first", enters_phase3=False,
                                   correctly_bound=False, knockout_testable=True)]).to_csv(d / "gate.csv", index=False)
                gc = BC.gate_cfg(d, ccfg | {"apply_gate": True})
                self.assertTrue(all(tuple(p[0]) == ("false_verified", "person_first") for p in gc["patch_pairs"]))
                # only correctly bound donors (false_verified|own_first is not)
                self.assertEqual([tuple(p[1]) for p in gc["patch_pairs"]], [("false_labelled", "person_first")])
                self.assertEqual(gc["knockout_cells"], [["false_verified", "person_first"], ["false_verified", "own_first"]])
                self.assertEqual(gc["direction_cells"]["partial"], [])
                bench = BC.stage_benchmark(run, source, "synthetic", lens, ccfg, n_readout=4)
                self.assertLess(bench["precision_noise_max_abs_logit"], 1e-3)  # float32 on the CPU
                self.assertEqual(set(bench["estimated_hours"]), {"readout", "patch", "knockout", "directions", "total"})
                self.assertGreater(bench["rows_per_patient"]["patch"], 0)
                self.assertLess(bench["batch_invariance_max_abs_dp"], 1e-4)  # float32 on the CPU
                # Tampering with the directions after the lock is refused.
                np.savez(run / "synthetic" / "directions.npz", x=np.zeros(1))
                with self.assertRaisesRegex(ValueError, "changed after the lock"):
                    BC.stage_patch(run, source, "synthetic", lens, ccfg)


if __name__ == "__main__":
    unittest.main()
