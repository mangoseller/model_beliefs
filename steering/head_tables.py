"""Attention heads written from tables of the lookup program's variables.

A lookup has four variables the program reads off the prompt's text: the *place* of the asked record (first or
second), the *owner* asked about, the *field*, and the *value* token of the right answer. On fit patients each
head's output at each question token is fitted as a sum of one vector per variable value (least squares; the
tokens after the phrase are the same for every lookup about one field, so positions align). On held-out
patients the program computes the variables and the heads' outputs are written from the tables:

    upstream   every head of the blocks before ``boundary`` except the readers, from the tables with the
               value left out: the program supplies place, owner and field in the model's own coordinates,
               and the model's later heads fetch the value themselves. With the place negated they should
               fetch the other record's value; with the owner negated nothing should change (the owner has
               become a place).
    all        every head after the phrase from the tables, value included: no attention after the phrase
               is the model's; its MLPs and unembedding finish. With the other record's value, that value
               is answered.
    values     the value heads from the tables, every earlier head at the neutral table (no place, no owner).
    sets       the top lookup heads from the tables with every other head live, or at the neutral table.

``explained`` is the share of each head's variation over lookups (around the neutral table) that the table
accounts for on held-out patients: how much of the head is the four variables.

A lookup's margin is the logit of the right record's value minus the other record's, at the answer token. A
head is (layer, head), layers counted from 0; heads' outputs are [layers, tokens, heads x head_dim].
"""
from __future__ import annotations

import functools
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from steering import circuit
from steering import program
from steering import prompts
from steering.results import read_rows, write_rows

ALIGNED = ("pre", "post", "tail", "last")       # token groups with the same tokens whatever the field
UNRANKED = 10 ** 6                              # rank given to a head the lookup ranking does not list


def table_key(group: str, field: str) -> str:
    """The table a token group is fitted in: its own, or (the field's own tokens) one per field."""
    return group if group in ALIGNED else f"field:{field}"


def lookups(engine, case: str, cases: pd.Series, cfg: dict, fields: int):
    """Every lookup of a patient: its pair, question, variables and the question tokens' positions by
    group."""
    for arm, order, pair in circuit.pairs_of(engine, circuit.case_cells(case, cases, cfg), fields):
        for query in prompts.QUERIES:
            asked = pair["queries"][query]
            right, other = (pair["tp"], pair["to"]) if query == "person" else (pair["to"], pair["tp"])
            yield dict(case_id=case, arm=arm, block_order=order, field=pair["code"], query=query, pair=pair,
                       ids=asked["ids"], cut=asked["cut"], place=float(circuit.place_sign(pair, query)),
                       owner=float(circuit.sign(query)), value=int(right), other_value=int(other),
                       groups={group: list(positions) for group, positions in pair["groups"].items()})


def run_clean(engine, lookup: dict) -> tuple[float, object]:
    """The lookup's margin for the right record and the heads' outputs at every token after the phrase."""
    pair, ids = lookup["pair"], lookup["ids"]
    after_phrase = list(range(lookup["cut"], len(ids)))
    _, records_state, _ = engine.run([ids[:pair["n_ctx"]]])
    logits, _, _ = engine.run([ids[pair["n_ctx"]:]], state=records_state, keep=False, zcapture=[after_phrase])
    margin = float(logits[0, lookup["value"]] - logits[0, lookup["other_value"]])
    return margin, engine.zcaptured[0][1]


