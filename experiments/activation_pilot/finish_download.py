"""Finish the slow 12B shard with verified parallel HTTP ranges.

Run only after stopping the original downloader. Xet >=1.3 writes sequentially,
so preserve its existing prefix. Verify the complete published SHA-256 before
renaming to the filename that triggers model loading.
"""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import shutil
import time

import requests

ROOT=Path('build/activation_pilot_12b/model')
NAME='model-00001-of-00002.safetensors'
REVISION='86cc6a8dedbc456dd0e4af01a9d09f396f77e558'
SHA='995cbd05b7bfd8f5ab5307b476eb5496b5ec3f5256a9dd26366236ce8816c93f'
SIZE=5367455313
URL=f'https://huggingface.co/mlx-community/gemma-3-12b-it-4bit/resolve/{REVISION}/{NAME}'
prefix=next((ROOT/'.cache/huggingface/download').glob(f'*.{SHA}.incomplete'))
target=ROOT/f'{NAME}.http-partial'
shutil.copyfile(prefix,target)
start=target.stat().st_size
assert 0<start<SIZE
print(f'Reusing {start/1e9:.2f} GB; remaining {(SIZE-start)/1e9:.2f} GB',flush=True)
fd=os.open(target,os.O_RDWR)
block=16*1024*1024
ranges=[(i,min(i+block,SIZE)-1) for i in range(start,SIZE,block)]


def fetch(bounds):
    first,last=bounds
    for attempt in range(4):
        try:
            with requests.get(URL,headers={'Range':f'bytes={first}-{last}'},stream=True,timeout=(30,90)) as r:
                r.raise_for_status()
                assert r.status_code==206 and r.headers['Content-Range']==f'bytes {first}-{last}/{SIZE}'
                offset=first
                for chunk in r.iter_content(256*1024):
                    written=0
                    while written<len(chunk):
                        written+=os.pwrite(fd,chunk[written:],offset+written)
                    offset+=len(chunk)
                assert offset==last+1
            return last-first+1
        except Exception:
            if attempt==3:raise
            time.sleep(1+attempt)


began=time.monotonic()
completed=0
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    futures=[pool.submit(fetch,b) for b in ranges]
    for i,future in enumerate(concurrent.futures.as_completed(futures),1):
        completed+=future.result()
        if i%8==0 or i==len(futures):
            print(f'{i}/{len(futures)} ranges; downloaded {completed/1e9:.2f} GB in {time.monotonic()-began:.0f}s',flush=True)
os.fsync(fd)
os.close(fd)
digest=hashlib.sha256()
with target.open('rb') as f:
    for chunk in iter(lambda:f.read(8*1024*1024),b''):digest.update(chunk)
assert target.stat().st_size==SIZE and digest.hexdigest()==SHA,'Integrity verification failed; refusing to load'
target.rename(ROOT/NAME)
(ROOT/'.cache/huggingface/download'/f'{NAME}.metadata').write_text(f'{REVISION}\n{SHA}\n{time.time()}\n')
print('Published SHA-256 verified; 12B checkpoint complete.',flush=True)
