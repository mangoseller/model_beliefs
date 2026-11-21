"""Smoke-test and tune local inference before the main run (the runner's ``tune`` stage).

    python -m second_order.tune --model qwen3_8b --out runs/<run_id>/record/tuning/qwen3_8b.json

1. **Benchmark set**: ``tuning.items`` real calls of the experiment, a seeded sample over
   both datasets, every condition and output (main run, first sample).
2. **Trials**: each candidate vLLM engine configuration (``tuning.candidates``: how many
   sequences run at once, the per-step token budget, the memory share for the KV cache,
   prefix caching) generates the whole benchmark set in a fresh process.  Output tokens
   per second, timed after the model has loaded, decide the winner.  A candidate that
   fails (e.g. out of memory) is recorded and skipped.
3. **Result**: the fastest engine settings and a chunk size (``batch_size``, calls handed
   to vLLM at a time: at least 4x its concurrent sequences, so the GPU rarely waits
   for the slowest answers) are written to ``--out``; the runner copies them into the
   run's config.  Engine settings do not change what is sampled, so they are not part
   of the cache's settings check.
4. **Smoke checks** on the winning trial's responses: parse rate (overall and per
   dataset and output), truncation rate (answers that hit the token limit), ``<think>``
   blocks, output lengths.  Exits non-zero if the parse rate is below
   ``tuning.min_parse_rate`` or truncation above ``tuning.max_truncated_rate``, so a
   broken setup stops before the long run.

Benchmark responses are not written to the generation cache.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from second_order import config as C
from second_order import datasets as DS

DEFAULTS = {
    "items": 1024, "min_parse_rate": 0.8, "max_truncated_rate": 0.1, "bespoke_share": 0.1,
    "candidates": [
        {"max_num_seqs": 256, "max_num_batched_tokens": 8192, "gpu_memory_utilization": 0.92,
         "enable_prefix_caching": True},
        {"max_num_seqs": 512, "max_num_batched_tokens": 16384, "gpu_memory_utilization": 0.92,
         "enable_prefix_caching": True},
        {"max_num_seqs": 1024, "max_num_batched_tokens": 32768, "gpu_memory_utilization": 0.92,
         "enable_prefix_caching": True},
    ],
}


def settings() -> dict:
    return {**DEFAULTS, **(C.load().get("tuning") or {})}


def benchmark_items(model: str, n: int, seed: int) -> list[dict]:
    """A seeded sample of the experiment's calls (main run, first sample), as plain dicts."""
    from second_order.run_generation import dataset_cases, experiment_items

    rng = np.random.default_rng(seed)
    conditions = C.scheduled_conditions(model)
    shares = {"ckd": 1 - settings()["bespoke_share"], "bespoke": settings()["bespoke_share"]}
    picked = []
    for dataset in C.datasets_for(model):
        try:
            pool = [i for i in experiment_items(model, dataset, dataset_cases(dataset, None, False), conditions)
                    if i.replicate == 0 and i.sample == 0]
        except FileNotFoundError:  # no bespoke item file
            continue
        k = min(len(pool), int(round(n * shares.get(dataset, 0))))
        picked += [pool[j] for j in sorted(rng.choice(len(pool), size=k, replace=False))]
    return [{"model": i.model, "case_id": i.case_id, "condition": i.condition, "output": i.output,
             "replicate": i.replicate, "prompt": i.prompt, "sample": i.sample, "dataset": i.dataset}
            for i in picked]


def smoke_stats(items, records) -> dict:
    from second_order.responses import visible_text

    stats: dict = {"calls": len(items), "by_output": {}}
    parsed, truncated, thinking, lengths = [], [], [], []
    for item, record in zip(items, records):
        text = record.get("text") or ""
        value, _ = DS.parse(item.dataset, item.output, visible_text(text))
        parsed.append(value is not None)
        truncated.append(record.get("stop_reason") == "length")
        thinking.append("<think>" in text and "</think>" in text and len(text.split("</think>")[0]) > 20)
        lengths.append((record.get("usage") or {}).get("output_tokens") or 0)
        key = f"{item.dataset}/{item.output}"
        stats["by_output"].setdefault(key, []).append(value is not None)
    stats["by_output"] = {k: round(float(np.mean(v)), 3) for k, v in sorted(stats["by_output"].items())}
    stats.update(parse_rate=round(float(np.mean(parsed)), 4), truncated_rate=round(float(np.mean(truncated)), 4),
                 thinking_rate=round(float(np.mean(thinking)), 4), mean_output_tokens=round(float(np.mean(lengths)), 1),
                 p95_output_tokens=float(np.percentile(lengths, 95)) if lengths else 0.0,
                 examples=[{"key": f"{i.dataset}/{i.condition}/{i.output}", "text": (r.get("text") or "")[-400:]}
                           for i, r in list(zip(items, records))[:3]])
    return stats


