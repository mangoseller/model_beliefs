"""Reparse original train/validation responses and compare scenario-keyed values."""
import json
from pathlib import Path
import numpy as np
from experiments.forecast_beliefs.run import frame,FORECASTING_SPECS,ROOT,save_json
from latent_beliefs.forecasting.parse import parse_output

def main():
    result=[]
    for size in ['4b','12b']:
        df=frame(size).set_index('source_prompt_id')
        for spec in FORECASTING_SPECS:
            checked=0;errors=[];seen=set()
            path=Path('data/release/responses/forecasting')/spec.name/f'gemma3_{size}.jsonl'
            for line in path.read_text().splitlines():
                row=json.loads(line)
                case=row['prompt_id'].removesuffix('__'+spec.name)
                if case not in df.index: continue
                assert case not in seen
                seen.add(case)
                actual=parse_output(spec.name,row['output_text'])['value']
                expected=float(df.at[case,spec.name])
                if actual is None or not np.isclose(actual,expected,rtol=0,atol=1e-12):
                    errors.append(dict(case=case,parsed=actual,dataset=expected))
                checked+=1
            assert checked==400
            result.append(dict(size=size,target=spec.name,checked=checked,mismatches=errors))
    summary=dict(checked=sum(r['checked'] for r in result),mismatch_count=sum(len(r['mismatches']) for r in result),partitions=['train','val'],outputs=result)
    save_json(ROOT/'response_coding_audit_train_validation.json',summary)
    print('Checked',summary['checked'],'train/validation responses; mismatches',summary['mismatch_count'])
    assert summary['mismatch_count']==0

if __name__=='__main__':main()
