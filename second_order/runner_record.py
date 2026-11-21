"""Record a run's constants and write its RUN_REPORT.md (the runner's ``report`` stage).

    python -m second_order.runner_record runs/<run_id>

Runs inside the run's environment (``SO_CONFIG`` etc. point at the run), so every
value below is the one the run used.  Writes into ``runs/<run_id>/``:

* ``record/constants.json``: every setting and constant behind the numbers (design,
  prompts, generation per model, latent fit, estimators, bootstrap, seeds);
* ``record/estimator.md``: the estimators, as implemented (``leakage.py``);
* ``record/usage.json``: calls, errors, tokens and wall-clock per model and dataset;
* ``record/example_prompts_ckd.md``: one CKD case in every condition, and its follow-ups;
* ``RUN_REPORT.md``: what ran, on what, with which settings, the results and the figures.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import asdict, fields
from pathlib import Path

import pandas as pd

from latent_beliefs.latent.model import FitConfig
from second_order import config as C
from second_order import leakage
from second_order.bespoke import FORMAT, table as bespoke_table
from second_order.comprehension import is_followup
from second_order.conditions import CONDITIONS, SETTINGS, field_label, pair_spec
from second_order.fit_pooled import PROBABILITY_EPSILON, QUADRATURE_POINTS, SUBSETS
from second_order.prompts_second_order import (OUTPUTS, PERSON, bin_boundaries, comprehension, output_kind,
                                               prompt_for, specs)
from second_order.report import display, md_table
from second_order.run_generation import sampling, settings_signature, template_kwargs

ESTIMATOR = """# Estimators, as implemented

