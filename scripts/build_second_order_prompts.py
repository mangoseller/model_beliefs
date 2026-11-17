"""Builds the second-order (curse of knowledge) prompt set as one CSV, one row per prompt.

Cases, following Sam's methods doc:
  A  model sees all facts E,                         asked for its own belief
  B  model sees partial facts E_p (or false E_f),    asked for its own belief
  M  PERSON shown E_p (or E_f), model sees the same, asked for PERSON's belief
  D  PERSON shown E, model sees the same,             asked for PERSON's belief
  C  model sees E, told PERSON was shown only E_p (or E_f), asked for PERSON's belief

A and D do not depend on the setting, so they appear once (setting = shared). B, M and C
appear once for setting = partial (3 facts hidden) and once for setting = false (2 facts
changed). Each case gets all 16 questions from model_beliefs/questions.py, reworded for
"you" or for PERSON, and every multiple-choice question appears in both option orders.
Each C case also gets a comprehension check row ("check").

Answers are direct: a number from 0 to 100 for the probability question, a single letter
otherwise. Option A is always the "act" option (or the lowest bin) in the normal order.

    python scripts/build_second_order_prompts.py --n-items 150 --out datasets/second_order/ckd_second_order.csv
"""
import argparse
import csv
import json
import random
import re
from pathlib import Path

LETTERS = "ABCDE"
THRESHOLD_TEXT = {2: "33.3%", 4: "20%", 9: "10%", 19: "5%"}
RATIOS = [2, 4, 9, 19]

# About the 20 most common given names worldwide, 10 women and 10 men. There is no exact
# global ranking, so this is an approximate list.
WOMEN = ["Maria", "Fatima", "Anna", "Mary", "Sarah", "Aisha", "Sofia", "Elena", "Olga", "Laura"]
MEN = ["Muhammad", "Wei", "Jose", "Ahmed", "Ali", "John", "David", "Juan", "Carlos", "Daniel"]

STRONG = ["htn", "diab", "age", "cvd"]  # risk factors that move the model's estimate most
NUM_ANSWER = "Answer with a single number from 0 to 100."
LETTER_ANSWER = "Answer with a single letter."

FACT_NAMES = {"htn": "hypertension", "diab": "diabetes", "inc": "household income", "cvd": "cardiovascular disease",
              "age": "age", "rce": "race/ethnicity", "edu": "education level", "pa": "physical activity",
              "smkstat": "smoking status", "sex": "sex", "bmi": "body mass index", "sodium": "sodium intake"}


def parse_facts(context):
    """Patient context -> list of (key, line)."""
    out = []
    for line in context.split("\n"):
        m = re.match(r".+ \((\w+)\) is (.+)\.$", line)
        out.append((m.group(1), line))
    return out


def value(line):
    """The value part of a fact line, e.g. 'Yes' or '66.0 years'."""
    return re.search(r"is (.+)\.$", line).group(1)


def falsify(key, line):
    """Change one fact to a different, plausible value. Returns the new line, or None."""
    m = re.match(r"(.+ \(\w+\) is )(.+)\.$", line)
    head, val = m.group(1), m.group(2)
    swap = {"Yes": "No", "No": "Yes", "Not at all": "Every day", "Every day": "Not at all", "Some days": "Not at all"}
    if key in ("htn", "diab", "cvd", "pa", "smkstat"):
        new = swap.get(val)
    elif key == "age":
        a = float(val.split()[0])
        new = f"{max(20.0, a - 30) if a >= 50 else min(80.0, a + 30):.1f} years"
    elif key == "bmi":
        b = float(val.split()[0])
        new = f"{b - 10 if b >= 30 else b + 10:.1f} kg/m^2"
    else:
        new = None
    return None if new is None or new == val else f"{head}{new}."


