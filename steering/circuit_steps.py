"""Tests of single steps of the record lookup: how a record gets its place, where the asked owner becomes a
place, and what the reader heads match on.

    stage_tag     How a record gets its place. The second record is read without sight of the first; if
                  "second" means "a record came before me", it should then pass for a first record.
    stage_phrase  Where the owner named in the question becomes a place: the tokens of the question's
                  phrase are taken from the other page order, or barred from the records.
    stage_match   What the reader heads match on. A reader's attention is a score between the answer
                  token's *query* and each record token's *key*. The account says the query carries the
                  place of the asked record and the keys carry each line's place tag, and the score is
                  high where they agree. Each side is exchanged alone, in the readers' blocks only, with
                  the residual stream untouched:

        query   the answer token's queries computed from another run's state: the other question's answer
                token, or the lookup's own state moved along the fitted place vector (owner and random
                vectors: controls)
        key     the record lines' keys computed from the same two records in the other page order (each
                line then carries the other place's tag, on natural activations), or from the records
                under the slot edit turned to the other record (values instead of keys, or keys in the
                conversion blocks: controls)
        both    query and keys exchanged together: the pointer and the address are both turned, so they
                agree again and the lookup should return to its own answer

A lookup's result is its margin: the logit of PERSON's value minus the model's at the answer token. ``base``
is the margin of the lookup as asked, ``donor`` that of the same lookup about the other record, ``patched``
the margin under an intervention.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from belief_leakage import binding
from steering import circuit
from steering.circuit import OTHER_QUERY, bar_blocks, value_margins
from steering import program
from steering import prompts
from steering.engine import Edit
from steering.interventions import OTHER_ORDER
from steering.results import each_patient, held_out_patients, load_vectors, read_rows, unfinished, write_rows

# the record exchanges that are also run together with every query exchange
WITH_QUERY_EXCHANGES = ("none", "keys, readers' blocks", "keys, readers' blocks, other page order")


def owners_exchanged(prompt: str) -> str:
    """The prompt with the two records' headers (and end markers) exchanged and every field line left where it
    is: each record's contents and place stay, its owner becomes the other one."""
    parsed = binding.parse(prompt)
    if len(parsed.blocks) != 2:
        raise ValueError("two record blocks expected")
    # per record: the lines that are not field lines (the header first, then any end marker), and the field
    # lines
    frames = [[line for line in lines if not binding.FACT.match(line)] for _, lines in parsed.blocks]
    bodies = [[line for line in lines if binding.FACT.match(line)] for _, lines in parsed.blocks]
    exchanged = []
    for place in (0, 1):
        other_owner, other_frame = parsed.blocks[1 - place][0], frames[1 - place]
        exchanged.append((other_owner, [other_frame[0]] + bodies[place] + other_frame[1:]))
    parsed.blocks = exchanged
    return binding.render(parsed)


def by_slot(layout_a: prompts.Layout, layout_b: prompts.Layout) -> tuple[list[int], list[int]]:
    """Positions in the two layouts of the same field-line tokens, matching records by place on the page."""
    positions_a, positions_b = [], []
    for block_a, block_b in zip(layout_a.blocks, layout_b.blocks):
        matched_a, matched_b, _ = prompts.matching_line_tokens(block_a.lines, block_b.lines)
        positions_a += matched_a
        positions_b += matched_b
    return positions_a, positions_b


# What the reader heads match on
@dataclass
class MatchSetup:
    slot_vectors: dict
    place_vectors: dict                 # "place|last", "owner|last", "random|last": [layers + 1, hidden]
    reader_layers: list[int]            # the reader heads, as parallel lists of layer and head
    reader_heads: list[int]
    reader_window: tuple[int, int]      # the readers' blocks, counted from 0
    convert_window: tuple[int, int]     # the blocks where the owner becomes a place (control)
    scales: list                        # multiples of a vector to move the query by
    exchange_owners: bool               # also use the record lines' states under exchanged owners
    slot_alpha: float
    min_gap: float | None               # None: every lookup whose two questions give different margins


