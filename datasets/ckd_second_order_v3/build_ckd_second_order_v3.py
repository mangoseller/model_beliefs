#!/usr/bin/env python3
"""Build the v3 main CKD battery and high-threshold supplement.
Run with the original ckd_second_order.csv and Python 3 with NetworkX installed.
See README.md for the sampling unit, contrasts, and check design.
"""
import argparse
import collections
import csv
import os
import random
import re
import sys
from itertools import combinations

import networkx as nx

# Retain the v2 false-record assignments exactly.
SEED = "ckd_second_order_v2"
AGE_STEP = 20.0
AGE_MIN = 20.0
# NHANES top-codes age at 80 ("80 or older"): never move an age to or from it.
AGE_TOPCODE = 80.0
# A false record must not newly make a patient under this age implausible
# (i.e. having CVD, or two or more of cvd/htn/diab).
PLAUSIBLE_AGE = 30.0
N_FACTS = 12

OWN_HEADER = "You have the following evidence about a patient:"
PERSON_HEADER = "{name} has been shown the following evidence about a patient:"
# Person header of every two-block prompt (C, D2, M2), in both settings.
TWO_BLOCK_PERSON_HEADER = "{name} has only been shown the following evidence about the patient:"

ORIG_COLUMNS = ["item_id", "label", "setting", "case", "question_id", "order",
                "person_name", "changed_facts", "prompt", "check_answer"]
STUDY_PERSON_HEADER = ("{name} has only the following record for this patient "
                       "and accepts its contents as accurate:")
COLUMNS = ORIG_COLUMNS + ["replicate", "direction", "direction_assignment", "block_order",
                          "check_fact", "check_relation"]

# Short names used in changed_facts and in the check questions.
SHORT = {"cvd": "cardiovascular disease", "pa": "physical activity",
         "smkstat": "smoking status", "htn": "hypertension", "diab": "diabetes",
         "bmi": "body mass index", "age": "age", "rce": "race/ethnicity",
         "inc": "household income", "sodium": "sodium intake",
         "edu": "education level", "sex": "sex"}
CODE_OF_SHORT = {v: k for k, v in SHORT.items()}
KEY_FACTS = ("cvd", "htn", "diab", "age")

FACT_RE = re.compile(r"^(.*) \((\w+)\) is (.*)\.$")


def rng(*parts):
    parts = [str(p) for p in parts]
    assert not any("|" in p for p in parts), parts  # keep seeds collision-free
    return random.Random("|".join([SEED] + parts))


def parse_block(block):
    """-> (header, [(code, label, value), ...]) in the order shown."""
    lines = block.split("\n")
    out = []
    for line in lines[1:]:
        m = FACT_RE.match(line)
        if not m:
            raise ValueError("unparseable fact line: %r" % line)
        out.append((m.group(2), m.group(1), m.group(3)))
    return lines[0], out


def render_block(header, facts, labels, order):
    return "\n".join([header] + ["%s (%s) is %s." % (labels[c], c, facts[c]) for c in order])


def name_to_placeholder(text, name):
    return re.sub(r"\b%s\b" % re.escape(name), "{name}", text)


def fmt_age(x):
    return "%.1f years" % x


# --------------------------------------------------------------------------
# Parse the original file
# --------------------------------------------------------------------------
def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != ORIG_COLUMNS:
            raise ValueError("unexpected columns: %r" % header)
        return [dict(zip(header, row)) for row in reader]


