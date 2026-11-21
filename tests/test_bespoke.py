"""Bespoke dataset: loading, exact Bayes answers, prompt invariants and comprehension questions."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from latent_beliefs.ckd.parse import parse_response
from second_order import bespoke as BS
from second_order.comprehension import parse_choice
from second_order.conditions import CONDITIONS

FIXTURE = str(Path(__file__).parents[1] / "datasets" / "example_bespoke_items.yaml")
CURSE = ["A", "B", "C", "M", "D", "B_F", "C_F", "M_F", "D_F"]


def prompt(case_id: str, condition: str) -> str:
    return BS.build_prompt(case_id, condition, path=FIXTURE)


class Loading(unittest.TestCase):
    def test_fixture_loads_and_has_placebos(self):
        items = BS.load(FIXTURE)
        self.assertEqual(len(items), 11)
        self.assertEqual(sum(i.placebo for i in items), 2)
        self.assertEqual(BS.case_ids(FIXTURE)[0], "bespoke__marbles_01")
        self.assertEqual([w for i in items for w in BS.checks(i)], [])

    def test_bare_false_key_survives_yaml(self):
        item = BS.items_by_case(FIXTURE)["bespoke__marbles_01"]
        self.assertEqual(set(item.false), {"d1", "d2"})

    def test_invalid_items_are_rejected(self):
        bad = {
            "partial id": "partial: [zz]",
            "no replacement": "partial: [d1]",
            "same text": "partial: [d1]\n    false: {d1: {text: 'Red.', p_true: 0.5, p_false: 0.5}}",
            "all partial": "partial: [d1]\n    false: {d1: {text: 'Blue.', p_true: 0.25, p_false: 0.75}}",
        }
        for name, tail in bad.items():
            evidence = "[{id: d1, text: 'Red.', p_true: 0.75, p_false: 0.25}"
            if name != "all partial":
                evidence += ", {id: d2, text: 'Red again.', p_true: 0.75, p_false: 0.25}"
            evidence += "]"
            text = (f"items:\n  - id: x\n    scenario: s\n    proposition: p\n    prior: 0.5\n"
                    f"    evidence: {evidence}\n    {tail}\n")
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
                handle.write(text)
            with self.assertRaises(ValueError, msg=name):
                BS.load(handle.name)


class Bayes(unittest.TestCase):
    def test_hand_computed_answers(self):
        items = BS.items_by_case(FIXTURE)
        m1 = items["bespoke__marbles_01"]  # R R B R from a 3:1 bag, fair coin
        self.assertAlmostEqual(m1.bayes("full"), 9 / 10)
        self.assertAlmostEqual(m1.bayes("partial"), 1 / 2)   # B R
        self.assertAlmostEqual(m1.bayes("false"), 1 / 10)    # B B B R
        t1 = items["bespoke__test_01"]  # prior 1/5, + - +, LR 4 each way
        self.assertAlmostEqual(t1.bayes("full"), 1 / 2)
        self.assertAlmostEqual(t1.bayes("partial"), (0.25 / 4) / (1 + 0.25 / 4))
        w2 = items["bespoke__witness_02"]
        self.assertAlmostEqual(w2.bayes("full"), 2.25 / 3.25)

    def test_placebo_gaps_are_zero_and_others_are_not(self):
        t = BS.table(FIXTURE)
        for _, row in t.iterrows():
            for setting in ("partial", "false"):
                gap = row[f"bayes_gap_{setting}"]
                (self.assertAlmostEqual if row.placebo else self.assertNotAlmostEqual)(gap, 0.0)

    def test_bayes_answer_follows_the_believer(self):
        cid = "bespoke__marbles_01"
        item = BS.items_by_case(FIXTURE)[cid]
        expected = {"A": "full", "B": "partial", "B_F": "false", "D": "full", "M": "partial",
                    "C": "partial", "D_F": "full", "M_F": "false", "C_F": "false"}
        for cond, setting in expected.items():
            self.assertAlmostEqual(BS.bayes_answer(cid, cond, FIXTURE), item.bayes(setting), msg=cond)

    def test_given_bayes_values_override(self):
        raw = {"id": "g", "scenario": "s", "proposition": "p", "bayes": {"full": 0.7, "partial": 0.4, False: 0.2},
               "evidence": [{"id": "a", "text": "A."}, {"id": "b", "text": "B."}], "partial": ["a"],
               "false": {"b": {"text": "Not B."}}}
        item = BS.parse_item(raw)
        BS.validate(item)
        self.assertEqual((item.bayes("full"), item.bayes("partial"), item.bayes("false")), (0.7, 0.4, 0.2))


class Prompts(unittest.TestCase):
    def test_every_prompt_ends_with_the_first_order_json_instruction_and_parses(self):
        for cid in BS.case_ids(FIXTURE):
            for cond in CURSE:
                p = prompt(cid, cond)
                self.assertTrue(p.endswith("\n\n" + BS.FORMAT + "\n"), (cid, cond))
                self.assertIn('{"probability": <number>}', p)
        self.assertEqual(parse_response("probability", 'He would say...\n{"probability": 0.42}')[0], 0.42)

    def test_m_is_d_with_the_agents_evidence_shown(self):
        for cid in BS.case_ids(FIXTURE):
            item = BS.items_by_case(FIXTURE)[cid]
            full = BS.evidence_lines(item.observations("full"))
            for m, d, setting in (("M", "D", "partial"), ("M_F", "D_F", "false")):
                shown = BS.evidence_lines(item.observations(setting))
                self.assertEqual(prompt(cid, m), prompt(cid, d).replace(full, shown, 1), (cid, m))

    def test_b_is_a_with_the_agents_evidence_and_matches_m(self):
        for cid in BS.case_ids(FIXTURE):
            item = BS.items_by_case(FIXTURE)[cid]
            full = BS.evidence_lines(item.observations("full"))
            for b, m, setting in (("B", "M", "partial"), ("B_F", "M_F", "false")):
                shown = BS.evidence_lines(item.observations(setting))
                self.assertEqual(prompt(cid, b), prompt(cid, "A").replace(full, shown, 1))
                head = prompt(cid, b).split("\n\n" + BS.SELF_QUESTION.split("{")[0])[0]
                self.assertTrue(prompt(cid, m).startswith(head + "\n\n"))

    def test_c_shows_everything_and_lists_the_agents_evidence(self):
        for cid in BS.case_ids(FIXTURE):
            item = BS.items_by_case(FIXTURE)[cid]
            full = BS.EVIDENCE_HEADER + BS.evidence_lines(item.observations("full")) + "\n\n"
            c = prompt(cid, "C")
            self.assertIn(full, c)
            self.assertIn("He saw only the following evidence:\n"
                          + BS.evidence_lines(item.observations("partial")) + "\n", c)
            cf = prompt(cid, "C_F")
            self.assertIn(full, cf)
            self.assertIn("He was told:\n" + BS.evidence_lines(item.observations("false")) + "\n", cf)
            for cond in ("C_F", "M_F", "D_F"):
                self.assertIn("someone else reported it to him", prompt(cid, cond))

    def test_m_contains_nothing_private(self):
        for cid in BS.case_ids(FIXTURE):
            item = BS.items_by_case(FIXTURE)[cid]
            held = [o.text for o in item.evidence if o.id not in set(item.partial)]
            for text in held:
                self.assertNotIn(text, prompt(cid, "M"))
            for oid in item.false:
                true = next(o.text for o in item.evidence if o.id == oid)
                self.assertNotIn(true, prompt(cid, "M_F"))

    def test_verbatim_prompts_override(self):
        raw = {"id": "v", "scenario": "s", "proposition": "p", "prior": 0.5,
               "evidence": [{"id": "a", "text": "A.", "p_true": 0.6, "p_false": 0.4},
                            {"id": "b", "text": "B.", "p_true": 0.6, "p_false": 0.4}],
               "partial": ["a"], "false": {"b": {"text": "Not B.", "p_true": 0.4, "p_false": 0.6}},
               "prompts": {"A": "custom"}}
        item = BS.parse_item(raw)
        self.assertEqual(item.prompts["A"], "custom")
        self.assertIn("lacks the JSON probability instruction", " ".join(BS.checks(item)))

    def test_outputs_other_than_the_probability_are_refused(self):
        with self.assertRaises(ValueError):
            BS.build_prompt("bespoke__marbles_01", "A", "likert", path=FIXTURE)


class Parsing(unittest.TestCase):
    def test_json_answers_parse(self):
        for text, value in (('reasoning\n{"probability": 0.42}', 0.42), ('```json\n{"probability": 0.0}\n```', 0.0),
                            ('{"probability": 0.9} then {"probability": 0.3}', 0.3), ('"probability": "65%"', 0.65)):
            self.assertEqual(BS.parse_response("probability", text)[0], value, text)

    def test_numbers_in_the_question_are_not_answers(self):
        """The first-order fallback reads 'the probability that ... Bag 1' as 1; bespoke parsing must not."""
        text = "We need the probability that the marbles were drawn from Bag 1.\nFinal: $\\boxed{0.9}$"
        self.assertEqual(parse_response("probability", text)[0], 1.0)  # the first-order parser's fallback
        self.assertIsNone(BS.parse_response("probability", text)[0])
        self.assertIsNone(BS.parse_response("probability", "")[0])
        self.assertIsNone(BS.parse_response("probability", '{"answer": 0.4}')[0])


class Comprehension(unittest.TestCase):
    def test_correct_option_is_the_agents_evidence(self):
        for cid in BS.case_ids(FIXTURE):
            item = BS.items_by_case(FIXTURE)[cid]
            for cond, setting in (("C", "partial"), ("C_F", "false")):
                q = BS.comprehension(cid, cond, FIXTURE)
                self.assertEqual(q.options[q.answer], "; ".join(o.text for o in item.observations(setting)))
                self.assertGreaterEqual(len(q.options), 2)
                self.assertEqual(len(set(q.options)), len(q.options))
                self.assertEqual(parse_choice(f"Choice: {q.answer_letter}", q.options), q.answer)
        self.assertTrue(all(CONDITIONS[c].role == "C" for c in ("C", "C_F")))
        with self.assertRaises(ValueError):
            BS.comprehension(BS.case_ids(FIXTURE)[0], "M", FIXTURE)

    def test_bayes_answers_stay_inside_the_clip(self):
        for item in BS.load(FIXTURE):
            for setting in BS.EVIDENCE_SETS:
                self.assertTrue(0.05 <= item.bayes(setting) <= 0.95)
                self.assertTrue(math.isfinite(BS.logit(item.bayes(setting))))


if __name__ == "__main__":
    unittest.main()