def clean_lookups(engine, pair: dict, ids: list[int], other_ids: list[int]):
    """The lookup and the same lookup about the other record, unedited: their margins and the residual stream
    of each one's answer token, [layers + 1, 1, hidden]."""
    n_ctx = pair["n_ctx"]
    _, state, _ = engine.run([ids[:n_ctx], other_ids[:n_ctx]])
    _, state, _ = engine.run([ids[n_ctx:-1], other_ids[n_ctx:-1]], state=state)
    logits, _, captured = engine.run([[ids[-1]], [other_ids[-1]]], state=state, keep=False,
                                     capture=[[len(ids) - 1], [len(other_ids) - 1]])
    base, donor = value_margins(logits, pair)
    return base, donor, captured[0][1], captured[1][1]


def query_exchanges(setup: MatchSetup, pair: dict, query: str, answer_position: list[int], own_state,
                    other_state) -> dict:
    """name -> the path entry that replaces the answer token's queries in the readers' blocks (None:
    unchanged). The states are the answer token's residual stream in the lookup itself and in the other
    question."""
    exchanges = {"none": None,
                 "the other question's": (answer_position, other_state, "q", setup.reader_window)}
    place_sign = float(circuit.place_sign(pair, query))
    owner_sign = float(circuit.sign(query))
    vectors = (("place vector", "place|last"), ("owner vector", "owner|last"),
               ("random vector", "random|last"))
    for name, key in vectors:
        sign = owner_sign if name == "owner vector" else place_sign
        vector = setup.place_vectors[key][:, None, :].to(own_state.dtype)      # [layers + 1, 1, hidden]
        for scale in setup.scales:
            moved = own_state - sign * float(scale) * vector
            exchanges[f"{name} x{scale:g}"] = (answer_position, moved, "q", setup.reader_window)
    return exchanges


def record_exchanges(engine, setup: MatchSetup, cells: dict, arm: str, order: str, query: str,
                     layout: prompts.Layout, record_ids: list[int]) -> dict:
    """name -> the path entry that replaces the record lines' keys or values (None: unchanged). The
    replacement states come from the same records under the slot edit, in the other page order, or with owners
    exchanged."""
    readers, conversion = setup.reader_window, setup.convert_window
    lines = layout.line_tokens()
    # the record lines under the slot edit turned to the other record
    slot_edit = circuit.slot_edit_away(layout, setup.slot_vectors, arm, query, setup.slot_alpha)
    _, _, captured = engine.run([record_ids], edits=[slot_edit], capture=[lines])
    edited = captured[0][1]
    exchanges = {"none": None,
                 "keys, readers' blocks": (lines, edited, "k", readers),
                 "values, readers' blocks": (lines, edited, "v", readers),
                 "keys, conversion blocks": (lines, edited, "k", conversion),
                 "keys and values, every block": (lines, edited, "kv")}

    swapped_prompt = cells.get((arm, OTHER_ORDER[order]))
    if swapped_prompt is not None:     # the same lines' states where the two records stand in the other order
        swapped_layout = prompts.layout(engine.tok, swapped_prompt, engine.kw, prefix="")
        here, there, _ = prompts.aligned(layout, swapped_layout)
        if here:
            _, _, captured = engine.run([swapped_layout.ids[:swapped_layout.n_ctx]], capture=[there])
            swapped = captured[0][1]
            exchanges["keys, readers' blocks, other page order"] = (here, swapped, "k", readers)
            exchanges["values, readers' blocks, other page order"] = (here, swapped, "v", readers)
            exchanges["keys, conversion blocks, other page order"] = (here, swapped, "k", conversion)
            exchanges["keys, every block, other page order"] = (here, swapped, "k")
            exchanges["values, conversion blocks, other page order"] = (here, swapped, "v", conversion)

    if setup.exchange_owners:           # the same lines' states where the two records' owners are exchanged
        exchanged_prompt = owners_exchanged(cells[(arm, order)])
        exchanged_layout = prompts.layout(engine.tok, exchanged_prompt, engine.kw, prefix="")
        here, there = by_slot(layout, exchanged_layout)
        _, _, captured = engine.run([exchanged_layout.ids[:exchanged_layout.n_ctx]], capture=[there])
        exchanged = captured[0][1]
        for what, kind in (("keys", "k"), ("values", "v")):
            exchanges[f"{what}, conversion blocks, owners exchanged"] = (here, exchanged, kind, conversion)
            exchanges[f"{what}, readers' blocks, owners exchanged"] = (here, exchanged, kind, readers)
        exchanges["keys and values, every block, owners exchanged"] = (here, exchanged, "kv")
    return exchanges


