"""A causal LM with per-row interventions, run in chunks over a shared key-value cache.

``Engine.run`` processes one chunk of tokens for a batch of rows. The first chunk is left-padded, later chunks
are right-padded after the cached keys, and every token keeps its own position id, so rows of different
lengths batch exactly. Interventions name token positions in a row's whole sequence and act in the chunk that
holds the position. Because an edit to the records sits in the cache, every later chunk (the question, the
answer digits, sampled reasoning) is computed under it.

Layers are counted in two ways, as in transformers. A *hidden index* i is the output of block i (index 0 is
the embeddings): residual-stream edits and captures use it. A *block* is counted from 0: head knockouts,
forced attention, head outputs, MLP outputs and projection paths use it.

The interventions, each set per row for one forward pass and applied by a hook:

    residual stream   add to it or replace it at chosen tokens (``Edit``); record it
    attention         bar chosen heads from chosen earlier tokens, or confine them to chosen tokens
    projections       compute chosen tokens' queries, keys or values from another run's residual stream
    head outputs      record them, replace them with another run's, or copy them between rows of the batch
    MLP outputs       record them, or replace them with another run's
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np


@dataclass
class Edit:
    """A change to one row's residual stream at ``positions`` (indices into the row's whole sequence).

    mode "add": ``value`` [layers + 1, hidden] (or [layers + 1, positions, hidden]) is added, times ``scale``.
    mode "set": ``value`` [layers + 1, len(positions), hidden] replaces the stream.
    ``layers``: the inclusive range of hidden indices the edit acts on; None means every block.
    """
    mode: str
    positions: list[int]
    value: object
    scale: float = 1.0
    layers: tuple[int, int] | None = None


@dataclass
class State:
    """The key-value cache of a batch after one or more chunks, and what appending the next chunk needs."""
    cache: tuple                   # per block (keys, values)
    mask: object                   # [rows, keys], 0 at padding
    lengths: list[int]             # real tokens so far, per row
    pad: list[int]                 # key column of each row's first token

    def select(self, rows: list[int]) -> "State":
        """The state of the given rows, in that order (a row may be repeated)."""
        index = self.mask.new_tensor(rows)
        cache = tuple((keys[index], values[index]) for keys, values in self.cache)
        lengths, pad = [self.lengths[row] for row in rows], [self.pad[row] for row in rows]
        return State(cache, self.mask[index], lengths, pad)


@dataclass
class Job:
    """One prompt to evaluate (``Engine.evaluate``) or to sample replies to (``Engine.generate``), with its
    interventions.

    ids      the token sequence; for a belief read-out it includes the forced answer prefix
    capture  positions whose residual stream to return, at every layer; ``reduce`` is applied to the capture
             when its batch finishes
    lookups  lookup questions sharing the prompt's records: (ids, token of PERSON's value, token of the
             model's)
    knock    (start, {block: (heads, key positions)}): from token ``start`` on, these heads cannot attend to
             these earlier tokens
    paths    entries (positions, residual stream [layers + 1, positions, hidden], kinds): from the second
             block on, the projections named in ``kinds`` ("q", "k", "v" or several) are computed for these
             tokens from this residual stream instead of the row's own. An optional fourth element (first
             block, last block) limits the entry to those blocks; an optional fifth, {block: columns of the
             projection's output}, limits it to some heads' queries (or some value heads' keys and values)
    steer    (vector [layers + 1, hidden], scale, (first, last hidden index)): added to the residual stream
             of every sampled token (``Engine.generate`` with the preallocated cache)
    follow, pair, split
             paired sampling: a job with ``follow`` = the index of another job of the same batch is not
             sampled. It is fed that job's sampled tokens, and at every token from ``split`` on (each job's
             own first token after the records) the leader's heads in ``pair`` ({block: columns}; None: every
             head) take this job's outputs. Leader and follower must have the same tokens from ``split`` on.
    """
    ids: list[int]
    edits: list[Edit] = field(default_factory=list)
    capture: list[int] | None = None
    reduce: object = None
    lookups: list[tuple] = field(default_factory=list)
    knock: tuple | None = None
    paths: list[tuple] = field(default_factory=list)
    steer: tuple | None = None
    follow: int | None = None
    pair: dict | None = None
    split: int | None = None
    meta: dict = field(default_factory=dict)


def growing_cache(config, rows: int, length: int, device, dtype):
    """A preallocated key-value cache whose attention sees only the positions written so far. transformers'
    StaticCache hands every layer the whole allocation at every token, and Qwen3's attention then expands the
    grouped keys and values over all of it; this one returns views up to the newest position."""
    from transformers import StaticCache

    class Growing(StaticCache):
        filled = 0

        def update(self, key_states, value_states, layer_idx, cache_kwargs=None):
            keys, values = super().update(key_states, value_states, layer_idx, cache_kwargs)
            return keys[:, :, :self.filled], values[:, :, :self.filled]

        def get_max_cache_shape(self):      # the width of the attention mask the model builds
            return self.filled

    return Growing(config=config, max_batch_size=rows, max_cache_len=length, device=device, dtype=dtype)


PROBABILITY = re.compile(r'\{\s*"probability"\s*:\s*([0-9]*\.?[0-9]+)\s*\}')


def stated_probability(text: str) -> float | None:
    """The number in a reply's last {"probability": x} object; None if there is none or it is outside
    [0, 1]."""
    found = PROBABILITY.findall(text or "")
    if not found:
        return None
    probability = float(found[-1])
    return probability if 0 <= probability <= 1 else None


def load_model(model: str, device, dtype: str, tf32: bool, revision: str | None):
    """Returns (model, tokenizer, device, revision, chat-template settings)."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from second_order import config
    from second_order.run_generation import model_revision, template_kwargs
    settings = config.model_config(model)
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    if tf32:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    revision = revision or model_revision(model)
    tokenizer = AutoTokenizer.from_pretrained(settings["path"], revision=revision)
    placement = {} if str(device) == "cpu" else {"device_map": {"": device}}     # straight onto the GPU
    lm = AutoModelForCausalLM.from_pretrained(settings["path"], revision=revision,
                                              torch_dtype=getattr(torch, dtype), **placement).to(device)
    return lm, tokenizer, device, revision, template_kwargs(model)


