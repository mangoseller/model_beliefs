"""Prompt-construction and parser tests for the CKD conditions."""

from __future__ import annotations

import json
import re
import unittest

from latent_beliefs.ckd.cases import PROFILE_FEATURES
from latent_beliefs.ckd.parse import parse_response
from latent_beliefs.ckd.prompts import patient_profile
from second_order import config as C
from second_order import prompts_second_order as P
from second_order.conditions import (
    CONDITIONS, NOT_RECORDED, SETTINGS, copy_profile, field_label, flip_field, flipped_value, profile_line,
    render_profile, setting_conditions, withheld_fields,
)

CASES = list(P.cases().index[:25])
FLIP = flip_field()


class FirstOrderIdentity(unittest.TestCase):
    def test_first_order_builder_matches_released_prompts(self):
        """The answer-format text every condition reuses is cut from the paper's exact prompts."""
        tasks = json.loads((C.RELEASE_PROMPTS / "tasks.json").read_text())
        for task in tasks:
            with (C.RELEASE_PROMPTS / task["path"]).open() as handle:
                for line in handle:
                    row = json.loads(line)
                    case = P.cases().loc[row["source_prompt_id"]]
                    self.assertEqual(P.first_order_prompt(task["output"], patient_profile(case)), row["prompt"],
                                     row["prompt_id"])

    def test_full_render_equals_patient_profile(self):
        for case_id in P.cases().index:
            case = P.cases().loc[case_id]
            self.assertEqual(render_profile(case), patient_profile(case))


