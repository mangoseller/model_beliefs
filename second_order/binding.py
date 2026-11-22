"""Binding vs inhibition experiment, session 1: prompts, plan, generation, activation capture, export.

The question is whether the model's own record leaks into its estimate of PERSON's belief because it
misattributes the records (binding: fixed by naming each record's owner) or because it cannot set aside what
it knows (inhibition: survives labelling and an explicit statement of what PERSON lacks). The design is a
superset of the chart-order follow-up (``order_followup``): every CKD patient sees every cell in both block
orders. A cell is (variant, binding, absence, order):

* variant:  ``partial`` (the release's three withheld fields), ``false`` (its two edited
  fields), ``strong`` (age, diabetes and hypertension withheld for every patient, built
  from the release's full records exactly as the release builds Ep: by deleting lines);
* binding:  ``original`` (partial wording) / ``verified`` (false wording with the
  "verified" cue), ``neutral`` (cue removed), ``labelled`` (named Record X / Record Y
  blocks with end markers, no cue), ``instructed`` (labelled plus an instruction to
  answer from PERSON's point of view);
* absence:  ``implicit`` (a field is missing) or ``explicit`` (PERSON's block says which
  fields it lacks and that PERSON does not know them);
* order:    ``own_first`` or ``person_first`` (whole blocks move; field order is kept).

Every prompt is a transformation of a released prompt, so field orders, names and
questions never change between arms.  Identical prompts are one item: an arm that
reproduces a base-run prompt reuses the archived answer, and an arm that reproduces an
order-follow-up prompt gets that plan's exact item key, so answers move between the two
plans with ``import-cache``.

    python -m second_order.binding prepare --run runs/binding-pilot --profile pilot \\
        --base-run <base run dir or tarball> --base-activations <base run dir or activations tar>
    python -m second_order.binding status   --run runs/binding-pilot
    python -m second_order.binding verify   --run runs/binding-pilot
    python -m second_order.binding import-cache --run runs/binding-full --from runs/binding-pilot
    python -m second_order.binding generate --run runs/binding-pilot --model qwen3_8b
    python -m second_order.binding capture  --run runs/binding-pilot --model qwen3_8b
    python -m second_order.binding export   --run runs/binding-pilot --model qwen3_8b
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import json
import re
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from second_order import config as C, datasets as DS, run_generation as G
from second_order import order_followup as F

PROTOCOL = "ckd-binding-v1"
TAG = "binding_v1"
DRIFT_TAG = "drift_v1"
ORDERS = F.ORDERS
CUE = F.CUE
OWN_HEAD = "Your record for this patient:"
PERSON_HEAD = re.compile(r"^(.+?) has only the following record for this patient and accepts its contents as accurate:$")
LABELLED_X = re.compile(r"^Record X \((.+?)'s record\)\. ")
LABELLED_Y = "Record Y (your record):"
FACT = re.compile(r"^(.+) \((\w+)\) is (.+)\.$", flags=re.M)
STRONG = ("age", "diab", "htn")
# Check and changed-facts names -> record field codes (verified against the release in the tests).
NAME_CODE = {"age": "age", "diabetes": "diab", "hypertension": "htn", "cardiovascular disease": "cvd",
             "race/ethnicity": "rce", "household income": "inc", "body mass index": "bmi",
             "smoking status": "smkstat", "physical activity": "pa", "sex": "sex",
             "education level": "edu", "sodium intake": "sodium"}
LABELS = {"sex": "sex", "inc": "household income bracket", "diab": "self-reported diabetes",
          "smkstat": "current smoking status", "edu": "education level", "bmi": "body mass index",
          "pa": "any physical activity indicator", "htn": "self-reported hypertension", "rce": "race/ethnicity",
          "age": "age", "cvd": "history of cardiovascular disease", "sodium": "estimated daily sodium intake"}

# arm -> (variant, binding, absence)
ARMS = {
    "partial_original": ("partial", "original", "implicit"),
    "false_verified": ("false", "verified", "implicit"),
    "false_neutral": ("false", "neutral", "implicit"),
    "partial_labelled": ("partial", "labelled", "implicit"),
    "false_labelled": ("false", "labelled", "implicit"),
    "partial_labelled_explicit": ("partial", "labelled", "explicit"),
    "partial_instructed": ("partial", "instructed", "implicit"),
    "partial_instructed_explicit": ("partial", "instructed", "explicit"),
    "false_instructed": ("false", "instructed", "implicit"),
    "strong_original": ("strong", "original", "implicit"),
    "strong_labelled": ("strong", "labelled", "implicit"),
    "strong_labelled_explicit": ("strong", "labelled", "explicit"),
    "strong_instructed": ("strong", "instructed", "implicit"),
    "strong_instructed_explicit": ("strong", "instructed", "explicit"),
}
ORDER_FOLLOWUP_ARMS = ("partial_original", "false_verified", "false_neutral")
# the order follow-up's (setting, wording) -> arm
FOLLOWUP_ARM = {("partial", "original"): "partial_original", ("false", "verified"): "false_verified",
                ("false", "neutral"): "false_neutral"}
# The six arms that decide H1-H3 (without the instruction or the strong variant).
CORE_ARMS = ORDER_FOLLOWUP_ARMS + ("partial_labelled", "false_labelled", "partial_labelled_explicit")
PROFILES = {
    # Part 1. 30 fit-half patients, Qwen3 8B, every arm, stated probability only (10 samples) plus checks.
    "pilot": dict(models=["qwen3_8b"], n_patients=30, arms=list(ARMS), latent_arms=[], direct_samples=10,
                  drift_fraction=0.0, activations=False),
    # Part 3a. All 150 patients, both models, every arm; the latent battery on the order follow-up's arms
    # and the two labelled arms.
    "full": dict(models=["qwen3_8b", "qwen3_14b"], n_patients="all", arms=list(ARMS),
                 latent_arms=list(CORE_ARMS[:5]), direct_samples=5, drift_fraction=0.02, activations=True),
    # Part 3b. Thinking mode on the deciding arms, stated probability only.
    "thinking": dict(models=["qwen3_8b_think", "qwen3_14b_think"], n_patients="all", arms=list(CORE_ARMS),
                     latent_arms=[], direct_samples=3, drift_fraction=0.0, activations=False),
    # Part 3c. Qwen3 32B (no baseline exists: every cell is new), stated probability only.
    "32b": dict(models=["qwen3_32b"], n_patients="all", arms=list(ARMS), latent_arms=[], direct_samples=5,
                drift_fraction=0.0, activations=False),
}
# Model entries added to the archived config when a profile needs them (existing entries are never changed,
# so every base-run settings signature stays the same). Thinking: same weights and sampling, thinking on, and
# a token limit that leaves room for the reasoning (max_model_len 16384).
_ENGINE = {"max_num_seqs": 1024, "max_num_batched_tokens": 32768, "gpu_memory_utilization": 0.92,
           "enable_prefix_caching": True}
_COMMON = {"backend": "vllm", "datasets": ["ckd", "bespoke"], "dtype": "bfloat16", "max_model_len": 16384,
           "batch_size": 4096}
EXTRA_MODELS = {
    # KV cache on an H200: ~250k tokens for 32B, ~600-770k for 14B/8B; thinking answers run to thousands of
    # tokens. max_num_seqs keeps the batch inside the cache (no preemption); larger batches mean fewer tails.
    "qwen3_32b": _COMMON | {"path": "Qwen/Qwen3-32B", "revision": "9216db5781bf21249d130ec9da846c4624c16137",
                            "enable_thinking": False, "engine": _ENGINE | {"max_num_seqs": 256}},
    "qwen3_8b_think": _COMMON | {"path": "Qwen/Qwen3-8B", "revision": "b968826d9c46dd6066d109eabc6255188de91218",
                                 "enable_thinking": True, "max_tokens": 12288, "batch_size": 8192,
                                 "engine": _ENGINE | {"max_num_seqs": 256}},
    "qwen3_14b_think": _COMMON | {"path": "Qwen/Qwen3-14B", "revision": "40c069824f4251a91eefaf281ebe4c544efd3e18",
                                  "enable_thinking": True, "max_tokens": 12288, "batch_size": 8192,
                                  "engine": _ENGINE | {"max_num_seqs": 256}},
}


# Prompt structure
@dataclass
class Parsed:
    blocks: list          # [(owner, [lines])], owner "own" or "person", in display order
    rest: list            # paragraphs after the records (question, instructions)
    name: str | None      # PERSON's name


def owner_of(head: str) -> tuple[str | None, str | None]:
    """(owner, PERSON's name) of a paragraph's first line, for release and labelled headers."""
    if head.startswith(OWN_HEAD) or head.startswith(LABELLED_Y):
        return "own", None
    if (m := PERSON_HEAD.match(head)) or (m := LABELLED_X.match(head)):
        return "person", m.group(1)
    return None, None


def parse(prompt: str) -> Parsed:
    blocks, rest, name = [], [], None
    for para in prompt.split("\n\n"):
        owner, who = owner_of(para.split("\n", 1)[0])
        if owner and not rest:
            blocks.append((owner, para.split("\n")))
            name = who or name
        else:
            rest.append(para)
    if not blocks:
        raise ValueError("prompt has no record block")
    return Parsed(blocks, rest, name)


def render(p: Parsed) -> str:
    return "\n\n".join(["\n".join(lines) for _, lines in p.blocks] + p.rest)


def record_blocks(prompt: str) -> list[dict]:
    """Every record block of any arm's prompt: owner, PERSON's name, character span, fields."""
    out, pos = [], 0
    for para in prompt.split("\n\n"):
        owner, name = owner_of(para.split("\n", 1)[0])
        if owner:
            out.append(dict(owner=owner, name=name, start=pos, end=pos + len(para), fields=fields(para.split("\n"))))
        pos += len(para) + 2
    return out


def person_name(prompt: str) -> str | None:
    return next((b["name"] for b in record_blocks(prompt) if b["name"]), None)


def fields(lines) -> dict[str, str]:
    return {code: value for _, code, value in FACT.findall("\n".join(lines))}


def block(p: Parsed, owner: str) -> list[str]:
    found = [lines for o, lines in p.blocks if o == owner]
    if len(found) != 1:
        raise ValueError(f"expected one {owner} block")
    return found[0]


# Prompt transformations (each applied to a released prompt)
def remove_fields(p: Parsed, codes, owners) -> None:
    for i, (owner, lines) in enumerate(p.blocks):
        if owner in owners:
            p.blocks[i] = (owner, [l for l in lines if not (m := FACT.match(l)) or m.group(2) not in codes])


def reorder(p: Parsed, order: str) -> None:
    if len(p.blocks) != 2:
        raise ValueError("order needs two record blocks")
    own, person = block(p, "own"), block(p, "person")
    p.blocks = [("own", own), ("person", person)] if order == "own_first" else [("person", person), ("own", own)]


def drop_cue(p: Parsed) -> None:
    for i, (owner, lines) in enumerate(p.blocks):
        if owner == "own" and lines[0] == OWN_HEAD + CUE:
            p.blocks[i] = (owner, [OWN_HEAD] + lines[1:])


def label(p: Parsed) -> None:
    """Named blocks with end markers. Only ownership is named: that PERSON has not seen Record Y is left to the
    instruction arm, so labelling alone adds no statement about what PERSON lacks."""
    name, out = p.name, []
    for owner, lines in p.blocks:
        if owner == "person":
            head = (f"Record X ({name}'s record). {name} has only this record for this patient "
                    f"and accepts its contents as accurate:")
            out.append((owner, [head] + lines[1:] + [f"(End of Record X, {name}'s record.)"]))
        else:
            head = LABELLED_Y
            out.append((owner, [head] + lines[1:] + ["(End of Record Y, your record.)"]))
    p.blocks = out


def explicit_absence(p: Parsed) -> None:
    """Name every field PERSON's block lacks, before its end marker."""
    missing = [LABELS[c] for c in LABELS if c not in fields(block(p, "person"))]
    if not missing:
        return
    listed = ", ".join(missing[:-1]) + (" or " if len(missing) > 1 else "") + missing[-1]
    line = f"Record X has no entry for {listed}. {p.name} does not know these values."
    for i, (owner, lines) in enumerate(p.blocks):
        if owner == "person":
            end = len(lines) - 1 if lines[-1].startswith("(End of Record X") else len(lines)
            p.blocks[i] = (owner, lines[:end] + [line] + lines[end:])


def instruction(name: str) -> str:
    return (f"Answer from {name}'s point of view. {name} knows only what is in Record X and has not seen "
            f"Record Y. When asked what {name} believes, give {name}'s estimate, not your own.")


def transform(source: str, arm: str | None, order: str | None, role: str, rest_from: str | None = None) -> str:
    """The prompt of ``role`` in cell (``arm``, ``order``) built from a released prompt (``rest_from``: take
    the question paragraphs from another prompt, for the strong checks). A and B take no order."""
    p = parse(source)
    if rest_from is not None:
        p.rest = parse(rest_from).rest
    if arm is None:
        return render(p)
    variant, binding, absence = ARMS[arm]
    if variant == "strong" and role in ("B", "C", "M"):
        remove_fields(p, STRONG, {"B": ("own",), "C": ("person",), "M": ("own", "person")}[role])
    if variant == "false" and binding != "verified":
        drop_cue(p)
    if role in ("C", "M", "D"):
        reorder(p, order)
        if binding in ("labelled", "instructed"):
            label(p)
        if absence == "explicit" and role in ("C", "M"):
            explicit_absence(p)
        if binding == "instructed":
            p.rest = [instruction(p.name)] + p.rest
    return render(p)


def source_condition(arm: str, role: str) -> str:
    """The released condition each role is built from."""
    variant, binding, _ = ARMS[arm]
    if variant == "partial":
        return role
    if variant == "false":
        return {"A": "A_F" if binding == "verified" else "A", "B": "B_F", "C": "C_F", "M": "M_F", "D": "D_F"}[role]
    return {"A": "A", "B": "A", "C": "D", "M": "D", "D": "D"}[role]


def own_belief_version(arm: str, role: str) -> str:
    """Name of the A/B version a cell uses (A/B are shared by every arm with the same version)."""
    variant, binding, _ = ARMS[arm]
    if role == "A":
        return "A_F" if variant == "false" and binding == "verified" else "A"
    return {"partial": "B", "strong": "B_strong"}.get(variant, "B_F" if binding == "verified" else "B_F_neutral")


def decode_condition(condition: str, case_id: str) -> tuple[str | None, str, str]:
    """(arm or None, block order or 'shared', role) of any condition name in a plan."""
    if condition.startswith(DRIFT_TAG + "__"):
        condition = condition.split("__", 1)[1]
    parts = condition.split("__")
    if parts[0] == TAG:
        _, what, order, role = parts
        return (what if what in ARMS else None), order, role
    if parts[0] == "followup_v1":
        _, setting, wording, order, role = parts
        return (FOLLOWUP_ARM[(setting, wording)] if role in "CMD" else None), order, role
    role = condition.split("_")[0]
    arm = "false_verified" if condition.endswith("_F") else "partial_original"
    order = DS.patients().loc[case_id, "block_order"] if role in "CMD" else "shared"
    return (arm if role in "CMD" else None), order, role


# Patients
def patient_split(seed: int) -> pd.Series:
    """Fixed fit/test halves (Phase 2 fits on 'fit', Phase 3 tests on 'test'), stratified by
    label x assigned block order."""
    rng = np.random.default_rng(int(seed) + 7919)
    out = {}
    for _, group in DS.patients().groupby(["label", "block_order"]):
        ids = sorted(group.item_id)
        rng.shuffle(ids)
        half = len(ids) // 2 + (rng.random() < 0.5 if len(ids) % 2 else 0)
        out |= {cid: "fit" if k < half else "test" for k, cid in enumerate(ids)}
    return pd.Series(out, name="split").sort_index()


def select_patients(n, split: pd.Series, seed: int) -> list[str]:
    """``n`` fit-half patients stratified by label x order (round robin), or every patient."""
    if n == "all":
        return sorted(split.index)
    rng = np.random.default_rng(int(seed) + 104729)
    pts = DS.patients().loc[split.index[split == "fit"]]
    strata = [sorted(g.item_id) for _, g in pts.groupby(["label", "block_order"])]
    for ids in strata:
        rng.shuffle(ids)
    chosen, k = [], 0
    while len(chosen) < int(n) and any(strata):
        if strata[k % len(strata)]:
            chosen.append(strata[k % len(strata)].pop())
        k += 1
    return sorted(chosen)


# Plan
release_frame = F.release_frame


class Builder:
    """Accumulates trials and unique items; identical prompts share one item."""

    def __init__(self, model, originals):
        self.model, self.originals = model, originals
        self.items, self.identity, self.trials = {}, {}, []

    def resolve(self, item: G.Item, source: str = "new") -> str:
        idt = (item.case_id, item.output, item.replicate, item.sample, item.prompt_sha)
        if idt not in self.identity:
            original = self.originals.get(idt[:3] + (item.prompt_sha,)) if item.sample == 0 else None
            chosen = original or item
            self.identity[idt] = chosen.key
            self.items[chosen.key] = asdict(chosen) | {"key": chosen.key, "prompt_sha": chosen.prompt_sha,
                                                       "source": "baseline" if original else source}
        return self.identity[idt]

    def add(self, trial: dict, item: G.Item) -> None:
        key = self.resolve(item)
        self.trials.append(trial | {"item_key": key, "source": self.items[key]["source"]})


def make_plan(model: str, cases, spec: dict, split: pd.Series, seed: int, use_baseline: bool = True):
    """(trials, items) of one model. ``use_baseline``: reuse the base run's answers and the order follow-up's
    item keys (only for models the base run generated)."""
    frame = release_frame(cases)
    records = frame.to_dict("records")
    index = {(r["case_id"], r["condition"], r["output"], r["replicate"]): r for r in records}
    originals = {}
    if use_baseline:
        for r in records:
            item = G.Item(model, r["case_id"], r["condition"], r["output"], r["replicate"], r["prompt"])
            originals.setdefault((item.case_id, item.output, item.replicate, item.prompt_sha), item)
    b = Builder(model, originals)
    if use_baseline:
        # Seed identities with the order follow-up's items so identical prompts get its exact keys.
        for data in F.make_plan(model, cases)[1].values():
            b.resolve(F.item_from(data), data["source"])
    latent = sorted(set(frame.output[~frame.question_id.isin(("check", "role_check"))]))
    k_direct = max(1, int(spec["direct_samples"]))
    for case in sorted(cases):
        for arm in spec["arms"]:
            variant, binding, absence = ARMS[arm]
            outputs = latent if arm in spec["latent_arms"] else ["probability/normal"]
            for role in ("A", "B", "C", "M", "D"):
                src = source_condition(arm, role)
                for order in (ORDERS if role in "CMD" else ("shared",)):
                    condition = (f"{TAG}__{arm}__{order}__{role}" if role in "CMD"
                                 else f"{TAG}__{own_belief_version(arm, role)}__shared__{role}")
                    trial = dict(model=model, dataset="ckd", case_id=case, split=split[case], arm=arm, variant=variant,
                                 binding=binding, absence=absence, block_order=order, role=role,
                                 ab_version=own_belief_version(arm, role) if role in "AB" else "",
                                 check_answer="", check_fact="", check_relation="", drift_of="")
                    for output in outputs:
                        for rep in ((0, 1) if role in "AB" else (0,)):
                            prompt = transform(index[(case, src, output, rep)]["prompt"], arm,
                                               order if role in "CMD" else None, role)
                            for sample in (range(k_direct) if output == "probability/normal" else (0,)):
                                b.add(trial | dict(output=output, replicate=rep, sample=sample),
                                      G.Item(model, case, condition, output, rep, prompt, sample))
                    if role == "C":
                        _add_checks(b, index, case, arm, order, condition, trial)
    if spec.get("drift_fraction"):
        _add_drift(b, float(spec["drift_fraction"]), seed, split)
    used = {t["item_key"] for t in b.trials}
    return b.trials, {k: v for k, v in b.items.items() if k in used}


def _add_checks(b, index, case, arm, order, condition, trial):
    """The cell's comprehension check, both option orders. Strong checks are re-keyed from their own records."""
    variant = ARMS[arm][0]
    for output in ("check/normal", "check/flip"):
        row = index[(case, "C_F" if variant == "false" else "C", output, 0)]
        if variant == "strong":
            prompt = transform(index[(case, "D", "probability/normal", 0)]["prompt"], arm, order, "C",
                               rest_from=row["prompt"])
            person = next(blk["fields"] for blk in record_blocks(prompt) if blk["owner"] == "person")
            shown = NAME_CODE[row["check_fact"]] in person
            answer = next(k for k, v in DS.options(prompt).items() if v == ("Yes" if shown else "No"))
            relation = "shown" if shown else "withheld"
        else:
            prompt = transform(row["prompt"], arm, order, "C")
            answer, relation = row["check_answer"], row["check_relation"]
        b.add(trial | dict(output=output, replicate=0, sample=0, check_answer=answer, check_fact=row["check_fact"],
                           check_relation=relation), G.Item(b.model, case, condition, output, 0, prompt))


def _add_drift(b, fraction, seed, split):
    """Re-generate a stratified sample of reused baseline items under new keys (session drift)."""
    strata = {}
    for k in sorted(k for k, v in b.items.items() if v["source"] == "baseline"):
        v = b.items[k]
        strata.setdefault((v["condition"], v["output"].split("/")[0]), []).append(k)
    rng = np.random.default_rng(int(seed) + 31337)
    for _, keys in sorted(strata.items()):
        n = max(1, int(round(fraction * len(keys))))
        for k in sorted(rng.choice(keys, size=min(n, len(keys)), replace=False)):
            v = b.items[k]
            b.add(dict(model=b.model, dataset="ckd", case_id=v["case_id"], split=split[v["case_id"]], arm="drift",
                       variant="", binding="", absence="", block_order="", role="drift", ab_version="",
                       output=v["output"], replicate=v["replicate"], sample=0, check_answer="", check_fact="",
                       check_relation="", drift_of=k),
                  G.Item(b.model, v["case_id"], f"{DRIFT_TAG}__{v['condition']}", v["output"], v["replicate"],
                         v["prompt"], 0))


def capture_items_of(plan, model) -> list[dict]:
    """Items whose activations a plan captures: new probability items of replicate 0 (binding plans: sample 0,
    no drift re-generations, and none unless the profile captures activations)."""
    if plan.get("protocol") == PROTOCOL and not plan["spec"]["activations"]:
        return []
    return [v for v in F.capture_items(plan, model)
            if v.get("sample", 0) == 0 and not v["condition"].startswith(DRIFT_TAG)]


def summary(plan) -> dict:
    out = {}
    for model in plan["models"]:
        items = [v for v in plan["items"].values() if v["model"] == model]
        new = {v["key"] for v in items if v["source"] == "new"}
        trials = [t for t in plan["trials"] if t["model"] == model]
        out[model] = dict(reused_generations=len(items) - len(new), new_generations=len(new),
                          new_by_kind=dict(Counter(v["output"].split("/")[0] for v in items if v["key"] in new)),
                          new_trial_rows_by_arm=dict(sorted(Counter(t["arm"] for t in trials if t["item_key"] in new).items())),
                          new_activation_rows=len(capture_items_of(plan, model)),
                          patients=len({t["case_id"] for t in trials}))
    return out


# Stages
def with_models(config_bytes: bytes, models) -> bytes:
    """The archived config, plus entries for requested models it lacks (existing entries untouched)."""
    cfg = yaml.safe_load(config_bytes)
    missing = [m for m in models if m not in cfg["models"]]
    if not missing:
        return config_bytes
    unknown = [m for m in missing if m not in EXTRA_MODELS]
    if unknown:
        raise ValueError(f"models {unknown} are neither in the config nor in EXTRA_MODELS")
    cfg["models"] |= {m: EXTRA_MODELS[m] for m in missing}
    return yaml.safe_dump(cfg, sort_keys=False).encode()


def prepare(args):
    root = args.run.resolve()
    root.mkdir(parents=True, exist_ok=True)
    spec = copy.deepcopy(PROFILES[args.profile]) | {"profile": args.profile}
    if args.models:
        spec["models"] = args.models
    if args.n_patients:
        spec["n_patients"] = args.n_patients if args.n_patients == "all" else int(args.n_patients)
    if args.direct_samples is not None:
        spec["direct_samples"] = args.direct_samples
    config_bytes = F.read_member(args.base_run, "config.yaml") if args.base_run else C.CONFIG_PATH.read_bytes()
    F.install_config(root, with_models(config_bytes, spec["models"]))
    seed = int(C.load()["seed"])
    split = patient_split(seed)
    cases = select_patients(spec["n_patients"], split, seed)
    # Only models the base run generated have archived answers to reuse.
    base_models = set(C.load()["experiment"]["models"]) if args.base_run else set()
    trials, items, signatures = [], {}, {}
    for model in spec["models"]:
        ts, its = make_plan(model, cases, spec, split, seed, model in base_models)
        trials.extend(ts)
        items.update(its)
        signatures[model] = G.settings_signature(model, "ckd")
    plan = dict(protocol=PROTOCOL, spec=spec, models=spec["models"], settings=signatures, cases=cases,
                patient_split=split.to_dict(), base_run=str(args.base_run.resolve()) if args.base_run else None,
                base_activations=str(args.base_activations.resolve()) if args.base_activations else None,
                trials=trials, items=items)
    F.write_plan(root, plan)
    split.rename_axis("case_id").reset_index().to_csv(root / "patient_split.csv", index=False)
    for model in spec["models"]:
        wanted = {k: v for k, v in items.items() if v["model"] == model and v["source"] == "baseline"}
        if wanted:
            F.copy_baseline(root, model, wanted, signatures[model], args.base_run, args.base_activations)
    F.atomic_json(root / "prepared.json", {"plan_digest": plan["digest"], "budget": summary(plan)})
    print(json.dumps(summary(plan), indent=2))


def read_plan(root, protocols=(PROTOCOL,)):
    """A verified plan (``order_followup.read_plan``) of one of ``protocols``."""
    plan = F.read_plan(root)
    if plan.get("protocol") not in protocols:
        raise ValueError(f"{root}: protocol {plan.get('protocol')} is not one of {protocols}")
    return plan


PLAN_DIGESTS = Path(__file__).with_name("binding_plans.json")


def content_digest(plan) -> str:
    """Digest of what a plan generates (items, trials, settings, patients), without the machine-specific paths
    of its base run, so a plan prepared on the GPU host can be checked against the reviewed one."""
    return F.digest({k: v for k, v in plan.items() if k not in ("digest", "base_run", "base_activations")})


def verify(root, expected: Path = PLAN_DIGESTS) -> None:
    """Refuse a prepared plan (binding or order follow-up) whose content differs from the committed one."""
    plan = read_plan(root, (PROTOCOL, F.PROTOCOL))
    want = json.loads(Path(expected).read_text()).get(Path(root).name)
    got = content_digest(plan)
    if want is None:
        raise ValueError(f"{root}: no reviewed plan named {Path(root).name} in {expected}")
    if want != got:
        raise ValueError(f"{root}: plan content {got} differs from the reviewed plan {want} ({expected})")
    print(f"{Path(root).name}: plan content verified ({got[:16]})")


def import_cache(root, sources) -> dict:
    """Copy successful answers (and activation captures) for this plan's new items from earlier runs (same key,
    prompt and settings), so no run regenerates what another produced. Works on binding plans and on the order
    follow-up's plan, whose items the full binding run generates under the same keys."""
    plan = read_plan(root, (PROTOCOL, F.PROTOCOL))
    counts = {}
    for model in plan["models"]:
        wanted = {v["key"]: v for v in plan["items"].values() if v["model"] == model and v["source"] == "new"}
        cache = G.Cache(model, C.CACHE)
        have = {k for k in wanted if (r := cache.get(k)) and r.get("ok")}
        copied = 0
        for source in map(Path, sources):
            for folder in (source / "data" / "generation_cache", source / "baseline_cache"):
                if not (folder / f"{model}.jsonl").exists():
                    continue
                batch = [r for k, r in G.Cache(model, folder).records.items()
                         if k in wanted and k not in have and r.get("ok")]
                for r in batch:
                    if r["prompt_sha"] != wanted[r["key"]]["prompt_sha"] or r["settings_sha"] != plan["settings"][model]:
                        raise ValueError(f"{folder}: {r['key']} has another prompt or settings; refusing to import")
                if batch:
                    cache.append(batch)
                    have |= {r["key"] for r in batch}
                    copied += len(batch)
        counts[model] = dict(imported=copied, new_items=len(wanted), still_pending=len(wanted) - len(have),
                             activations_imported=_import_activations(root, plan, model, sources))
    print(json.dumps(counts, indent=2))
    return counts


def _import_activations(root, plan, model, sources) -> int:
    """Copy earlier runs' captures of this plan's capture items (same key, prompt, settings; checksummed)."""
    wanted = {v["key"]: v for v in capture_items_of(plan, model)}
    cache = G.Cache(model, C.CACHE)
    dest_root = root / "data" / "activations" / model / "ckd"
    copied = 0
    for source in map(Path, sources):
        src_root = source / "data" / "activations" / model / "ckd"
        if not src_root.is_dir():
            continue
        for key, item in wanted.items():
            src, dest = src_root / key, dest_root / key
            if dest.exists() or not (src / "meta.json").exists() or not (src / "acts.npy").exists():
                continue
            meta = json.loads((src / "meta.json").read_text())
            if meta.get("prompt_sha") != item["prompt_sha"] or meta.get("settings_sha") != plan["settings"][model]:
                raise ValueError(f"{src}: capture of another prompt or settings; refusing to import")
            record = cache.get(key)
            if not record or meta.get("response_sha") != F.digest([record["text"], record["token_ids"]]):
                continue  # not a capture of this run's answer: capture makes its own
            if F.file_sha256(src / "acts.npy") != meta.get("array_sha256"):
                continue  # damaged: capture regenerates it
            tmp = dest.with_name(key + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(src, tmp)
            tmp.replace(dest)
            copied += 1
    return copied


@contextlib.contextmanager
def synthetic_backend(model):
    """The synthetic backend reads records from prompts (binding_synthetic), not dataset rows."""
    if C.model_config(model)["backend"] != "synthetic":
        yield
        return
    from second_order import binding_synthetic
    saved = G.BACKENDS["synthetic"]
    G.BACKENDS["synthetic"] = binding_synthetic.generate
    try:
        yield
    finally:
        G.BACKENDS["synthetic"] = saved


def generate(root, model, engine: dict | None = None):
    """Every pending new item, in the config's batches (one vLLM call per batch, so the engine keeps its full
    concurrency; an interrupted batch is regenerated identically on resume, since every item is seeded from its
    key). ``engine`` overrides vLLM engine settings, which change speed only."""
    plan = read_plan(root)
    if model not in plan["models"]:
        raise ValueError(f"model {model} is not in the prepared plan")
    if engine:
        C.model_config(model)["engine"] = (C.model_config(model).get("engine") or {}) | engine
        print(f"[{model}] engine: {C.model_config(model)['engine']}", flush=True)
        if G.settings_signature(model, "ckd") != plan["settings"][model]:
            raise ValueError("the engine override changed the settings signature")
    items = F.new_items(plan, model)
    with synthetic_backend(model):
        result = G.run(items, label=f"{model}/binding")
    F.atomic_json(root / f"generation_{model}.json", result)
    if G.pending(items):
        raise RuntimeError("generation incomplete; rerun to retry failed calls")


def capture(root, model):
    """Residual stream at the three positions of ``freeze`` for every capture item (``order_followup.capture``).
    Phase 3 captures its own prompt positions separately."""
    plan = read_plan(root)
    items = capture_items_of(plan, model)
    if items:
        F.capture(root, model, items=items)
    else:
        print(f"{model}: this profile captures no activations")


def export(root, model):
    plan = read_plan(root)
    rows = [trial | dict(status=result.status, value=result.value if result.status == "parsed" else np.nan,
                         text=(record or {}).get("text"), condition=item.condition)
            for trial, item, record, result in F.scored_trials(root, plan, model)]
    long = pd.DataFrame(rows)
    out = root / "outputs"
    out.mkdir(exist_ok=True)
    long.drop(columns=["text"]).to_parquet(out / f"{model}_responses.parquet", index=False)
    long[["item_key", "text"]].drop_duplicates("item_key").to_parquet(out / f"{model}_texts.parquet", index=False)
    long.groupby(["arm", "block_order", "role", "output", "status"]).size().rename("count").to_csv(
        out / f"{model}_retention.csv")
    print(f"{model}: exported {len(long)} trial rows; not generated: {(long.status == 'not_generated').sum()}")


def status(root):
    return F.status(root, read_plan(root), summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=["prepare", "status", "verify", "import-cache", "generate", "capture", "export"])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--profile", choices=list(PROFILES), default="pilot")
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--n-patients")
    parser.add_argument("--direct-samples", type=int)
    parser.add_argument("--base-run", type=Path)
    parser.add_argument("--base-activations", type=Path)
    parser.add_argument("--from", dest="sources", nargs="+", type=Path, default=[],
                        help="import-cache: earlier run directories whose answers this plan reuses")
    parser.add_argument("--model")
    parser.add_argument("--engine", type=json.loads, default=None,
                        help='generate: vLLM engine overrides, e.g. \'{"max_num_batched_tokens": 65536}\'')
    args = parser.parse_args()
    if args.base_run and not args.base_activations:
        parser.error("--base-run needs --base-activations")
    args.run = args.run.resolve()
    args.run.mkdir(parents=True, exist_ok=True)
    # Stages that change the plan or the cache take the run's lock; capture (its own atomic files), export and
    # the read-only stages do not, so a CPU export can run beside a GPU capture of the same run.
    exclusive = args.stage in ("prepare", "import-cache", "generate")
    with (args.run / ".binding.lock").open("a+b") as lock:
        if exclusive:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if args.stage == "prepare":
            prepare(args)
        elif args.stage == "status":
            status(args.run)
        elif args.stage == "verify":
            verify(args.run)
        elif args.stage == "import-cache":
            import_cache(args.run, args.sources)
        elif not args.model:
            parser.error("stage requires --model")
        elif args.stage == "generate":
            generate(args.run, args.model, args.engine)
        else:
            {"capture": capture, "export": export}[args.stage](args.run, args.model)


if __name__ == "__main__":
    main()