def parse_items(rows):
    """Recover, per item: full evidence E, label, name, withheld facts."""
    by_item = collections.OrderedDict()
    for r in rows:
        by_item.setdefault(r["item_id"], []).append(r)

    labels = None
    items = collections.OrderedDict()
    for item_id, rs in by_item.items():
        a_rows = [r for r in rs if r["case"] == "A"]
        _, facts0 = parse_block(a_rows[0]["prompt"].split("\n\n")[0])
        E = {c: v for c, _, v in facts0}
        item_labels = {c: l for c, l, _ in facts0}
        if labels is None:
            labels = item_labels
        assert item_labels == labels and len(E) == N_FACTS, item_id

        names = {r["person_name"] for r in rs if r["person_name"]}
        lab = {r["label"] for r in rs}
        assert len(names) == 1 and len(lab) == 1, item_id

        # Every A / D / C-own block must show exactly E.
        for r in rs:
            blocks = r["prompt"].split("\n\n")
            if r["case"] in ("A", "D") or r["case"] == "C":
                _, fs = parse_block(blocks[0])
                assert {c: v for c, _, v in fs} == E, (item_id, r["case"])

        # Partial evidence: identical across partial B / M / C-person blocks.
        ep_sets = set()
        cf = set()
        for r in rs:
            if r["setting"] != "partial":
                continue
            blocks = r["prompt"].split("\n\n")
            _, fs = parse_block(blocks[1] if r["case"] == "C" else blocks[0])
            ep_sets.add(tuple(sorted((c, v) for c, _, v in fs)))
            cf.add(r["changed_facts"])
        assert len(ep_sets) == 1 and len(cf) == 1, item_id
        Ep = dict(next(iter(ep_sets)))
        assert all(E[c] == v for c, v in Ep.items()), item_id
        withheld = [CODE_OF_SHORT[s] for s in next(iter(cf)).split("; ")]
        assert set(withheld) == set(E) - set(Ep) and len(withheld) == 3, item_id

        items[item_id] = dict(item_id=item_id, label=next(iter(lab)), name=next(iter(names)),
                              E=E, Ep=Ep, partial_changed=next(iter(cf)), rows=rs)
    return items, labels


def extract_templates(items):
    """Question text (everything after the evidence) per (question_id, order).

    Returns first-order, second-order and false-check-stem templates, with the
    person's name replaced by {name}. Asserts they are identical across items.
    """
    first, second = {}, {}
    check_stems = {"partial": set(), "false": set()}
    partial_opts = set()
    q_order = []
    for it in items.values():
        for r in it["rows"]:
            blocks = r["prompt"].split("\n\n")
            key = (r["question_id"], r["order"])
            if r["question_id"] == "check":
                q = blocks[2].split("\n")
                stem = name_to_placeholder(q[0], it["name"])
                code = CODE_OF_SHORT[re.search(r"patient's (.*)\?$", stem).group(1)]
                check_stems[r["setting"]].add(stem.replace(SHORT[code], "{fact}"))
                assert blocks[3] == "Answer with a single letter." and len(blocks) == 4
                if r["setting"] == "partial":
                    partial_opts.add((r["order"], tuple(q[1:])))
                continue
            n_ev = 2 if r["case"] == "C" else 1
            tail = "\n\n".join(blocks[n_ev:])
            if r["case"] in ("A", "B"):
                assert first.setdefault(key, tail) == tail, key
                if r["case"] == "A" and key not in q_order:
                    q_order.append(key)
            else:
                tail = name_to_placeholder(tail, it["name"])
                assert second.setdefault(key, tail) == tail, key
    assert all(len(v) == 1 for v in check_stems.values()), check_stems
    assert partial_opts == {("normal", ("A. Yes", "B. No")), ("flip", ("A. No", "B. Yes"))}, partial_opts
    assert set(first) == set(second) == set(q_order) and len(q_order) == 31
    for t in list(first.values()) + list(second.values()):
        assert "{" not in t.replace("{name}", "") and "}" not in t.replace("{name}", ""), t
    stems = {k: next(iter(v)) for k, v in check_stems.items()}
    return first, second, stems, q_order


