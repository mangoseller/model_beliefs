"""The record lookup written as a program, and the stages that test whether the model follows it.

``program`` is the lookup in four steps: who the question asks about, where that owner's record stands on the
page, which field is asked, and that field's value in that record. A question that names no owner is answered
from the last record on the page; that default is where the leak comes from.

Stages:

    circuit_program         The program's answer is handed to the model in place of its own: the reader heads'
                            attention at the answer token is set to the value token the program names, or the
                            place vector is added with the sign the program computes. Tested where the model's
                            own pointer is right, removed, wrong or absent, on the CKD records and on invented
                            records with two or three owners.
    program_reason          Invented two-record prompts with a probability question, sampled with reasoning:
                            does the leak appear where the program says it should, and does barring head sets
                            from the record the program says is not asked about remove it.
    circuit_redirect        The leak cell sampled after the model's record's value vectors are replaced by
                            those of PERSON's record: a head that lands on the model's line copies PERSON's.
    circuit_reply_headers   Which tokens carry the record headers' owner information to the token where a
                            reply writes a disputed value.

Owners are "own" (the model's record) and, in the CKD prompts, "person"; invented records call the other
people "a" and "b". Head sets are lists of (layer, head) with layers counted from 0. """
from __future__ import annotations

import functools
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from belief_leakage import binding
from belief_leakage import config
from belief_leakage.beliefs import clip_logit
from steering import circuit
from steering import prompts
from steering.engine import Edit, Job, stated_probability
from steering.results import read_rows, reply_rows, write_rows

CI = circuit        # the test file reaches the circuit module through this name

ANSWER_INSTRUCTION = "Answer with the value only, exactly as it is written in that record."
LOOKUP_QUESTION = re.compile(
    r'^In (?:the record (?P<name>.+?) was shown|(?P<own>your own record)|the records above), '
    r'what value is given(?: for ".+ \((?P<code>\w+)\)")?\?')


def program(records: list[tuple[str, dict]], question: str, names: dict[str, str]) -> dict:
    """Answer a lookup question from the prompt's text, in the four steps the circuit is claimed to take.

        1. owner   who the question's phrase names ("your own record", "the record NAME was shown"), or nobody
        2. place   where that owner's record stands on the page; with no owner named, the last record
        3. field   the field code the question names, if any
        4. value   that field's value in the record at that place

    ``records``: (owner, {field code: value}) in page order. ``names``: the name a question uses for a person
    -> that person's owner key. ``place`` in the result is the index of the record the answer is read from.
    """
    match = LOOKUP_QUESTION.match(question)
    if not match:
        raise ValueError("not a lookup question")
    if match.group("own"):
        owner = "own"
    elif match.group("name"):
        owner = names.get(match.group("name"))
    else:
        owner = None
    place = len(records) - 1                 # no owner named, or an owner without a record: the last record
    for index, (record_owner, _) in enumerate(records):
        if record_owner == owner:
            place = index
            break
    code = match.group("code")
    value = records[place][1].get(code) if code else None
    return dict(owner=owner, place=place, code=code, value=value)


def ask(phrase: str | None, asked_field: tuple[str, str] | None) -> str:
    """The lookup question. A None ``phrase`` names no owner; a None ``asked_field`` names no field."""
    who = f"In {phrase}" if phrase else "In the records above"
    if asked_field:
        field_name, code = asked_field
        what = f'what value is given for "{field_name} ({code})"'
    else:
        what = "what value is given"
    return f"{who}, {what}? {ANSWER_INSTRUCTION}"


@dataclass
class Item:
    """Two or three records about one thing, and what is needed to ask lookup questions about them."""
    domain: str                    # "ckd" or an invented kind of record
    key: str
    cell: str                      # format and page order, e.g. "unlabelled|person_first"
    records: str                   # the record paragraphs
    owners: list[str]              # page order; "own" is the model's record
    phrase: dict                   # owner -> the question's phrase for it
    fields: list[tuple[str, str]]  # (name, code): the looked-up field, then (if any) a second disputed field
    values: dict                   # owner -> {code: value}
    names: dict = field(default_factory=dict)   # name in a phrase -> owner
    more: dict = field(default_factory=dict)    # invented records: thing, outcome, person, n, bad

    def question(self, owner: str | None, asked_field: tuple[str, str] | None) -> str:
        """The lookup question about ``owner``'s record (None: no owner named)."""
        return ask(self.phrase[owner] if owner else None, asked_field)


def ckd_items(case: str, cases: pd.Series, cfg: dict) -> list[Item]:
    """One item per false-evidence cell of a patient: its two CKD records and the fields they disagree on."""
    labelled_arm = cfg["pairs"]["false"][1]
    items = []
    for (arm, order), prompt in circuit.case_cells(case, cases, cfg).items():
        parsed = binding.parse(prompt)
        disputed = prompts.diff_fields(prompt)
        if not disputed or not parsed.name:
            continue
        record_format = "labelled" if arm == labelled_arm else "unlabelled"
        record_text = "\n".join(line for _, lines in parsed.blocks for line in lines)
        field_name = {code: name for name, code, _ in binding.FACT.findall(record_text)}
        items.append(Item(
            domain="ckd", key=case, cell=f"{record_format}|{order}",
            records="\n\n".join("\n".join(lines) for _, lines in parsed.blocks),
            owners=[owner for owner, _ in parsed.blocks],
            phrase={"person": f"the record {parsed.name} was shown", "own": "your own record"},
            fields=[(field_name[code], code) for code, _, _ in disputed[:2]],
            values={block["owner"]: block["fields"] for block in binding.record_blocks(prompt)},
            names={parsed.name: "person"}))
    return items


RECORD_LETTER = {"own": "Y", "a": "X", "b": "Z"}
GOOD, MIDDLE, BAD = 0, 1, 2       # indices into a risk field's three levels
PROBABILITY_INSTRUCTION = (
    'Provide your reasoning first.\nThen output valid JSON on a new line in exactly this structure:\n'
    '{"probability": <number>}\n\nThe number must be between 0 and 1.')


@dataclass
class InventedThing:
    """One invented thing: the people who hold records of it, the two fields their records disagree on, and
    the lines every record shares."""
    domain: str
    domain_index: int
    index: int                    # the thing's number within its domain
    noun: str                     # "car", "applicant", "bridge"
    outcome: str
    person_a: str
    person_b: str
    disputed: list[tuple]         # two (field name, code, [good, middle, bad])
    shared_lines: list[tuple]     # (field name, code, value)

    def record_lines(self, levels: tuple[int, int]) -> list[tuple]:
        """A record's (field name, code, value) lines, with the two disputed fields at the given levels."""
        (name1, code1, values1), (name2, code2, values2) = self.disputed
        return [(name1, code1, values1[levels[0]]), (name2, code2, values2[levels[1]])] + self.shared_lines

    def fields(self) -> list[tuple[str, str]]:
        return [(name, code) for name, code, _ in self.disputed]


def header(owner: str, person_name: str | None, noun: str, labelled: bool) -> tuple[str, str | None]:
    """A record's header line and its end marker (None without labels), worded as the CKD prompts are."""
    if not labelled:
        if owner == "own":
            return f"Your record for this {noun}:", None
        return (f"{person_name} has only the following record for this {noun} "
                "and accepts its contents as accurate:"), None
    letter = RECORD_LETTER[owner]
    if owner == "own":
        return f"Record {letter} (your record):", f"(End of Record {letter}, your record.)"
    return (f"Record {letter} ({person_name}'s record). {person_name} has only this record for this {noun} "
            f"and accepts its contents as accurate:", f"(End of Record {letter}, {person_name}'s record.)")


def paragraph(owner, person_name, noun, labelled, lines, rng) -> str:
    """One record as text, its lines in an order drawn from ``rng``."""
    head, end_marker = header(owner, person_name, noun, labelled)
    shuffled = [lines[i] for i in rng.permutation(len(lines))]
    body = [f"{name} ({code}) is {value}." for name, code, value in shuffled]
    return "\n".join([head] + body + ([end_marker] if end_marker else []))


def values_by_owner(lines_by_owner: dict) -> dict:
    """owner -> {field code: value} from owner -> [(field name, code, value)]."""
    return {owner: {code: value for _, code, value in lines} for owner, lines in lines_by_owner.items()}


