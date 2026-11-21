"""Record a run's constants and write its RUN_REPORT.md (the runner's ``report`` stage).

    python -m second_order.runner_record runs/<run_id>

Runs inside the run's environment (``SO_CONFIG`` etc. point at the run), so every
value below is the one the run used.  Writes into ``runs/<run_id>/``:

* ``record/constants.json``: every setting and constant behind the numbers (design,
  prompts, generation per model, latent fit, estimators, bootstrap, seeds);
* ``record/estimator.md``: the estimators, as implemented (``leakage.py``);
* ``record/usage.json``: calls, errors, tokens and wall-clock per model and dataset;
* ``record/example_prompts.md``: one CKD patient and one bespoke item in every condition;
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
from second_order import datasets as DS
from second_order import leakage
from second_order.conditions import CONDITIONS, SETTINGS, references
from second_order.fit_pooled import PROBABILITY_EPSILON, QUADRATURE_POINTS
from second_order.report import display, md_table
from second_order.responses import configured_specs
from second_order.run_generation import FOLLOWUP, sampling, settings_signature, template_kwargs

ESTIMATOR = """# Estimators, as implemented

All beliefs are on the log-odds scale. Stated probabilities (read from the JSON object
both datasets ask for) are clipped to [0.01, 0.99] before the logit. Latent beliefs are
posterior means of logit z from the pooled Smolin & Wilder fit of the believer's family
(own belief: A and every B; PERSON: every M, D, M2, D2 and C), with each CKD question in
each option order an output with its own item parameters. Per case (CKD patient or
bespoke item), in each setting (partial; false: the _F conditions) and for each
no-leakage reference of C (CKD two_record: M2 and D2, the headline; single_record: M and
D, as in the Methods):

    x   = l_A  - l_B                    the model's own gap (replicate 0)
    x'  = l_A' - l_B'                   the same gap from the independent second replicate
    beta_D = slope of (l_D - l_M) on x  (OLS with intercept; M, D = the reference's conditions)
    beta_C = slope of (l_C - l_M) on x

    lambda = beta_C / beta_D            leakage (the Wald estimator), per setting
    rho    = Cov(x, x') / Var(x)        reliability of the gap, per setting
    tau    = (beta_D - beta_C) / rho    theory-of-mind score, = phi (1 - lambda), per setting
    phi    = beta_D / rho               responsiveness, shared by the two settings: pooled
                                        within-setting beta_D over pooled rho

rho, phi and tau use calibrated values (latent posterior means de-shrunk with each fit's
own calibration line; equal to the raw values for stated probabilities). lambda uses raw
values; it is a ratio of slopes from one fit, so shrinkage cancels.

Uncertainty: paired bootstrap over patients (CKD) or items (bespoke), carrying all of a
case's conditions and replicates together; 95% percentile intervals, item parameters
held fixed. lambda also has a 95% Fieller confidence set from the bootstrap covariance of
beta_C and beta_D, unbounded when beta_D is weak. Ratio rule: any other ratio is blanked
when its denominator's interval includes 0 (tau: rho; phi: pooled rho).

Also reported (CKD): lambda by assigned record order; false-record lambda for the
randomized sicker and healthier edits (64 patients, bootstrap within label x direction);
the comprehension checks; the role-swap gaps and probe pairs. Bespoke: placebo gaps, the
report-assignment control (C_ctrl_F - D_F), answers against the Bayes answer.

Checks of the modelling assumptions:
- phi shared by the settings: each setting's own phi_setting = beta_D / rho, and diff_phi_setting;
- affine form: phi_D_on_A = Cov(l_D, l_A') / Cov(l_A, l_A') and
  phi_M_on_B = Cov(l_M, l_B') / Cov(l_B, l_B'), which should match phi;
  quadratic terms of both gap regressions (curvature_*), which should be 0;
- partial linearity: lambda for x > 0 and x < 0 separately, and within thirds of |x|;
- noise independence: placebo items (bespoke), whose three gaps should be 0.

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
    fit = FitConfig(progress_every=0, **(cfg.get("latent_fit") or {}))
    sets = cfg["datasets"]
    return {
        "design": {"settings": SETTINGS, "conditions": {k: asdict(c) for k, c in CONDITIONS.items()},
                   "references": {d: references(d) for d in ("ckd", "bespoke")},
                   "camerer_w": cfg["experiment"]["camerer_w"]},
        "ckd": available(lambda: {
            "dir": str(DS.ckd_dir()), "batteries": sets["ckd"]["batteries"], "patients": cfg["main_cases"],
            "checksums_ok": DS.ckd_checksums(),
            "prompts_per_battery": {b: int(len(DS.rows("ckd", [b]))) for b in sets["ckd"]["batteries"]},
            "person_names": sorted(DS.patients().person_name.unique()),
        }),
        "bespoke": available(lambda: {
            "file": sets["bespoke"]["file"], "samples": sets["bespoke"]["samples"],
            "replicates": sets["bespoke"]["replicates"], "items": DS.case_ids("bespoke"),
            "placebo_items": sorted(DS.placebo_ids()), "prompts": int(len(DS.rows("bespoke"))),
            "person_names": sorted(DS.rows("bespoke").person.unique()),
        }),
        "generation": {m: {"backend": C.model_config(m)["backend"], "weights": C.model_config(m).get("path"),
                           "revision": C.model_config(m).get("revision"),
                           "sampling": {d: {o: sampling(m, d, o) for o in ("probability", "choice")}
                                        for d in C.datasets_for(m)},
                           "chat_template": template_kwargs(m),
                           "settings_signature": {d: settings_signature(m, d) for d in C.datasets_for(m)},
                           "max_model_len": C.model_config(m).get("max_model_len"),
                           "batch_size": C.model_config(m).get("batch_size"),
                           "system_prompt": None} for m in models},
        "latent_fit": available(lambda: {
            "fit_config": {f.name: getattr(fit, f.name) for f in fields(fit)}, "batteries": cfg["latent"]["batteries"],
            "anchor_cutoffs": bool(cfg["latent"].get("anchor_cutoffs")),
            "outputs": {s.name: {"kind": s.kind, "cutoff": s.nominal_threshold} for s in configured_specs()},
            "quadrature_points": QUADRATURE_POINTS, "probability_epsilon": PROBABILITY_EPSILON,
            "calibration_simulated_cases": 20000,
            "split": "seeded per model over patients: 20% test, then 20% of the rest validation"}),
        "estimators": {"ratio_rule_denominators": leakage.DENOMINATOR, "min_cases_per_sign": leakage.MIN_SIGN_CASES,
                       "min_cases_per_third": leakage.MIN_THIRD_CASES, "logit_clip": [0.01, 0.99],
                       "fieller_z": leakage.FIELLER_Z,
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
                stage = "comprehension" if r.get("output") == FOLLOWUP else "answers"
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


def example_prompts() -> str:
    """One CKD patient and one bespoke item in every condition, exactly as sent."""
    ckd = DS.rows("ckd", list(DS.CKD_FILES))
    case_id = sorted(ckd.case_id.unique())[0]
    lines = [f"# CKD patient {case_id} in every condition\n\nThe probability question (and each check, probe "
             "and role swap); the other questions share each condition's records.\n"]
    pick = ckd[(ckd.case_id == case_id) & (ckd.replicate == 0)
               & ckd.output.isin(["probability/normal", "check/normal", "role_check/normal"])]
    for r in pick.itertuples():
        lines.append(f"\n## {r.condition} / {r.output}\n\n```\n{r.prompt}\n```\n")
    bespoke = DS.rows("bespoke")
    item = sorted(bespoke.case_id.unique())[0]
    lines.append(f"\n# Bespoke item {item} in every condition\n")
    for r in bespoke[bespoke.case_id == item].itertuples():
        lines.append(f"\n## {r.condition}\n\n```\n{r.prompt}\n```\n")
        if r.comprehension_q:
            lines.append(f"\nFollow-up: {r.comprehension_q} (keyed: {r.comprehension_a})\n")
    return "".join(lines)


def section(text: str, depth: int = 1) -> str:
    """Demote markdown headings by ``depth`` levels."""
    return "\n".join("#" * depth + line if line.startswith("#") else line for line in text.splitlines())


def write_report(run_dir: Path) -> None:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    consts = json.loads((run_dir / "record" / "constants.json").read_text())
    use = json.loads((run_dir / "record" / "usage.json").read_text())
    parts = [f"# Curse-of-knowledge run {manifest['run_id']}\n\n",
             f"Created {manifest['created']}; report written {manifest.get('updated')}. "
             f"Models: {', '.join(display(m) for m in manifest['models'])}."
             f"{' **Smoke run** (reduced cases, samples and fits).' if manifest.get('smoke') else ''}\n\n"]
    warnings = [f"Dataset file `{name}` does not match its recorded SHA-256." for name, ok in
                manifest.get("datasets", {}).get("ckd_checksums", {}).items() if not ok]
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
    data = manifest.get("datasets", {})
    checks = data.get("ckd_checksums", {})
    setup.append(f"- **CKD data**: `datasets/ckd_second_order_v3`, {sum(checks.values())} of {len(checks)} files "
                 f"match `SHA256SUMS`; its validator {data.get('ckd_validator', 'did not run')}.")
    setup.append(f"- **Bespoke data**: `datasets/second_order_simple_prompts.csv` (SHA-256 "
                 f"`{data.get('bespoke_sha256', '?')[:16]}…`).")
    z = manifest.get("zenodo")
    setup.append(f"- **Paper reproduction**: Smolin & Wilder results bundle, Zenodo record "
                 f"{z['record'] if z else '23350428'}{', MD5 ' + z['md5'] if z else ''}.")
    parts.append("\n".join(setup) + "\n\n")

    try:
        parts.append(settings_table(consts))
    except (KeyError, TypeError) as exc:
        parts.append(f"## Settings\n\nUnavailable ({exc!r}); see `config.yaml` and `record/constants.json`.\n\n")
    parts.append("## Estimators\n\nIn full in `record/estimator.md`. With x = l_A − l_B (log-odds): λ = β_C / β_D "
                 "(slopes of l_C − l_M and l_D − l_M on x; CKD's headline uses C's matched references M2 and D2), "
                 "ρ = Cov(x, x′) / Var(x) from the second replicate, τ = (β_D − β_C) / ρ per setting, and "
                 "φ = β_D / ρ shared by the two settings; bootstrap over patients or items, Fieller set for λ.\n\n")
    write_results(parts, run_dir, manifest, use)


def settings_table(consts: dict) -> str:
    d, ck, gen = consts["design"], consts["ckd"], consts["generation"]
    be, lf = consts["bespoke"], consts["latent_fit"]
    rows = [("Settings", ", ".join(d["settings"])),
            ("Conditions", ", ".join(d["conditions"])),
            ("CKD patients", str(ck["patients"])),
            ("CKD prompt files", ", ".join(f"{b} ({n:,} prompts)" for b, n in ck["prompts_per_battery"].items())),
            ("Bespoke", f"{len(be['items'])} items ({len(be['placebo_items'])} placebo), {be['samples']} samples "
                        f"per item, condition and run; second runs: "
                        + ", ".join(f"{k} x{v}" for k, v in be["replicates"].items())),
            ("Latent fit", f"{len(lf['outputs'])} outputs ({', '.join(lf['batteries'])}), cutoffs "
                           f"{'anchored' if lf['anchor_cutoffs'] else 'free'}; "
                           + ", ".join(f"{k} {v}" for k, v in lf["fit_config"].items())
                           + f"; {lf['quadrature_points']}-point quadrature"),
            ("Bootstrap", f"{consts['estimators']['bootstrap_resamples']} resamples, "
                          f"{consts['estimators']['interval']}, seed {consts['seed']}"),
            ("Camerer et al. w", str(d["camerer_w"]))]
    for m, g in gen.items():
        s = next(iter(next(iter(g["sampling"].values())).values()))
        limits = ", ".join(f"{d} {o} {v['max_tokens']}" for d, by in g["sampling"].items() for o, v in by.items())
        rows.append((f"Generation, {display(m)}", f"{g['backend']}, temperature {s['temperature']}, top-p {s['top_p']}, "
                     f"top-k {s['top_k']}, max tokens {limits}, chat template {g['chat_template']}, "
                     f"no system prompt"))
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
        parts.append("## Original paper reproduced\n\n`latent_beliefs/reproduce.sh analysis` outputs are in `paper/` "
                     "(figures in `paper/figures/`). CKD reports↔decisions against `latent_beliefs/baseline_ckd.csv` "
                     "(the paper's numbers); "
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
    prompts = available(example_prompts)
    (record / "example_prompts.md").write_text(prompts if isinstance(prompts, str)
                                               else f"Unavailable: {prompts['unavailable']}\n")
    write_report(run_dir)
    print(f"wrote {run_dir / 'RUN_REPORT.md'}")


if __name__ == "__main__":
    main()
