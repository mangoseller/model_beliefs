"""Build the 500 scientific-forecasting cases and their ground truth.

A case is a pair of randomized conditions (A, B) and one outcome; the
forecast target is whether mean(outcome | A) > mean(outcome | B).  Cases come
from Ashokkumar, Hewitt et al. (2026):

* 469 primary-archive scenarios (TESS-style survey experiments), built in R
  from the authors' Code Ocean data (``R/primary_scenarios.R``);
* the first 31 megastudy contrasts, all from the Voelkel et al. (2024)
  Strengthening Democracy Challenge: each treatment is compared with the
  first-listed treatment of its outcome (24 partisan-animosity contrasts and
  the first 7 anti-democratic-attitudes contrasts).

Ground truth (``R/gold.R``) uses the released participant responses (primary)
and condition-level effect estimates (megastudy).  Each primary difference is
oriented to the response scale shown in the prompt: the source study reversed
some outcome scales when prompting its LLM (``scale_flip``), and our prompts
reuse those transcripts.  Supports the
scientific-forecasting domain throughout the paper (Methods; Appendix).

Outputs in ``SOURCES_DIR/forecasting``: ``primary_scenarios.jsonl``,
``megastudy_effects.csv``, ``voelkel_2024_supplement.txt`` (pdftotext
rendering), ``megastudy_condition_materials.json``, ``scenarios.jsonl``
(the 500 cases with all prompt materials), and ``scenario_gold.csv``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from archive.smolin_wilder_replication.config import ASSETS_DIR, SOURCES_DIR, ensure
from archive.smolin_wilder_replication.forecasting.download import CODEOCEAN_DATA_DIR, VOELKEL_2024_SUPPLEMENT

R_DIR = Path(__file__).resolve().parent / "R"
ENRICHMENT = ASSETS_DIR / "forecasting" / "megastudy_prompt_enrichment.json"

N_CASES = 500
N_PRIMARY = 469

VOELKEL_2024_SETTING = (
    "US online participants in the Strengthening Democracy Challenge were randomized to scalable "
    "interventions intended to reduce anti-democratic and hostile partisan attitudes."
)
VOELKEL_2024_OUTCOMES = {
    "ADA": "Anti-democratic-attitudes index, coded so that higher values indicate fewer anti-democratic attitudes.",
    "PA": "Partisan-animosity index, coded so that higher values indicate less partisan animosity.",
}
VOELKEL_2024_SOURCE_TYPE = "author-submitted protocol description from supplement"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def run_r(script: str, *args: Path) -> None:
    subprocess.run(["Rscript", str(R_DIR / script), *map(str, args)], check=True)


# Voelkel (2024) materials
def compact(value: str) -> str:
    value = value.replace("­", "").replace("", "-")
    value = re.sub(r"\n\s*\d+\s*\n", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def paragraphize(value: str) -> str:
    value = compact(value)
    value = re.sub(r"(?<!\n)\n(?!\n)", " ", value)
    return re.sub(r" +", " ", value).strip()


def norm(value: str) -> str:
    value = value.lower().replace("<u+2013>", " ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def voelkel_2024_materials(text: str, effects: list[dict[str, str]]) -> dict[str, dict]:
    """Treatment descriptions from section S3.2 of the Voelkel et al. supplement."""
    start = text.rindex("S3.2 Treatments")
    region = text[start : text.index("S4 Supplementary Text", start)]
    titles = sorted({row["treatment_title"] for row in effects})
    aliases = {
        "Outpartisans Experiences of Harm": "Outpartisans' Experiences of Harm",
        "Outpartisans Willingness to Learn": "Outpartisans' Willingness to Learn",
        "Pro Democracy Bipartisan Elite Cues": "Pro-Democracy Bipartisan Elite Cues",
        "Pro Democracy Inparty Elite Cues": "Pro-Democracy Inparty Elite Cues",
    }
    starts = []
    for title in titles:
        heading = aliases.get(title, title)
        match = re.search(rf"(?m)^{re.escape(heading)}\s*$", region)
        if not match:
            raise KeyError(f"Voelkel 2024 heading not found: {title}")
        starts.append((match.start(), title, match.end()))
    starts.sort()
    materials = {}
    for index, (_, title, content_start) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(region)
        block = region[content_start:end]
        desc = re.search(
            r"(?:Description:\s*)?(Participants .*?)(?:Available for review|$)", block, re.S
        )
        if not desc:
            raise ValueError(f"Voelkel 2024 description not parsed: {title}")
        materials[title] = paragraphize(desc.group(1))

    normalized = {norm(key): value for key, value in materials.items()}
    out = {}
    for row in effects:
        key = norm(row["treatment_title"])
        if key not in normalized:
            raise KeyError(f"No voelkel_2024 material for {row['treatment_title']!r}")
        out[row["effect_id"]] = {
            "material": compact(normalized[key]),
            "source_type": VOELKEL_2024_SOURCE_TYPE,
            "local_material": row["local_material"],
        }
    return out


# Megastudy scenarios
def megastudy_scenarios(
    effects: list[dict[str, str]], extracted: dict[str, dict], enrichments: dict[str, dict]
) -> list[dict]:
    """Alternative-versus-reference contrasts within each study outcome."""
    per_effect = {effect_id: details["material"] for effect_id, details in extracted.items()}
    per_effect_source = {
        effect_id: details["source_type"] for effect_id, details in extracted.items()
    }
    # Hand-written protocol summaries replace the extracted text for these effects.
    for pair_key, details in enrichments.items():
        effect_a, effect_b = pair_key.split("__")
        per_effect[effect_a] = details["condition_a_material"]
        per_effect[effect_b] = details["condition_b_material"]
        per_effect_source[effect_a] = details["material_status"]
        per_effect_source[effect_b] = details["material_status"]

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in effects:
        groups[(row["dataset"], row["outcome_key"])].append(row)

    scenarios = []
    for rows in groups.values():
        reference = rows[0]
        for alternative in rows[1:]:
            alternative_embedded = (
                alternative["effect_id"] in per_effect
                or alternative["treatment_description"].strip()
                != alternative["treatment_title"].strip()
            )
            reference_embedded = (
                reference["effect_id"] in per_effect
                or reference["treatment_description"].strip()
                != reference["treatment_title"].strip()
            )
            scenarios.append(
                {
                    "archive": "megastudy",
                    "study": alternative["study_key"],
                    "study_outcome_group": alternative["dataset"],
                    "outcome": alternative["outcome_key"],
                    "condition_a": alternative["treatment_title"],
                    "condition_b": reference["treatment_title"],
                    "condition_a_material": per_effect.get(
                        alternative["effect_id"],
                        f"Archived condition description: {alternative['treatment_description']}",
                    ),
                    "condition_b_material": per_effect.get(
                        reference["effect_id"],
                        f"Archived condition description: {reference['treatment_description']}",
                    ),
                    "study_setting": VOELKEL_2024_SETTING,
                    "outcome_measure": VOELKEL_2024_OUTCOMES[alternative["outcome_key"]],
                    "material_status": "Condition A: "
                    + per_effect_source.get(
                        alternative["effect_id"], alternative["treatment_description_level"]
                    )
                    + "; Condition B: "
                    + per_effect_source.get(
                        reference["effect_id"], reference["treatment_description_level"]
                    ),
                    "materials_ready": alternative_embedded and reference_embedded,
                    "source_effect_id_a": alternative["effect_id"],
                    "source_effect_id_b": reference["effect_id"],
                    "source_url": alternative["source_url"],
                    "local_material": alternative["local_material"],
                }
            )
    for index, row in enumerate(scenarios, 1):
        row["scenario_id"] = f"megastudy_{index:03d}"
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codeocean-data-dir", type=Path, default=CODEOCEAN_DATA_DIR)
    parser.add_argument("--voelkel-supplement", type=Path, default=VOELKEL_2024_SUPPLEMENT)
    parser.add_argument("--out-dir", type=Path, default=SOURCES_DIR / "forecasting")
    args = parser.parse_args()
    out_dir = ensure(args.out_dir)

    primary_path = out_dir / "primary_scenarios.jsonl"
    run_r("primary_scenarios.R", args.codeocean_data_dir, primary_path)
    primary = read_jsonl(primary_path)
    if len(primary) != N_PRIMARY:
        raise RuntimeError(f"Expected {N_PRIMARY} primary scenarios; found {len(primary)}")

    effects_path = out_dir / "megastudy_effects.csv"
    run_r("megastudy_effects.R", args.codeocean_data_dir / "megastudies.RDS", effects_path)
    with effects_path.open(newline="", encoding="utf-8") as handle:
        effects = list(csv.DictReader(handle))

    text_path = out_dir / "voelkel_2024_supplement.txt"
    subprocess.run(
        ["pdftotext", "-layout", str(args.voelkel_supplement), str(text_path)], check=True
    )
    extracted = voelkel_2024_materials(
        text_path.read_text(errors="replace"), effects
    )
    (out_dir / "megastudy_condition_materials.json").write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    enrichments = json.loads(ENRICHMENT.read_text(encoding="utf-8"))
    mega = megastudy_scenarios(effects, extracted, enrichments)
    scenarios = (primary + mega)[:N_CASES]
    write_jsonl(out_dir / "scenarios.jsonl", scenarios)

    run_r(
        "gold.R",
        out_dir / "scenarios.jsonl",
        args.codeocean_data_dir / "rct_responses.RDS",
        effects_path,
        args.codeocean_data_dir / "llm_responses.RDS",
        out_dir / "scenario_gold.csv",
    )
    print(f"Wrote {len(scenarios)} scenarios to {out_dir}")


if __name__ == "__main__":
    main()