def fidelity_check(items, labels, first, second):
    """Rebuild every original non-check prompt from parsed parts; must match."""
    n = 0
    for it in items.values():
        for r in it["rows"]:
            if r["question_id"] == "check":
                continue
            blocks = r["prompt"].split("\n\n")
            key = (r["question_id"], r["order"])
            if r["case"] in ("A", "B"):
                _, fs = parse_block(blocks[0])
                rebuilt = render_block(OWN_HEADER, {c: v for c, _, v in fs}, labels,
                                       [c for c, _, _ in fs]) + "\n\n" + first[key]
            elif r["case"] in ("M", "D"):
                _, fs = parse_block(blocks[0])
                rebuilt = render_block(PERSON_HEADER.format(name=it["name"]),
                                       {c: v for c, _, v in fs}, labels,
                                       [c for c, _, _ in fs]) + "\n\n" + second[key].format(name=it["name"])
            else:
                _, fo = parse_block(blocks[0])
                h, fp = parse_block(blocks[1])
                if r["setting"] == "partial":
                    assert h == TWO_BLOCK_PERSON_HEADER.format(name=it["name"])
                rebuilt = (render_block(OWN_HEADER, {c: v for c, _, v in fo}, labels, [c for c, _, _ in fo])
                           + "\n\n" + render_block(h, {c: v for c, _, v in fp}, labels, [c for c, _, _ in fp])
                           + "\n\n" + second[key].format(name=it["name"]))
            assert rebuilt == r["prompt"], (it["item_id"], r["setting"], r["case"], key)
            n += 1
    return n


# --------------------------------------------------------------------------
# False-belief redesign
# --------------------------------------------------------------------------
def directional_changes(E):
    """-> {'sicker': {code: new_value}, 'healthier': {code: new_value}}."""
    out = {"sicker": {}, "healthier": {}}
    for c in ("cvd", "htn", "diab"):
        if E[c] == "No":
            out["sicker"][c] = "Yes"
        elif E[c] == "Yes":
            out["healthier"][c] = "No"
    age = float(E["age"].split()[0])
    assert E["age"] == fmt_age(age)
    if age < AGE_TOPCODE:  # a top-coded age (80 = "80 or older") is never moved
        if age + AGE_STEP < AGE_TOPCODE:
            out["sicker"]["age"] = fmt_age(age + AGE_STEP)
        if age - AGE_STEP >= AGE_MIN:
            out["healthier"]["age"] = fmt_age(age - AGE_STEP)
    if E["pa"] == "Yes":
        out["sicker"]["pa"] = "No"
    elif E["pa"] == "No":
        out["healthier"]["pa"] = "Yes"
    if E["smkstat"] in ("Not at all", "Some days"):
        out["sicker"]["smkstat"] = "Every day"
    if E["smkstat"] in ("Every day", "Some days"):
        out["healthier"]["smkstat"] = "Not at all"
    return out


def implausible(rec):
    """Young patient with CVD, or with two or more of cvd/htn/diab."""
    age = float(rec["age"].split()[0])
    n_cond = sum(rec[c] == "Yes" for c in ("cvd", "htn", "diab"))
    return age < PLAUSIBLE_AGE and (rec["cvd"] == "Yes" or n_cond >= 2)


def valid_pairs(E, cands):
    """Pairs of changes containing a key fact whose false record is plausible.

    A record that is already "implausible" in the real data is allowed to stay
    so; the false record just must not newly become implausible.
    """
    codes = sorted(cands)
    out = []
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            if a not in KEY_FACTS and b not in KEY_FACTS:
                continue
            Ef = dict(E)
            Ef[a], Ef[b] = cands[a], cands[b]
            if implausible(Ef) and not implausible(E):
                continue
            out.append((a, b))
    return out


