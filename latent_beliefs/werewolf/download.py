"""Download the Kaggle Werewolf replay archive used to build the Werewolf cases.

Source: Kaggle dataset ``kaggle/werewolf-dataset``, version 4 (about 57 GB;
7,448 non-empty ``<replay_id>.json`` replays plus zero-byte placeholders).
The case sampler shuffles the full list of populated replays, so the complete
archive is required to reproduce the sample.

The archive location is ``config.WEREWOLF_REPLAY_DIR`` (default
``<RAW_DIR>/werewolf/werewolf-dataset-v4``).  Set ``LB_WEREWOLF_REPLAY_DIR``
(or pass ``--replay-dir``) to use an existing copy, for example kagglehub's
cache
``~/.cache/kagglehub/datasets/kaggle/werewolf-dataset/versions/4``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from latent_beliefs.config import WEREWOLF_REPLAY_DIR

KAGGLE_HANDLE = "kaggle/werewolf-dataset/versions/4"
POPULATED_REPLAYS = 7448


def populated_replays(replay_dir: Path) -> list[Path]:
    """Non-empty replay files; the archive also contains empty placeholders."""
    return sorted(
        path for path in replay_dir.glob("*.json") if path.stat().st_size > 0
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay-dir", type=Path, default=WEREWOLF_REPLAY_DIR)
    args = parser.parse_args()
    replay_dir = args.replay_dir.expanduser().resolve()
    if not (replay_dir.exists() and populated_replays(replay_dir)):
        try:
            import kagglehub
        except ImportError as exc:
            raise SystemExit(
                "Downloading requires the kagglehub package (pip install kagglehub) "
                "and Kaggle credentials."
            ) from exc
        replay_dir.mkdir(parents=True, exist_ok=True)
        kagglehub.dataset_download(KAGGLE_HANDLE, output_dir=str(replay_dir))
    count = len(populated_replays(replay_dir))
    if count != POPULATED_REPLAYS:
        raise SystemExit(
            f"{replay_dir} has {count} populated replays; expected {POPULATED_REPLAYS}"
        )
    print(f"{replay_dir}: {count} populated replays")


if __name__ == "__main__":
    main()