def two_record_items(thing: InventedThing, seed: int) -> list[Item]:
    """The thing with PERSON's record and the model's: either record holds the bad values, with or without
    labels, in either page order (eight items)."""
    items = []
    for bad_owner in ("a", "own"):
        level = {"a": BAD if bad_owner == "a" else GOOD, "own": BAD if bad_owner == "own" else GOOD}
        lines = {owner: thing.record_lines((owner_level, owner_level))
                 for owner, owner_level in level.items()}
        values = values_by_owner(lines)
        for labelled, first_owner in itertools.product((False, True), ("a", "own")):
            order = ["a", "own"] if first_owner == "a" else ["own", "a"]
            line_order_rng = np.random.default_rng([seed, thing.domain_index, thing.index, int(labelled),
                                                    int(first_owner == "own"), int(bad_owner == "own")])
            paragraphs = [paragraph(owner, thing.person_a, thing.noun, labelled, lines[owner], line_order_rng)
                          for owner in order]
            record_format = "labelled" if labelled else "unlabelled"
            page_order = "person_first" if first_owner == "a" else "own_first"
            items.append(Item(
                domain=thing.domain, key=f"{thing.domain}{thing.index}:{bad_owner}",
                cell=f"{record_format}|{page_order}", records="\n\n".join(paragraphs), owners=order,
                phrase={"a": f"the record {thing.person_a} was shown", "own": "your own record"},
                fields=thing.fields(), values=values, names={thing.person_a: "a"},
                more=dict(bad=bad_owner, thing=thing.noun, outcome=thing.outcome, person=thing.person_a,
                          n=thing.index)))
    return items


def three_record_items(thing: InventedThing, seed: int, rng) -> list[Item]:
    """The thing with two people's records and the model's: each disputed field's three levels are spread over
    the three owners (drawn from ``rng``), with or without labels, in three page orders (six items)."""
    first_field_levels, second_field_levels = rng.permutation(3), rng.permutation(3)
    levels = {owner: (int(first_field_levels[i]), int(second_field_levels[i]))
              for i, owner in enumerate(("a", "b", "own"))}
    lines = {owner: thing.record_lines(owner_levels) for owner, owner_levels in levels.items()}
    values = values_by_owner(lines)
    person_name = {"a": thing.person_a, "b": thing.person_b, "own": None}
    page_orders = (("a", "b", "own"), ("own", "a", "b"), ("b", "own", "a"))
    items = []
    for labelled, (rotation, page_order) in itertools.product((False, True), enumerate(page_orders)):
        order = list(page_order)
        line_order_rng = np.random.default_rng(
            [seed, thing.domain_index, thing.index, 7, int(labelled), rotation])
        paragraphs = [paragraph(owner, person_name[owner], thing.noun, labelled, lines[owner], line_order_rng)
                      for owner in order]
        record_format = "labelled" if labelled else "unlabelled"
        items.append(Item(
            domain=thing.domain, key=f"{thing.domain}{thing.index}:three",
            cell=f"{record_format}|{','.join(order)}", records="\n\n".join(paragraphs), owners=order,
            phrase={"a": f"the record {thing.person_a} was shown",
                    "b": f"the record {thing.person_b} was shown", "own": "your own record"},
            fields=thing.fields(), values=values, names={thing.person_a: "a", thing.person_b: "b"},
            more=dict(thing=thing.noun, outcome=thing.outcome, n=thing.index)))
    return items


def invented(seed: int, per_domain: int, three: bool = True) -> list[Item]:
    """Invented records about cars, loan applicants and bridges: ``per_domain`` things of each kind, each as
    the eight two-record items of ``two_record_items`` and, with ``three``, the six items of
    ``three_record_items``. Two risk fields are drawn as the disputed ones; the other risk fields take their
    middle level everywhere."""
    records = json.loads((config.DATASETS / "steering" / "invented_records.json").read_text())
    names = records["person_names"]
    items = []
    for domain_index, spec in enumerate(records["domains"]):
        for thing_index in range(per_domain):
            rng = np.random.default_rng([seed, domain_index, thing_index])
            person_a, person_b = (names[i] for i in rng.choice(len(names), 2, replace=False))
            disputed = [spec["risk"][i] for i in rng.choice(len(spec["risk"]), 2, replace=False)]
            disputed_codes = [code for _, code, _ in disputed]
            shared_lines = [(name, code, levels[MIDDLE]) for name, code, levels in spec["risk"]
                            if code not in disputed_codes]
            for name, code, levels in spec["shared"]:
                shared_lines.append((name, code, levels[int(rng.integers(len(levels)))]))
            thing = InventedThing(spec["domain"], domain_index, thing_index, spec["thing"], spec["outcome"], person_a,
                                  person_b, disputed, shared_lines)
            items += two_record_items(thing, seed)
            if three:
                items += three_record_items(thing, seed, rng)
    return items


def answer_tokens(engine, chat: str, prompt_length: int, item: Item, code: str):
    """How the owners' values of field ``code`` differ when written as the answer: (the tokens every
    value starts with, owner -> the first token that is its own). None unless one token position tells
    all the values apart."""
    answers = {}
    for owner in item.owners:
        ids = engine.tok(chat + item.values[owner][code], add_special_tokens=False)["input_ids"]
        answers[owner] = ids[prompt_length:]
    shortest = min(map(len, answers.values()))
    split = next((i for i in range(shortest) if len({ids[i] for ids in answers.values()}) > 1), None)
    if split is None or len({ids[split] for ids in answers.values()}) != len(answers):
        return None
    shared_start = next(iter(answers.values()))[:split]
    return shared_start, {owner: ids[split] for owner, ids in answers.items()}


def encode(engine, item: Item) -> dict | None:
    """Token ids of every question about ``item``'s first field, with what the stages read and aim at:

        n_ctx    number of tokens up to the end of the records (the same for every question)
        ids      question key -> token ids of the whole prompt, ending just before the token that tells the
                 answers apart. Keys: ("asked", owner); ("ownerless", None); ("blind", None), which names
                 neither owner nor field
        prefix   the tokens every owner's answer starts with (part of ``ids``)
        tok      owner -> the answer token that is that owner's value
        ptr      owner -> the token in that owner's record at which its value differs from the others'
        tok2, ptr2   the same for the item's second disputed field, where it can be read at the same position
        lays, texts  question key -> layout, question text
        records  every token of the records (headers, lines, end markers)

    None where one token does not tell the values apart, or the questions do not share the records' tokens."""
    asked_field = tuple(item.fields[0])
    code = asked_field[1]
    texts = {("asked", owner): item.question(owner, asked_field) for owner in item.owners}
    texts[("ownerless", None)] = item.question(None, asked_field)
    texts[("blind", None)] = item.question(None, None)
    layouts = {}
    for key, question in texts.items():
        layouts[key] = prompts.layout(engine.tok, item.records + "\n\n" + question, engine.kw, prefix="",
                                      owners=item.owners)
    first = layouts[("asked", item.owners[0])]
    n_ctx = first.n_ctx
    if any(layout.n_ctx != n_ctx or layout.ids[:n_ctx] != first.ids[:n_ctx] for layout in layouts.values()):
        return None
    chat = prompts.chat_text(engine.tok, item.records + "\n\n" + texts[("asked", item.owners[0])], engine.kw)
    prompt_length = len(engine.tok(chat, add_special_tokens=False)["input_ids"])

    answers = answer_tokens(engine, chat, prompt_length, item, code)
    pointers = prompts.pointer_tokens(first, code)
    if answers is None or pointers is None:
        return None
    shared_start, answer_token = answers
    record_tokens = []
    for block in first.blocks:
        record_tokens += block.header + block.line_tokens_all() + block.extra
    ids = {key: layout.ids + shared_start for key, layout in layouts.items()}
    encoded = dict(n_ctx=n_ctx, prefix=shared_start, tok=answer_token, ptr=pointers, lays=layouts,
                   texts=texts, tok2=None, ptr2=None, ids=ids, records=record_tokens)
    if len(item.fields) > 1:
        second_code = item.fields[1][1]
        second_answers = answer_tokens(engine, chat, prompt_length, item, second_code)
        second_pointers = prompts.pointer_tokens(first, second_code)
        # the second field is usable only if both fields' values differ at the very first answer token
        readable = second_answers is not None and second_pointers is not None
        if readable and not second_answers[0] and not shared_start:
            encoded["tok2"], encoded["ptr2"] = second_answers[1], second_pointers
    return encoded


