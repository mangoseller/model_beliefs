"""Assemble, verify, and unpack the redistributable results bundle.

The bundle (``RELEASE_DIR``) holds everything needed to rebuild every figure and
table without re-collecting LLM responses, re-downloading source datasets, or
refitting models on a GPU::

    prompts/<domain>/            exact prompts sent to the models (+ tasks.json)
    prompts/branching/cases.jsonl
    sources/ckd/cases.csv        CKD case table with ground-truth labels
    sources/forecasting/scenario_gold.csv
    responses/<domain>/<task_id>/<model>.jsonl
    responses/branching/<experiment>/<stage>/<model>.jsonl
    fits/<fit>/...               fitted models (see FIT_FILES)
    MANIFEST.sha256

Subcommands:

``pack``    copy the bundle files from a build tree (prompts and sources from
            BUILD_DIR, responses from MATERIALIZED_DIR, fits from REFIT_DIR)
            into RELEASE_DIR and write the manifest.
``manifest`` (re)write MANIFEST.sha256 for the files in RELEASE_DIR.
``verify``  check every file in RELEASE_DIR against MANIFEST.sha256.
``unpack``  copy the bundled prompts and source tables into BUILD_DIR, so the
            analysis can start from the released responses.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

from archive.smolin_wilder_replication.config import (
    BUILD_DIR,
    DOMAINS,
    MATERIALIZED_DIR,
    PROMPTS_DIR,
    REFIT_DIR,
    RELEASE_DIR,
    SOURCES_DIR,
)

MANIFEST = "MANIFEST.sha256"

SOURCE_FILES = ("ckd/cases.csv", "forecasting/scenario_gold.csv")

# Files of each fit that downstream evaluations and figures read.
FIT_FILES = {
    "main": ("parameters.csv", "latent_posteriors.csv", "run_metadata.json"),
    "spline": ("parameters.csv", "run_metadata.json"),
    "two_d": ("leave_one_output_out_predictions.csv", "leave_one_output_out_metrics.csv"),
    "holdout_domain": ("shared_parameters.csv", "run_metadata.json"),
    "holdout_joint": ("combined_prompt_metrics.csv", "run_metadata.json"),
    "holdout_framing": (
        "full_family_prompt_metrics_eight_decision.csv",
        "full_family_prompt_metrics_report.csv",
        "run_metadata.json",
    ),
    "holdout_report_domain": ("report_prompt_metrics.csv", "run_metadata.json"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def bundle_files(release_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in release_dir.rglob("*")
        if path.is_file() and path.name != MANIFEST and not path.name.startswith(".")
    )


def pack(release_dir: Path) -> None:
    for domain in DOMAINS:
        for source in sorted((PROMPTS_DIR / domain).glob("*")):
            copy(source, release_dir / "prompts" / domain / source.name)
    copy(PROMPTS_DIR / "branching" / "cases.jsonl", release_dir / "prompts/branching/cases.jsonl")
    for relative in SOURCE_FILES:
        copy(SOURCES_DIR / relative, release_dir / "sources" / relative)
    for source in sorted(MATERIALIZED_DIR.rglob("*.jsonl")):
        copy(source, release_dir / "responses" / source.relative_to(MATERIALIZED_DIR))
    for fit, names in FIT_FILES.items():
        for name in names:
            copy(REFIT_DIR / fit / name, release_dir / "fits" / fit / name)
    manifest(release_dir)


def manifest(release_dir: Path) -> None:
    lines = [f"{sha256(path)}  {path.relative_to(release_dir)}" for path in bundle_files(release_dir)]
    (release_dir / MANIFEST).write_text("\n".join(lines) + "\n")
    print(f"Wrote {MANIFEST} for {len(lines)} files in {release_dir}")


def verify(release_dir: Path) -> None:
    expected = {}
    for line in (release_dir / MANIFEST).read_text().splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    present = {str(path.relative_to(release_dir)) for path in bundle_files(release_dir)}
    missing = sorted(set(expected) - present)
    extra = sorted(present - set(expected))
    changed = sorted(
        relative
        for relative in set(expected) & present
        if sha256(release_dir / relative) != expected[relative]
    )
    for label, items in (("missing", missing), ("unexpected", extra), ("changed", changed)):
        for item in items:
            print(f"{label}: {item}")
    if missing or extra or changed:
        raise SystemExit(1)
    print(f"All {len(expected)} files in {release_dir} match {MANIFEST}")


def unpack(release_dir: Path) -> None:
    for source in sorted((release_dir / "prompts").rglob("*")):
        if source.is_file():
            copy(source, PROMPTS_DIR / source.relative_to(release_dir / "prompts"))
    for relative in SOURCE_FILES:
        copy(release_dir / "sources" / relative, SOURCES_DIR / relative)
    print(f"Unpacked prompts and source tables from {release_dir} into {BUILD_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("pack", "manifest", "verify", "unpack"))
    parser.add_argument("--release-dir", type=Path, default=RELEASE_DIR)
    args = parser.parse_args()
    commands = {"pack": pack, "manifest": manifest, "verify": verify, "unpack": unpack}
    commands[args.command](args.release_dir)


if __name__ == "__main__":
    main()
