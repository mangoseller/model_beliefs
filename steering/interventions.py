"""Mean vectors of the record tokens, and the residual-stream edits built from them.

A mean vector (after Feng & Steinhardt, arXiv:2310.17191) is the difference of the average residual stream
between record tokens that differ in one respect only. There is one vector per layer and token role (the name,
value and line-end tokens of a record line, and the header's last token):

    slot|<arm>|<tokrole>                       second record minus first, the same record in both orders
    owner|<arm>|<tokrole>                      PERSON's record minus the model's, on lines both records share
    label|<variant>|<order>|<owner>|<tokrole>  labelled arm minus unlabelled arm, same record and position
    random<k>|<arm>|<tokrole>                  a random direction with the slot vector's norm at every layer

A transplant copies the residual stream of chosen tokens, at every layer, from another prompt of the same
patient.
"""
from __future__ import annotations

from itertools import product
from pathlib import Path

import pandas as pd

from belief_leakage import binding
from steering import prompts
from steering.engine import Edit

TOKEN_ROLES_AT = {"lines": prompts.TOKROLES, "hdr": ("hdr",), "both": prompts.TOKROLES + ("hdr",)}
GROUPS = ("person_lines", "own_lines", "lines", "hdr", "lines_hdr", "ctx_end", "question")
OTHER_ORDER = {"person_first": "own_first", "own_first": "person_first"}


class Means:
    """Running sums of pooled residual vectors [layers + 1, hidden] over the fit patients, keyed by
    (arm, order, prompt role, owner, token role, line set, half of the fit patients)."""

    def __init__(self):
        self.sum, self.n, self.cases = {}, {}, []

    def add(self, key: tuple, vector) -> None:
        if key in self.sum:
            self.sum[key] += vector
            self.n[key] += 1
        else:
            self.sum[key], self.n[key] = vector.clone(), 1

    def mean(self, arm, order, prole, owner, tokrole, lineset="all", half=None):
        """``half`` None: over both halves of the fit patients."""
        halves = (0, 1) if half is None else (half,)
        keys = [(arm, order, prole, owner, tokrole, lineset, one_half) for one_half in halves]
        keys = [key for key in keys if key in self.sum]
        if not keys:
            raise KeyError((arm, order, prole, owner, tokrole, lineset, half))
        return sum(self.sum[key] for key in keys) / sum(self.n[key] for key in keys)

    def arms(self) -> list[str]:
        return sorted({key[0] for key in self.sum})

    def save(self, path: Path) -> None:
        import torch
        sums = {"|".join(map(str, key)): vector.cpu() for key, vector in self.sum.items()}
        counts = {"|".join(map(str, key)): count for key, count in self.n.items()}
        torch.save({"sum": sums, "n": counts, "cases": self.cases}, path)

    @classmethod
    def load(cls, path: Path, device="cpu") -> "Means":
        import torch
        data, means = torch.load(path, map_location=device), cls()
        for name, vector in data["sum"].items():
            *group, half = name.split("|")
            key = (*group, int(half))
            means.sum[key], means.n[key] = vector, data["n"][name]
        means.cases = list(data["cases"])
        return means


def pool(lay: prompts.Layout, hidden, captured: list[int], prompt: str) -> dict:
    """One prompt's captured residual stream averaged over the tokens of each (owner, token role), over every
    line ("all") and over the lines both records share with the same value ("same"):
    {(owner, tokrole, lineset): vector [layers + 1, hidden]}."""
    column_of = {position: column for column, position in enumerate(captured)}
    disputed = {code for code, _, _ in prompts.diff_fields(prompt)}
    pooled = {}
    for block in lay.blocks:
        if len(lay.blocks) == 2:
            person_lines, own_lines = lay.block("person").lines, lay.block("own").lines
            shared = [code for code in block.lines
                      if code in person_lines and code in own_lines and code not in disputed]
        else:
            shared = list(block.lines)
        linesets = (("all", None), ("same", shared))
        for tokrole, (lineset, codes) in product(prompts.TOKROLES + ("hdr",), linesets):
            if tokrole == "hdr" and lineset == "same":
                continue
            positions = block.of(tokrole, codes)
            if not positions:
                continue
            columns = [column_of[position] for position in positions]
            pooled[(block.owner, tokrole, lineset)] = hidden[:, columns].float().mean(1)
    return pooled


def capture_positions(lay: prompts.Layout) -> list[int]:
    """Every line token and each header's last token."""
    return sorted(set(lay.line_tokens()) | {block.header[-1] for block in lay.blocks if block.header})


