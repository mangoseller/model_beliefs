"""Run the whole curse-of-knowledge experiment end to end and package everything.

Launched by ``run_experiment.sh`` (which installs the dependencies first):

    bash run_experiment.sh                          # Qwen3 8B and 14B, everything
    bash run_experiment.sh --smoke                  # 20 CKD cases, 3 samples: a quick full check
    bash run_experiment.sh --run-id 20261121-0930   # resume an interrupted run

Stages, in order (``--stages`` runs a subset, ``--skip`` leaves some out):

1. ``fetch_data``: the Smolin & Wilder results bundle from Zenodo (CKD cases, prompts, bin
   edges), checked against Zenodo's MD5 and the bundle manifest.  Resolves the bespoke item
   file; if none is supplied the test fixture stands in and the run is marked as using
   placeholder data.
2. ``fetch_models``: downloads each local model's weights, tokenizer and config from the
   Hugging Face Hub and pins the exact snapshot (commit hash) in the run's config, so vLLM
   loads exactly those files (offline from then on).
3. ``checks``: unit tests, the bespoke item checks, the GPU and disk.
4. ``paper``: ``./reproduce.sh analysis`` (the original paper's figures and tables) and a
   comparison with the recorded CKD numbers.
5. ``generate``: every condition, run, sample and comprehension follow-up per model
   (``run_generation``).  Each model's parsing and CPU latent fits (``phase3``) start in the
   background as soon as its generation ends, while the next model generates.
6. ``analyze``: ``second_order.curse`` (estimates, checks, tables) and ``second_order.figures``.
7. ``report``: ``RUN_REPORT.md`` and ``record/`` (every constant, the estimator, the
   environment, the model snapshots, the data provenance, usage, example prompts).
8. ``package``: ``runs/<run_id>.tar.gz`` with everything except the model weights and files
   rebuilt from the bundle.

Everything a run writes is under ``runs/<run_id>/``: ``config.yaml`` (the exact settings
used), ``data/`` (generation cache, responses, latents, beliefs), ``outputs/`` (tables and
``curse/RESULTS.md``), ``figures/``, ``paper/`` (the reproduced paper), ``record/``,
``logs/``, ``manifest.json`` and ``RUN_REPORT.md``.  Reruns with the same ``--run-id``
resume: cached responses are never regenerated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
DEFAULT_MODELS = ("qwen3_8b", "qwen3_14b")
STAGES = ("fetch_data", "fetch_models", "checks", "paper", "generate", "analyze", "report", "package")
ZENODO_RECORD = "23350428"
ZENODO_FILE = "llm_beliefs_replication_results.tar.gz"
FIXTURE_ITEMS = ROOT / "tests" / "fixtures" / "bespoke_items.yaml"
DEFAULT_ITEMS = ROOT / "data" / "bespoke" / "items.yaml"
MODEL_FILES = ["*.json", "*.safetensors", "*.txt", "*.model", "*.jinja", "*.py", "*.tiktoken", "LICENSE*"]
EXCLUDE_FROM_TARBALL = ("paper/prompts", "paper/parsed", "paper/sources")
SO_ENV = ("SO_CONFIG", "SO_DATA_DIR", "SO_RESULTS_DIR", "SO_FIGURES_DIR", "SO_BESPOKE_ITEMS")


# ---------------------------------------------------------------- run state
class Run:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.id = args.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ("-smoke" if args.smoke else "")
        self.dir = RUNS / self.id
        for sub in ("logs", "record", "data", "outputs", "figures", "paper"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {
            "run_id": self.id, "created": now(), "stages": {}}
        self.manifest.update({"last_invocation": now(), "argv": sys.argv[1:], "models": list(args.models),
                              "smoke": bool(args.smoke)})
        self.config_path = self.dir / "config.yaml"
        self.env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                        SO_CONFIG=str(self.config_path), SO_DATA_DIR=str(self.dir / "data"),
                        SO_RESULTS_DIR=str(self.dir / "outputs"), SO_FIGURES_DIR=str(self.dir / "figures"),
                        PYTHONUNBUFFERED="1")
        if self.manifest.get("bespoke_items"):
            self.env["SO_BESPOKE_ITEMS"] = self.manifest["bespoke_items"]["path"]
        if self.manifest.get("models_pinned"):
            self.env["HF_HUB_OFFLINE"] = "1"
        self.save()

    def save(self) -> None:
        self.manifest["updated"] = now()
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2, default=str))

    def log(self, message: str) -> None:
        line = f"[{now()}] {message}"
        print(line, flush=True)
        with (self.dir / "logs" / "runner.log").open("a") as handle:
            handle.write(line + "\n")

    def sh(self, command: list[str], log: str, *, env: dict | None = None, check: bool = True,
           cwd: Path = ROOT) -> int:
        """Run a command, streaming its output into logs/<log>.log; returns the exit code."""
        self.log(f"$ {' '.join(command)}  (log: logs/{log}.log)")
        with (self.dir / "logs" / f"{log}.log").open("a") as handle:
            handle.write(f"\n=== {now()} $ {' '.join(command)}\n")
            handle.flush()
            code = subprocess.run(command, cwd=cwd, env=env or self.env, stdout=handle, stderr=subprocess.STDOUT).returncode
        if check and code:
            raise RuntimeError(f"{command[2] if len(command) > 2 else command[0]} exited with {code}; see logs/{log}.log")
        return code

    def py(self, module: str, *args: str, log: str, check: bool = True) -> int:
        return self.sh([sys.executable, "-m", module, *args], log, check=check)

    def popen(self, module: str, *args: str, log: str) -> subprocess.Popen:
        command = [sys.executable, "-m", module, *args]
        self.log(f"$ {' '.join(command)} &  (log: logs/{log}.log)")
        handle = (self.dir / "logs" / f"{log}.log").open("a")
        # Background CPU work (latent fits) leaves half the cores to vLLM's scheduler and tokenizer.
        threads = str(max(2, (os.cpu_count() or 4) // 2))
        env = dict(self.env, OMP_NUM_THREADS=threads, MKL_NUM_THREADS=threads)
        return subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def capture(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(unavailable: {exc})"


# ---------------------------------------------------------------- config
def write_config(run: Run) -> dict:
    """The run's config: the repository config with this run's choices (kept on resume)."""
    if run.config_path.exists():
        return yaml.safe_load(run.config_path.read_text())
    cfg = yaml.safe_load((ROOT / "configs" / "second_order.yaml").read_text())
    args = run.args
    cfg["experiment"]["models"] = list(args.models)
    cfg["main_cases"] = args.cases if args.cases is not None else (20 if args.smoke else cfg["main_cases"])
    if args.samples is not None or args.smoke:
        cfg["bespoke"]["samples"] = args.samples if args.samples is not None else 3
    if args.smoke:
        cfg["latent_fit"] = {"epochs": 3000, "patience": 200}
    cfg["analysis"]["bootstrap_resamples"] = args.resamples or (200 if args.smoke else
                                                                cfg["analysis"]["bootstrap_resamples"])
    run.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return cfg


# ---------------------------------------------------------------- stages
def stage_fetch_data(run: Run, cfg: dict) -> None:
    release = ROOT / "data" / "release"
    if not (release / "MANIFEST.sha256").exists():
        import requests

        meta = requests.get(f"https://zenodo.org/api/records/{ZENODO_RECORD}", timeout=60).json()
        entry = next(f for f in meta["files"] if f["key"] == ZENODO_FILE)
        target = ROOT / "data" / "downloads" / ZENODO_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or hashlib.md5(target.read_bytes()).hexdigest() != entry["checksum"].split(":")[1]:
            run.log(f"downloading the results bundle ({entry['size'] / 1e6:.0f} MB) from Zenodo")
            with requests.get(entry["links"]["self"], stream=True, timeout=600) as r:
                r.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in r.iter_content(1 << 20):
                        handle.write(chunk)
        md5 = hashlib.md5(target.read_bytes()).hexdigest()
        if md5 != entry["checksum"].split(":")[1]:
            raise RuntimeError(f"Zenodo bundle MD5 {md5} does not match {entry['checksum']}")
        with tarfile.open(target) as archive:
            archive.extractall(ROOT)
        run.manifest["zenodo"] = {"record": ZENODO_RECORD, "file": ZENODO_FILE, "md5": md5}
    run.sh([sys.executable, "-m", "latent_beliefs.release", "verify"], "fetch_data")
    run.manifest["release_manifest_sha256"] = sha256(release / "MANIFEST.sha256")

    items = Path(run.args.bespoke_items or os.environ.get("BESPOKE_ITEMS") or DEFAULT_ITEMS)
    placeholder = not items.exists()
    if placeholder:
        if run.args.require_real_data:
            raise RuntimeError(f"bespoke item file {items} not found (--require-real-data)")
        run.log(f"WARNING: no bespoke item file at {items}; using the test fixture as a PLACEHOLDER")
        items = FIXTURE_ITEMS
    shutil.copy(items, run.dir / "record" / "bespoke_items.yaml")
    run.manifest["bespoke_items"] = {"path": str(items.resolve()), "sha256": sha256(items), "placeholder": placeholder}
    run.env["SO_BESPOKE_ITEMS"] = str(items.resolve())
    run.py("second_order.bespoke", "--items", str(items), "--examples",
           str(run.dir / "record" / "example_prompts_bespoke.md"), log="fetch_data")


def local_models(run: Run, cfg: dict) -> list[str]:
    return [m for m in run.args.models if cfg["models"][m]["backend"] in ("vllm", "hf")]


def stage_fetch_models(run: Run, cfg: dict) -> None:
    from huggingface_hub import HfApi, snapshot_download

    pins = run.manifest.setdefault("model_snapshots", {})
    for model in local_models(run, cfg):
        entry = cfg["models"][model]
        repo = entry["path"]
        revision = entry.get("revision") or HfApi().model_info(repo).sha
        run.log(f"downloading {repo} at {revision}")
        path = snapshot_download(repo, revision=revision, allow_patterns=MODEL_FILES)
        files = sorted(p.name for p in Path(path).iterdir())
        size = sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())
        pins[model] = {"repo": repo, "revision": revision, "local_path": path, "files": files,
                       "size_gb": round(size / 1e9, 2),
                       "config": json.loads((Path(path) / "config.json").read_text())}
        entry["revision"] = revision
    run.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    run.manifest["models_pinned"] = True
    run.env["HF_HUB_OFFLINE"] = "1"


def stage_checks(run: Run, cfg: dict) -> None:
    clean = {k: v for k, v in run.env.items() if k not in SO_ENV}  # the tests use the repository config
    run.sh([sys.executable, "-m", "unittest", "tests.test_prompts_second_order", "tests.test_leakage",
            "tests.test_bespoke", "tests.test_generation"], "checks", env=clean)
    (run.dir / "record" / "nvidia_smi.txt").write_text(capture(["nvidia-smi"]))
    gpus = capture(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    free = shutil.disk_usage(ROOT).free / 1e9
    run.manifest["hardware"] = {"gpus": [g.strip() for g in gpus.splitlines() if g.strip()],
                                "cpus": os.cpu_count(), "disk_free_gb": round(free, 1),
                                "platform": platform.platform(), "python": sys.version.split()[0]}
    run.manifest["tools"] = {tool: (capture([tool, "--version"]).strip().splitlines() or ["not installed"])[0]
                             if shutil.which(tool) else "not installed" for tool in ("claude", "codex", "node", "uv")}
    if local_models(run, cfg) and not run.manifest["hardware"]["gpus"]:
        raise RuntimeError("no GPU found (nvidia-smi) but local models are scheduled")
    if free < 40:
        run.log(f"WARNING: only {free:.0f} GB of disk free; a full run needs about 120 GB including the weights")


def stage_paper(run: Run, cfg: dict) -> None:
    env = dict(run.env, LB_BUILD_DIR=str(run.dir / "paper"), PYTHON=sys.executable,
               WORKERS=str(max(1, min(8, (os.cpu_count() or 2) - 1))))
    run.sh(["bash", "reproduce.sh", "analysis"], "paper", env=env)
    import pandas as pd

    ref = pd.read_csv(ROOT / "docs" / "baseline_ckd.csv")
    new = pd.read_csv(run.dir / "paper" / "evaluation" / "reciprocal" / "model_summary.csv")
    merged = ref.merge(new[new.domain == "ckd"], on=["model_key", "condition"], suffixes=("_paper", "_rerun"))
    summary = {}
    for metric in [c for c in ref.columns if c.startswith(("mean_c_index", "pooled_r2"))]:
        diff = (merged[f"{metric}_paper"] - merged[f"{metric}_rerun"]).abs()
        merged[f"{metric}_absdiff"] = diff
        summary[metric] = {"max_abs_diff": float(diff.max()), "exact_rows": int((diff < 1e-12).sum()),
                           "rows": int(len(diff))}
    merged.to_csv(run.dir / "record" / "paper_baseline_comparison.csv", index=False)
    run.manifest["paper_reproduction"] = summary


def stage_generate(run: Run, cfg: dict) -> None:
    """Generation per model; each model's phase3 runs in the background during the next generation."""
    cases = ["--cases", str(cfg["main_cases"])]
    fits: list[tuple[str, subprocess.Popen]] = []
    failures = []
    for model in run.args.models:
        started = time.time()
        code = run.py("second_order.run_generation", "--model", model, "--approve", *cases,
                      log=f"generate_{model}", check=False)
        run.manifest.setdefault("generation", {})[model] = {"exit_code": code,
                                                             "minutes": round((time.time() - started) / 60, 1)}
        run.save()
        if code:
            failures.append(f"generation for {model} exited with {code}")
            continue
        job = run.popen("second_order.phase3", "--model", model, *cases, log=f"phase3_{model}")
        if run.args.no_parallel_fits:
            job.wait()
        fits.append((model, job))
    for model, job in fits:
        started = time.time()
        code = job.wait()
        run.manifest.setdefault("phase3", {})[model] = {"exit_code": code,
                                                         "wait_minutes": round((time.time() - started) / 60, 1)}
        if code:
            failures.append(f"phase3 for {model} exited with {code}")
    if failures:
        raise RuntimeError("; ".join(failures))


