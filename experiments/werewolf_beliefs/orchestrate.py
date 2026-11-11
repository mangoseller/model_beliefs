"""Resume the complete Werewolf experiment with sequential GPU extraction and rolling tests."""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT=Path('build/werewolf_beliefs')
FIT=ROOT/'full_experiment'
SIZES=('4b','12b');CURVES=('linear','monotone_spline')
METHODS=('behavior','activation','raw_profile','shuffled','activation_only')
PYTHON=sys.executable
jobs={};handles=[]

def save(name,value):
    p=ROOT/name;tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2));tmp.replace(p)

def start(name,module,args,mlx=False):
    h=(ROOT/f'{name}.log').open('a');handles.append(h)
    p=subprocess.Popen([PYTHON,'-u','-m',module,*args],stdout=h,stderr=subprocess.STDOUT,
        env=dict(os.environ,PYTHONPATH='build/pilot_dependencies:.' if mlx else '.'))
    jobs[name]=p
    print(f'START {name} pid={p.pid}',flush=True)

def cache_complete(size):
    p=ROOT/size/'extraction/extraction.json'
    return p.exists() and json.loads(p.read_text()).get('complete',False)

def final_complete(size,curve):
    return all((FIT/size/curve/'final'/f'{m}_complete.json').exists() for m in METHODS)

def run_stage(args):
    subprocess.run([PYTHON,'-m','experiments.werewolf_beliefs.run',*args],check=True,env=dict(os.environ,PYTHONPATH='.'))

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    lock=(ROOT/'.orchestrator.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    signal.signal(signal.SIGTERM,lambda *_: sys.exit(143))
    awake=subprocess.Popen(['caffeinate','-i','-w',str(os.getpid())])
    try:
        run_stage(['initialize'])
        for size in SIZES:
            start(f'text_{size}','experiments.werewolf_beliefs.prefit_text',['--size',size])
        last_power=0
        while True:
            for name,p in jobs.items():
                if p.poll() not in [None,0]: raise RuntimeError(f'{name} failed ({p.returncode}); see {ROOT/name}.log')
            if time.monotonic()-last_power>60:
                power=subprocess.check_output(['pmset','-g','batt'],text=True)
                pct=re.search(r'(\d+)%',power)
                if 'Battery Power' in power and pct and int(pct.group(1))<=10:
                    save('power_interruption.json',dict(status='waiting_for_external_power',battery=power,timestamp=time.time()))
                    raise RuntimeError('Battery at or below 10%; saved progress requires external power to resume.')
                last_power=time.monotonic()
            # Only one model occupies GPU memory at a time.
            for size in SIZES:
                if not cache_complete(size):
                    name=f'extract_{size}'
                    if name not in jobs: start(name,'experiments.werewolf_beliefs.extract',['--size',size],mlx=True)
                    break
                if f'extract_{size}' in jobs and jobs[f'extract_{size}'].poll() is None: break
            for size in SIZES:
                selected=all((FIT/size/c/'selection.json').exists() for c in CURVES)
                if cache_complete(size) and jobs[f'text_{size}'].poll()==0 and not selected and f'search_{size}' not in jobs:
                    start(f'search_{size}','experiments.werewolf_beliefs.run',['search','--size',size])
                for curve in CURVES:
                    name=f'final_{size}_{curve}'
                    if (FIT/size/curve/'selection.json').exists() and not final_complete(size,curve) and name not in jobs:
                        run_stage(['lock','--size',size,'--curve',curve])
                        start(name,'experiments.werewolf_beliefs.run',['final','--size',size,'--curve',curve])
                name=f'interim_{size}'
                if all(final_complete(size,c) for c in CURVES) and name not in jobs:
                    start(name,'experiments.werewolf_beliefs.interim_report',['--size',size])
            save('orchestrator_status.json',dict(updated=time.time(),jobs={n:dict(pid=p.pid,returncode=p.poll()) for n,p in jobs.items()}))
            if all(final_complete(s,c) for s in SIZES for c in CURVES) and all(p.poll()==0 for p in jobs.values()): break
            time.sleep(5)
        run_stage(['lock'])
        subprocess.run([PYTHON,'-m','experiments.werewolf_beliefs.report'],check=True,env=dict(os.environ,PYTHONPATH='.'))
        subprocess.run([PYTHON,'-m','experiments.werewolf_beliefs.cross_domain'],check=True,env=dict(os.environ,PYTHONPATH='.'))
        print('EXPERIMENT COMPLETE',flush=True)
    finally:
        for p in jobs.values():
            if p.poll() is None: p.terminate()
        for p in jobs.values():
            if p.poll() is None:
                try: p.wait(timeout=5)
                except subprocess.TimeoutExpired: p.kill()
        awake.terminate()
        for h in handles: h.close()

if __name__=='__main__': main()
