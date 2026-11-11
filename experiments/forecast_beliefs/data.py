"""Forecast inputs: only the materials visible to the responding model."""
import json
from pathlib import Path
import numpy as np
import torch
from sklearn.feature_extraction.text import TfidfVectorizer
from latent_beliefs.forecasting.prompts import parse_framings

PROMPTS = Path('data/release/prompts/forecasting/probability_positive.jsonl')
PROPOSITION = 'The proposition of interest is: the true population mean outcome under Condition A is strictly greater than the true population mean outcome under Condition B.'

def prompts():
    suffix = '\n\n' + parse_framings()['probability_positive']
    result = {}
    for line in PROMPTS.read_text().splitlines():
        row = json.loads(line)
        assert row['prompt'].endswith(suffix)
        result[row['scenario_id']] = row['prompt'][:-len(suffix)] + '\n\n' + PROPOSITION
    assert len(result) == 500
    return result

def covariates(df, fit):
    texts = prompts()
    documents = [texts[c] for c in df.source_prompt_id]
    vectorizer = TfidfVectorizer(ngram_range=(1,2), min_df=2, max_features=2048, dtype=np.float64)
    vectorizer.fit([documents[i] for i in fit])
    raw = vectorizer.transform(documents).toarray()
    mean = raw[fit].mean(0)
    scale = max(float(np.sqrt(np.mean((raw[fit]-mean)**2))), 1e-8)
    return torch.tensor((raw-mean)/scale, dtype=torch.float64)
