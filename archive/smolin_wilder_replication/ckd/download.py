"""Download the NHANES 2011-2018 files used to build the CKD cases.

Sixteen components for each of the four continuous-NHANES cycles G, H, I, and
J are fetched from the CDC into ``RAW_DIR/ckd/nhanes/<period>/<COMPONENT>_<cycle>.XPT``.
Existing files are never re-downloaded; every file is checked against the
SHA-256 digests in ``assets/ckd/nhanes_xpt_sha256.txt`` (the files used for
the paper).  The CDC occasionally revises public files; a digest mismatch
means the downloaded file differs from the one used for the paper.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from archive.smolin_wilder_replication.config import ASSETS_DIR, RAW_DIR

NHANES_DIR = RAW_DIR / "ckd" / "nhanes"
SHA256_MANIFEST = ASSETS_DIR / "ckd" / "nhanes_xpt_sha256.txt"


@dataclass(frozen=True)
class Cycle:
    code: str
    period: str
    public_year: str


CYCLES = (
    Cycle(code="G", period="2011-2012", public_year="2011"),
    Cycle(code="H", period="2013-2014", public_year="2013"),
    Cycle(code="I", period="2015-2016", public_year="2015"),
    Cycle(code="J", period="2017-2018", public_year="2017"),
)

COMPONENTS = (
    "DEMO",
    "ALB_CR",
    "BIOPRO",
    "BMX",
    "BPX",
    "BPQ",
    "DIQ",
    "SMQ",
    "ALQ",
    "MCQ",
    "HIQ",
    "HUQ",
    "FSQ",
    "PAQ",
    "DR1TOT",
    "DR2TOT",
)


def xpt_url(cycle: Cycle, component: str) -> str:
    return (
        f"https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/{cycle.public_year}"
        f"/DataFiles/{component}_{cycle.code}.XPT"
    )


def xpt_path(cycle: Cycle, component: str, nhanes_dir: Path = NHANES_DIR) -> Path:
    return nhanes_dir / cycle.period / f"{component}_{cycle.code}.XPT"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def expected_digests() -> dict[str, str]:
    """Map ``<period>/<file>`` to its SHA-256 digest."""
    digests: dict[str, str] = {}
    for line in SHA256_MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, relative_path = line.split(maxsplit=1)
            digests[relative_path.strip()] = digest
    return digests


def download(url: str, destination: Path) -> str:
    if destination.exists():
        return "present"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            with urlopen(url, timeout=120) as response:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return "downloaded"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only verify files that are already present; download nothing.",
    )
    args = parser.parse_args()

    digests = expected_digests()
    missing: list[Path] = []
    mismatched: list[str] = []
    for cycle in CYCLES:
        for component in COMPONENTS:
            path = xpt_path(cycle, component)
            if args.check_only and not path.exists():
                missing.append(path)
                continue
            status = "present" if args.check_only else download(xpt_url(cycle, component), path)
            relative_path = f"{cycle.period}/{path.name}"
            actual = sha256(path)
            if actual != digests[relative_path]:
                mismatched.append(
                    f"{path}: expected {digests[relative_path]}, found {actual}"
                )
                status = "SHA-256 MISMATCH"
            print(f"{status:>16}  {path}")
    if missing:
        raise SystemExit("Missing files:\n" + "\n".join(str(path) for path in missing))
    if mismatched:
        raise SystemExit("Files differ from those used for the paper:\n" + "\n".join(mismatched))


if __name__ == "__main__":
    main()