def build_vectors(means: Means, pairs: dict, half: int | None = None) -> dict:
    """Every slot, owner and label vector the means support. ``pairs``: variant -> (unlabelled arm, labelled
    arm). ``half``: use one half of the fit patients only (for the split-half reliability)."""
    vectors = {}
    for arm in means.arms():
        for tokrole in prompts.TOKROLES + ("hdr",):
            try:
                # an owner's record is the second one in the order named after the other owner
                vectors[f"slot|{arm}|{tokrole}"] = sum(
                    means.mean(arm, second, "C", owner, tokrole, half=half)
                    - means.mean(arm, OTHER_ORDER[second], "C", owner, tokrole, half=half)
                    for owner, second in (("person", "own_first"), ("own", "person_first"))) / 2
                lineset = "all" if tokrole == "hdr" else "same"
                vectors[f"owner|{arm}|{tokrole}"] = sum(
                    means.mean(arm, order, "C", "person", tokrole, lineset, half)
                    - means.mean(arm, order, "C", "own", tokrole, lineset, half)
                    for order in binding.ORDERS) / 2
            except KeyError:        # the arm has no such tokens
                continue
    for variant, (plain, labelled) in pairs.items():
        for order, owner, tokrole in product(binding.ORDERS, prompts.QUERIES, prompts.TOKROLES + ("hdr",)):
            try:
                vectors[f"label|{variant}|{order}|{owner}|{tokrole}"] = (
                    means.mean(labelled, order, "C", owner, tokrole, half=half)
                    - means.mean(plain, order, "C", owner, tokrole, half=half))
            except KeyError:        # one of the two arms has no such tokens
                continue
    return vectors


def random_like(vector, name: str, index: int, seed: int = 0):
    """A random direction (the same at every layer) with ``vector``'s norm at every layer. Seeded by the name
    of the vector it matches, so it does not depend on which other vectors exist."""
    import torch
    generator = torch.Generator().manual_seed(1000 * seed + 17 * index + sum(map(ord, name)))
    direction = torch.randn(vector.shape[-1], generator=generator).to(vector)
    return (direction / direction.norm())[None, :] * vector.norm(dim=-1, keepdim=True)


def add_random(V: dict, seeds: int, seed: int = 0) -> None:
    """Add to ``V``, for each slot vector, ``seeds`` random directions of its norm:
    random<k>|<arm>|<tokrole>."""
    for name in [name for name in V if name.startswith("slot|")]:
        arm_and_tokrole = name.split("|", 1)[1]
        for index in range(seeds):
            V[f"random{index}|{arm_and_tokrole}"] = random_like(V[name], name, index, seed)


def matched_random(V: dict, seeds: int, seed: int = 0) -> dict:
    """``V`` with, for each label and owner vector, ``seeds`` random directions of its norm:
    randlabel<k>|... and randowner<k>|..., keyed like the vector they match."""
    with_random = dict(V)
    for name, vector in V.items():
        kind, rest = name.split("|", 1)
        if kind in ("label", "owner"):
            for index in range(seeds):
                with_random[f"rand{kind}{index}|{rest}"] = random_like(vector, name, index, seed)
    return with_random


def reliability(means: Means, pairs: dict) -> pd.DataFrame:
    """Per vector and layer: its norm, the norm of the average residual stream it is taken from, and the
    cosine between the vectors estimated on the two halves of the fit patients."""
    import torch
    full = build_vectors(means, pairs)
    halves = [build_vectors(means, pairs, half) for half in (0, 1)]
    stream_norms = {}       # token role -> the norm of each group's mean stream, per layer
    for (arm, order, prole, owner, tokrole, lineset, _) in means.sum:
        if prole == "C" and lineset == "all":
            group_norm = means.mean(arm, order, prole, owner, tokrole).norm(dim=-1)
            stream_norms.setdefault(tokrole, []).append(group_norm)
    stream_norm = {tokrole: torch.stack(norms).mean(0) for tokrole, norms in stream_norms.items()}
    rows = []
    for name, vector in full.items():
        if name not in halves[0] or name not in halves[1]:
            continue
        cosine = torch.nn.functional.cosine_similarity(halves[0][name], halves[1][name], dim=-1)
        tokrole = name.rsplit("|", 1)[1]
        for layer in range(1, vector.shape[0]):
            rows.append(dict(vector=name, layer=layer, norm=float(vector[layer].norm()),
                             stream_norm=float(stream_norm[tokrole][layer]),
                             split_half_cosine=float(cosine[layer])))
    return pd.DataFrame(rows)


