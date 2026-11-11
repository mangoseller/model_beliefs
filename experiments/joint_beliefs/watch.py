"""Finish and report cells as the already-running searches produce selections.

Does not alter fitting settings or start new searches. Run only one watcher.
"""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT=Path('build/joint_beliefs/full_experiment')
CELLS=[(s,c) for s in ['4b','12b'] for c in ['linear','monotone_spline']]
METHODS=['behavior','activation','raw_profile','shuffled','activation_only']
ENV=dict(os.environ,PYTHONPATH='.')
RUN='experiments/joint_beliefs/run.py'


def save(value):
    p=ROOT/'watch_status.json'
    tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2))
    tmp.replace(p)


def completed(size,curve):
    out=ROOT/size/curve/'final'
    return [m for m in METHODS if (out/f'{m}_complete.json').exists()]


def execute(args,log):
    with log.open('a') as handle:
        subprocess.run([sys.executable,*args],env=ENV,stdout=handle,stderr=subprocess.STDOUT,check=True)


def main():
    pid_path=ROOT/'watch.pid'
    if pid_path.exists():
        old=int(pid_path.read_text())
        try:
            os.kill(old,0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError(f'Watcher {old} is already running')
    pid_path.write_text(str(os.getpid()))
    active={}
    reported={}
    try:
        while True:
            for key,(process,handle) in list(active.items()):
                code=process.poll()
                if code is not None:
                    handle.close()
                    del active[key]
                    if code:
                        raise RuntimeError(f'Final fit failed for {key}, exit {code}')
            for log in ROOT.glob('search_*.log'):
                if 'Traceback (most recent call last)' in log.read_text():
                    raise RuntimeError(f'Search error in {log}')
            for size,curve in CELLS:
                key=f'{size}/{curve}'
                cell=ROOT/size/curve
                ready=completed(size,curve)
                if len(ready)<5 and key not in active and (cell/'selection.json').exists() and len(active)<2:
                    execute([RUN,'lock','--size',size,'--curve',curve],ROOT/'watch.log')
                    handle=(ROOT/f'final_{size}_{curve}.log').open('a')
                    process=subprocess.Popen([sys.executable,RUN,'final','--size',size,'--curve',curve],
                                             env=ENV,stdout=handle,stderr=subprocess.STDOUT)
                    active[key]=(process,handle)
                    print(f'Started final {key}, pid={process.pid}',flush=True)
                if {'behavior','activation'}.issubset(ready) and reported.get(key)!=len(ready):
                    execute(['experiments/joint_beliefs/partial_report.py','--size',size,'--curve',curve],
                            cell/'final'/'partial_report.log')
                    reported[key]=len(ready)
                    print(f'Updated report {key}: {len(ready)} methods',flush=True)
            state=dict(state='running',pid=os.getpid(),
                       active={k:p.pid for k,(p,h) in active.items()},
                       completed={f'{s}/{c}':completed(s,c) for s,c in CELLS},
                       updated_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
            save(state)
            if not active and all(len(completed(s,c))==5 for s,c in CELLS):
                execute([RUN,'lock'],ROOT/'watch.log')
                execute(['experiments/joint_beliefs/report.py'],ROOT/'report.log')
                save(dict(state,state='complete',report=str(ROOT/'report.html')))
                print('Full experiment report complete.',flush=True)
                break
            time.sleep(10)
    except BaseException as exc:
        save(dict(state='error',error=str(exc),traceback=traceback.format_exc(),
                  active={k:p.pid for k,(p,h) in active.items()}))
        raise
    finally:
        pid_path.unlink(missing_ok=True)


if __name__=='__main__':
    main()
