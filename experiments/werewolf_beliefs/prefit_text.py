"""Fit planned text candidates while GPU activation extraction runs."""
import argparse
import numpy as np
import torch
from experiments.werewolf_beliefs.run import *

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--size',choices=SIZES,required=True)
    args=p.parse_args()
    initialize()
    torch.set_num_threads(PROTOCOL['threads'])
    torch.manual_seed(PROTOCOL['seed'])
    df=frame(args.size)
    train,val=np.flatnonzero(df.split=='train'),np.flatnonzero(df.split=='val')
    y=torch.tensor(likelihood_values(df,WEREWOLF_SPECS),dtype=torch.float64)
    x=covariates(df,train)
    for curve in CURVES:
        cell=ROOT/args.size/curve
        base,_=get_baseline(cell,curve,y,train,val)
        for penalty in PROTOCOL['penalties']:
            print(f'TEXT {args.size} {curve} penalty={penalty}',flush=True)
            one_candidate(cell/'candidates'/f'raw_profile_p{penalty:g}',base,y,x,train,val,penalty)

if __name__=='__main__':main()