def stage_analyze(run: Run, cfg: dict) -> None:
    done = [m for m in run.args.models if run.manifest.get("phase3", {}).get(m, {}).get("exit_code") == 0
            or (run.dir / "data" / "beliefs" / m).exists()]
    if not done:
        raise RuntimeError("no model has beliefs to analyse")
    run.py("second_order.curse", "--models", *done, "--resamples", str(cfg["analysis"]["bootstrap_resamples"]),
           log="analyze")
    run.py("second_order.figures", log="analyze")


def stage_report(run: Run, cfg: dict) -> None:
    freeze = capture(["uv", "pip", "freeze", "--python", sys.executable]) if shutil.which("uv") else ""
    (run.dir / "record" / "pip_freeze.txt").write_text(freeze or capture([sys.executable, "-m", "pip", "freeze"]))
    run.manifest["git"] = {"commit": capture(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip(),
                           "branch": capture(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"]).strip(),
                           "dirty": bool(capture(["git", "-C", str(ROOT), "status", "--porcelain"]).strip())}
    run.manifest["stages"]["report"] = {"status": "running", "started": now()}
    run.save()
    # In a subprocess, so the second_order modules read this run's config (SO_CONFIG).
    run.py("second_order.runner_record", str(run.dir), log="report")


def stage_package(run: Run, cfg: dict) -> None:
    target = RUNS / f"{run.id}.tar.gz"
    run.manifest["tarball"] = str(target)
    run.save()

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = info.name.split("/", 1)[1] if "/" in info.name else ""
        return None if any(rel == p or rel.startswith(p + "/") for p in EXCLUDE_FROM_TARBALL) else info

    with tarfile.open(target, "w:gz") as archive:
        archive.add(run.dir, arcname=run.id, filter=keep)
    digest = sha256(target)
    (RUNS / f"{run.id}.tar.gz.sha256").write_text(f"{digest}  {target.name}\n")
    run.log(f"packaged {target} ({target.stat().st_size / 1e6:.0f} MB, sha256 {digest[:16]}...)")


STAGE_FUNCTIONS = {name: globals()[f"stage_{name}"] for name in STAGES}


# ---------------------------------------------------------------- driver
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--run-id", default=None, help="resume this run (runs/<run_id>)")
    parser.add_argument("--stages", nargs="+", choices=STAGES, default=list(STAGES))
    parser.add_argument("--skip", nargs="+", choices=STAGES, default=[])
    parser.add_argument("--smoke", action="store_true", help="20 CKD cases, 3 bespoke samples, short fits")
    parser.add_argument("--cases", default=None, help="CKD cases: 'all' or an integer (default: config)")
    parser.add_argument("--samples", type=int, default=None, help="bespoke samples per item and condition")
    parser.add_argument("--resamples", type=int, default=None, help="bootstrap resamples (default: config)")
    parser.add_argument("--bespoke-items", default=None, help=f"bespoke item file (default {DEFAULT_ITEMS})")
    parser.add_argument("--require-real-data", action="store_true",
                        help="fail instead of using the placeholder bespoke items")
    parser.add_argument("--no-parallel-fits", action="store_true", help="run each phase3 before the next model")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="run later stages after a failed one (report and package always run)")
    args = parser.parse_args()
    run = Run(args)
    cfg = write_config(run)
    run.log(f"run {run.id}: models {', '.join(args.models)}; directory {run.dir}")
    failed = False
    for stage in [s for s in STAGES if s in args.stages and s not in args.skip]:
        if failed and stage not in ("report", "package") and not args.continue_on_error:
            run.manifest["stages"][stage] = {"status": "skipped after an earlier failure"}
            continue
        run.log(f"stage {stage}: start")
        started = time.time()
        record = {"started": now()}
        try:
            STAGE_FUNCTIONS[stage](run, cfg)
            record["status"] = "ok"
        except Exception as exc:  # recorded; report and package still run
            failed = True
            record.update(status="failed", error=repr(exc), traceback=traceback.format_exc())
            run.log(f"stage {stage}: FAILED: {exc!r}")
        record["minutes"] = round((time.time() - started) / 60, 2)
        run.manifest["stages"][stage] = record
        run.save()
        run.log(f"stage {stage}: {record['status']} ({record['minutes']} min)")
    if run.manifest.get("bespoke_items", {}).get("placeholder"):
        run.log("NOTE: this run used PLACEHOLDER bespoke items (tests/fixtures/bespoke_items.yaml)")
    run.log(f"done; tarball: {run.manifest.get('tarball', '(not packaged)')}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