def match_rows(engine, setup: MatchSetup, cells: dict, case: str, arm: str, order: str, pair: dict,
               query: str) -> list[dict]:
    """One lookup under every exchange of the record lines' keys and of the answer token's queries: the margin
    and the readers' attention to the asked field's value in the asked record and in the other one."""
    other_owner = OTHER_QUERY[query]
    asked = pair["queries"][query]
    layout, ids, other_ids = asked["lay"], asked["ids"], pair["queries"][other_owner]["ids"]
    n_ctx = pair["n_ctx"]
    base, donor, own_state, other_state = clean_lookups(engine, pair, ids, other_ids)
    if not circuit.is_clean_flip(circuit.sign(query), base, donor, setup.min_gap):
        return []
    query_paths = query_exchanges(setup, pair, query, [len(ids) - 1], own_state, other_state)
    record_paths = record_exchanges(engine, setup, cells, arm, order, query, layout, ids[:n_ctx])

    # the records once per record exchange, then the question up to the answer token
    record_names = list(record_paths)
    paths = [[record_paths[name]] if record_paths[name] else [] for name in record_names]
    _, records_state, _ = engine.run([ids[:n_ctx]] * len(record_names), paths=paths)
    _, question_state, _ = engine.run([ids[n_ctx:-1]] * len(record_names), state=records_state)
    # the answer token: every record exchange with the query as it is, and every query exchange with the
    # records as they are and with their keys exchanged in the readers' blocks
    conditions = [(row, record_name, query_name)
                  for row, record_name in enumerate(record_names) for query_name in query_paths
                  if record_name in WITH_QUERY_EXCHANGES or query_name == "none"]
    value_tokens = {"asked": layout.block(query).lines[pair["code"]].val,
                    "other": layout.block(other_owner).lines[pair["code"]].val}
    answer_paths = [[query_paths[name]] if query_paths[name] else [] for _, _, name in conditions]
    logits = circuit.run_from(engine, question_state, [row for row, _, _ in conditions],
                              ([ids[-1]], dict(paths=answer_paths, groups=[value_tokens] * len(conditions))))
    patched = value_margins(logits, pair)

    meta = dict(case_id=case, arm=arm, block_order=order, field=pair["code"], query=query, base=base,
                donor=donor)
    readers = (setup.reader_layers, setup.reader_heads)
    rows = []
    for index, (_, record_name, query_name) in enumerate(conditions):
        # name -> [layers, heads, 2]: the attention from the last token, and its mean over the chunk's tokens
        attention = engine.attention[index]
        rows.append(dict(meta, records=record_name, queries=query_name, patched=patched[index],
                         att_asked=float(attention["asked"][readers[0], readers[1], 0].mean()),
                         att_other=float(attention["other"][readers[0], readers[1], 0].mean())))
    return rows