def design_false(items):
    """Assign a direction per item and draw E_f.

    For each item and direction, the feasible pairs of changes are split by
    "composition": whether age is one of the two changes (a large CKD signal).

    "random" items: both directions are feasible with a common composition.
        The composition is drawn first (from those common to both directions),
        then the direction is randomised 50/50 within label. So among these
        items direction is independent of the patient profile and of whether
        age changes.
    "nonrandom" items: only one direction is feasible (it is used), or both
        are but with no common composition (healthier is used: it is the
        scarcer direction). Exclude these from sicker-vs-healthier contrasts.
    """
    opts = {}
    for it in items.values():
        d = directional_changes(it["E"])
        opts[it["item_id"]] = {k: (v, valid_pairs(it["E"], v)) for k, v in d.items()}
    comps = {i: {k: {("age" in p) for p in o[k][1]} for k in o} for i, o in opts.items()}

    direction, want_age = {}, {}
    for label in sorted({it["label"] for it in items.values()}):
        ids = [i for i, it in items.items() if it["label"] == label]
        free = []
        for i in ids:
            feas = [k for k in ("sicker", "healthier") if opts[i][k][1]]
            assert feas, "item with no feasible direction: %s" % i
            common = comps[i]["sicker"] & comps[i]["healthier"]
            r = rng("false_composition", i)
            if len(feas) == 2 and common:
                free.append(i)
                want_age[i] = r.choice(sorted(common))
                items[i]["direction_assignment"] = "random"
            else:
                direction[i] = feas[0] if len(feas) == 1 else "healthier"
                want_age[i] = r.random() < 0.5
                items[i]["direction_assignment"] = "nonrandom"
        r = rng("direction", label)
        r.shuffle(free)
        n_h = len(free) // 2 + (len(free) % 2 if r.random() < 0.5 else 0)
        for j, i in enumerate(free):
            direction[i] = "healthier" if j < n_h else "sicker"

    for i, it in items.items():
        d = direction[i]
        cands, pairs = opts[i][d]
        r = rng("false_pair", i)
        pool = [p for p in pairs if ("age" in p) == want_age[i]]
        if not pool:  # only possible for nonrandom items
            assert it["direction_assignment"] == "nonrandom", i
            pool = pairs
        a, b = r.choice(pool)
        # Preserve the v2 ordering of the two edits.
        if a in KEY_FACTS and b in KEY_FACTS:
            first, second = (a, b) if r.random() < 0.5 else (b, a)
        else:
            first, second = (a, b) if a in KEY_FACTS else (b, a)
        Ef = dict(it["E"])
        Ef[first], Ef[second] = cands[first], cands[second]
        it["direction"] = d
        it["Ef"] = Ef
        it["false_changed"] = "; ".join("%s: %s -> %s" % (SHORT[c], it["E"][c], Ef[c])
                                        for c in (first, second))


# --------------------------------------------------------------------------
# Optional high-threshold questions
# --------------------------------------------------------------------------
def sub_once(text, old, new):
    assert text.count(old) == 1, (old, text)
    return text.replace(old, new)