class OwnBelief(unittest.TestCase):
    def test_a_is_the_first_order_prompt_with_the_profile_first(self):
        for case_id in CASES[:5]:
            full = render_profile(P.cases().loc[case_id])
            for output in P.OUTPUTS:
                question, fmt = P.split_first_order(output, full)
                self.assertEqual(P.build_prompt(case_id, "A", output),
                                 f"{P.PROFILE_HEADER}{full}\n\n{question}\n\n{fmt}\n")

    def test_b_is_a_with_persons_copy(self):
        for setting in SETTINGS:
            b = setting_conditions(setting)["B"]
            for case_id in CASES:
                case = P.cases().loc[case_id]
                full, copy = render_profile(case), copy_profile(case, setting)
                for output in P.OUTPUTS:
                    a = P.build_prompt(case_id, "A", output)
                    self.assertEqual(P.build_prompt(case_id, b, output),
                                     a.replace(P.PROFILE_HEADER + full, P.PROFILE_HEADER + copy))

    def test_withheld_fields_keep_their_lines_as_not_recorded(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            block = P.build_prompt(case_id, "B", "probability").split(P.PROFILE_HEADER)[1].split("\n\n")[0]
            lines = block.split("\n")
            self.assertEqual(len(lines), len(PROFILE_FEATURES))
            for f in withheld_fields():
                self.assertIn(f"{field_label(f)} is {NOT_RECORDED}.", lines)
                self.assertNotIn(profile_line(case, f), lines)  # the true line is gone

    def test_b_f_shows_the_flipped_line_and_not_the_true_one(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            true, flipped = profile_line(case, FLIP), profile_line(case, FLIP, flipped_value(case, FLIP))
            self.assertNotEqual(true, flipped)
            block = P.build_prompt(case_id, "B_F", "probability").split(P.PROFILE_HEADER)[1].split("\n\n")[0]
            lines = block.split("\n")
            self.assertIn(flipped, lines)
            self.assertNotIn(true, lines)
            self.assertEqual(len(lines), len(PROFILE_FEATURES))


class SharedSuffix(unittest.TestCase):
    """Every condition is the profile, then the question and the same first-order answer format."""

    def test_every_condition_starts_with_the_profile_and_ends_with_the_format_block(self):
        for case_id in CASES[:5]:
            full = render_profile(P.cases().loc[case_id])
            for output in P.OUTPUTS:
                _, fmt = P.split_first_order(output, full)
                for cond in CONDITIONS:
                    prompt = P.build_prompt(case_id, cond, output)
                    self.assertTrue(prompt.endswith("\n\n" + fmt + "\n"), (cond, output))
                    self.assertTrue(prompt.startswith(P.PROFILE_HEADER), (cond, output))

    def test_a_is_d_without_the_paragraph_and_with_the_own_question(self):
        for case_id in CASES[:5]:
            case = P.cases().loc[case_id]
            full = render_profile(case)
            for output in P.OUTPUTS:
                question, _ = P.split_first_order(output, full)
                d = P.build_prompt(case_id, "D", output)
                a = P.build_prompt(case_id, "A", output)
                self.assertEqual(a, d.replace(P.agent_paragraph(case, "D") + "\n\n", "").replace(
                    P.agent_question(output, full), question))


class MatchedControl(unittest.TestCase):
    def test_m_is_d_with_persons_copy_as_the_profile(self):
        """M and D share one template and differ only in the profile, which is byte-identical to B's."""
        for setting in SETTINGS:
            k = setting_conditions(setting)
            for case_id in CASES:
                case = P.cases().loc[case_id]
                full, copy = render_profile(case), copy_profile(case, setting)
                for output in P.OUTPUTS:
                    m = P.build_prompt(case_id, k["M"], output)
                    d = P.build_prompt(case_id, k["D"], output)
                    self.assertEqual(m, d.replace(P.PROFILE_HEADER + full, P.PROFILE_HEADER + copy))
                    self.assertIn(P.PROFILE_HEADER + copy + "\n", P.build_prompt(case_id, k["B"], output))

    def test_m_contains_nothing_private(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            for f in withheld_fields():
                self.assertNotIn(profile_line(case, f), P.build_prompt(case_id, "M", "probability"))
            self.assertNotIn(profile_line(case, FLIP), P.build_prompt(case_id, "M_F", "probability"))


class FalseSetting(unittest.TestCase):
    def test_c_f_has_the_true_profile_and_the_misread_line_in_the_paragraph(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            full = render_profile(case)
            flipped = profile_line(case, FLIP, flipped_value(case, FLIP))
            for output in P.OUTPUTS:
                prompt = P.build_prompt(case_id, "C_F", output)
                self.assertIn(P.PROFILE_HEADER + full + "\n", prompt)
                self.assertEqual(prompt.count(flipped), 1)
                self.assertIn("The colleague told him:\n" + flipped + "\nHe believes what he was told", prompt)

    def test_m_c_and_d_all_frame_persons_evidence_as_reported(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            for cond in ("C_F", "M_F", "D_F"):
                self.assertIn("a colleague read it to him", P.agent_paragraph(case, cond))
            self.assertEqual(P.agent_paragraph(case, "M_F"), P.agent_paragraph(case, "D_F"))
            self.assertNotEqual(P.agent_paragraph(case, "D_F"), P.agent_paragraph(case, "D"))


class PartialSetting(unittest.TestCase):
    def test_c_has_the_full_profile_and_the_exact_withheld_list(self):
        for case_id in CASES:
            full = render_profile(P.cases().loc[case_id])
            for output in P.OUTPUTS:
                prompt = P.build_prompt(case_id, "C", output)
                self.assertIn(P.PROFILE_HEADER + full + "\n", prompt)
                block = prompt.split("no other information about them:\n")[1].split("\nEverything else")[0]
                self.assertEqual([line[2:] for line in block.split("\n")],
                                 [field_label(f) for f in withheld_fields()])

    def test_c_and_d_differ_only_in_what_person_reviewed(self):
        for case_id in CASES:
            for output in P.OUTPUTS:
                c, d = P.build_prompt(case_id, "C", output), P.build_prompt(case_id, "D", output)
                i = next(n for n, (x, y) in enumerate(zip(c, d)) if x != y)
                j = next(n for n, (x, y) in enumerate(zip(reversed(c), reversed(d))) if x != y)
                self.assertTrue(c[:i].endswith(P.AGENT_INTRO.format(**P.PERSON) + " He reviewed "), c[:i][-80:])
                self.assertTrue(c[i:len(c) - j].startswith("an earlier copy"))
                self.assertTrue(d[i:len(d) - j].startswith("exactly the profile"))


class NoHints(unittest.TestCase):
    def test_prompts_about_person_give_no_hints(self):
        banned = ("ignore", "differ", "may not", "might not", "unknown", "lack", "missing")
        for case_id in CASES[:5]:
            for cond in [c for c in CONDITIONS if CONDITIONS[c].about == "person"]:
                for output in P.OUTPUTS:
                    # Profile values (e.g. "Missing") are data, not instructions.
                    shown = P.model_profile(P.cases().loc[case_id], cond)
                    prompt = P.build_prompt(case_id, cond, output).replace(shown, "").lower()
                    for word in banned:
                        self.assertNotIn(word, prompt, (cond, output))


class Comprehension(unittest.TestCase):
    def test_questions_have_one_correct_option(self):
        for case_id in CASES:
            q = P.comprehension(case_id, "C")
            self.assertEqual(q.options[q.answer], "; ".join(field_label(f) for f in withheld_fields()))
            self.assertEqual(len(set(q.options)), len(q.options))
            q = P.comprehension(case_id, "C_F")
            self.assertEqual(q.options[q.answer], field_label(FLIP))
            self.assertIn("colleague read to him", q.question)
            self.assertEqual(q, P.comprehension(case_id, "C_F"))  # deterministic order
        with self.assertRaises(ValueError):
            P.comprehension(CASES[0], "D")

    def test_answers_parse_by_letter_and_text(self):
        from second_order.comprehension import parse_choice

        q = P.comprehension(CASES[0], "C")
        for i, option in enumerate(q.options):
            letter = chr(65 + i)
            for text in (f"x\nChoice: {letter}", f"x\nChoice: {option}", f"**Choice:** {letter}. {option}",
                         f"Choice: A\nthinking again\nChoice: ({letter})"):
                self.assertEqual(parse_choice(text, q.options), i, text)
        self.assertIsNone(parse_choice("no choice line", q.options))
        self.assertIsNone(parse_choice("Choice: Z", q.options))
        wrong = (q.answer + 1) % len(q.options)
        self.assertIsNone(parse_choice(f"Choice: {chr(65 + q.answer)}. {q.options[wrong]}", q.options))


HAND_WRITTEN = {
    "probability": 'John would weigh his diabetes history...\n{"probability": 0.18}',
    "likert": "He would see moderate risk.\nChoice: Somewhat likely",
    "probability_bin_quintile": "I would put it in the upper bins.\nChoice: 13.02% to less than 29.72%",
    "decision_no_threshold": "He would screen.\nChoice: Recommend screening for CKD",
    "threshold": "I would not reach the threshold.\nChoice: Do not screen this patient for CKD",
    "bet": "He would accept.\nChoice: Accept the bet",
    "cost": "I would not screen.\nChoice: Recommend not screening for CKD",
}
EXPECTED = {"probability": 0.18, "likert": 4.0, "probability_bin_quintile": 4.0,
            "decision_no_threshold": 1.0, "threshold": 0.0, "bet": 1.0, "cost": 0.0}


class Parsing(unittest.TestCase):
    def test_hand_written_answers_parse(self):
        for output in P.OUTPUTS:
            kind = P.output_kind(output)
            value, method = parse_response(output, HAND_WRITTEN[kind])
            self.assertEqual(value, EXPECTED[kind], output)
            self.assertEqual(method, "strict")

    def test_options_in_cd_prompts_parse_by_letter_and_text(self):
        for output in P.OUTPUTS:
            if output == "probability":
                continue
            prompt = P.build_prompt(CASES[0], "C", output)
            options = re.findall(r"^([A-E])\. (.+)$", prompt, flags=re.M)
            for letter, text in options:
                by_letter, _ = parse_response(output, f"x\nChoice: {letter}")
                by_text, _ = parse_response(output, f"x\nChoice: {text}")
                self.assertIsNotNone(by_letter, (output, letter))
                self.assertEqual(by_letter, by_text, (output, text))


if __name__ == "__main__":
    unittest.main()
