"""Cache integrity, token alignment, and probe isolation regression checks."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import torch
from transformers import AutoTokenizer

import experiments.activation_pilot.fit as base
from experiments.activation_pilot.position_search import fit_candidate, load_features
from experiments.activation_pilot.positions import POSITION_NAMES, locate, prompt_for, valid_cache


class PositionTests(unittest.TestCase):
    def test_semantic_alignment_with_repeated_proposition(self):
        tokenizer = AutoTokenizer.from_pretrained('build/activation_pilot/model', local_files_only=True)
        # A repeated phrase in the profile must not move the concluding-proposition anchor.
        prompt = prompt_for({'patient_profile': 'CKD? The proposition of interest is: this patient has CKD.'})
        ids, spans = locate(tokenizer, prompt)
        self.assertEqual(tokenizer.decode(ids[spans[0]]), 'D')
        self.assertEqual(tokenizer.decode(ids[spans[1]]), '.')
        self.assertEqual(tokenizer.decode(ids[spans[2]]), '<end_of_turn>')
        self.assertEqual(tokenizer.decode(ids[spans[3]]), '\n')
        self.assertEqual(tokenizer.decode(ids[spans[4]]),
                         'The proposition of interest is: this patient has CKD.')
        self.assertEqual(spans[3], [len(ids) - 1])

    def test_cache_provenance_and_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'activations').mkdir()
            shape = (2, 5, 3)
            h = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
            ids, spans = np.arange(8), [[0], [1], [2], [7], [0, 1]]
            path = root / 'activations/case.npz'
            np.savez(path, h=h, token_ids=ids, position_token_indices=json.dumps(spans),
                     fingerprint='test', case_id='case', position_names=POSITION_NAMES)
            (root / 'extraction.json').write_text(json.dumps(dict(
                fingerprint='test', position_names=POSITION_NAMES, shape_per_case=shape)))
            self.assertTrue(valid_cache(path, ids, spans, shape, 'test'))
            self.assertFalse(valid_cache(path, ids + 1, spans, shape, 'test'))
            self.assertFalse(valid_cache(path, ids, spans, shape, 'different'))
            loaded = load_features(root, ['case'], ['proposition_mean', 'ckd_end'], [2])
            np.testing.assert_array_equal(loaded[0, 0], h[1, [4, 0]])
            h[0, 0, 0] = np.inf
            np.savez(path, h=h, token_ids=ids, position_token_indices=json.dumps(spans),
                     fingerprint='test', case_id='case', position_names=POSITION_NAMES)
            self.assertFalse(valid_cache(path, ids, spans, shape, 'test'))
            with self.assertRaises(ValueError):
                load_features(root, ['case'], ['ckd_end'], [1])

    def test_validation_cannot_change_fitted_probe(self):
        torch.set_num_threads(2)
        base.FLEXIBLE = True
        torch.manual_seed(1)
        model = base.new_model()
        base.freeze(model)
        rng = np.random.default_rng(4)
        raw = rng.normal(size=(24, 5))
        y = torch.tensor(rng.integers(0, 2, (24, 12)), dtype=torch.float64)
        train, val = np.arange(18), np.arange(18, 24)
        _, prediction, params = fit_candidate(model, raw, y, train, val, .1)
        altered = y.clone()
        altered[val, 8:] = 1 - altered[val, 8:]
        _, other_prediction, other_params = fit_candidate(model, raw, altered, train, val, .1)
        np.testing.assert_array_equal(params['w'], other_params['w'])
        np.testing.assert_array_equal(prediction, other_prediction)
        changed_raw = raw.copy()
        changed_raw[val] += 1000
        _, _, changed = fit_candidate(model, changed_raw, y, train, val, .1)
        np.testing.assert_array_equal(params['mean'], changed['mean'])
        self.assertEqual(params['scale'], changed['scale'])
        np.testing.assert_array_equal(params['w'], changed['w'])


if __name__ == '__main__':
    unittest.main()