def high_threshold_templates(first, second):
    """Derive 50% / 66.7% / 80% variants from the original k=2 templates."""
    fn = "a false negative, meaning failing to screen a patient who truly has CKD,"
    fp = "a false positive, meaning screening a patient who does not have CKD"
    cost_old = "Assume that the cost of %s is 2 times the cost of %s." % (fn, fp)
    cost_new = {
        "1": "Assume that the cost of %s is equal to the cost of %s." % (fn, fp),
        "0.5": "Assume that the cost of %s, is 2 times the cost of %s." % (fp, fn.rstrip(",")),
        "0.25": "Assume that the cost of %s, is 4 times the cost of %s." % (fp, fn.rstrip(",")),
    }
    pct = {"1": "50%", "0.5": "66.7%", "0.25": "80%"}
    loss = {"1": "1 point", "0.5": "2 points", "0.25": "4 points"}
    new_first, new_second, q_order = {}, {}, []
    for kind in ("threshold", "bet", "cost"):
        for k in ("1", "0.5", "0.25"):
            for order in ("normal", "flip"):
                key = ("%s_%s" % (kind, k), order)
                q_order.append(key)
                for src, dst in ((first, new_first), (second, new_second)):
                    t = src[("%s_2" % kind, order)]
                    if kind == "threshold":
                        t = sub_once(t, "is at least 33.3%.", "is at least %s." % pct[k])
                    elif kind == "bet":
                        t = sub_once(t, "the net gain is 2 points.", "the net gain is 1 point.")
                        t = sub_once(t, "the net loss is 1 point.", "the net loss is %s." % loss[k])
                    else:
                        t = sub_once(t, cost_old, cost_new[k])
                    dst[key] = t
    return new_first, new_second, q_order


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def assign_block_order(items):
    # Split within each direction/assignment stratum, and as evenly as possible
    # within label. Alternate any spare patient across the odd-sized strata.
    strata = collections.defaultdict(lambda: collections.defaultdict(list))
    for it in items.values():
        strata[it["direction_assignment"], it["direction"]][it["label"]].append(it)
    extra_own = True
    for stratum, by_label in sorted(strata.items()):
        labels = sorted(by_label)
        rng("block_order_label", *stratum).shuffle(labels)
        for label in labels:
            patients = by_label[label]
            rng("block_order", *stratum, label).shuffle(patients)
            half, odd = divmod(len(patients), 2)
            n_own = half + (odd and extra_own)
            if odd:
                extra_own = not extra_own
            for j, it in enumerate(patients):
                it["block_order"] = "own_first" if j < n_own else "person_first"


def assign_checks(items, setting):
    """Pair patients so each fact and option set has opposite correct answers.
    For false checks even the model's own value matches across the pair.
    """
    graph = nx.Graph()
    graph.add_nodes_from(sorted(items))
    choices = {}
    r = rng("matched_checks", setting)
    for i, j in combinations(sorted(items), 2):
        candidates = []
        for changed_id, control_id in ((i, j), (j, i)):
            changed, control = items[changed_id], items[control_id]
            for code in KEY_FACTS:
                if setting == "partial":
                    if code in changed["Ep"] or code not in control["Ep"]:
                        continue
                else:
                    own = changed["E"][code]
                    if (changed["Ef"][code] == own or control["E"][code] != own
                            or control["Ef"][code] != own):
                        continue
                candidates.append((changed_id, control_id, code))
        if candidates:
            # Prefer pairs in the same record-order arm. Exact age matches are
            # scarcer than binary matches, so use them when feasible.
            age_matches = [c for c in candidates if c[2] == "age"] if setting == "false" else []
            choices[i, j] = age_matches or candidates
            weight = r.randrange(1, 2**32)
            if age_matches:
                weight += 2**40
            if items[i]["block_order"] == items[j]["block_order"]:
                weight += 2**50
            graph.add_edge(i, j, weight=weight)
    pairs = nx.max_weight_matching(graph, maxcardinality=True)
    if 2 * len(pairs) != len(items):
        raise ValueError("Could not pair all patients for " + setting + " checks")
    for i, j in sorted(tuple(sorted(edge)) for edge in pairs):
        changed_id, control_id, code = r.choice(choices[i, j])
        changed, control = items[changed_id], items[control_id]
        if setting == "partial":
            options = ("Yes", "No")
            answers = ((changed, "withheld", "No"), (control, "shown", "Yes"))
        else:
            own, reported = changed["E"][code], changed["Ef"][code]
            options = [own, reported]
            r.shuffle(options)
            answers = ((changed, "changed", reported), (control, "unchanged", own))
        for it, relation, answer in answers:
            it[setting + "_check"] = (code, relation, options, answer)


