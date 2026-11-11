"""Read-only progress summary for the resumable full experiment."""
import json
import time
from pathlib import Path

root = Path('build/joint_beliefs/full_experiment')
if (root / 'protocol.json').exists():
    print(f"Elapsed: {(time.time() - (root / 'protocol.json').stat().st_mtime) / 60:.1f} minutes")
for size in ['4b', '12b']:
    for curve in ['linear', 'monotone_spline']:
        cell = root / size / curve
        candidates = cell / 'candidates'
        act = len(list(candidates.glob('activation_l*/result.json')))
        raw = len(list(candidates.glob('raw_profile_p*/result.json')))
        shuffle = int((candidates / 'shuffled' / 'result.json').exists())
        selected = (cell / 'selection.json').exists()
        locked = (cell / 'selection_lock.json').exists()
        final = len(list((cell / 'final').glob('*_complete.json')))
        print(f'{size} {curve}: activation {act}/32; raw {raw}/4; shuffled {shuffle}/1; selected {selected}; locked {locked}; evaluated {final}/5')
    for stage in ['search', 'final']:
        for log in sorted(root.glob(f'{stage}_{size}*.log')):
            if log.name == 'search_12b.log' and (root / 'parallel_cells.json').exists():
                continue
            lines = log.read_text().splitlines()
            if lines:
                print(f'{log.stem}: {lines[-1]}')
            if any('Traceback' in line for line in lines):
                print('\n'.join(lines[-15:]))
print('Global lock (all four cells):', (root / 'selection_lock.json').exists())
if (root / 'watch_status.json').exists():
    watch = json.loads((root / 'watch_status.json').read_text())
    print('Coordinator:', watch['state'], watch.get('error', ''))