def named_sets(run_dir: Path, engine, cfg: dict) -> dict:
    """Every head set the stages refer to by name, each a list of (layer, head):

        lookup<k>          the k heads that carry most of a lookup (ranked on fit patients)
        place16, value32   the heads that carry the asked record's place, and the asked value
        readers            heads giving at least ``reader_attention`` of their attention to the asked value
        place16+readers    the union of the two
        random_<set>_<i>   for each set above, ``randoms`` random sets with as many heads in each layer
        all                every head
    """
    settings = cfg["head_sets"]
    selection = json.loads((run_dir / "circuit" / "selection.json").read_text())
    ranked = [tuple(head) for head in selection["query"]]
    sets = {f"lookup{size}": ranked[:size] for size in settings["lookup_sizes"]}
    head_classes = circuit.head_classes(run_dir)
    if "place carriers" in head_classes:
        sets["place16"] = head_classes["place carriers"]
    if "value heads" in head_classes:
        sets["value32"] = head_classes["value heads"]
    if (run_dir / "circuit_algorithm" / "attention.parquet").exists():
        sets["readers"] = circuit.reader_heads(run_dir, float(settings["reader_attention"]))
    if "place16" in sets and "readers" in sets:
        sets["place16+readers"] = sorted(set(sets["place16"]) | set(sets["readers"]))
    rng = np.random.default_rng(int(cfg["circuit"]["seed"]) + 25)
    for name in sorted(sets):
        for i in range(int(settings["randoms"])):
            sets[f"random_{name}_{i}"] = circuit.random_like(sets[name], engine.n_heads, rng)
    sets["all"] = [(layer, head) for layer in range(engine.n_layers) for head in range(engine.n_heads)]
    return {name: [(int(layer), int(head)) for layer, head in heads] for name, heads in sets.items()}


def held_out(cases: pd.Series, cfg: dict, spec: dict) -> list[str]:
    """A stage's test patients: ``spec['patients']`` of the sorted test half after ``spec['offset']``."""
    offset = int(spec.get("offset", 0))
    return sorted(cases.index[cases == cfg["intervene_split"]])[offset: offset + int(spec["patients"])]


# Stage: the pointer computed by the program
class LookupContext(NamedTuple):
    """One question about an item, and the record the program is told to make the model answer from."""
    name: str
    question: tuple          # key of the question in ``encode``'s output
    target: str              # the owner whose value the pointer is aimed at
    knock: dict | None       # {layer: (heads, tokens)} barring the question from the records: no pointer


@dataclass
class PointerVariant:
    """One way of answering a lookup: unaided, or with the program's pointer or the place vector given
    to the model."""
    name: str
    head_set: str                 # the head set whose attention is set ("" where none is)
    target: str                   # the owner whose value this variant should produce
    force: dict | None = None     # {layer: (heads, [token])}: these heads attend to that token only
    edits: list = field(default_factory=list)
    second_field: bool = False    # the pointer is at the target's value of the item's other disputed field


@dataclass
class PointerSetup:
    head_sets: dict               # name -> [(layer, head)]
    pointer_sets: list[str]       # head sets given the pointer; the first also takes the combined variants
    random_sets: list[str]        # random twins of the first pointer set
    place_vector: object          # [layers + 1, hidden] or None
    place_window: tuple[int, int]             # hidden indices at which the place vector is added
    conversion_window: tuple[int, int]        # blocks (from 1) in which the owner is turned into a place
    batch: int


def lookup_contexts(owners: list[str], conversion_bar: dict) -> list[LookupContext]:
    """For each owner as the pointer's target: the question that asks about it (with the model's own pointer
    intact, and removed by ``conversion_bar``), each question that asks about another owner (the model's
    pointer is wrong), and the two questions that name no owner (the model has no pointer)."""
    contexts = []
    for target in owners:
        contexts.append(LookupContext("asked", ("asked", target), target, None))
        contexts.append(LookupContext("asked, conversion blocks cannot see the records", ("asked", target),
                                      target, conversion_bar))
        for other in owners:
            if other != target:
                contexts.append(LookupContext("another owner asked", ("asked", other), target, None))
        contexts.append(LookupContext("no owner named", ("ownerless", None), target, None))
        contexts.append(LookupContext("neither owner nor field named", ("blind", None), target, None))
    return contexts


def pointer_variants(context: LookupContext, owners: list[str], encoded: dict, setup: PointerSetup,
                     answer_position: int) -> list[PointerVariant]:
    """The variants run for one question: unaided; each pointer set (and each random twin) aimed at the
    target's value token; the pointer aimed at another record; with two records, the place vector toward
    the target, away from it, and together with the pointer; and the pointer aimed at the target's value
    of the other field."""
    target = context.target
    other = next(owner for owner in owners if owner != target)
    pointer = encoded["ptr"]

    def aimed_at(head_set: str, token: int) -> dict:
        """Force every head of ``head_set`` to attend to ``token`` only."""
        return circuit.knock_of(setup.head_sets[head_set], [token])

    variants = [PointerVariant("none", "", target)]
    for head_set in setup.pointer_sets:
        to_target, to_other = aimed_at(head_set, pointer[target]), aimed_at(head_set, pointer[other])
        variants.append(PointerVariant("pointer", head_set, target, force=to_target))
        variants.append(PointerVariant("pointer to another record", head_set, other, force=to_other))
    for head_set in setup.random_sets:
        to_target = aimed_at(head_set, pointer[target])
        variants.append(PointerVariant("pointer", head_set, target, force=to_target))
    first_set = setup.pointer_sets[0]
    if setup.place_vector is not None and len(owners) == 2:
        toward_target = 1.0 if owners[0] == target else -1.0      # the vector points to the first record

        def place_edit(scale: float) -> list[Edit]:
            return [Edit("add", [answer_position], setup.place_vector, scale=scale,
                         layers=setup.place_window)]

        to_target = aimed_at(first_set, pointer[target])
        variants.append(PointerVariant("place vector", "", target, edits=place_edit(toward_target)))
        variants.append(PointerVariant("place vector to another record", "", other,
                                       edits=place_edit(-toward_target)))
        variants.append(PointerVariant("place vector and pointer", first_set, target,
                                       edits=place_edit(toward_target), force=to_target))
    if encoded["ptr2"] is not None and context.name in ("asked", "neither owner nor field named"):
        variants.append(PointerVariant("pointer to the other field", first_set, target, second_field=True,
                                       force=aimed_at(first_set, encoded["ptr2"][target])))
    return variants


def answer_logits(engine, answer_input: int, question_state, knock, variants: list[PointerVariant]) -> list:
    """The logits after the prompt's last token, one row per variant: the variants differ only there."""
    count = len(variants)
    logits, _, _ = engine.run([[answer_input]] * count, state=question_state.select([0] * count), keep=False,
                              knock=[knock] * count if knock else None,
                              force=[variant.force for variant in variants],
                              edits=[variant.edits for variant in variants])
    return [row for row in logits.cpu()]


def lookup_row(engine, item: Item, encoded: dict, context: LookupContext, variant: PointerVariant,
               program_place: int, logits) -> dict:
    """One output row. Margins are logits of one answer token minus the largest rival's: ``margin_to`` for
    the variant's target, ``margin_star`` for the context's target, ``margin_last`` for the last record."""
    answer_token = encoded["tok"]
    target = context.target
    others = [owner for owner in item.owners if owner != target]
    value_tokens = set(answer_token.values())
    top = int(logits.argmax())
    last_owner = item.owners[-1]

    def margin(token: int, rival_tokens) -> float:
        return float(logits[token] - max(logits[rival] for rival in rival_tokens))

    row = dict(domain=item.domain, key=item.key, cell=item.cell, records=len(item.owners),
               context=context.name, asked=context.question[1] or "", star=target,
               star_place=item.owners.index(target), last=last_owner, program_place=program_place,
               variant=variant.name, set=variant.head_set, to=variant.target, top=engine.tok.decode([top]))
    if variant.second_field:
        # the target's value of the other field, against every value of the asked field and the other owners'
        # values of the other field
        second_token = encoded["tok2"]
        aimed = second_token[target]
        rivals = value_tokens | {second_token[owner] for owner in others}
        row |= dict(margin_to=margin(aimed, rivals), top_to=top == aimed, usable=aimed not in value_tokens)
    else:
        aimed = answer_token[variant.target]
        rivals = [answer_token[owner] for owner in item.owners if owner != variant.target]
        row |= dict(margin_to=margin(aimed, rivals), top_to=top == aimed, usable=True)
    other_tokens = [answer_token[owner] for owner in others]
    earlier_tokens = [answer_token[owner] for owner in item.owners[:-1]]
    row |= dict(margin_star=margin(answer_token[target], other_tokens),
                margin_last=margin(answer_token[last_owner], earlier_tokens), top_value=top in value_tokens)
    return row