# Edits
def mean_edits(lay: prompts.Layout, V: dict, kind: str, arm: str, *, alpha: float = 1.0, site: str = "lines",
               layers=None, source: str | None = None, only: int | None = None) -> list[Edit]:
    """Add a mean vector to the record tokens of ``lay`` (a prompt of ``arm``).

    slot, random<k>   first record + alpha v, second record - alpha v: alpha 1 exchanges the slot marks
    owner             PERSON's record - alpha v, the model's + alpha v: alpha 1 exchanges the owner marks
    label             every record + alpha v: what labelling adds (negative alpha removes it)
    randlabel<k>, randowner<k>   a random direction of the label or owner vector's norm, applied like it

    ``site``: the record lines' tokens ("lines"), the headers' last tokens ("hdr") or "both".
    ``source``: the arm (slot, owner) or variant (label) whose vector to use, by default the prompt's own.
    ``only``: edit just the first (0) or second (1) record.
    """
    edits = []
    for block in lay.blocks:
        if only is not None and block.slot != only:
            continue
        for tokrole in TOKEN_ROLES_AT[site]:
            positions = block.of(tokrole)
            if not positions:
                continue
            if kind == "label" or kind.startswith("randlabel"):
                variant = source or binding.ARMS[arm][0]
                key, sign = f"{kind}|{variant}|{lay.order}|{block.owner}|{tokrole}", 1
            elif kind == "owner" or kind.startswith("randowner"):
                key, sign = f"{kind}|{source or arm}|{tokrole}", -1 if block.owner == "person" else 1
            else:
                key, sign = f"{kind}|{source or arm}|{tokrole}", 1 if block.slot == 0 else -1
            edits.append(Edit("add", positions, V[key], scale=sign * alpha, layers=layers))
    return edits


def donor_positions(lay: prompts.Layout) -> list[int]:
    """What a transplant may copy: line tokens, header ends, the last record token, the question and the
    tail."""
    return sorted(set(capture_positions(lay)) | {lay.n_ctx - 1} | set(lay.question) | set(lay.tail))


def transplant_pairs(recipient: prompts.Layout, donor: prompts.Layout,
                     group: str) -> tuple[list[int], list[int]] | None:
    """Matching positions (recipient's, donor's) of a token group, or None if the group cannot be matched."""
    if group in ("person_lines", "own_lines", "lines", "lines_hdr"):
        owners = {"person_lines": ("person",), "own_lines": ("own",)}.get(group, prompts.QUERIES)
        to, source, _ = prompts.aligned(recipient, donor, owners)
    else:
        to, source = [], []
    if group in ("hdr", "lines_hdr"):
        to += [recipient.block(owner).header[-1] for owner in prompts.QUERIES]
        source += [donor.block(owner).header[-1] for owner in prompts.QUERIES]
    if group == "ctx_end":
        to, source = [recipient.n_ctx - 1], [donor.n_ctx - 1]
    if group == "question":
        to, source = recipient.question + recipient.tail, donor.question + donor.tail
        if len(to) != len(source):
            return None
    return (to, source) if to else None


def transplant_edits(rec: prompts.Layout, don: prompts.Layout, hidden, captured: list[int], group: str,
                     layers=None, memo: dict | None = None):
    """Replace the recipient's residual stream at a token group with the donor's (``hidden``: the donor's
    capture at ``captured``), on ``layers`` (every block by default). None if the group cannot be matched.
    ``memo`` shares the copied values between edits of the same donor and group."""
    pairs = transplant_pairs(rec, don, group)
    if pairs is None:
        return None
    recipient_positions, donor_positions_ = pairs
    key = (id(hidden), id(rec), group)
    if memo is None or key not in memo:
        column_of = {position: column for column, position in enumerate(captured)}
        value = hidden[:, [column_of[position] for position in donor_positions_]]
        if memo is not None:
            memo[key] = value
    else:
        value = memo[key]
    return [Edit("set", recipient_positions, value, layers=layers)]


def windows(n_layers: int, parts: int) -> list[tuple[int, int]]:
    """``parts`` consecutive layer windows covering blocks 1..n_layers."""
    edges = [round(index * n_layers / parts) for index in range(parts + 1)]
    return [(edges[index] + 1, edges[index + 1]) for index in range(parts)]
