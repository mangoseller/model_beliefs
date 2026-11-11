"""Audit retained train/validation response coding and full-dataset coverage."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from experiments.werewolf_beliefs.data import PROMPTS,DATASET,common_cases
from experiments.werewolf_beliefs.run import frame,WEREWOLF_SPECS,ROOT,save_json
from latent_beliefs.werewolf.parse import parse_response

def main():
    metadata={r['case_id']:r for r in (json.loads(line) for line in PROMPTS.read_text().splitlines())}
    result=[]
    for size in ['4b','12b']:
        df=frame(size).set_index('source_prompt_id')
        for spec in WEREWOLF_SPECS:
            checked=0;errors=[];seen=set()
            path=Path('data/release/responses/werewolf')/f'werewolf_same500_{spec.name}'/f'gemma3_{size}.jsonl'
            for line in path.read_text().splitlines():
                row=json.loads(line)
                case=row['prompt_id'].removeprefix('werewolf500__').removesuffix('__'+spec.name)
                if case not in df.index: continue
                assert case not in seen;seen.add(case)
                value,method=parse_response(spec.name,row['output_text'],target_player=metadata[case]['target_player'],truncated=str(row.get('stop_reason') or '').lower()=='max_tokens')
                expected=float(df.at[case,spec.name])
                if value is None or not np.isclose(value,expected,rtol=0,atol=1e-12):
                    errors.append(dict(case=case,parsed=value,dataset=expected))
                checked+=1
            assert checked==325,(size,spec.name,checked)
            result.append(dict(size=size,target=spec.name,checked=checked,mismatches=errors))
    summary=dict(checked=sum(r['checked'] for r in result),mismatch_count=sum(len(r['mismatches']) for r in result),partitions=['train','val'],outputs=result)
    save_json(ROOT/'response_coding_audit_train_validation.json',summary)
    assert summary['mismatch_count']==0
    all_data=pd.read_csv(DATASET)
    common=common_cases()
    groups={size:set(all_data.loc[all_data.model_key=='gemma3_'+size,'source_prompt_id']) for size in ['4b','12b']}
    coverage=dict(original_cases=len(metadata),paired_cases=len(common),complete_cases={s:len(cases) for s,cases in groups.items()},
                  paired_split=frame('4b',True).split.value_counts().to_dict(),
                  excluded_from_pairing={s:sorted(cases-common) for s,cases in groups.items()},
                  incomplete_cases={s:sorted(set(metadata)-cases) for s,cases in groups.items()},
                  limitation='Primary estimates condition on complete responses in both sizes; excluded cases were not regenerated.',
                  main_domains=['ckd','forecasting','werewolf'],
                  branching='Separate reasoning-prefix follow-up experiments reuse the three main domains; not an additional main-domain dataset.')
    save_json(ROOT/'coverage.json',coverage)
    print('Checked',summary['checked'],'original train/validation responses; mismatches',summary['mismatch_count'])
    print('Paired coverage:',coverage['paired_cases'],'/',coverage['original_cases'])

if __name__=='__main__':main()