class Chunk(NamedTuple):
    """Where one chunk's tokens sit: in this forward pass's batch tensor and in each row's whole sequence."""
    lengths: list[int]         # per row: tokens in this chunk
    width: int                 # columns of the batch tensor (the longest row)
    earlier: list[int]         # per row: tokens in the chunks before this one
    first_column: list[int]    # per row: column of the row's first token of this chunk


class ResidualEdit(NamedTuple):
    """An ``Edit`` restricted to the tokens of one row that fall in the current chunk."""
    first_layer: int
    last_layer: int
    mode: str
    row: int
    columns: object            # columns of the batch tensor
    selected: object           # indices into the edit's positions (and so into a per-position value)
    value: object
    scale: float


class PathEntry(NamedTuple):
    """One projection path (``Job.paths``) restricted to one row's tokens that fall in the current chunk."""
    row: int
    columns: object
    selected: object
    value: object              # the residual stream [layers + 1, positions, hidden] to project instead
    kinds: str                 # which projections: "q", "k", "v" or several
    window: tuple | None       # (first block, last block); None: every block
    output_columns: dict | None    # {block: columns of the projection's output}; None: the whole projection

    def applies(self, block: int, kind: str) -> bool:
        return kind in self.kinds and (self.window is None or self.window[0] <= block <= self.window[1])


