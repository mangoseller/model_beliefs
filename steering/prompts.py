"""Prompts and token layouts.

Every prompt is rebuilt from the released CKD data with ``belief_leakage.binding.transform``, so a cell here is
the binding experiment's cell, character for character. A ``Layout`` names the tokens of one encoded prompt:
each record block's header, its field lines (field name, value, line end) and end marker, the question, and
the chat-template tail with the forced answer prefix.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd

from belief_leakage import binding
from belief_leakage.readout import PREFIX, chat_text, encode_prompt, first_difference
from belief_leakage import config
from belief_leakage.binding import ROLES

TOKROLES = ("name", "val", "eol")   # within a field line; "hdr" is the last token of a block's header
QUERIES = ("person", "own")


# Patients and prompts
def patients(limit: int | None = None) -> pd.Series:
    """case_id -> 'fit' or 'test' (the binding experiment's split); ``limit`` keeps that many, half per
    split."""
    split = binding.patient_split(int(config.load()["seed"]))
    if limit:
        per_split = max(1, limit // 2)
        kept = [case for name in ("fit", "test") for case in sorted(split.index[split == name])[:per_split]]
        split = split.loc[sorted(kept)]
    return split


@lru_cache(maxsize=1)
def released_prompts() -> dict:
    """(patient, released condition, replicate) -> the released stated-probability prompt."""
    frame = binding.release_frame()
    frame = frame[frame.output == "probability/normal"]
    return {(row.case_id, row.condition, int(row.replicate)): row.prompt for row in frame.itertuples()}


def build(cases: pd.Series, arms, roles=ROLES) -> pd.DataFrame:
    """One row per (patient, arm, order, role, replicate) with the stated-probability prompt. A and B take no
    order and have two replicates; arms that share an own-belief version share those prompts."""
    released, rows = released_prompts(), []

    def released_prompt(case, source, output, replicate):
        return released[(case, source, replicate)]

    for case, arm, role, order in binding.prompt_cells(cases.index, arms, roles):
        prompts = binding.cell_prompts(case, arm, role, order, ("probability/normal",), released_prompt)
        for _, replicate, prompt in prompts:
            rows.append(dict(case_id=case, split=cases[case], arm=arm, block_order=order, role=role,
                             replicate=replicate, prompt=prompt))
    return pd.DataFrame(rows)


def diff_fields(prompt: str) -> list[tuple[str, str, str]]:
    """(code, PERSON's value, the model's value) of every field the two records both hold with different
    values."""
    fields = {block["owner"]: block["fields"] for block in binding.record_blocks(prompt)}
    if set(fields) != {"person", "own"}:
        return []
    person, own = fields["person"], fields["own"]
    return [(code, value, own[code]) for code, value in person.items() if code in own and own[code] != value]


def lookup_prompt(prompt: str, code: str, query: str) -> str:
    """The prompt's records followed by a question about one field of one record (``query``: whose)."""
    parsed = binding.parse(prompt)
    field_names = {}
    for _, lines in parsed.blocks:
        for name, field_code, _ in binding.FACT.findall("\n".join(lines)):
            field_names[field_code] = name
    who = f"the record {parsed.name} was shown" if query == "person" else "your own record"
    parsed.rest = [f'In {who}, what value is given for "{field_names[code]} ({code})"? '
                   f'Answer with the value only, exactly as it is written in that record.']
    return binding.render(parsed)


# Token layouts
@dataclass
class Line:
    """One field line of a record, "<name> (<code>) is <value>.", as token positions."""
    code: str
    value: str
    name: list[int] = field(default_factory=list)   # the field's name and code, up to "is"
    val: list[int] = field(default_factory=list)    # the value, from the space before it
    eol: list[int] = field(default_factory=list)    # the final "." (its token usually covers the newline too)

    @property
    def tokens(self) -> list[int]:
        return self.name + self.val + self.eol


@dataclass
class Block:
    """One record on the page."""
    owner: str                     # "person" or "own"
    slot: int                      # 0: first record block, 1: second
    header: list[int] = field(default_factory=list)
    lines: dict[str, Line] = field(default_factory=dict)
    extra: list[int] = field(default_factory=list)   # end marker and absence sentence

    def line_tokens_all(self) -> list[int]:
        return [token for line in self.lines.values() for token in line.tokens]

    def record_tokens(self) -> list[int]:
        """Every token of the record: header, field lines and end marker."""
        return self.header + self.line_tokens_all() + self.extra

    def of(self, tokrole: str, codes=None) -> list[int]:
        """The tokens with one role ("name", "val", "eol", or "hdr" for the header's last token), in every
        line or only in the lines of the fields ``codes``."""
        if tokrole == "hdr":
            return self.header[-1:]
        return [token for code, line in self.lines.items() if codes is None or code in codes
                for token in getattr(line, tokrole)]


@dataclass
class Layout:
    """The tokens of one encoded prompt, by what they are."""
    ids: list[int]
    blocks: list[Block]
    n_ctx: int                     # tokens before the question (the records)
    question: list[int]            # question and instruction tokens
    tail: list[int]                # chat-template tail and forced prefix
    name_q: int | None             # PERSON's name in the question

    def block(self, owner: str) -> Block:
        return next(block for block in self.blocks if block.owner == owner)

    @property
    def order(self) -> str:
        return "person_first" if self.blocks[0].owner == "person" else "own_first"

    def line_tokens(self, owners=QUERIES) -> list[int]:
        return sorted(token for block in self.blocks if block.owner in owners
                      for line in block.lines.values() for token in line.tokens)

    def header_tokens(self) -> list[int]:
        """Every record's header and end-marker tokens: where the page says whose record it is."""
        return [token for block in self.blocks for token in block.header + block.extra]


def tokens_in(starts: np.ndarray, begin: int, end: int) -> list[int]:
    """The tokens whose first character lies in [begin, end) of the prompt (``starts``: each token's first
    character, counted from the prompt's start)."""
    return np.flatnonzero((starts >= begin) & (starts < end)).tolist()


def record_block(paragraph: str, owner: str, slot: int, paragraph_start: int, starts: np.ndarray) -> Block:
    """A record paragraph as a ``Block``: its first line is the header, lines that state a field are field
    lines, anything else (end marker, absence sentence) is ``extra``."""
    block, line_start = Block(owner, slot), paragraph_start
    for index, text in enumerate(paragraph.split("\n")):
        line_end = line_start + len(text) + 1               # past the newline
        fact = binding.FACT.match(text)
        if index == 0:
            block.header = tokens_in(starts, line_start, line_end)
        elif fact:
            code, value = fact.group(2), fact.group(3)
            value_start = line_end - 3 - len(value)         # the space before the value
            full_stop = line_end - 2                        # the final '.'
            block.lines[code] = Line(code, value,
                                     name=tokens_in(starts, line_start, value_start),
                                     val=tokens_in(starts, value_start, full_stop),
                                     eol=tokens_in(starts, full_stop, line_end))
        else:
            block.extra += tokens_in(starts, line_start, line_end)
        line_start = line_end
    return block


def layout(tok, prompt: str, template_kwargs: dict, prefix: str = PREFIX,
           owners: list[str] | None = None) -> Layout:
    """Encode ``prompt`` in the chat template, followed by ``prefix``, and name its tokens. A token belongs to
    the segment its first character is in, so a line's final '.' token (which usually also covers the newline)
    is that line's ``eol``. ``owners``: take the first paragraphs as record blocks with these owners, whatever
    their headers say (prompts whose headers name no owner)."""
    ids, offsets, prompt_start, _ = encode_prompt(tok, prompt, template_kwargs, prefix)
    starts = offsets[:, 0] - prompt_start

    # the record paragraphs come first; the first paragraph after them starts the question
    blocks, question_start, name = [], None, None
    for paragraph, paragraph_start, owner, who in binding.paragraphs(prompt):
        name = name or who
        if owners is not None:
            owner = owners[len(blocks)] if len(blocks) < len(owners) and question_start is None else None
        if owner and question_start is None:    # (a paragraph before the first record is a preamble)
            blocks.append(record_block(paragraph, owner, len(blocks), paragraph_start, starts))
        elif blocks and question_start is None:
            question_start = paragraph_start
    if not blocks or question_start is None:
        raise ValueError("prompt needs a record block and a question")

    n_ctx = int(np.flatnonzero(starts >= question_start)[0])
    prompt_end = int(np.flatnonzero(starts < len(prompt))[-1]) + 1
    name_q = None
    if name:
        name_at = prompt.find(f" {name} ", question_start)
        if name_at >= 0:
            name_q = int(np.flatnonzero(starts < name_at + 1 + len(name))[-1])
    n_tokens = len(ids)
    return Layout(ids, blocks, n_ctx, list(range(n_ctx, prompt_end)),
                  list(range(prompt_end, n_tokens)), name_q)


def matching_line_tokens(lines_a: dict, lines_b: dict, tokroles=TOKROLES) -> tuple[list[int], list[int], int]:
    """Positions of the same field-line tokens (same field, role and index) in two records' lines, and how
    many line parts could not be matched because their token counts differ."""
    positions_a, positions_b, missed = [], [], 0
    for code, role in itertools.product(lines_a, tokroles):
        tokens_a = getattr(lines_a[code], role)
        tokens_b = getattr(lines_b[code], role) if code in lines_b else []
        if len(tokens_a) == len(tokens_b):
            positions_a += tokens_a
            positions_b += tokens_b
        else:
            missed += 1
    return positions_a, positions_b, missed


def aligned(layout_a: Layout, layout_b: Layout, owners=QUERIES,
            tokroles=TOKROLES) -> tuple[list[int], list[int], int]:
    """``matching_line_tokens`` over the records of ``owners`` in two layouts."""
    positions_a, positions_b, missed = [], [], 0
    for owner in owners:
        matched = matching_line_tokens(layout_a.block(owner).lines, layout_b.block(owner).lines, tokroles)
        positions_a += matched[0]
        positions_b += matched[1]
        missed += matched[2]
    return positions_a, positions_b, missed


def lookup_ids(tok, prompt: str, code: str, query: str, values: tuple[str, str], template_kwargs: dict):
    """(token ids, token of PERSON's value, token of the model's value) for a lookup question: the chat prompt
    followed by whatever the two values' tokens share, so the next token tells the two records apart. None if
    one value's tokens are a prefix of the other's."""
    return answer_ids(tok, chat_text(tok, lookup_prompt(prompt, code, query), template_kwargs), values)


def split_answer_tokens(sequences):
    """Shared prefix and the first distinct token per answer, if one position distinguishes all answers."""
    split = first_difference(sequences)
    if split is None:
        return None
    tokens = [ids[split] for ids in sequences]
    return (sequences[0][:split], tokens) if len(set(tokens)) == len(tokens) else None


def answer_ids(tok, chat: str, values: tuple[str, str]):
    """``lookup_ids`` for an already rendered chat string."""
    head = tok(chat, add_special_tokens=False)["input_ids"]
    first, second = (tok(chat + value, add_special_tokens=False)["input_ids"] for value in values)
    if first[:len(head)] != head or second[:len(head)] != head:
        return None
    found = split_answer_tokens([first, second])
    return (found[0], *found[1]) if found else None


def pointer_tokens(lay: Layout, code: str) -> dict | None:
    """owner -> the token of its line for the field at which the two records' values differ."""
    value_positions = {block.owner: block.lines[code].val for block in lay.blocks}
    value_ids = [[lay.ids[position] for position in positions] for positions in value_positions.values()]
    split = first_difference(value_ids)
    if split is None:
        return None
    return {owner: positions[split] for owner, positions in value_positions.items()}
