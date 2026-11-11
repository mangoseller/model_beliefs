"""Re-run the CKD first-order evaluation from the cached responses and compare it with a reference.

Runs the unchanged first-order stages (strict parse -> complete-case dataset ->
reciprocal evaluation from the released main fit) into a separate output
directory, so nothing under ``build/`` is overwritten, then compares:

* the rebuilt dataset with ``build/datasets/ckd.csv`` (byte identity);
* the reciprocal model summary with a reference ``model_summary.csv``
  (default ``build/evaluation/reciprocal/model_summary.csv``), to 3 decimals.

Usage::

    python -m second_order.baseline_check --out-dir <dir>

Exit status is non-zero if anything differs.  Writes ``<dir>/baseline_ckd.csv``
(concordance and pooled R^2 per model and direction) and
``<dir>/baseline_comparison.csv``.
"""

from __future__ import annotations

import argparse
import filecmp
import subprocess
import sys
from pathlib import Path

import pandas as pd

from latent_beliefs.config import BUILD_DIR, EVALUATION_DIR

METRICS = (
    "mean_c_index",
    "mean_c_index_ci_lower",
    "mean_c_index_ci_upper",
    "pooled_r2",
    "pooled_r2_ci_lower",
    "pooled_r2_ci_upper",
)
KEYS = ("model_key", "condition")


def run(*module_args: str) -> None:
    command = [sys.executable, "-m", *module_args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--reference", type=Path, default=EVALUATION_DIR / "reciprocal" / "model_summary.csv"
    )
    parser.add_argument("--decimals", type=int, default=3)
    args = parser.parse_args()
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    run("latent_beliefs.ckd.parse", "--out-dir", str(out / "parsed" / "ckd"))
    run("latent_beliefs.datasets", "--domains", "ckd", "--parsed-dir", str(out / "parsed"),
        "--out-dir", str(out / "datasets"))
    dataset_same = filecmp.cmp(out / "datasets" / "ckd.csv", BUILD_DIR / "datasets" / "ckd.csv",
                               shallow=False)
    print(f"dataset identical to build/datasets/ckd.csv: {dataset_same}")
    # The reciprocal stage reads build/datasets/ckd.csv, which was just shown identical.
    run("latent_beliefs.evaluation.reciprocal", "--domains", "ckd",
        "--output-dir", str(out / "reciprocal"))

    new = pd.read_csv(out / "reciprocal" / "model_summary.csv")
    new[["model_key", "model_display", "condition", *METRICS]].to_csv(
        out / "baseline_ckd.csv", index=False)
    ref = pd.read_csv(args.reference)
    ref = ref[ref["domain"] == "ckd"]
    merged = ref[[*KEYS, *METRICS]].merge(new[[*KEYS, *METRICS]], on=list(KEYS),
                                          suffixes=("_reference", "_rerun"), validate="one_to_one")
    worst = 0.0
    for metric in METRICS:
        diff = (merged[f"{metric}_reference"] - merged[f"{metric}_rerun"]).abs()
        merged[f"{metric}_absdiff"] = diff
        worst = max(worst, float(diff.max()))
    merged.to_csv(out / "baseline_comparison.csv", index=False)
    tolerance = 0.5 * 10 ** (-args.decimals)
    matches = len(merged) == len(ref) == len(new) and worst < tolerance
    print(f"rows compared: {len(merged)}; max abs difference: {worst:.3g}; "
          f"match to {args.decimals} decimals: {matches}")
    if not (matches and dataset_same):
        sys.exit(1)


if __name__ == "__main__":
    main()