def build_item(it, labels, first, second, q_order, stems, battery):
    i = it["item_id"]
    rows = []
    # Match field order across all questions and both batteries within a trial.
    # Give first-order replicates independent rankings from the attributions.
    rankings = {}
    def block(role, facts, replicate, task):
        key = (role, replicate, task)
        if key not in rankings:
            order = sorted(labels)
            rng("v3_fact_order", i, *key).shuffle(order)
            rankings[key] = order
        order = [code for code in rankings[key] if code in facts]
        header = "Your record for this patient:" if role == "own" else STUDY_PERSON_HEADER.format(name=it["name"])
        return render_block(header, facts, labels, order)

    def emit(setting, case, replicate, key, question, own=None, person=None, check_answer=""):
        q, opt_order = key
        blocks = []
        if own is not None:
            task = "first_order" if person is None else "attribution"
            blocks.append(block("own", own, replicate, task))
        if person is not None:
            blocks.append(block("person", person, replicate, "attribution"))
        two = len(blocks) == 2
        if two and it["block_order"] == "person_first":
            blocks.reverse()
        prompt = "\n\n".join(blocks + [question])
        check = it[setting + "_check"] if q == "check" else None
        rows.append(dict(item_id=i, label=it["label"], setting=setting, case=case,
                         question_id=q, order=opt_order,
                         person_name=it["name"] if person is not None else "",
                         changed_facts=it[setting + "_changed"] if setting != "shared" else "",
                         prompt=prompt.replace("80.0 years", "80 years or older"),
                         check_answer=check_answer, replicate=replicate,
                         direction=it["direction"] if setting == "false" else "",
                         direction_assignment=it["direction_assignment"] if setting == "false" else "",
                         block_order=it["block_order"] if two else "",
                         check_fact=SHORT[check[0]] if check else "",
                         check_relation=check[1] if check else ""))

    E = it["E"]
    for key in q_order:
        own_q = first[key]
        person_q = second[key].format(name=it["name"])
        for rep in ("1", "2"):
            emit("shared", "A", rep, key, own_q, own=E)
        emit("shared", "D", "1", key, person_q, person=E)
        emit("shared", "D2", "1", key, person_q, own=E, person=E)
        for setting, Ep in (("partial", it["Ep"]), ("false", it["Ef"])):
            for rep in ("1", "2"):
                emit(setting, "B", rep, key, own_q, own=Ep)
            emit(setting, "M", "1", key, person_q, person=Ep)
            emit(setting, "M2", "1", key, person_q, own=Ep, person=Ep)
            emit(setting, "C", "1", key, person_q, own=E, person=Ep)
    if battery == "main":
        for setting, Ep in (("partial", it["Ep"]), ("false", it["Ef"])):
            code, _, options, answer = it[setting + "_check"]
            stem = stems[setting].format(name=it["name"], fact=SHORT[code])
            if setting == "partial":
                stem = f"Did the record shown to {it['name']} include a value for {SHORT[code]}?"
            for order, opts in (("normal", options), ("flip", options[::-1])):
                q = f"{stem}\nA. {opts[0]}\nB. {opts[1]}\n\nAnswer with a single letter."
                emit(setting, "C", "1", ("check", order), q, own=E, person=Ep,
                     check_answer="AB"[opts.index(answer)])
    return rows


