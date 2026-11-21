"""The two datasets: integrity, the condition mapping against the Methods design, and answer parsing."""

from __future__ import annotations

import re
import unittest


from second_order import datasets as DS
from second_order.conditions import CONDITIONS, setting_conditions

CKD = DS.rows("ckd", list(DS.CKD_FILES))
BESPOKE = DS.rows("bespoke")


def blocks(prompt: str) -> dict[str, frozenset[str]]:
    """CKD prompt -> {"own": record lines, "person": record lines} for the records shown."""
    out = {}
    for block in prompt.split("\n\n"):
        head, *lines = block.split("\n")
        if head.startswith("Your record"):
            out["own"] = frozenset(lines)
        elif "has only the following record" in head:
            out["person"] = frozenset(lines)
    return out


def ckd_prompt(case_id: str, condition: str, output: str = "probability/normal", replicate: int = 0) -> str:
    return DS.row("ckd", case_id, condition, output, replicate)["prompt"]


class Integrity(unittest.TestCase):
    def test_files_match_their_checksums(self):
        self.assertTrue(all(DS.ckd_checksums().values()), DS.ckd_checksums())

    def test_row_counts(self):
        counts = CKD.groupby("battery").size().to_dict()
        self.assertEqual(counts, {"main": 79650, "high_thresholds": 45900, "role_swap": 1500})
        self.assertEqual(len(BESPOKE), 190)
        self.assertEqual(len(DS.patients()), 150)
        self.assertEqual(len(DS.case_ids("bespoke")), 20)
        self.assertEqual(DS.placebo_ids(), {"p01", "p02"})

    def test_every_row_has_a_known_condition_and_a_unique_key(self):
        for frame in (CKD, BESPOKE):
            self.assertTrue(frame.condition.isin(CONDITIONS).all())
        self.assertEqual(len(DS.lookup("ckd")), len(CKD))
        self.assertEqual(len(DS.lookup("bespoke")), len(BESPOKE))

    def test_every_option_has_a_meaning(self):
        for r in CKD.drop_duplicates(["question_id", "order"]).itertuples():
            opts = DS.options(r.prompt)
            values = [DS.option_value(r.question_id, t) for t in opts.values()] if opts else []
            if r.question_id == "probability":
                self.assertEqual(opts, {})
            else:
                self.assertEqual(len(set(values)), len(values), (r.question_id, r.order))

    def test_flipped_orders_reverse_the_options(self):
        case_id = DS.case_ids("ckd")[0]
        for q in ("decision", "bet_4", "likert", "quintile", "threshold_1"):
            normal = list(DS.options(ckd_prompt(case_id, "A", f"{q}/normal")).values())
            flip = list(DS.options(ckd_prompt(case_id, "A", f"{q}/flip")).values())
            self.assertEqual(flip, normal[::-1], q)


class CkdDesign(unittest.TestCase):
    """The records each condition shows match the Methods table (D, D2 shared by both settings)."""

    @classmethod
    def setUpClass(cls):
        cls.cases = DS.case_ids("ckd")[:10]

    def test_records_shown_per_condition(self):
        for case_id in self.cases:
            full = blocks(ckd_prompt(case_id, "A"))["own"]
            for setting in ("partial", "false"):
                k = setting_conditions("ckd", setting)
                for role in ("A", "B", "C", "M", "D"):
                    self.assertEqual("Your chart gives the verified patient information." in ckd_prompt(case_id, k[role]),
                                     setting == "false")
                b = blocks(ckd_prompt(case_id, k["B"]))
                person = b["own"]  # PERSON's evidence: E_p or E_f
                self.assertEqual(set(b), {"own"})
                self.assertNotEqual(person, full)
                if setting == "partial":
                    self.assertLess(person, full)
                self.assertEqual(blocks(ckd_prompt(case_id, k["M_single"])), {"person": person})
                self.assertEqual(blocks(ckd_prompt(case_id, k["D_single"])), {"person": full})
                self.assertEqual(blocks(ckd_prompt(case_id, k["M"])), {"own": person, "person": person})
                self.assertEqual(blocks(ckd_prompt(case_id, k["D"])), {"own": full, "person": full})
                self.assertEqual(blocks(ckd_prompt(case_id, k["C"])), {"own": full, "person": person})
                swap = blocks(ckd_prompt(case_id, "C_swap" + ("_F" if setting == "false" else "")))
                self.assertEqual(swap, {"own": person, "person": full})

    def test_matched_layout_differs_only_in_evidence(self):
        for suffix in ("", "_F"):
            prompts = CKD[CKD.condition.isin([r + suffix for r in "CMD"]) & (CKD.question_id != "check")].copy()
            prompts["layout"] = prompts.prompt.str.replace(r"(?m)^.+ \(\w+\) is .+\.$\n?", "", regex=True)
            groups = prompts.groupby(["case_id", "output"])
            self.assertTrue(groups.size().eq(3).all())
            self.assertTrue(groups.layout.nunique().eq(1).all())

    def test_a_and_b_have_two_replicates_and_attributions_one(self):
        reps = CKD[CKD.battery == "main"].groupby("condition").replicate.max().to_dict()
        for condition, top in reps.items():
            self.assertEqual(top, 1 if CONDITIONS[condition].role in ("A", "B") else 0, condition)

    def test_person_names_vary_and_match_the_patient(self):
        patients = DS.patients()
        self.assertGreater(patients.person_name.nunique(), 10)
        for case_id in self.cases:
            self.assertIn(patients.loc[case_id, "person_name"], ckd_prompt(case_id, "C"))