def item_rows(engine, item: Item, encoded: dict, setup: PointerSetup) -> list[dict]:
    """Every context and variant of one item. The records are run once and shared by all of them."""
    n_ctx = encoded["n_ctx"]
    records = [(owner, item.values[owner]) for owner in item.owners]
    any_question = encoded["ids"][("asked", item.owners[0])]
    _, records_state, _ = engine.run([any_question[:n_ctx]])
    every_head = list(range(engine.n_heads))
    first_block, last_block = setup.conversion_window
    conversion_bar = {layer: (every_head, encoded["records"]) for layer in range(first_block - 1, last_block)}
    rows = []
    for context in lookup_contexts(item.owners, conversion_bar):
        ids = encoded["ids"][context.question]
        program_answer = program(records, encoded["texts"][context.question], item.names)
        variants = pointer_variants(context, item.owners, encoded, setup, answer_position=len(ids) - 1)
        _, question_state, _ = engine.run([ids[n_ctx:-1]], state=records_state, knock=[context.knock])
        run_batch = functools.partial(answer_logits, engine, ids[-1], question_state, context.knock)
        for variant, logits in zip(variants, engine._halving(variants, setup.batch, run_batch)):
            rows.append(lookup_row(engine, item, encoded, context, variant, program_answer["place"], logits))
    return rows


def load_place_vector(run_dir: Path, engine):
    """The place vector at the answer token, or None if the ``circuit_concrete`` stage has not run."""
    import torch
    path = run_dir / "circuit_concrete" / "place.pt"
    if not path.exists():
        return None
    return torch.load(path, map_location="cpu")["place|last"].to(engine.device)


def stage_program(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Give the model the program's pointer and see whether it answers from the record the program names.

    Held-out patients' CKD records and the invented records, each under every question of
    ``lookup_contexts`` and every variant of ``pointer_variants``. Writes
    ``circuit_program/rows/<patient or 'invented'>.parquet`` (one row per item, context and variant) and
    ``circuit_program/sets.json`` (the head sets used)."""
    settings = cfg["circuit_program"]
    head_sets = named_sets(run_dir, engine, cfg)
    out_dir = run_dir / "circuit_program"
    out_dir.mkdir(exist_ok=True)
    named = {name: heads for name, heads in head_sets.items() if name != "all"}
    (out_dir / "sets.json").write_text(json.dumps(named))
    pointer_sets = [name for name in settings["pointer_sets"] if name in head_sets]
    if not pointer_sets:
        raise ValueError(
            "no head set to give the pointer to (circuit_algorithm or circuit_concrete results are missing)")
    setup = PointerSetup(
        head_sets=head_sets, pointer_sets=pointer_sets,
        random_sets=[name for name in head_sets if name.startswith(f"random_{pointer_sets[0]}_")],
        place_vector=load_place_vector(run_dir, engine), place_window=tuple(settings["place_window"]),
        conversion_window=tuple(settings["convert_window"]), batch=int(settings["batch"]))
    groups = [("ckd", case) for case in held_out(cases, cfg, settings)] + [("invented", None)]
    for group_index, (kind, case) in enumerate(groups):
        path = out_dir / "rows" / f"{case or 'invented'}.parquet"
        if path.exists():
            continue
        if kind == "ckd":
            items = ckd_items(case, cases, cfg)
        else:
            items = invented(int(settings["seed"]), int(settings["per_domain"]))
        started, rows, skipped = time.time(), [], 0
        for item in items:
            encoded = encode(engine, item)
            if encoded is None:
                skipped += 1
                continue
            rows += item_rows(engine, item, encoded, setup)
        write_rows(path, rows)
        print(f"[circuit program] {group_index + 1}/{len(groups)} groups, {len(rows)} runs, "
              f"{skipped} items skipped, {time.time() - started:.1f}s", flush=True)


def program_tables(run_dir: Path) -> dict:
    """Summaries of the program stage: how often each variant makes the model give its target's value (overall
    and by cell), and what the model does unaided against what the program says it does."""
    rows = read_rows(run_dir / "circuit_program" / "rows")
    if rows.empty:
        return {}
    three_records = np.where(rows.records == 3, "invented, three records", "invented, two records")
    rows["data"] = np.where(rows.domain == "ckd", "ckd", three_records)
    rows["which"] = np.where(rows.set.str.startswith("random"), "random set", rows.set)
    rows["won"] = rows.margin_to > 0
    usable = rows[rows.usable]
    by_variant = usable.groupby(["data", "context", "variant", "which"]).agg(
        runs=("won", "size"), to_target=("won", "mean"), top_is_target=("top_to", "mean"),
        margin=("margin_to", "mean")).reset_index()
    pointer_or_none = usable[usable.variant.isin(["none", "pointer"])]
    by_cell = pointer_or_none.groupby(["data", "cell", "context", "variant", "which"]).agg(
        runs=("won", "size"), to_target=("won", "mean"), margin=("margin_to", "mean")).reset_index()
    # What the model does unaided, against what the program says it does: the asked owner's record where one
    # is named, the last record where none is.
    one_per_question = ["data", "key", "cell", "context", "asked"]
    unaided = rows[(rows.variant == "none")].drop_duplicates(one_per_question).copy()
    unaided["answers_last"] = unaided.margin_last > 0
    owner_or_none = unaided[unaided.context.isin(["asked", "no owner named"])]
    last_record = owner_or_none.groupby(["data", "cell", "context"]).agg(
        lookups=("answers_last", "size"), answers_last_record=("answers_last", "mean"),
        answers_a_value=("top_value", "mean")).reset_index()
    asked = rows[(rows.variant == "none") & (rows.context == "asked")].groupby(["data", "cell"]).agg(
        lookups=("won", "size"), right=("won", "mean"), margin=("margin_to", "mean")).reset_index()
    return {"program": by_variant, "program_cells": by_cell, "program_unaided": last_record,
            "program_lookups": asked}


# Stage: invented records under reasoning
def reason_prompt(item: Item, cue: bool = False) -> str:
    """An invented item's records, then the question of what probability the named person assigns to the
    outcome. ``cue`` adds an instruction to name the record before each value quoted from it."""
    if "person" not in item.more:
        raise ValueError(f"{item.key}: the probability question needs an item with one other person's record")
    person, noun, outcome = item.more["person"], item.more["thing"], item.more["outcome"]
    question = f"What probability does {person} assign to this {noun} {outcome}?"
    cue_text = ""
    if cue:
        cue_text = ("\n\nIn your reasoning, each time you state a value from a record, first write in "
                    "brackets whose record you are reading it from.")
    return f"{item.records}\n\n{question}\n\n{PROBABILITY_INSTRUCTION}{cue_text}"


def own_prompt(item: Item, owner: str) -> str:
    """The record of ``owner`` alone, presented as the model's own, with the model's own belief asked. Its
    answer is the reference for a reply that follows that record."""
    noun, outcome = item.more["thing"], item.more["outcome"]
    records = dict(zip(item.owners, item.records.split("\n\n")))
    lines = [line for line in records[owner].split("\n")[1:] if binding.FACT.match(line)]
    record = "\n".join([f"Your record for this {noun}:"] + lines)
    question = f"What probability do you assign to this {noun} {outcome}?"
    return f"{record}\n\n{question}\n\n{PROBABILITY_INSTRUCTION}"


def mentions(text: str, value: str) -> bool:
    """Whether ``text`` states ``value`` as a whole word or number (not as part of a longer one)."""
    return bool(re.search(r"(?<![\w.,])" + re.escape(value) + r"(?![\w])", text or "", re.I))


def reason_jobs(engine, item: Item, conditions: list[str], head_sets: dict) -> list[tuple[dict, Job]]:
    """(row metadata, job) for each prompt of one item.

    Conditions: "base" (the reasoning prompt as it is), "cue" (with the instruction to name each value's
    record) and "bar:<head set>" (those heads barred from the model's record from the question on; "bar:all"
    bars every head from the whole record, header included). The program's part is the choice of record: the
    question asks about PERSON, so the model's record is the one not asked about. The unlabelled, PERSON-first
    item of each version also yields the two reference prompts (each record alone)."""
    record_format, order = item.cell.split("|")
    disputed_codes = [code for _, code in item.fields]
    meta = dict(domain=item.domain, key=item.key, thing=item.more["n"], cell=item.cell, bad=item.more["bad"],
                own_values=json.dumps([item.values["own"][code] for code in disputed_codes]),
                person_values=json.dumps([item.values["a"][code] for code in disputed_codes]))
    jobs = []
    if (record_format, order) == ("unlabelled", "person_first"):
        for owner, label in (("a", "PERSON"), ("own", "own")):
            layout = prompts.layout(engine.tok, own_prompt(item, owner), engine.kw, prefix="", owners=["own"])
            jobs.append((dict(meta, condition=f"reference: {label} record alone"), Job(layout.ids)))
    for condition in conditions:
        layout = prompts.layout(engine.tok, reason_prompt(item, cue=condition == "cue"), engine.kw, prefix="",
                                owners=item.owners)
        knock = None
        if condition.startswith("bar:"):
            head_set = condition.split(":", 1)[1]
            if head_set not in head_sets:
                continue
            own_record = layout.block("own")
            barred_tokens = own_record.line_tokens_all()
            if head_set == "all":
                barred_tokens = own_record.header + own_record.line_tokens_all() + own_record.extra
            knock = (layout.n_ctx, circuit.knock_of(head_sets[head_set], barred_tokens))
        jobs.append((dict(meta, condition=condition), Job(layout.ids, knock=knock)))
    return jobs


def reason_rows(meta: dict, replies: list[dict]) -> list[dict]:
    """One row per sampled reply, with whether its reasoning quotes a disputed value of each record."""
    own_values, person_values = json.loads(meta["own_values"]), json.loads(meta["person_values"])
    rows = []
    for sample, reply in enumerate(replies):
        reasoning = reply["text"].split('{"probability"')[0]
        probability = stated_probability(reply["text"])
        rows.append(dict(meta, sample=sample, text=reply["text"], n_tokens=reply["n_tokens"],
                         truncated=reply["truncated"],
                         value=float("nan") if probability is None else probability,
                         cites_own=any(mentions(reasoning, value) for value in own_values),
                         cites_person=any(mentions(reasoning, value) for value in person_values)))
    return rows


def stage_program_reason(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Does the leak appear on invented records, and does the program's intervention remove it?

    Each invented thing is sampled with reasoning in the configured cells ("<format>|<order>") under the
    conditions of ``reason_jobs``. Writes ``program_reason/<domain><n>.parquet``, one row per reply."""
    settings = cfg["program_reason"]
    engine.cast(settings["dtype"])
    head_sets = named_sets(run_dir, engine, cfg)
    items = invented(int(settings["seed"]), int(settings["per_domain"]), three=False)
    domains = dict.fromkeys(item.domain for item in items)  # generator order determines the sampling seed
    thing_numbers = sorted({item.more["n"] for item in items})
    out_dir = run_dir / "program_reason"
    seed = int(config.load()["seed"])
    conditions_of_cell = {tuple(cell.split("|")): conditions for cell, conditions in settings["cells"]}
    for (domain_index, domain), thing_number in itertools.product(enumerate(domains), thing_numbers):
        path = out_dir / f"{domain}{thing_number}.parquet"
        if path.exists():
            continue
        started, jobs = time.time(), []
        for item in items:
            if item.domain == domain and item.more["n"] == thing_number:
                conditions = conditions_of_cell.get(tuple(item.cell.split("|")), [])
                jobs += reason_jobs(engine, item, conditions, head_sets)
        thing_seed = seed + 1000 * domain_index + thing_number
        replies = engine.generate([job for _, job in jobs], int(settings["samples"]),
                                  int(settings["max_new_tokens"]), thing_seed, int(settings["batch"]))
        rows = []
        for (meta, _), job_replies in zip(jobs, replies):
            rows += reason_rows(meta, job_replies)
        write_rows(path, rows)
        print(f"[program reason] {domain} {thing_number}: {len(rows)} answers, {time.time() - started:.1f}s",
              flush=True)


