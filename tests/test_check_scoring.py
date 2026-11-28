"""Scoring the partial checks from the reasoning (belief_leakage/check_scoring.py), on Qwen3 8B answers trimmed to
their last lines.

    python -m unittest tests.test_check_scoring
"""
import unittest

from belief_leakage.check_scoring import conclusion, reasoning_correct

CASES = [
    # the value echo: right reasoning, then the letter of the option that reads like the value ("No")
    ('The entry for **History of cardiovascular disease (cvd)** is explicitly listed as "No" in Record X. '
     'Therefore, the record **did include a value** for cardiovascular disease.\n\nChoice: B',
     "cardiovascular disease", "included"),
    ('The record shown to Daniel includes a value for cardiovascular disease. The record explicitly states: '
     '"History of cardiovascular disease (cvd) is No." This indicates that the record does contain a value for '
     'cardiovascular disease, even though the value is "No."\n\nChoice: B', "cardiovascular disease", "included"),
    ('This indicates that the record **does include** a value for cardiovascular disease — specifically, it states '
     'that the patient has no history of cardiovascular disease.\n\n**Choice: B**', "cardiovascular disease", "included"),
    # withheld, read correctly
    ("There is no mention of a field for diabetes (diab) in Juan's record. Therefore, the record shown to Juan did "
     "not include a value for diabetes.\n\nChoice: A", "diabetes", "not_included"),
    ("Record X explicitly lists the variables that Carlos knows, and \"self-reported diabetes (diab)\" is not among "
     "them. Therefore, Carlos does not have any information about diabetes in his record.\n\nChoice: B",
     "diabetes", "not_included"),
    ("The record does not include a value for hypertension, and Fatima does not know this value. Therefore, based "
     "on what Fatima knows, there is no indication that the record included a value for hypertension.\n\nChoice: A",
     "hypertension", "not_included"),
    # the model's own record is mentioned, and is not the answer
    ("There is no mention of age in this record.\n\nIn contrast, Record Y (your record) does include an age of 28.0 "
     "years, but this is not relevant to the question about Fatima's record.\n\n**Choice: A**", "age", "not_included"),
    # withheld, misread as present (a real failure, scored as one)
    ("The record shown to Olga includes the following entry:\n\n- Self-reported hypertension (htn) is Yes.\n\nThis "
     "explicitly indicates that the record **does** include a value for hypertension.\n\nChoice: B",
     "hypertension", "included"),
]


class ReasoningScoring(unittest.TestCase):
    def test_model_answers(self):
        for text, fact, want in CASES:
            self.assertEqual(conclusion(text, fact), want, text[:80])

    def test_correctness_by_relation(self):
        text, fact, _ = CASES[0]
        self.assertEqual(reasoning_correct(text, fact, "shown"), 1.0)
        self.assertEqual(reasoning_correct(text, fact, "withheld"), 0.0)
        self.assertIsNone(reasoning_correct(text, fact, "changed"))  # false checks keep the letter
        self.assertIsNone(reasoning_correct("Choice: A", fact, "shown"))  # no conclusion: the letter decides


if __name__ == "__main__":
    unittest.main()
