"""Download the public source data for the scientific-forecasting domain.

Sources (all written under ``RAW_DIR/forecasting``):

* The authors' versioned Code Ocean v1 capsule for Ashokkumar, Hewitt,
  Ghezae, and Willer, "Large language models can predict the results of
  social science experiments" (Nature, 2026; doi:10.1038/s41586-026-10742-x).
  Only the data licence (CC0) and the three data files used here are
  fetched: the primary-archive participant responses, the primary-archive
  LLM prompts (whose condition transcripts become our prompts), and the
  megastudy effect estimates.
* The supplementary materials of Voelkel et al. (2024), the Strengthening
  Democracy Challenge megastudy, which supply the treatment descriptions for
  the megastudy cases.  This PDF has no stable direct-download URL and must be
  obtained manually from https://osf.io/jzbnt/ and saved at the path below.

Existing files are never re-downloaded; every file is checked against its
SHA-256 digest.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from pathlib import Path
from urllib.request import urlopen

from archive.smolin_wilder_replication.config import RAW_DIR

FORECASTING_RAW_DIR = RAW_DIR / "forecasting"
CODEOCEAN_DATA_DIR = FORECASTING_RAW_DIR / "codeocean_v1" / "data"
VOELKEL_2024_SUPPLEMENT = (
    FORECASTING_RAW_DIR / "megastudies_public" / "voelkel_2024" / "supplement.pdf"
)

CODEOCEAN_BASE = (
    "https://files.codeocean.com/files/verified/"
    "4427d699-e1a1-4c93-9cf8-7374e6607e18_v1.0"
)
CODEOCEAN_FILES = {
    "data/LICENSE": "36ffd9dc085d529a7e60e1276d73ae5a030b020313e6c5408593a6ae2af39673",
    "data/llm_responses.RDS": "c46dc49c2d83a5b4761d09d8e964ba0c28cf384c546dffd86942098339d8f07a",
    "data/megastudies.RDS": "db80e9ca7b17339fabf920a5f4c1e03a1677302836de2c3f17e4b4fc88e25978",
    "data/rct_responses.RDS": "4c7e28206585972bc8ee2d6ea838204f0a5679febd0591105a7553375ba1b36b",
}
MANUAL_FILES = {
    VOELKEL_2024_SUPPLEMENT: (
        "https://osf.io/jzbnt/",
        "70f342b1ce5b90ee8148f4528efdcc6533ddd736e8aa5ad3d321808068f5d0ab",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
                expected_size = response.headers.get("Content-Length")
                written = 0
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    written += len(chunk)
            if expected_size is not None and written != int(expected_size):
                raise OSError(
                    f"Incomplete download for {url}: expected {expected_size} bytes, "
                    f"received {written}"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return "downloaded"


def verify(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {path}: expected {expected}, found {actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only verify files that are already present; download nothing.",
    )
    args = parser.parse_args()

    missing: list[str] = []
    for relative_path, digest in CODEOCEAN_FILES.items():
        destination = FORECASTING_RAW_DIR / "codeocean_v1" / relative_path
        if args.check_only and not destination.exists():
            missing.append(f"{destination} (Code Ocean: {relative_path})")
            continue
        status = "present"
        if not args.check_only:
            status = download(f"{CODEOCEAN_BASE}/{relative_path}?download", destination)
        verify(destination, digest)
        print(f"{status:10} ok  {destination}")
    for destination, (source, digest) in MANUAL_FILES.items():
        if not destination.exists():
            missing.append(f"{destination} (download manually from {source}; sha256 {digest})")
            continue
        verify(destination, digest)
        print(f"{'present':10} ok  {destination}")
    if missing:
        raise SystemExit("Missing files:\n  " + "\n  ".join(missing))


if __name__ == "__main__":
    main()