def leak(mixed_gap: np.ndarray, alone_gap: np.ndarray) -> float:
    """The leak on invented records: 0 if the answers follow PERSON's record, 1 if the model's,
    0.5 if neither.

    ``mixed_gap``: per thing, the answer about PERSON when PERSON's record is the bad one minus when it is
    the good one (the model's record then holds the opposite). ``alone_gap``: the bad record answered
    alone minus the good one. Answers that follow PERSON's record reproduce ``alone_gap``; answers that
    follow the model's reverse it."""
    return float((1 - (mixed_gap * alone_gap).sum() / (alone_gap * alone_gap).sum()) / 2)


def program_reason_tables(run_dir: Path, draws: int = 2000) -> dict:
    """The leak per cell and condition of ``stage_program_reason`` (bootstrap interval over things), the
    share of replies quoting each record, and the reference gap between the two records alone."""
    replies = read_rows(run_dir / "program_reason")
    if replies.empty:
        return {}
    replies["logit"] = clip_logit(replies.value.fillna(0.5))
    replies.loc[replies.value.isna(), "logit"] = np.nan
    replies["thing_id"] = replies.domain + replies.thing.astype(str)
    is_reference = replies.condition.str.startswith("reference")
    reference = replies[is_reference]
    # whether the record answered alone is the bad one: PERSON's record ("a") or the model's
    persons_record_alone = reference.condition.str.contains("PERSON")
    bad_alone = np.where(persons_record_alone, reference.bad == "a", reference.bad == "own")
    reference = reference.assign(bad_alone=bad_alone)
    alone = reference.groupby(["thing_id", "bad_alone"]).logit.mean().unstack()
    alone_gap = (alone[True] - alone[False]).dropna()      # the bad record alone minus the good record alone
    rng, rows = np.random.default_rng(0), []
    for (cell, condition), group in replies[~is_reference].groupby(["cell", "condition"]):
        by_bad_owner = group.groupby(["thing_id", "bad"]).logit.mean().unstack()
        if not {"a", "own"} <= set(by_bad_owner.columns):
            continue
        # PERSON's record the bad one minus PERSON's record the good one
        mixed_gap = (by_bad_owner["a"] - by_bad_owner["own"]).dropna()
        things = sorted(set(mixed_gap.index) & set(alone_gap.index))
        if len(things) < 4:
            continue
        mixed, alone_values = mixed_gap.loc[things].to_numpy(), alone_gap.loc[things].to_numpy()
        resamples = rng.integers(0, len(things), (draws, len(things)))
        boot = [leak(mixed[resample], alone_values[resample]) for resample in resamples]
        own_only = (group.cites_own & ~group.cites_person).mean()
        rows.append(dict(cell=cell, condition=condition, things=len(things), leak=leak(mixed, alone_values),
                         lo=np.percentile(boot, 2.5), hi=np.percentile(boot, 97.5), cites_own_only=own_only,
                         cites_both=(group.cites_own & group.cites_person).mean(),
                         cites_person_only=(group.cites_person & ~group.cites_own).mean(),
                         parsed=group.value.notna().mean(), truncated=group.truncated.mean(),
                         tokens=group.n_tokens.median()))
    reference_summary = pd.DataFrame(dict(things=[len(alone_gap)], mean_gap_logits=[alone_gap.mean()],
                                          share_positive=[(alone_gap > 0).mean()]))
    return {"program_reason": pd.DataFrame(rows), "program_reason_reference": reference_summary}


def tables(run_dir: Path, cfg: dict) -> dict:
    out = {}
    out |= program_tables(run_dir)
    out |= program_reason_tables(run_dir)
    out |= reply_headers_tables(run_dir)
    return out