def run_patient_lookups(engine, run_dir, cfg, cases, stage, rows_for_pair):
    """Run the stage's lookup pairs for each unfinished patient, preserving their configured order."""
    folder = run_dir / stage / "rows"
    fields = int(cfg["circuit"]["fields"])

    def run_patient(case):
        rows = []
        cells = circuit.case_cells(case, cases, cfg)
        for arm, order, pair in circuit.pairs_of(engine, cells, fields):
            rows.extend(rows_for_pair(case, cells, arm, order, pair))
        write_rows(folder / f"{case}.parquet", rows)
        return f"patients, {len(rows)} runs"

    todo = unfinished(held_out_patients(cases, cfg, cfg[stage]), folder)
    each_patient(todo, stage.replace("_", " "), run_patient, counted=False)


def stage_match(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """The readers' match taken apart on held-out patients: each lookup with the record lines' keys (or
    values) and the answer token's queries exchanged, alone and together. Writes
    circuit_match/rows/<patient>.parquet."""
    settings, circuit_settings = cfg["circuit_match"], cfg["circuit"]
    readers = circuit.named_sets(run_dir, engine, cfg)["readers"]
    setup = MatchSetup(
        slot_vectors=load_vectors(run_dir, engine.device),
        place_vectors=circuit.load_place_vectors(engine, run_dir),
        reader_layers=[layer for layer, _ in readers], reader_heads=[head for _, head in readers],
        reader_window=tuple(settings["reader_window"]), convert_window=tuple(settings["convert_window"]),
        scales=settings["scales"], exchange_owners=bool(settings.get("owners")),
        slot_alpha=float(circuit_settings["alpha"]), min_gap=circuit_settings["min_gap"])

    def rows_for_pair(case, cells, arm, order, pair):
        for query in prompts.QUERIES:
            yield from match_rows(engine, setup, cells, case, arm, order, pair, query)

    run_patient_lookups(engine, run_dir, cfg, cases, "circuit_match", rows_for_pair)


def tables(run_dir: Path, cfg: dict) -> dict:
    """Per exchange: how far the margin moves toward the other record's answer, the share of lookups flipped,
    and the readers' attention to the asked field's value in each record."""
    rows = read_rows(run_dir / "circuit_match" / "rows")
    if rows.empty:
        return {}
    rows["moved"] = circuit.effect_of(rows.patched, rows.base, rows.donor)
    rows["flipped"] = circuit.flipped(rows)
    summary = rows.groupby(["records", "queries"]).agg(
        lookups=("moved", "size"), moved=("moved", "mean"), flipped=("flipped", "mean"),
        readers_on_asked=("att_asked", "mean"), readers_on_other=("att_other", "mean")).reset_index()
    return {"match": summary}


# Where the owner becomes a place
class Knockout(NamedTuple):
    """One barring condition of the phrase stage. A knockout maps a block to (heads, the keys they may not
    read)."""
    what: str
    window: str                     # the blocks barred, "first-last", counted from 1
    at_phrase: dict | None          # the knockout for the phrase's tokens and the question tokens before them
    after_phrase: dict | None       # the knockout for the tokens after the phrase


def phrase_from_other_order(engine, pair: dict, swapped_pair: dict | None, query: str, phrase: list[int],
                            before: list[int], settings: dict) -> list[tuple[dict, float]]:
    """The phrase's tokens given the residual stream they have, after one block, in the same question over the
    same records in the other page order: (condition, patched margin) per token group and block. A move to the
    other record's value means those tokens hold the record's place. Empty if the two prompts' questions do
    not line up token for token."""
    if swapped_pair is None:
        return []
    ids, n_ctx = pair["queries"][query]["ids"], pair["n_ctx"]
    swapped_ids = swapped_pair["queries"][query]["ids"]
    if swapped_pair["n_ctx"] != n_ctx or swapped_ids[n_ctx:] != ids[n_ctx:]:
        return []
    token_groups = {"the phrase": phrase,
                    "the phrase's last token": phrase[-1:],
                    "the phrase without its last token": phrase[:-1],
                    "the question up to the phrase's end": before + phrase}
    captured_positions = before + phrase
    _, records_state, _ = engine.run([swapped_ids[:n_ctx]])
    _, _, captured = engine.run([swapped_ids[n_ctx:]], state=records_state, keep=False,
                                capture=[captured_positions])
    donor_stream = captured[0][1]           # [layers + 1, captured positions, hidden]
    conditions = []
    for what, positions in token_groups.items():
        if not positions:
            continue
        columns = [captured_positions.index(position) for position in positions]
        for block in settings["blocks"]:
            patch = Edit("set", positions, donor_stream[:, columns], layers=(int(block), int(block)))
            conditions.append(dict(query=query, test="other page order", what=what, window=str(block),
                                   resid=[patch]))
    margins = circuit.run_items(engine, pair, conditions, int(settings["batch"]))
    return list(zip(conditions, margins))


def phrase_knockouts(layout: prompts.Layout, phrase: list[int], windows: list,
                     n_heads: int) -> list[Knockout]:
    """Per block window: the phrase's tokens, the tokens after it, or both barred from the records (whole,
    headers and end markers, field lines), and the tokens after the phrase barred from the phrase."""
    headers_and_ends = layout.header_tokens()
    lines = layout.line_tokens()
    record_parts = (("the whole records", headers_and_ends + lines),
                    ("headers and end markers", headers_and_ends),
                    ("field lines", lines))
    knockouts = []
    for first_block, last_block in windows:
        window = f"{first_block}-{last_block}"
        for part, keys in record_parts:
            barred = bar_blocks(n_heads, keys, first_block, last_block)
            knockouts += [Knockout(f"the phrase's tokens barred from {part}", window, barred, None),
                          Knockout(f"the tokens after the phrase barred from {part}", window, None, barred),
                          Knockout(f"every question token barred from {part}", window, barred, barred)]
        from_phrase = bar_blocks(n_heads, phrase, first_block, last_block)
        knockouts.append(Knockout("the tokens after the phrase barred from the phrase", window, None,
                                  from_phrase))
    return knockouts


def run_phrase_knockouts(engine, pair: dict, ids: list[int], phrase_end: int, records_state,
                         knockouts: list[Knockout]) -> list[float]:
    """The lookup's margin under each knockout: the question up to the phrase's end under one knockout, the
    rest of the question under the other."""
    at_phrase = dict(knock=[knockout.at_phrase for knockout in knockouts])
    after_phrase = dict(knock=[knockout.after_phrase for knockout in knockouts])
    logits = circuit.run_from(engine, records_state, [0] * len(knockouts),
                              (ids[pair["n_ctx"]:phrase_end], at_phrase), (ids[phrase_end:], after_phrase))
    return value_margins(logits, pair)


def phrase_rows(engine, settings: dict, case: str, arm: str, order: str, pair: dict,
                swapped_pair: dict | None, query: str) -> list[dict]:
    """One lookup with the phrase's tokens taken from the other page order, and under every knockout."""
    asked = pair["queries"][query]
    ids, layout, phrase_end, n_ctx = asked["ids"], asked["lay"], asked["cut"], pair["n_ctx"]
    other_ids = pair["queries"][OTHER_QUERY[query]]["ids"]
    _, records_state, _ = engine.run([ids[:n_ctx], other_ids[:n_ctx]])
    logits, _, _ = engine.run([ids[n_ctx:], other_ids[n_ctx:]], state=records_state, keep=False)
    base, donor = value_margins(logits, pair)
    meta = dict(case_id=case, arm=arm, block_order=order, field=pair["code"], query=query, base=base,
                donor=donor)
    phrase = list(range(pair["start"], phrase_end))
    before = list(range(n_ctx, pair["start"]))      # the question's tokens before the phrase

    rows = []
    from_other_order = phrase_from_other_order(engine, pair, swapped_pair, query, phrase, before, settings)
    for condition, margin in from_other_order:
        rows.append(dict(meta, test=condition["test"], what=condition["what"], window=condition["window"],
                         patched=margin))

    knockouts = phrase_knockouts(layout, phrase, settings["windows"], engine.n_heads)
    _, records_state, _ = engine.run([ids[:n_ctx]])
    run_batch = functools.partial(run_phrase_knockouts, engine, pair, ids, phrase_end, records_state)
    margins = engine._halving(knockouts, int(settings["batch"]), run_batch)
    for knockout, margin in zip(knockouts, margins):
        rows.append(dict(meta, test="barred", what=knockout.what, window=knockout.window, patched=margin))
    return rows


def lookup_in_other_order(engine, cells: dict, arm: str, order: str, pair: dict) -> dict | None:
    """The same field's lookup pair over the same records in the other page order, if that prompt disputes
    it."""
    swapped_prompt = cells.get((arm, OTHER_ORDER[order]))
    if not swapped_prompt:
        return None
    disputed = {code: (person_value, own_value)
                for code, person_value, own_value in prompts.diff_fields(swapped_prompt)}
    if pair["code"] not in disputed:
        return None
    return circuit.lookup_pair(engine, swapped_prompt, pair["code"], *disputed[pair["code"]])


def stage_phrase(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Is the owner turned into a place at the phrase's own tokens? The two questions differ in the phrase, so
    exchanging the questions cannot say. (a) The phrase tokens' residual stream after one block is taken from
    the same question over the same records in the other page order (a move to the other record's value means
    they hold the place). (b) In the conversion blocks, only the phrase's tokens, or only the tokens after it,
    are barred from the records, or from the headers and end markers. (c) The tokens after the phrase are
    barred from the phrase. Writes circuit_phrase/rows/<patient>.parquet."""
    def rows_for_pair(case, cells, arm, order, pair):
        swapped = lookup_in_other_order(engine, cells, arm, order, pair)
        for query in prompts.QUERIES:
            yield from phrase_rows(engine, cfg["circuit_phrase"], case, arm, order, pair, swapped, query)

    run_patient_lookups(engine, run_dir, cfg, cases, "circuit_phrase", rows_for_pair)


def phrase_tables(run_dir: Path, cfg: dict) -> dict:
    """Per phrase condition: how far the margin moves toward the other record's answer and the share of
    lookups flipped, over all lookups and by prompt format and page order."""
    rows = read_rows(run_dir / "circuit_phrase" / "rows")
    if rows.empty:
        return {}
    effects = circuit.effects(rows, cfg["circuit"]["min_gap"])
    effects["flipped"] = circuit.flipped(effects)
    effects["cell"] = circuit.cell_labels(effects, cfg)
    condition = ["test", "what", "window"]
    summary = effects.groupby(condition).agg(lookups=("effect", "size"), moved=("effect", "mean"),
                                             flipped=("flipped", "mean")).reset_index()
    by_cell = effects.pivot_table(index=condition, columns="cell", values="flipped").reset_index()
    return {"phrase": summary, "phrase_cells": by_cell}


# How a record gets its place
def tag_conditions(first_record: prompts.Block, windows: list, n_heads: int) -> list[tuple]:
    """(what the second record cannot see, block window, knockout): nothing, then per window the first record
    whole, its header and end marker, and its field lines."""
    key_sets = {"the first record": first_record.record_tokens(),
                "the first record's header and end marker": first_record.header + first_record.extra,
                "the first record's lines": first_record.line_tokens_all()}
    conditions = [("nothing", "", None)]
    for first_block, last_block in windows:
        for name, keys in key_sets.items():
            knockout = bar_blocks(n_heads, keys, first_block, last_block)
            conditions.append((name, f"{first_block}-{last_block}", knockout))
    return conditions


def tag_questions(engine, item, encoded: dict, layout: prompts.Layout) -> list[tuple]:
    """(kind, what is asked, token ids, the owner whose value is right): the lookup by owner for each record,
    and the lookup by place ("in the first / second record above") where its prompt shares the records'
    tokens."""
    n_ctx = encoded["n_ctx"]
    questions = [("owner", owner, encoded["ids"][("asked", owner)], owner) for owner in item.owners]
    for place, word in enumerate(("first", "second")):
        text = item.records + "\n\n" + program.ask(f"the {word} record above", item.fields[0])
        by_place = prompts.layout(engine.tok, text, engine.kw, prefix="", owners=item.owners)
        if by_place.n_ctx == n_ctx and by_place.ids[:n_ctx] == layout.ids[:n_ctx]:
            questions.append(("place", word, by_place.ids + encoded["prefix"], item.owners[place]))
    return questions


def tag_rows(engine, settings: dict, case: str, item) -> list[dict]:
    """One prompt's lookups with the second record read under each condition of ``tag_conditions``."""
    encoded = program.encode(engine, item)
    if encoded is None:
        return []
    layout = encoded["lays"][("asked", item.owners[0])]
    first_record, second_record = layout.blocks
    n_ctx, second_start, value_token = encoded["n_ctx"], second_record.header[0], encoded["tok"]
    conditions = tag_conditions(first_record, settings["windows"], engine.n_heads)
    questions = tag_questions(engine, item, encoded, layout)

    # the first record once; the second record once per condition; then each question reads both as usual
    record_ids = questions[0][2]
    count = len(conditions)
    _, first_state, _ = engine.run([record_ids[:second_start]])
    _, records_state, _ = engine.run([record_ids[second_start:n_ctx]] * count,
                                     state=first_state.select([0] * count),
                                     knock=[knockout for _, _, knockout in conditions])
    rows = []
    for kind, asked, ids, right_owner in questions:
        other_owner = next(owner for owner in item.owners if owner != right_owner)
        logits, _, _ = engine.run([ids[n_ctx:]] * count, state=records_state, keep=False)
        for (name, window, _), row_logits in zip(conditions, logits):
            margin = float(row_logits[value_token[right_owner]] - row_logits[value_token[other_owner]])
            rows.append(dict(case_id=case, cell=item.cell, question=kind, asked=asked,
                             asked_place=item.owners.index(right_owner) + 1, barred=name, window=window,
                             margin=margin))
    return rows


def stage_tag(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """The second record is read with every head of a block window barred from the first record's tokens; the
    question then reads both records as usual. Lookups by owner and by place ("in the first / second record
    above"). Writes circuit_tag/rows/<patient>.parquet; ``margin`` is positive when the asked record's value
    wins."""
    settings = cfg["circuit_tag"]
    rows_dir = run_dir / "circuit_tag" / "rows"
    for done, case in enumerate(unfinished(held_out_patients(cases, cfg, settings), rows_dir)):
        rows = []
        for item in program.ckd_items(case, cases, cfg):
            rows += tag_rows(engine, settings, case, item)
        write_rows(rows_dir / f"{case}.parquet", rows)
        print(f"[circuit tag] {done + 1} patients, {len(rows)} runs", flush=True)


def tag_tables(run_dir: Path, cfg: dict) -> dict:
    """The share of lookups answered from the asked record, per barring condition and kind of question, over
    all prompts and (unbarred against the first record barred) by prompt format and page order."""
    rows = read_rows(run_dir / "circuit_tag" / "rows")
    if rows.empty:
        return {}
    rows["right"] = rows.margin > 0
    rows["what"] = rows.question + ": the " + np.where(rows.asked_place == 1, "first", "second") + " record"
    overall = rows.pivot_table(index=["barred", "window"], columns="what", values="right").reset_index()
    whole_record = rows[rows.barred.isin(["nothing", "the first record"])]
    by_cell = whole_record.pivot_table(index=["cell", "barred", "window"], columns="what",
                                       values="right").reset_index()
    return {"tag": overall, "tag_cells": by_cell}
