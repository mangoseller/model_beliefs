"""Run the whole curse-of-knowledge experiment end to end and package everything.

Launched by ``run_experiment.sh`` (which installs the dependencies first):

    bash run_experiment.sh                          # Qwen3 8B and 14B, everything
    bash run_experiment.sh --smoke                  # 20 CKD cases, 3 samples: a quick full check
    bash run_experiment.sh --run-id 20261121-0930   # resume an interrupted run

Stages, in order (``--stages`` runs a subset, ``--skip`` leaves some out):

1. ``fetch_data``: the Smolin & Wilder results bundle from Zenodo (for the ``paper`` stage),
   checked against Zenodo's MD5 and the bundle manifest; checks the two datasets in
   ``datasets/`` against their recorded SHA-256 sums.
2. ``fetch_models``: downloads each local model's weights, tokenizer and config from the
   Hugging Face Hub and pins the exact snapshot (commit hash) in the run's config, so vLLM
   loads exactly those files (offline from then on).
3. ``checks``: unit tests, the CKD dataset's own validator, the GPU and disk.
4. ``paper``: ``latent_beliefs/reproduce.sh analysis`` (the original paper's figures and
   tables) and a comparison with the paper's CKD numbers (``latent_beliefs/baseline_ckd.csv``).
5. ``tune``: a smoke test and inference tuning per model (``second_order.tune``): a sample
   of real calls under a few vLLM engine settings, the fastest kept in the run's config;
   stops the run if the answers do not parse or are truncated.
6. ``generate``: every condition, run, sample and comprehension follow-up per model
   (``run_generation``, with a progress bar).  Each model's parsing and CPU latent fits
   (``phase3``) start in the background as soon as its generation ends.
7. ``activations``: residual-stream activations at three positions and every layer, for
   the probability answers (``second_order.freeze``), on the GPU while the fits run.
8. ``fits``: waits for the background fits (or runs them, on a resumed run).
9. ``analyze``: ``second_order.curse`` (estimates, checks, tables) and ``second_order.figures``.
10. ``report``: ``RUN_REPORT.md`` and ``record/`` (every constant, the estimator, the
    environment, the model snapshots, the tuning, the data provenance, usage, example prompts).
11. ``package``: ``runs/<run_id>.tar.gz`` with everything except the model weights, the
    activations and files rebuilt from the bundle; ``runs/<run_id>_activations.tar`` holds
    the activations.

Everything a run writes is under ``runs/<run_id>/``: ``config.yaml`` (the exact settings
used), ``data/`` (generation cache, responses, latents, beliefs), ``outputs/`` (tables and
``curse/RESULTS.md``), ``figures/``, ``paper/`` (the reproduced paper), ``record/``,
``logs/``, ``manifest.json`` and ``RUN_REPORT.md``.  Reruns with the same ``--run-id``
resume: cached responses are never regenerated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
DEFAULT_MODELS = ("qwen3_8b", "qwen3_14b")
STAGES = ("fetch_data", "fetch_models", "checks", "paper", "tune", "generate", "activations", "fits", "analyze",
          "report", "package")
ZENODO_RECORD = "23350428"
ZENODO_FILE = "llm_beliefs_replication_results.tar.gz"
MODEL_FILES = ["*.json", "*.safetensors", "*.txt", "*.model", "*.jinja", "*.py", "*.tiktoken", "LICENSE*"]
EXCLUDE_FROM_TARBALL = ("paper/prompts", "paper/parsed", "paper/sources", "data/activations")
SO_ENV = ("SO_CONFIG", "SO_DATA_DIR", "SO_RESULTS_DIR", "SO_FIGURES_DIR", "SO_DATASETS_DIR")


# ---------------------------------------------------------------- run state
class Run:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.id = args.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ("-smoke" if args.smoke else "")
        self.dir = RUNS / self.id
        for sub in ("logs", "record", "data", "outputs", "figures", "paper"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.fits: list[tuple[str, subprocess.Popen]] = []  # background phase3 jobs of this invocation
        self.heartbeat: Heartbeat | None = None
        self.manifest_path = self.dir / "manifest.json"
        self.resumed = self.manifest_path.exists()
        self.manifest = json.loads(self.manifest_path.read_text()) if self.resumed else {
            "run_id": self.id, "created": now(), "stages": {}, "models": list(args.models), "smoke": bool(args.smoke)}
        if self.resumed:  # a resumed run keeps the models and smoke setting it started with
            args.models, args.smoke = self.manifest["models"], self.manifest["smoke"]
        self.manifest.update({"last_invocation": now(), "argv": sys.argv[1:]})
        self.config_path = self.dir / "config.yaml"
        self.env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                        SO_CONFIG=str(self.config_path), SO_DATA_DIR=str(self.dir / "data"),
                        SO_RESULTS_DIR=str(self.dir / "outputs"), SO_FIGURES_DIR=str(self.dir / "figures"),
                        PYTHONUNBUFFERED="1",
                        # progress bars: every second when someone watches a terminal, else every 30 s
                        SO_PROGRESS_INTERVAL="1" if sys.stdout.isatty() else "30")
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
           cwd: Path = ROOT, echo: bool = False) -> int:
        """Run a command, its output into logs/<log>.log (and to the console with ``echo``)."""
        self.log(f"$ {' '.join(command)}  (log: logs/{log}.log)")
        with (self.dir / "logs" / f"{log}.log").open("ab") as handle:
            handle.write(f"\n=== {now()} $ {' '.join(command)}\n".encode())
            handle.flush()
            if not echo:
                code = subprocess.run(command, cwd=cwd, env=env or self.env, stdout=handle,
                                      stderr=subprocess.STDOUT).returncode
            else:  # tee, byte for byte, so progress bars (carriage returns) survive
                proc = subprocess.Popen(command, cwd=cwd, env=env or self.env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT)
                while chunk := os.read(proc.stdout.fileno(), 65536):
                    handle.write(chunk)
                    handle.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                code = proc.wait()
        if check and code:
            raise RuntimeError(f"{command[2] if len(command) > 2 else command[0]} exited with {code}; see logs/{log}.log")
        return code

    def py(self, module: str, *args: str, log: str, check: bool = True, echo: bool = False) -> int:
        return self.sh([sys.executable, "-m", module, *args], log, check=check, echo=echo)

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
    cfg = yaml.safe_load((ROOT / "second_order" / "config.yaml").read_text())
    args = run.args
    cfg["experiment"]["models"] = list(args.models)
    cfg["main_cases"] = args.cases if args.cases is not None else (20 if args.smoke else cfg["main_cases"])
    if args.samples is not None or args.smoke:
        cfg["datasets"]["bespoke"]["samples"] = args.samples if args.samples is not None else 3
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
            from tqdm import tqdm

            run.log(f"downloading the results bundle ({entry['size'] / 1e6:.0f} MB) from Zenodo")
            with requests.get(entry["links"]["self"], stream=True, timeout=600) as r, \
                    tqdm(total=entry["size"], unit="B", unit_scale=True, desc="Zenodo bundle",
                         mininterval=float(run.env["SO_PROGRESS_INTERVAL"])) as bar:
                r.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in r.iter_content(1 << 20):
                        handle.write(chunk)
                        bar.update(len(chunk))
        md5 = hashlib.md5(target.read_bytes()).hexdigest()
        if md5 != entry["checksum"].split(":")[1]:
            raise RuntimeError(f"Zenodo bundle MD5 {md5} does not match {entry['checksum']}")
        with tarfile.open(target) as archive:
            archive.extractall(ROOT)
        run.manifest["zenodo"] = {"record": ZENODO_RECORD, "file": ZENODO_FILE, "md5": md5}
    run.sh([sys.executable, "-m", "latent_beliefs.release", "verify"], "fetch_data")
    run.manifest["release_manifest_sha256"] = sha256(release / "MANIFEST.sha256")

    from second_order import config as C
    from second_order import datasets as DS

    sums = DS.ckd_checksums()
    run.manifest["datasets"] = {"ckd_checksums": sums,
                                "bespoke_sha256": sha256(C.resolve(cfg["datasets"]["bespoke"]["file"]))}
    bad = [name for name, ok in sums.items() if not ok]
    if bad:
        raise RuntimeError(f"CKD dataset files differ from SHA256SUMS: {', '.join(bad)}")
    run.log(f"datasets: {len(sums)} CKD files match SHA256SUMS; {len(DS.case_ids('ckd'))} patients, "
            f"{len(DS.case_ids('bespoke'))} bespoke items")


def validate_ckd(run: Run) -> None:
    """Run the CKD dataset's own validator on a decompressed copy of the release."""
    from second_order import datasets as DS

    work = run.dir / "tmp_validate"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(DS.ckd_dir(), work)
    for gz in work.rglob("*.gz"):
        with gzip.open(gz) as src, gz.with_suffix("").open("wb") as dst:
            shutil.copyfileobj(src, dst)
        gz.unlink()
    code = subprocess.run([sys.executable, "-I", "validate.py"], cwd=work, capture_output=True, text=True)
    (run.dir / "logs" / "ckd_validate.log").write_text(code.stdout + code.stderr)
    shutil.rmtree(work, ignore_errors=True)
    run.manifest["datasets"]["ckd_validator"] = "passed" if code.returncode == 0 else f"failed ({code.returncode})"
    if code.returncode:
        raise RuntimeError("the CKD dataset validator failed; see logs/ckd_validate.log")


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
    run.sh([sys.executable, "-m", "unittest", "tests.test_datasets", "tests.test_leakage",
            "tests.test_generation", "tests.test_runner"], "checks", env=clean)
    validate_ckd(run)
    has_smi = shutil.which("nvidia-smi") is not None
    (run.dir / "record" / "nvidia_smi.txt").write_text(capture(["nvidia-smi"]) if has_smi else "nvidia-smi not found\n")
    gpus = capture(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]) \
        if has_smi else ""
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
    run.sh(["bash", "latent_beliefs/reproduce.sh", "analysis"], "paper", env=env, echo=True)
    import pandas as pd

    ref = pd.read_csv(ROOT / "latent_beliefs" / "baseline_ckd.csv")
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