# Stage: the read redirected by rewriting the cached values (no token of the reply is touched)
def value_heads_by_layer(heads: list[tuple[int, int]], heads_per_value_head: int) -> dict:
    """{layer: the value heads a set of attention heads uses}. Qwen3 shares each key and value projection
    among ``heads_per_value_head`` attention heads, so a value vector can only be replaced for all of them."""
    by_layer = {}
    for layer, head in heads:
        by_layer.setdefault(layer, set()).add(head // heads_per_value_head)
    return by_layer


def other_value_heads(used_by_layer: dict, n_value_heads: int, rng) -> dict:
    """A control: per layer, as many value heads as ``used_by_layer`` has there, none of them among those."""
    by_layer = {}
    for layer, used in used_by_layer.items():
        free = [value_head for value_head in range(n_value_heads) if value_head not in used]
        if free:
            drawn = rng.choice(free, size=min(len(used), len(free)), replace=False)
            by_layer[layer] = set(int(value_head) for value_head in drawn)
    return by_layer


def redirect_sets(engine, set_names: list[str], head_sets: dict, rng) -> tuple[dict, dict]:
    """For each configured set that exists: (name -> {layer: columns of the value projection's output},
    name -> {layer: value heads}). A name "other value heads than <set> <i>" is the control for ``<set>``."""
    import torch
    heads_per_value_head = engine.n_heads // engine.lm.config.num_key_value_heads
    n_value_heads = engine.n_heads // heads_per_value_head
    width = engine.head_dim
    columns, value_heads = {}, {}
    for name in set_names:
        if name.startswith("other value heads than "):
            reference_set = name.split(" than ")[1].rsplit(" ", 1)[0]
            used = value_heads_by_layer(head_sets[reference_set], heads_per_value_head)
            by_layer = other_value_heads(used, n_value_heads, rng)
        elif name in head_sets:
            by_layer = value_heads_by_layer(head_sets[name], heads_per_value_head)
        else:
            continue
        value_heads[name] = {int(layer): sorted(chosen) for layer, chosen in by_layer.items()}
        columns[name] = {
            layer: torch.cat([torch.arange(value_head * width, (value_head + 1) * width)
                              for value_head in sorted(chosen)]).to(engine.device)
            for layer, chosen in by_layer.items()}
    return columns, value_heads


def matching_line_tokens(layout) -> tuple[list[int], list[int]]:
    """Tokens of the model's record's lines, and the tokens of PERSON's record that hold the same part
    (name, value, line end) of the same field, pairwise. Parts with different token counts in the two
    records are left out."""
    own_record, person_record = layout.block("own"), layout.block("person")
    own_tokens, person_tokens = [], []
    for code, own_line in own_record.lines.items():
        if code not in person_record.lines:
            continue
        for part in prompts.TOKROLES:
            own_part, person_part = getattr(own_line, part), getattr(person_record.lines[code], part)
            if len(own_part) == len(person_part):
                own_tokens += own_part
                person_tokens += person_part
    return own_tokens, person_tokens


def reasoned_rows(meta: dict, replies: list[dict]) -> list[dict]:
    """One row per sampled reply, in the format of the reasoning stage."""
    return reply_rows(meta, replies)


def stage_redirect(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Remove the leak by changing what the lookup heads copy, with attention left alone.

    In the leak cell (unlabelled, PERSON's record first) the value vector of every line token of the
    model's record is replaced, once and before the reply, by that of the matching token of PERSON's
    record, in the value heads of a head set. A head that lands on the model's line for a field then
    copies what PERSON's line holds. Writes sampled replies in the reasoning stage's format to
    ``reason_redirect`` (or ``folder``) and the value heads used to ``circuit_redirect/``."""
    from steering import run
    settings = cfg["circuit_redirect"]
    engine.cast(settings["dtype"])
    plain_arm = cfg["pairs"]["false"][0]
    head_sets = named_sets(run_dir, engine, cfg)
    rng = np.random.default_rng(int(cfg["circuit"]["seed"]) + 51)
    value_columns, value_heads = redirect_sets(engine, settings["sets"], head_sets, rng)
    folder_name = settings.get("folder", "reason_redirect")
    (run_dir / "circuit_redirect").mkdir(exist_ok=True)
    (run_dir / "circuit_redirect" / f"value_heads_{folder_name}.json").write_text(json.dumps(
        {name: dict(value_heads=sum(len(chosen) for chosen in by_layer.values()), by_block=by_layer)
         for name, by_layer in value_heads.items()}))
    folder, seed, group_size = run_dir / folder_name, int(config.load()["seed"]), int(settings["group"])
    patients = sorted(cases.index[cases == cfg["intervene_split"]])[: int(settings["patients"])]
    todo = [case for case in patients if not (folder / f"{case}.parquet").exists()]
    for start in range(0, len(todo), group_size):
        started, group, jobs = time.time(), todo[start:start + group_size], []
        for case in group:
            prompt_row = run.case_prompts(case, cases, [plain_arm])[(plain_arm, "person_first", "C")]
            layout = prompts.layout(engine.tok, prompt_row["prompt"], engine.kw, prefix="")
            own_tokens, person_tokens = matching_line_tokens(layout)
            _, _, captured = engine.run([layout.ids[:layout.n_ctx]], capture=[person_tokens])
            person_stream = captured[0][1]           # [layers + 1, tokens, hidden] at PERSON's line tokens
            for name, columns in value_columns.items():
                meta = run.meta_of(prompt_row, part="reason", variant=f"redirect: {name}", kind="redirect")
                # at the model's line tokens, these value columns are computed from PERSON's residual stream
                redirect = (own_tokens, person_stream, "v", None, columns)
                jobs.append((case, meta, Job(layout.ids, paths=[redirect])))
        group_seed = seed + sum(map(ord, group[0]))
        replies = engine.generate([job for _, _, job in jobs], int(settings["samples"]),
                                  int(settings["max_new_tokens"]), group_seed, int(settings["batch"]))
        rows = {case: [] for case in group}
        for (case, meta, _), job_replies in zip(jobs, replies):
            rows[case] += reasoned_rows(meta, job_replies)
        for case in group:
            write_rows(folder / f"{case}.parquet", rows[case])
        n_answers = sum(map(len, rows.values()))
        print(f"[redirect] {min(start + group_size, len(todo))}/{len(todo)} patients, {n_answers} answers, "
              f"{time.time() - started:.1f}s", flush=True)


def runs_of_equal(first: int, last: int, label) -> list[tuple[int, int]]:
    """Split the token positions first..last (inclusive) into consecutive runs (start, end) over which
    ``label(position)`` is constant, so that each run can go through the model as one chunk."""
    runs, start = [], first
    while start <= last:
        end = start
        while end + 1 <= last and label(end + 1) == label(start):
            end += 1
        runs.append((start, end))
        start = end + 1
    return runs


def run_hidden(engine, ids: list[int], hidden, upto: int):
    """The state after ``ids[:upto]`` when no other token may attend (any head, any block) to the tokens in
    ``hidden``. ``Engine.run`` cannot bar tokens within one chunk, so the tokens go in chunks that change
    wherever a hidden token follows a visible one or the reverse: a visible chunk is barred from the hidden
    tokens before it; a hidden chunk is seen by nothing after it."""
    hidden = {position for position in hidden if position < upto}
    every_head = list(range(engine.n_heads))
    state = None
    for start, end in runs_of_equal(0, upto - 1, lambda position: position in hidden):
        chunk = ids[start:end + 1]
        if state is None:
            _, state, _ = engine.run([chunk])
            continue
        hidden_before = sorted(position for position in hidden if position < start)
        knock = None
        if hidden_before and start not in hidden:
            knock = [{layer: (every_head, hidden_before) for layer in range(engine.n_layers)}]
        _, state, _ = engine.run([chunk], state=state, knock=knock)
    return state


# Stage: which tokens of a reply bring the headers' owner information to the value being written
RECORD_MENTION = re.compile(
    r"Record\s+[A-Z]\b|[Yy]our\s+(?:own\s+)?record|[Mm]y\s+(?:own\s+)?record|user['’]?s\s+record|"
    r"both\s+records|either\s+record|"
    r"(?:first|second|earlier|later|original|updated|other|previous|current)\s+record", re.S)
REPLY_HEADERS_FOLDERS = (("circuit_reply_headers", "reply_headers"),
                         ("circuit_reply_headers_cross", "reply_headers_cross"),
                         ("circuit_reply_headers_layout", "reply_headers_layout"),
                         ("circuit_reply_headers_lines", "reply_headers_lines"))


class Barring(NamedTuple):
    """One condition of the reply-headers stage: tokens that may not attend to the records' headers."""
    name: str
    records_too: bool          # the records' own tokens are barred from the headers as well (in every head)
    later_tokens: set          # the positions after the records that are barred
    heads: list                # the (layer, head) in which ``later_tokens`` are barred


class ValueTokens(NamedTuple):
    """The two tokens a reply could write next: the model's record's value and PERSON's."""
    own: int
    person: int


@dataclass
class ReplyTokens:
    """The token positions of one reply, fed back up to the token that writes its first disputed value."""
    headers: list[int]         # both records' headers and end markers
    question: set              # from the end of the records to the end of the prompt
    reply: set                 # the reply so far, the writing token included
    naming: set                # reply tokens that name a record or an owner (never the writing token)
    writing: int               # the last position: its next token is the disputed value


@dataclass
class HeaderHeads:
    """The head sets in which tokens are barred from the headers."""
    every: list
    owner: list                # the heads found to read a record's owner from its header
    random: list               # as many heads per layer as ``owner``, drawn at random


def header_token_positions(layout) -> list[int]:
    """Every record's header and end-marker tokens: where a prompt says whose record it is."""
    return [position for block in layout.blocks for position in block.header + block.extra]


def naming_token_positions(engine, reply_ids: list[int], first_position: int, person_name: str | None) -> set:
    """Positions of the reply tokens that are part of a mention of a record ("Record X", "your record", "the
    first record") or of PERSON's name. ``reply_ids`` starts at position ``first_position``."""
    pieces = [engine.tok.decode([token]) for token in reply_ids]
    text = "".join(pieces)
    piece_start = np.cumsum([0] + [len(piece) for piece in pieces])     # each token's character offset
    mentions_at = [match.span() for match in RECORD_MENTION.finditer(text)]
    if person_name:
        mentions_at += [match.span() for match in re.finditer(rf"\b{person_name}\b", text)]
    return {first_position + i for i in range(len(pieces)) for start, end in mentions_at
            if piece_start[i] < end and piece_start[i + 1] > start}


def barring_conditions(tokens: ReplyTokens, heads: HeaderHeads, ends_only: bool) -> list[Barring]:
    """The conditions run on one reply. ``ends_only`` keeps the four that bar nothing or everything."""
    later = tokens.question | tokens.reply
    barrings = [
        Barring("nothing barred", False, set(), heads.every),
        Barring("every later token", False, later, heads.every),
        Barring("every later token, the owner heads only", False, later, heads.owner),
        Barring("every later token, random heads", False, later, heads.random),
        Barring("the question's tokens only", False, tokens.question, heads.every),
        Barring("the reply's tokens only", False, tokens.reply, heads.every),
        Barring("the reply's tokens that name a record or an owner", False, tokens.naming, heads.every),
        Barring("the reply's other tokens", False, tokens.reply - tokens.naming, heads.every),
        Barring("the writing token only", False, {tokens.writing}, heads.every),
        Barring("the records' own tokens", True, set(), heads.every),
        Barring("the records' own tokens and every later token", True, later, heads.every)]
    if ends_only:
        return [barrings[0], barrings[1], barrings[-2], barrings[-1]]
    return barrings


def final_logits(engine, ids: list[int], n_ctx: int, records_state, barred: set, knocks: list[dict]):
    """Logits after the last token of ``ids``, one row per entry of ``knocks``. The tokens from ``n_ctx`` on
    are run in chunks; in a chunk of ``barred`` tokens, row i is under ``knocks[i]``. All rows share the
    records."""
    count, last = len(knocks), len(ids) - 1
    state = records_state.select([0] * count)
    for start, end in runs_of_equal(n_ctx, last, lambda position: position in barred):
        chunk = [ids[start:end + 1]] * count
        chunk_knock = knocks if start in barred else None
        if end == last:
            logits, _, _ = engine.run(chunk, state=state, keep=False, knock=chunk_knock)
        else:
            _, state, _ = engine.run(chunk, state=state, knock=chunk_knock)
    return logits


def value_margins_by_condition(engine, ids: list[int], n_ctx: int, header_tokens: list[int],
                               barrings: list[Barring], values: ValueTokens) -> dict:
    """condition name -> logit of the model's value minus PERSON's after the last token of ``ids``.

    The records are run once without and once with their own tokens barred from the headers; barrings that bar
    the same later tokens (in different heads) share their forward passes as rows of one batch."""
    margins = {}
    for records_too in (False, True):
        chosen = [barring for barring in barrings if barring.records_too == records_too]
        if not chosen:
            continue
        if records_too:
            records_state = run_hidden(engine, ids, header_tokens, n_ctx)
        else:
            _, records_state, _ = engine.run([ids[:n_ctx]])
        by_barred_tokens = {}
        for barring in chosen:
            by_barred_tokens.setdefault(tuple(sorted(barring.later_tokens)), []).append(barring)
        for barred_tokens, members in by_barred_tokens.items():
            knocks = [circuit.knock_of(member.heads, header_tokens) for member in members]
            logits = final_logits(engine, ids, n_ctx, records_state, set(barred_tokens), knocks)
            for member, row in zip(members, logits):
                margins[member.name] = float(row[values.own] - row[values.person])
    return margins


def other_arm_margins(engine, ids: list[int], n_ctx: int, other_arm_layout, barrings: list[Barring],
                      values: ValueTokens) -> dict:
    """The same question and reply after the other arm's records (labelled for unlabelled and the reverse),
    under the same barrings moved to the new positions."""
    shift = other_arm_layout.n_ctx - n_ctx
    moved = []
    for barring in barrings:
        moved.append(barring._replace(later_tokens={position + shift for position in barring.later_tokens}))
    other_ids = other_arm_layout.ids[:other_arm_layout.n_ctx] + ids[n_ctx:]
    headers = header_token_positions(other_arm_layout)
    return value_margins_by_condition(engine, other_ids, other_arm_layout.n_ctx, headers, moved, values)


def with_filler(ids: list[int], places: list[int], filler: int, count: int):
    """``ids`` with ``count`` filler tokens inserted before each position in ``places``: (the new ids, the
    fillers' positions, old position -> new position)."""
    padded, added, new_position = [], [], {}
    for position, token in enumerate(ids):
        if position in places:
            added += list(range(len(padded), len(padded) + count))
            padded += [filler] * count
        new_position[position] = len(padded)
        padded.append(token)
    return padded, added, new_position


def layout_margins(engine, ids: list[int], layout, tokens: ReplyTokens, everything_barred: Barring,
                   every_head: list, n_added: int, values: ValueTokens) -> dict:
    """With nothing of the headers reaching any token, only their place on the page is left of them. Two
    checks: the hidden tokens replaced by a filler token (must change nothing), and hidden filler tokens added
    where the labelled prompt has end markers: between the records, before the question, or both."""
    filler = engine.tok(" the", add_special_tokens=False)["input_ids"][0]
    n_ctx = layout.n_ctx
    margins = {}
    replaced = list(ids)
    for position in tokens.headers:
        replaced[position] = filler
    result = value_margins_by_condition(engine, replaced, n_ctx, tokens.headers, [everything_barred], values)
    for name, margin in result.items():
        margins["hidden tokens replaced by a filler: " + name] = margin
    first_record = layout.blocks[0]
    after_first_record = max(first_record.header + first_record.line_tokens_all() + first_record.extra) + 1
    insertions = (("before the question", [n_ctx]), ("between the records", [after_first_record]),
                  ("in both places", [after_first_record, n_ctx]))
    for where, places in insertions:
        padded, added, new_position = with_filler(ids, places, filler, n_added)
        later_tokens = {new_position[position] for position in everything_barred.later_tokens}
        barring = Barring(everything_barred.name, True, later_tokens, every_head)
        hidden = [new_position[position] for position in tokens.headers] + added
        result = value_margins_by_condition(engine, padded, new_position[n_ctx], hidden, [barring], values)
        for name, margin in result.items():
            margins[f"{n_added} hidden tokens added {where}: " + name] = margin
    return margins


def header_attention(engine, ids: list[int], n_ctx: int, tokens: ReplyTokens, owner_heads: list) -> list:
    """Attention to the headers from each class of later token: the mean over the owner heads and over every
    head. One dict(tokens_from, tokens, owner_heads, every_head) per class that has tokens."""
    classes = {"the question": sorted(tokens.question),
               "reply tokens naming a record or an owner": sorted(tokens.naming),
               "other reply tokens": sorted(tokens.reply - tokens.naming - {tokens.writing}),
               "the writing token": [tokens.writing]}
    class_of = {position: name for name, positions in classes.items() for position in positions}
    owner_layers = [layer for layer, _ in owner_heads]
    owner_indices = [head for _, head in owner_heads]
    owner_sum = {name: 0.0 for name in classes}
    every_sum = {name: 0.0 for name in classes}
    n_tokens = {name: 0 for name in classes}
    _, state, _ = engine.run([ids[:n_ctx]])
    for start, end in runs_of_equal(n_ctx, tokens.writing, class_of.__getitem__):
        _, state, _ = engine.run([ids[start:end + 1]], state=state, groups=[{"headers": tokens.headers}])
        attention = engine.attention[0]["headers"][:, :, 1]      # [layers, heads], mean over the chunk
        name, length = class_of[start], end - start + 1
        owner_sum[name] += float(attention[owner_layers, owner_indices].mean()) * length
        every_sum[name] += float(attention.mean()) * length
        n_tokens[name] += length
    return [dict(tokens_from=name, tokens=n_tokens[name], owner_heads=owner_sum[name] / n_tokens[name],
                 every_head=every_sum[name] / n_tokens[name]) for name in classes if n_tokens[name]]


def classify_reply_tokens(engine, layout, prompt: str, ids: list[int]) -> ReplyTokens | None:
    """Classify the positions of a reply fed back up to its writing token; None if the reply has no token
    before the one that writes the value."""
    n_ctx, prompt_length, writing = layout.n_ctx, len(layout.ids), len(ids) - 1
    if writing <= prompt_length:
        return None
    asked_person = re.search(r"What probability does (\w+) assign", prompt)
    person_name = asked_person.group(1) if asked_person else None
    naming = naming_token_positions(engine, ids[prompt_length:writing + 1], prompt_length, person_name)
    naming -= {writing}
    return ReplyTokens(headers=header_token_positions(layout), question=set(range(n_ctx, prompt_length)),
                       reply=set(range(prompt_length, writing + 1)), naming=naming, writing=writing)


def stage_reply_headers(engine, run_dir: Path, cfg: dict, cases: pd.Series, load_vectors) -> None:
    """Which tokens bring the headers' owner information to the value a reply writes?

    Unedited replies of the false cells are fed back up to the token that writes their first disputed
    value. The readout is the logit of the model's value minus PERSON's there, with tokens barred from
    both records' headers and end markers as ``barring_conditions`` lists. Settings: ``replies`` per cell;
    ``cross`` keeps the four end conditions and adds the same reply after the other arm's records;
    ``layout`` (with ``cross``) adds ``layout_margins`` with that many filler tokens. Without ``cross``
    the owner heads' attention to the headers is measured too. Writes ``<folder>/rows.parquet`` (one row
    per reply and condition) and ``<folder>/attention.parquet``."""
    from steering import run
    settings = cfg["circuit_reply_headers"]
    out_dir = run_dir / settings.get("folder", "circuit_reply_headers")
    path = out_dir / "rows.parquet"
    if path.exists():
        return
    vectors = load_vectors(run_dir, engine.device)
    false_arms = cfg["pairs"]["false"]
    plain_arm, labelled_arm = false_arms
    rng = np.random.default_rng(int(cfg["circuit"]["seed"]) + 61)
    owner_heads = [tuple(head) for head in settings["owner_heads"]]
    random_heads = circuit.random_like(owner_heads, engine.n_heads, rng)
    every_head = [(layer, head) for layer in range(engine.n_layers) for head in range(engine.n_heads)]
    heads = HeaderHeads(every=every_head, owner=owner_heads, random=random_heads)
    cross, n_added = bool(settings.get("cross")), settings.get("layout")
    rows, attention_rows, used_per_cell, started = [], [], {}, time.time()
    probes = circuit.reply_probes(engine, run_dir, cfg, cases, vectors, settings["folders"],
                                  float(cfg["circuit"]["alpha"]), int(settings["max_tokens"]))
    for probe in probes:
        arm, order = probe["arm"], probe["block_order"]
        if used_per_cell.get((arm, order), 0) >= int(settings["replies"]):
            continue
        cell_prompts = run.case_prompts(probe["case_id"], cases, false_arms)
        prompt = cell_prompts[(arm, order, "C")]["prompt"]
        layout = prompts.layout(engine.tok, prompt, engine.kw, prefix="")
        ids, n_ctx = probe["ids"], layout.n_ctx
        tokens = classify_reply_tokens(engine, layout, prompt, ids)
        if tokens is None:
            continue
        used_per_cell[(arm, order)] = used_per_cell.get((arm, order), 0) + 1
        barrings = barring_conditions(tokens, heads, ends_only=cross)
        values = ValueTokens(own=probe["own_tok"], person=probe["person_tok"])
        margins = value_margins_by_condition(engine, ids, n_ctx, tokens.headers, barrings, values)
        if cross and probe["twin"] is not None:
            other_arm = labelled_arm if arm == plain_arm else plain_arm
            other_prompt = cell_prompts[(other_arm, order, "C")]["prompt"]
            other_arm_layout = prompts.layout(engine.tok, other_prompt, engine.kw, prefix="")
            other = other_arm_margins(engine, ids, n_ctx, other_arm_layout, barrings, values)
            margins |= {"the other arm's records: " + name: margin for name, margin in other.items()}
        if cross and n_added:
            margins |= layout_margins(engine, ids, layout, tokens, everything_barred=barrings[-1],
                                      every_head=every_head, n_added=int(n_added), values=values)
        reply_key = dict(case_id=probe["case_id"], arm=arm, block_order=order, sample=probe["sample"])
        if not cross:
            for measured in header_attention(engine, ids, n_ctx, tokens, owner_heads):
                attention_rows.append(reply_key | measured)
        reply_length = len(tokens.reply)
        for condition, margin in margins.items():
            rows.append(reply_key | dict(field=probe["field"], wrote=probe["wrote"], condition=condition,
                                         margin=margin, base=margins["nothing barred"],
                                         reply_tokens=reply_length, naming_tokens=len(tokens.naming)))
        used = sum(used_per_cell.values())
        if used % 20 == 0:
            per_cell = {f"{cell[0]}|{cell[1]}": count for cell, count in used_per_cell.items()}
            print(f"[reply headers] {used} replies {per_cell}, {time.time() - started:.1f}s", flush=True)
    out_dir.mkdir(exist_ok=True)
    write_rows(path, rows)
    write_rows(out_dir / "attention.parquet", attention_rows)
    print(f"[reply headers] {sum(used_per_cell.values())} replies, {len(rows)} rows, "
          f"{time.time() - started:.1f}s", flush=True)


def reply_headers_tables(run_dir: Path) -> dict:
    """The summary tables of every reply-headers run that exists, each renamed after its folder."""
    out = {}
    for folder, tag in REPLY_HEADERS_FOLDERS:
        tables_of_run = reply_headers_tables_of(run_dir / folder)
        out |= {name.replace("reply_headers", tag, 1): table for name, table in tables_of_run.items()}
    return out


def reply_headers_tables_of(folder: Path) -> dict:
    """Per cell and condition of one reply-headers run: how often the model's value is favoured, the mean
    margin and its shift from the unbarred margin; and the attention to the headers by class of token, where
    it was measured."""
    rows_path = folder / "rows.parquet"
    if not rows_path.exists():
        return {}
    rows = pd.read_parquet(rows_path)
    if rows.empty:
        return {}
    rows["own"] = rows.margin > 0
    rows["shift"] = rows.margin - rows.base
    out = {"reply_headers": rows.groupby(["arm", "block_order", "condition"]).agg(
        replies=("own", "size"), models_value_favoured=("own", "mean"), margin=("margin", "mean"),
        shift=("shift", "mean"), naming_tokens=("naming_tokens", "mean")).reset_index()}
    attention_path = folder / "attention.parquet"
    if not attention_path.exists():
        return out
    attention = pd.read_parquet(attention_path)
    if not attention.empty:
        out["reply_headers_attention"] = attention.groupby(["arm", "block_order", "tokens_from"]).agg(
            replies=("tokens", "size"), tokens=("tokens", "mean"), owner_heads=("owner_heads", "mean"),
            every_head=("every_head", "mean")).reset_index()
    return out