def role_swap_rows(it, canonical):
    """Hold both physical records fixed, exchanging only their ownership."""
    setting = canonical["setting"]
    name = it["name"]
    own_head = "Your record for this patient:"
    person_head = STUDY_PERSON_HEADER.format(name=name)
    swap = {own_head: person_head, person_head: own_head}
    *records, question = canonical["prompt"].split("\n\n")
    assert len(records) == 2
    reversed_records = []
    for record in records:
        header, facts = record.split("\n", 1)
        reversed_records.append(swap[header] + "\n" + facts)
    reversed_order = "person_first" if canonical["block_order"] == "own_first" else "own_first"
    swapped = dict(canonical, case="C_swap", block_order=reversed_order,
                   prompt="\n\n".join(reversed_records + [question]))
    yield swapped

    E = it["E"]
    Ep = it["Ep"] if setting == "partial" else it["Ef"]
    options_rng = rng("v3_role_check", it["item_id"], setting)
    eligible = [code for code in KEY_FACTS if E[code] != Ep.get(code)]
    code = options_rng.choice(eligible)
    probe_changes = canonical["changed_facts"]
    if setting == "partial":
        # Present two nine-field records with eight shared fields. Both lists
        # are drawn from E. Each has a unique field that can be probed.
        person_only = options_rng.choice([k for k in KEY_FACTS if k in Ep])
        base_own_codes = (set(Ep) - {person_only}) | {code}
        for idx, record in enumerate(records):
            header, fields = parse_block(record)
            if header == own_head:
                lines = record.splitlines()
                chosen_lines = [line for line, (k, _, _) in zip(lines[1:], fields) if k in base_own_codes]
                records[idx] = "\n".join([header] + chosen_lines)
                reversed_records[idx] = "\n".join([person_head] + chosen_lines)
        probe_changes = f"base model only: {SHORT[code]}; base person only: {SHORT[person_only]}"
        if it["probe_person_only"]:
            code = person_only
            answers, relations = ("Yes", "No"), ("shown", "withheld")
        else:
            answers, relations = ("No", "Yes"), ("withheld", "shown")
        stem = f"Is {SHORT[code]} listed in the record available to {name}?"
    else:
        stem = f"Which value for {SHORT[code]} is listed in the record available to {name}?"
        answers = (Ep[code], E[code])
        relations = ("conflicting", "conflicting")
    field = SHORT[code]
    options = list(answers)
    options_rng.shuffle(options)
    for record_set, base, answer, relation in zip((records, reversed_records), (canonical, swapped),
                                                  answers, relations):
        for order, opts in (("normal", options), ("flip", options[::-1])):
            tail = f"{stem}\nA. {opts[0]}\nB. {opts[1]}\n\nAnswer with a single letter."
            yield dict(base, case="probe" if base is canonical else "probe_swap",
                       changed_facts=probe_changes, question_id="role_check", order=order, check_fact=field,
                       check_relation=relation, check_answer="AB"[opts.index(answer)],
                       prompt="\n\n".join(record_set + [tail]))


def write_csv(path, rows):
    # Same dialect as the original file: minimal quoting, CRLF row terminator.
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r[c] for c in COLUMNS])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("original_csv")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    rows = load(args.original_csv)
    items, labels = parse_items(rows)
    first, second, stems, q_order = extract_templates(items)
    n = fidelity_check(items, labels, first, second)
    print("parsed %d items; rebuilt %d original prompts byte-for-byte" % (len(items), n))

    design_false(items)
    assign_block_order(items)
    probe_ids = sorted(items)
    rng("v3_probe_target_side").shuffle(probe_ids)
    for j, i in enumerate(probe_ids):
        items[i]["probe_person_only"] = j < len(probe_ids) // 2
    for setting in ("partial", "false"):
        assign_checks(items, setting)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "patients.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["item_id", "label", "person_name", "block_order", "false_direction",
                         "direction_assignment", "direction_contrast_eligible"])
        for it in items.values():
            writer.writerow([it["item_id"], it["label"], it["name"], it["block_order"],
                             it["direction"], it["direction_assignment"],
                             int(it["direction_assignment"] == "random")])
    role_rows = []
    for battery, filename, templates in (
        ("main", "ckd_second_order_v3.csv", (first, second, q_order)),
        ("high_thresholds", "ckd_second_order_v3_high_thresholds.csv",
         high_threshold_templates(first, second)),
    ):
        path = os.path.join(args.out_dir, filename)
        bat_first, bat_second, keys = templates
        def generate_rows():
            for it in items.values():
                for row in build_item(it, labels, bat_first, bat_second, keys, stems, battery):
                    if row["case"] == "C" and row["question_id"] == "probability":
                        role_rows.extend(role_swap_rows(it, row))
                    yield row
        write_csv(path, generate_rows())
        print("wrote", path)
    path = os.path.join(args.out_dir, "ckd_second_order_v3_role_swap.csv")
    write_csv(path, role_rows)
    print("wrote", path)


if __name__ == "__main__":
    sys.exit(main())
