"""Werewolf inputs restricted to player-visible evidence and paired complete cases."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from sklearn.feature_extraction.text import TfidfVectorizer
from latent_beliefs.werewolf.prompts import SYSTEM_PROMPT, FAMILIES, question_block

PROMPTS=Path('data/release/prompts/werewolf/werewolf_same500_probability.jsonl')
DATASET=Path('build/datasets/werewolf.csv')

def common_cases():
    df=pd.read_csv(DATASET,usecols=['model_key','source_prompt_id'])
    common=set(df.loc[df.model_key=='gemma3_4b','source_prompt_id']) & set(df.loc[df.model_key=='gemma3_12b','source_prompt_id'])
    assert len(common)==405
    return common

def neutral_prompt(row):
    question,_=question_block(FAMILIES[0],row['target_player'])
    prefix=SYSTEM_PROMPT+'\n\n'+question+'\n\n'
    assert row['prompt'].startswith(prefix)
    context=row['prompt'][len(prefix):]
    assert context.startswith('GAME STATE AT THE CUT\n')
    return SYSTEM_PROMPT+'\n\n'+context+'\n\n'+f'The proposition of interest is: player "{row["target_player"]}" is a Werewolf.'

def prompts():
    common=common_cases()
    result={}
    for line in PROMPTS.read_text().splitlines():
        row=json.loads(line)
        if row['case_id'] in common:
            result[row['case_id']]=neutral_prompt(row)
    assert set(result)==common
    return result

def covariates(df,fit):
    texts=prompts()
    documents=[texts[c] for c in df.source_prompt_id]
    vectorizer=TfidfVectorizer(ngram_range=(1,2),min_df=2,max_features=2048,dtype=np.float64)
    vectorizer.fit([documents[i] for i in fit])
    raw=vectorizer.transform(documents).toarray()
    mean=raw[fit].mean(0)
    scale=max(float(np.sqrt(np.mean((raw[fit]-mean)**2))),1e-8)
    return torch.tensor((raw-mean)/scale,dtype=torch.float64)