class BespokeDesign(unittest.TestCase):
    def test_bayes_answers_follow_the_believer(self):
        by = {(r.case_id, r.condition): r for r in BESPOKE.itertuples()}
        for item in DS.case_ids("bespoke"):
            p = lambda c, item=item: float(by[(item, c)].normative_p)  # noqa: E731
            self.assertEqual(p("M"), p("B"))
            self.assertEqual(p("D"), p("A"))
            self.assertEqual(p("C"), p("M"))
            self.assertEqual(float(by[(item, "C")].full_leak_p), p("A"))
            if (item, "C_F") in by:
                self.assertEqual(p("M_F"), p("B_F"))
                self.assertEqual(p("C_F"), p("M_F"))
                self.assertEqual(p("C_ctrl_F"), p("D_F"))

    def test_placebo_items_have_no_gap(self):
        for item in DS.placebo_ids():
            values = set(BESPOKE[BESPOKE.case_id == item].normative_p)
            self.assertEqual(len(values), 1)

    def test_bayes_answers_stay_inside_the_logit_clip(self):
        p = BESPOKE.normative_p.astype(float)
        self.assertTrue(((p > 0.01) & (p < 0.99)).all())

    def test_keyed_comprehension_answers_score_as_correct(self):
        rows = BESPOKE[BESPOKE.comprehension_q != ""]
        self.assertEqual(set(rows.condition), set(DS.COMPREHENSION_CONDITIONS))
        for r in rows.itertuples():
            self.assertEqual(DS.score_comprehension(r.comprehension_q, r.comprehension_a, r.comprehension_a), 1.0)


