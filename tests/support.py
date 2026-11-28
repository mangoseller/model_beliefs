"""Fixtures for synthetic experiment runs."""
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import yaml

from belief_leakage import config as C, order_followup as F, run_generation as G

ROOT = Path(__file__).resolve().parents[1]


def synthetic_config():
    cfg = yaml.safe_load(C.BUNDLED_CONFIG.read_text())
    cfg["latent_fit"] = {"epochs": 1500, "patience": 100}
    return cfg


def write_config(root, cfg):
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def run_env(root, cfg):
    return {**os.environ, "SO_CONFIG": str(write_config(root, cfg)), "PYTHONPATH": str(ROOT)}


def run(args, env):
    done = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    if done.returncode:
        raise AssertionError(f"{args} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
    return done


@contextmanager
def isolated_config():
    saved = C.CONFIG_PATH, C.CACHE
    try:
        yield
    finally:
        C.CONFIG_PATH, C.CACHE = saved
        C.load.cache_clear()
        G.cache_index.cache_clear()


def prepared_followup(root, trials, items):
    plan = dict(protocol=F.PROTOCOL, models=["synthetic"], trials=trials, items=items,
                settings={"synthetic": G.settings_signature("synthetic", "ckd")})
    F.write_plan(root, plan)
    F.atomic_json(root / "prepared.json", {"plan_digest": plan["digest"]})
    return plan
