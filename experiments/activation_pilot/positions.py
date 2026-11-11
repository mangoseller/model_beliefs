"""Fixed, label-independent extraction rules for the token-position pilot."""
import hashlib
import json
from pathlib import Path

import numpy as np

POSITION_NAMES = ('ckd_end', 'proposition_period', 'user_end_of_turn',
                  'assistant_prefix_end', 'proposition_mean')
PROPOSITION = 'The proposition of interest is: this patient has CKD.'
PROMPTS = Path('data/release/prompts/ckd/ckd_same1000_threshold_05pct_revised.jsonl')
SCHEMA_VERSION = 1


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prompt_for(row):
    return ('Consider whether the following patient has chronic kidney disease (CKD).'
            '\n\nPatient profile:\n' + row['patient_profile'] + '\n\n' + PROPOSITION)


def locate(tokenizer, prompt):
    """Return exact token IDs and token spans; do not consult any response/label."""
    messages = [{'role': 'user', 'content': prompt}]
    rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    encoded = tokenizer(rendered, add_special_tokens=False, return_offsets_mapping=True)
    ids = encoded['input_ids']
    if ids != tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True):
        raise ValueError('Offset tokenization differs from model input tokenization')
    start = rendered.rindex(PROPOSITION)
    end = start + len(PROPOSITION)
    if not prompt.endswith(PROPOSITION) or rendered[start:end] != PROPOSITION:
        raise ValueError('Expected concluding proposition is absent')
    offsets = encoded['offset_mapping']

    def at(character):
        matches = [i for i, (a, b) in enumerate(offsets) if a <= character < b]
        if len(matches) != 1:
            raise ValueError(f'Ambiguous character-to-token alignment at {character}')
        return matches[0]

    ckd_end = at(end - 2)  # D, the last character before the final period.
    period = at(end - 1)
    turn_end = period + 1
    if tokenizer.convert_ids_to_tokens(ids[turn_end]) != '<end_of_turn>':
        raise ValueError('Expected end-of-turn token immediately after proposition')
    if not rendered.endswith('<start_of_turn>model\n'):
        raise ValueError('Unexpected assistant-generation prefix')
    if offsets[-1] != (len(rendered) - 1, len(rendered)):
        raise ValueError('Final input token is not the assistant-prefix newline')
    prop = [i for i, (a, b) in enumerate(offsets) if b > start and a < end]
    if not prop or prop != list(range(prop[0], period + 1)):
        raise ValueError('Proposition span must be contiguous and include its period')
    spans = [[ckd_end], [period], [turn_end], [len(ids) - 1], prop]
    return np.asarray(ids, dtype=np.int64), spans


def valid_cache(path, ids, spans, shape, fingerprint):
    """Resume only a complete, finite cache with matching input and provenance."""
    try:
        with np.load(path, allow_pickle=False) as saved:
            h = saved['h']
            return (h.shape == shape and h.dtype == np.float32 and np.isfinite(h).all()
                    and saved['fingerprint'].item() == fingerprint
                    and saved['position_names'].tolist() == list(POSITION_NAMES)
                    and np.array_equal(saved['token_ids'], ids)
                    and json.loads(saved['position_token_indices'].item()) == spans)
    except (OSError, ValueError, KeyError, EOFError):
        return False
