"""Complete extraction, selection, locked evaluation and report; resume safely."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path('build/forecast_beliefs')
FIT=ROOT/'full_experiment'
PYTHON=sys.executable
jobs={}
handles=[]

def complete(path):
    return path.exists() and json.loads(path.read_text()).get('complete',False)

def start(name,module,args,mlx=False):
    handle=(ROOT/f'{name}.log').open('a')
    handles.append(handle)
    env=dict(os.environ,PYTHONPATH='build/pilot_dependencies:.' if mlx else '.')
    process=subprocess.Popen([PYTHON,'-u','-m',module,*args],env=env,stdout=handle,stderr=subprocess.STDOUT)
    jobs[name]=process
    print(f'START {name} pid={process.pid}',flush=True)
    return process

def check():
    for name,p in jobs.items():
        if p.poll() not in [None,0]: raise RuntimeError(f'{name} failed ({p.returncode}); see {ROOT/name}.log')
    status=dict(updated=time.time(),jobs={name:dict(pid=p.pid,returncode=p.poll()) for name,p in jobs.items()})
    tmp=ROOT/'orchestrator_status.tmp'
    tmp.write_text(json.dumps(status,indent=2))
    tmp.replace(ROOT/'orchestrator_status.json')

def until(predicate):
    while not predicate():
        check()
        time.sleep(5)
    check()

def main():
    lock=(ROOT/'.orchestrator.lock').open('a')
    import fcntl
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    awake=subprocess.Popen(['caffeinate','-i','-w',str(os.getpid())])
    try:
        # 4B extraction and both baselines were launched before this coordinator.
        until(lambda: complete(ROOT/'4b/extraction/extraction.json'))
        if not complete(ROOT/'12b/extraction/extraction.json'):
            start('extract_12b','experiments.forecast_beliefs.extract',['--size','12b'],mlx=True)
        for size in ['4b','12b']:
            until(lambda: complete(ROOT/size/'extraction/extraction.json') and
                  all((FIT/size/c/'baseline/result.json').exists() for c in ['linear','monotone_spline']))
            if not all((FIT/size/c/'selection.json').exists() for c in ['linear','monotone_spline']):
                start(f'search_{size}','experiments.forecast_beliefs.run',['search','--size',size])
        until(lambda: all((FIT/s/c/'selection.json').exists() for s in ['4b','12b'] for c in ['linear','monotone_spline']))
        subprocess.run([PYTHON,'-m','experiments.forecast_beliefs.run','lock'],check=True,env=dict(os.environ,PYTHONPATH='.'))
        for size in ['4b','12b']:
            start(f'final_{size}','experiments.forecast_beliefs.run',['final','--size',size])
        until(lambda: all(p.poll()==0 for p in jobs.values()))
        subprocess.run([PYTHON,'-m','experiments.forecast_beliefs.report'],check=True,env=dict(os.environ,PYTHONPATH='.'))
        print('EXPERIMENT COMPLETE',flush=True)
    finally:
        awake.terminate()
        for h in handles: h.close()

if __name__=='__main__': main()
