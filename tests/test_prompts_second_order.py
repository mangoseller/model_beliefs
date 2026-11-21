"""Prompt-construction and parser tests for the second-order conditions (plan, Phase 2)."""

from __future__ import annotations

import difflib
import json
import re
import unittest

from latent_beliefs.ckd.cases import PROFILE_FEATURES
from latent_beliefs.ckd.parse import parse_response
from latent_beliefs.ckd.prompts import FEATURE_KEYS, patient_profile
from second_order import config as C
from second_order import prompts_second_order as P
from second_order.conditions import (
    CONDITIONS, NOT_RECORDED, copy_profile, field_label, flipped_value, pair_conditions,
    profile_line, render_profile, withheld_fields,
)

CASES = list(P.cases().index[:25])
SETS = list(C.load()["withheld_sets"])
FLIP = C.load()["flip_field"]


def words(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]|\n", text)


class FirstOrderIdentity(unittest.TestCase):
    def test_condition_a_matches_released_prompts(self):
        tasks = json.loads((C.RELEASE_PROMPTS / "tasks.json").read_text())
        for task in tasks:
            with (C.RELEASE_PROMPTS / task["path"]).open() as handle:
                for line in handle:
                    row = json.loads(line)
                    built = P.build_prompt(row["source_prompt_id"], "A", task["output"],
                                           self_layout="first_order")
                    self.assertEqual(built, row["prompt"], row["prompt_id"])

    def test_full_render_equals_patient_profile(self):
        for case_id in P.cases().index:
            case = P.cases().loc[case_id]
            self.assertEqual(render_profile(case), patient_profile(case))


class ConditionB(unittest.TestCase):
    def test_removed_rendering_has_no_withheld_content(self):
        for name in SETS:
            fields = withheld_fields(name)
            for case_id in CASES:
                case = P.cases().loc[case_id]
                for output in P.OUTPUTS:
                    prompt = P.build_prompt(case_id, "B", output, withheld=fields, missing="removed",
                                            self_layout="first_order")
                    for f in fields:
                        self.assertNotIn(field_label(f).split(" (")[0], prompt)
                        self.assertNotIn(f"({FEATURE_KEYS[f]})", prompt)
                    kept = [f for f in PROFILE_FEATURES if f not in fields]
                    block = prompt.split(P.PROFILE_HEADER)[1].rstrip("\n").split("\n")
                    self.assertEqual(len(block), len(kept))  # one line per kept field
                    self.assertIn(render_profile(case, fields), prompt)

    def test_not_recorded_rendering_keeps_lines_and_hides_values(self):
        for name in SETS:
            fields = withheld_fields(name)
            for case_id in CASES:
                case = P.cases().loc[case_id]
                prompt = P.build_prompt(case_id, "B", "probability", withheld=fields, missing="not_recorded",
                                        self_layout="first_order")
                block = prompt.split(P.PROFILE_HEADER)[1].rstrip("\n").split("\n")
                self.assertEqual(len(block), len(PROFILE_FEATURES))
                for f in fields:
                    self.assertIn(f"{field_label(f)} is {NOT_RECORDED}.", block)
                    self.assertNotIn(profile_line(case, f), block)  # the true line is gone

    def test_b_is_a_with_the_agents_copy(self):
        for cond in ("B", "B_S1", "B_H", "B_F"):
            for case_id in CASES:
                case = P.cases().loc[case_id]
                full, copy = render_profile(case), copy_profile(case, CONDITIONS[cond].pair)
                for output in P.OUTPUTS:
                    self.assertEqual(P.build_prompt(case_id, cond, output, self_layout="first_order"),
                                     P.first_order_prompt(output, copy))
                    a = P.build_prompt(case_id, "A", output, self_layout="matched")
                    self.assertEqual(P.build_prompt(case_id, cond, output, self_layout="matched"),
                                     a.replace(P.PROFILE_HEADER + full, P.PROFILE_HEADER + copy))