All beliefs are on the log-odds scale. Stated probabilities are clipped to [0.01, 0.99]
before the logit. Latent beliefs are posterior means of logit z from the pooled
Smolin & Wilder fit of the believer's family (own belief: A and every B; PERSON: every C,
M and D). Per case and setting (partial = pair S4, false belief = pair F):

    x   = l_A  - l_B                    the model's own gap (main run, replicate 0)
    x'  = l_A' - l_B'                   the same gap from an independent second run (replicate 1)
    beta_D = slope of (l_D - l_M) on x  (OLS with intercept)
    beta_C = slope of (l_C - l_M) on x

    lambda = beta_C / beta_D            (identical to the matched-control 1 - T_C / T_M,
                                         T_C = slope of (l_D - l_C) on x, T_M = beta_D)
    rho    = Cov(x, x') / Var(x)        reliability of the gap
    phi    = beta_D / rho               responsiveness of the attributed belief to evidence
    tau    = (beta_D - beta_C) / rho    = phi (1 - lambda), the theory-of-mind score

rho, phi and tau use calibrated values (latent posterior means de-shrunk with each fit's
own calibration line; equal to the raw values for stated probabilities). lambda uses raw
values; it is a ratio of slopes from one fit, so shrinkage cancels.

Uncertainty: paired bootstrap over cases (bespoke: over items), 95% percentile
intervals, item parameters held fixed. Ratio rule: a ratio is blanked when its
denominator's interval includes 0 (lambda: beta_D; phi and tau: rho).

Checks of the modelling assumptions:
- affine form: phi_D_on_A = Cov(l_D, l_A') / Cov(l_A, l_A') and
  phi_M_on_B = Cov(l_M, l_B') / Cov(l_B, l_B'), which should match phi;
  quadratic terms of both gap regressions (curvature_*), which should be 0;
- partial linearity: lambda for x > 0 and x < 0 separately, and by thirds of |x|;
- noise independence: placebo items (bespoke), whose three gaps should be 0;
- phi shared across settings: phi_pooled (within-setting slopes) and diff_phi.

The implementation's own description (leakage.py):

"""


def available(compute):
    """``compute()``, or a note of why it is unavailable (e.g. the data stage failed)."""
    try:
        return compute()
    except Exception as exc:
        return {"unavailable": repr(exc)}


def constants() -> dict:
    cfg = C.load()
    models = list(cfg["experiment"]["models"])
    fields_s4, _ = pair_spec("S4")
    _, flip = pair_spec("F")
    fit = FitConfig(progress_every=0, **(cfg.get("latent_fit") or {}))
    return {
        "design": {
            "settings": SETTINGS, "conditions": {c: asdict(CONDITIONS[c]) for c in cfg["experiment"]["conditions"]},
            "replicates": cfg["experiment"]["replicates"], "comprehension": cfg["experiment"]["comprehension"],
            "camerer_w": cfg["experiment"]["camerer_w"], "person_name_in_prompts": PERSON["john"]["A"],
        },
        "ckd": available(lambda: {
            "cases": cfg["main_cases"], "withheld_fields_partial": {f: field_label(f) for f in fields_s4},
            "flipped_field_false_belief": {flip: field_label(flip)}, "missing_fields": cfg["missing_fields"],
            "layout": cfg["layout"], "self_layout": cfg["self_layout"],
            "false_belief_framing": cfg["false_belief_framing"],
            "outputs": {o: {"kind": output_kind(o), **{k: v for k, v in specs()[o].items() if k != "builder"}}
                        for o in OUTPUTS},
            "probability_bin_boundaries": bin_boundaries(),
        }),
        "bespoke": {"items_file": str(C.bespoke_items_path()), "samples": cfg["bespoke"]["samples"],
                    "answer_format": FORMAT, "items": available(lambda: bespoke_table().to_dict(orient="records"))},
        "generation": {m: {"backend": C.model_config(m)["backend"], "weights": C.model_config(m).get("path"),
                           "revision": C.model_config(m).get("revision"), "sampling": sampling(m),
                           "chat_template": template_kwargs(m), "settings_signature": settings_signature(m),
                           "max_model_len": C.model_config(m).get("max_model_len"),
                           "batch_size": C.model_config(m).get("batch_size"),
                           "system_prompt": None, "split": C.model_config(m).get("split")} for m in models},
        "latent_fit": {"fit_config": {f.name: getattr(fit, f.name) for f in fields(fit)},
                       "quadrature_points": QUADRATURE_POINTS, "probability_epsilon": PROBABILITY_EPSILON,
                       "output_subsets": SUBSETS, "calibration_simulated_cases": 20000,
                       "split": "seeded per model: 20% test, then 20% of the rest validation"},
        "estimators": {"ratio_rule_denominators": leakage.DENOMINATOR, "min_cases_per_sign": leakage.MIN_SIGN_CASES,
                       "min_cases_per_third": 50, "logit_clip": [0.01, 0.99],
                       "bootstrap_resamples": cfg["analysis"]["bootstrap_resamples"], "interval": "95% percentile",
                       "seed": cfg["seed"]},
        "seed": cfg["seed"],
    }


def usage(run_dir: Path) -> dict:
    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for path in sorted((run_dir / "data" / "generation_cache").glob("*.jsonl")):
        times = []
        with path.open() as handle:
            for line in handle:
                r = json.loads(line)
                stage = "comprehension" if is_followup(r.get("output", "")) else "answers"
                u = out[path.stem][f"{r.get('dataset', 'ckd')}/{stage}"]
                u["calls"] += 1
                u["errors"] += not r.get("ok", False)
                u["truncated"] += r.get("stop_reason") == "length"
                for key in ("input_tokens", "output_tokens"):
                    u[key] += (r.get("usage") or {}).get(key) or 0
                times.append(r.get("time", 0))
        if times:
            out[path.stem]["all"]["first_to_last_response_hours"] = round((max(times) - min(times)) / 3600, 2)
    return json.loads(json.dumps(out))


def example_prompts(model: str) -> str:
    case_id = sorted(__import__("second_order.prompts_second_order", fromlist=["cases"]).cases().index)[0]
    lines = [f"# One CKD case ({case_id}) in every condition, as sent to {model}\n\n"
             "Numeric probability output; the other 15 outputs share each prompt's profile and framing.\n"]
    for cond in C.load()["experiment"]["conditions"]:
        lines.append(f"\n## {cond}\n\n```\n{prompt_for(model, case_id, cond, 'probability')}```\n")
        if CONDITIONS[cond].role == "C":
            q = comprehension(case_id, cond)
            lines.append(f"\nComprehension follow-up (correct: {q.answer_letter}):\n\n```\n{q.question}```\n")
    return "".join(lines)


def section(text: str, depth: int = 1) -> str:
    """Demote markdown headings by ``depth`` levels."""
    return "\n".join("#" * depth + line if line.startswith("#") else line for line in text.splitlines())


def write_report(run_dir: Path) -> None:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    consts = json.loads((run_dir / "record" / "constants.json").read_text())
    use = json.loads((run_dir / "record" / "usage.json").read_text())
    items = manifest.get("bespoke_items", {})
    parts = [f"# Curse-of-knowledge run {manifest['run_id']}\n\n",
             f"Created {manifest['created']}; report written {manifest.get('updated')}. "
             f"Models: {', '.join(display(m) for m in manifest['models'])}."
             f"{' **Smoke run** (reduced cases, samples and fits).' if manifest.get('smoke') else ''}\n\n"]
    warnings = []
    if items.get("placeholder"):
        warnings.append("The bespoke dataset is a **placeholder** (the test fixture, "
                        "`tests/fixtures/bespoke_items.yaml`). Bespoke numbers are not results.")
    warnings += [f"Stage `{s}` {r['status']}: {r.get('error', '')}" for s, r in manifest["stages"].items()
                 if r.get("status") not in ("ok", "running")]
    if manifest.get("git", {}).get("dirty"):
        warnings.append("The repository had uncommitted changes (`record/` holds the exact settings used).")
    if warnings:
        parts.append("## Warnings\n\n" + "".join(f"- {w}\n" for w in warnings) + "\n")

    stages = pd.DataFrame([{"stage": s, "status": r.get("status"), "minutes": r.get("minutes")}
                           for s, r in manifest["stages"].items()])
    parts.append("## Stages\n\n" + md_table(stages, list(stages.columns), 1) + "\n")

    setup = ["## Setup\n"]
    for m, pin in manifest.get("model_snapshots", {}).items():
        setup.append(f"- **{display(m)}**: `{pin['repo']}` at revision `{pin['revision']}` "
                     f"({pin['size_gb']} GB; architecture `{pin['config'].get('architectures')}`, "
                     f"{pin['config'].get('num_hidden_layers')} layers, hidden size {pin['config'].get('hidden_size')}).")
    hw = manifest.get("hardware", {})
    if hw:
        setup.append(f"- **Hardware**: {'; '.join(hw.get('gpus', [])) or 'no GPU'}; {hw.get('cpus')} CPUs; "
                     f"Python {hw.get('python')}; {hw.get('platform')}.")
    git = manifest.get("git", {})
    setup.append(f"- **Code**: commit `{git.get('commit', '?')}` on `{git.get('branch', '?')}`"
                 f"{' (with uncommitted changes)' if git.get('dirty') else ''}; packages in `record/pip_freeze.txt`.")
    z = manifest.get("zenodo")
    setup.append(f"- **CKD data**: Smolin & Wilder results bundle, Zenodo record {z['record'] if z else '23350428'}"
                 f"{', MD5 ' + z['md5'] if z else ''}; bundle manifest SHA-256 "
                 f"`{manifest.get('release_manifest_sha256', '?')[:16]}…`.")
    setup.append(f"- **Bespoke data**: `{items.get('path', '?')}` (SHA-256 `{items.get('sha256', '?')[:16]}…`)"
                 f"{', PLACEHOLDER' if items.get('placeholder') else ''}; copy in `record/bespoke_items.yaml`.")
    parts.append("\n".join(setup) + "\n\n")

    try:
        parts.append(settings_table(consts))
    except (KeyError, TypeError) as exc:
        parts.append(f"## Settings\n\nUnavailable ({exc!r}); see `config.yaml` and `record/constants.json`.\n\n")
    parts.append("## Estimators\n\nIn full in `record/estimator.md`. With x = l_A − l_B (log-odds): λ = β_C / β_D "
                 "(slopes of l_C − l_M and l_D − l_M on x), ρ = Cov(x, x′) / Var(x) from the second run, "
                 "φ = β_D / ρ, τ = (β_D − β_C) / ρ = φ(1 − λ); paired case bootstrap, ratio rule.\n\n")
    write_results(parts, run_dir, manifest, use)


def settings_table(consts: dict) -> str:
    d, ck, gen = consts["design"], consts["ckd"], consts["generation"]
    rows = [("Settings", ", ".join(f"{k} = pair {v}" for k, v in d["settings"].items())),
            ("Conditions", ", ".join(d["conditions"])),
            ("Second runs (for rho)", ", ".join(f"{k} x{v}" for k, v in d["replicates"].items())),
            ("CKD cases", str(ck["cases"])), ("CKD outputs", f"{len(ck['outputs'])} (Smolin & Wilder)"),
            ("Withheld fields (partial)", "; ".join(ck["withheld_fields_partial"].values())),
            ("Flipped field (false belief)", "; ".join(ck["flipped_field_false_belief"].values())),
            ("Withheld rendering / layouts", f"{ck['missing_fields']} / agent {ck['layout']}, own {ck['self_layout']}"
                                             f" / false belief {ck['false_belief_framing']}"),
            ("Bespoke samples per item, condition, run", str(consts["bespoke"]["samples"])),
            ("Comprehension follow-ups", f"after {', '.join(d['comprehension']['conditions'])}"),
            ("Latent fit", ", ".join(f"{k} {v}" for k, v in consts["latent_fit"]["fit_config"].items())
             + f"; {consts['latent_fit']['quadrature_points']}-point quadrature"),
            ("Bootstrap", f"{consts['estimators']['bootstrap_resamples']} resamples, "
                          f"{consts['estimators']['interval']}, seed {consts['seed']}"),
            ("Camerer et al. w", str(d["camerer_w"]))]
    for m, g in gen.items():
        s = g["sampling"]
        rows.append((f"Generation, {display(m)}", f"{g['backend']}, temperature {s['temperature']}, top-p {s['top_p']}, "
                     f"top-k {s['top_k']}, max {s['max_tokens']} tokens, chat template {g['chat_template']}, "
                     f"no system prompt, settings hash {g['settings_signature']}"))
    return ("## Settings\n\nEvery value is in `record/constants.json`; the full config is `config.yaml`.\n\n"
            + md_table(pd.DataFrame(rows, columns=["setting", "value"]), ["setting", "value"]) + "\n")


def write_results(parts: list[str], run_dir: Path, manifest: dict, use: dict) -> None:

    results = run_dir / "outputs" / "curse" / "RESULTS.md"
    if results.exists():
        parts.append("## Results\n\n" + section(results.read_text().split("\n", 1)[1], 1) + "\n\n")
    figures = sorted((run_dir / "figures").glob("*.png"))
    if figures:
        parts.append("## Figures\n\n" + "".join(f"### {f.stem}\n\n![{f.stem}](figures/{f.name})\n\n" for f in figures))
    paper = manifest.get("paper_reproduction")
    if paper:
        parts.append("## Original paper reproduced\n\n`./reproduce.sh analysis` outputs are in `paper/` (figures in "
                     "`paper/figures/`). CKD reports↔decisions against `docs/baseline_ckd.csv` (the paper's numbers); "
                     "c-indices may differ by up to about 0.01 off the pinned macOS build:\n\n"
                     + md_table(pd.DataFrame([{"metric": k, **v} for k, v in paper.items()]),
                                ["metric", "max_abs_diff", "exact_rows", "rows"], 4) + "\n")
    tuning = manifest.get("tuning", {})
    if tuning:
        rows = []
        for m, t in tuning.items():
            for k, trial in enumerate(t.get("trials", []), 1):
                rows.append({"model": m, "trial": k, "engine": json.dumps(trial.get("engine")),
                             "output tok/s": trial.get("output_tokens_per_second"),
                             "chosen": "yes" if trial.get("engine") == t.get("engine") else "",
                             "error": (trial.get("error") or "")[:80]})
        smoke = pd.DataFrame([{"model": m, **{k: v for k, v in (t.get("smoke") or {}).items() if k != "by_output"},
                               "batch_size": t.get("batch_size")} for m, t in tuning.items()])
        parts.append("## Inference tuning and smoke test\n\nEach vLLM engine configuration generated the same "
                     "sample of real calls; the fastest was used for the run (full detail in `record/tuning/`).\n\n"
                     + md_table(pd.DataFrame(rows), ["model", "trial", "engine", "output tok/s", "chosen", "error"], 0)
                     + "\nSmoke checks on the chosen configuration's answers:\n\n"
                     + md_table(smoke, [c for c in ("model", "calls", "parse_rate", "truncated_rate", "thinking_rate",
                                                    "mean_output_tokens", "p95_output_tokens", "batch_size")
                                        if c in smoke]) + "\n")
    acts = manifest.get("activations", {})
    if acts:
        parts.append("## Frozen activations\n\nResidual stream at the end of the evidence, the end of the prompt and "
                     "just before the answer, every layer, bfloat16 (`second_order/freeze.py`; `freeze.load` reads "
                     f"them). In `{manifest['run_id']}_activations.tar`, not the main tarball.\n\n"
                     + md_table(pd.DataFrame([{"model": m, **v} for m, v in acts.items()]),
                                ["model", "exit_code", "gb", "minutes"], 2) + "\n")
    if use and "unavailable" not in use:
        rows = [{"model": m, "part": part, **{k: int(v) for k, v in u.items() if k != "first_to_last_response_hours"}}
                for m, calls in use.items() for part, u in calls.items() if part != "all"]
        parts.append("## Usage\n\n" + md_table(pd.DataFrame(rows), ["model", "part", "calls", "errors", "truncated",
                                                                    "input_tokens", "output_tokens"]) + "\n")
    parts.append("## Files\n\n"
                 "- `config.yaml`: the exact config used; `manifest.json`: stages, provenance, hardware\n"
                 "- `record/`: constants, estimator, usage, example prompts, bespoke items, pip freeze, nvidia-smi\n"
                 "- `outputs/curse/`: estimates, scatter, placebo, Bayes, comprehension, thresholds, decision curves, "
                 "RESULTS.md\n"
                 "- `outputs/retention/`, `outputs/fits/`, `outputs/comprehension/`, `outputs/transcripts_C/`\n"
                 "- `data/`: generation cache (every prompt hash and response), parsed responses, latents, beliefs\n"
                 "- `figures/`: the figures above (PDF and PNG); `paper/`: the reproduced paper\n"
                 "- `record/tuning/`: every inference-tuning trial and its smoke checks\n"
                 "- `<run_id>_activations.tar` (separate): `data/activations/<model>/<dataset>/` acts.npy, index, meta\n"
                 "- `logs/`: one log per stage and model\n")
    (run_dir / "RUN_REPORT.md").write_text("".join(parts))


def main() -> None:
    run_dir = Path(sys.argv[1]).resolve()
    record = run_dir / "record"
    record.mkdir(parents=True, exist_ok=True)
    (record / "constants.json").write_text(json.dumps(constants(), indent=2, default=str))
    (record / "estimator.md").write_text(ESTIMATOR + leakage.__doc__)
    (record / "usage.json").write_text(json.dumps(available(lambda: usage(run_dir)), indent=2))
    models = list(C.load()["experiment"]["models"])
    if models:
        prompts = available(lambda: example_prompts(models[0]))
        (record / "example_prompts_ckd.md").write_text(prompts if isinstance(prompts, str)
                                                         else f"Unavailable: {prompts['unavailable']}\n")
    write_report(run_dir)
    print(f"wrote {run_dir / 'RUN_REPORT.md'}")


if __name__ == "__main__":
    main()