class Tables:
    """Least squares of the heads' outputs on indicator features of the variables. ``coef[key]``:
    [features, layers, tokens of the group, heads x head_dim]; the field's own tokens have one fit per field.

    A lookup is a dict with ``field``, ``place`` and ``owner`` (each +1 or -1), ``value`` (a token id) and
    ``groups`` (token group -> positions among the tokens after the phrase)."""

    def __init__(self, fields: list[str], values: list[int]):
        """``fields``: the field codes, and ``values``: the answer value tokens, that get a feature each."""
        self.fields, self.values = list(fields), list(values)
        self.names = ["one", "place", "owner"]
        self.names += [f"field:{field}" for field in self.fields]
        self.names += [f"value:{value}" for value in self.values]
        # per table: the normal equations' sums while fitting, then the coefficients
        self.xtx, self.xtz, self.coef, self.lengths, self.n = {}, {}, {}, {}, {}
        self.value_mean = {}        # field -> the mean value indicator over its fit lookups (neutral value)

    def features(self, field: str, place: float, owner: float, value) -> np.ndarray:
        """A lookup's feature vector: a constant, the place, the owner, one indicator per field and one per
        value token. ``value``: a token id, or None for the field's neutral value (the mean indicator over its
        fit lookups)."""
        indicators = np.zeros(len(self.names))
        indicators[0], indicators[1], indicators[2] = 1.0, place, owner
        indicators[3 + self.fields.index(field)] = 1.0
        first_value = 3 + len(self.fields)
        if value is None:
            indicators[first_value:] = self.value_mean[field]
        else:
            indicators[first_value + self.values.index(value)] = 1.0
        return indicators

    def add(self, torch, lookup: dict, head_outputs) -> None:
        """Add one fit lookup to the sums.
        ``head_outputs``: [layers, tokens after the phrase, heads x head_dim]."""
        # sums over hundreds of lookups are kept in float32 whatever precision the model runs in
        head_outputs = head_outputs.float()
        features = self.features(lookup["field"], lookup["place"], lookup["owner"], lookup["value"])
        features = torch.tensor(features, dtype=head_outputs.dtype, device=head_outputs.device)
        n_features = len(features)
        for group, positions in lookup["groups"].items():
            key = table_key(group, lookup["field"])
            outputs = head_outputs[:, positions]
            if key not in self.xtz:
                self.lengths[key], self.n[key] = len(positions), 0
                self.xtx[key] = torch.zeros((n_features, n_features), dtype=torch.float64,
                                            device=head_outputs.device)
                self.xtz[key] = torch.zeros((n_features, *outputs.shape), dtype=head_outputs.dtype,
                                            device=head_outputs.device)
            if len(positions) != self.lengths[key]:     # tokenised to another length: cannot share the table
                continue
            self.xtx[key] += torch.outer(features, features).double()
            self.xtz[key] += features.view(-1, 1, 1, 1) * outputs
            self.n[key] += 1

    def solve(self, torch, counts: dict) -> None:
        """Turn the sums into coefficients. ``counts``: (field, value token) -> its number of fit lookups."""
        for key in self.xtz:
            pinv = torch.linalg.pinv(self.xtx[key]).to(self.xtz[key].dtype)
            self.coef[key] = torch.einsum("fg,glpd->flpd", pinv, self.xtz[key])
        for field in self.fields:
            per_value = np.array([counts.get((field, value), 0) for value in self.values], float)
            self.value_mean[field] = per_value / max(per_value.sum(), 1.0)
        self.xtx, self.xtz = {}, {}

    def usable(self, lookup: dict) -> bool:
        """Whether the tables cover this lookup: its field, both value tokens, and token groups of the fitted
        length."""
        if lookup["field"] not in self.fields:
            return False
        if lookup["value"] not in self.values or lookup["other_value"] not in self.values:
            return False
        return all(len(positions) == self.lengths.get(table_key(group, lookup["field"]))
                   for group, positions in lookup["groups"].items())

    def predict(self, torch, lookup: dict, place: float, owner: float, value):
        """The tables' outputs for every head at every token after the phrase, for a lookup with these
        variables: [layers, tokens, heads x head_dim]."""
        some = next(iter(self.coef.values()))       # any table: [features, layers, tokens, heads x head_dim]
        features = torch.tensor(self.features(lookup["field"], place, owner, value), dtype=some.dtype,
                                device=some.device)
        n_tokens = sum(len(positions) for positions in lookup["groups"].values())
        out = torch.zeros((some.shape[1], n_tokens, some.shape[3]), dtype=some.dtype, device=some.device)
        for group, positions in lookup["groups"].items():
            coefficients = self.coef[table_key(group, lookup["field"])]
            out[:, positions] = torch.einsum("f,flpd->lpd", features, coefficients)
        return out


