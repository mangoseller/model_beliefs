"""Prefit existing grid candidates alongside a lock-aware cell search."""
import argparse

import numpy as np
import torch

from experiments.joint_beliefs.run import (ROOT, PROTOCOL, frame, get_baseline,
                                          one_candidate, covariates, activation_arrays, standardize)
from latent_beliefs.latent.domains import CKD_SPECS, likelihood_values


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size',choices=['4b','12b'],required=True)
    parser.add_argument('--curve',choices=['linear','monotone_spline'],required=True)
    parser.add_argument('--activation-tail',action='store_true',
                        help='Prefit the last layer at the last two predeclared penalties instead of raw controls')
    args=parser.parse_args()
    torch.set_num_threads(PROTOCOL['threads'])
    torch.manual_seed(PROTOCOL['seed'])
    cell=ROOT/args.size/args.curve
    if not (cell/'baseline'/'result.json').exists():
        raise RuntimeError('Prepare the behavioral baseline in the main search first')
    if (cell/'selection.json').exists():
        raise RuntimeError('Cell selection is already complete')
    df=frame(args.size)
    train=np.flatnonzero(df.split=='train')
    val=np.flatnonzero(df.split=='val')
    assert (len(train),len(val))==(313,84)
    y=torch.tensor(likelihood_values(df,CKD_SPECS),dtype=torch.float64)
    base,_=get_baseline(cell,args.curve,y,train,val)
    if args.activation_tail:
        activations=activation_arrays(args.size,df)
        layer=max(activations)
        x,_=standardize(activations[layer],train)
        prefix=f'activation_l{layer}'
        penalties=PROTOCOL['penalties'][-2:]
    else:
        x=covariates(df,train)
        prefix='raw_profile'
        penalties=PROTOCOL['penalties']
    for penalty in penalties:
        print(f'PREFIT {args.size} {args.curve} {prefix} L2={penalty}',flush=True)
        one_candidate(cell/'candidates'/f'{prefix}_p{penalty:g}',base,y,x,train,val,penalty)
    print('CANDIDATE PREFITTING COMPLETE',flush=True)


if __name__=='__main__':
    main()