def last_line(path: Path, marker: str) -> str:
    """The last line of a log that contains ``marker`` (for progress updates)."""
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if marker in line]
    except OSError:
        return ""
    return lines[-1].strip() if lines else ""


class Heartbeat:
    """Every few minutes, report background jobs (latent fits) while the foreground runs."""

    def __init__(self, run: Run, every: float = 300):
        self.run, self.every, self.jobs, self.stop = run, every, [], threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def add(self, model: str, job: subprocess.Popen) -> None:
        self.jobs.append((model, job, time.time()))

    def loop(self) -> None:
        while not self.stop.wait(self.every):
            for model, job, started in self.jobs:
                if job.poll() is None:
                    latest = last_line(self.run.dir / "logs" / f"phase3_{model}.log", "fit ") or "parsing responses"
                    self.run.log(f"background: phase3 {model} running {(time.time() - started) / 60:.0f} min; "
                                 f"latest: {latest}")

    def close(self) -> None:
        self.stop.set()


def stage_tune(run: Run, cfg: dict) -> None:
    """Smoke test and engine tuning for each vLLM model; the winner goes into the run's config."""
    failures = []
    for model in [m for m in run.args.models if cfg["models"][m]["backend"] == "vllm"]:
        out = run.dir / "record" / "tuning" / f"{model}.json"
        out.unlink(missing_ok=True)  # never read a previous invocation's result
        extra = ["--items", "256", "--candidates", "2"] if run.args.smoke else []
        run.log(f"tuning {model}: timing engine settings on a sample of real calls (log: logs/tune_{model}.log)")
        code = run.py("second_order.tune", "--model", model, "--out", str(out), *extra, log=f"tune_{model}",
                      check=False, echo=True)
        summary = json.loads(out.read_text()) if out.exists() else {}
        if code and not summary.get("error"):
            summary["error"] = f"tune exited with {code} (log: logs/tune_{model}.log)"
        if summary.get("engine"):
            cfg["models"][model]["engine"] = summary["engine"]
            cfg["models"][model]["batch_size"] = summary["batch_size"]
        run.manifest.setdefault("tuning", {})[model] = {
            k: summary.get(k) for k in ("engine", "batch_size", "output_tokens_per_second", "speedup_over_first",
                                        "error")} | {"smoke": {k: v for k, v in (summary.get("smoke") or {}).items()
                                                              if k != "examples"},
                                                     "trials": [{k: t.get(k) for k in ("engine", "ok", "error",
                                                                                       "output_tokens_per_second")}
                                                                for t in summary.get("trials", [])]}
        if summary.get("error"):
            failures.append(f"{model}: {summary['error']}")
    run.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    if failures:
        raise RuntimeError("; ".join(failures))