class Engine:
    """A model with hooks for the interventions above. The ``run`` call that asks for a read-out leaves it in
    ``attention``, ``attention_maps``, ``zcaptured`` (head outputs) or ``mcaptured`` (MLP outputs)."""

    def __init__(self, model: str | None = None, *, lm=None, tok=None, device=None, dtype: str = "float32",
                 template_kwargs=None, statistic: str = "mean_logit", tf32: bool = True,
                 revision: str | None = None):
        import torch
        self.torch = torch
        if lm is None:
            lm, tok, device, revision, template_kwargs = load_model(model, device, dtype, tf32, revision)
        lm.eval()
        # nothing here needs gradients; a state computed from the weights outside ``run`` would otherwise
        # carry a graph (and keep its inputs alive)
        lm.requires_grad_(False)
        self.lm, self.tok, self.revision = lm, tok, revision
        self.device = torch.device(device) if device is not None else next(lm.parameters()).device
        self.kw = template_kwargs if template_kwargs is not None else {"enable_thinking": False}
        self.statistic = statistic
        self.n_layers, self.hidden = len(lm.model.layers), lm.config.hidden_size
        self.n_heads = lm.config.num_attention_heads
        self.head_dim = lm.model.layers[0].self_attn.o_proj.in_features // self.n_heads
        self.digits = [tok.convert_tokens_to_ids(str(digit)) for digit in range(10)]
        missing = [token for token in self.digits if token is None or token == tok.unk_token_id]
        if len(set(self.digits)) != 10 or missing:
            raise ValueError("tokenizer lacks single-digit tokens")
        self.pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
        stops = {tok.eos_token_id, tok.convert_tokens_to_ids("<|im_end|>"),
                 tok.convert_tokens_to_ids("<|endoftext|>")}
        self.stops = {token for token in stops if token is not None and token != tok.unk_token_id}
        self.batch_cap = None       # the batch size an out-of-memory error has reduced every later call to
        # the interventions of the forward pass in progress, read by the hooks
        self._residual_edits, self._residual_captures = [], []
        self._barred, self._forced = None, None
        self._path_entries = []
        self._head_captures, self._head_patches, self._head_pairs = [], {}, None
        self._mlp_captures, self._mlp_patches = [], {}
        self._steering = None
        # read-outs of the last ``run`` call that asked for them
        self.attention = None       # run(groups=...)
        self.attention_maps = None  # run(attn_heads=...)
        self.zcaptured = None       # run(zcapture=...)
        self.mcaptured = None       # run(mcapture=...)
        self._handles = self._register_hooks()

    def _register_hooks(self) -> list:
        blocks = list(enumerate(self.lm.model.layers))
        handles = [block.register_forward_hook(self._residual_hook(index + 1)) for index, block in blocks]
        for index, block in blocks:
            mask_hook = self._attention_mask_hook(index)
            handles.append(block.self_attn.register_forward_pre_hook(mask_hook, with_kwargs=True))
        for index, block in blocks:
            for kind in "qkv":
                projection, norm = getattr(block.self_attn, f"{kind}_proj"), block.input_layernorm
                input_hook = self._projection_input_hook(index, kind, norm)
                output_hook = self._projection_output_hook(index, kind, norm)
                handles.append(projection.register_forward_pre_hook(input_hook))
                handles.append(projection.register_forward_hook(output_hook))
            handles.append(block.self_attn.o_proj.register_forward_pre_hook(self._head_output_hook(index)))
            handles.append(block.mlp.register_forward_hook(self._mlp_output_hook(index)))
        return handles

    def close(self):
        for handle in self._handles:
            handle.remove()

    def cast(self, dtype: str) -> None:
        """Run in another precision from here on. Qwen3's weights are stored in bfloat16, so moving between it
        and a wider type loses nothing. Left alone on a CPU, where half precision is slow."""
        wanted = getattr(self.torch, dtype)
        if next(self.lm.parameters()).dtype != wanted and self.device.type != "cpu":
            self.lm.to(dtype=wanted)

    # Hooks: each applies one kind of intervention of the forward pass in progress
    def _residual_hook(self, layer: int):
        """After a block (hidden index ``layer``): steer sampled tokens, apply the edits, then record."""
        def hook(module, args, output):
            if not self._residual_edits and not self._residual_captures and self._steering is None:
                return
            stream = output[0] if isinstance(output, tuple) else output       # [rows, columns, hidden]
            if self._steering is not None and layer in self._steering:        # one vector per sampled row
                stream += self._steering[layer][:, None, :].to(stream.dtype)
            for edit in self._residual_edits:
                if not edit.first_layer <= layer <= edit.last_layer:
                    continue
                if edit.mode == "add":
                    vector = edit.value[layer]      # [hidden], or [positions, hidden]
                    per_token = vector if vector.dim() == 1 else vector[edit.selected]
                    stream[edit.row, edit.columns] += (per_token * edit.scale).to(stream.dtype)
                else:
                    stream[edit.row, edit.columns] = edit.value[layer][edit.selected].to(stream.dtype)
            for row, columns, store in self._residual_captures:
                store[layer] = stream[row, columns].clone()
        return hook

    def _attention_mask_hook(self, block: int):
        """Before a block's attention: bar the chosen heads of the chosen rows from the chosen keys, and
        confine the forced heads to theirs, by giving the block a per-head copy of the attention mask."""
        def hook(module, args, kwargs):
            barred = (self._barred or {}).get(block)       # [rows, heads, keys]
            forced = (self._forced or {}).get(block)       # (bool [rows, heads], allowed [rows, heads, keys])
            if barred is None and forced is None:
                return None
            mask = kwargs.get("attention_mask")
            if mask is None or mask.dim() != 4:
                raise RuntimeError("head knockouts need the 4D attention mask")
            torch = self.torch
            mask = mask.expand(-1, self.n_heads, -1, -1).clone()      # [rows, heads, queries, keys]
            blocked = torch.finfo(mask.dtype).min
            if barred is not None:
                width = min(barred.shape[-1], mask.shape[-1])
                mask[..., :width].masked_fill_(barred[:, :, None, :width], blocked)
            if forced is not None:      # after the knockouts: a forced head sees its allowed keys only
                is_forced, allowed = forced
                keys = mask.shape[-1]
                allowed_wide = torch.zeros((*is_forced.shape, keys), dtype=torch.bool, device=mask.device)
                allowed_wide[..., :min(keys, allowed.shape[-1])] = allowed[..., :keys]
                mask.masked_fill_((is_forced[:, :, None] & ~allowed_wide)[:, :, None, :], blocked)
                mask.masked_fill_((is_forced[:, :, None] & allowed_wide)[:, :, None, :], 0.0)
            kwargs["attention_mask"] = mask
            return args, kwargs
        return hook

    def _projection_input_hook(self, block: int, kind: str, norm):
        """Before a block's query, key or value projection: for the chosen tokens, project the normalised
        residual stream of another run instead. Block 0 reads the embeddings, which no edit touches."""
        def hook(module, args):
            entries = [entry for entry in self._path_entries
                       if entry.output_columns is None and entry.applies(block, kind)]
            if not entries or block == 0:
                return None
            stream = args[0].clone()         # the other projections keep the original input
            for entry in entries:
                stream[entry.row, entry.columns] = norm(entry.value[block][entry.selected].to(stream.dtype))
            return (stream,)
        return hook

    def _projection_output_hook(self, block: int, kind: str, norm):
        """After a block's query, key or value projection: entries that name columns of the projection's
        output (some heads' queries, or some value heads' keys or values) replace only those columns, at the
        chosen tokens, by the projection of another residual stream."""
        def hook(module, args, output):
            entries = [entry for entry in self._path_entries if entry.output_columns is not None
                       and block in entry.output_columns and entry.applies(block, kind)]
            if not entries or block == 0:
                return None
            replaced = output.clone()
            for entry in entries:
                wanted = entry.output_columns[block]
                other_stream = norm(entry.value[block][entry.selected].to(output.dtype))
                other = self.torch.nn.functional.linear(other_stream, module.weight, module.bias)
                replaced[entry.row, entry.columns[:, None], wanted[None, :]] = other[:, wanted]
            return replaced
        return hook

    def _head_output_hook(self, block: int):
        """Before a block's attention output projection, whose input is the heads' outputs side by side:
        record them at the chosen tokens, replace the chosen heads' outputs there with another run's, then
        copy the paired rows' outputs."""
        def hook(module, args):
            patches, pairs = self._head_patches.get(block), self._head_pairs
            if not self._head_captures and not patches and not pairs:
                return None
            outputs = args[0]       # [rows, columns, heads x head_dim]
            for row, columns, store in self._head_captures:
                store[block] = outputs[row, columns].clone()
            if not patches and not pairs:
                return None
            outputs = outputs.clone()
            for row, columns, selected, value, head_columns in patches or []:
                if head_columns is None:
                    outputs[row, columns] = value[block][selected].to(outputs.dtype)
                else:
                    donor = value[block][selected][:, head_columns].to(outputs.dtype)
                    outputs[row, columns[:, None], head_columns[None, :]] = donor
            for destination, source, columns_by_block in pairs or []:      # rows of this batch, every token
                if columns_by_block is None:
                    outputs[destination] = outputs[source]
                elif block in columns_by_block:
                    mixed = outputs[destination]
                    mixed[:, :, columns_by_block[block]] = outputs[source][:, :, columns_by_block[block]]
                    outputs[destination] = mixed
            return (outputs,)
        return hook

    def _mlp_output_hook(self, block: int):
        """After a block's MLP: record its output at the chosen tokens, then replace it there with another
        run's."""
        def hook(module, args, output):
            patches = self._mlp_patches.get(block)
            if not self._mlp_captures and not patches:
                return None
            for row, columns, store in self._mlp_captures:
                store[block] = output[row, columns].clone()
            if not patches:
                return None
            output = output.clone()
            for row, columns, selected, value in patches:
                output[row, columns] = value[block][selected].to(output.dtype)
            return output
        return hook

    def head_dims(self, heads) -> dict:
        """{block: the columns of the heads' outputs} for a list of (block, head); block 0 is the first."""
        torch, heads_by_block = self.torch, {}
        for block, head in heads:
            heads_by_block.setdefault(int(block), []).append(int(head))
        width, columns = self.head_dim, {}
        for block, members in heads_by_block.items():
            spans = [torch.arange(head * width, (head + 1) * width) for head in sorted(members)]
            columns[block] = torch.cat(spans).to(self.device)
        return columns

    # Setting up one forward pass
    def _head_key_entries(self, specs, pad: list[int]) -> list[tuple]:
        """(row, block, heads, key columns) for each non-empty entry of per-row {block: (heads, key
        positions)}."""
        return [(row, int(block), heads, [pad[row] + key for key in keys])
                for row, spec in enumerate(specs or [])
                for block, (heads, keys) in (spec or {}).items() if len(heads) and len(keys)]

    def _compile_knock(self, knock, pad: list[int]):
        """{block: bool [rows, heads, key columns]}, true where a row's head may not attend, or None if no row
        has a knockout."""
        torch, device, barred = self.torch, self.device, {}
        entries = self._head_key_entries(knock, pad)
        if not entries:
            return None
        width = max(max(columns) for _, _, _, columns in entries) + 1
        for row, block, heads, columns in entries:
            if block not in barred:
                shape = (len(knock), self.n_heads, width)
                barred[block] = torch.zeros(shape, dtype=torch.bool, device=device)
            head_index = torch.tensor(heads, device=device)[:, None]
            barred[block][row, head_index, torch.tensor(columns, device=device)[None, :]] = True
        return barred

    def _compile_force(self, force, pad: list[int]):
        """{block: (bool [rows, heads], bool [rows, heads, key columns])}: the heads whose attention is set,
        and the only keys each may attend to; None if no row forces a head."""
        torch, device, forced = self.torch, self.device, {}
        entries = self._head_key_entries(force, pad)
        if not entries:
            return None
        width = max(max(columns) for _, _, _, columns in entries) + 1
        for row, block, heads, columns in entries:
            if block not in forced:
                shape = (len(force), self.n_heads)
                forced[block] = (torch.zeros(shape, dtype=torch.bool, device=device),
                                 torch.zeros((*shape, width), dtype=torch.bool, device=device))
            is_forced, allowed = forced[block]
            head_index = torch.tensor(heads, device=device)
            is_forced[row, head_index] = True
            allowed[row, head_index[:, None], torch.tensor(columns, device=device)[None, :]] = True
        return forced

    def _locate(self, chunk: Chunk, row: int, positions: list[int]):
        """Of ``positions`` (indices into the row's whole sequence), those inside this chunk: their indices
        into ``positions``, and their columns in the batch tensor."""
        start, end = chunk.earlier[row], chunk.earlier[row] + chunk.lengths[row]
        inside = [index for index, position in enumerate(positions) if start <= position < end]
        columns = [chunk.first_column[row] + positions[index] - start for index in inside]
        return inside, self.torch.tensor(columns, device=self.device)

    def _selected(self, inside: list[int]):
        return self.torch.tensor(inside, device=self.device)

    def _residual_edits_in(self, chunk: Chunk, edits) -> list[ResidualEdit]:
        found = []
        for row, row_edits in enumerate(edits or []):
            for edit in row_edits:
                inside, columns = self._locate(chunk, row, edit.positions)
                if inside:
                    first_layer, last_layer = edit.layers or (1, self.n_layers)
                    found.append(ResidualEdit(first_layer, last_layer, edit.mode, row, columns,
                                              self._selected(inside), edit.value, float(edit.scale)))
        return found

    def _captures_in(self, chunk: Chunk, requests) -> tuple[list, dict]:
        """For per-row lists of positions to record: the (row, columns, store) entries a hook fills, and
        {row: (the positions that fall in this chunk, the same store)}. A store maps layer -> tensor."""
        entries, stores = [], {}
        for row, positions in enumerate(requests or []):
            if not positions:
                continue
            inside, columns = self._locate(chunk, row, positions)
            if inside:
                stores[row] = ([positions[index] for index in inside], {})
                entries.append((row, columns, stores[row][1]))
        return entries, stores

    def _paths_in(self, chunk: Chunk, paths) -> list[PathEntry]:
        found = []
        for row, row_paths in enumerate(paths or []):
            for positions, value, kinds, *limits in row_paths or []:
                inside, columns = self._locate(chunk, row, positions)
                if inside:
                    window = tuple(limits[0]) if limits and limits[0] is not None else None
                    output_columns = limits[1] if len(limits) > 1 else None
                    found.append(PathEntry(row, columns, self._selected(inside), value, kinds, window,
                                           output_columns))
        return found

    def _head_patches_in(self, chunk: Chunk, zpatch) -> dict:
        """The head-output replacements whose tokens fall in this chunk: {block: [(row, columns, selected
        positions, value, the heads' columns or None for every head)]}."""
        found = {}
        for row, row_patches in enumerate(zpatch or []):
            for positions, value, columns_by_block in row_patches or []:
                inside, columns = self._locate(chunk, row, positions)
                if not inside:
                    continue
                selected = self._selected(inside)
                for block in (range(self.n_layers) if columns_by_block is None else columns_by_block):
                    head_columns = None if columns_by_block is None else columns_by_block[block]
                    found.setdefault(block, []).append((row, columns, selected, value, head_columns))
        return found

    def _mlp_patches_in(self, chunk: Chunk, mpatch) -> dict:
        """The MLP-output replacements whose tokens fall in this chunk: {block: [(row, columns, selected
        positions, value)]}."""
        found = {}
        for row, row_patches in enumerate(mpatch or []):
            for positions, value, blocks in row_patches or []:
                inside, columns = self._locate(chunk, row, positions)
                if not inside:
                    continue
                selected = self._selected(inside)
                for block in (range(self.n_layers) if blocks is None else blocks):
                    found.setdefault(int(block), []).append((row, columns, selected, value))
        return found

    def _per_head_attention_mask(self, chunk: Chunk, state: State, full_mask):
        """The causal attention mask written out in 4D [rows, 1, queries, keys], so that each block can be
        handed its own per-head version of it."""
        torch, device = self.torch, self.device
        cached, width = state.mask.shape[1], chunk.width
        dtype = next(self.lm.parameters()).dtype
        key_index = torch.arange(cached + width, device=device)[None, :]
        query_index = cached + torch.arange(width, device=device)[:, None]
        not_later = key_index <= query_index                                   # [queries, keys]
        allowed = (full_mask[:, None, None, :] == 1) & not_later[None, None]
        mask = torch.zeros((len(chunk.lengths), 1, width, cached + width), dtype=dtype, device=device)
        return mask.masked_fill_(~allowed, torch.finfo(dtype).min)

    def _clear_pass(self) -> None:
        """Forget the finished pass's interventions: the hooks read them on every forward pass, so anything
        left would act on the next one. (Steering is set and cleared by the sampling loop.)"""
        self._residual_edits, self._residual_captures = [], []
        self._barred, self._forced = None, None
        self._path_entries = []
        self._head_captures, self._head_patches, self._head_pairs = [], {}, None
        self._mlp_captures, self._mlp_patches = [], {}

    def _attention_to_groups(self, attentions, groups, chunk: Chunk, state: State) -> list[dict]:
        """The read-out of ``run(groups=...)``: per row {group name: [blocks, heads, 2]}."""
        torch, per_row = self.torch, []
        for row, named in enumerate(groups):
            first, last = chunk.first_column[row], chunk.first_column[row] + chunk.lengths[row]
            found = {}
            for name, positions in (named or {}).items():
                key_columns = torch.tensor([state.pad[row] + key for key in positions], device=self.device)
                to_group = torch.stack([block_attention[row][:, first:last][:, :, key_columns].float().sum(-1)
                                        for block_attention in attentions])        # [blocks, heads, queries]
                found[name] = torch.stack([to_group[:, :, -1], to_group.mean(-1)], -1).cpu()
            per_row.append(found)
        return per_row

    def _attention_of_heads(self, attentions, attn_heads, chunk: Chunk, full_mask) -> list:
        """The read-out of ``run(attn_heads=...)``: per row [heads, this chunk's tokens, the row's real tokens
        so far]."""
        torch, per_row = self.torch, []
        for row in range(len(chunk.lengths)):
            first, last = chunk.first_column[row], chunk.first_column[row] + chunk.lengths[row]
            # the row's real tokens so far, in order (a row shorter than its batch in an earlier later chunk
            # has padding between its cached tokens and this chunk's)
            real_keys = full_mask[row].nonzero()[:, 0][:chunk.earlier[row] + chunk.lengths[row]]
            per_row.append(torch.stack([attentions[block][row, head, first:last][:, real_keys].float()
                                        for block, head in attn_heads]))
        return per_row

    def _captured_per_row(self, stores: dict, rows: int, indices):
        """What the hooks recorded, per row: (positions, tensor [len(indices), positions, width]) or None for
        a row with nothing in this chunk; None if no row recorded anything."""
        if not stores:
            return None
        per_row = [None] * rows
        for row, (positions, store) in stores.items():
            per_row[row] = (positions, self.torch.stack([store[index] for index in indices]))
        return per_row

    def _chunk_inputs(self, seqs: list[list[int]], state: State | None):
        """The batch tensors of one chunk: (where its tokens sit, token ids [rows, width], position ids
        [rows, width], the attention mask over cached and new keys [rows, keys], the cache to extend or
        None)."""
        torch, device = self.torch, self.device
        rows, lengths = len(seqs), [len(tokens) for tokens in seqs]
        if min(lengths) < 1:
            raise ValueError("every row needs at least one token in a chunk")
        width, is_first = max(lengths), state is None
        earlier = [0] * rows if is_first else state.lengths
        # only a first chunk is left-padded
        first_column = [width - length if is_first else 0 for length in lengths]
        ids = torch.full((rows, width), self.pad, dtype=torch.long)
        mask = torch.zeros((rows, width), dtype=torch.long)
        for row, tokens in enumerate(seqs):
            ids[row, first_column[row]:first_column[row] + lengths[row]] = torch.tensor(tokens)
            mask[row, first_column[row]:first_column[row] + lengths[row]] = 1
        ids, mask = ids.to(device), mask.to(device)
        if is_first:
            position = (mask.cumsum(-1) - 1).clamp_min(0)
            full_mask, cache = mask, None
        else:
            from transformers import DynamicCache
            offsets = torch.arange(width, device=device)
            position = torch.tensor(earlier, device=device)[:, None] + offsets[None, :]
            full_mask = torch.cat([state.mask, mask], 1)
            cache = DynamicCache.from_legacy_cache(state.cache)
        return Chunk(lengths, width, earlier, first_column), ids, position, full_mask, cache

    def run(self, seqs: list[list[int]], *, state: State | None = None, edits=None, capture=None,
            keep: bool = True, knock=None, groups=None, paths=None, zcapture=None, zpatch=None,
            attn_heads=None, mcapture=None, mpatch=None, zpair=None, force=None):
        """Process one chunk per row. Returns (logits after each row's last token, the new state or None,
        captures). Every per-row argument has one entry per row; positions index the row's whole sequence and
        only those inside this chunk take effect.

        state       the state after the earlier chunks; None for a first chunk
        keep        whether to return the new state
        edits       per row, a list of ``Edit``
        capture     per row, positions whose residual stream to return: the row's capture is (positions,
                    tensor [layers + 1, positions, hidden]) for the positions in this chunk (index 0, the
                    embeddings, is left at zero), or None
        knock       (later chunks) per row {block: (heads, key positions in the first chunk)}: heads that may
                    not attend to those keys from this chunk's tokens
        force       (later chunks) per row {block: (heads, key positions)}: from every token of this chunk
                    those heads attend to those earlier tokens and to nothing else. With one key all of the
                    head's attention goes to it, and its output is its value-output map applied to that token:
                    the pointer is given, the copy is the model's. Applied after ``knock``
        groups      (later chunks) per row {name: key positions in the first chunk}; leaves in
                    ``self.attention`` per row {name: [blocks, heads, 2]}, the attention to those keys from
                    the row's last token and averaged over its tokens in this chunk
        attn_heads  (later chunks) a list of (block, head); leaves in ``self.attention_maps`` per row a tensor
                    [heads, this chunk's tokens, every token so far]
        paths       per row, entries as in ``Job.paths``
        zcapture    per row, positions whose per-head attention outputs to keep; leaves in ``self.zcaptured``
                    per row (positions, tensor [blocks, positions, heads x head_dim]) or None
        zpatch      per row a list of (positions, tensor [blocks, positions, heads x head_dim], columns): at
                    those positions the heads' outputs are replaced by the tensor's, for every head (columns
                    None) or for the columns ``head_dims`` gives ({block: columns}); later entries overwrite
                    earlier ones
        mcapture, mpatch
                    as zcapture and zpatch for the blocks' MLP outputs ([blocks, positions, hidden]; a patch
                    is (positions, tensor, blocks or None for every block)); captures land in
                    ``self.mcaptured``
        zpair       a list of (destination rows, source rows, columns by block or None): within this forward
                    pass the destination rows take the source rows' head outputs at every token (rows of equal
                    length)
        """
        torch, device = self.torch, self.device
        chunk, ids, position, full_mask, cache = self._chunk_inputs(seqs, state)
        rows, lengths, first_column = len(seqs), chunk.lengths, chunk.first_column
        self._residual_edits = self._residual_edits_in(chunk, edits)
        self._residual_captures, residual_stores = self._captures_in(chunk, capture)
        self._path_entries = self._paths_in(chunk, paths)
        self._head_captures, head_stores = self._captures_in(chunk, zcapture)
        self._head_patches = self._head_patches_in(chunk, zpatch)
        self._mlp_captures, mlp_stores = self._captures_in(chunk, mcapture)
        self._mlp_patches = self._mlp_patches_in(chunk, mpatch)
        attention_mask = full_mask
        self._barred = None if state is None else self._compile_knock(knock, state.pad)
        self._forced = None if state is None else self._compile_force(force, state.pad)
        if self._barred or self._forced:
            attention_mask = self._per_head_attention_mask(chunk, state, full_mask)
        self.attention = None
        self._head_pairs = None
        if zpair:
            self._head_pairs = [(torch.as_tensor(destination, device=device),
                                 torch.as_tensor(source, device=device), columns_by_block)
                                for destination, source, columns_by_block in zpair]
        try:
            with torch.inference_mode():
                out = self.lm.model(input_ids=ids, attention_mask=attention_mask, position_ids=position,
                                    past_key_values=cache, use_cache=True,
                                    output_attentions=bool(groups or attn_heads))
                last_column = torch.tensor([first_column[row] + lengths[row] - 1 for row in range(rows)],
                                           device=device)
                last_hidden = out.last_hidden_state[torch.arange(rows, device=device), last_column]
                logits = self.lm.lm_head(last_hidden).float()
                if groups:
                    self.attention = self._attention_to_groups(out.attentions, groups, chunk, state)
                self.attention_maps = None
                if attn_heads:
                    self.attention_maps = self._attention_of_heads(out.attentions, attn_heads, chunk,
                                                                   full_mask)
        finally:
            self._clear_pass()

        self.mcaptured = self._captured_per_row(mlp_stores, rows, range(self.n_layers))
        self.zcaptured = self._captured_per_row(head_stores, rows, range(self.n_layers))
        new_state = None
        if keep:
            new_state = State(out.past_key_values.to_legacy_cache(), full_mask,
                              [before + added for before, added in zip(chunk.earlier, lengths)],
                              first_column if state is None else state.pad)
        hidden = [None] * rows
        for row, (positions, store) in residual_stores.items():
            blocks = torch.stack([store[layer] for layer in range(1, self.n_layers + 1)])
            embeddings = torch.zeros_like(blocks[:1])       # hidden index 0 is not recorded
            hidden[row] = (positions, torch.cat([embeddings, blocks]))
        return logits, new_state, hidden

    # Read-outs
    def branches(self, tokens: list[int], state: State, knock=None):
        """Logits after each of ``tokens`` taken as alternative next tokens of every row: one pass in which
        the alternatives share a position and each sees the cached keys and itself, not the others.
        [rows, tokens, vocab]"""
        torch, device = self.torch, self.device
        from transformers import DynamicCache
        rows, alternatives, cached = len(state.lengths), len(tokens), state.mask.shape[1]
        dtype = next(self.lm.parameters()).dtype
        blocked = torch.finfo(dtype).min
        mask = torch.full((rows, 1, alternatives, cached + alternatives), blocked, dtype=dtype, device=device)
        mask[:, 0, :, :cached] = torch.zeros((rows, 1, cached), dtype=dtype, device=device).masked_fill_(
            state.mask[:, None, :] != 1, blocked)
        own = torch.arange(alternatives)
        mask[:, 0, own, cached + own] = 0      # each alternative sees itself
        ids = torch.tensor([tokens] * rows, device=device)
        position = torch.tensor(state.lengths, device=device)[:, None].expand(-1, alternatives)
        self._barred = self._compile_knock(knock, state.pad)
        try:
            with torch.inference_mode():
                cache = DynamicCache.from_legacy_cache(state.cache)
                out = self.lm.model(input_ids=ids, attention_mask=mask, position_ids=position,
                                    past_key_values=cache, use_cache=True)
                return self.lm.lm_head(out.last_hidden_state).float()
        finally:
            self._barred = None

    def two_digit(self, logits, state: State, knock=None):
        """The belief read from the two digits after the forced prefix: the probability whose clipped logit is
        the mean clipped logit over the answer distribution ("mean_logit"), or the expected probability. The
        second digit is computed for every first digit that has any weight. Returns (value, digit mass of the
        first token)."""
        torch = self.torch
        rows = logits.shape[0]
        first_digit = logits.softmax(-1)[:, self.digits]       # [rows, 10]
        mass = first_digit.sum(-1)
        first_share = first_digit / mass[:, None]
        if self.statistic == "expected_p":
            def score(probability):
                return probability
        else:
            def score(probability):
                probability = probability.clamp(0.01, 0.99)
                return torch.log(probability) - torch.log1p(-probability)
        tenths = torch.arange(10, device=self.device, dtype=torch.float32)
        # per first digit, the expected score of the answers that start with it; a first digit without weight
        # gets the middle of its interval
        expected = score(tenths / 10 + 0.045).repeat(rows, 1)
        candidates = [digit for digit in range(10) if float(first_share[:, digit].max()) >= 1e-5]
        second_logits = self.branches([self.digits[digit] for digit in candidates], state, knock)
        second_digit = second_logits.softmax(-1)[:, :, self.digits]            # [rows, candidates, 10]
        ends = (1 - second_digit.sum(-1)).clamp_min(0)                         # anything else ends the number
        first = torch.tensor(candidates, device=self.device, dtype=torch.float32)
        two_digit_scores = score(first[None, :, None] / 10 + tenths[None, None, :] / 100)
        one_digit_scores = score(first[None, :] / 10)
        expected[:, candidates] = (((second_digit * two_digit_scores).sum(-1) + ends * one_digit_scores)
                                   / (second_digit.sum(-1) + ends))
        value = (first_share * expected).sum(-1)
        if self.statistic != "expected_p":
            value = torch.sigmoid(value)
        return value.cpu().numpy(), mass.cpu().numpy()

    @staticmethod
    def _shared_prefix_length(job: Job) -> int:
        """How many leading tokens the job's belief sequence and its lookup questions share, ending before any
        knockout starts: the first chunk, computed once for all of them."""
        shared = len(job.ids) - 1
        for lookup_ids, _, _ in job.lookups:
            pairs = enumerate(zip(job.ids, lookup_ids))
            first_difference = next((index for index, (ours, theirs) in pairs if ours != theirs),
                                    min(len(job.ids), len(lookup_ids)))
            shared = min(shared, first_difference)
        return min(shared, job.knock[0]) if job.knock else shared

    def _evaluate(self, jobs: list[Job]) -> list[dict]:
        """One batch of ``evaluate``: the shared start of each job in one pass, then each lookup question and
        the belief sequence as later chunks over its cache."""
        count = len(jobs)
        shared = [self._shared_prefix_length(job) for job in jobs]
        edits, capture = [job.edits for job in jobs], [job.capture for job in jobs]
        knock = [job.knock[1] if job.knock else None for job in jobs]
        knock = knock if any(knock) else None
        paths = [job.paths for job in jobs] if any(job.paths for job in jobs) else None
        _, state, hidden_start = self.run([job.ids[:cut] for job, cut in zip(jobs, shared)], edits=edits,
                                          capture=capture, paths=paths)
        margins = [[None] * len(job.lookups) for job in jobs]
        for index in range(max((len(job.lookups) for job in jobs), default=0)):
            rows = [row for row, job in enumerate(jobs) if len(job.lookups) > index]
            row_state = state if len(rows) == count else state.select(rows)
            questions = [jobs[row].lookups[index][0][shared[row]:] for row in rows]
            lookup_logits, _, _ = self.run(questions, state=row_state, keep=False,
                                           knock=[knock[row] for row in rows] if knock else None)
            for row, row_logits in zip(rows, lookup_logits):
                person_token, own_token = jobs[row].lookups[index][1:]
                margins[row][index] = float(row_logits[person_token] - row_logits[own_token])
        logits, after, hidden_rest = self.run([job.ids[cut:] for job, cut in zip(jobs, shared)], state=state,
                                              edits=edits, capture=capture, knock=knock, paths=paths)
        del state       # the digits need only the cache that includes the question
        belief, mass = self.two_digit(logits, after, knock)
        del after
        results = [dict(p=float(belief[row]), digit_mass=float(mass[row]), lookups=margins[row], hidden=None)
                   for row in range(count)]
        for row, job in enumerate(jobs):
            if job.capture:
                results[row]["hidden"] = self._joined_capture(job, [hidden_start[row], hidden_rest[row]])
        return results

    def _joined_capture(self, job: Job, chunk_captures: list):
        """The job's captured residual stream [layers + 1, positions, hidden], in the order of
        ``job.capture``, joined from the two chunks' captures (and reduced, if the job asks for it)."""
        parts = [part for part in chunk_captures if part is not None]
        captured_positions = [position for part in parts for position in part[0]]
        column_of = {position: column for column, position in enumerate(captured_positions)}
        if set(column_of) != set(job.capture):
            raise ValueError("a capture position lies outside the sequence")
        joined = self.torch.cat([part[1] for part in parts], 1)
        hidden = joined[:, [column_of[position] for position in job.capture]]
        return job.reduce(hidden) if job.reduce else hidden

    def _halving(self, items: list, size: int, fn) -> list:
        """``fn`` on consecutive chunks of ``items``; a chunk that runs out of memory is retried at half the
        size, which is then kept."""
        torch = self.torch
        size = min(size, self.batch_cap or size)
        results, done = [], 0
        while done < len(items):
            try:
                results += fn(items[done:done + size])
            except torch.OutOfMemoryError:
                if size == 1:
                    raise
                torch.cuda.empty_cache()
                size = self.batch_cap = max(1, size // 2)
                print(f"[engine] out of memory: batch -> {size}", flush=True)
                continue
            done += size
        return results

    def evaluate(self, jobs: list[Job], batch: int = 32) -> list[dict]:
        """Per job: the belief ``p`` and ``digit_mass`` after the forced prefix, each lookup's logit
        difference (PERSON's value minus the model's), and the captured residual stream
        [layers + 1, positions, hidden]."""
        return self._halving(jobs, batch, self._evaluate)

    # Sampled answers
    def _run_prompts(self, jobs: list[Job], static: bool):
        """The prompts of one batch of ``generate``, in one pass or in two where an intervention acts from a
        token on. Returns (logits after each prompt, state, per-job knockouts, (leader, follower, columns) of
        each paired job)."""
        edits = [job.edits for job in jobs]
        knock = [job.knock[1] if job.knock else None for job in jobs]
        followers = [(job.follow, index, job.pair) for index, job in enumerate(jobs)
                     if job.follow is not None]
        if followers:       # two chunks: the records, then everything after them under the pairing
            if any(knock) or not static or any(job.split is None for job in jobs):
                raise ValueError("paired sampling needs the preallocated cache, a split for every job, "
                                 "and no knockouts")
            _, state, _ = self.run([job.ids[:job.split] for job in jobs], edits=edits)
            pairs = [([leader], [follower], columns) for leader, follower, columns in followers]
            logits, state, _ = self.run([job.ids[job.split:] for job in jobs], state=state, edits=edits,
                                        zpair=pairs)
        elif any(knock):    # two chunks, so that the question's tokens are under the knockout too
            cuts = [job.knock[0] if job.knock else len(job.ids) - 1 for job in jobs]
            _, state, _ = self.run([job.ids[:cut] for job, cut in zip(jobs, cuts)], edits=edits)
            logits, state, _ = self.run([job.ids[cut:] for job, cut in zip(jobs, cuts)], state=state,
                                        edits=edits, knock=knock)
        else:       # (key and value paths of the prompt's tokens stay in the cache for the whole reply)
            paths = [job.paths for job in jobs] if any(job.paths for job in jobs) else None
            logits, state, _ = self.run([job.ids for job in jobs], edits=edits, paths=paths)
        return logits, state, knock, followers

    def _steering_of_rows(self, jobs: list[Job], job_of_row: list[int], static: bool):
        """{hidden index: [rows, hidden]}: the vector added to each sampled row's residual stream at every
        sampled token (zero for rows whose job has none); None if no job steers."""
        if not any(job.steer for job in jobs):
            return None
        if not static:
            raise ValueError("steering sampled tokens needs the preallocated cache")
        steering = {}
        for row, job_index in enumerate(job_of_row):
            if not jobs[job_index].steer:
                continue
            value, scale, (first_layer, last_layer) = jobs[job_index].steer
            for layer in range(first_layer, last_layer + 1):
                if layer not in steering:
                    steering[layer] = self.torch.zeros((len(job_of_row), self.hidden), device=self.device)
                steering[layer][row] = value[layer].float() * float(scale)
        return steering

    def _preallocated(self, state: State, max_new_tokens: int):
        """The prompts' cache copied into one with room for the reply: (cache, mask [rows, keys + room], each
        row's prompt length). Written in place from then on: appending to a growing cache copies all of it at
        every token."""
        torch, device = self.torch, self.device
        rows, cached = state.mask.shape
        cache = growing_cache(self.lm.config, rows, cached + max_new_tokens, device,
                              next(self.lm.parameters()).dtype)
        for block, (keys, values) in enumerate(state.cache):
            cache.key_cache[block][:, :, :cached] = keys
            cache.value_cache[block][:, :, :cached] = values
        room = torch.ones((rows, max_new_tokens), dtype=torch.long, device=device)
        return cache, torch.cat([state.mask, room], 1), torch.tensor(state.lengths, device=device)

    def _keep_slots(self, cache, mask, lengths, steering, live_slots: list[int]):
        """Drop the finished rows from the preallocated cache and from what is indexed like it: returns the
        mask, the prompt lengths and the steering vectors of the rows kept."""
        live_index = self.torch.tensor(live_slots, device=self.device)
        cache.key_cache = [keys[live_index] for keys in cache.key_cache]
        cache.value_cache = [values[live_index] for values in cache.value_cache]
        cache.max_batch_size = len(live_slots)
        if steering is not None:
            steering = {layer: vectors[live_index] for layer, vectors in steering.items()}
        return mask[live_index], lengths[live_index], steering

    def _next_logits(self, cache, mask, lengths, next_tokens: list[int], cached: int, step: int):
        """One sampled token per row appended to the preallocated cache: the logits after it. [rows, vocab]"""
        torch, device = self.torch, self.device
        cache.filled = cached + step + 1
        with torch.inference_mode():
            out = self.lm.model(input_ids=torch.tensor(next_tokens, device=device)[:, None],
                                attention_mask=mask[:, :cached + step + 1],
                                position_ids=(lengths + step)[:, None], past_key_values=cache,
                                cache_position=torch.tensor([cached + step], device=device), use_cache=True)
            return self.lm.lm_head(out.last_hidden_state[:, -1]).float()

    def _generate(self, jobs: list[Job], samples: int, max_new_tokens: int, generator,
                  static: bool) -> list[list[dict]]:
        """One batch of ``generate``: per job, ``samples`` dicts (text, n_tokens, truncated). All rows draw
        their next tokens from one generator in one call, so a reply depends on which jobs share its batch."""
        torch, device = self.torch, self.device
        logits, state, knock, followers = self._run_prompts(jobs, static)
        job_of_row = [job_index for job_index in range(len(jobs)) for _ in range(samples)]
        rows = len(job_of_row)
        state, logits = state.select(job_of_row), logits[torch.tensor(job_of_row, device=device)]
        knock = [knock[job_index] for job_index in job_of_row] if any(knock) else None
        barred = self._compile_knock(knock, state.pad)
        cached = state.mask.shape[1]
        steering = self._steering_of_rows(jobs, job_of_row, static)
        if static:
            cache, mask, lengths = self._preallocated(state, max_new_tokens)
            state = None
        alive = list(range(rows))                      # the rows still in the batch, in the batch's order
        tokens, stopped = [[] for _ in range(rows)], [False] * rows
        row_pairs = [(leader * samples + sample, follower * samples + sample, columns)
                     for leader, follower, columns in followers for sample in range(samples)]
        for step in range(max_new_tokens):
            next_tokens = torch.multinomial(logits.softmax(-1), 1, generator=generator)[:, 0].tolist()
            slot_of = {row: slot for slot, row in enumerate(alive)}
            for leader, follower, _ in row_pairs:       # a follower reads its leader's token
                if leader in slot_of and follower in slot_of:
                    next_tokens[slot_of[follower]] = next_tokens[slot_of[leader]]
            for row, token in zip(alive, next_tokens):
                if stopped[row]:
                    continue
                if token in self.stops:
                    stopped[row] = True
                else:
                    tokens[row].append(token)
            live_slots = [slot for slot, row in enumerate(alive) if not stopped[row]]
            if not live_slots or step == max_new_tokens - 1:
                break
            if len(live_slots) <= len(alive) // 2 and not knock:     # drop finished rows once half are done
                alive = [alive[slot] for slot in live_slots]
                next_tokens = [next_tokens[slot] for slot in live_slots]
                if static:
                    mask, lengths, steering = self._keep_slots(cache, mask, lengths, steering, live_slots)
                else:
                    state = state.select(live_slots)
            if not static:
                logits, state, _ = self.run([[token] for token in next_tokens], state=state, knock=knock)
                continue
            self._barred, self._steering = barred, steering
            if row_pairs:
                slot_of = {row: slot for slot, row in enumerate(alive)}
                self._head_pairs = [(torch.tensor([slot_of[leader]], device=device),
                                     torch.tensor([slot_of[follower]], device=device), columns)
                                    for leader, follower, columns in row_pairs
                                    if leader in slot_of and follower in slot_of]
            try:
                logits = self._next_logits(cache, mask, lengths, next_tokens, cached, step)
            finally:
                self._barred, self._steering, self._head_pairs = None, None, None
        replies = [[] for _ in jobs]
        for row, job_index in enumerate(job_of_row):
            replies[job_index].append(dict(text=self.tok.decode(tokens[row], skip_special_tokens=True),
                                           n_tokens=len(tokens[row]), truncated=not stopped[row]))
        return replies

    def generate(self, jobs: list[Job], samples: int, max_new_tokens: int, seed: int, batch: int = 16,
                 static: bool = True):
        """``samples`` sampled replies per job (temperature 1, no truncation of the distribution), with each
        job's interventions applied. ``batch`` counts jobs, so a pass holds batch x samples rows.
        ``static``: write new keys into a preallocated cache instead of growing one (same replies, less
        copying)."""
        generator = self.torch.Generator(device=self.device).manual_seed(int(seed))

        def sample_batch(part):
            return self._generate(part, samples, max_new_tokens, generator, static)
        if any(job.follow is not None for job in jobs):
            # ``follow`` is an index into the job list, so a pair cannot be split over batches
            if batch < len(jobs):
                raise ValueError(f"paired jobs must be sampled in one batch: {len(jobs)} jobs, batch {batch}")
            return sample_batch(jobs)
        return self._halving(jobs, batch, sample_batch)


def to_device(tree, device):
    """Tensors of a (nested) dict moved to ``device``."""
    return {key: to_device(value, device) if isinstance(value, dict) else value.to(device)
            for key, value in tree.items()}


def half_bits(tensor) -> np.ndarray:
    """A tensor as float16 numpy."""
    return tensor.detach().to("cpu").float().numpy().astype(np.float16)
