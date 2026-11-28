"""Appendix table: complete-case retention under strict parsing.

For each model and domain, the fraction of the 500 collected cases for which
all 16 outputs parsed, i.e. the number of rows of that model in the analysis
matrix ``<datasets>/<domain>.csv`` divided by 500.  Writes
``<tables>/complete_case_retention.tex``.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from archive.smolin_wilder_replication.config import DATASETS_DIR, DOMAINS, TABLES_DIR, ensure
from archive.smolin_wilder_replication.models import MODEL_DISPLAY, MODELS

COLLECTED_CASES = 500
OUTPUTS_PER_CASE = 16


def retention_rates(datasets_dir: Path) -> dict[tuple[str, str], float]:
    rates: dict[tuple[str, str], float] = {}
    for domain in DOMAINS:
        with (datasets_dir / f"{domain}.csv").open() as handle:
            reader = csv.DictReader(handle)
            outputs = [name for name in reader.fieldnames if name.endswith("__input")]
            rows = list(reader)
        assert len(outputs) == OUTPUTS_PER_CASE, f"Expected sixteen outputs for {domain}"
        assert {r["model_key"] for r in rows} == set(MODELS)
        for model in MODELS:
            selected = [r for r in rows if r["model_key"] == model]
            assert len({r["source_prompt_id"] for r in selected}) == len(selected)
            assert all(r[name] not in ("", "nan", "NaN") for r in selected for name in outputs)
            assert 0 < len(selected) <= COLLECTED_CASES
            rates[model, domain] = len(selected) / COLLECTED_CASES
    return rates


def render(rates: dict[tuple[str, str], float]) -> str:
    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\small",
        r"\begin{tabular}{lrrr}",
        r"\toprule",
        r"Model & CKD & Werewolf & Scientific forecasting \\",
        r"\midrule",
    ]
    for model in MODELS:
        lines.append(
            MODEL_DISPLAY[model]
            + " & "
            + " & ".join(f"{100 * rates[model, domain]:.1f}\\%" for domain in DOMAINS)
            + r" \\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\caption{Complete-case retention under strict parsing. Each percentage is "
            r"the fraction of 500 collected cases for which all 16 outputs parse, "
            r"calculated separately for each model and domain.}",
            r"\label{tab:complete-case-retention}",
            r"\end{table}",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--datasets-dir", type=Path, default=DATASETS_DIR)
    parser.add_argument("--out-dir", type=Path, default=TABLES_DIR)
    args = parser.parse_args()
    path = ensure(args.out_dir) / "complete_case_retention.tex"
    path.write_text(render(retention_rates(args.datasets_dir)))
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
