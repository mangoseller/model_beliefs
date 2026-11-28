"""Steering experiment: layouts, the chunked engine and every edit checked on a tiny random Qwen3, then the
stages end to end.

Needs a Qwen3 tokenizer: the Hub snapshot of Qwen/Qwen3-0.6B (cached), or ``SO_TEST_TOKENIZER``.

    python -m unittest tests.test_steering
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from second_order import binding as B
from second_order import binding_causal as BC
from steering import interventions as I
from steering import prompts as P
from steering.engine import Edit, Engine, Job, stated_probability
from tests.test_binding_causal import tokenizer_path, two_digit_value


def tiny_engine(statistic="mean_logit"):
    import torch
    from transformers import AutoTokenizer, Qwen3Config, Qwen3ForCausalLM
    tok = AutoTokenizer.from_pretrained(tokenizer_path())
    cfg = Qwen3Config(vocab_size=len(tok), hidden_size=64, intermediate_size=128, num_hidden_layers=4,
                      num_attention_heads=4, num_key_value_heads=2, head_dim=16, max_position_embeddings=4096,
                      sliding_window=None, tie_word_embeddings=True)
    torch.manual_seed(0)
    lm = Qwen3ForCausalLM(cfg).double().eval()     # float64: every equality below is exact to rounding
    return Engine(lm=lm, tok=tok, device="cpu", template_kwargs={"enable_thinking": False}, statistic=statistic)


def cell_prompts(case=None):
    cases = P.patients(4)
    case = case or cases.index[0]
    frame = P.build(cases.loc[[case]], ["false_neutral", "false_labelled", "strong_original"])
    return {(r.arm, r.block_order, r.role, r.replicate): r.prompt for r in frame.itertuples()}


@unittest.skipIf(tokenizer_path() is None, "no Qwen3 tokenizer available")
class Layouts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.E, cls.prompts = tiny_engine(), cell_prompts()

    def lay(self, arm, order, role="C"):
        return P.layout(self.E.tok, BC.no_reasoning(self.prompts[(arm, order, role, 0)]), self.E.kw)

    def test_tokens_decode_to_their_text(self):
        for arm in ("false_neutral", "false_labelled", "strong_original"):
            for order in B.ORDERS:
                prompt = self.prompts[(arm, order, "C", 0)]
                lay, dec = self.lay(arm, order), self.E.tok.decode
                self.assertEqual(lay.order, order)
                self.assertEqual([b.slot for b in lay.blocks], [0, 1])
                fields = {b["owner"]: b["fields"] for b in B.record_blocks(prompt)}
                for blk in lay.blocks:
                    self.assertEqual(set(blk.lines), set(fields[blk.owner]))
                    for code, line in blk.lines.items():
                        self.assertIn(f"({code}) is", dec([lay.ids[t] for t in line.name]))
                        self.assertEqual(dec([lay.ids[t] for t in line.val]).strip(), line.value)
                        self.assertTrue(dec([lay.ids[t] for t in line.eol]).startswith("."))
                    self.assertTrue(dec([lay.ids[t] for t in blk.header]).rstrip().endswith(":"))
                    self.assertEqual("End of Record" in dec([lay.ids[t] for t in blk.extra]), arm == "false_labelled")
                self.assertTrue(dec([lay.ids[t] for t in lay.question]).startswith("What probability"))
                self.assertTrue(dec([lay.ids[t] for t in lay.tail]).endswith(BC.PREFIX))
                self.assertEqual(dec([lay.ids[lay.name_q]]).strip(), B.person_name(prompt))
                # every record token is before the question, and the groups do not overlap
                named = [t for b in lay.blocks for t in b.header + b.extra + [x for l in b.lines.values() for x in l.tokens]]
                self.assertEqual(len(named), len(set(named)))
                self.assertLess(max(named), lay.n_ctx)

    def test_lines_align_between_arms(self):
        for order in B.ORDERS:
            a, b = self.lay("false_neutral", order), self.lay("false_labelled", order)
            pa, pb, missed = P.aligned(a, b)
            self.assertEqual(missed, 0)
            self.assertEqual(len(pa), len(a.line_tokens()))
            same = sum(a.ids[x] == b.ids[y] for x, y in zip(pa, pb))
            self.assertGreaterEqual(same, len(pa) - 2)      # only a block's last line end can differ

    def test_lookups(self):
        prompt = self.prompts[("false_neutral", "person_first", "C", 0)]
        diff = P.diff_fields(prompt)
        self.assertEqual(len(diff), 2)
        self.assertEqual(P.diff_fields(self.prompts[("false_neutral", "person_first", "M", 0)]), [])
        lay = self.lay("false_neutral", "person_first")
        for code, pv, ov in diff:
            for query in P.QUERIES:
                ids, ta, tb = P.lookup_ids(self.E.tok, prompt, code, query, (pv, ov), self.E.kw)
                self.assertNotEqual(ta, tb)
                self.assertEqual(ids[:lay.n_ctx], lay.ids[:lay.n_ctx])     # the records are a shared prefix
                text = self.E.tok.decode(ids)
                self.assertIn(f"({code})", text[text.rfind("In "):])
                chat = P.chat_text(self.E.tok, P.lookup_prompt(prompt, code, query), self.E.kw)
                for value, t in ((pv, ta), (ov, tb)):
                    full = self.E.tok(chat + value, add_special_tokens=False)["input_ids"]
                    self.assertEqual(full[:len(ids) + 1], ids + [t])


@unittest.skipIf(tokenizer_path() is None, "no Qwen3 tokenizer available")
class EngineChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.E, cls.prompts = tiny_engine(), cell_prompts()
        cls.lays = {k: P.layout(cls.E.tok, BC.no_reasoning(v), cls.E.kw) for k, v in cls.prompts.items() if k[2] == "C"}

    def lay(self, arm="false_neutral", order="person_first"):
        return self.lays[(arm, order, "C", 0)]

    def test_chunks_and_padding_are_exact(self):
        E, a, b = self.E, self.lay().ids, self.lay("false_labelled").ids
        self.assertNotEqual(len(a), len(b))
        one = [E.run([s])[0][0] for s in (a, b)]
        both, state, _ = E.run([a, b])
        for x, y in zip(one, both):
            np.testing.assert_allclose(x.numpy(), y.numpy(), atol=1e-9)
        # a left-padded first chunk and a ragged, right-padded second one
        _, state, _ = E.run([a[:200], b[:150]])
        split, state, _ = E.run([a[200:], b[150:]], state=state)
        np.testing.assert_allclose(split.numpy(), both.numpy(), atol=1e-9)
        # a third chunk continues every row from its own last token
        d = E.digits[3]
        nxt, _, _ = E.run([[d], [d]], state=state, keep=False)
        np.testing.assert_allclose(nxt[0].numpy(), E.run([a + [d]])[0][0].numpy(), atol=1e-9)
        np.testing.assert_allclose(nxt[1].numpy(), E.run([b + [d]])[0][0].numpy(), atol=1e-9)

    def test_two_digit_readout(self):
        import torch
        for statistic in ("mean_logit", "expected_p"):
            E = tiny_engine(statistic)
            ids = self.lay().ids
            got = E.evaluate([Job(ids)])[0]
            p1 = E.run([ids])[0][0].softmax(-1)[E.digits]
            q2 = [E.run([ids + [E.digits[a]]])[0][0].softmax(-1)[E.digits] for a in range(10)]
            self.assertAlmostEqual(got["p"], two_digit_value(p1 / p1.sum(), q2, statistic), places=6)
            self.assertAlmostEqual(got["digit_mass"], float(p1.sum()), places=9)
            self.assertTrue(torch.isfinite(torch.tensor(got["p"])))

    def test_lookup_is_the_logit_difference(self):
        E, prompt = self.E, self.prompts[("false_neutral", "person_first", "C", 0)]
        code, pv, ov = P.diff_fields(prompt)[0]
        looks = [P.lookup_ids(E.tok, prompt, code, q, (pv, ov), E.kw) for q in P.QUERIES]
        jobs = [Job(self.lay().ids, lookups=looks), Job(self.lay("false_labelled").ids), Job(self.lay().ids, lookups=looks[:1])]
        res = E.evaluate(jobs)
        for (ids, ta, tb), got in zip(looks, res[0]["lookups"]):
            logits = E.run([ids])[0][0]
            self.assertAlmostEqual(got, float(logits[ta] - logits[tb]), places=8)
        self.assertEqual(res[1]["lookups"], [])
        self.assertAlmostEqual(res[2]["lookups"][0], res[0]["lookups"][0], places=8)
        self.assertAlmostEqual(res[0]["p"], E.evaluate([Job(self.lay().ids)])[0]["p"], places=9)

    def test_edits(self):
        import torch
        E, rec, don = self.E, self.lay(), self.lay("false_labelled")
        base, other = E.evaluate([Job(rec.ids)])[0]["p"], E.evaluate([Job(don.ids)])[0]["p"]
        cap = I.donor_positions(rec)
        hidden = E.evaluate([Job(rec.ids, capture=cap)])[0]["hidden"]
        self.assertEqual(tuple(hidden.shape), (E.n_layers + 1, len(cap), E.hidden))
        # a capture is the layer's output at that token
        with torch.inference_mode():
            hs = E.lm.model(torch.tensor([rec.ids]), output_hidden_states=True).hidden_states
        for layer in range(1, E.n_layers):
            np.testing.assert_allclose(hidden[layer].numpy(), hs[layer][0, cap].numpy(), atol=1e-9)
        # a transplant from the prompt itself, a zero-strength vector and an empty window change nothing
        v = torch.randn(E.n_layers + 1, E.hidden, dtype=torch.float64)
        lines = rec.line_tokens()
        for edits in (I.transplant_edits(rec, rec, hidden, cap, "lines_hdr"), [Edit("add", lines, v, scale=0.0)],
                      I.transplant_edits(rec, rec, hidden, cap, "question", (1, 2))):
            self.assertAlmostEqual(E.evaluate([Job(rec.ids, edits)])[0]["p"], base, places=9)
        # an added vector changes the answer, only from the layer it starts at, and independently per row
        moved = E.evaluate([Job(rec.ids, [Edit("add", lines, v)]), Job(rec.ids), Job(rec.ids, [Edit("add", lines, v, scale=-1.0)])])
        self.assertNotAlmostEqual(moved[0]["p"], base, places=4)
        self.assertNotAlmostEqual(moved[2]["p"], moved[0]["p"], places=4)
        self.assertAlmostEqual(moved[1]["p"], base, places=9)
        late = E.evaluate([Job(rec.ids, [Edit("add", lines, v, layers=(E.n_layers, E.n_layers))])])[0]["p"]
        self.assertAlmostEqual(late, base, places=9)      # the last block's record tokens feed nothing downstream
        added = E.evaluate([Job(rec.ids, [Edit("add", lines[:3], v, layers=(2, 3))], capture=lines[:3])])[0]["hidden"]
        k = [cap.index(p) for p in lines[:3]]
        np.testing.assert_allclose((added[1] - hidden[1][k]).numpy(), 0, atol=1e-9)
        np.testing.assert_allclose((added[2] - hidden[2][k]).numpy(), v[2].expand(3, -1).numpy(), atol=1e-9)
        # replacing every token of a prompt at every layer reproduces the donor's answer
        every = list(range(len(don.ids)))
        dh = E.evaluate([Job(don.ids, capture=every)])[0]["hidden"]
        scrambled = [E.digits[i % 10] for i in every]
        got = E.evaluate([Job(scrambled, [Edit("set", every, dh)])])[0]["p"]
        self.assertAlmostEqual(got, other, places=6)      # logits are read in float32
        # line transplants from the labelled prompt act through the cache: lookups move too
        prompt = self.prompts[("false_neutral", "person_first", "C", 0)]
        code, pv, ov = P.diff_fields(prompt)[0]
        looks = [P.lookup_ids(E.tok, prompt, code, "person", (pv, ov), E.kw)]
        dcap = I.donor_positions(don)
        dhid = E.evaluate([Job(don.ids, capture=dcap)])[0]["hidden"]
        plain, patched = E.evaluate([Job(rec.ids, lookups=looks),
                                     Job(rec.ids, I.transplant_edits(rec, don, dhid, dcap, "lines"), lookups=looks)])
        self.assertNotAlmostEqual(plain["lookups"][0], patched["lookups"][0], places=5)
        self.assertNotAlmostEqual(plain["p"], patched["p"], places=5)

    def test_head_knockouts_and_attention(self):
        import torch
        E, lay = self.E, self.lay()
        keys = lay.blocks[1].line_tokens_all()
        every = {l: (list(range(E.n_heads)), keys) for l in range(E.n_layers)}
        base = E.evaluate([Job(lay.ids)])[0]["p"]
        # every head barred from a block, from the question on = those keys masked for the later chunks
        got = E.evaluate([Job(lay.ids, knock=(lay.n_ctx, every)), Job(lay.ids), Job(lay.ids, knock=(lay.n_ctx, {}))])
        _, state, _ = E.run([lay.ids[:lay.n_ctx]])
        state.mask[0, keys] = 0
        logits, after, _ = E.run([lay.ids[lay.n_ctx:]], state=state)
        self.assertAlmostEqual(got[0]["p"], float(E.two_digit(logits, after)[0][0]), places=6)
        self.assertNotAlmostEqual(got[0]["p"], base, places=4)
        self.assertAlmostEqual(got[1]["p"], base, places=9)       # rows are independent
        self.assertAlmostEqual(got[2]["p"], base, places=9)
        one = E.evaluate([Job(lay.ids, knock=(lay.n_ctx, {1: ([2], keys)}))])[0]["p"]
        self.assertNotAlmostEqual(one, base, places=6)
        self.assertNotAlmostEqual(one, got[0]["p"], places=4)
        # measured attention: a share per head, and zero where the head is barred
        groups = {"first": lay.blocks[0].line_tokens_all(), "second": keys}
        _, state, _ = E.run([lay.ids[:lay.n_ctx]] * 2)
        E.run([lay.ids[lay.n_ctx:]] * 2, state=state, keep=False, groups=[groups] * 2, knock=[None, {1: ([2], keys)}])
        free, barred = E.attention
        self.assertEqual(tuple(free["second"].shape), (E.n_layers, E.n_heads, 2))
        total = free["first"] + free["second"]
        self.assertTrue(bool((total >= 0).all()) and bool((total <= 1 + 1e-6).all()))
        self.assertLess(float(barred["second"][1, 2].abs().max()), 1e-9)
        self.assertGreater(float(free["second"][1, 2].min()), 1e-6)
        torch.testing.assert_close(barred["second"][0], free["second"][0])      # the block before the knockout is untouched
        # sampled replies under a knockout: the preallocated and the growing cache agree
        jobs = [Job(lay.ids[:-6], knock=(lay.n_ctx, every)), Job(lay.ids[:-6])]
        a = E.generate(jobs, 2, 8, seed=5, batch=2)
        self.assertEqual(a, E.generate(jobs, 2, 8, seed=5, batch=2, static=False))
        self.assertNotEqual(a[0], E.generate(jobs[1:], 2, 8, seed=5, batch=1)[0])

    def test_forced_attention(self):
        import torch
        E, lay = self.E, self.lay()
        target = lay.blocks[0].lines["age"].val[0]
        last, n = len(lay.ids) - 1, lay.n_ctx
        heads = [(1, 2), (3, 0)]
        _, s1, _ = E.run([lay.ids[:n]] * 2)
        _, s2, _ = E.run([lay.ids[n:last]] * 2, state=s1)
        force = {1: ([2], [target]), 3: ([0], [target])}
        logits, _, _ = E.run([[lay.ids[last]]] * 2, state=s2, keep=False, force=[force, None], attn_heads=heads + [(1, 1)])
        forced, free = E.attention_maps
        for i in range(2):      # all of a forced head's attention is on the named token
            self.assertAlmostEqual(float(forced[i, 0, target]), 1.0, places=9)
            self.assertAlmostEqual(float(forced[i, 0].sum()), 1.0, places=9)
        self.assertLess(float(free[0, 0, target]), 0.9)
        torch.testing.assert_close(forced[2], free[2])               # another head of the same block is untouched
        plain, _, _ = E.run([[lay.ids[last]]], state=s2.select([0]), keep=False)
        torch.testing.assert_close(logits[1], plain[0])               # rows are independent
        self.assertGreater(float((logits[0] - plain[0]).abs().max()), 1e-9)
        # the forced head's output is its value-output map of that token: the same as taking the head's output from
        # a run in which every other key is hidden from it
        every = [p for p in range(last + 1) if p != target]
        _, t1, _ = E.run([lay.ids[:n]])
        _, t2, _ = E.run([lay.ids[n:last]], state=t1)
        E.run([[lay.ids[last]]], state=t2, keep=False, zcapture=[[last]], force=[{1: ([2], [target])}])
        z = E.zcaptured[0][1]
        E.run([[lay.ids[last]]], state=t2, keep=False, zcapture=[[last]],
              knock=[{1: ([2], [p for p in every if p < last])}])   # (a head cannot be barred from the token itself here)
        dims = E.head_dims([(1, 2)])[1]
        self.assertGreater(float((z[1][:, dims] - E.zcaptured[0][1][1][:, dims]).abs().max()), 0)   # it still saw itself
        # forcing after a knockout of the same key: the pointer wins
        both, _, _ = E.run([[lay.ids[last]]], state=t2, keep=False, knock=[{1: (list(range(E.n_heads)), [target])}],
                           force=[{1: ([2], [target])}], attn_heads=[(1, 2), (1, 0)])
        self.assertAlmostEqual(float(E.attention_maps[0][0, 0, target]), 1.0, places=9)
        self.assertLess(float(E.attention_maps[0][1, 0, target]), 1e-9)

    def test_query_paths_by_head(self):
        import torch
        E, lay = self.E, self.lay()
        n, last = lay.n_ctx, len(lay.ids) - 1
        _, s1, _ = E.run([lay.ids[:n]])
        _, s2, _ = E.run([lay.ids[n:last]], state=s1)
        _, _, hid = E.run([[lay.ids[last]]], state=s2, keep=False, capture=[[last]])
        own = hid[0][1]
        plain, _, _ = E.run([[lay.ids[last]]], state=s2, keep=False, zcapture=[[last]])
        z0 = E.zcaptured[0][1]
        cols = E.head_dims([(2, 1)])
        # the token's own state as the query's source changes nothing, for the block or for one head of it
        for extra in ((), (None, cols)):
            same, _, _ = E.run([[lay.ids[last]]], state=s2, keep=False, paths=[[([last], own, "q", *extra)]])
            torch.testing.assert_close(same, plain, atol=1e-9, rtol=0)
        other = own + torch.randn_like(own)
        E.run([[lay.ids[last]]], state=s2, keep=False, paths=[[([last], other, "q", (2, 2))]], zcapture=[[last]])
        whole = E.zcaptured[0][1]
        E.run([[lay.ids[last]]], state=s2, keep=False, paths=[[([last], other, "q", (2, 2), cols)]], zcapture=[[last]])
        one = E.zcaptured[0][1]
        d = cols[2]
        rest = torch.tensor([c for c in range(z0.shape[-1]) if c not in set(d.tolist())])
        torch.testing.assert_close(one[2][:, d], whole[2][:, d], atol=1e-9, rtol=0)        # that head: as with the whole block's queries
        torch.testing.assert_close(one[2][:, rest], z0[2][:, rest], atol=1e-9, rtol=0)     # the block's other heads: untouched
        self.assertGreater(float((one[2][:, d] - z0[2][:, d]).abs().max()), 1e-9)
        torch.testing.assert_close(one[:2], z0[:2], atol=1e-9, rtol=0)                     # earlier blocks: untouched

    def test_head_outputs(self):
        import torch
        from steering import circuit
        E, prompt = self.E, self.prompts[("false_labelled", "person_first", "C", 0)]
        code, pv, ov = P.diff_fields(prompt)[0]
        pair = circuit.lookup_pair(E, prompt, code, pv, ov)
        a, b, n = pair["queries"]["person"], pair["queries"]["own"], pair["n_ctx"]
        self.assertEqual(a["ids"][a["cut"]:], b["ids"][b["cut"]:])                 # the questions share their ending
        self.assertNotEqual(a["ids"][pair["start"]:a["cut"]], b["ids"][pair["start"]:b["cut"]])
        self.assertIn(code, E.tok.decode([a["ids"][a["cut"] + j] for j in pair["groups"]["field"]]))
        self.assertEqual(sorted(j for g in circuit.GROUPS for j in pair["groups"][g]), list(range(pair["shared"])))
        runs, full = {}, {}
        for q, one in pair["queries"].items():
            after = list(range(n, len(one["ids"])))
            _, state, _ = E.run([one["ids"][:n]])
            logits, _, _ = E.run([one["ids"][n:]], state=state, keep=False, zcapture=[after])
            runs[q], full[q] = E.zcaptured[0][1], logits[0]
        self.assertEqual(tuple(runs["person"].shape), (E.n_layers, len(a["ids"]) - n, E.n_heads * E.head_dim))
        pos = list(range(a["cut"], len(a["ids"])))
        donor = runs["own"][:, b["cut"] - n:]
        own = runs["person"][:, a["cut"] - n:]
        _, state, _ = E.run([a["ids"][:n]] * 5)
        one = E.head_dims([(1, 2)])
        logits, _, _ = E.run([a["ids"][n:]] * 5, state=state, keep=False, zpatch=[
            [(pos, donor, None)],                                    # every head from the other question's run: that run
            [(pos, own, None)],                                      # its own outputs: nothing changes
            [(pos, donor, None), (pos, own, None)],                  # ... and back
            [(pos, donor, one)],                                     # one head
            None])
        torch.testing.assert_close(logits[0], full["own"], atol=1e-9, rtol=0)
        for r in (1, 2, 4):
            torch.testing.assert_close(logits[r], full["person"], atol=1e-9, rtol=0)
        self.assertGreater(float((logits[3] - full["person"]).abs().max()), 1e-6)
        self.assertGreater(float((logits[3] - full["own"]).abs().max()), 1e-6)
        # every other head held at its own value: the head's effect without the later heads' reaction to it
        rest = E.head_dims([(l, h) for l in range(E.n_layers) for h in range(E.n_heads) if (l, h) != (1, 2)])
        _, state, _ = E.run([a["ids"][:n]])
        again, _, _ = E.run([a["ids"][n:]], state=state, keep=False, zpatch=[[(pos, donor, None), (pos, own, rest)]])
        self.assertGreater(float((again[0] - full["person"]).abs().max()), 1e-6)
        self.assertGreater(float((again[0] - logits[3]).abs().max()), 1e-9)

    def test_mlp_outputs_and_steered_sampling(self):
        import torch
        E, lay = self.E, self.lay()
        n, after = lay.n_ctx, list(range(lay.n_ctx, len(lay.ids)))
        other = self.E.tok("Another question entirely, of some length?", add_special_tokens=False)["input_ids"]
        tails = [lay.ids[n:], (other * 40)[:len(after)]]
        _, state, _ = E.run([lay.ids[:n]] * 2)
        full, _, _ = E.run(tails, state=state, keep=False, mcapture=[after, after], zcapture=[after, after])
        m0, m1, z1 = E.mcaptured[0][1], E.mcaptured[1][1], E.zcaptured[1][1]
        self.assertEqual(tuple(m0.shape), (E.n_layers, len(after), E.hidden))
        got, _, _ = E.run([tails[0]] * 3, state=state.select([0, 0, 0]), keep=False,
                          mpatch=[[(after, m0, None)], [(after, m1, [1])], None])
        torch.testing.assert_close(got[0], full[0], atol=1e-9, rtol=0)            # its own MLP outputs: nothing changes
        torch.testing.assert_close(got[2], full[0], atol=1e-9, rtol=0)
        self.assertGreater(float((got[1] - full[0]).abs().max()), 1e-6)           # one block's MLP from the other run
        # sampled tokens under a steering vector: other replies than without it, rows without one unchanged
        v = torch.randn(E.n_layers + 1, E.hidden, dtype=torch.float64) * 3
        jobs = [Job(lay.ids[:-6], steer=(v, 1.0, (2, 3))), Job(lay.ids[:-6])]
        a = E.generate(jobs, 2, 8, seed=5, batch=2)
        b = E.generate([Job(lay.ids[:-6]), Job(lay.ids[:-6])], 2, 8, seed=5, batch=2)
        self.assertEqual(a[1], b[1])
        self.assertNotEqual(a[0], b[0])
        self.assertEqual(a, E.generate(jobs, 2, 8, seed=5, batch=2))

    def test_paired_rows(self):
        import torch
        E, lay = self.E, self.lay()
        n = lay.n_ctx
        v = torch.randn(E.n_layers + 1, E.hidden, dtype=torch.float64) * 0.5
        edit = [Edit("add", lay.line_tokens(), v)]
        _, state, _ = E.run([lay.ids[:n]] * 3, edits=[[], edit, []])
        alone, _, _ = E.run([lay.ids[n:]] * 3, state=state, keep=False)
        self.assertGreater(float((alone[0] - alone[1]).abs().max()), 1e-6)
        # row 0 takes every head's output from row 1 at every later token: it becomes row 1; row 2 is untouched
        got, _, _ = E.run([lay.ids[n:]] * 3, state=state, keep=False, zpair=[([0], [1], None)])
        torch.testing.assert_close(got[0], alone[1], atol=1e-9, rtol=0)
        torch.testing.assert_close(got[2], alone[0], atol=1e-9, rtol=0)
        # one head only: neither run
        one = E.head_dims([(1, 2)])
        got, _, _ = E.run([lay.ids[n:]] * 3, state=state, keep=False, zpair=[([0], [1], one)])
        self.assertGreater(float((got[0] - alone[0]).abs().max()), 1e-9)
        self.assertGreater(float((got[0] - alone[1]).abs().max()), 1e-9)
        torch.testing.assert_close(got[1], alone[1], atol=1e-9, rtol=0)
        # paired sampling: the follower reads the leader's tokens; with every head taken from a follower on the
        # edited records the leader samples as that prompt would; a leader paired with its own copy is unchanged
        ids = lay.ids[:-6]
        gen = lambda jobs: E._generate(jobs, 2, 8, torch.Generator().manual_seed(3), True)
        a = gen([Job(ids, split=n), Job(ids, edit, split=n, follow=0, pair=None)])
        self.assertEqual([r["text"] for r in a[0]], [r["text"] for r in a[1]])
        b = gen([Job(ids, edit, split=n), Job(ids, edit, split=n, follow=0, pair=None)])
        self.assertEqual([r["text"] for r in a[0]], [r["text"] for r in b[0]])
        c = gen([Job(ids, split=n), Job(ids, split=n, follow=0, pair=None)])
        self.assertNotEqual([r["text"] for r in a[0]], [r["text"] for r in c[0]])

    def test_key_and_value_paths(self):
        import torch
        E, lay = self.E, self.lay()
        lines = lay.line_tokens()
        v = torch.randn(E.n_layers + 1, E.hidden, dtype=torch.float64) * 0.5
        base, edited = E.evaluate([Job(lay.ids, capture=lines), Job(lay.ids, [Edit("add", lines, v)], capture=lines)])
        got = E.evaluate([Job(lay.ids, paths=[(lines, base["hidden"], "kv")]),       # its own stream: nothing changes
                          Job(lay.ids, paths=[(lines, edited["hidden"], "k")]),
                          Job(lay.ids, paths=[(lines, edited["hidden"], "v")]),
                          Job(lay.ids, paths=[(lines, edited["hidden"], "kv")]),
                          Job(lay.ids)])
        self.assertAlmostEqual(got[0]["p"], base["p"], places=9)
        self.assertAlmostEqual(got[4]["p"], base["p"], places=9)
        ps = [g["p"] for g in got[1:4]]
        self.assertTrue(all(abs(p - base["p"]) > 1e-6 for p in ps) and len({round(p, 9) for p in ps}) == 3)
        # only later tokens see the patched keys and values: the patched tokens' own stream is the unedited one
        again = E.evaluate([Job(lay.ids, paths=[(lines[-5:], edited["hidden"][:, -5:], "kv")], capture=lines[:20])])[0]
        torch.testing.assert_close(again["hidden"], base["hidden"][:, :20], atol=1e-9, rtol=0)

    def test_generation_matches_a_full_pass(self):
        import torch
        E, rec = self.E, self.lays[("false_neutral", "person_first", "C", 0)]
        v = torch.randn(E.n_layers + 1, E.hidden, dtype=torch.float64)
        jobs = [Job(rec.ids[:-6]), Job(rec.ids[:-6], [Edit("add", rec.line_tokens(), v)])]
        out = E.generate(jobs, samples=3, max_new_tokens=6, seed=1, batch=2)
        self.assertEqual([len(o) for o in out], [3, 3])
        self.assertEqual(out, E.generate(jobs, samples=3, max_new_tokens=6, seed=1, batch=2))
        self.assertNotEqual(out, E.generate(jobs, samples=3, max_new_tokens=6, seed=2, batch=2))
        self.assertTrue(all(x["n_tokens"] <= 6 for o in out for x in o))
        # the preallocated cache and the growing one give the same replies, also with rows of different lengths
        ragged = jobs + [Job(self.lay("false_labelled").ids[:-9])]
        for n in (6, 25):
            self.assertEqual(E.generate(ragged, 2, n, seed=3, batch=3), E.generate(ragged, 2, n, seed=3, batch=3, static=False))
        self.assertEqual(stated_probability('so\n{"probability": 0.25}'), 0.25)
        self.assertIsNone(stated_probability('{"probability": 7}'))


@unittest.skipIf(tokenizer_path() is None, "no Qwen3 tokenizer available")
class Vectors(unittest.TestCase):
    def test_vectors_and_edits(self):
        import torch
        E = tiny_engine()
        prompts = cell_prompts()
        means, pairs = I.Means(), {"false": ("false_neutral", "false_labelled")}
        lays = {}
        for (arm, order, role, rep), prompt in prompts.items():
            if role != "C" or arm == "strong_original":
                continue
            lay = P.layout(E.tok, BC.no_reasoning(prompt), E.kw)
            cap = I.capture_positions(lay)
            pooled = E.evaluate([Job(lay.ids, capture=cap, reduce=lambda h, lay=lay, cap=cap, prompt=prompt: I.pool(lay, h, cap, prompt))])[0]["hidden"]
            self.assertEqual({k[2] for k in pooled}, {"all", "same"})
            for half in (0, 1):
                for (owner, tokrole, lineset), v in pooled.items():
                    means.add((arm, order, "C", owner, tokrole, lineset, half), v * (1 + half))
            lays[(arm, order)] = lay
        V = I.build_vectors(means, pairs)
        m = means.mean
        np.testing.assert_allclose(
            V["slot|false_neutral|val"].numpy(),
            ((m("false_neutral", "own_first", "C", "person", "val") - m("false_neutral", "person_first", "C", "person", "val"))
             + (m("false_neutral", "person_first", "C", "own", "val") - m("false_neutral", "own_first", "C", "own", "val"))).numpy() / 2)
        np.testing.assert_allclose(
            V["label|false|person_first|own|eol"].numpy(),
            (m("false_labelled", "person_first", "C", "own", "eol") - m("false_neutral", "person_first", "C", "own", "eol")).numpy())
        self.assertIn("owner|false_labelled|hdr", V)
        I.add_random(V, 2)
        np.testing.assert_allclose(V["random1|false_neutral|val"].norm(dim=-1).numpy(), V["slot|false_neutral|val"].norm(dim=-1).numpy())
        rel = I.reliability(means, pairs)
        self.assertTrue((rel.split_half_cosine > 0.999).all())     # the halves were made proportional
        with tempfile.TemporaryDirectory() as tmp:
            means.cases = ["a"]
            means.save(Path(tmp) / "m.pt")
            again = I.Means.load(Path(tmp) / "m.pt")
            self.assertEqual((again.cases, again.n), (["a"], means.n))
        lay = lays[("false_neutral", "person_first")]
        slot = I.mean_edits(lay, V, "slot", "false_neutral")
        self.assertEqual(sorted(e.scale for e in slot), [-1.0] * 3 + [1.0] * 3)
        first = [e for e in slot if e.positions[0] in lay.blocks[0].line_tokens_all()]
        self.assertTrue(all(e.scale == 1.0 for e in first))
        owner = I.mean_edits(lay, V, "owner", "false_neutral", site="both")
        self.assertEqual(len(owner), 8)
        self.assertTrue(all((e.scale < 0) == (e.positions[0] in lay.block("person").line_tokens_all() + lay.block("person").header)
                            for e in owner))
        label = I.mean_edits(lays[("false_labelled", "own_first")], V, "label", "false_labelled", alpha=-1.0)
        self.assertTrue(all(e.scale == -1.0 for e in label))
        self.assertTrue(torch.equal(label[0].value, V[f"label|false|own_first|own|name"]))
        self.assertEqual(I.windows(36, 4), [(1, 9), (10, 18), (19, 27), (28, 36)])
        self.assertEqual(I.windows(40, 4), [(1, 10), (11, 20), (21, 30), (31, 40)])


@unittest.skipIf(tokenizer_path() is None, "no Qwen3 tokenizer available")
class EndToEnd(unittest.TestCase):
    def test_every_stage(self):
        from steering import analysis as A
        from steering import run as R
        E = tiny_engine()
        cfg = json.loads(json.dumps(R.DEFAULTS))
        cfg |= {"alphas": [0.5, 1.0, 1.5], "batch": 32, "random_seeds": 2, "dtype": "float64"}
        cfg["reason"] |= {"patients": 1, "samples": 2, "max_new_tokens": 6, "dtype": "float64"}
        cases = P.patients(4)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "tiny"
            d.mkdir()
            for stage in R.STAGES:
                R.run_stage(stage, E, d, cfg, cases)
            means = I.Means.load(d / "means.pt")
            self.assertEqual(sorted(means.cases), sorted(cases.index[cases == "fit"]))
            self.assertEqual(set(means.n.values()), {1})      # two fit patients, one per half
            stamp = {p: p.stat().st_mtime_ns for p in d.rglob("*.parquet")}
            for stage in ("baseline", "dose", "reason"):     # a second run recomputes nothing
                R.run_stage(stage, E, d, cfg, cases)
            self.assertEqual(stamp, {p: p.stat().st_mtime_ns for p in d.rglob("*.parquet")})
            self.assertEqual(I.Means.load(d / "means.pt").n, means.n)

            base = R.read_rows(d / "baseline")
            self.assertEqual(set(base.case_id), set(cases.index))
            b = base[base.readout == "belief"]
            self.assertEqual(len(b), len(cases) * (4 * 2 * 3 + 4 * 2 * 2))     # C, M, D by order; A, B twice, per arm
            self.assertTrue(b.value.between(0, 1).all())
            looks = base[base.readout == "lookup"]
            self.assertEqual(set(looks.arm), {"false_neutral", "false_labelled"})
            self.assertEqual(len(looks), len(cases) * 2 * 2 * 2 * 2)             # arms x orders x fields x queries
            pooled = np.load(next((d / "pooled").glob("*.npz")))
            self.assertEqual(pooled["false_neutral|person_first|own|val"].shape, (len(pooled["layers"]), E.hidden))

            test = sorted(cases.index[cases == "test"])
            dose = R.read_rows(d / "dose")
            self.assertEqual((set(dose.part), set(dose.variant), set(dose.arm)), ({"dose"}, {"slot@lines"}, {"false_neutral"}))
            reason = R.read_rows(d / "reason")
            self.assertEqual(len(reason), (5 + 4 + 2 + 2) * 2)      # the configured cells, two samples each
            self.assertEqual(set(reason[reason.kind == "transplant"].donor), {"false_neutral", "false_labelled"})
            self.assertEqual(set(reason.case_id), {test[0]})

            R.run_stage("controls", E, d, cfg, cases)
            ctl = R.read_rows(d / "controls")
            self.assertEqual(set(ctl.variant.str.replace(r"\d*@lines$", "", regex=True)), {"random", "slot"})
            self.assertEqual(set(ctl[ctl.variant == "slot@lines"].alpha), {0.25, 0.5})
            R.run_stage("probe", E, d, cfg, cases)
            probe = R.read_rows(d / "probe")
            self.assertEqual(set(probe.variant), {"slot@lines", "slot@lines:first", "slot@lines:second"})
            both = pd.concat([dose, ctl, probe])
            both = both[(both.variant == "slot@lines") & (both.readout == "belief") & both.part.isin(["dose"])]
            # every strength, in every cell and role, exactly once across the three stages
            counts = both[both.alpha.isin([-0.5, -0.25, -0.125, 0.125, 0.25, 0.5])].groupby(
                ["case_id", "arm", "block_order", "role", "alpha"]).size()
            self.assertEqual((set(counts), len(counts)), ({1}, len(test) * 4 * 3 * 6 - len(test) * 2 * 2 * 0))
            second = cfg | {"reason": cfg["reason"] | {"folder": "reason_controls", "base": False, "cells": [
                ["false", "plain", "person_first", [["slot", 0.25, None], ["slot", -0.25, None], ["randlabel0", 1.0, None]]],
                ["false", "plain", "own_first", [["label", 1.0, None]]]]}}
            R.run_stage("reason", E, d, second, cases)
            extra = R.read_rows(d / "reason_controls")
            self.assertEqual(sorted(set(zip(extra.variant, extra.alpha))), [("label@lines", 1.0), ("randlabel0@lines", 1.0),
                                                                           ("slot@lines", -0.25), ("slot@lines", 0.25)])
            cfg["branch"] |= {"folders": ["reason"], "patients": 2, "resamples": 2, "max_new_tokens": 5, "batch": 8, "dtype": "float64"}
            R.run_stage("branch", E, d, cfg, cases)       # (the tiny model's replies rarely qualify: the stage must still run)
            from steering import transcripts as T2
            pr = cell_prompts(test[0])[("false_neutral", "person_first", "C", 0)]
            (code, pv, ov), = [x for x in T2.differing(pr) if x[0] in T2.TERMS or x[0] in T2.NUMBER][:1]
            if code in T2.TERMS:
                line = f"- {T2.TERMS[code].split('|')[0].replace(chr(92), '')}es status: {ov}"
                got = T2.stated_span(line, pr)
                self.assertEqual((got[0], got[1], line[got[2]:got[3]], got[4]), (code, "model's", ov, pv))
                got = T2.stated_span(line.replace(f": {ov}", f": {pv.lower()}"), pr)
                self.assertEqual((got[1], got[4]), ("PERSON's", ov.lower()))
            else:
                num = lambda s: s.split()[0].rstrip("0").rstrip(".") if "." in s.split()[0] else s.split()[0]
                line = f"The patient is {num(ov)} years old, BMI {num(ov)}, sodium {num(ov)}."
                got = T2.stated_span(line, pr)
                self.assertEqual((got[1], line[got[2]:got[3]], got[4]), ("model's", num(ov), num(pv)))
            self.assertIsNone(T2.stated_span("Nothing about the disputed fields here.", pr))
            cfg["circuit"] |= {"scan_patients": 1, "patients": 1, "ks": [1, 4], "randoms": 2, "where_heads": 2, "batch": 64,
                               "min_gap": None}
            R.run_stage("circuit", E, d, cfg, cases)
            from steering import circuit
            ct = circuit.tables(d, cfg, draws=20)
            self.assertLess(float(ct["circuit_checks"].value.max()), 1e-6)
            self.assertEqual(len(ct["circuit_heads"]), E.n_layers * E.n_heads)
            self.assertEqual(set(ct["circuit_layerpos"].group), set(circuit.GROUPS) | {"suffix", "phrase_last"})
            lp = ct["circuit_layerpos"]
            # the whole shared ending patched after the last block is the donor's answer; after an early block it is not
            last = lp[(lp.arm == "all") & (lp.group == "suffix")].set_index("layer").effect
            self.assertAlmostEqual(float(last[E.n_layers]), 1.0, places=6)
            self.assertEqual(set(ct["circuit_faith"].kind), {"keep", "swap"})
            self.assertEqual(set(ct["circuit_where"].columns) - {"cf", "layer", "head"}, set(circuit.GROUPS) | {"phrase"})
            sel = json.loads((d / "circuit" / "selection.json").read_text())
            self.assertEqual(len(sel["query"]), E.n_layers * E.n_heads)
            cfg["circuit_reply"] |= {"folders": ["reason"], "ks": [1, 4], "randoms": 2, "batch": 8}
            R.run_stage("circuit_reply", E, d, cfg, cases)       # (the tiny model's replies rarely state a value: it must still run)
            # a reply written by hand that states the model's value of a disputed field first
            from steering import transcripts as T3
            words = {"diab": "Diabetes", "htn": "Hypertension", "cvd": "Cardiovascular disease", "pa": "Physical activity"}
            fake = []
            for case in cases.index[cases == "test"]:
                pr = R.case_prompts(case, cases, ["false_neutral"])[("false_neutral", "person_first", "C")]["prompt"]
                for code, pv, ov in T3.differing(pr):
                    line = f"1. {words[code]}: {ov}" if code in words else f"The patient is {ov.split('.')[0]} years old." if code == "age" else None
                    if line:
                        fake.append(dict(case_id=case, arm="false_neutral", block_order="person_first", variant="base", sample=0,
                                         text="Let me go through the record.\n" + line + '\n{"probability": 0.2}', value=0.2))
                        break
            self.assertTrue(fake)
            R.write_rows(d / "reason_fake" / "all.parquet", fake)
            cfg["circuit_reply"] |= {"folders": ["reason_fake"]}
            for f in (d / "circuit_reply").glob("*.parquet"):
                f.unlink()
            R.run_stage("circuit_reply", E, d, cfg, cases)
            rt = circuit.reply_table(d, cfg, draws=20)
            pooled = rt[(rt.arm == "all") & (rt.kind == "keep")].set_index(["k", "set"]).value
            self.assertAlmostEqual(float(pooled[(0, "none")]), 0.0, places=6)                    # every head from the edited run
            self.assertAlmostEqual(float(pooled[(E.n_layers * E.n_heads, "all")]), 1.0, places=6)  # ... and all put back
            self.assertEqual(set(rt.kind), {"keep", "swap", "swap_last"})
            cfg["circuit_fine"] |= {"candidates": 6, "greedy_patients": 1, "max_heads": 3, "stop": 2.0, "patients": 1, "wiring_heads": 5,
                                    "batch": 32, "randoms": 1}
            cfg["circuit_fine"]["reply"] |= {"folders": ["reason_fake"], "scan_replies": 1, "batch": 16, "ks": [1, 4], "randoms": 1}
            R.run_stage("circuit_fine", E, d, cfg, cases)
            self.assertEqual(len(json.loads((d / "circuit_fine" / "greedy.json").read_text())), 3)
            cfg["circuit_algorithm"] |= {"heads": 5, "patients": 2}
            R.run_stage("circuit_algorithm", E, d, cfg, cases)
            at = circuit.algorithm_tables(d)
            reads = at["circuit_reads"]
            self.assertEqual(set(reads.dest), {"phrase", "phrase_last", "pre", "field", "post", "tail", "last"})
            srcs = [c for c in reads.columns if c not in ("layer", "head", "cond", "dest")]
            self.assertIn("queried_field_value", srcs)
            total = reads[srcs].sum(1)
            self.assertTrue(bool((total <= 1 + 1e-6).all()) and float(total.max()) > 0.5, (float(total.min()), float(total.max())))
            self.assertEqual(len(at["circuit_writes"]), E.n_layers * E.n_heads)
            cd = at["circuit_code"]
            self.assertTrue(cd.cosine.between(-1 - 1e-6, 1 + 1e-6).all())
            self.assertIn("same cell, other half of the patients", set(cd.cell_b))
            cfg["circuit_concrete"] |= {"fit_patients": 2, "scan_patients": 1, "field_patients": 1, "patients": 1, "parts": 2, "batch": 64,
                                        "knock_batch": 16, "require_crossed": False, "reader_attention": 0.0, "vector_alphas": [1.0],
                                        "vector_layers": [2, 3], "vector_windows": [[2, 3]], "replies": 2, "reply_batch": 16}
            R.run_stage("circuit_concrete", E, d, cfg, cases)
            kt = circuit.concrete_tables(d, cfg)
            look = kt["concrete_lookup"]
            self.assertTrue({"order", "knock_from_question", "knock_from_tail", "knock_heads", "mlp_swap", "add_place", "add_owner",
                             "add_random"} <= set(look.test))
            swap = look[look.test == "mlp_swap"].set_index("group")
            # every block's MLP from the other question with every head held at base, plus the reverse, are the two halves
            self.assertGreater(float(swap.share.abs().max()), 1e-6)
            order = look[(look.test == "order") & (look.group == "suffix")].set_index("layer").share
            self.assertEqual(len(order), E.n_layers)
            self.assertIn("order_effect", kt["concrete_heads"].columns)
            self.assertIn("concrete_reply_add", kt)
            import torch as torch_
            pv = torch_.load(d / "circuit_concrete" / "place.pt")
            self.assertEqual(tuple(pv["place|last"].shape), (E.n_layers + 1, E.hidden))
            torch_.testing.assert_close(pv["random|last"].norm(dim=-1), pv["place|last"].norm(dim=-1), rtol=1e-5, atol=1e-8)
            cfg["references"] |= {"patients": 1, "samples": 2, "max_new_tokens": 5, "dtype": "float64", "group": 1, "batch": 16}
            R.run_stage("references", E, d, cfg, cases)
            rf = R.read_rows(d / "refs_own")
            self.assertEqual(set(rf.role), set("ABMD"))
            self.assertEqual(len(rf), 4 * 4 * 2)
            cfg["circuit_paired"] |= {"patients": 1, "samples": 2, "max_new_tokens": 5, "dtype": "float64", "group": 1, "heads": 4,
                                      "cells": [["false", "plain", "person_first", "twin", 0.0, ["lookup{n}", "every head"]],
                                                ["strong", "plain", "person_first", "slot", -0.25, ["random{n}"]]]}
            R.run_stage("circuit_paired", E, d, cfg, cases)
            pp = R.read_rows(d / "reason_paired")
            self.assertEqual(set(pp.variant), {"paired: lookup4 see the other arm's records", "paired: every head see the other arm's records",
                                               "paired: random4 see the slot-edited records"})
            self.assertEqual(len(pp), 3 * 2)
            cfg["circuit_steer"] |= {"patients": 1, "samples": 2, "max_new_tokens": 6, "dtype": "float64", "group": 1, "window": [2, 3],
                                     "reader_attention": 0.0}
            R.run_stage("circuit_steer", E, d, cfg, cases)
            st = R.read_rows(d / "reason_circuit")
            self.assertEqual(set(st.variant), {"steer:place@2-3", "steer:random@2-3", "knock:readers:own", "knock:random0:own",
                                               "knock:readers:person", "knock:random0:person"})
            self.assertEqual(len(st), 8 * 2)
            # the same stage with the reply barred from both headers, from a line of each record, and unedited
            cfg_h = cfg | {"circuit_steer": cfg["circuit_steer"] | {"folder": "reason_headers", "cells": [
                ["labelled", "own_first", [["base", None, None], ["knock", "all", "headers"], ["knock", "all", "first lines"]]]]}}
            R.run_stage("circuit_steer", E, d, cfg_h, cases)
            sh = R.read_rows(d / "reason_headers")
            self.assertEqual(set(sh.variant), {"base", "knock:all:headers", "knock:all:first lines"})
            self.assertEqual(set(sh.block_order), {"own_first"})
            texts_h = {v: list(g.sort_values("sample").text) for v, g in sh.groupby("variant")}
            self.assertNotEqual(texts_h["base"], texts_h["knock:all:headers"])
            # the program's pointer, invented records under reasoning
            from steering import program as PG
            cfg["head_sets"] |= {"lookup_sizes": [2, 4], "reader_attention": 0.0, "randoms": 1}
            cfg["circuit_program"] |= {"patients": 1, "offset": 0, "pointer_sets": ["readers", "value32"], "convert_window": [2, 3],
                                       "place_window": [2, 3], "per_domain": 1, "batch": 8}
            R.run_stage("circuit_program", E, d, cfg, cases)
            pr = circuit.read(d / "circuit_program" / "rows")
            self.assertEqual(set(pr.domain), {"ckd", "car", "loan", "bridge"})
            self.assertEqual(set(pr.records), {2, 3})
            self.assertLessEqual({"none", "pointer", "pointer to another record", "place vector", "place vector and pointer"},
                                 set(pr.variant))
            self.assertEqual(set(pr[pr.records == 3].variant) & {"place vector"}, set())       # the place vector is for two records
            self.assertEqual(set(pr.context), {"asked", "asked, conversion blocks cannot see the records", "another owner asked",
                                               "no owner named", "neither owner nor field named"})
            pt = PG.program_tables(d)
            self.assertLessEqual({"program", "program_cells", "program_unaided", "program_lookups"}, set(pt))
            cfg["program_reason"] |= {"per_domain": 1, "samples": 2, "max_new_tokens": 6, "dtype": "float64", "batch": 8,
                                      "cells": [["unlabelled|person_first", ["base", "cue", "bar:lookup4", "bar:all"]],
                                                ["labelled|person_first", ["base"]]]}
            R.run_stage("program_reason", E, d, cfg, cases)
            rr = circuit.read(d / "program_reason")
            self.assertEqual(set(rr.condition), {"base", "cue", "bar:lookup4", "bar:all", "reference: PERSON record alone",
                                                 "reference: own record alone"})
            self.assertEqual(len(rr), 3 * 2 * (2 + 4 + 1) * 2)      # kinds x versions x (references + conditions) x samples
            PG.program_reason_tables(d, draws=10)
            # every head after the phrase from tables of the program's variables
            from steering import head_tables as HT
            cfg["circuit_tables"] |= {"fit_patients": 2, "patients": 2, "offset": 0, "min_margin": -1e9, "boundary": 2,
                                      "sets": ["lookup4"], "batch": 8,
                                      "fit_split": "test", "boundaries": [1, 3], "bands": [[0, 1], [2, 3]]}
            R.run_stage("circuit_tables", E, d, cfg, cases)
            tr = circuit.read(d / "circuit_tables" / "rows")
            self.assertLessEqual({"model", "upstream", "all", "lookup4 from the tables, others live", "one band neutral, others live",
                                  "tables before block 2, later heads live", "tables before block 4, later heads live"}, set(tr.test))
            fitted = json.loads((d / "circuit_tables" / "fit.json").read_text())
            self.assertGreater(fitted["lookups"], 0)
            ht = HT.tables(d, cfg)
            self.assertLessEqual({"tables", "tables_fields", "tables_explained"}, set(ht))
            # a table fitted and read on the same lookups, one lookup per combination of the variables, is exact
            Tb = HT.Tables(["age"], [5, 7])
            lks = [dict(field="age", place=p_, owner=o_, value=v_, groups={"pre": [0, 1], "field": [2], "post": [3], "tail": [4], "last": [5]})
                   for p_, o_, v_ in ((1.0, 1.0, 5), (-1.0, 1.0, 7), (1.0, -1.0, 7), (-1.0, -1.0, 5))]
            torch_.manual_seed(1)
            base_, a_, b_, c_ = (torch_.randn(3, 6, 8, dtype=torch_.float64) for _ in range(4))
            zs = [base_ + lk["place"] * a_ + lk["owner"] * b_ + (1.0 if lk["value"] == 5 else -1.0) * c_ for lk in lks]
            for lk, z in zip(lks, zs):
                Tb.add(torch_, lk, z)
            Tb.solve(torch_, {("age", 5): 2, ("age", 7): 2})
            for lk, z in zip(lks, zs):
                torch_.testing.assert_close(Tb.predict(torch_, lk, lk["place"], lk["owner"], lk["value"]), z, atol=1e-4, rtol=0, check_dtype=False)
            torch_.testing.assert_close(Tb.predict(torch_, lks[0], 0.0, 0.0, None), base_, atol=1e-4, rtol=0, check_dtype=False)       # the neutral table
            torch_.testing.assert_close(Tb.predict(torch_, lks[0], -1.0, 1.0, 5), base_ - a_ + b_ + c_, atol=1e-4, rtol=0, check_dtype=False)
            # the readers' match: queries or keys exchanged alone
            from steering import circuit_steps as MT
            cfg["circuit_match"] |= {"patients": 1, "offset": 0, "reader_window": [2, 3], "convert_window": [1, 1], "scales": [1.0],
                                     "owners": True}
            for (arm_, order_), prompt_ in circuit.case_cells(sorted(cases.index[cases == "test"])[0], cases, cfg).items():
                x_ = MT.owners_exchanged(prompt_)       # contents and places stay; the headers trade
                a_, b_ = B.record_blocks(prompt_), B.record_blocks(x_)
                self.assertEqual([r["owner"] for r in b_], [r["owner"] for r in a_][::-1])
                self.assertEqual([r["fields"] for r in b_], [r["fields"] for r in a_])
                self.assertEqual(x_.split("\n\n")[2:], prompt_.split("\n\n")[2:])
                la_, lb_ = (P.layout(E.tok, t_, E.kw, prefix="") for t_ in (prompt_, x_))
                pa_, pb_ = MT.by_slot(la_, lb_)
                self.assertGreaterEqual(len(pa_), len(la_.line_tokens()) - 4)
                self.assertGreaterEqual(sum(la_.ids[i] == lb_.ids[j] for i, j in zip(pa_, pb_)), len(pa_) - 2)
            R.run_stage("circuit_match", E, d, cfg, cases)
            mt = MT.tables(d, cfg)["match"]
            self.assertEqual(set(mt.records), {"none", "keys, readers' blocks", "values, readers' blocks", "keys, conversion blocks",
                                               "keys and values, every block", "keys, readers' blocks, other page order",
                                               "values, readers' blocks, other page order", "keys, conversion blocks, other page order",
                                               "keys, every block, other page order", "values, conversion blocks, other page order",
                                               "keys, conversion blocks, owners exchanged", "values, conversion blocks, owners exchanged",
                                               "keys, readers' blocks, owners exchanged", "values, readers' blocks, owners exchanged",
                                               "keys and values, every block, owners exchanged"})
            self.assertLessEqual({"none", "the other question's", "place vector x1", "owner vector x1", "random vector x1"}, set(mt.queries))
            plain = mt[(mt.records == "none") & (mt.queries == "none")]
            self.assertAlmostEqual(float(plain.moved.iloc[0]), 0.0, places=4)     # nothing exchanged: the lookup's own answer
            self.assertGreater(float(mt[mt.queries == "the other question's"].moved.abs().max()), 1e-9)
            cfg["circuit_tag"] |= {"patients": 1, "offset": 0, "windows": [[1, 4], [2, 3]]}
            R.run_stage("circuit_tag", E, d, cfg, cases)
            tg_ = circuit.read(d / "circuit_tag" / "rows")
            self.assertEqual(set(tg_.question), {"owner", "place"})
            self.assertEqual(len(tg_), 4 * 4 * (1 + 2 * 3))        # cells x questions x conditions
            self.assertIn("tag", MT.tag_tables(d))
            cfg["circuit_phrase"] |= {"patients": 1, "offset": 0, "blocks": [1, 3], "windows": [[2, 3]], "batch": 8}
            R.run_stage("circuit_phrase", E, d, cfg, cases)
            ph = circuit.read(d / "circuit_phrase" / "rows")
            self.assertEqual(set(ph.test), {"other page order", "barred"})
            self.assertLessEqual({"the phrase", "the phrase's last token", "the tokens after the phrase barred from the phrase",
                                  "the phrase's tokens barred from the whole records"}, set(ph.what))
            self.assertIn("phrase", MT.phrase_tables(d, cfg))
            cfg["circuit_redirect"] |= {"patients": 1, "samples": 2, "max_new_tokens": 5, "dtype": "float64", "group": 1, "batch": 8,
                                        "sets": ["lookup4", "random_lookup4_0", "all", "other value heads than lookup4 0"]}
            R.run_stage("circuit_redirect", E, d, cfg, cases)
            rdr = R.read_rows(d / "reason_redirect")
            self.assertEqual(set(rdr.variant), {"redirect: lookup4", "redirect: random_lookup4_0", "redirect: all",
                                                "redirect: other value heads than lookup4 0"})
            vh = json.loads((d / "circuit_redirect" / "value_heads_reason_redirect.json").read_text())
            for l_, gs_ in vh["other value heads than lookup4 0"]["by_block"].items():        # none shared with the named set
                self.assertFalse(set(gs_) & set(vh["lookup4"]["by_block"].get(l_, [])))
            # with every value head's vectors exchanged, the reply is the one given when the model's record's lines hold PERSON's states
            case_r = sorted(cases.index[cases == "test"])[0]
            pr_ = R.case_prompts(case_r, cases, ["false_neutral"])[("false_neutral", "person_first", "C")]["prompt"]
            lay_r = P.layout(E.tok, pr_, E.kw, prefix="")
            plain_r = E.generate([Job(lay_r.ids)], 2, 5, seed=int(__import__("second_order.config", fromlist=["x"]).load()["seed"]) + sum(map(ord, case_r)), batch=8)
            got_r = [list(rdr[(rdr.variant == "redirect: all") & (rdr["sample"] == s_)].text)[0] for s_ in (0, 1)]
            self.assertNotEqual(got_r, [x["text"] for x in plain_r[0]])
            cfg["circuit_tables_reply"] |= {"folders": ["reason"], "max_tokens": 700, "fit_patients": 2, "boundary": 2, "scales": [1.0]}
            cfg["circuit_tables"] |= {"fit_split": "test"}
            R.run_stage("circuit_tables_reply", E, d, cfg, cases)       # (the tiny model's replies rarely state a value: it must still run)
            self.assertTrue((d / "circuit_tables_reply" / "rows.parquet").exists())
            # tokens hidden from every other token: what they are cannot matter to anything after them
            ph_ = R.case_prompts(sorted(cases.index[cases == "test"])[0], cases, ["false_labelled"])[("false_labelled", "person_first", "C")]["prompt"]
            lh_ = P.layout(E.tok, ph_, E.kw, prefix="")
            hid_ = [t for b_ in lh_.blocks for t in b_.header + b_.extra]
            other_ids = list(lh_.ids)
            for t in hid_:
                other_ids[t] = (other_ids[t] + 7) % 200 + 5
            outs_ = []
            for seq_ in (lh_.ids, other_ids):
                st_ = PG.run_hidden(E, seq_, hid_, lh_.n_ctx)
                lg_, _, _ = E.run([seq_[lh_.n_ctx:]], state=st_, keep=False, knock=[PG.CI.knock_of([(l, h) for l in range(E.n_layers) for h in range(E.n_heads)], hid_)])
                outs_.append(lg_[0])
            self.assertTrue(bool((outs_[0] - outs_[1]).abs().max() < 1e-8))
            plain_, _, _ = E.run([lh_.ids])
            self.assertTrue(bool((outs_[0] - plain_[0]).abs().max() > 1e-4))         # and hiding them does change the answer
            cfg["circuit_reply_headers"] |= {"folders": ["reason"], "replies": 2, "owner_heads": [[0, 1], [1, 0]]}
            R.run_stage("circuit_reply_headers", E, d, cfg, cases)
            self.assertTrue((d / "circuit_reply_headers" / "rows.parquet").exists())
            rh_ = pd.read_parquet(d / "circuit_reply_headers" / "rows.parquet")
            if len(rh_):
                self.assertEqual(rh_.condition.nunique(), 11)
                nb_ = rh_[rh_.condition == "nothing barred"]
                self.assertTrue(np.allclose(nb_.margin, nb_.base))
            cfg["circuit_reply_headers"] |= {"folder": "circuit_reply_headers_cross", "cross": True, "layout": 3}
            R.run_stage("circuit_reply_headers", E, d, cfg, cases)
            self.assertTrue((d / "circuit_reply_headers_cross" / "rows.parquet").exists())
            cfg["circuit_reply_headers"] |= {"folder": "circuit_reply_headers", "cross": False}
            HT.reply_tables(d, cfg)
            cfg["decision"] |= {"folders": ["reason"], "batch": 8, "attention_parts": 2}
            R.run_stage("decision", E, d, cfg, cases)       # (the tiny model's replies rarely state a value: it must still run)
            A.MIN_CASES = 2
            tables = A.analyse(d, cfg, resamples=20)
            found = json.loads((d / "analysis" / "checks.json").read_text())
            self.assertIn("baseline_median_digit_mass", found)
            e = tables["edited_cells"]
            self.assertEqual(set(e.part), {"control", "dose", "probe"})
            self.assertEqual(len(tables["dose_trend"]), 4)       # the slot vector in both arms, two orders each
            self.assertLessEqual({"base", "label@lines", "slot@lines", "random0@lines", "owner@lines", "label_false@lines",
                                  "transplant:lines", "randlabel0@lines", "steer:place@2-3", "steer:random@2-3",
                                  "knock:readers:own", "knock:random0:own", "knock:readers:person", "knock:random0:person"},
                                 set(tables["reason_cells"].variant))
            self.assertIn("## Strength", (d / "analysis" / "RESULTS.md").read_text())
            # the lock refuses changed vectors
            import torch
            V = torch.load(d / "vectors.pt")
            V["slot|false_neutral|val"] += 1
            torch.save(V, d / "vectors.pt")
            with self.assertRaises(ValueError):
                R.load_vectors(d, "cpu")

    def test_estimator_recovers_a_planted_leak(self):
        from steering import analysis as A
        rng = np.random.default_rng(0)
        n, rows = 300, []
        x, m = rng.normal(0, 1, n), rng.normal(-2, 0.5, n)
        for i in range(n):
            vals = {"A": x[i], "B": 0.0, "M": m[i], "D": m[i] + 0.8 * x[i], "C": m[i] + 0.8 * 0.7 * x[i]}
            for role, v in vals.items():
                rows.append(dict(case_id=f"c{i}", split="test", arm="false_neutral", role=role, replicate=0, value=v,
                                 block_order="shared" if role in "AB" else "person_first"))
                if role in "CMD":
                    rows.append(dict(rows[-1], block_order="own_first", value=v if role != "C" else m[i]))
        base = pd.DataFrame(rows)
        cells = A.baseline_cells(base, ["false_neutral"], 200, 0, "planted").set_index("order")
        self.assertAlmostEqual(cells.loc["person_first", "lambda"], 0.7, places=6)
        self.assertAlmostEqual(cells.loc["own_first", "lambda"], 0.0, places=6)
        self.assertAlmostEqual(cells.loc["person_first", "beta_D"], 0.8, places=6)
        # an edit that halves the leak sits halfway to a cell with none
        sig = 1 / (1 + np.exp(-(m + 0.8 * 0.35 * x)))
        edited = pd.DataFrame(dict(case_id=[f"c{i}" for i in range(n)], arm="false_neutral", block_order="person_first",
                                   role="C", readout="belief", value=sig, part="mean", variant="v", alpha=1.0, lo=1, hi=4,
                                   donor=""))
        none = base.assign(arm="false_labelled")       # the other arm of the pair: no leak in either order
        none.loc[none.role == "C", "value"] = np.repeat(m, 2)
        got = A.edited_cells(pd.concat([base, none]), edited, {"false": ("false_neutral", "false_labelled")}, 200, 0).iloc[0]
        self.assertAlmostEqual(got["lambda"], 0.35, places=3)
        self.assertAlmostEqual(got["fraction"], 0.5, places=2)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(tokenizer_path() is None, "no Qwen3 tokenizer available")
class Program(unittest.TestCase):
    def test_program_and_items(self):
        from steering import program as K
        from steering import run as R
        recs = [("a", {"age": "59", "diab": "Yes"}), ("own", {"age": "39", "diab": "No"})]
        names = {"Elena": "a"}
        q = K.ask("the record Elena was shown", ("Age", "age"))
        self.assertEqual(K.program(recs, q, names), dict(owner="a", place=0, code="age", value="59"))
        self.assertEqual(K.program(recs[::-1], q, names)["place"], 1)
        self.assertEqual(K.program(recs, K.ask("your own record", ("Self-reported diabetes", "diab")), names)["value"], "No")
        # no owner named: the nearest record, whichever it is
        self.assertEqual(K.program(recs, K.ask(None, ("Age", "age")), names), dict(owner=None, place=1, code="age", value="39"))
        self.assertEqual(K.program(recs[::-1], K.ask(None, ("Age", "age")), names)["value"], "59")
        self.assertIsNone(K.program(recs, K.ask(None, None), names)["code"])
        E = tiny_engine()
        cfg = json.loads(json.dumps(R.DEFAULTS))
        cases = P.patients(4)
        case = sorted(cases.index[cases == "test"])[0]
        items = K.ckd_items(case, cases, cfg)
        self.assertEqual(len(items), 4)
        for it in items:        # the item's question is the lookup question of the earlier stages, character for character
            prompt = next(p for (arm, order), p in circuit_cells(case, cases, cfg).items()
                          if f"{'labelled' if arm == 'false_labelled' else 'unlabelled'}|{order}" == it.cell)
            for owner in ("person", "own"):
                self.assertEqual(it.records + "\n\n" + it.question(owner, it.fields[0]),
                                 P.lookup_prompt(prompt, it.fields[0][1], owner))
            enc = K.encode(E, it)
            pair = lookup_pair_of(E, prompt, it.fields[0][1])
            self.assertEqual(enc["ids"][("asked", "person")], pair["queries"]["person"]["ids"])
            self.assertEqual((enc["tok"]["person"], enc["tok"]["own"]), (pair["tp"], pair["to"]))
            for owner in it.owners:     # the pointer is a token of that owner's value for the field
                lay = enc["lays"][("asked", owner)]
                self.assertIn(enc["ptr"][owner], lay.block(owner).lines[it.fields[0][1]].val)
                self.assertNotEqual(lay.ids[enc["ptr"]["person"]], lay.ids[enc["ptr"]["own"]])
                said = K.program([(o, it.values[o]) for o in it.owners], enc["texts"][("asked", owner)], it.names)
                self.assertEqual((it.owners[said["place"]], said["value"]), (owner, it.values[owner][it.fields[0][1]]))
        inv = K.invented(1, 2)
        self.assertEqual(len(inv), 3 * 2 * (2 * 2 * 2 + 2 * 3))
        ok = 0
        for it in inv:
            n = len(it.owners)
            self.assertEqual(len(it.records.split("\n\n")), n)
            vals = [it.values[o][it.fields[0][1]] for o in it.owners]
            self.assertEqual(len(set(vals)), n)                 # every record gives the field its own value
            enc = K.encode(E, it)
            if enc is None:
                continue
            ok += 1
            self.assertEqual(len(set(enc["tok"].values())), n)
            lay = enc["lays"][("asked", it.owners[0])]
            self.assertEqual([b.owner for b in lay.blocks], it.owners)
            for o in it.owners:
                line = lay.block(o).lines[it.fields[0][1]]
                self.assertEqual(E.tok.decode([lay.ids[t] for t in line.val]).strip(), it.values[o][it.fields[0][1]])
            self.assertEqual("End of Record" in it.records, it.cell.startswith("labelled"))
        self.assertGreater(ok, 0.9 * len(inv))
        two = [it for it in inv if len(it.owners) == 2]
        self.assertIn("What probability does", K.reason_prompt(two[0]))
        self.assertIn("whose record", K.reason_prompt(two[0], cue=True))
        self.assertTrue(K.own_prompt(two[0], "a").startswith("Your record for this"))
        self.assertAlmostEqual(K.leak(np.array([1.0, 2.0]), np.array([1.0, 2.0])), 0.0)
        self.assertAlmostEqual(K.leak(np.array([-1.0, -2.0]), np.array([1.0, 2.0])), 1.0)
        self.assertAlmostEqual(K.leak(np.array([0.0, 0.0]), np.array([1.0, 2.0])), 0.5)
        self.assertTrue(K.mentions("the pads are at 1 mm now", "1 mm"))
        self.assertFalse(K.mentions("tread of 1.2 mm", "2 mm"))


@unittest.skipIf(tokenizer_path() is None, "no Qwen3 tokenizer available")
class Guards(unittest.TestCase):
    """Mistakes in how a stage is called are reported, not passed over."""

    def test_unknown_stage_and_missing_replies(self):
        from steering import run as R
        cfg = json.loads(json.dumps(R.DEFAULTS))
        empty_run = Path(tempfile.gettempdir()) / "steering-run-that-does-not-exist"
        with self.assertRaisesRegex(ValueError, "unknown stage"):
            R.run_stage("no_such_stage", None, empty_run, cfg, P.patients(4))
        with self.assertRaisesRegex(ValueError, "run the reason stage first"):
            R.unedited_replies(empty_run, ["reason"])

    def test_paired_jobs_stay_in_one_batch(self):
        E = tiny_engine()
        ids = P.layout(E.tok, cell_prompts()[("false_neutral", "person_first", "C", 0)], E.kw, prefix="").ids
        jobs = [Job(ids, split=len(ids) - 2), Job(ids, follow=0, split=len(ids) - 2), Job(ids, split=len(ids) - 2)]
        with self.assertRaisesRegex(ValueError, "one batch"):
            E.generate(jobs, 1, 2, seed=1, batch=2)
        self.assertEqual(len(E.generate(jobs, 1, 2, seed=1, batch=3)), 3)

    def test_probability_question_needs_one_person(self):
        from steering import program as K
        from steering import circuit
        three = next(it for it in K.invented(1, 1) if len(it.owners) == 3)
        with self.assertRaisesRegex(ValueError, "one other person"):
            K.reason_prompt(three)
        self.assertEqual(circuit.direct_rows(None, []), [])


def circuit_cells(case, cases, cfg):
    from steering import circuit
    return circuit.case_cells(case, cases, cfg)


def lookup_pair_of(E, prompt, code):
    from steering import circuit
    pv, ov = next((p, o) for c, p, o in P.diff_fields(prompt) if c == code)
    return circuit.lookup_pair(E, prompt, code, pv, ov)
