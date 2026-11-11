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
from second_order.conditions import field_label, render_profile, withheld_fields

CASES = list(P.cases().index[:25])
SETS = list(C.load()["withheld_sets"])


def words(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]|\n", text)


class FirstOrderIdentity(unittest.TestCase):
    def test_condition_a_matches_released_prompts(self):
        tasks = json.loads((C.RELEASE_PROMPTS / "tasks.json").read_text())
        for task in tasks:
            with (C.RELEASE_PROMPTS / task["path"]).open() as handle:
                for line in handle:
                    row = json.loads(line)
                    built = P.build_prompt(row["source_prompt_id"], "A", task["output"])
                    self.assertEqual(built, row["prompt"], row["prompt_id"])

    def test_full_render_equals_patient_profile(self):
        for case_id in P.cases().index:
            case = P.cases().loc[case_id]
            self.assertEqual(render_profile(case), patient_profile(case))


class ConditionB(unittest.TestCase):
    def test_no_withheld_content(self):
        for name in SETS:
            fields = withheld_fields(name)
            for case_id in CASES:
                case = P.cases().loc[case_id]
                for output in P.OUTPUTS:
                    prompt = P.build_prompt(case_id, "B", output, withheld=fields)
                    for f in fields:
                        self.assertNotIn(field_label(f).split(" (")[0], prompt)
                        self.assertNotIn(f"({FEATURE_KEYS[f]})", prompt)
                    kept = [f for f in PROFILE_FEATURES if f not in fields]
                    block = prompt.split(P.PROFILE_HEADER)[1].rstrip("\n").split("\n")
                    self.assertEqual(len(block), len(kept))  # one line per kept field
                    self.assertIn(render_profile(case, fields), prompt)

    def test_b_is_a_with_partial_profile(self):
        fields = withheld_fields()
        for case_id in CASES:
            case = P.cases().loc[case_id]
            for output in P.OUTPUTS:
                self.assertEqual(
                    P.build_prompt(case_id, "B", output),
                    P.first_order_prompt(output, render_profile(case, fields)),
                )


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
                            prompt = P.build_prompt(case_id, cond, output, layout=layout,
                                                    withheld=fields)
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
                        self.assertTrue(d[i:len(d) - j].startswith("the same profile"))
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
                for cond in ("C_john", "D_john", "C_user", "D_user", "E_self"):
                    self.assertIn(fmt, P.build_prompt(case_id, cond, output))

    def test_no_hints(self):
        banned = ("ignore", "differ", "may not", "might not", "unknown", "lack", "missing")
        for case_id in CASES[:5]:
            full = render_profile(P.cases().loc[case_id])
            for cond in ("C_john", "D_john", "C_user", "D_user"):
                for output in P.OUTPUTS:
                    # Profile values (e.g. "Missing") are data, not instructions.
                    prompt = P.build_prompt(case_id, cond, output).replace(full, "").lower()
                    for word in banned:
                        self.assertNotIn(word, prompt, (cond, output))


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
