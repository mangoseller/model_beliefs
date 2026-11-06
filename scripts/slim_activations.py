"""Keep a subset of layers from saved activations so a run is small enough to copy off the GPU box.

Writes <run>/activations_slim/<case>.npz with the same three positions and all rows, but only the
chosen layers (default: every 4th layer plus the last). The layer indices kept are stored in each
file under "layers".

    python scripts/slim_activations.py data/insecure_code_em__Qwen2.5-1.5B-Instruct
"""
import argparse
import time
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--every", type=int, default=4, help="keep every k-th layer (plus the last)")
    ap.add_argument("--skip-recent", action="store_true",
                    help="skip files changed in the last 2 minutes (for use while a run is still going)")
    args = ap.parse_args()

    for run in args.runs:
        src, dst = run / "activations", run / "activations_slim"
        dst.mkdir(exist_ok=True)
        files = sorted(src.glob("*.npz"))
        done, keep = 0, []
        for f in files:
            out = dst / f.name
            if out.exists() or (args.skip_recent and time.time() - f.stat().st_mtime < 120):  # may still be writing
                continue
            z = np.load(f)
            n_layers = z[z.files[0]].shape[1]
            keep = sorted(set(range(0, n_layers, args.every)) | {n_layers - 1})
            tmp = out.with_name(out.stem + ".tmp.npz")  # write then rename, so a crash leaves no half file
            np.savez(tmp, layers=np.array(keep), **{k: z[k][:, keep] for k in z.files})
            tmp.rename(out)
            done += 1
        print(f"{run}: {len(files)} cases, {done} newly slimmed, layers kept {keep}")


if __name__ == "__main__":
    main()