def trial(model: str, engine: dict, items_path: Path, out: Path) -> None:
    """One engine configuration in this process: load, generate every benchmark item, time it."""
    from second_order.run_generation import Item, generate_vllm, item_seed  # noqa: F401  (item_seed: same seeds)

    C.load()["models"][model]["engine"] = engine  # this process only
    items = [Item(**d) for d in json.loads(items_path.read_text())]
    result = {"engine": engine, "ok": False}
    try:
        started = time.time()
        generate_vllm(model, items[:1], int(C.load()["seed"]))  # load and warm up (CUDA graphs, compile)
        result["load_seconds"] = round(time.time() - started, 1)
        started = time.time()
        records = generate_vllm(model, items, int(C.load()["seed"]))
        seconds = time.time() - started
        tokens = sum((r.get("usage") or {}).get("output_tokens") or 0 for r in records)
        result.update(ok=True, seconds=round(seconds, 1), output_tokens=tokens,
                      output_tokens_per_second=round(tokens / seconds, 1),
                      calls_per_second=round(len(items) / seconds, 2), smoke=smoke_stats(items, records))
    except Exception as exc:  # out of memory and similar: recorded, the next candidate runs
        result["error"] = repr(exc)[:2000]
    out.write_text(json.dumps(result, indent=2))


def choose(model: str, n_items: int, trials: list[dict], s: dict) -> dict:
    """The fastest successful trial, its chunk size, and the smoke checks on its answers."""
    good = [t for t in trials if t.get("ok")]
    summary = {"model": model, "benchmark_calls": n_items, "trials": trials, "settings": s}
    if not good:
        summary["error"] = "every engine configuration failed"
        return summary
    best = max(good, key=lambda t: t["output_tokens_per_second"])
    seqs = int(best["engine"].get("max_num_seqs", 256))
    summary.update(engine=best["engine"], batch_size=max(1024, 4 * seqs),
                   output_tokens_per_second=best["output_tokens_per_second"], smoke=best["smoke"],
                   speedup_over_first=round(best["output_tokens_per_second"] / good[0]["output_tokens_per_second"], 2))
    problems = []
    if best["smoke"]["parse_rate"] < float(s["min_parse_rate"]):
        problems.append(f"parse rate {best['smoke']['parse_rate']:.1%} < {float(s['min_parse_rate']):.0%}")
    if best["smoke"]["truncated_rate"] > float(s["max_truncated_rate"]):
        problems.append(f"truncated {best['smoke']['truncated_rate']:.1%} > {float(s['max_truncated_rate']):.0%}")
    if problems:
        summary["error"] = "smoke check failed: " + "; ".join(problems)
    return summary


def tune(model: str, out: Path, n: int | None = None, candidates: list[dict] | None = None) -> dict:
    s = settings()
    n = n or int(s["items"])
    candidates = candidates or s["candidates"]
    items = benchmark_items(model, n, int(C.load()["seed"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tune_", dir=out.parent))
    (work / "items.json").write_text(json.dumps(items))
    print(f"[tune {model}] benchmark: {len(items)} calls; {len(candidates)} engine configurations", flush=True)
    trials = []
    for k, engine in enumerate(candidates, 1):
        print(f"[tune {model}] trial {k}/{len(candidates)}: {engine}", flush=True)
        path = work / f"trial_{k}.json"
        code = subprocess.run([sys.executable, "-m", "second_order.tune", "trial", "--model", model,
                               "--engine", json.dumps(engine), "--items", str(work / "items.json"),
                               "--out", str(path)], env=os.environ).returncode
        result = json.loads(path.read_text()) if path.exists() else {"engine": engine, "ok": False,
                                                                       "error": f"exit {code}"}
        trials.append(result)
        if result.get("ok"):
            print(f"[tune {model}] trial {k}: {result['output_tokens_per_second']:,.0f} output tok/s, "
                  f"{result['calls_per_second']} calls/s, parse rate {result['smoke']['parse_rate']:.1%}, "
                  f"truncated {result['smoke']['truncated_rate']:.1%} (load {result['load_seconds']} s)", flush=True)
        else:
            print(f"[tune {model}] trial {k} failed: {result.get('error')}", flush=True)
    summary = choose(model, len(items), trials, s)
    if summary.get("engine"):
        print(f"[tune {model}] chosen: {summary['engine']}, batch {summary['batch_size']}; "
              f"{summary['output_tokens_per_second']:,.0f} output tok/s ({summary['speedup_over_first']}x the first "
              f"configuration); smoke: parse {summary['smoke']['parse_rate']:.1%}, truncated "
              f"{summary['smoke']['truncated_rate']:.1%}, mean {summary['smoke']['mean_output_tokens']:.0f} output "
              "tokens", flush=True)
    if summary.get("error"):
        print(f"[tune {model}] {summary['error']}", flush=True)
    out.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")
    t = sub.add_parser("trial")
    t.add_argument("--model", required=True)
    t.add_argument("--engine", required=True)
    t.add_argument("--items", required=True)
    t.add_argument("--out", required=True)
    parser.add_argument("--model")
    parser.add_argument("--out")
    parser.add_argument("--items", type=int, default=None)
    parser.add_argument("--candidates", type=int, default=None, help="use only the first N candidates")
    args = parser.parse_args()
    if args.cmd == "trial":
        trial(args.model, json.loads(args.engine), Path(args.items), Path(args.out))
        return
    if C.model_config(args.model)["backend"] != "vllm":
        print(f"{args.model}: tuning applies to the vllm backend only")
        return
    candidates = settings()["candidates"][: args.candidates] if args.candidates else None
    summary = tune(args.model, Path(args.out), args.items, candidates)
    sys.exit(1 if summary.get("error") else 0)


if __name__ == "__main__":
    main()