def stage_generate(run: Run, cfg: dict) -> None:
    """Generation per model; each model's phase3 starts in the background as soon as it ends."""
    cases = ["--cases", str(cfg["main_cases"])]
    failures = []
    run.heartbeat = run.heartbeat or Heartbeat(run)
    for n, model in enumerate(run.args.models, 1):
        run.log(f"model {n}/{len(run.args.models)}: {model}: generating (progress bar below; "
                f"log: logs/generate_{model}.log)")
        started = time.time()
        code = run.py("second_order.run_generation", "--model", model, *cases,
                      log=f"generate_{model}", check=False, echo=True)
        run.manifest.setdefault("generation", {})[model] = {"exit_code": code,
                                                             "minutes": round((time.time() - started) / 60, 1)}
        run.save()
        if code:
            failures.append(f"generation for {model} exited with {code}")
            continue
        run.log(f"model {model}: generation done in {(time.time() - started) / 60:.1f} min; "
                f"parsing and latent fits start in the background (log: logs/phase3_{model}.log)")
        job = run.popen("second_order.phase3", "--model", model, *cases, log=f"phase3_{model}")
        run.heartbeat.add(model, job)
        if run.args.no_parallel_fits:
            job.wait()
        run.fits.append((model, job))
    if failures:
        raise RuntimeError("; ".join(failures))