class MatchedSelfLayout(unittest.TestCase):
    """Own-belief prompts end with the same suffix as the prompts about the agent."""

    def test_every_condition_ends_with_the_same_format_block(self):
        for case_id in CASES[:5]:
            full = render_profile(P.cases().loc[case_id])
            for output in P.OUTPUTS:
                question, fmt = P.split_first_order(output, full)
                for cond in CONDITIONS:
                    prompt = P.build_prompt(case_id, cond, output, self_layout="matched")
                    self.assertTrue(prompt.endswith("\n\n" + fmt + "\n"), (cond, output))
                    self.assertTrue(prompt.startswith(P.PROFILE_HEADER), (cond, output))
                a = P.build_prompt(case_id, "A", output, self_layout="matched")
                self.assertEqual(a, f"{P.PROFILE_HEADER}{full}\n\n{question}\n\n{fmt}\n")

    def test_a_is_d_without_the_agent_paragraph_and_with_the_own_question(self):
        for case_id in CASES[:5]:
            case = P.cases().loc[case_id]
            full = render_profile(case)
            for output in P.OUTPUTS:
                question, _ = P.split_first_order(output, full)
                d = P.build_prompt(case_id, "D_john", output)
                a = P.build_prompt(case_id, "A", output, self_layout="matched")
                para = P.agent_paragraph(case, "D_john")
                self.assertEqual(a, d.replace(para + "\n\n", "").replace(
                    P.agent_question(output, "john", full), question))

    def test_repo_layout_matched_is_first_order(self):
        for case_id in CASES[:5]:
            for output in P.OUTPUTS:
                self.assertEqual(P.build_prompt(case_id, "B", output, layout="repo", self_layout="matched"),
                                 P.build_prompt(case_id, "B", output, self_layout="first_order"))

    def test_prompt_for_resolves_the_layout_per_model(self):
        case_id, output = CASES[0], "probability"
        self.assertEqual(P.prompt_for("gemma3_4b", case_id, "A", output),
                         P.build_prompt(case_id, "A", output, self_layout="first_order"))
        self.assertEqual(P.prompt_for("qwen3_8b", case_id, "A", output),
                         P.build_prompt(case_id, "A", output, self_layout="matched"))


