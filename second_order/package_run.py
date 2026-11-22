"""Package a finished run directory as independent result archives.

    python -m second_order.package_run --run runs/binding-pilot [--out ..] [--name binding-pilot] [--what main]

Writes ``<name>.tar.gz`` (everything but activations: plan, config, caches, exports,
analysis) and, when the run has activations, ``<name>_activations.tar`` (uncompressed:
the arrays do not compress), each with a ``.sha256`` file in ``sha256sum`` format, so
each part of the experiment can be downloaded and analysed while later parts run.
"""
from __future__ import annotations

import argparse
import hashlib
import tarfile
from pathlib import Path

SKIP = {".followup.lock", ".binding.lock", ".causal.lock", ".freeze.lock"}


def _sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write(target: Path, root: Path, members, mode: str) -> Path:
    tmp = target.with_name(target.name + ".tmp")
    with tarfile.open(tmp, mode) as tar:
        for path in members:
            tar.add(path, arcname=str(Path(root.name) / path.relative_to(root)), recursive=False)
    tmp.replace(target)
    target.with_name(target.name + ".sha256").write_text(f"{_sha(target)}  {target.name}\n")
    return target


def package(run: Path, out: Path | None = None, name: str | None = None, what: str = "all") -> list[Path]:
    """``what``: "all", "main" (everything but activations) or "activations"."""
    run = Path(run).resolve()
    out = Path(out or run.parent).resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = name or run.name
    everything = sorted(p for p in run.rglob("*") if p.name not in SKIP and not p.name.endswith(".tmp"))
    acts = [p for p in everything if "activations" in p.relative_to(run).parts[:2]]
    main = sorted(set(everything) - set(acts))
    written = [_write(out / f"{name}.tar.gz", run, main, "w:gz")] if what in ("all", "main") else []
    if what in ("all", "activations") and any(p.is_file() for p in acts):
        written.append(_write(out / f"{name}_activations.tar", run, acts, "w"))
    for path in written:
        print(f"{path} ({path.stat().st_size / 1e6:.1f} MB)")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--what", choices=("all", "main", "activations"), default="all")
    args = parser.parse_args()
    package(args.run, args.out, args.name, args.what)


if __name__ == "__main__":
    main()