def generated(run: Run) -> list[str]:
    return [m for m in run.args.models if run.manifest.get("generation", {}).get(m, {}).get("exit_code") == 0]


def stage_activations(run: Run, cfg: dict) -> None:
    """Freeze residual-stream activations (GPU) while the latent fits run (CPU)."""
    failures = []
    for model in [m for m in generated(run) if cfg["models"][m]["backend"] in ("vllm", "hf")]:
        started = time.time()
        code = run.py("second_order.freeze", "--model", model, "--cases", str(cfg["main_cases"]),
                      log=f"activations_{model}", check=False, echo=True)
        folder = run.dir / "data" / "activations" / model
        size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) if folder.exists() else 0
        run.manifest.setdefault("activations", {})[model] = {"exit_code": code, "gb": round(size / 1e9, 2),
                                                              "minutes": round((time.time() - started) / 60, 1)}
        if code:
            failures.append(f"freezing activations for {model} exited with {code}")
    if failures:
        raise RuntimeError("; ".join(failures))


def stage_fits(run: Run, cfg: dict) -> None:
    """Wait for the background phase3 jobs; run phase3 for generated models that have none."""
    run.heartbeat = run.heartbeat or Heartbeat(run)
    started_here = {m for m, _ in run.fits}
    for model in [m for m in generated(run) if m not in started_here]:
        job = run.popen("second_order.phase3", "--model", model, "--cases", str(cfg["main_cases"]),
                        log=f"phase3_{model}")
        run.fits.append((model, job))
        run.heartbeat.add(model, job)
    if any(job.poll() is None for _, job in run.fits):
        run.log("waiting for the latent fits (CPU) to finish; progress every 5 minutes")
    failures = []
    for model, job in run.fits:
        started = time.time()
        code = job.wait()
        run.log(f"model {model}: phase3 finished (exit {code}); "
                f"{last_line(run.dir / 'logs' / f'phase3_{model}.log', 'cases complete') or ''}")
        run.manifest.setdefault("phase3", {})[model] = {"exit_code": code,
                                                         "wait_minutes": round((time.time() - started) / 60, 1)}
        if code:
            failures.append(f"phase3 for {model} exited with {code}")
    run.heartbeat.close()
    run.heartbeat = None
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
    import pandas as pd

    est = pd.read_csv(run.dir / "outputs" / "curse" / "estimates.csv")
    head = est[(est.case_set == "all") & est.statistic.isin(["lambda", "phi", "tau"])
               & (est.setting != "false_minus_partial")]
    for (model, dataset, method, setting), g in head.groupby(["model", "dataset", "method", "setting"], sort=False):
        g = g.set_index("statistic")
        cells = [f"{s} {g.loc[s].estimate:.2f} [{g.loc[s].ci_lower:.2f}, {g.loc[s].ci_upper:.2f}]"
                 for s in ("lambda", "phi", "tau") if s in g.index and pd.notna(g.loc[s].estimate)]
        run.log(f"result: {model} {dataset} {method} {setting}: " + ("; ".join(cells) or "blank (ratio rule)"))


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


def write_archive(target: Path, add) -> str:
    """Write a tar archive atomically (a temporary file renamed on success); returns its SHA-256."""
    partial = target.with_name(target.name + ".partial")
    with tarfile.open(partial, "w:gz" if target.name.endswith(".gz") else "w") as archive:
        add(archive)
    os.replace(partial, target)
    digest = sha256(target)
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n")
    return digest