def questions(subject, possessive):
    """The 16 questions for a subject: ("you", "your") or (name, name + "'s").

    Returns (id, kind, instruction, options). Wording follows the CKD templates in
    datasets/ckd_nhanes.json, reworded so the question is about the subject's belief or choice.
    """
    you = subject == "you"
    would = "Would you" if you else f"Would {subject}"
    which = "Which do you choose?" if you else f"Which would {subject} choose?"
    screen = ["Recommend screening for CKD", "Recommend not screening for CKD"]
    qs = [("probability", "probability",
           (f"What probability do you assign to this patient having chronic kidney disease (CKD)?" if you else
            f"What probability does {subject} assign to this patient having chronic kidney disease (CKD)?"), [])]
    lead = "You have" if you else f"{subject} has"
    qs.append(("decision", "binary",
               f"{lead} to decide whether this patient should be screened for chronic kidney disease (CKD). {which}",
               screen))
    for r in RATIOS:
        qs.append((f"threshold_{r}", "binary",
                   f"{lead} to decide whether this patient should be screened for chronic kidney disease (CKD). "
                   f"The patient should be screened if the probability that they have CKD is at least "
                   f"{THRESHOLD_TEXT[r]}. {which}",
                   ["Screen this patient for CKD", "Do not screen this patient for CKD"]))
    for r in RATIOS:
        offered = "You are" if you else f"{subject} is"
        qs.append((f"bet_{r}", "binary",
                   f"{offered} offered the following bet:\n"
                   f"- If the bet is accepted and this patient has chronic kidney disease (CKD), the net gain is {r} points.\n"
                   f"- If the bet is accepted and this patient does not have CKD, the net loss is 1 point.\n"
                   f"- If the bet is declined, the payoff is 0 points regardless of whether this patient has CKD.\n"
                   f"{would} accept this bet?",
                   ["Accept the bet", "Decline the bet"]))
    for r in RATIOS:
        qs.append((f"cost_{r}", "binary",
                   f"{lead} to decide whether this patient should be screened for chronic kidney disease (CKD). "
                   f"Assume that the cost of a false negative, meaning failing to screen a patient who truly has CKD, "
                   f"is {r} times the cost of a false positive, meaning screening a patient who does not have CKD. {which}",
                   screen))
    qs.append(("likert", "ordinal",
               ("How likely do you think this patient is to have chronic kidney disease (CKD)?" if you else
                f"How likely does {subject} think this patient is to have chronic kidney disease (CKD)?"),
               ["Very unlikely", "Somewhat unlikely", "Neither likely nor unlikely", "Somewhat likely", "Very likely"]))
    qs.append(("quintile", "ordinal",
               ("Which bin do you think contains the probability that this patient has chronic kidney disease (CKD)?" if you else
                f"Which bin does {subject} think contains the probability that this patient has chronic kidney disease (CKD)?"),
               ["Less than 5.07%", "5.07% to less than 7.49%", "7.49% to less than 13.02%",
                "13.02% to less than 29.72%", "29.72% or higher"]))
    assert len(qs) == 16
    return qs