class Parsing(unittest.TestCase):
    def test_both_datasets_ask_for_the_same_answers(self):
        json_ask = ('\n\nProvide your reasoning first.\nThen output valid JSON on a new line in exactly this '
                    'structure:\n{"probability": <number>}\n\nThe number must be between 0 and 1.')
        probability = CKD[CKD.question_id == "probability"]
        self.assertTrue(probability.prompt.str.endswith(json_ask).all())
        self.assertTrue(BESPOKE.prompt.str.endswith(json_ask).all())
        choice = CKD[CKD.question_id != "probability"].prompt
        self.assertTrue(choice.str.endswith("\n\n" + DS.CHOICE_ASK).all())
        self.assertFalse(choice.str.contains(DS.ARCHIVED_CHOICE_ASK, regex=False).any())

    def test_letters(self):
        opts = {"A": "Accept the bet", "B": "Decline the bet"}
        for text, letter in (("A", "A"), (" B.", "B"), ("**A**", "A"), ("A. Accept the bet", "A"),
                             ("Decline the bet", "B"), ("(B)", "B")):
            self.assertEqual(DS.parse_letter(text, opts), letter, text)
        for text in ("C", "A. Decline the bet", "Either", "A or B"):
            self.assertIsNone(DS.parse_letter(text, opts), text)

    def test_choices_are_read_from_the_final_choice_line(self):
        for text, letter in (("Reasoning.\nChoice: A", "A"), ("Reasoning.\n\n**Choice:** B\n", "B"),
                             ("Reasoning.\nChoice: **A**", "A"), ("Reasoning.\nchoice: B. Decline the bet", "B"),
                             ("Choice: Decline the bet", "B"), ("A", "A"),
                             ("Reasoning.\n\n**Choice: B**  \nDecline the bet.", "B"),
                             ("Reasoning.\nChoice: A\n**Accept the bet**", "A"),
                             ("Reasoning.\n\n### **Choice:**\nB. Decline the bet", "B"),
                             ("Reasoning.\n\n**Choice: A**  \n**Answer: Choice: A**", "A")):
            self.assertEqual(DS.parse("ckd", "bet_4/normal", "A. Accept the bet\nB. Decline the bet", text)[0],
                             1.0 if letter == "A" else 0.0, text)
        for text in ("Reasoning.\nChoice: A\nOn reflection, maybe B.", "I would pick A.", "Choice: A or B",
                     "Reasoning.\n### Choice:", "My choice: A, but the final answer is unclear.\nMore reasoning",
                     "Reasoning that runs out of tokens before"):
            self.assertIsNone(DS.parse("ckd", "bet_4/normal", "A. Accept the bet\nB. Decline the bet", text)[0], text)

    def test_followups_ask_for_the_statement_format(self):
        questions = BESPOKE.comprehension_q[BESPOKE.comprehension_q != ""]
        self.assertTrue(questions.str.contains(' in the form "(Draw|Witness|Test) ', regex=True).all())
        self.assertEqual(DS.followup_question("What was Ben told about witness 1?"),
                         'What was Ben told about witness 1? Answer on one line in the form "Witness 1: says "<name>"."')
        self.assertEqual(DS.followup_question("Which draws did Alice see?"),
                         'Which draws did Alice see? Write one line per draw in the form "Draw <number>: <result>."')

    def test_followup_statements_ignore_markdown_and_json_escapes(self):
        q, a = "Which draws did Alice see? List them with their results.", "Draw 2: red. Draw 3: red. Draw 4: blue."
        bold = "Alice saw:\n\n- **Draw 2**: red\n- **Draw 3**: red\n- **Draw 4**: blue\n\nSo **Draw 2 (red)** and more."
        self.assertEqual(DS.score_comprehension(q, a, bold), 1.0)
        self.assertEqual(DS.statements('["Witness 3: says \\"Sam\\""]'), {("witness", 3): {"sam"}})

    def test_ckd_values_are_meanings_not_positions(self):
        case_id = DS.case_ids("ckd")[0]
        for order, letter in (("normal", "A"), ("flip", "B")):
            prompt = ckd_prompt(case_id, "A", f"bet_4/{order}")
            self.assertEqual(DS.parse("ckd", f"bet_4/{order}", prompt, letter)[0], 1.0)  # accept
            prompt = ckd_prompt(case_id, "A", f"likert/{order}")
            self.assertEqual(DS.parse("ckd", f"likert/{order}", prompt, "A")[0], 1.0 if order == "normal" else 5.0)

    def test_probabilities_are_read_from_the_json_only(self):
        for text, value in (('reasoning\n{"probability": 0.42}', 0.42), ('```json\n{"probability": 0.0}\n```', 0.0),
                            ('{"probability": 0.9} then {"probability": 0.3}', 0.3), ('"probability": "65%"', None)):
            self.assertEqual(DS.parse_probability(text)[0], value, text)
        for text in ("We need the probability that the marbles were drawn from Bag A, after Draw 2.\nFinal: 0.9",
                     "The probability for a 46-year-old is low.", '{"answer": 0.4}', "", None):
            self.assertIsNone(DS.parse_probability(text)[0], text)
        for invalid in ('{"probability": 1.2}', '{"probability": true}', '{"probability": NaN}',
                        '{"probability": 0.4} then {"probability":', '{"probability": 0.2, "x": 1}'):
            self.assertEqual(DS.parse_probability(invalid), (None, "invalid"))
        self.assertEqual(DS.fallback_probability('"probability": "65%"'), .65)
        case_id = DS.case_ids("ckd")[0]
        prompt = ckd_prompt(case_id, "C")
        self.assertEqual(DS.parse("ckd", "probability/normal", prompt, 'Age matters.\n{"probability": 0.12}')[0], 0.12)
        self.assertEqual(DS.parse("bespoke", "probability", "", '{"probability": 0.8}')[0], 0.8)

    def test_free_text_comprehension(self):
        q, a = "Which draws did Alice see? List them with their results.", "Draw 2: red. Draw 3: red. Draw 4: blue."
        self.assertEqual(DS.score_comprehension(q, a, "Alice saw draw 2: red, draw 3: red and draw 4: blue."), 1.0)
        self.assertEqual(DS.score_comprehension(q, a, "Draw 2: red. Draw 3: red."), 0.0)
        self.assertEqual(DS.score_comprehension(q, a, "Draw 1: red. Draw 2: red. Draw 3: red. Draw 4: blue."), 0.0)
        self.assertIsNone(DS.score_comprehension(q, a, "I don't know."))
        q, a = "What was Alice told about draw 1?", "Draw 1: blue."
        self.assertEqual(DS.score_comprehension(q, a, "She was told Draw 1: blue."), 1.0)
        self.assertEqual(DS.score_comprehension(q, a, "Draw 1: red."), 0.0)
        w = 'Witness 3: says "Kim".'
        self.assertEqual(DS.score_comprehension("What was Sofia told about witness 3?", w, 'Witness 3: says "Kim".'), 1.0)
        self.assertEqual(DS.statements("Draw 1: red. Bag C draw 1: blue."), {("draw", 1): {"red"}})


if __name__ == "__main__":
    unittest.main()