def stage_package(run: Run, cfg: dict) -> None:
    for model, job in run.fits:  # never archive files a background fit is still writing
        if job.poll() is None:
            run.log(f"waiting for phase3 {model} before packaging")
            job.wait()

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = info.name.split("/", 1)[1] if "/" in info.name else ""
        return None if any(rel == p or rel.startswith(p + "/") for p in EXCLUDE_FROM_TARBALL) else info

    target = RUNS / f"{run.id}.tar.gz"
    digest = write_archive(target, lambda archive: archive.add(run.dir, arcname=run.id, filter=keep))
    run.manifest["tarball"] = str(target)
    run.log(f"packaged {target} ({target.stat().st_size / 1e6:.0f} MB, sha256 {digest[:16]}...)")
    # Activations: only folders whose freeze finished (meta.json says complete); bf16 bits barely compress.
    complete = [m.parent for m in (run.dir / "data" / "activations").rglob("meta.json")
                if json.loads(m.read_text()).get("complete")]
    if complete:
        extra = RUNS / f"{run.id}_activations.tar"
        digest = write_archive(extra, lambda archive: [archive.add(f, arcname=f"{run.id}/{f.relative_to(run.dir)}")
                                                       for f in complete])
        run.manifest["activations_tarball"] = str(extra)
        run.log(f"packaged {extra} ({extra.stat().st_size / 1e9:.2f} GB, sha256 {digest[:16]}...): "
                f"{len(complete)} complete activation sets")


STAGE_FUNCTIONS = {
    "fetch_data": stage_fetch_data, "fetch_models": stage_fetch_models, "checks": stage_checks,
    "paper": stage_paper, "tune": stage_tune, "generate": stage_generate, "activations": stage_activations,
    "fits": stage_fits, "analyze": stage_analyze, "report": stage_report, "package": stage_package,
}
# Run even after a failed stage: collect background fits, analyse whatever finished, report, package.
ALWAYS_RUN = ("fits", "analyze", "report", "package")
# On a resumed run, skipped when they already succeeded (unless named in --stages): slow, and redone
# identically (the tuned engine settings are already in the run's config).
ONCE = ("paper", "tune")


# ---------------------------------------------------------------- driver
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--run-id", default=None, help="resume this run (runs/<run_id>)")
    parser.add_argument("--stages", nargs="+", choices=STAGES, default=None, help="default: all")
    parser.add_argument("--skip", nargs="+", choices=STAGES, default=[])
    parser.add_argument("--smoke", action="store_true", help="20 CKD cases, 3 bespoke samples, short fits")
    parser.add_argument("--cases", default=None, help="CKD cases: 'all' or an integer (default: config)")
    parser.add_argument("--samples", type=int, default=None, help="bespoke samples per item and condition")
    parser.add_argument("--resamples", type=int, default=None, help="bootstrap resamples (default: config)")
    parser.add_argument("--no-parallel-fits", action="store_true", help="run each phase3 before the next model")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="run every later stage after a failed one (fits, analyze, report and "
                             "package always run)")
    args = parser.parse_args()
    run = Run(args)
    cfg = write_config(run)
    run.log(f"run {run.id}: models {', '.join(args.models)}; directory {run.dir}")
    failed = False
    run_started = time.time()
    selected = [s for s in STAGES if s in (args.stages or STAGES) and s not in args.skip]
    if run.resumed and args.stages is None:
        done = [s for s in ONCE if run.manifest["stages"].get(s, {}).get("status") == "ok"]
        if done:
            run.log(f"resuming: {', '.join(done)} already done (name them in --stages to redo)")
        selected = [s for s in selected if s not in done]
    for number, stage in enumerate(selected, 1):
        if failed and stage not in ALWAYS_RUN and not args.continue_on_error:
            run.manifest["stages"][stage] = {"status": "skipped after an earlier failure"}
            run.save()
            continue
        run.log(f"[stage {number}/{len(selected)}] {stage}: start")
        started = time.time()
        record = {"started": now()}
        try:
            STAGE_FUNCTIONS[stage](run, cfg)
            record["status"] = "ok"
        except Exception as exc:  # recorded; the ALWAYS_RUN stages still run
            failed = True
            record.update(status="failed", error=repr(exc), traceback=traceback.format_exc())
            run.log(f"stage {stage}: FAILED: {exc!r}")
        record["minutes"] = round((time.time() - started) / 60, 2)
        run.manifest["stages"][stage] = record
        run.save()
        run.log(f"[stage {number}/{len(selected)}] {stage}: {record['status']} ({record['minutes']} min; "
                f"{(time.time() - run_started) / 3600:.2f} h so far)")
    run.log(f"done; tarball: {run.manifest.get('tarball', '(not packaged)')}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