class FalseBelief(unittest.TestCase):
    def test_b_f_shows_the_flipped_line_and_not_the_true_one(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            true, flipped = profile_line(case, FLIP), profile_line(case, FLIP, flipped_value(case, FLIP))
            self.assertNotEqual(true, flipped)
            block = P.build_prompt(case_id, "B_F", "probability", self_layout="first_order").split(
                P.PROFILE_HEADER)[1].split("\n")
            self.assertIn(flipped, block)
            self.assertNotIn(true, block)
            self.assertEqual(len([l for l in block if l]), len(PROFILE_FEATURES))

    def test_c_f_has_the_true_profile_and_one_flipped_line_in_the_agent_paragraph(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            full = render_profile(case)
            flipped = profile_line(case, FLIP, flipped_value(case, FLIP))
            for output in P.OUTPUTS:
                prompt = P.build_prompt(case_id, "C_F", output)
                self.assertIn(P.PROFILE_HEADER + full + "\n", prompt)
                self.assertEqual(prompt.count(flipped), 1)
                self.assertIn("The colleague told him:\n" + flipped + "\nHe believes what he was told", prompt)
                old = P.build_prompt(case_id, "C_F", output, false_framing="copy")
                self.assertIn("copy said:\n" + flipped + "\nHe trusts his copy", old)

    def test_false_belief_conditions_share_the_reported_framing(self):
        """M_F, C_F and D_F all say the profile was read to John; M_F and D_F share one sentence."""
        for case_id in CASES:
            case = P.cases().loc[case_id]
            for cond in ("C_F", "M_F", "D_F"):
                self.assertIn("a colleague read it to him", P.agent_paragraph(case, cond))
            self.assertEqual(P.agent_paragraph(case, "M_F"), P.agent_paragraph(case, "D_F"))
            self.assertNotEqual(P.agent_paragraph(case, "D_F"), P.agent_paragraph(case, "D_john"))
            self.assertEqual(pair_conditions("john", "F")["D"], "D_F")
            self.assertEqual(pair_conditions("john", "F", shared_d=True)["D"], "D_john")


class MatchedControl(unittest.TestCase):
    def test_m_is_d_with_the_agents_copy_as_the_profile(self):
        """M differs from D only in the profile block, which is byte-identical to B's."""
        for layout in ("plan", "repo"):
            for pair in ("S4", "H", "F"):
                k = pair_conditions("john", pair)
                for case_id in CASES:
                    case = P.cases().loc[case_id]
                    full, copy = render_profile(case), copy_profile(case, pair)
                    for output in P.OUTPUTS:
                        m = P.build_prompt(case_id, k["M"], output, layout=layout)
                        d = P.build_prompt(case_id, k["D"], output, layout=layout)
                        self.assertEqual(m, d.replace(P.PROFILE_HEADER + full, P.PROFILE_HEADER + copy))
                        self.assertIn(P.PROFILE_HEADER + copy + "\n",
                                      P.build_prompt(case_id, k["B"], output))

    def test_m_contains_nothing_private(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            for f in withheld_fields():
                self.assertNotIn(profile_line(case, f), P.build_prompt(case_id, "M_S4", "probability"))
            self.assertNotIn(profile_line(case, FLIP), P.build_prompt(case_id, "M_F", "probability"))


class ConditionsCD(unittest.TestCase):
    def listed_fields(self, prompt: str) -> list[str]:
        block = prompt.split("no other information about them:\n")[1].split("\nEverything else")[0]
        return [line[2:] for line in block.split("\n")]

    def test_c_has_full_profile_and_exact_withheld_list(self):
        for layout in ("plan", "repo"):
            for name in SETS:
                fields = withheld_fields(name)
                for case_id in CASES:
                    full = render_profile(P.cases().loc[case_id])
                    for cond in ("C_john", "C_user"):
                        for output in P.OUTPUTS:
                            for missing in ("removed", "not_recorded"):
                                prompt = P.build_prompt(case_id, cond, output, layout=layout,
                                                        withheld=fields, missing=missing)
                                self.assertIn(P.PROFILE_HEADER + full + "\n", prompt)
                                self.assertEqual(self.listed_fields(prompt),
                                                 [field_label(f) for f in fields])

    def test_c_and_d_differ_only_in_copy_description(self):
        for layout in ("plan", "repo"):
            for framing in ("john", "user"):
                intro = P.AGENT_INTRO.format(**P.PERSON[framing])
                for case_id in CASES:
                    for output in P.OUTPUTS:
                        c = P.build_prompt(case_id, f"C_{framing}", output, layout=layout)
                        d = P.build_prompt(case_id, f"D_{framing}", output, layout=layout)
                        i = 0
                        while c[i] == d[i]:
                            i += 1
                        j = 0
                        while c[-1 - j] == d[-1 - j]:
                            j += 1
                        prefix, suffix = c[:i], c[len(c) - j:]
                        self.assertTrue(prefix.endswith(intro + " " + P.PERSON[framing]["Subj"]
                                                        + " reviewed "), prefix[-80:])
                        self.assertTrue(c[i:len(c) - j].startswith("an earlier copy"))
                        self.assertTrue(d[i:len(d) - j].startswith("exactly the profile"))
                        # what follows the differing span is identical and contains the question
                        self.assertIn(P.agent_question(output, framing,
                                                       render_profile(P.cases().loc[case_id])),
                                      c[:i] + suffix)

    def test_john_and_user_differ_only_in_person_words(self):
        for layout in ("plan", "repo"):
            for cond in ("C", "D"):
                for case_id in CASES[:5]:
                    for output in P.OUTPUTS:
                        a = words(P.build_prompt(case_id, f"{cond}_john", output, layout=layout))
                        b = words(P.build_prompt(case_id, f"{cond}_user", output, layout=layout))
                        matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
                        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                            if tag == "equal":
                                continue
                            changed = set(a[i1:i2]) | set(b[j1:j2])
                            self.assertTrue(changed <= P.PERSON_WORDS,
                                            f"{output}: {a[i1:i2]} -> {b[j1:j2]}")

    def test_answer_format_is_verbatim_first_order(self):
        for case_id in CASES[:5]:
            full = render_profile(P.cases().loc[case_id])
            for output in P.OUTPUTS:
                _, fmt = P.split_first_order(output, full)
                for cond in [c for c in CONDITIONS if CONDITIONS[c].about == "agent"]:
                    self.assertIn(fmt, P.build_prompt(case_id, cond, output))

    def test_no_hints(self):
        banned = ("ignore", "differ", "may not", "might not", "unknown", "lack", "missing")
        for case_id in CASES[:5]:
            full = render_profile(P.cases().loc[case_id])
            for cond in ("C_john", "D_john", "C_user", "D_user", "C_S1", "C_H", "M_S4", "M_H", "M_F", "D_F"):
                for output in P.OUTPUTS:
                    # Profile values (e.g. "Missing") are data, not instructions.
                    shown = P.model_profile(P.cases().loc[case_id], cond)
                    prompt = P.build_prompt(case_id, cond, output).replace(shown, "").lower()
                    for word in banned:
                        self.assertNotIn(word, prompt, (cond, output))


class Comprehension(unittest.TestCase):
    def test_questions_have_one_correct_option(self):
        for case_id in CASES:
            case = P.cases().loc[case_id]
            q = P.comprehension(case_id, "C_john")
            self.assertEqual(q.options[q.answer], "; ".join(field_label(f) for f in withheld_fields()))
            self.assertEqual(len(set(q.options)), len(q.options))
            q = P.comprehension(case_id, "C_F")
            self.assertEqual(q.options[q.answer], field_label(FLIP))
            self.assertIn("colleague read to him", q.question)
            self.assertEqual(q, P.comprehension(case_id, "C_F"))  # deterministic order
        with self.assertRaises(ValueError):
            P.comprehension(CASES[0], "D_john")

    def test_answers_parse_by_letter_and_text(self):
        from second_order.comprehension import parse_choice

        q = P.comprehension(CASES[0], "C_john")
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
            prompt = P.build_prompt(CASES[0], "C_john", output)
            options = re.findall(r"^([A-E])\. (.+)$", prompt, flags=re.M)
            for letter, text in options:
                by_letter, _ = parse_response(output, f"x\nChoice: {letter}")
                by_text, _ = parse_response(output, f"x\nChoice: {text}")
                self.assertIsNotNone(by_letter, (output, letter))
                self.assertEqual(by_letter, by_text, (output, text))


if __name__ == "__main__":
    unittest.main()