def lookup_identity(case: str, lookup: dict) -> tuple:
    return case, lookup["arm"], lookup["block_order"], lookup["field"], lookup["query"]


def fit(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> Tables:
    """Fit the tables on the fit patients' lookups that the model answers with at least ``min_margin``, and
    write a summary of the fit to circuit_tables/fit.json."""
    import torch
    settings = cfg["circuit_tables"]
    patients = sorted(cases.index[cases == settings.get("fit_split", "fit")])[: int(settings["fit_patients"])]
    n_fields, min_margin = int(settings["fields"]), float(settings["min_margin"])
    started = time.time()
    # first pass: which lookups the model answers, and the value tokens and fields that occur
    answered, counts = [], {}
    for case in patients:
        for lookup in lookups(engine, case, cases, cfg, n_fields):
            margin, _ = run_clean(engine, lookup)
            if margin >= min_margin:
                answered.append(lookup_identity(case, lookup))
                variable = (lookup["field"], lookup["value"])
                counts[variable] = counts.get(variable, 0) + 1
    table = Tables(sorted({field for field, _ in counts}), sorted({value for _, value in counts}))
    # second pass: the answered lookups' head outputs
    answered_set = set(answered)
    for case in patients:
        for lookup in lookups(engine, case, cases, cfg, n_fields):
            if lookup_identity(case, lookup) in answered_set:
                _, head_outputs = run_clean(engine, lookup)
                table.add(torch, lookup, head_outputs)
    table.solve(torch, counts)

    (run_dir / "circuit_tables").mkdir(exist_ok=True)
    per_group = {key: dict(lookups=int(count), tokens=int(table.lengths[key]))
                 for key, count in table.n.items()}
    summary = dict(lookups=len(answered), fields=table.fields,
                   values=[engine.tok.decode([value]) for value in table.values],
                   features=len(table.names), per_group=per_group)
    (run_dir / "circuit_tables" / "fit.json").write_text(json.dumps(summary))
    print(f"[circuit tables] fitted on {len(answered)} lookups of {len(patients)} patients, "
          f"{len(table.names)} features, {time.time() - started:.1f}s", flush=True)
    return table


def all_heads(engine) -> list[tuple[int, int]]:
    return [(layer, head) for layer in range(engine.n_layers) for head in range(engine.n_heads)]


def head_sets_to_write(engine, sets: dict, settings: dict) -> dict:
    """name -> the heads written from the tables under that name.

    Besides the upstream heads, the value heads and the named lookup sets, two families locate where the owner
    becomes a place without a list of heads: the heads of the blocks before a boundary (written with place and
    owner, the owner alone, the place alone, or neither, every later head live), and the heads of one band of
    blocks (written from the neutral table, every other head live). Reader heads are never in these sets."""
    every_head = all_heads(engine)
    readers = set(sets.get("readers", []))
    boundary = int(settings["boundary"])
    value_heads = sets.get("value32", [])
    value_head_set = set(value_heads)
    named = {"upstream": [head for head in every_head if head[0] < boundary and head not in readers],
             "values": value_heads}
    named["before values"] = [head for head in every_head
                              if head[0] < boundary and head not in value_head_set and head not in readers]
    for name in settings["sets"]:
        if name in sets:
            members = set(sets[name])
            named[name] = sets[name]
            named[f"all but {name}"] = [head for head in every_head if head not in members]
    for first_live in settings.get("boundaries", []):
        before = [head for head in every_head if head[0] < int(first_live) and head not in readers]
        named[f"before block {int(first_live) + 1}"] = before
    for first, last in settings.get("bands", []):
        band = [head for head in every_head if int(first) <= head[0] <= int(last) and head not in readers]
        named[f"blocks {int(first) + 1}-{int(last) + 1}"] = band
    return named


def write_condition(test: str, setting: str, patches: list) -> dict:
    """One condition a lookup is run under: the test it belongs to, the setting of the variables, and the head
    outputs to write, as ``Engine.run`` takes them: (positions, outputs, the heads' columns or None for every
    head)."""
    return dict(test=test, setting=setting, zpatch=patches)


def variable_conditions(predict, lookup: dict, positions: list[int], dims: dict, neutral) -> list[dict]:
    """The upstream heads, every head, and the value heads written from the tables, under each setting of the
    variables. ``predict(place, owner, value)`` gives the tables' outputs for this lookup."""
    place, owner = lookup["place"], lookup["owner"]
    right, other = lookup["value"], lookup["other_value"]
    conditions = []
    # upstream heads from the tables (no value in them); the later heads fetch
    for setting, place_, owner_ in (("as the prompt", place, owner),
                                    ("place negated", -place, owner),
                                    ("owner negated", place, -owner),
                                    ("place and owner negated", -place, -owner),
                                    ("no place", 0.0, owner),
                                    ("no place, no owner", 0.0, 0.0)):
        patch = (positions, predict(place_, owner_, None), dims["upstream"])
        conditions.append(write_condition("upstream", setting, [patch]))
    # every head from the tables
    for setting, place_, owner_, value_ in (("as the prompt", place, owner, right),
                                            ("the other record's value", place, owner, other),
                                            ("the other record's value and place", -place, owner, other),
                                            ("place negated", -place, owner, right),
                                            ("no value", place, owner, None),
                                            ("no place, no owner", 0.0, 0.0, right),
                                            ("nothing but the field", 0.0, 0.0, None)):
        patch = (positions, predict(place_, owner_, value_), None)
        conditions.append(write_condition("all", setting, [patch]))
    # the value heads from the tables, the heads before them neutral
    if "values" in dims and "before values" in dims:
        neutral_before = (positions, neutral, dims["before values"])
        for setting, value_ in (("as the prompt", right), ("the other record's value", other)):
            value_patch = (positions, predict(0.0, 0.0, value_), dims["values"])
            conditions.append(write_condition("values", setting, [neutral_before, value_patch]))
        conditions.append(write_condition("values", "neutral before, value heads live", [neutral_before]))
    return conditions


def head_set_conditions(predict, lookup: dict, positions: list[int], dims: dict, settings: dict, full,
                        neutral) -> list[dict]:
    """Named head sets, the blocks before a boundary, and single bands of blocks written from the tables."""
    place, owner, other = lookup["place"], lookup["owner"], lookup["other_value"]
    conditions = []
    for name in settings["sets"]:
        if name not in dims:
            continue
        own_tables = (positions, full, dims[name])
        other_record = (positions, predict(-place, owner, other), dims[name])
        everything_neutral = (positions, neutral, None)
        others_neutral = (positions, neutral, dims[f"all but {name}"])
        from_tables = f"{name} from the tables"
        conditions += [
            write_condition(f"{from_tables}, others live", "as the prompt", [own_tables]),
            write_condition(f"{from_tables}, others live", "the other record's value and place",
                            [other_record]),
            write_condition(f"{from_tables}, others neutral", "as the prompt",
                            [everything_neutral, own_tables]),
            write_condition(f"{name} live, others neutral", "as the prompt", [others_neutral])]
    for first_live in settings.get("boundaries", []):
        name = f"before block {int(first_live) + 1}"
        for setting, place_, owner_ in (("place and owner", place, owner),
                                        ("owner only", 0.0, owner),
                                        ("place only", place, 0.0),
                                        ("neither", 0.0, 0.0),
                                        ("place negated", -place, owner)):
            patch = (positions, predict(place_, owner_, None), dims[name])
            conditions.append(write_condition(f"tables {name}, later heads live", setting, [patch]))
    for first, last in settings.get("bands", []):
        name = f"blocks {int(first) + 1}-{int(last) + 1}"
        patch = (positions, neutral, dims[name])
        conditions.append(write_condition("one band neutral, others live", name, [patch]))
    return conditions


def table_conditions(table: Tables, torch, lookup: dict, positions: list[int], dims: dict, settings: dict,
                     full, neutral) -> list[dict]:
    """Every condition one lookup is run under, in order. ``dims``: head set -> the columns of its heads'
    outputs; ``full`` and ``neutral``: the tables' outputs with the lookup's own variables, and with no place,
    owner or value."""
    predict = functools.partial(table.predict, torch, lookup)
    return (variable_conditions(predict, lookup, positions, dims, neutral)
            + head_set_conditions(predict, lookup, positions, dims, settings, full, neutral))


def run_conditions(engine, lookup: dict, records_state, conditions: list[dict]) -> list[tuple[float, int]]:
    """(margin, top token) of the lookup under each condition's written head outputs."""
    question = lookup["ids"][lookup["pair"]["n_ctx"]:]
    count = len(conditions)
    logits, _, _ = engine.run([question] * count, state=records_state.select([0] * count), keep=False,
                              zpatch=[condition["zpatch"] for condition in conditions])
    return [(float(row[lookup["value"]] - row[lookup["other_value"]]), int(row.argmax())) for row in logits]


def squared_distance_per_head(engine, head_outputs, reference, n_tokens: int):
    """Per (layer, head): the squared distance between two sets of head outputs, summed over tokens."""
    difference = (head_outputs.float() - reference).view(engine.n_layers, n_tokens, engine.n_heads, -1)
    return difference.pow(2).sum((1, 3)).cpu()


def stage_tables(engine, run_dir: Path, cfg: dict, cases: pd.Series) -> None:
    """Held-out lookups with head sets written from the tables (module docstring). Writes
    circuit_tables/rows/<patient>.parquet (one row per lookup and condition: margin, and whether the right or
    the other record's value is the top token), sets.json, and explained.parquet (per head: the share of its
    variation the tables account for)."""
    import torch
    settings = cfg["circuit_tables"]
    out = run_dir / "circuit_tables"
    held_out = [case for case in program.held_out(cases, cfg, settings)
                if not (out / "rows" / f"{case}.parquet").exists()]
    if not held_out:
        return
    table = fit(engine, run_dir, cfg, cases)
    named = head_sets_to_write(engine, program.named_sets(run_dir, engine, cfg), settings)
    dims = {name: engine.head_dims(heads) for name, heads in named.items() if heads}
    short_sets = {name: heads for name, heads in named.items() if len(heads) < 200}
    (out / "sets.json").write_text(json.dumps(short_sets))
    n_fields, min_margin = int(settings["fields"]), float(settings["min_margin"])
    # per (layer, head), summed over lookups: squared error of the tables, and squared distance from the
    # neutral table
    error = torch.zeros((engine.n_layers, engine.n_heads))
    variation = torch.zeros((engine.n_layers, engine.n_heads))
    n_lookups = 0
    for done, case in enumerate(held_out):
        started, rows = time.time(), []
        for lookup in lookups(engine, case, cases, cfg, n_fields):
            margin, head_outputs = run_clean(engine, lookup)
            if not table.usable(lookup) or margin < min_margin:
                continue
            ids, n_ctx = lookup["ids"], lookup["pair"]["n_ctx"]
            positions = list(range(lookup["cut"], len(ids)))        # the tokens after the phrase
            right, other = lookup["value"], lookup["other_value"]
            full = table.predict(torch, lookup, lookup["place"], lookup["owner"], right)
            neutral = table.predict(torch, lookup, 0.0, 0.0, None)
            error = error + squared_distance_per_head(engine, head_outputs, full, len(positions))
            variation = variation + squared_distance_per_head(engine, head_outputs, neutral, len(positions))
            n_lookups += 1
            conditions = table_conditions(table, torch, lookup, positions, dims, settings, full, neutral)
            _, records_state, _ = engine.run([ids[:n_ctx]])
            run_batch = functools.partial(run_conditions, engine, lookup, records_state)
            results = engine._halving(conditions, int(settings["batch"]), run_batch)
            meta = dict(case_id=case, arm=lookup["arm"], block_order=lookup["block_order"],
                        field=lookup["field"], query=lookup["query"])
            rows.append(dict(meta, test="model", setting="unedited", margin=margin, top_right=True,
                             top_other=False))
            for condition, (patched_margin, top) in zip(conditions, results):
                rows.append(dict(meta, test=condition["test"], setting=condition["setting"],
                                 margin=patched_margin, top_right=top == right, top_other=top == other))
        write_rows(out / "rows" / f"{case}.parquet", rows)
        seconds = time.time() - started
        print(f"[circuit tables] {done + 1}/{len(held_out)} patients, {len(rows)} runs, {seconds:.1f}s",
              flush=True)
    if n_lookups:
        share = (1 - error / variation.clamp_min(1e-12)).numpy()
        explained = [dict(layer=layer, head=head, explained=float(share[layer, head]),
                          variation=float(variation[layer, head] / n_lookups))
                     for layer, head in all_heads(engine)]
        pd.DataFrame(explained).to_parquet(out / "explained.parquet", index=False)


def explained_summary(label: str, heads: pd.DataFrame) -> dict:
    """How much of a group of heads the tables account for: median, variation-weighted mean, share under one
    half."""
    total_variation = heads.variation.sum()
    if total_variation:
        weighted = float((heads.explained * heads.variation).sum() / total_variation)
    else:           # no head in the group (a rank band beyond the model's heads)
        weighted = float("nan")
    return dict(heads=label, n=len(heads), explained_median=heads.explained.median(),
                explained_weighted=weighted, below_half=float((heads.explained < 0.5).mean()))


def explained_tables(run_dir: Path) -> dict:
    """The share of each head the tables account for, summarised by the head's rank in the lookup ranking and
    for the value heads, and listed for the 64 top-ranked heads."""
    explained = pd.read_parquet(run_dir / "circuit_tables" / "explained.parquet")
    sets = json.loads((run_dir / "circuit_tables" / "sets.json").read_text())
    ranking = json.loads((run_dir / "circuit" / "selection.json").read_text())["query"]
    rank_of = {tuple(head): rank for rank, head in enumerate(ranking)}
    heads = list(zip(explained["layer"], explained["head"]))
    explained["rank"] = [rank_of.get((int(layer), int(head)), UNRANKED) for layer, head in heads]
    rows = []
    rank_bands = (("lookup top 16", 0, 16), ("lookup top 64", 0, 64), ("lookup 65-256", 64, 256),
                  ("all other heads", 256, 10 ** 7))
    for label, first_rank, end_rank in rank_bands:
        in_band = explained[(explained["rank"] >= first_rank) & (explained["rank"] < end_rank)]
        rows.append(explained_summary(label, in_band))
    value_heads = {tuple(head) for head in sets.get("values", [])}
    of_value_heads = explained[[(int(layer), int(head)) in value_heads for layer, head in heads]]
    if len(of_value_heads):
        rows.append(explained_summary(f"value heads ({len(of_value_heads)})", of_value_heads))
    return {"tables_explained": pd.DataFrame(rows),
            "tables_explained_heads": explained.sort_values("rank").head(64)}


def tables(run_dir: Path, cfg: dict) -> dict:
    """Per test and setting: the share of lookups answered with the right value, with the right or the other
    record's value as the top token, and the mean margin; the same by field for the prompt's own settings; and
    how much of each head the tables account for."""
    rows = read_rows(run_dir / "circuit_tables" / "rows")
    if rows.empty:
        return {}
    rows["right"], rows["other"] = rows.margin > 0, rows.margin < 0
    out = {"tables": rows.groupby(["test", "setting"]).agg(
        lookups=("right", "size"), right_value=("right", "mean"), right_is_top=("top_right", "mean"),
        other_is_top=("top_other", "mean"), margin=("margin", "mean")).reset_index()}
    out["tables_fields"] = rows[rows.setting == "as the prompt"].groupby(["test", "field"]).agg(
        lookups=("right", "size"), right_value=("right", "mean"), margin=("margin", "mean")).reset_index()
    tables_dir = run_dir / "circuit_tables"
    if (tables_dir / "explained.parquet").exists() and (tables_dir / "sets.json").exists():
        out |= explained_tables(run_dir)
    return out


# The program at the token where a reply writes a value
def random_like(torch, reference):
    """A fixed random tensor with ``reference``'s shape and its norm along the last axis."""
    generator = torch.Generator(device="cpu").manual_seed(0)
    noise = torch.randn(reference.shape, generator=generator).to(reference.device)
    scale = reference.norm(dim=-1, keepdim=True) / noise.norm(dim=-1, keepdim=True).clamp_min(1e-9)
    return noise * scale


def reply_rows(engine, cfg: dict, cases: pd.Series, settings: dict, probe: dict, readers: list,
               random_heads: list, upstream_dims: dict, terms: dict) -> list[dict]:
    """One reply, fed up to the token that writes its first disputed value: whether that token changes record
    when the readers are pointed at a value token, or when the upstream heads' outputs are moved by a table
    term. ``terms``: the tables' place and owner terms at the answer token and a random tensor of the place
    term's size, each [layers, 1, heads x head_dim]. Empty if the reply's own answer is not the value it went
    on to write."""
    from steering import run
    cell_prompts = run.case_prompts(probe["case_id"], cases, cfg["pairs"]["false"])
    prompt = cell_prompts[(probe["arm"], probe["block_order"], "C")]["prompt"]
    layout = prompts.layout(engine.tok, prompt, engine.kw, prefix="")
    field = probe["field"]
    if field not in layout.blocks[0].lines or field not in layout.blocks[1].lines:
        return []
    pointer = prompts.pointer_tokens(layout, field)
    if pointer is None:
        return []
    ids, n_ctx, writing = probe["ids"], probe["n_ctx"], len(probe["ids"]) - 1
    wrote = "own" if probe["wrote"] == "model's" else "person"
    other = "person" if wrote == "own" else "own"
    place_sign = 1.0 if probe["first_slot"] == 0 else -1.0      # the place of the record about to be quoted
    owner_sign = 1.0 if wrote == "person" else -1.0
    wrote_sign = 1.0 if wrote == "own" else -1.0                # the margin is the model's minus PERSON's

    _, records_state, _ = engine.run([ids[:n_ctx]])
    _, reply_state, _ = engine.run([ids[n_ctx:writing]], state=records_state)
    logits, _, _ = engine.run([[ids[writing]]], state=reply_state, keep=False, zcapture=[[writing]])
    head_outputs = engine.zcaptured[0][1].float()               # [layers, 1, heads x head_dim]
    base = float(logits[0, probe["own_tok"]] - logits[0, probe["person_tok"]])
    if wrote_sign * base <= 0:
        return []

    conditions = [dict(test="pointer", what="the other record's value",
                       force=circuit.knock_of(readers, [pointer[other]])),
                  dict(test="pointer", what="the quoted record's value",
                       force=circuit.knock_of(readers, [pointer[wrote]]))]
    if random_heads:
        conditions.append(dict(test="pointer", what="the other record's value, random heads",
                               force=circuit.knock_of(random_heads, [pointer[other]])))
    moves = (("place", -2.0 * place_sign * terms["place"]), ("owner", -2.0 * owner_sign * terms["owner"]),
             ("random", terms["random"] * 2.0))
    for scale in settings["scales"]:
        for what, move in moves:
            patch = ([writing], head_outputs + float(scale) * move, upstream_dims)
            conditions.append(dict(test="tables", what=f"{what} term x{scale:g}", zpatch=[patch]))
    count = len(conditions)
    patched, _, _ = engine.run([[ids[writing]]] * count, state=reply_state.select([0] * count), keep=False,
                               force=[condition.get("force") for condition in conditions],
                               zpatch=[condition.get("zpatch") for condition in conditions])
    rows = []
    for condition, row_logits in zip(conditions, patched):
        margin = float(row_logits[probe["own_tok"]] - row_logits[probe["person_tok"]])
        rows.append(dict(case_id=probe["case_id"], arm=probe["arm"], block_order=probe["block_order"],
                         sample=probe["sample"], field=field, wrote=probe["wrote"], test=condition["test"],
                         what=condition["what"], base=base, patched=margin, flipped=wrote_sign * margin < 0))
    return rows


def stage_tables_reply(engine, run_dir: Path, cfg: dict, cases: pd.Series, load_vectors) -> None:
    """The program at the token where a reasoned reply writes a disputed value (unedited replies,
    teacher-forced to their first disputed value). (a) The pointer: the readers' attention set to the asked
    field's value token in the other record, or in the record the reply was about to quote. (b) The place
    tables fitted on lookups: every upstream head's output at the writing token moved by its place term to the
    other record (owner term and a random direction of the same size per block: controls). Read: whether the
    value written changes record. Writes circuit_tables_reply/rows.parquet."""
    import torch
    settings = cfg["circuit_tables_reply"]
    out = run_dir / "circuit_tables_reply"
    path = out / "rows.parquet"
    if path.exists():
        return
    slot_vectors = load_vectors(run_dir, engine.device)
    fit_settings = cfg["circuit_tables"] | {"fit_patients": settings["fit_patients"]}
    table = fit(engine, run_dir, cfg | {"circuit_tables": fit_settings}, cases)
    sets = program.named_sets(run_dir, engine, cfg)
    readers = sets["readers"]
    boundary = int(settings["boundary"])
    upstream = [head for head in all_heads(engine) if head[0] < boundary and head not in set(readers)]
    upstream_dims = engine.head_dims(upstream)
    place_term, owner_term = table.coef["last"][1], table.coef["last"][2]      # [layers, 1, heads x head_dim]
    terms = dict(place=place_term, owner=owner_term, random=random_like(torch, place_term))
    probes = circuit.reply_probes(engine, run_dir, cfg, cases, slot_vectors, settings["folders"],
                                  float(cfg["circuit"]["alpha"]), int(settings["max_tokens"]))
    rows, started = [], time.time()
    for probe in probes:
        rows += reply_rows(engine, cfg, cases, settings, probe, readers, sets.get("random_readers_0", []),
                           upstream_dims, terms)
    out.mkdir(exist_ok=True)
    write_rows(path, rows)
    print(f"[circuit tables reply] {len(rows)} runs, {time.time() - started:.1f}s", flush=True)


def reply_tables(run_dir: Path, cfg: dict) -> dict:
    """Per pointer or table condition: the share of replies whose written value changes record and the margin
    before and after, over all replies and by prompt format, page order and whose value the reply wrote."""
    path = run_dir / "circuit_tables_reply" / "rows.parquet"
    if not path.exists():
        return {}
    rows = pd.read_parquet(path)
    if rows.empty:
        return {}
    labelled_arm = cfg["pairs"]["false"][1]
    labelling = pd.Series(np.where(rows.arm == labelled_arm, "labelled", "unlabelled"), index=rows.index)
    rows["cell"] = labelling + ", " + rows.block_order + ", wrote " + rows.wrote
    summary = rows.groupby(["test", "what"]).agg(
        replies=("flipped", "size"), flipped=("flipped", "mean"), margin_before=("base", "mean"),
        margin_after=("patched", "mean")).reset_index()
    by_cell = rows.pivot_table(index=["test", "what"], columns="cell", values="flipped").reset_index()
    return {"tables_reply": summary, "tables_reply_cells": by_cell}
