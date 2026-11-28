"""Which attention heads carry a record lookup, and what each of them does.

The task is the lookup question about one field of one record ("In the record NAME was shown [In your own
record], what value is given for FIELD?"). The answer is read as a *margin*: the logit of PERSON's value minus
the logit of the model's own value at the answer token. Each lookup (the *base* run) is compared with a
*donor* run whose answer is the other record's value:

    query donor   the same records, the other question (the two differ only in the phrase naming the record)
    slot donor    the same question, with the records under the slot edit turned towards the other record

A patch copies part of the donor run into the base run. Its *effect* is the share of the way the margin moves
from the base answer to the donor's: effect = (patched - base) / (donor - base).

The records come before the question, so the two questions' runs share them exactly and every patch is made at
a question token.

Stages in this file:

    stage_circuit      every head scanned on fit patients and ranked; the top sets tested on held-out patients
    stage_reply        the same head sets at the token where a reasoned reply first writes a disputed value
    stage_fine         the smallest head set (greedy search), and how its heads depend on each other
    stage_algorithm    what each head attends to and writes; whether "whose record" is coded as owner or place
    stage_concrete     a test of each step of the lookup; the place vector; where labelling enters a reply
    stage_steer        sampled replies under the place vector, or with a head set blocked from a record
    stage_paired       sampled replies in which a head set reads other records than the rest of the model
    stage_references   the reference conditions (A, B, M, D) sampled with this sampler
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from belief_leakage import binding
from belief_leakage import config as experiment_config
from steering import interventions
from steering import prompts
from steering.transcripts import WritingPoint, first_written_value
from steering.engine import Edit, Job
from steering.results import (SIDE, case_prompts, each_patient, held_out_patients, load_vectors, meta_of,
                              patients_to_sample, read_rows as read, replies_with_prompts, sample_patients,
                              unedited_replies, unfinished, write_rows as write)

# The tokens the two lookup questions share after the phrase that names the record, in order: before the
# quoted field name, the field name, the rest of the question, the chat template's tail, and the answer
# position.
GROUPS = ("pre", "field", "post", "tail", "last")
OTHER_QUERY = {"person": "own", "own": "person"}
COUNTERFACTUALS = ("query", "slot")
LOOKUP_KEYS = ["case_id", "arm", "block_order", "field", "query"]      # one lookup


# ==== A lookup: its tokens, its runs and its donors

def lookup_pair(engine, prompt: str, code: str, person_value: str, own_value: str,
                preamble: str = "") -> dict | None:
    """The two lookup questions about one disputed field of a two-record prompt, tokenised.

    Returns None if the answer cannot be read at one token, or the two questions do not share their records
    and a long enough ending. Otherwise a dict:

        code       the field's code
        queries    per query ("person", "own"): ``ids`` (the prompt and the answer's shared first tokens),
                   ``lay`` (its layout), ``chat`` (its text) and ``cut`` (the first token after the phrase
                   naming the record)
        n_ctx      the number of tokens up to the end of the records, the same for both questions
        start      the first token at which the two questions differ: where the phrase begins
        shared     how many trailing tokens the two questions have in common
        groups     those shared tokens by group (``GROUPS``), as offsets from ``cut``
        tp, to     the answer token of PERSON's value and of the model's own value
    """
    queries, answer_tokens = {}, set()
    for query in prompts.QUERIES:
        text = preamble + prompts.lookup_prompt(prompt, code, query)
        chat = prompts.chat_text(engine.tok, text, engine.kw)
        answer = prompts.answer_ids(engine.tok, chat, (person_value, own_value))
        layout = prompts.layout(engine.tok, text, engine.kw, prefix="")
        if answer is None or answer[0][:len(layout.ids)] != layout.ids:
            return None
        queries[query] = dict(ids=answer[0], lay=layout, chat=chat)
        answer_tokens.add((answer[1], answer[2]))
    if len(answer_tokens) != 1:         # both questions must be read at the same two answer tokens
        return None
    person_token, own_token = answer_tokens.pop()

    person_ids, own_ids = queries["person"]["ids"], queries["own"]["ids"]
    n_ctx = queries["person"]["lay"].n_ctx
    if queries["own"]["lay"].n_ctx != n_ctx or person_ids[:n_ctx] != own_ids[:n_ctx]:
        return None
    start = next(i for i, (person_id, own_id) in enumerate(zip(person_ids, own_ids)) if person_id != own_id)
    shared = 0
    longest = min(len(person_ids), len(own_ids)) - start
    while shared < longest and person_ids[-1 - shared] == own_ids[-1 - shared]:
        shared += 1
    for one in queries.values():
        one["cut"] = len(one["ids"]) - shared

    groups = shared_token_groups(engine, queries["person"], code, shared)
    if groups is None:
        return None
    return dict(code=code, queries=queries, n_ctx=n_ctx, start=start, shared=shared, groups=groups,
                tp=person_token, to=own_token)


def shared_token_groups(engine, question: dict, code: str, shared: int) -> dict | None:
    """The ``shared`` trailing tokens of a lookup question split into ``GROUPS``, as offsets from its ``cut``.

    The question quotes the field as "Name (code)"; the tokens of that quotation are the "field" group. None
    if the quotation is not inside the shared tokens or nothing lies between it and the template's tail."""
    chat, cut = question["chat"], question["cut"]
    encoded = engine.tok(chat, add_special_tokens=False, return_offsets_mapping=True)
    token_starts = np.array(encoded["offset_mapping"]).reshape(-1, 2)[:, 0]
    quote_end = chat.rfind(f'({code})"')
    quote_start = chat.rfind('"', 0, quote_end)
    in_quote = (token_starts >= quote_start) & (token_starts < quote_end + len(code) + 3)
    field = [int(token) - cut for token in np.flatnonzero(in_quote)]
    tail_start = question["lay"].tail[0] - cut
    if shared < 4 or not field or min(field) < 0 or tail_start <= max(field):
        return None
    return dict(pre=list(range(0, min(field))), field=field, post=list(range(max(field) + 1, tail_start)),
                tail=list(range(tail_start, shared - 1)), last=[shared - 1])


def sign(query: str) -> int:
    """+1 for the question about PERSON's record, -1 for the one about the model's: the sign of a right
    margin."""
    return 1 if query == "person" else -1


def place_sign(pair: dict, query: str) -> int:
    """+1 if the queried record is the first on the page, -1 if the second."""
    return 1 if pair["queries"][query]["lay"].block(query).slot == 0 else -1


def slot_edit_away(layout: prompts.Layout, vectors: dict, arm: str, record: str, alpha: float) -> list[Edit]:
    """The slot edit at strength ``alpha`` turned away from ``record``'s record, towards the other one (positive
    strength moves the reading to the last record, negative to the first)."""
    signed_alpha = alpha if layout.block(record).slot == 0 else -alpha
    return interventions.mean_edits(layout, vectors, "slot", arm, alpha=signed_alpha)


def is_clean_flip(right_sign, base, donor, min_gap):
    """Whether a lookup counts: the base margin is right (has ``right_sign``), the donor's is the other
    record's, and they lie at least ``min_gap`` logits apart. ``min_gap`` None accepts any pair of margins
    that differ (for tests on a model that cannot do the task)."""
    if min_gap is None:
        return abs(base - donor) > 1e-9
    return (right_sign * base > 0) & (right_sign * donor < 0) & (right_sign * (base - donor) >= min_gap)


def all_heads(engine) -> list[tuple[int, int]]:
    """Every (layer, head), layer 0 being the first block."""
    return [(layer, head) for layer in range(engine.n_layers) for head in range(engine.n_heads)]


def fit_patients(cases: pd.Series) -> list[str]:
    return sorted(cases.index[cases == "fit"])


@dataclass
class LookupRun:
    """One forward pass of a lookup question, recorded at every token after the records."""
    margin: float                   # logit of PERSON's value minus the model's, at the answer token
    resid: object                   # residual stream [layers + 1, tokens, hidden]
    head_out: object                # the heads' outputs side by side [layers, tokens, heads x head_dim]
    attention: dict | None = None   # {token group: [layers, heads, 2]}: from the last token, and averaged
    mlp_out: object = None          # the MLPs' outputs [layers, tokens, hidden]
    attention_map: object = None    # some heads' attention [heads, question tokens, every position]


def attention_source_groups(pair: dict, query: str) -> dict:
    """Earlier tokens a lookup's question may attend to, by name: the asked field's line, the rest of the
    record and the header in the queried and in the other record, and the phrase naming the record."""
    question = pair["queries"][query]
    layout, code = question["lay"], pair["code"]
    queried, other = layout.block(query), layout.block(OTHER_QUERY[query])

    def other_lines(block):
        lines = [line for line_code, line in block.lines.items() if line_code != code]
        return [token for line in lines for token in line.tokens]

    return {"queried_field": queried.lines[code].tokens, "other_field": other.lines[code].tokens,
            "queried_rest": other_lines(queried), "other_rest": other_lines(other),
            "queried_header": queried.header, "other_header": other.header,
            "phrase": list(range(pair["start"], question["cut"]))}


def lookup_runs(engine, vectors: dict, arm: str, pair: dict, alpha: float, attention: bool = False,
                heads: list | None = None) -> dict:
    """The four unpatched runs of a pair: {(query, "clean" | "slot"): LookupRun}.

    "slot" is the same question with the records under the slot edit, turned towards the record not asked
    about. All four share one batch. With ``attention``, each run also holds its attention to
    ``attention_source_groups``; with ``heads``, the token-level attention of those heads."""
    n_ctx = pair["n_ctx"]
    keys, edits, groups = [], [], []
    for query in prompts.QUERIES:
        layout = pair["queries"][query]["lay"]
        sources = attention_source_groups(pair, query)
        for condition in ("clean", "slot"):
            keys.append((query, condition))
            edits.append(slot_edit_away(layout, vectors, arm, query, alpha) if condition == "slot" else [])
            groups.append(sources)
    ids = [pair["queries"][query]["ids"] for query, _ in keys]
    after_records = [list(range(n_ctx, len(row))) for row in ids]
    _, records_state, _ = engine.run([row[:n_ctx] for row in ids], edits=edits)
    logits, _, hidden = engine.run([row[n_ctx:] for row in ids], state=records_state, keep=False,
                                   capture=after_records, zcapture=after_records)
    runs = {}
    for row, key in enumerate(keys):
        margin = float(logits[row, pair["tp"]] - logits[row, pair["to"]])
        runs[key] = LookupRun(margin, hidden[row][1], engine.zcaptured[row][1])
    if attention:
        engine.run([row[n_ctx:] for row in ids], state=records_state, keep=False, groups=groups)
        for row, key in enumerate(keys):
            runs[key].attention = engine.attention[row]
    if heads:
        engine.run([row[n_ctx:] for row in ids], state=records_state, keep=False, attn_heads=heads)
        for row, key in enumerate(keys):
            runs[key].attention_map = engine.attention_maps[row]
    return runs


# ==== Running the rest of a prompt from a cached state, one row per intervention

def run_from(engine, state, rows: list[int], *chunks):
    """The logits after ``chunks`` run one after another, each output row continuing the row of ``state`` that
    ``rows`` names (a row may be named more than once). A chunk is (tokens, keywords): the tokens every row
    runs, or a list of them per row, and that chunk's ``Engine.run`` keywords, each a list with one entry per
    row."""
    state = state.select(rows)
    for index, (tokens, keywords) in enumerate(chunks):
        sequences = tokens if isinstance(tokens[0], list) else [tokens] * len(rows)
        if index == len(chunks) - 1:
            logits, _, _ = engine.run(sequences, state=state, keep=False, **keywords)
            return logits
        _, state, _ = engine.run(sequences, state=state, **keywords)


def margins(logits, first: int, second: int) -> list[float]:
    """Per row: the logit of token ``first`` minus that of token ``second``."""
    return (logits[:, first] - logits[:, second]).tolist()


def value_margins(logits, pair: dict) -> list[float]:
    """Per row: the logit of PERSON's value minus the model's."""
    return margins(logits, pair["tp"], pair["to"])


def patched_margins(engine, state, items: list[dict], batch: int, question, tokens: tuple[int, int]) -> list:
    """The margin (the logit of ``tokens[0]`` minus ``tokens[1]``) of each item after the tokens
    ``question(item)``, run from row 0 of ``state`` under the item's ``resid`` (residual-stream edits),
    ``zpatch`` (head outputs to replace) and ``mpatch`` (MLP outputs to replace)."""
    def run_batch(part):
        patches = dict(edits=[item.get("resid") or [] for item in part],
                       zpatch=[item.get("zpatch") for item in part], mpatch=[item.get("mpatch") for item in part])
        logits = run_from(engine, state, [0] * len(part), ([question(item) for item in part], patches))
        return margins(logits, *tokens)

    return engine._halving(items, batch, run_batch)


def run_items(engine, pair: dict, items: list[dict], batch: int) -> list[float]:
    """The margin of each patched run of a lookup (see ``patched_margins``); an item names its ``query``. The
    records are run once and shared by every item."""
    n_ctx = pair["n_ctx"]
    _, records_state, _ = engine.run([pair["queries"]["person"]["ids"][:n_ctx]])
    return patched_margins(engine, records_state, items, batch,
                           lambda item: pair["queries"][item["query"]]["ids"][n_ctx:], (pair["tp"], pair["to"]))


@dataclass
class Counterfactual:
    """A base run of a lookup and one of its donors, with the tokens at which the two can be exchanged."""
    kind: str                # "query": the donor asks about the other record; "slot": its records are edited
    query: str               # which record the base run asks about
    base: LookupRun
    donor: LookupRun
    positions: list[int]     # base tokens that may be patched: after the phrase for "query", else every one
    first: int               # the base run's first token after the phrase
    rows: list[int]          # where those tokens sit in the donor's recorded arrays

    def donor_head_out(self):
        """The donor's head outputs at the patchable tokens [layers, positions, heads x head_dim]."""
        return self.donor.head_out[:, self.rows]

    def base_head_out(self, n_ctx: int):
        return self.base.head_out[:, [position - n_ctx for position in self.positions]]

    def margins(self) -> dict:
        """The fields of an output row that say which lookup and donor this is, and their two margins."""
        return dict(cf=self.kind, query=self.query, base=self.base.margin, donor=self.donor.margin)


def counterfactuals(pair: dict, runs: dict) -> list[Counterfactual]:
    """Both donors of both questions, in the order (person: query, slot), (own: query, slot).

    The other question has a phrase of another length, so only the tokens after the phrase line up; under the
    slot edit the tokens are the same and every token after the records lines up."""
    n_ctx, found = pair["n_ctx"], []
    for query in prompts.QUERIES:
        base, other = pair["queries"][query], pair["queries"][OTHER_QUERY[query]]
        found.append(Counterfactual(
            "query", query, runs[(query, "clean")], runs[(OTHER_QUERY[query], "clean")],
            positions=list(range(base["cut"], len(base["ids"]))), first=base["cut"],
            rows=list(range(other["cut"] - n_ctx, len(other["ids"]) - n_ctx))))
        found.append(Counterfactual(
            "slot", query, runs[(query, "clean")], runs[(query, "slot")],
            positions=list(range(n_ctx, len(base["ids"]))), first=base["cut"],
            rows=list(range(len(base["ids"]) - n_ctx))))
    return found


def pairs_of(engine, cell_prompts: dict, fields: int):
    """(arm, order, lookup pair) for the first ``fields`` disputed fields of each cell's prompt that
    tokenise."""
    for (arm, order), prompt in cell_prompts.items():
        for code, person_value, own_value in prompts.diff_fields(prompt)[:fields]:
            pair = lookup_pair(engine, prompt, code, person_value, own_value)
            if pair is not None:
                yield arm, order, pair


def case_cells(case: str, cases: pd.Series, cfg: dict) -> dict:
    """{(arm, order): prompt} of one patient's false-evidence cells (the prompt asking for PERSON's
    belief)."""
    frame = prompts.build(cases.loc[[case]], cfg["pairs"]["false"], roles=("C",))
    return {(row.arm, row.block_order): row.prompt for row in frame.itertuples()}


def unpatched_rows(cell: dict, pair: dict, runs: dict) -> list[dict]:
    return [dict(cell, field=pair["code"], query=query, cond=condition, diff=lookup_run.margin)
            for (query, condition), lookup_run in runs.items()]


# ==== Stage circuit, step 1 (fit patients): every head patched alone, then one ranking

def head_attention_rows(cell: dict, pair: dict, runs: dict, heads: list) -> list[dict]:
    """One row per run, token group and head: the head's attention to the group from the answer token ("last")
    and averaged over the question's tokens ("mean")."""
    rows = []
    for (query, condition), lookup_run in runs.items():
        for group, weights in lookup_run.attention.items():
            weights = weights.numpy().astype(np.float32)
            run_fields = dict(cell, field=pair["code"], query=query, cond=condition, group=group)
            rows += [dict(run_fields, layer=layer, head=head, last=weights[layer, head, 0],
                          mean=weights[layer, head, 1]) for layer, head in heads]
    return rows


def single_head_rows(engine, cell: dict, pair: dict, runs: dict, head_columns: dict,
                     batch: int) -> list[dict]:
    """One row per donor and head: the margin with that head's output alone taken from the donor."""
    items = []
    for counterfactual in counterfactuals(pair, runs):
        donor_out = counterfactual.donor_head_out()
        for (layer, head), columns in head_columns.items():
            items.append(dict(counterfactual.margins(), layer=layer, head=head,
                              zpatch=[(counterfactual.positions, donor_out, columns)]))
    patched = run_items(engine, pair, items, batch)
    return [dict(cell, field=pair["code"], cf=item["cf"], query=item["query"], layer=item["layer"],
                 head=item["head"], base=item["base"], donor=item["donor"], patched=margin)
            for item, margin in zip(items, patched)]


def scan_heads(engine, run_dir: Path, cfg: dict, cases: pd.Series, vectors: dict) -> None:
    """Fit patients: each head patched alone from each donor (``circuit/scan``), each head's attention by
    token group (``circuit/attention``) and the unpatched margins (``circuit/clean_fit``). One file per
    patient."""
    settings, folder = cfg["circuit"], run_dir / "circuit"
    heads = all_heads(engine)
    head_columns = {head: engine.head_dims([head]) for head in heads}

    def run_patient(case):
        scan_rows, margin_rows, attention_rows = [], [], []
        for arm, order, pair in pairs_of(engine, case_cells(case, cases, cfg), int(settings["scan_fields"])):
            cell = dict(case_id=case, arm=arm, block_order=order)
            runs = lookup_runs(engine, vectors, arm, pair, float(settings["alpha"]), attention=True)
            margin_rows += unpatched_rows(cell, pair, runs)
            attention_rows += head_attention_rows(cell, pair, runs, heads)
            scan_rows += single_head_rows(engine, cell, pair, runs, head_columns, int(settings["batch"]))
        write(folder / "clean_fit" / f"{case}.parquet", margin_rows)
        write(folder / "attention" / f"{case}.parquet", attention_rows)
        write(folder / "scan" / f"{case}.parquet", scan_rows)
        return f"patients, {len(scan_rows)} patched runs"

    patients = fit_patients(cases)[: int(settings["scan_patients"])]
    each_patient(unfinished(patients, folder / "scan"), "circuit scan", run_patient, counted=False)


def effect_of(patched, base, donor):
    """The share of the way from the base margin to the donor's that the patched margin has gone."""
    return (patched - base) / (donor - base)


def right_sign(rows: pd.DataFrame):
    """Per row, the sign of a right margin: of a lookup, +1 where it asks about PERSON's record (its margin is
    PERSON's value minus the model's); of a reply (rows without ``query``), +1 where it wrote the model's value
    (its margin is the model's value minus PERSON's)."""
    if "query" in rows:
        return np.where(rows["query"] == "person", 1.0, -1.0)
    return np.where(rows.wrote == "model's", 1.0, -1.0)


def flipped(rows: pd.DataFrame):
    """Whether each patched margin gives the other record's answer."""
    return right_sign(rows) * rows.patched < 0


def cell_labels(rows: pd.DataFrame, cfg: dict) -> pd.Series:
    """"labelled, person_first" and so on: the prompt format and page order of each row."""
    labelled_arm = cfg["pairs"]["false"][1]
    labelling = pd.Series(np.where(rows.arm == labelled_arm, "labelled", "unlabelled"), index=rows.index)
    return labelling + ", " + rows.block_order


def effects(frame: pd.DataFrame, min_gap: float | None) -> pd.DataFrame:
    """The rows that count (see ``is_clean_flip``, by ``right_sign``, ``base`` and ``donor``), with a column
    ``effect``: the share of the way from the base margin to the donor's that the patched margin has gone."""
    counts = is_clean_flip(right_sign(frame), frame.base, frame.donor, min_gap)
    kept = frame[counts].copy()
    kept["effect"] = effect_of(kept.patched, kept.base, kept.donor)
    return kept


def rank_heads(run_dir: Path, cfg: dict) -> dict:
    """The heads ranked by their mean single-head effect on the fit patients, once for each donor.

    Written to ``circuit/selection.json`` the first time and read from there afterwards, so that every later
    stage uses the same ranking: {"query": [[layer, head], ...], "slot": [...], "pairs": lookups that
    counted}."""
    path = run_dir / "circuit" / "selection.json"
    if path.exists():
        return json.loads(path.read_text())
    scanned = read(run_dir / "circuit" / "scan")
    counted = effects(scanned, cfg["circuit"]["min_gap"])
    scores = counted.pivot_table(index=["layer", "head"], columns="cf", values="effect", aggfunc="mean")
    every_head = scanned[["layer", "head"]].drop_duplicates().sort_values(["layer", "head"])
    scores = scores.reindex(pd.MultiIndex.from_frame(every_head))
    for counterfactual in COUNTERFACTUALS:
        if counterfactual not in scores:
            scores[counterfactual] = 0.0
    scores = scores.fillna(0.0).reset_index()
    ranking = {}
    for counterfactual in COUNTERFACTUALS:
        ordered = scores.sort_values(counterfactual, ascending=False)[["layer", "head"]].to_numpy()
        ranking[counterfactual] = [[int(layer), int(head)] for layer, head in ordered]
    ranking["pairs"] = {}
    for counterfactual in COUNTERFACTUALS:
        lookups = counted[counted.cf == counterfactual].groupby(LOOKUP_KEYS).ngroups
        ranking["pairs"][counterfactual] = int(lookups)
    scores.to_csv(run_dir / "circuit" / "head_scores.csv", index=False)
    path.write_text(json.dumps(ranking))
    return ranking


def random_like(heads, n_heads: int, rng) -> list[tuple[int, int]]:
    """A random head set with as many heads in each layer as ``heads`` has, drawn from the layer's other heads
    where there are enough of them."""
    chosen, drawn = {tuple(head) for head in heads}, []
    for layer in sorted({layer for layer, _ in chosen}):
        wanted = sum(1 for chosen_layer, _ in chosen if chosen_layer == layer)
        others = [head for head in range(n_heads) if (layer, head) not in chosen]
        take = []
        if others:
            take = [int(head) for head in rng.choice(others, size=min(wanted, len(others)), replace=False)]
        if len(take) < wanted:
            rest = [head for head in range(n_heads) if head not in take]
            take += [int(head) for head in rng.choice(rest, size=wanted - len(take), replace=False)]
        drawn += [(layer, head) for head in take]
    return drawn


# ==== Stage circuit, step 2 (held-out patients): the top heads as sets

def ranked_and_random_sets(engine, ranking: dict, settings: dict) -> dict:
    """{(ranking, k, name): heads}: for each ranking and size k, the top k heads ("top") and random sets with
    the same number of heads per layer ("random0", ...)."""
    rng = np.random.default_rng(int(settings["seed"]))
    head_sets = {}
    for which in COUNTERFACTUALS:
        for size in settings["ks"]:
            top = [tuple(head) for head in ranking[which][: int(size)]]
            head_sets[(which, int(size), "top")] = top
            for draw in range(int(settings["randoms"])):
                head_sets[(which, int(size), f"random{draw}")] = random_like(top, engine.n_heads, rng)
    return head_sets


def head_set_patches(positions: list[int], donor_out, base_out, columns) -> tuple[list, list]:
    """The two patches that test a head set (``columns``; None: every head) as a whole. "keep": every head's
    output from the donor except the set's, held at their base values: how much of the base answer the set
    restores. "swap": only the set's outputs from the donor."""
    return [(positions, donor_out, None), (positions, base_out, columns)], [(positions, donor_out, columns)]


def exact_check_items(engine, positions: list[int], donor_out, base_out) -> list[dict]:
    """The two exact checks among the kept sets: no head held (the donor's run) and every head held (the base
    run)."""
    every_head_kept, _ = head_set_patches(positions, donor_out, base_out, None)
    return [dict(kind="keep", k=0, set="none", zpatch=[(positions, donor_out, None)]),
            dict(kind="keep", k=engine.n_layers * engine.n_heads, set="all", zpatch=every_head_kept)]


def set_test_items(engine, counterfactual: Counterfactual, n_ctx: int, set_columns: dict) -> list[dict]:
    """Patches that test head sets as wholes, kept and swapped (``head_set_patches``), after the exact
    checks."""
    positions = counterfactual.positions
    donor_out, base_out = counterfactual.donor_head_out(), counterfactual.base_head_out(n_ctx)
    margins = counterfactual.margins()
    exact_checks = exact_check_items(engine, positions, donor_out, base_out)
    items = [dict(margins, ranking="", **item) for item in exact_checks]
    for (which, size, name), columns in set_columns.items():
        keep, swap = head_set_patches(positions, donor_out, base_out, columns)
        items.append(dict(margins, kind="keep", ranking=which, k=size, set=name, zpatch=keep))
        items.append(dict(margins, kind="swap", ranking=which, k=size, set=name, zpatch=swap))
    return items


def where_items(counterfactual: Counterfactual, pair: dict, shown_columns: dict) -> list[dict]:
    """Each shown head patched from the donor at one group of question tokens: where the head acts. The phrase
    is a group only under the slot donor, whose phrase has the same tokens."""
    first = counterfactual.first
    by_group = {group: [first + offset for offset in pair["groups"][group]] for group in GROUPS}
    if counterfactual.kind == "slot":
        by_group["phrase"] = list(range(pair["start"], counterfactual.first))
    items = []
    for group, positions in by_group.items():
        donor_rows = [counterfactual.rows[counterfactual.positions.index(position)] for position in positions]
        donor_out = counterfactual.donor.head_out[:, donor_rows]
        for (layer, head), columns in shown_columns.items():
            items.append(dict(counterfactual.margins(), kind="where", group=group, layer=layer, head=head,
                              zpatch=[(positions, donor_out, columns)]))
    return items


def block_by_block(engine, positions: list[int], donor_resid, **fields) -> list[dict]:
    """Items that set the residual stream at ``positions`` to the donor's (``donor_resid``, [layers + 1,
    positions, hidden]) after one block, for each block in turn (``layer`` 1..n), with ``fields``."""
    return [dict(fields, layer=layer, resid=[Edit("set", positions, donor_resid, layers=(layer, layer))])
            for layer in range(1, engine.n_layers + 1)]


def layerpos_items(engine, counterfactual: Counterfactual, pair: dict) -> list[dict]:
    """The residual stream after one block, at one group of question tokens, taken from the other question's
    run: where "whose record" sits on its way from the phrase to the answer token."""
    n_ctx = pair["n_ctx"]
    base, other = pair["queries"][counterfactual.query], pair["queries"][OTHER_QUERY[counterfactual.query]]
    by_group = {}       # group -> (positions in the base run, rows of the donor's recorded arrays)
    for group, offsets in pair["groups"].items():
        positions = [base["cut"] + offset for offset in offsets]
        by_group[group] = (positions, [other["cut"] - n_ctx + offset for offset in offsets])
    by_group["suffix"] = (counterfactual.positions, counterfactual.rows)
    by_group["phrase_last"] = ([base["cut"] - 1], [other["cut"] - 1 - n_ctx])
    items = []
    for group, (positions, donor_rows) in by_group.items():
        items += block_by_block(engine, positions, counterfactual.donor.resid[:, donor_rows],
                                **counterfactual.margins(), kind="layerpos", group=group)
    return items


def test_head_sets(engine, run_dir: Path, cfg: dict, cases: pd.Series, vectors: dict, ranking: dict) -> None:
    """Held-out patients: the top head sets kept or swapped (``circuit/faith``), the top heads patched at one
    token group (``circuit/where``), the residual stream patched by block and token group
    (``circuit/layerpos``) and the unpatched margins (``circuit/clean_test``). One file per patient."""
    settings, folder = cfg["circuit"], run_dir / "circuit"
    head_sets = ranked_and_random_sets(engine, ranking, settings)
    set_columns = {key: engine.head_dims(heads) for key, heads in head_sets.items()}
    shown = list(dict.fromkeys(tuple(head) for which in COUNTERFACTUALS
                               for head in ranking[which][: int(settings["where_heads"])]))
    shown_columns = {head: engine.head_dims([head]) for head in shown}
    saved_sets = {"|".join(map(str, key)): heads for key, heads in head_sets.items()}
    (folder / "sets.json").write_text(json.dumps(saved_sets))

    def run_patient(case):
        rows = {"layerpos": [], "where": [], "faith": []}
        margin_rows = []
        for arm, order, pair in pairs_of(engine, case_cells(case, cases, cfg), int(settings["fields"])):
            cell = dict(case_id=case, arm=arm, block_order=order)
            runs = lookup_runs(engine, vectors, arm, pair, float(settings["alpha"]))
            margin_rows += unpatched_rows(cell, pair, runs)
            items = []
            for counterfactual in counterfactuals(pair, runs):
                items += set_test_items(engine, counterfactual, pair["n_ctx"], set_columns)
                items += where_items(counterfactual, pair, shown_columns)
                if counterfactual.kind == "query":
                    items += layerpos_items(engine, counterfactual, pair)
            patched = run_items(engine, pair, items, int(settings["batch"]))
            for item, margin in zip(items, patched):
                table, row = head_set_test_row(dict(cell, field=pair["code"]), item, margin)
                rows[table].append(row)
        write(folder / "clean_test" / f"{case}.parquet", margin_rows)
        for table in ("layerpos", "where", "faith"):
            write(folder / table / f"{case}.parquet", rows[table])
        return f"patients, {sum(len(table_rows) for table_rows in rows.values())} patched runs"

    patients = held_out_patients(cases, cfg, settings)
    each_patient(unfinished(patients, folder / "faith"), "circuit test", run_patient, counted=False)


def head_set_test_row(lookup: dict, item: dict, patched: float) -> tuple[str, dict]:
    """(table, row) of one patched run of ``test_head_sets``: "layerpos", "where", or "faith" for keep and
    swap."""
    row = dict(lookup, cf=item["cf"], query=item["query"], base=item["base"], donor=item["donor"],
               patched=patched)
    if item["kind"] == "layerpos":
        return "layerpos", dict(row, group=item["group"], layer=item["layer"])
    if item["kind"] == "where":
        return "where", dict(row, group=item["group"], layer=item["layer"], head=item["head"])
    return "faith", dict(row, kind=item["kind"], ranking=item["ranking"], k=item["k"], set=item["set"])


def stage_circuit(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Which heads carry the lookup: scan and rank them on fit patients, test the top sets on held-out
    patients."""
    vectors = load_vectors(run_dir, engine.device)
    scan_heads(engine, run_dir, cfg, cases, vectors)
    ranking = rank_heads(run_dir, cfg)
    test_head_sets(engine, run_dir, cfg, cases, vectors, ranking)


# ==== Stage circuit: tables

def bootstrap_interval(frame: pd.DataFrame, column: str, draws: int, rng) -> tuple[float, float]:
    """95% interval of the mean of ``column`` over patients, each patient's mean resampled; (nan, nan) for
    fewer than three patients."""
    per_patient = frame.groupby("case_id")[column].mean().to_numpy()
    if len(per_patient) < 3:
        return np.nan, np.nan
    resampled = rng.integers(0, len(per_patient), (draws, len(per_patient)))
    means = per_patient[resampled].mean(1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def layerpos_table(layerpos: pd.DataFrame, min_gap) -> pd.DataFrame:
    """Mean effect of the residual-stream patch by block and token group, per cell and over all cells."""
    counted = effects(layerpos, min_gap)
    with_all = pd.concat([counted, counted.assign(arm="all", block_order="all")])
    grouped = with_all.groupby(["arm", "block_order", "group", "layer"])
    return grouped.agg(effect=("effect", "mean"), lookups=("effect", "size")).reset_index()


def answer_token_attention(folder: Path, attention: pd.DataFrame, min_gap) -> pd.DataFrame:
    """Per head: its mean attention from the answer token to each token group, over the fit lookups the model
    answers right, unedited (``att_<group>``) and under the slot edit (``att_<group>_under_slot``)."""
    margins = read(folder / "clean_fit")
    answered_right = (right_sign(margins) * margins["diff"] > 0) | (min_gap is None)
    right = margins[(margins.cond == "clean") & answered_right][LOOKUP_KEYS]
    attention = attention.merge(right, on=list(right.columns))
    wide = attention.pivot_table(index=["layer", "head"], columns=["cond", "group"], values="last",
                                 aggfunc="mean")
    wide.columns = [f"att_{group}" if condition == "clean" else f"att_{group}_under_slot"
                    for condition, group in wide.columns]
    unedited = ("queried_field", "other_field", "queried_header", "other_header", "phrase")
    shown = [f"att_{group}" for group in unedited]
    shown += ["att_queried_field_under_slot", "att_other_field_under_slot"]
    return wide[[column for column in shown if column in wide]]


def head_table(folder: Path, scanned: pd.DataFrame, min_gap) -> pd.DataFrame:
    """Per head: its mean single-head effect under each donor, its rank by each, and where it attends."""
    counted = effects(scanned, min_gap)
    heads = counted.pivot_table(index=["layer", "head"], columns="cf", values="effect", aggfunc="mean")
    heads = heads.add_prefix("effect_")
    attention = read(folder / "attention")
    if not attention.empty:
        heads = heads.join(answer_token_attention(folder, attention, min_gap))
    heads = heads.reset_index()
    for counterfactual in COUNTERFACTUALS:
        if f"effect_{counterfactual}" in heads:
            ranks = heads[f"effect_{counterfactual}"].rank(ascending=False, method="first")
            heads[f"rank_{counterfactual}"] = ranks.astype(int)
    return heads


def head_summary(heads: pd.DataFrame) -> pd.DataFrame:
    """How concentrated the single-head effects are, and how far the two donors' rankings agree."""
    rows = []
    for counterfactual in COUNTERFACTUALS:
        if f"effect_{counterfactual}" not in heads:
            continue
        ordered = heads[f"effect_{counterfactual}"].fillna(0).sort_values(ascending=False).to_numpy()
        positive = ordered.clip(min=0).sum()
        for size in (1, 4, 8, 16, 32, 64, 128, 256):
            share = ordered[:size].clip(min=0).sum() / positive if positive else np.nan
            rows.append(dict(measure=f"top {size} heads' share of the summed positive single-head effects",
                             cf=counterfactual, value=share))
        rows.append(dict(measure="sum of all single-head effects", cf=counterfactual, value=ordered.sum()))
        rows.append(dict(measure="largest single-head effect", cf=counterfactual, value=ordered[0]))
    if {"effect_query", "effect_slot"} <= set(heads):
        correlation = heads[["effect_query", "effect_slot"]].rank().corr().iloc[0, 1]
        rows.append(dict(measure="rank correlation of the two counterfactuals' head effects", cf="both",
                         value=correlation))
        for size in (16, 32, 64):
            in_both = set(heads[heads.rank_query <= size].index) & set(heads[heads.rank_slot <= size].index)
            rows.append(dict(measure=f"heads in both top {size} lists", cf="both", value=len(in_both)))
    return pd.DataFrame(rows)


def faith_table(faith: pd.DataFrame, min_gap, draws: int, rng) -> pd.DataFrame:
    """Per donor, kind of patch, ranking and set size: the share of the answer the set restores ("keep") or
    moves ("swap"), with a patient-bootstrap interval; the random sets of one size are pooled."""
    counted = effects(faith, min_gap)
    counted["restored"] = np.where(counted.kind == "keep", 1 - counted.effect, counted.effect)
    counted["which"] = np.where(counted.set.str.startswith("random"), "random", counted.set)
    rows = []
    for key, group in counted.groupby(["cf", "kind", "ranking", "k", "which"]):
        lower, upper = bootstrap_interval(group, "restored", draws, rng)
        per_set = group.groupby("set").restored.mean()
        rows.append(dict(zip(["cf", "kind", "ranking", "k", "set"], key), value=group.restored.mean(),
                         lower=lower, upper=upper, lowest_set=per_set.min(), highest_set=per_set.max(),
                         lookups=group.groupby(LOOKUP_KEYS).ngroups))
    return pd.DataFrame(rows)


def exactness_check(faith: pd.DataFrame) -> pd.DataFrame:
    """The patching arithmetic checked: with every head from the donor the run must be the donor's, and with
    every head put back from the base run it must be the base run."""
    exact = faith[faith.set.isin(["none", "all"])]
    expected = np.where(exact.set == "none", exact.donor, exact.base)
    check = ("every head from the donor = the donor's run; then every head back from the base = the base run "
             "(largest logit difference)")
    return pd.DataFrame([dict(check=check, value=float(np.abs(exact.patched - expected).max()))])


def tables(run_dir: Path, cfg: dict, draws: int = 1000) -> dict:
    """The tables of ``stage_circuit`` by name. A table is left out if its rows have not been written yet."""
    settings, folder = cfg["circuit"], run_dir / "circuit"
    min_gap = settings["min_gap"]
    rng = np.random.default_rng(int(settings["seed"]))
    out = {}
    layerpos = read(folder / "layerpos")
    if not layerpos.empty:
        out["circuit_layerpos"] = layerpos_table(layerpos, min_gap)
    scanned = read(folder / "scan")
    if not scanned.empty:
        heads = head_table(folder, scanned, min_gap)
        order_by = "effect_query" if "effect_query" in heads else heads.columns[2]
        out["circuit_heads"] = heads.sort_values(order_by, ascending=False)
        out["circuit_summary"] = head_summary(heads)
    faith = read(folder / "faith")
    if not faith.empty:
        out["circuit_faith"] = faith_table(faith, min_gap, draws, rng)
        out["circuit_checks"] = exactness_check(faith)
    where = read(folder / "where")
    if not where.empty:
        counted = effects(where, min_gap)
        by_group = counted.pivot_table(index=["cf", "layer", "head"], columns="group", values="effect",
                                       aggfunc="mean")
        out["circuit_where"] = by_group.reset_index()
    return out


# ==== Stage reply: the lookup's head sets at the token where a reasoned reply first writes a disputed value

def reply_head_set_rows(engine, vectors: dict, reply, point: WritingPoint, set_columns: dict, alpha: float,
                        batch: int) -> list[dict]:
    """One reply's rows for ``stage_reply``: the margin at the writing token with head sets kept or swapped.

    The margin is the logit of the model's value minus PERSON's. The donor is the same tokens with the records
    under the slot edit, turned away from the record whose value the reply wrote."""
    ids, n_ctx = point.ids, point.layout.n_ctx
    own_token, person_token = point.own_token, point.person_token
    after_records = list(range(n_ctx, len(ids)))
    slot_edit = slot_edit_away(point.layout, vectors, reply.arm, point.written_record, alpha)
    _, records_state, _ = engine.run([ids[:n_ctx]] * 2, edits=[[], slot_edit])
    logits, _, _ = engine.run([ids[n_ctx:]] * 2, state=records_state, keep=False,
                              zcapture=[after_records, after_records])
    base, donor = margins(logits, own_token, person_token)
    base_out, donor_out = engine.zcaptured[0][1], engine.zcaptured[1][1]

    items = exact_check_items(engine, after_records, donor_out, base_out)
    for (size, name), columns in set_columns.items():
        keep, swap = head_set_patches(after_records, donor_out, base_out, columns)
        items.append(dict(kind="keep", k=size, set=name, zpatch=keep))
        items.append(dict(kind="swap", k=size, set=name, zpatch=swap))
        if name == "top":       # the set from the donor at the writing token alone
            items.append(dict(kind="swap_last", k=size, set=name,
                              zpatch=[(after_records[-1:], donor_out[:, -1:], columns)]))
    # every item runs from row 0, the unedited records
    patched = patched_margins(engine, records_state, items, batch, lambda item: ids[n_ctx:],
                              (own_token, person_token))
    meta = dict(case_id=reply.case_id, arm=reply.arm, block_order=reply.block_order, sample=int(reply.sample),
                field=point.code, wrote=point.whose, tokens=len(ids) - n_ctx, base=base, donor=donor)
    return [dict(meta, kind=item["kind"], k=item["k"], set=item["set"], patched=margin)
            for item, margin in zip(items, patched)]


def stage_reply(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Do the lookup's heads also carry the value a reasoned reply writes?

    Unedited sampled replies of the false-evidence cells are fed back up to the first disputed value they
    write. At that token the head sets of ``stage_circuit`` (the top heads of the query ranking, and its
    random sets) are patched at every token after the records: "keep" (every head from the slot-edited run
    except the set), "swap" (only the set from it) and "swap_last" (the set from it at the writing token
    alone). Writes ``circuit_reply``, one file per patient."""
    settings = cfg["circuit_reply"]
    alpha = float(cfg["circuit"]["alpha"])
    vectors = load_vectors(run_dir, engine.device)
    saved_sets = json.loads((run_dir / "circuit" / "sets.json").read_text())
    names = ["top"] + [f"random{draw}" for draw in range(int(settings["randoms"]))]
    head_sets = {(size, name): [tuple(head) for head in saved_sets[f"query|{size}|{name}"]]
                 for size in settings["ks"] for name in names}
    set_columns = {key: engine.head_dims(heads) for key, heads in head_sets.items()}
    replies = unedited_replies(run_dir, settings["folders"], cfg["pairs"]["false"])
    folder = run_dir / "circuit_reply"

    def run_patient(case):
        rows = []
        for reply, prompt_of in replies_with_prompts(replies, case, cases):
            prompt = prompt_of[(reply.arm, reply.block_order)]
            point = first_written_value(engine, prompt, reply.text, int(settings["max_tokens"]))
            if point is None:
                continue
            rows += reply_head_set_rows(engine, vectors, reply, point, set_columns, alpha,
                                        int(settings["batch"]))
        write(folder / f"{case}.parquet", rows)
        return f"patients, {len(rows)} patched runs"

    each_patient(unfinished(sorted(set(replies.case_id)), folder), "circuit reply", run_patient)


def reply_table(run_dir: Path, cfg: dict, draws: int = 1000) -> pd.DataFrame:
    """Per cell and what the reply wrote: the share of the written value's margin a head set restores under
    the slot edit ("keep"), or the share of the edit's effect it carries ("swap", "swap_last"). Only replies
    whose written value the slot edit flips by at least ``min_gap`` logits count; ``attrs['flipped']`` is
    their share."""
    patched = read(run_dir / "circuit_reply")
    if patched.empty:
        return patched
    min_gap = cfg["circuit"]["min_gap"]
    rng = np.random.default_rng(int(cfg["circuit"]["seed"]))
    counts = is_clean_flip(right_sign(patched), patched.base, patched.donor, min_gap)
    counted = effects(patched, min_gap)
    counted["value"] = np.where(counted.kind == "keep", 1 - counted.effect, counted.effect)
    counted["which"] = np.where(counted.set.str.startswith("random"), "random", counted.set)
    with_all = pd.concat([counted, counted.assign(arm="all", block_order="all", wrote="any")])
    rows = []
    for key, group in with_all.groupby(["arm", "block_order", "wrote", "kind", "k", "which"]):
        lower, upper = bootstrap_interval(group, "value", draws, rng)
        replies = group.groupby(["case_id", "sample"]).ngroups
        keys = dict(zip(["arm", "block_order", "wrote", "kind", "k", "set"], key))
        rows.append(dict(keys, value=group.value.mean(), lower=lower, upper=upper, replies=replies))
    table = pd.DataFrame(rows)
    one_reply = [patched.case_id, patched.arm, patched.block_order, patched["sample"]]
    per_reply = counts.groupby(one_reply).first()
    table.attrs["flipped"] = float(per_reply.mean())
    return table


def reply_tables(run_dir: Path, cfg: dict) -> dict:
    """``reply_table`` under its name, once replies have been patched."""
    table = reply_table(run_dir, cfg)
    return {} if table.empty else {"circuit_reply": table}


def reply_probes(engine, run_dir: Path, cfg: dict, cases: pd.Series, vectors: dict, folders, alpha: float,
                 max_tokens: int):
    """The unedited replies of the false-evidence cells, each fed back to its first written disputed value,
    with what its two donors need. Yields one dict per usable reply:

        case_id, arm, block_order, sample, field, wrote     which reply, and whose value it wrote
        ids, n_ctx, own_tok, person_tok                     the tokens, where the records end, the two answer
                                                            tokens
        slot_edits     the slot edit that turns the reading away from the record whose value was written
        twin           the other arm's records (labelled for an unlabelled prompt and the reverse) as tokens,
                       or None if the question does not tokenise the same after them
        q_end, prompt_len, line_tok     the first token of the template's tail, of the reply, and of the line
                                        that states the value
        first_slot     0 if the written value's record is the first on the page, else 1
    """
    false_arms = cfg["pairs"]["false"]
    plain, labelled = false_arms
    replies = unedited_replies(run_dir, folders, false_arms)
    for case in sorted(set(replies.case_id)):
        by_sample = replies_with_prompts(replies, case, cases, false_arms, ["sample", "arm", "block_order"])
        for reply, prompt_of in by_sample:
            prompt = prompt_of[(reply.arm, reply.block_order)]
            point = first_written_value(engine, prompt, reply.text, max_tokens)
            if point is None:
                continue
            other_arm = labelled if reply.arm == plain else plain
            twin_prompt = prompt_of[(other_arm, reply.block_order)]
            yield reply_probe(engine, vectors, reply, prompt, twin_prompt, point, alpha)


def reply_probe(engine, vectors: dict, reply, prompt: str, twin_prompt: str, point: WritingPoint,
                alpha: float) -> dict:
    """One reply's entry of ``reply_probes``."""
    layout = point.layout
    twin = prompts.layout(engine.tok, twin_prompt, engine.kw, prefix="")
    same_question = twin.ids[twin.n_ctx:] == layout.ids[layout.n_ctx:]
    up_to_line = prompts.chat_text(engine.tok, prompt, engine.kw) + reply.text[:point.line_start]
    line_token = len(engine.tok(up_to_line, add_special_tokens=False)["input_ids"])
    writing_token = len(point.ids)
    return dict(case_id=reply.case_id, arm=reply.arm, block_order=reply.block_order, sample=int(reply.sample),
                field=point.code, wrote=point.whose, ids=point.ids, n_ctx=layout.n_ctx,
                own_tok=point.own_token, person_tok=point.person_token,
                slot_edits=slot_edit_away(layout, vectors, reply.arm, point.written_record, alpha),
                twin=twin.ids[:twin.n_ctx] if same_question else None, q_end=layout.tail[0],
                prompt_len=len(layout.ids), line_tok=min(max(line_token, len(layout.ids)), writing_token - 1),
                first_slot=layout.block(point.written_record).slot)


# ==== Stage fine: the smallest head set found for the lookup, and how its heads depend on each other

def greedy_lookups(engine, cfg: dict, cases: pd.Series, vectors: dict, patients: int) -> list[dict]:
    """The fit patients' lookups that count, each with its query donor: what the greedy search is scored
    on."""
    alpha, min_gap = float(cfg["circuit"]["alpha"]), cfg["circuit"]["min_gap"]
    lookups = []
    for case in fit_patients(cases)[:patients]:
        for arm, _, pair in pairs_of(engine, case_cells(case, cases, cfg), 1):
            runs = lookup_runs(engine, vectors, arm, pair, alpha)
            for counterfactual in counterfactuals(pair, runs):
                base, donor = counterfactual.base.margin, counterfactual.donor.margin
                if counterfactual.kind != "query":
                    continue
                if not is_clean_flip(sign(counterfactual.query), base, donor, min_gap):
                    continue
                lookups.append(dict(pair=pair, query=counterfactual.query, positions=counterfactual.positions,
                                    base=base, donor=donor, donor_out=counterfactual.donor_head_out().clone(),
                                    base_out=counterfactual.base_head_out(pair["n_ctx"]).clone()))
    return lookups


def greedy_search(engine, lookups: list[dict], candidates: list, settings: dict) -> list[dict]:
    """Grow a head set one head at a time: at each step add the candidate with which the set, held at its base
    values while every other head comes from the other question's run, restores most of the base answer. Stops
    at ``max_heads`` heads or once the set restores ``stop`` of the answer."""
    chosen, steps = [], []
    while len(chosen) < int(settings["max_heads"]) and lookups:
        remaining = [head for head in candidates if head not in chosen]
        if not remaining:
            break
        columns = {head: engine.head_dims(chosen + [head]) for head in remaining}
        restored = np.zeros(len(remaining))
        for lookup in lookups:
            items = [dict(query=lookup["query"], zpatch=head_set_patches(lookup["positions"], lookup["donor_out"],
                                                                         lookup["base_out"], columns[head])[0])
                     for head in remaining]
            patched = np.array(run_items(engine, lookup["pair"], items, int(settings["batch"])))
            restored += 1 - effect_of(patched, lookup["base"], lookup["donor"])
        best = int(np.argmax(restored))
        chosen.append(remaining[best])
        steps.append(dict(step=len(chosen), layer=remaining[best][0], head=remaining[best][1],
                          restored=float(restored[best] / len(lookups)), lookups=len(lookups)))
        print(f"[circuit fine] greedy {len(chosen)}: head {remaining[best]} -> {steps[-1]['restored']:.3f}",
              flush=True)
        if steps[-1]["restored"] >= float(settings["stop"]):
            break
    return steps


def greedy_and_random_sets(engine, greedy: list, settings: dict, seed: int) -> dict:
    """{("greedy", k, name): heads}: the first k greedy heads for every k, and for some k random sets with the
    same number of heads per layer."""
    rng = np.random.default_rng(seed)
    head_sets = {("greedy", size, "top"): greedy[:size] for size in range(1, len(greedy) + 1)}
    sizes_with_random = {size for size in (4, 8, 12, 16, 24, 32, len(greedy)) if 0 < size <= len(greedy)}
    for size in sorted(sizes_with_random):
        for draw in range(int(settings["randoms"])):
            head_sets[("greedy", size, f"random{draw}")] = random_like(greedy[:size], engine.n_heads, rng)
    return head_sets


def greedy_set_rows(engine, pair: dict, counterfactual: Counterfactual, lookup: dict, set_columns: dict,
                    batch: int) -> list[dict]:
    """The margin with each greedy or random set kept at its base values, and with each greedy set swapped."""
    positions = counterfactual.positions
    donor_out, base_out = counterfactual.donor_head_out(), counterfactual.base_head_out(pair["n_ctx"])
    items = []
    for (_, size, name), columns in set_columns.items():
        keep, swap = head_set_patches(positions, donor_out, base_out, columns)
        items.append(dict(lookup, kind="keep", k=size, set=name, zpatch=keep))
        if name == "top":
            items.append(dict(lookup, kind="swap", k=size, set=name, zpatch=swap))
    patched = run_items(engine, pair, items, batch)
    return [dict(lookup, kind=item["kind"], k=item["k"], set=item["set"], patched=margin)
            for item, margin in zip(items, patched)]


def heads_apart(engine, head_out, heads: list):
    """The listed heads' outputs cut out of the side-by-side array [layers, tokens, heads x head_dim], as
    float32: [heads, tokens, head_dim]."""
    width = engine.head_dim
    apart = [head_out[layer][:, head * width:(head + 1) * width].float() for layer, head in heads]
    return engine.torch.stack(apart)


def wiring_rows(engine, pair: dict, counterfactual: Counterfactual, lookup: dict, wired: list,
                wired_columns: dict) -> list[dict]:
    """How the heads depend on each other. One head (upstream) is given its donor output and the later heads
    react; for each later head (downstream), ``share`` is the part of its own base-to-donor change that
    follows:
    sum over tokens of <z(patched) - z(base), z(donor) - z(base)> / |z(donor) - z(base)|^2."""
    positions = counterfactual.positions
    donor_out, base_out = counterfactual.donor_head_out(), counterfactual.base_head_out(pair["n_ctx"])
    base, donor = counterfactual.base.margin, counterfactual.donor.margin
    base_apart = heads_apart(engine, base_out, wired)
    full_change = heads_apart(engine, donor_out - base_out, wired)      # each head's base-to-donor change
    change_size = (full_change ** 2).sum((1, 2)).clamp_min(1e-12)
    ids, n_ctx = pair["queries"][counterfactual.query]["ids"], pair["n_ctx"]
    _, records_state, _ = engine.run([ids[:n_ctx]])
    one_per_head = dict(zpatch=[[(positions, donor_out, wired_columns[head])] for head in wired],
                        zcapture=[positions] * len(wired))
    logits = run_from(engine, records_state, [0] * len(wired), (ids[n_ctx:], one_per_head))
    up_margins = value_margins(logits, pair)
    rows = []
    for row, upstream in enumerate(wired):
        change = heads_apart(engine, engine.zcaptured[row][1], wired) - base_apart
        share = ((change * full_change).sum((1, 2)) / change_size).cpu().numpy()
        up_effect = effect_of(up_margins[row], base, donor)
        for index, downstream in enumerate(wired):
            if downstream[0] <= upstream[0]:        # only heads in later blocks can react
                continue
            rows.append(dict(lookup, up_layer=upstream[0], up_head=upstream[1], down_layer=downstream[0],
                             down_head=downstream[1], share=float(share[index]), up_effect=up_effect))
    return rows


def stage_fine(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """The lookup circuit made finer.

    (a) A greedy search on fit patients for the smallest head set that restores the base answer
        (``circuit_fine/greedy.json``, written once).
    (b) Held-out patients: the first k greedy heads kept, for every k, and swapped, and random sets of the
        same shape, under both donors (``circuit_fine/test``).
    (c) Held-out patients: how the heads of the set depend on each other (``circuit_fine/wiring``)."""
    settings, circuit_settings = cfg["circuit_fine"], cfg["circuit"]
    alpha, min_gap = float(circuit_settings["alpha"]), circuit_settings["min_gap"]
    vectors = load_vectors(run_dir, engine.device)
    ranking = json.loads((run_dir / "circuit" / "selection.json").read_text())
    folder = run_dir / "circuit_fine"
    folder.mkdir(exist_ok=True)
    candidates = [tuple(head) for head in ranking["query"][: int(settings["candidates"])]]
    if not (folder / "greedy.json").exists():
        started = time.time()
        lookups = greedy_lookups(engine, cfg, cases, vectors, int(settings["greedy_patients"]))
        steps = greedy_search(engine, lookups, candidates, settings)
        (folder / "greedy.json").write_text(json.dumps(steps))
        seconds = time.time() - started
        print(f"[circuit fine] greedy search on {len(lookups)} lookups, {seconds:.1f}s", flush=True)
        del lookups
    greedy = [(step["layer"], step["head"]) for step in json.loads((folder / "greedy.json").read_text())]
    head_sets = greedy_and_random_sets(engine, greedy, settings, int(circuit_settings["seed"]) + 1)
    set_columns = {key: engine.head_dims(heads) for key, heads in head_sets.items()}
    wired = list(dict.fromkeys(greedy + candidates))[: int(settings["wiring_heads"])]
    wired_columns = {head: engine.head_dims([head]) for head in wired}

    def run_patient(case):
        set_rows, wire_rows = [], []
        for arm, order, pair in pairs_of(engine, case_cells(case, cases, cfg), 1):
            runs = lookup_runs(engine, vectors, arm, pair, alpha)
            for counterfactual in counterfactuals(pair, runs):
                lookup = dict(case_id=case, arm=arm, block_order=order, field=pair["code"],
                              **counterfactual.margins())
                set_rows += greedy_set_rows(engine, pair, counterfactual, lookup, set_columns,
                                            int(settings["batch"]))
                base, donor = counterfactual.base.margin, counterfactual.donor.margin
                if not is_clean_flip(sign(counterfactual.query), base, donor, min_gap):
                    continue
                wire_rows += wiring_rows(engine, pair, counterfactual, lookup, wired, wired_columns)
        write(folder / "test" / f"{case}.parquet", set_rows)
        write(folder / "wiring" / f"{case}.parquet", wire_rows)
        return f"test patients, {len(set_rows)} patched runs, {len(wire_rows)} head pairs"

    patients = held_out_patients(cases, cfg)[: int(settings["patients"])]
    each_patient(unfinished(patients, folder / "wiring"), "circuit fine", run_patient, counted=False)


# ==== Stage algorithm: what each head reads and writes, and how the question's "whose record" is coded

def question_and_source_groups(pair: dict, query: str) -> tuple[dict, dict]:
    """Token positions of one lookup by name: (destinations, sources).

    Destinations are groups of the question's tokens, from which attention is measured: the phrase, its last
    token, and the shared groups. Sources are groups of earlier tokens, to which it is measured: in the
    queried and in the other record the header, the asked field's name, value and line end, the other lines
    and the end marker; and the parts of the question."""
    question = pair["queries"][query]
    layout, cut, code = question["lay"], question["cut"], pair["code"]
    shared = {name: [cut + offset for offset in offsets] for name, offsets in pair["groups"].items()}
    destinations = dict(phrase=list(range(pair["start"], cut)), phrase_last=[cut - 1], **shared)
    sources = {"before_records": list(range(0, min(layout.blocks[0].header)))}
    for which, owner in (("queried", query), ("other", OTHER_QUERY[query])):
        block = layout.block(owner)
        line = block.lines[code]
        rest = [other for line_code, other in block.lines.items() if line_code != code]
        other_lines = [token for other in rest for token in other.tokens]
        sources |= {f"{which}_header": block.header, f"{which}_field_name": line.name,
                    f"{which}_field_value": line.val, f"{which}_field_end": line.eol,
                    f"{which}_other_lines": other_lines, f"{which}_end_marker": block.extra}
    sources |= {"question_start": list(range(pair["n_ctx"], pair["start"])), "phrase": destinations["phrase"],
                "after_phrase": shared["pre"], "field_in_question": shared["field"],
                "rest_of_question": shared["post"], "template_tail": shared["tail"] + shared["last"]}
    return destinations, sources


def add_to_running_sum(sums: dict, key, value) -> None:
    total, count = sums.get(key, (0.0, 0))
    sums[key] = (total + value, count + 1)


def answer_readout(engine, lookup_run: LookupRun, pair: dict):
    """The direction of the final residual stream that the margin reads: the two answer tokens' unembedding
    difference through the final norm, at the scale the answer token has in this run. A component's output at
    the answer token times this direction is its direct contribution to the margin."""
    torch, final_norm = engine.torch, engine.lm.model.norm
    unembedding = engine.lm.lm_head.weight
    answer_state = lookup_run.resid[engine.n_layers, -1].float()
    eps = getattr(final_norm, "eps", getattr(final_norm, "variance_epsilon", 1e-6))
    token_difference = (unembedding[pair["tp"]] - unembedding[pair["to"]]).float()
    return token_difference * final_norm.weight.float() / torch.sqrt((answer_state ** 2).mean() + eps)


def direct_head_contributions(engine, lookup_run: LookupRun, pair: dict, query: str):
    """Each head's direct contribution to the right answer [layers, heads]: its output at the answer token
    through the block's output projection and ``answer_readout``, signed so that positive favours the asked
    record."""
    torch, n_heads, head_dim = engine.torch, engine.n_heads, engine.head_dim
    # each block's output projection [hidden, heads x head_dim]
    output_projections = [layer.self_attn.o_proj.weight for layer in engine.lm.model.layers]
    with torch.no_grad():
        readout = answer_readout(engine, lookup_run, pair)
        per_layer = []
        for layer in range(engine.n_layers):
            head_out = lookup_run.head_out[layer, -1].float().view(n_heads, head_dim)
            readout_per_head = (readout @ output_projections[layer].float()).view(n_heads, head_dim)
            per_layer.append((head_out * readout_per_head).sum(-1))
        return torch.stack(per_layer).cpu().numpy() * sign(query)


def question_difference(pair: dict, runs: dict, offsets: list[int]):
    """The residual stream of the question about PERSON's record minus that of the question about the model's,
    each averaged over one group of shared tokens [layers + 1, hidden]: what codes "whose record" there."""
    n_ctx = pair["n_ctx"]
    means = []
    for query in prompts.QUERIES:
        rows = [pair["queries"][query]["cut"] - n_ctx + offset for offset in offsets]
        means.append(runs[(query, "clean")].resid[:, rows].float().mean(1))
    return (means[0] - means[1]).cpu()


def add_attention(attention_sums: dict, lookup_key: tuple, attention_map, pair: dict, query: str) -> None:
    """Add one run's attention, from each destination group to each source group, to the running sums.
    ``attention_map``: [heads, question tokens, every position]. The entry per head is the attention to the
    source group summed over its tokens and averaged over the destination's tokens."""
    n_ctx = pair["n_ctx"]
    destinations, sources = question_and_source_groups(pair, query)
    for destination, destination_positions in destinations.items():
        from_destination = attention_map[:, [position - n_ctx for position in destination_positions]]
        for source, source_positions in sources.items():
            if not source_positions:
                continue
            per_head = from_destination[:, :, source_positions].sum(-1).mean(-1).cpu().numpy()
            add_to_running_sum(attention_sums, (*lookup_key, destination, source), per_head)


def attention_rows(attention_sums: dict, heads: list) -> list[dict]:
    """One row per cell, question, condition, destination, source and head: mean attention over lookups."""
    rows = []
    for (arm, order, query, condition, destination, source), (total, count) in attention_sums.items():
        group = dict(arm=arm, block_order=order, query=query, cond=condition, dest=destination, source=source)
        rows += [dict(group, layer=layer, head=head, attention=float(total[index] / count), lookups=count)
                 for index, (layer, head) in enumerate(heads)]
    return rows


def direct_rows(engine, direct: list[dict]) -> list[dict]:
    """One row per cell, question, condition and head: its mean direct contribution over the lookups answered
    right."""
    keys, rows = ["arm", "block_order", "query", "cond"], []
    if not direct:
        return rows
    for key, group in pd.DataFrame(direct).groupby(keys):
        group = group[group.right]
        if not len(group):
            continue
        mean = np.mean(np.stack(group.dla.to_list()), 0)
        rows += [dict(zip(keys, key), layer=layer, head=head, direct=float(mean[layer, head]),
                      margin=float(group.margin.mean()), lookups=len(group))
                 for layer, head in all_heads(engine)]
    return rows


def code_rows(engine, code_sums: dict) -> list[dict]:
    """Cosines, per block and token group, between cells' mean question differences.

    Between the two page orders of an arm the cosine is +1 if the difference codes the owner asked about and
    -1 if it codes the record's place. Each cell is also compared with itself across two halves of the
    patients, which says how reliable the mean is."""
    torch = engine.torch
    means = {key: total / count for key, (total, count) in code_sums.items()}      # (arm, order, group, half)
    cells = sorted({key[:2] for key in means})
    layers = range(1, engine.n_layers + 1)

    def cosine(first, second):
        """Cosine per layer of two [layers + 1, hidden] arrays."""
        return torch.nn.functional.cosine_similarity(first, second, dim=-1).numpy()

    def cell_mean(arm, order, group):
        """A cell's mean question difference over the halves of the patients that have it, or None."""
        halves = [means[(arm, order, group, half)] for half in (0, 1) if (arm, order, group, half) in means]
        return sum(halves) / len(halves) if halves else None

    rows = []
    for group in sorted({key[2] for key in means}):
        for index, (arm, order) in enumerate(cells):
            if (arm, order, group, 0) in means and (arm, order, group, 1) in means:
                by_layer = cosine(means[(arm, order, group, 0)], means[(arm, order, group, 1)])
                pair_of_cells = dict(group=group, cell_a=f"{arm}|{order}",
                                     cell_b="same cell, other half of the patients")
                rows += [dict(pair_of_cells, layer=layer, cosine=float(by_layer[layer])) for layer in layers]
            for other_arm, other_order in cells[index + 1:]:
                first, second = cell_mean(arm, order, group), cell_mean(other_arm, other_order, group)
                if first is None or second is None:         # a cell without this token group has no row
                    continue
                by_layer = cosine(first, second)
                pair_of_cells = dict(group=group, cell_a=f"{arm}|{order}",
                                     cell_b=f"{other_arm}|{other_order}")
                rows += [dict(pair_of_cells, layer=layer, cosine=float(by_layer[layer])) for layer in layers]
    return rows


def stage_algorithm(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """What the circuit's heads do, on fit patients' lookups (both questions, unedited and slot-edited).

        reads    attention from each group of question tokens to each group of earlier tokens
                 (``circuit_algorithm/attention.parquet``), for the top heads
        writes   every head's direct contribution to the answer (``direct.parquet``)
        code     the difference between the two questions' residual streams at each group of shared question
                 tokens, and its cosine between cells: is "whose record" coded as owner or as place
                 (``code.parquet``)
    """
    settings, circuit_settings = cfg["circuit_algorithm"], cfg["circuit"]
    alpha, min_gap = float(circuit_settings["alpha"]), circuit_settings["min_gap"]
    vectors = load_vectors(run_dir, engine.device)
    folder = run_dir / "circuit_algorithm"
    folder.mkdir(exist_ok=True)
    ranking = json.loads((run_dir / "circuit" / "selection.json").read_text())
    greedy_path = run_dir / "circuit_fine" / "greedy.json"
    greedy = []         # the greedy set of stage_fine, if that stage has run, heads the list
    if greedy_path.exists():
        greedy = [(step["layer"], step["head"]) for step in json.loads(greedy_path.read_text())]
    heads = list(dict.fromkeys(greedy + [tuple(head) for head in ranking["query"]]))[: int(settings["heads"])]
    attention_sums, code_sums = {}, {}       # running sums, see add_to_running_sum
    direct = []
    patients = fit_patients(cases)[: int(settings["patients"])]
    for patient_index, case in enumerate(patients):
        for arm, order, pair in pairs_of(engine, case_cells(case, cases, cfg), 1):
            runs = lookup_runs(engine, vectors, arm, pair, alpha, heads=heads)
            answered_right = {query: sign(query) * runs[(query, "clean")].margin > 0 or min_gap is None
                              for query in prompts.QUERIES}
            for (query, condition), lookup_run in runs.items():
                if answered_right[query]:
                    add_attention(attention_sums, (arm, order, query, condition), lookup_run.attention_map, pair,
                                  query)
                margin = sign(query) * lookup_run.margin
                contributions = direct_head_contributions(engine, lookup_run, pair, query)
                direct.append(dict(case_id=case, arm=arm, block_order=order, query=query, cond=condition,
                                   right=bool(answered_right[query]), margin=margin, dla=contributions))
            if not all(answered_right.values()):
                continue
            half = patient_index % 2
            for group, offsets in dict(pair["groups"], phrase_last=[-1]).items():
                difference = question_difference(pair, runs, offsets)
                add_to_running_sum(code_sums, (arm, order, group, half), difference)
        print(f"[circuit algorithm] {patient_index + 1}/{len(patients)} fit patients", flush=True)
    write(folder / "attention.parquet", attention_rows(attention_sums, heads))
    write(folder / "direct.parquet", direct_rows(engine, direct))
    write(folder / "code.parquet", code_rows(engine, code_sums))
    (folder / "heads.json").write_text(json.dumps(heads))


def algorithm_tables(run_dir: Path, cfg: dict) -> dict:
    """The tables of ``stage_algorithm``: ``circuit_reads`` (attention by head, condition and destination, one
    column per source), ``circuit_writes`` (direct contribution per head, unedited and under the slot edit)
    and ``circuit_code``."""
    out, folder = {}, run_dir / "circuit_algorithm"
    if not (folder / "attention.parquet").exists():
        return out
    attention = pd.read_parquet(folder / "attention.parquet")
    # a token group that a cell lacks (end markers in the unlabelled prompt) counts as no attention
    per_cell = attention.pivot_table(index=["arm", "block_order", "query", "cond", "layer", "head", "dest"],
                                     columns="source", values="attention").fillna(0.0)
    out["circuit_reads"] = per_cell.groupby(["layer", "head", "cond", "dest"]).mean().reset_index()
    direct = pd.read_parquet(folder / "direct.parquet")
    per_head = direct.groupby(["cond", "layer", "head"]).agg(
        direct=("direct", "mean"), margin=("margin", "mean"))
    per_head = per_head.reset_index()
    writes = per_head.pivot_table(index=["layer", "head"], columns="cond", values="direct")
    writes = writes.add_prefix("direct_").reset_index()
    writes["margin_clean"] = float(per_head[per_head.cond == "clean"].margin.mean())
    out["circuit_writes"] = writes.sort_values("direct_clean", ascending=False)
    out["circuit_code"] = pd.read_parquet(folder / "code.parquet")
    return out


def reader_heads(run_dir: Path, threshold: float) -> list[tuple[int, int]]:
    """The heads recorded by ``stage_algorithm`` that, from the answer token, give at least ``threshold`` of
    their attention to the asked field's value in the queried record."""
    attention = pd.read_parquet(run_dir / "circuit_algorithm" / "attention.parquet")
    to_value = attention[(attention.dest == "last") & (attention.cond == "clean")
                         & (attention.source == "queried_field_value")]
    mean = to_value.groupby(["layer", "head"]).attention.mean()
    return [(int(layer), int(head)) for (layer, head), share in mean.items() if share >= threshold]


# ==== Stage concrete: each step of the lookup tested, the place vector, and where labelling enters a reply

# Where "whose record" is measured or written among the question's tokens: the shared groups and the phrase's
# end.
QUESTION_SITES = list(GROUPS) + ["phrase_last"]


@dataclass
class ConcreteSetup:
    engine: object
    run_dir: Path
    cfg: dict
    cases: pd.Series
    vectors: dict            # the slot vectors
    settings: dict           # cfg["circuit_concrete"]
    folder: Path             # <run dir>/circuit_concrete
    alpha: float             # strength of the slot edit in the lookup runs
    min_gap: float | None    # see ``is_clean_flip``
    batch: int


def unpatched_run(engine, ids: list[int], n_ctx: int, person_token: int, own_token: int,
                  mlp: bool = False) -> LookupRun:
    """One run of a question on its own, recorded at every token after the records (with ``mlp``, the MLPs
    too)."""
    after_records = list(range(n_ctx, len(ids)))
    _, records_state, _ = engine.run([ids[:n_ctx]], edits=[[]])
    logits, _, hidden = engine.run([ids[n_ctx:]], state=records_state, keep=False, capture=[after_records],
                                   zcapture=[after_records], mcapture=[after_records] if mlp else None)
    margin = float(logits[0, person_token] - logits[0, own_token])
    mlp_out = engine.mcaptured[0][1] if mlp else None
    return LookupRun(margin, hidden[0][1], engine.zcaptured[0][1], mlp_out=mlp_out)


def group_positions(pair: dict, query: str, site: str) -> tuple[list[int], list[int]]:
    """One of ``QUESTION_SITES`` in one question: (token positions, the same as rows of a run's recorded
    arrays)."""
    cut = pair["queries"][query]["cut"]
    offsets = [-1] if site == "phrase_last" else pair["groups"][site]
    return [cut + offset for offset in offsets], [cut - pair["n_ctx"] + offset for offset in offsets]


def after_phrase(pair: dict, query: str) -> tuple[list[int], list[int]]:
    """Every token of a question after the phrase: (token positions, rows of a run's recorded arrays)."""
    question, n_ctx = pair["queries"][query], pair["n_ctx"]
    cut, end = question["cut"], len(question["ids"])
    return list(range(cut, end)), list(range(cut - n_ctx, end - n_ctx))


def lookups_in_both_orders(setup: ConcreteSetup, case: str, arm: str) -> dict:
    """{order: (pair, runs)} for the first disputed field of an arm's prompt in both page orders; empty unless
    both orders tokenise and ask about the same field. One order is the other's donor: the same question over
    the same records in the other places."""
    found = {}
    cell_prompts = case_cells(case, setup.cases, setup.cfg)
    for order in binding.ORDERS:
        prompt = cell_prompts[(arm, order)]
        disputed = prompts.diff_fields(prompt)
        if not disputed:
            continue
        code, person_value, own_value = disputed[0]
        pair = lookup_pair(setup.engine, prompt, code, person_value, own_value)
        if pair is not None:
            found[order] = (pair, lookup_runs(setup.engine, setup.vectors, arm, pair, setup.alpha))
    codes = {pair["code"] for pair, _ in found.values()}
    return found if len(found) == 2 and len(codes) == 1 else {}


def crossed_field_pairs(setup: ConcreteSetup, case: str, arm: str, order: str) -> tuple[dict, dict] | None:
    """The lookup pairs of a cell's first two disputed fields, or None. The second field's question is the
    donor for the first's: the same record asked, another field. With ``require_crossed`` only prompts count
    whose two fields are yes/no fields with crossed values, so that the first field's answer tokens also tell
    the second field's values apart."""
    prompt = case_cells(case, setup.cases, setup.cfg)[(arm, order)]
    disputed = prompts.diff_fields(prompt)
    if len(disputed) < 2:
        return None
    (first_code, first_person, first_own), (second_code, second_person, second_own) = disputed[:2]
    yes_and_no = {first_person, first_own} == {"Yes", "No"}
    is_crossed = yes_and_no and second_person == first_own and second_own == first_person
    if setup.settings["require_crossed"] and not is_crossed:
        return None
    first = lookup_pair(setup.engine, prompt, first_code, first_person, first_own)
    second = lookup_pair(setup.engine, prompt, second_code, second_person, second_own)
    if first is None or second is None:
        return None
    if any(len(first["groups"][group]) != len(second["groups"][group]) for group in ("post", "tail", "last")):
        return None
    return first, second


def field_donor_runs(engine, asked: dict, donor: dict, query: str) -> tuple[LookupRun, LookupRun]:
    """The question about the asked field and the same question about the donor field, both read with the
    asked field's answer tokens."""
    asked_ids, donor_ids = asked["queries"][query]["ids"], donor["queries"][query]["ids"]
    base = unpatched_run(engine, asked_ids, asked["n_ctx"], asked["tp"], asked["to"])
    donor_run = unpatched_run(engine, donor_ids, donor["n_ctx"], asked["tp"], asked["to"])
    return base, donor_run


def step_test_row(lookup: dict, item: dict, patched: float) -> dict:
    return dict(lookup, test=item["test"], group=item["group"], layer=item["layer"], variant=item["variant"],
                patched=patched)


# -- 1. Fit patients: the place and owner vectors, and each head against the order donor

def add_place_and_owner_sums(sums: dict, both_orders: dict) -> None:
    """Add the lookups' residual streams at each question site to running sums keyed (kind, site, side): by
    the asked record's place (first +1, second -1) and by its owner (PERSON +1, the model -1). The labelled
    arm in both orders and both questions balances the two."""
    for pair, runs in both_orders.values():
        for query in prompts.QUERIES:
            for site in QUESTION_SITES:
                _, rows = group_positions(pair, query, site)
                state = runs[(query, "clean")].resid[:, rows].float().mean(1)
                add_to_running_sum(sums, ("place", site, place_sign(pair, query)), state)
                add_to_running_sum(sums, ("owner", site, sign(query)), state)


def place_owner_random_vectors(setup: ConcreteSetup, sums: dict) -> dict:
    """{"place|<site>", "owner|<site>", "random|<site>": [layers + 1, hidden]}: the mean residual stream of
    lookups about the first record minus the second (place), about PERSON's minus the model's (owner), and a
    random direction with the place vector's norm at every layer."""
    torch = setup.engine.torch
    found = {}
    for kind in ("place", "owner"):
        for site in QUESTION_SITES:
            if (kind, site, 1) not in sums or (kind, site, -1) not in sums:
                continue
            plus_total, plus_count = sums[(kind, site, 1)]
            minus_total, minus_count = sums[(kind, site, -1)]
            found[f"{kind}|{site}"] = (plus_total / plus_count - minus_total / minus_count).cpu()
    generator = torch.Generator().manual_seed(int(setup.cfg["circuit"]["seed"]) + 7)
    for site in QUESTION_SITES:
        if f"place|{site}" in found:
            noise = torch.randn(setup.engine.hidden, generator=generator)
            direction = torch.nn.functional.normalize(noise, dim=-1)
            found[f"random|{site}"] = direction[None, :] * found[f"place|{site}"].norm(dim=-1, keepdim=True)
    return found


class OrderDonorLookup(NamedTuple):
    """A lookup of the labelled arm and its order donor: the same question with the records in the other page
    order."""
    order: str
    pair: dict
    query: str
    base: float               # the lookup's margin
    other: float              # the margin of the other question over the same records
    donor_pair: dict
    donor: LookupRun          # the order donor's unpatched run
    counts: bool              # the lookup counts (``is_clean_flip``) and its donor is answered right


def order_donor_lookups(setup: ConcreteSetup, both_orders: dict):
    """Each lookup of ``lookups_in_both_orders`` with its order donor, as ``OrderDonorLookup``."""
    for order, (pair, runs) in both_orders.items():
        other_order = [candidate for candidate in both_orders if candidate != order][0]
        donor_pair, donor_runs = both_orders[other_order]
        for query in prompts.QUERIES:
            base = runs[(query, "clean")].margin
            other_question = runs[(OTHER_QUERY[query], "clean")].margin
            donor = donor_runs[(query, "clean")]
            counts = bool(is_clean_flip(sign(query), base, other_question, setup.min_gap)
                          and sign(query) * donor.margin > 0)
            yield OrderDonorLookup(order, pair, query, base, other_question, donor_pair, donor, counts)


def order_scan_rows(setup: ConcreteSetup, case: str, both_orders: dict, head_columns: dict) -> list[dict]:
    """Each head patched alone from the order donor, at the tokens after the phrase. Only lookups that count
    are scanned."""
    rows = []
    for lookup in order_donor_lookups(setup, both_orders):
        if not lookup.counts:
            continue
        positions, _ = after_phrase(lookup.pair, lookup.query)
        _, donor_rows = after_phrase(lookup.donor_pair, lookup.query)
        donor_out = lookup.donor.head_out[:, donor_rows]
        items = [dict(query=lookup.query, zpatch=[(positions, donor_out, columns)])
                 for columns in head_columns.values()]
        patched = run_items(setup.engine, lookup.pair, items, setup.batch)
        fields = dict(case_id=case, block_order=lookup.order, query=lookup.query)
        rows += [dict(fields, layer=layer, head=head, base=lookup.base, other=lookup.other, patched=margin)
                 for (layer, head), margin in zip(head_columns, patched)]
    return rows


def fit_place_vectors(setup: ConcreteSetup) -> None:
    """Fit patients, labelled arm: writes the place, owner and random vectors (``place.pt``) and the scan of
    every head against the order donor (``order_scan.parquet``, on the first ``scan_patients``)."""
    engine, settings = setup.engine, setup.settings
    labelled = setup.cfg["pairs"]["false"][1]
    started = time.time()
    sums, scan_rows = {}, []
    head_columns = {head: engine.head_dims([head]) for head in all_heads(engine)}
    for index, case in enumerate(fit_patients(setup.cases)[: int(settings["fit_patients"])]):
        both_orders = lookups_in_both_orders(setup, case, labelled)
        add_place_and_owner_sums(sums, both_orders)
        if index < int(settings["scan_patients"]):
            scan_rows += order_scan_rows(setup, case, both_orders, head_columns)
    write(setup.folder / "order_scan.parquet", scan_rows)
    engine.torch.save(place_owner_random_vectors(setup, sums), setup.folder / "place.pt")
    print(f"[circuit concrete] place and owner vectors, order scan ({len(scan_rows)} patched runs), "
          f"{time.time() - started:.1f}s", flush=True)


# -- 2. Fit patients: each head against the field donor

def field_scan_rows(setup: ConcreteSetup, case: str, arm: str, order: str, query: str, crossed: tuple,
                    head_columns: dict) -> list[dict] | None:
    """Each head patched alone from the field donor, at the tokens after the field's name. None if the lookup
    does not count."""
    asked, donor_pair = crossed
    base, donor = field_donor_runs(setup.engine, asked, donor_pair, query)
    if not is_clean_flip(sign(query), base.margin, donor.margin, setup.min_gap):
        return None
    positions, donor_rows = [], []
    for group in ("post", "tail", "last"):
        positions += group_positions(asked, query, group)[0]
        donor_rows += group_positions(donor_pair, query, group)[1]
    donor_out = donor.head_out[:, donor_rows]
    items = [dict(query=query, zpatch=[(positions, donor_out, columns)]) for columns in head_columns.values()]
    patched = run_items(setup.engine, asked, items, setup.batch)
    lookup = dict(case_id=case, arm=arm, block_order=order, query=query)
    return [dict(lookup, layer=layer, head=head, base=base.margin, donor=donor.margin, patched=margin)
            for (layer, head), margin in zip(head_columns, patched)]


def field_scan(setup: ConcreteSetup) -> None:
    """Fit patients whose two disputed fields are crossed, up to ``field_patients`` of them: writes the scan
    of every head against the field donor (``field_scan.parquet``)."""
    plain, labelled = setup.cfg["pairs"]["false"]
    cells = ((labelled, "person_first"), (labelled, "own_first"), (plain, "person_first"))
    started = time.time()
    rows, used = [], 0
    head_columns = {head: setup.engine.head_dims([head]) for head in all_heads(setup.engine)}
    for case in fit_patients(setup.cases):
        if used >= int(setup.settings["field_patients"]):
            break
        scanned = False
        for arm, order in cells:
            crossed = crossed_field_pairs(setup, case, arm, order)
            if crossed is None:
                continue
            for query in prompts.QUERIES:
                query_rows = field_scan_rows(setup, case, arm, order, query, crossed, head_columns)
                if query_rows is None:
                    continue
                rows += query_rows
                scanned = True
        used += scanned
    write(setup.folder / "field_scan.parquet", rows)
    print(f"[circuit concrete] field scan on {used} patients ({len(rows)} patched runs), "
          f"{time.time() - started:.1f}s", flush=True)


# -- 3. Held-out patients: the steps of the lookup

def order_donor_rows(setup: ConcreteSetup, case: str) -> list[dict]:
    """Labelled arm: the residual stream after one block, at one question site (or at every token after the
    phrase: "suffix"), taken from the order donor. Where the record's place is carried, block by block."""
    labelled = setup.cfg["pairs"]["false"][1]
    rows = []
    for lookup in order_donor_lookups(setup, lookups_in_both_orders(setup, case, labelled)):
        pair, query, donor_pair = lookup.pair, lookup.query, lookup.donor_pair
        fields = dict(case_id=case, arm=labelled, block_order=lookup.order, query=query, base=lookup.base,
                      other=lookup.other, ok=lookup.counts)
        items = []
        for site in QUESTION_SITES + ["suffix"]:
            if site == "suffix":
                positions, _ = after_phrase(pair, query)
                _, donor_rows = after_phrase(donor_pair, query)
            else:
                positions, _ = group_positions(pair, query, site)
                _, donor_rows = group_positions(donor_pair, query, site)
            items += block_by_block(setup.engine, positions, lookup.donor.resid[:, donor_rows], query=query,
                                    test="order", group=site, variant="")
        patched = run_items(setup.engine, pair, items, setup.batch)
        rows += [step_test_row(fields, item, margin) for item, margin in zip(items, patched)]
    return rows


def bar_blocks(n_heads: int, keys: list[int], first_block: int, last_block: int) -> dict:
    """A knockout: every head of blocks ``first_block``..``last_block`` (counted from 1) barred from ``keys``
    (none without keys)."""
    every_head = list(range(n_heads))
    return {layer: (every_head, keys) for layer in range(int(first_block) - 1, int(last_block))} if keys else {}


def knock_of(heads, keys: list[int]) -> dict:
    """A knockout {layer: (heads, keys)}: the listed (layer, head) barred from ``keys``."""
    spec = {}
    for layer, head in heads:
        spec.setdefault(int(layer), ([], keys))[0].append(int(head))
    return spec if keys else {}


def knockout_items(engine, pair: dict, query: str, windows: list, reader_sets: dict) -> list[dict]:
    """Attention knockouts for one lookup, each over a window of blocks.

    knock_from_question   from the first question token on, every head barred from the headers or end markers:
                          where the owner's record is found
    knock_from_tail       from the template's tail on, every head barred from a part of the question or from a
                          record's lines: what the last tokens still need
    knock_heads           the reader heads, or a random set like them, barred from a record's lines"""
    question = pair["queries"][query]
    layout, cut, groups = question["lay"], question["cut"], pair["groups"]
    queried, other = layout.block(query), layout.block(OTHER_QUERY[query])
    first, second = layout.blocks[0], layout.blocks[1]
    record_keys = {"both_headers": first.header + second.header, "queried_header": queried.header,
                   "other_header": other.header, "end_markers": first.extra + second.extra,
                   "queried_lines": queried.line_tokens_all(), "other_lines": other.line_tokens_all()}
    question_keys = {"phrase": list(range(pair["start"], cut)),
                     "after_phrase": [cut + offset for offset in groups["pre"]],
                     "field_name": [cut + offset for offset in groups["field"]],
                     "rest_of_question": [cut + offset for offset in groups["post"]],
                     "queried_lines": record_keys["queried_lines"], "other_lines": record_keys["other_lines"]}
    items = []
    for name in ("both_headers", "queried_header", "other_header", "end_markers"):
        if not record_keys[name]:
            continue
        items += [dict(test="knock_from_question", group=name, layer=high, variant=f"{low}-{high}",
                       knock_q=bar_blocks(engine.n_heads, record_keys[name], low, high)) for low, high in windows]
    for name, keys in question_keys.items():
        items += [dict(test="knock_from_tail", group=name, layer=high, variant=f"{low}-{high}",
                       knock_t=bar_blocks(engine.n_heads, keys, low, high)) for low, high in windows]
    for set_name, heads in reader_sets.items():
        for name in ("queried_lines", "other_lines"):
            items.append(dict(test="knock_heads", group=name, layer=0, variant=set_name,
                              knock_t=knock_of(heads, record_keys[name])))
    return items


def knockouts_from_tail(item: dict) -> dict:
    """The knockouts in force from the template's tail on: the item's ``knock_q`` and its ``knock_t``
    together."""
    from_question = item.get("knock_q") or {}
    merged = {layer: (list(heads), list(keys)) for layer, (heads, keys) in from_question.items()}
    for layer, (heads, keys) in (item.get("knock_t") or {}).items():
        if layer in merged:        # (the two are not used together with different head sets)
            merged[layer] = (merged[layer][0], sorted(set(merged[layer][1]) | set(keys)))
        else:
            merged[layer] = (list(heads), list(keys))
    return merged


def run_with_knockouts(engine, pair: dict, query: str, items: list[dict], batch: int) -> list[float]:
    """The margin of each item under its knockouts. ``knock_q`` holds from the first question token on;
    ``knock_t`` from the template's tail on, so its keys may be question tokens."""
    question = pair["queries"][query]
    ids, n_ctx = question["ids"], pair["n_ctx"]
    tail_start = question["cut"] + pair["groups"]["tail"][0]
    _, records_state, _ = engine.run([ids[:n_ctx]])

    def run_batch(part):
        """The margins of one batch of items: the question under ``knock_q``, then the tail under both."""
        logits = run_from(engine, records_state, [0] * len(part),
                          (ids[n_ctx:tail_start], dict(knock=[item.get("knock_q") or None for item in part])),
                          (ids[tail_start:], dict(knock=[knockouts_from_tail(item) for item in part])))
        return value_margins(logits, pair)

    return engine._halving(items, batch, run_batch)


def mlp_swap_items(engine, pair: dict, query: str, base: LookupRun, donor: LookupRun) -> list[dict]:
    """The MLPs' outputs at the tokens after the phrase taken from the other question's run: one block at a
    time, every block, every block with the heads held at their base outputs, and the complement of that
    (every head from the other question, the MLPs held at base). The last two say how much of the answer the
    MLPs carry alone."""
    positions, base_rows = after_phrase(pair, query)
    _, donor_rows = after_phrase(pair, OTHER_QUERY[query])
    donor_mlp, donor_heads = donor.mlp_out[:, donor_rows], donor.head_out[:, donor_rows]
    swap = dict(query=query, test="mlp_swap", variant="")
    items = [dict(swap, group="one block", layer=layer + 1, mpatch=[(positions, donor_mlp, [layer])])
             for layer in range(engine.n_layers)]
    items.append(dict(swap, group="every block", layer=0, mpatch=[(positions, donor_mlp, None)]))
    items.append(dict(swap, group="every block, heads held at base", layer=0,
                      mpatch=[(positions, donor_mlp, None)],
                      zpatch=[(positions, base.head_out[:, base_rows], None)]))
    items.append(dict(swap, group="no block, every head from the other question", layer=0,
                      mpatch=[(positions, base.mlp_out[:, base_rows], None)],
                      zpatch=[(positions, donor_heads, None)]))
    return items


def added_vector_items(kind: str, vector, direction: float, strengths, windows, sites, **fields) -> list[dict]:
    """Items that add ``vector`` (a place, owner or random vector) times ``direction`` and a strength, in a
    window of blocks, at the positions of one site: every strength, window and site of ``sites`` ((group,
    positions) pairs), with ``fields``."""
    items = []
    for strength in strengths:
        for low, high in windows:
            for group, positions in sites:
                edit = Edit("add", positions, vector, scale=direction * float(strength), layers=(low, high))
                items.append(dict(fields, test=f"add_{kind}", group=group, layer=high,
                                  variant=f"{low}-{high}@{strength:g}", resid=[edit]))
    return items


def vector_items(pair: dict, query: str, place_vectors: dict, settings: dict) -> list[dict]:
    """The place, owner and random vectors added at the answer token ("last") or the template's tail, pointing
    away from the asked record: to the other place (place, random) or the other owner."""
    cut = pair["queries"][query]["cut"]
    windows = [(layer, layer) for layer in settings["vector_layers"]]         # single blocks, then windows
    windows += [tuple(window) for window in settings["vector_windows"]]
    items = []
    for site in ("last", "tail"):
        positions = [cut + offset for offset in pair["groups"][site]]
        for kind in ("place", "owner", "random"):
            vector = place_vectors.get(f"{kind}|{site}")
            if vector is None:
                continue
            away = -sign(query) if kind == "owner" else -place_sign(pair, query)
            items += added_vector_items(kind, vector, away, settings["vector_alphas"], windows,
                                        [(site, positions)], query=query)
    return items


def mlp_direct_contributions(engine, base: LookupRun, pair: dict, query: str):
    """Each block's MLP's direct contribution to the right answer at the answer token [layers]."""
    with engine.torch.no_grad():
        readout = answer_readout(engine, base, pair)
        return (base.mlp_out[:, -1].float() @ readout).cpu().numpy() * sign(query)


def lookup_step_rows(setup: ConcreteSetup, case: str, arm: str, order: str, pair: dict, query: str,
                     place_vectors: dict, reader_sets: dict, windows: list) -> list[dict]:
    """One lookup under the knockouts, the MLP swaps and the added vectors, and its MLPs' direct
    contributions."""
    engine = setup.engine
    ids, other_ids = pair["queries"][query]["ids"], pair["queries"][OTHER_QUERY[query]]["ids"]
    base = unpatched_run(engine, ids, pair["n_ctx"], pair["tp"], pair["to"], mlp=True)
    donor = unpatched_run(engine, other_ids, pair["n_ctx"], pair["tp"], pair["to"], mlp=True)
    counts = bool(is_clean_flip(sign(query), base.margin, donor.margin, setup.min_gap))
    lookup = dict(case_id=case, arm=arm, block_order=order, query=query, base=base.margin, other=donor.margin,
                  ok=counts)

    items = knockout_items(engine, pair, query, windows, reader_sets)
    patched = run_with_knockouts(engine, pair, query, items, int(setup.settings["knock_batch"]))
    rows = [step_test_row(lookup, item, margin) for item, margin in zip(items, patched)]

    items = mlp_swap_items(engine, pair, query, base, donor)
    items += vector_items(pair, query, place_vectors, setup.settings)
    patched = run_items(engine, pair, items, setup.batch)
    rows += [step_test_row(lookup, item, margin) for item, margin in zip(items, patched)]

    direct = mlp_direct_contributions(engine, base, pair, query)
    rows += [dict(lookup, test="mlp_direct", group="", layer=layer + 1, variant="",
                  patched=float(direct[layer])) for layer in range(engine.n_layers)]
    return rows


def field_donor_rows(setup: ConcreteSetup, case: str, arm: str, order: str) -> list[dict]:
    """Cells whose two disputed fields are crossed: the residual stream after one block, at one group of
    tokens after the field's name, taken from the field donor. Where the asked field is carried, block by
    block."""
    crossed = crossed_field_pairs(setup, case, arm, order)
    if crossed is None:
        return []
    asked, donor_pair = crossed
    engine, rows = setup.engine, []
    for query in prompts.QUERIES:
        base, donor = field_donor_runs(engine, asked, donor_pair, query)
        counts = bool(is_clean_flip(sign(query), base.margin, donor.margin, setup.min_gap))
        lookup = dict(case_id=case, arm=arm, block_order=order, query=query, base=base.margin,
                      other=donor.margin, ok=counts)
        after_field = ("post", "tail", "last")
        sites = {}          # site -> (positions in the asked field's question, rows of the donor's arrays)
        for group in after_field:
            positions, _ = group_positions(asked, query, group)
            _, donor_rows = group_positions(donor_pair, query, group)
            sites[group] = (positions, donor_rows)
        sites["field_last"] = (group_positions(asked, query, "field")[0][-1:],
                               group_positions(donor_pair, query, "field")[1][-1:])
        sites["after_field"] = (sum((sites[group][0] for group in after_field), []),
                                sum((sites[group][1] for group in after_field), []))
        items = [item for site, (positions, donor_rows) in sites.items()
                 for item in block_by_block(engine, positions, donor.resid[:, donor_rows], query=query,
                                            test="field", group=site, variant="")]
        patched = run_items(engine, asked, items, setup.batch)
        rows += [step_test_row(lookup, item, margin) for item, margin in zip(items, patched)]
    return rows


def reader_and_random_sets(engine, readers: list, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    random_sets = {f"random{draw}": random_like(readers, engine.n_heads, rng) for draw in range(2)}
    return {"readers": readers} | random_sets


def test_lookup_steps(setup: ConcreteSetup, place_vectors: dict) -> None:
    """Held-out patients: every step test on lookups (``test``, one file per patient)."""
    engine, settings = setup.engine, setup.settings
    readers = []
    if (setup.run_dir / "circuit_algorithm" / "attention.parquet").exists():
        readers = reader_heads(setup.run_dir, float(settings["reader_attention"]))
    seed = int(setup.cfg["circuit"]["seed"]) + 8
    reader_sets = reader_and_random_sets(engine, readers, seed) if readers else {}
    windows = interventions.windows(engine.n_layers, int(settings["parts"])) + [(1, engine.n_layers)]

    def run_patient(case):
        rows = order_donor_rows(setup, case)
        for arm, order, pair in pairs_of(engine, case_cells(case, setup.cases, setup.cfg), 1):
            for query in prompts.QUERIES:
                rows += lookup_step_rows(setup, case, arm, order, pair, query, place_vectors, reader_sets,
                                         windows)
            rows += field_donor_rows(setup, case, arm, order)
        write(setup.folder / "test" / f"{case}.parquet", rows)
        return f"test patients, {len(rows)} patched runs"

    patients = held_out_patients(setup.cases, setup.cfg, settings)
    each_patient(unfinished(patients, setup.folder / "test"), "circuit concrete", run_patient, counted=False)


# -- 4. Replies: where the labelling difference enters, and the place vector at the writing token

def reply_vector_items(probe: dict, place_vectors: dict, settings: dict) -> list[dict]:
    """The place, owner and random vectors added at the writing token, or at every reply token, pointing away
    from the record whose value the reply wrote."""
    ids = probe["ids"]
    writing_token = [len(ids) - 1]
    reply_tokens = list(range(probe["prompt_len"], len(ids)))
    sites = (("writing token", writing_token), ("every reply token", reply_tokens))
    to_other_place = -1.0 if probe["first_slot"] == 0 else 1.0
    to_other_owner = 1.0 if probe["wrote"] == "model's" else -1.0      # owner vector: PERSON minus the model
    windows = [tuple(window) for window in settings["vector_windows"]]
    items = []
    for kind in ("place", "owner", "random"):
        vector = place_vectors.get(f"{kind}|last")
        if vector is None:
            continue
        direction = to_other_owner if kind == "owner" else to_other_place
        items += added_vector_items(kind, vector, direction, settings["vector_alphas"], windows, sites)
    return items


def label_donor_items(engine, probe: dict, donor_resid) -> list[dict]:
    """The residual stream after one block, at one stretch of the question or the reply, taken from the run
    after the other arm's records (labelled for an unlabelled prompt): where what labelling changes enters."""
    ids, n_ctx = probe["ids"], probe["n_ctx"]
    stretches = {"question": range(n_ctx, probe["q_end"]),
                 "template tail": range(probe["q_end"], probe["prompt_len"]),
                 "reply before the line": range(probe["prompt_len"], probe["line_tok"]),
                 "the line before the value": range(probe["line_tok"], len(ids) - 1),
                 "writing token": [len(ids) - 1]}
    items = []
    for group, stretch in stretches.items():
        positions = [position for position in stretch if n_ctx <= position < len(ids)]
        if not positions:
            continue
        value = donor_resid[:, [position - n_ctx for position in positions]]
        items += block_by_block(engine, positions, value, test="label", group=group, variant="")
    return items


def reply_step_rows(setup: ConcreteSetup, probe: dict, place_vectors: dict,
                    place_units: dict) -> tuple[list[dict], bool]:
    """One reply at its writing token: (rows, whether the label donor was usable).

    The margin is the logit of the model's value minus PERSON's. Rows: the projection of the writing token's
    state on the place vector, the margin with the vectors added, and (if the reply after the other arm's
    records writes the other value) the margin with the residual stream patched from that run."""
    engine, settings = setup.engine, setup.settings
    ids, n_ctx = probe["ids"], probe["n_ctx"]
    own_token, person_token = probe["own_tok"], probe["person_tok"]
    reply_length = len(ids) - n_ctx
    names, record_tokens = ["base"], [ids[:n_ctx]]
    if probe["twin"] is not None:
        names, record_tokens = names + ["label"], record_tokens + [probe["twin"]]
    _, records_state, _ = engine.run(record_tokens)
    recorded = [list(range(len(tokens), len(tokens) + reply_length)) for tokens in record_tokens]
    logits, _, hidden = engine.run([ids[n_ctx:]] * len(names), state=records_state, keep=False,
                                   capture=recorded)
    unpatched = margins(logits, own_token, person_token)
    wrote_sign = 1.0 if probe["wrote"] == "model's" else -1.0
    has_label_donor = len(unpatched) > 1
    counts = bool(has_label_donor and is_clean_flip(wrote_sign, unpatched[0], unpatched[1], setup.min_gap))
    lookup = {key: probe[key] for key in ("case_id", "arm", "block_order", "sample", "field", "wrote")}
    other_margin = unpatched[1] if has_label_donor else float("nan")
    lookup |= dict(base=unpatched[0], other=other_margin, tokens=reply_length, ok=counts)

    written_place = "first" if probe["first_slot"] == 0 else "second"
    rows = []
    for layer, unit in place_units.items():
        for row, name in enumerate(names):
            projection = float(hidden[row][1][layer, -1].float() @ unit)
            rows.append(dict(lookup, test="place_projection", group=name, layer=layer, variant=written_place,
                             patched=projection))

    items = reply_vector_items(probe, place_vectors, settings)
    if counts:
        items += label_donor_items(engine, probe, hidden[1][1])
    _, base_state, _ = engine.run([ids[:n_ctx]])
    patched = patched_margins(engine, base_state, items, int(settings["reply_batch"]), lambda item: ids[n_ctx:],
                              (own_token, person_token))
    rows += [step_test_row(lookup, item, margin) for item, margin in zip(items, patched)]
    return rows, counts


def test_reply_steps(setup: ConcreteSetup, place_vectors: dict) -> None:
    """Replies of the second half of the patients, fed to their first written disputed value: writes
    ``replies.parquet``. At most ``replies`` replies get the label donor's patches."""
    engine, settings = setup.engine, setup.settings
    torch = engine.torch
    started = time.time()
    rows, used = [], 0
    reply_settings = setup.cfg["circuit_fine"]["reply"]
    probes = list(reply_probes(engine, setup.run_dir, setup.cfg, setup.cases, setup.vectors,
                               reply_settings["folders"], setup.alpha, int(settings["reply_tokens"])))
    patients = sorted({probe["case_id"] for probe in probes})
    second_half = set(patients[len(patients) // 2:])
    place_units = {}
    if "place|last" in place_vectors:
        place_at_answer = place_vectors["place|last"]
        place_units = {layer: torch.nn.functional.normalize(place_at_answer[layer].float(), dim=-1)
                       for layer in settings["vector_layers"]}
    for probe in probes:
        if probe["case_id"] not in second_half or used >= int(settings["replies"]):
            continue
        probe_rows, used_label_donor = reply_step_rows(setup, probe, place_vectors, place_units)
        rows += probe_rows
        used += used_label_donor
    write(setup.folder / "replies.parquet", rows)
    seconds = time.time() - started
    print(f"[circuit concrete] replies: {used} with the label donor, {len(rows)} rows, {seconds:.1f}s",
          flush=True)


def stage_concrete(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Each step of the lookup made concrete, in four parts (a part is skipped once its file exists).

    1. Fit patients: the place and owner vectors at each question site, and every head patched alone from
       the order donor, the same question with the records in the other page order (``place.pt``,
       ``order_scan.parquet``).
    2. Fit patients: every head patched alone from the field donor, the same record asked for another field
       (``field_scan.parquet``). The two scans give the heads that carry the place and those that carry the
       value.
    3. Held-out patients: residual patches from both donors by block and token group; knockouts of attention
       to headers, end markers, the phrase and the field's name; each block's MLP from the other question;
       the place, owner and random vectors added at the answer token (``test``).
    4. Replies at their writing token: residual patches from the run after the other arm's records, by block
       and stretch of text, and the place vector (``replies.parquet``)."""
    settings, circuit_settings = cfg["circuit_concrete"], cfg["circuit"]
    folder = run_dir / "circuit_concrete"
    vectors = load_vectors(run_dir, engine.device)
    folder.mkdir(exist_ok=True)
    setup = ConcreteSetup(engine, run_dir, cfg, cases, vectors, settings, folder,
                          alpha=float(circuit_settings["alpha"]), min_gap=circuit_settings["min_gap"],
                          batch=int(settings["batch"]))
    if not (folder / "place.pt").exists():
        fit_place_vectors(setup)
    place_vectors = load_place_vectors(engine, run_dir)
    if not (folder / "field_scan.parquet").exists():
        field_scan(setup)
    test_lookup_steps(setup, place_vectors)
    if not (folder / "replies.parquet").exists():
        test_reply_steps(setup, place_vectors)


# -- Tables of stage concrete, and the head classes its two scans give

def scan_effects(run_dir: Path, scan: str) -> pd.Series | None:
    """Per head, its mean single-head effect in the "order" or the "field" scan of ``stage_concrete``; None if
    the scan has not been written or is empty."""
    path = run_dir / "circuit_concrete" / f"{scan}_scan.parquet"
    if not path.exists():
        return None
    rows = pd.read_parquet(path)
    if rows.empty:
        return None
    donor_column = {"order": "other", "field": "donor"}[scan]
    rows["share"] = effect_of(rows.patched, rows.base, rows[donor_column])
    return rows.groupby(["layer", "head"]).share.mean()


def head_classes(run_dir: Path, n_place: int = 16, n_value: int = 32) -> dict:
    """Head sets from the two scans of ``stage_concrete``: the "place carriers" (the top heads against the
    order donor) and the "value heads" (the top heads against the field donor)."""
    classes = {}
    for scan, name, size in (("order", "place carriers", n_place), ("field", "value heads", n_value)):
        effect = scan_effects(run_dir, scan)
        if effect is not None:
            top = effect.sort_values(ascending=False).head(size)
            classes[name] = [(int(layer), int(head)) for layer, head in top.index]
    return classes


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
    classes = head_classes(run_dir)
    if "place carriers" in classes:
        sets["place16"] = classes["place carriers"]
    if "value heads" in classes:
        sets["value32"] = classes["value heads"]
    if (run_dir / "circuit_algorithm" / "attention.parquet").exists():
        sets["readers"] = reader_heads(run_dir, float(settings["reader_attention"]))
    if "place16" in sets and "readers" in sets:
        sets["place16+readers"] = sorted(set(sets["place16"]) | set(sets["readers"]))
    rng = np.random.default_rng(int(cfg["circuit"]["seed"]) + 25)
    for name in sorted(sets):
        for i in range(int(settings["randoms"])):
            sets[f"random_{name}_{i}"] = random_like(sets[name], engine.n_heads, rng)
    sets["all"] = all_heads(engine)
    return {name: [(int(layer), int(head)) for layer, head in heads] for name, heads in sets.items()}


def lookup_step_tables(test: pd.DataFrame) -> dict:
    """Per step test on lookups that count: the mean share of the way to the other answer and how often the
    answer flips (``concrete_lookup``), and the MLPs' direct contributions by block
    (``concrete_mlp_direct``)."""
    out = {}
    test["share"] = effect_of(test.patched, test.base, test.other)
    test["flipped"] = flipped(test)
    patched = test[test.ok & (test.test != "mlp_direct")]
    out["concrete_lookup"] = patched.groupby(["test", "group", "variant", "layer"]).agg(
        share=("share", "mean"), flipped=("flipped", "mean"), lookups=("share", "size")).reset_index()
    direct = test[test.ok & (test.test == "mlp_direct")]
    if not direct.empty:
        out["concrete_mlp_direct"] = direct.groupby("layer").agg(
            direct=("patched", "mean"), lookups=("patched", "size")).reset_index()
    return out


def scanned_heads_table(run_dir: Path) -> pd.DataFrame | None:
    """Per head: its effect and rank in the order and field scans, and its rank in the lookup ranking."""
    scans = [effect.rename(f"{scan}_effect") for scan in ("order", "field")
             if (effect := scan_effects(run_dir, scan)) is not None]
    if not scans:
        return None
    heads = pd.concat(scans, axis=1).reset_index()
    for column in [name for name in heads.columns if name.endswith("_effect")]:
        heads["rank_" + column[:-len("_effect")]] = heads[column].rank(ascending=False, method="first")
    ranking_path = run_dir / "circuit" / "selection.json"
    ranking = json.loads(ranking_path.read_text()) if ranking_path.exists() else None
    if ranking:
        place = {tuple(head): index + 1 for index, head in enumerate(ranking["query"])}
        heads["rank_lookup"] = [place.get((int(layer), int(head)), np.nan)
                                for layer, head in zip(heads.layer, heads["head"])]
    return heads.sort_values(heads.columns[2], ascending=False)


def reply_step_tables(replies: pd.DataFrame) -> dict:
    """The reply tests: the label donor's patches by stretch of text and block (``concrete_reply_label``), the
    added vectors (``concrete_reply_add``) and the projection on the place vector
    (``concrete_reply_projection``)."""
    out = {}
    replies["share"] = effect_of(replies.patched, replies.base, replies.other)
    replies["flipped"] = flipped(replies)
    replies["as_written"] = right_sign(replies) * replies.base > 0
    label = replies[(replies.test == "label") & replies.ok]
    if not label.empty:
        out["concrete_reply_label"] = label.groupby(["group", "layer"]).agg(
            share=("share", "mean"), replies=("share", "size")).reset_index()
    added = replies[replies.test.str.startswith("add_") & replies.as_written]
    if not added.empty:
        def mean_change(patched):
            """Mean change of the margin from the unpatched run."""
            return float((patched - added.loc[patched.index, "base"]).mean())
        out["concrete_reply_add"] = added.groupby(["test", "group", "variant"]).agg(
            flipped=("flipped", "mean"), margin_change=("patched", mean_change),
            replies=("flipped", "size")).reset_index()
    projection = replies[replies.test == "place_projection"]
    if not projection.empty:
        out["concrete_reply_projection"] = projection.groupby(
            ["arm", "block_order", "wrote", "variant", "group", "layer"]).agg(
            projection=("patched", "mean"), replies=("patched", "size")).reset_index()
    return out


def concrete_tables(run_dir: Path, cfg: dict) -> dict:
    """The tables of ``stage_concrete`` by name. A table is left out if its rows have not been written yet."""
    out, folder = {}, run_dir / "circuit_concrete"
    if not folder.is_dir():
        return out
    test = read(folder / "test")
    if not test.empty:
        out |= lookup_step_tables(test)
    heads = scanned_heads_table(run_dir)
    if heads is not None:
        out["concrete_heads"] = heads
    if (folder / "replies.parquet").exists():
        replies = pd.read_parquet(folder / "replies.parquet")
        if not replies.empty:
            out |= reply_step_tables(replies)
    return out


# ==== Sampled replies under the circuit's handles

def load_place_vectors(engine, run_dir: Path) -> dict:
    saved = engine.torch.load(run_dir / "circuit_concrete" / "place.pt", map_location="cpu")
    return {name: vector.to(engine.device) for name, vector in saved.items()}


def knockout_keys(layout: prompts.Layout, head_set: str, target: str) -> list[int]:
    """The tokens a ``stage_steer`` knockout bars its heads from.

    "headers": both records' headers and end markers, the field lines staying in sight. "first lines": as many
    tokens elsewhere, the first field line of each record. A record ("own", "person"): its field lines, or,
    for the head set "all", the whole record with header and end marker."""
    if target == "headers":
        return layout.header_tokens()
    if target == "first lines":
        return [token for block in layout.blocks for token in next(iter(block.lines.values())).tokens]
    block = layout.block(target)
    return block.record_tokens() if head_set == "all" else block.line_tokens_all()


def steer_job(prompt_row: dict, layout: prompts.Layout, condition: list, head_sets: dict, place_vectors: dict,
              window: tuple[int, int]) -> tuple[dict, Job]:
    """(row fields, sampling job) of one condition of ``stage_steer``:

    ["base", None, None]            the prompt as it is
    ["knock", head set, target]     the head set barred from ``knockout_keys`` from the question on
    [vector kind, strength, also]   the place, owner or random vector added to the prompt's last token and to
                                    every sampled token, in the block window; ``also``, if given, is
                                    "<head set>:<record>": that head set barred from the record's lines too"""
    kind, first, second = condition
    if kind == "base":
        return meta_of(prompt_row, part="reason", variant="base", kind="base"), Job(layout.ids)
    if kind == "knock":
        head_set, target = first, second
        meta = meta_of(prompt_row, part="reason", variant=f"knock:{head_set}:{target}", kind="knock",
                           site=target, donor=head_set)
        knock = (layout.n_ctx, knock_of(head_sets[head_set], knockout_keys(layout, head_set, target)))
        return meta, Job(layout.ids, knock=knock)
    strength, also_knock = float(first), second
    low, high = window
    vector = place_vectors[f"{kind}|last"]
    knock = None
    if also_knock:
        head_set, record = also_knock.split(":")
        knock = (layout.n_ctx, knock_of(head_sets[head_set], layout.block(record).line_tokens_all()))
    variant = f"steer:{kind}" + (f"+knock:{also_knock}" if also_knock else "") + f"@{low}-{high}"
    meta = meta_of(prompt_row, part="reason", variant=variant, kind="steer", site="answer",
                       alpha=strength, lo=low, hi=high)
    at_last_token = Edit("add", [len(layout.ids) - 1], vector, scale=strength, layers=(low, high))
    return meta, Job(layout.ids, [at_last_token], steer=(vector, strength, (low, high)), knock=knock)


def stage_steer(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Sampled reasoned replies under the circuit's two handles: the place vector at the answer position, and
    head sets barred from part of the page (see ``steer_job``). The head sets are the readers, two random sets
    like them, and the named sets of ``named_sets``. Rows are in the reasoning stage's format, so the
    analysis treats them as reasoning cells; one file per patient in the configured folder."""
    settings = cfg["circuit_steer"]
    engine.cast(settings["dtype"])
    place_vectors = load_place_vectors(engine, run_dir)
    readers = reader_heads(run_dir, float(settings["reader_attention"]))
    head_sets = reader_and_random_sets(engine, readers, int(cfg["circuit"]["seed"]) + 8)
    (run_dir / "circuit_concrete" / "reader_sets.json").write_text(json.dumps(head_sets))
    if "head_sets" in cfg:
        named = named_sets(run_dir, engine, cfg)
        head_sets |= {name: heads for name, heads in named.items() if name not in head_sets}
    window = tuple(settings["window"])
    folder = run_dir / settings["folder"]
    seed = int(experiment_config.load()["seed"])
    todo = patients_to_sample(cases, cfg, folder, int(settings["patients"]))

    def add_jobs(case, batch):
        prompt_rows = case_prompts(case, cases, cfg["pairs"]["false"])
        for side, order, conditions in settings["cells"]:
            prompt_row = prompt_rows[(cfg["pairs"]["false"][SIDE[side]], order, "C")]
            layout = prompts.layout(engine.tok, prompt_row["prompt"], engine.kw, prefix="")
            for condition in conditions:
                meta, job = steer_job(prompt_row, layout, condition, head_sets, place_vectors, window)
                batch.add(case, meta, job)

    sample_patients(engine, folder, todo, settings, seed, "circuit steer", add_jobs)


def paired_head_columns(engine, run_dir: Path, cfg: dict, n_heads: int) -> tuple[dict, int]:
    """({head set name: columns of the heads' outputs, None meaning every head}, size of the lookup set).

    The sets: the top ``n_heads`` lookup heads, a random set like them, every other head, every head, and the
    named sets of ``named_sets``. The first two (and the named ones) are saved beside the ranking."""
    seed = int(cfg["circuit"]["seed"]) + 11
    ranking = json.loads((run_dir / "circuit" / "selection.json").read_text())
    top = [tuple(head) for head in ranking["query"][:n_heads]]
    random_set = random_like(top, engine.n_heads, np.random.default_rng(seed))
    others = [head for head in all_heads(engine) if head not in set(top)]
    size = len(top)
    columns = {f"lookup{size}": engine.head_dims(top), f"random{size}": engine.head_dims(random_set),
               f"all but lookup{size}": engine.head_dims(others), "every head": None}
    saved = {f"lookup{size}": top, f"random{size}": random_set}
    (run_dir / "circuit" / "paired_sets.json").write_text(json.dumps(saved))
    if "head_sets" in cfg:
        named = named_sets(run_dir, engine, cfg)
        columns |= {name: engine.head_dims(heads) for name, heads in named.items()
                    if name not in columns and name != "all"}
        saved_named = {name: heads for name, heads in named.items() if name != "all"}
        (run_dir / "circuit" / "paired_sets_named.json").write_text(json.dumps(saved_named))
    return columns, size


def paired_jobs(engine, vectors: dict, prompt_rows: dict, cell: list, cfg: dict, columns: dict, size: int,
                first_index: int) -> list[tuple[dict | None, Job]]:
    """The jobs of one cell of ``stage_paired``, two per head set: the sampled stream (with its row fields)
    and, right after it, the stream that follows it over other records (row fields None).

    ``cell``: [pair of arms, side, order, donor, strength, head sets]. The donor "twin" is the other arm's
    records; otherwise the same records under the slot edit at ``strength``. A twin whose question tokenises
    differently gives no jobs. ``first_index``: the index the first job will have in the batch."""
    variant, side, order, donor, strength, set_names = cell
    arm = cfg["pairs"][variant][SIDE[side]]
    other_arm = cfg["pairs"][variant][1 - SIDE[side]]
    prompt_row = prompt_rows[(arm, order, "C")]
    layout = prompts.layout(engine.tok, prompt_row["prompt"], engine.kw, prefix="")
    if donor == "twin":
        twin_prompt = prompt_rows[(other_arm, order, "C")]["prompt"]
        twin = prompts.layout(engine.tok, twin_prompt, engine.kw, prefix="")
        if twin.ids[twin.n_ctx:] != layout.ids[layout.n_ctx:]:
            return []
        follower_ids, follower_split, follower_edits = twin.ids, twin.n_ctx, []
        sees = "the other arm's records"
    else:
        follower_ids, follower_split, sees = layout.ids, layout.n_ctx, "the slot-edited records"
        follower_edits = interventions.mean_edits(layout, vectors, "slot", arm, alpha=float(strength))
    jobs = []
    for set_name in set_names:
        set_name = set_name.format(n=size)
        meta = meta_of(prompt_row, part="reason", variant=f"paired: {set_name} see {sees}", kind="paired",
                           alpha=float(strength))
        jobs.append((meta, Job(layout.ids, split=layout.n_ctx)))
        leader_index = first_index + len(jobs) - 1
        jobs.append((None, Job(follower_ids, follower_edits, split=follower_split, follow=leader_index,
                               pair=columns[set_name])))
    return jobs


def stage_paired(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Sampled replies in which only a head set reads other records.

    Each reply is sampled from a prompt as it is, while a second stream reads the same tokens after other
    records (the other arm's, or the same records under the slot edit). At every token after the records the
    head set's outputs in the sampled stream are replaced by the second stream's. Rows are in the reasoning
    stage's format, for the sampled stream only; one file per patient in the configured folder
    (``reason_paired`` by default)."""
    settings = cfg["circuit_paired"]
    engine.cast(settings["dtype"])
    vectors = load_vectors(run_dir, engine.device)
    columns, size = paired_head_columns(engine, run_dir, cfg, int(settings["heads"]))
    folder = run_dir / settings.get("folder", "reason_paired")
    seed = int(experiment_config.load()["seed"])
    todo = patients_to_sample(cases, cfg, folder, int(settings["patients"]))
    arms = sorted({arm for variant, *_ in settings["cells"] for arm in cfg["pairs"][variant]})

    def add_jobs(case, batch):
        prompt_rows = case_prompts(case, cases, arms)
        for cell in settings["cells"]:
            cell_jobs = paired_jobs(engine, vectors, prompt_rows, cell, cfg, columns, size,
                                    first_index=len(batch.jobs))
            for meta, job in cell_jobs:
                batch.add(case, meta, job)

    sample_patients(engine, folder, todo, settings, seed, "circuit paired", add_jobs, paired=True)


def reference_jobs(engine, cases: pd.Series, case: str, cfg: dict, cells: list, arms: list,
                   first_index: int) -> tuple[list, list]:
    """One patient's reference prompts: (jobs, wanted).

    For each cell the four roles A, B, M and D are wanted; a prompt that several cells share (A and B show one
    record, whatever the page order) gets one job. ``wanted`` lists (patient, row fields, index of the job
    that samples the prompt); ``first_index`` is the index the first job will have in the batch."""
    frame = prompts.build(cases.loc[[case]], arms)
    frame = frame[frame.replicate == 0]
    jobs, wanted, job_of_prompt = [], [], {}
    for variant, side, order in cells:
        arm = cfg["pairs"][variant][SIDE[side]]
        for role in "ABMD":
            role_order = order if role in "MD" else "shared"
            match = (frame.arm == arm) & (frame.role == role) & (frame.block_order == role_order)
            prompt_row = frame[match].iloc[0].to_dict()
            prompt = prompt_row["prompt"]
            if prompt not in job_of_prompt:
                job_of_prompt[prompt] = first_index + len(jobs)
                jobs.append(Job(prompts.layout(engine.tok, prompt, engine.kw, prefix="").ids))
            meta = meta_of(prompt_row, part="refs", variant="base") | dict(cell_order=order)
            wanted.append((case, meta, job_of_prompt[prompt]))
    return jobs, wanted


def stage_references(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """The reference conditions sampled with this sampler: for the listed cells, the own-belief prompts A and
    B and the references M and D, sampled as the reasoning stage samples C. Rows are in the reasoning stage's
    format with the role kept (``refs_own``, one file per patient)."""
    settings = cfg["references"]
    engine.cast(settings["dtype"])
    folder = run_dir / "refs_own"
    seed = int(experiment_config.load()["seed"]) + 5
    todo = patients_to_sample(cases, cfg, folder, int(settings["patients"]))
    arms = sorted({cfg["pairs"][variant][SIDE[side]] for variant, side, _ in settings["cells"]})

    def add_jobs(case, batch):
        case_jobs, case_wanted = reference_jobs(engine, cases, case, cfg, settings["cells"], arms,
                                                first_index=len(batch.jobs))
        batch.jobs.extend(case_jobs)
        batch.wanted.extend(case_wanted)

    sample_patients(engine, folder, todo, settings, seed, "references", add_jobs)