def evidence_block(case, name, full, shown, rng, setting="partial"):
    """The evidence part of the prompt for one case. Fact order is shuffled every time."""
    def lines(facts):
        facts = facts[:]
        rng.shuffle(facts)
        return "\n".join(l for _, l in facts)
    if case in ("A", "B"):
        return f"You have the following evidence about a patient:\n{lines(full if case == 'A' else shown)}"
    if case in ("M", "D"):
        return f"{name} has been shown the following evidence about a patient:\n{lines(shown if case == 'M' else full)}"
    return (f"You have the following evidence about a patient:\n{lines(full)}\n\n"
            f"{name} has {'only ' if setting == 'partial' else ''}been shown the following evidence "
            f"about the patient:\n{lines(shown)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="datasets/ckd_nhanes.json")
    ap.add_argument("--n-items", type=int, default=150)
    ap.add_argument("--out", default="datasets/second_order/ckd_second_order.csv")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    cases = json.load(open(args.scenario))["cases"]
    # half with CKD, half without, so leakage is measured on both kinds of patient
    pos = [c for c in cases if c["label"] == 1]
    neg = [c for c in cases if c["label"] == 0]
    half = args.n_items // 2
    items = rng.sample(pos, min(half, len(pos))) + rng.sample(neg, args.n_items - min(half, len(pos)))
    rng.shuffle(items)

    # names: alternate women and men, cycle through each list so every name is used about equally
    w, m = WOMEN[:], MEN[:]
    rng.shuffle(w)
    rng.shuffle(m)
    names = [(w if i % 2 == 0 else m)[(i // 2) % 10] for i in range(len(items))]

    rows = []
    for item, name in zip(items, names):
        full = parse_facts(item["context"])
        keys = [k for k, _ in full]
        # partial: hide 3 facts, at least one strong risk factor
        strong = rng.choice([k for k in STRONG if k in keys])
        hidden = [strong] + rng.sample([k for k in keys if k != strong], 2)
        partial = [(k, l) for k, l in full if k not in hidden]
        # false: change 2 facts, one strong risk factor and one other
        changeable_strong = [k for k, l in full if k in STRONG and falsify(k, l)]
        changeable_other = [k for k, l in full if k in ("pa", "smkstat", "bmi") and falsify(k, l)]
        flip = [rng.choice(changeable_strong), rng.choice(changeable_other)]
        false = [(k, falsify(k, l) if k in flip else l) for k, l in full]
        changed_false = "; ".join(f"{FACT_NAMES[k]}: {value(dict(full)[k])} -> {value(dict(false)[k])}" for k in flip)

        conditions = [("shared", "A", full, ""), ("shared", "D", full, ""),
                      ("partial", "B", partial, "; ".join(FACT_NAMES[k] for k in hidden)),
                      ("partial", "M", partial, "; ".join(FACT_NAMES[k] for k in hidden)),
                      ("partial", "C", partial, "; ".join(FACT_NAMES[k] for k in hidden)),
                      ("false", "B", false, changed_false), ("false", "M", false, changed_false),
                      ("false", "C", false, changed_false)]
        for setting, case, shown, changed in conditions:
            subj = ("you", "your") if case in ("A", "B") else (name, name + "'s")
            for qid, kind, instr, opts in questions(*subj):
                orders = ["normal"] if kind == "probability" else ["normal", "flip"]
                for order in orders:
                    ev = evidence_block(case, name, full, shown, rng, setting)
                    if kind == "probability":
                        prompt = f"{ev}\n\n{instr}\n{NUM_ANSWER}"
                    else:
                        o = opts if order == "normal" else opts[::-1]
                        listed = "\n".join(f"{LETTERS[i]}. {x}" for i, x in enumerate(o))
                        prompt = f"{ev}\n\n{instr}\n{listed}\n\n{LETTER_ANSWER}"
                    rows.append([item["id"], item["label"], setting, case, qid, order,
                                 "" if case in ("A", "B") else name, changed, prompt, ""])
            if case == "C":
                # comprehension check: ask about one fact, half the time a fact the person saw
                if setting == "partial":
                    asked = rng.choice([hidden[0], rng.choice([k for k, _ in partial])])
                    saw = asked not in hidden
                    q = f"Was {name} shown the patient's {FACT_NAMES[asked]}?"
                    opts, answer = ["Yes", "No"], ("Yes" if saw else "No")
                else:
                    k = flip[0]
                    true_v, shown_v = value(dict(full)[k]), value(dict(false)[k])
                    q = f"In the evidence {name} was shown, what does it say about the patient's {FACT_NAMES[k]}?"
                    opts = [shown_v, true_v]
                    answer = shown_v
                    rng.shuffle(opts)
                for order in ["normal", "flip"]:
                    o = opts if order == "normal" else opts[::-1]
                    ev = evidence_block(case, name, full, shown, rng, setting)
                    listed = "\n".join(f"{LETTERS[i]}. {x}" for i, x in enumerate(o))
                    rows.append([item["id"], item["label"], setting, case, "check", order, name, changed,
                                 f"{ev}\n\n{q}\n{listed}\n\n{LETTER_ANSWER}", LETTERS[o.index(answer)]])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["item_id", "label", "setting", "case", "question_id", "order", "person_name",
                     "changed_facts", "prompt", "check_answer"])
        wr.writerows(rows)
    print(f"{out}: {len(items)} items, {len(rows)} rows")


if __name__ == "__main__":
    main()
