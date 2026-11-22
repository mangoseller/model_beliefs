#!/usr/bin/env bash
# The binding experiment on one GPU, in one command, after `git pull` on the instance that ran 20261121-101311:
#
#   nohup bash run_binding_experiment.sh > binding.out 2>&1 &     # everything, in priority order
#   bash run_binding_experiment.sh part3a part3d                  # or any list of parts (setup always runs)
#
# Setup (idempotent): the pinned environment (as run_experiment.sh: uv, .venv-experiment,
# requirements/lock-cuda.txt); then every plan is prepared from the archived run and refused unless its content
# matches the reviewed plan (second_order/binding_plans.json). Parts, in order:
#
#   part1   binding pilot: Qwen3 8B, 30 fit-half patients, every arm, stated probability x10
#   part3a  full binding run: Qwen3 8B and 14B, 150 patients, every arm (imports part 1; generates the
#           order follow-up's items too, under its exact keys)
#   part2   Astra's chart-order follow-up (both models, latent battery), plus its analysis: imports its
#           answers and activation captures from part 3a, so it needs almost no GPU time after 3a
#   part3b  thinking mode, Qwen3 8B and 14B, the six deciding arms
#   part3d  causal tests (HF transformers) on part 3a, after its analysis
#   part3c  Qwen3 32B, every arm, stated probability
#
# Each part produces its own build in $RESULTS (partN_<run>.tar.gz, plus partN_<run>_activations.tar where it
# captured any, each with a .sha256), to download while later parts run. Every stage resumes from its cache.
# The GPU only ever runs one thing. Within a part: generation, then (in the background) export, latent refits,
# analysis and the main build while the GPU captures activations, whose build follows separately; nothing
# downstream needs the captures. CPU stages overlap the next part's generation; part3d waits for part3a's
# analysis, and the script waits for every background stage before it exits.
#
# Environment: BASE_RUN (the archived run: its directory or tarball; default runs/20261121-101311, else
# runs/20261121-101311_reanalysed.tar.gz), BASE_ACTIVATIONS (its activation index: the run directory, or
# runs/20261121-101311_activations_index.tar or _activations.tar; found automatically),
# PYTHON (skip the environment setup and use this interpreter), VENV (default .venv-experiment), RESULTS
# (default runs/results), RUNS (default runs), ANALYSIS_THREADS (CPU threads for background analysis; default
# half the cores), ENGINE_<MODEL> (vLLM engine overrides as JSON for one model's generation, e.g.
# ENGINE_QWEN3_8B; speed only).
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
RUNS="${RUNS:-runs}"
RESULTS="${RESULTS:-$RUNS/results}"
THREADS="${ANALYSIS_THREADS:-$(( $(nproc) / 2 > 1 ? $(nproc) / 2 : 1 ))}"
mkdir -p "$RESULTS" "$RUNS/logs"
: > "$RUNS/logs/background.list"

if [[ -n "${PYTHON:-}" ]]; then
    PY="$PYTHON"
else
    VENV="${VENV:-.venv-experiment}"
    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    fi
    [[ -x "$VENV/bin/python" ]] || uv venv --python 3.11 "$VENV"
    uv pip install --python "$VENV/bin/python" -r requirements/lock-cuda.txt
    PY="$VENV/bin/python"
fi
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" TOKENIZERS_PARALLELISM=false HF_HUB_ENABLE_HF_TRANSFER=1

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$RUNS/logs/parts.log"; }
step() { local name="$1"; shift; log "start $name"; "$@" >> "$RUNS/logs/$name.log" 2>&1; log "done $name"; }
cpu() {  # CPU-only work, thread-limited so it can overlap GPU generation
    OMP_NUM_THREADS="$THREADS" MKL_NUM_THREADS="$THREADS" OPENBLAS_NUM_THREADS="$THREADS" CUDA_VISIBLE_DEVICES="" "$@"
}
background() {  # part, then a function to run in the background; its exit status lands in a file, so a part can
    # be awaited from any shell (parts run in subshells, so one failing part cannot stop the others)
    local part="$1"; shift
    rm -f "$RUNS/logs/background_$part.status"
    ( set +e; ( set -e; "$@" ); echo $? > "$RUNS/logs/background_$part.status" ) &
    echo "$part" >> "$RUNS/logs/background.list"
    log "$part: CPU stages running in the background (pid $!)"
}
await() {  # part: wait for its background stages (if it started any); fail if they failed
    grep -qxF "$1" "$RUNS/logs/background.list" 2>/dev/null || return 0
    while [[ ! -f "$RUNS/logs/background_$1.status" ]]; do sleep 20; done
    if [[ "$(cat "$RUNS/logs/background_$1.status")" == 0 ]]; then log "$1: background stages finished"
    else log "$1: background stages FAILED (see its logs)"; return 1; fi
}

generate() {  # run_dir model...  (the GPU stage of session 1)
    local run="$1"; shift
    local name; name="$(basename "$run")"
    step "${name}_status" "$PY" -m second_order.binding status --run "$run"
    for model in "$@"; do
        local engine_var="ENGINE_${model^^}"   # e.g. ENGINE_QWEN3_8B='{"max_num_batched_tokens": 65536}'
        step "${name}_${model}_generate" "$PY" -u -m second_order.binding generate --run "$run" --model "$model" \
            ${!engine_var:+--engine "${!engine_var}"}
    done
}
analyse() {  # part run_dir model...  (CPU: export, analysis, the part's main build)
    local part="$1" run="$2"; shift 2
    local name; name="$(basename "$run")"
    for model in "$@"; do
        step "${name}_${model}_export" cpu "$PY" -u -m second_order.binding export --run "$run" --model "$model"
        step "${name}_${model}_analysis" cpu "$PY" -u -m second_order.binding_analysis --run "$run" --model "$model"
    done
    step "${name}_package" cpu "$PY" -m second_order.package_run --run "$run" --out "$RESULTS" --name "${part}_${name}" --what main
}
capture() {  # part run_dir model...  (GPU: activation captures; then, in the background, the activations build)
    local part="$1" run="$2"; shift 2
    local name; name="$(basename "$run")"
    for model in "$@"; do
        step "${name}_${model}_capture" "$PY" -u -m second_order.binding capture --run "$run" --model "$model"
    done
    background "${part}_activations" step "${name}_package_activations" cpu "$PY" -m second_order.package_run \
        --run "$run" --out "$RESULTS" --name "${part}_${name}" --what activations
}
session1() {  # part run_dir model...: results first, captures after (nothing downstream needs them)
    local part="$1" run="$2"; shift 2
    generate "$run" "$@"
    background "$part" analyse "$part" "$run" "$@"
    capture "$part" "$run" "$@"
}

base_inputs() {  # sets BASE_RUN and BASE_ACTIVATIONS, or explains what is missing
    local run="${BASE_RUN:-}"
    if [[ -z "$run" ]]; then
        for candidate in "$RUNS/20261121-101311" "$RUNS/20261121-101311_reanalysed.tar.gz" "$RUNS/20261121-101311.tar.gz"; do
            [[ -e "$candidate" ]] && { run="$candidate"; break; }
        done
    fi
    [[ -e "$run" ]] || { log "no archived run 20261121-101311: set BASE_RUN to its directory or tarball"; return 1; }
    local acts="${BASE_ACTIVATIONS:-}"
    if [[ -z "$acts" ]]; then
        # Only each model's index.parquet and meta.json are read, so the 1 MB _activations_index.tar will do.
        if [[ -d "$run" && -f "$run/data/activations/qwen3_8b/ckd/index.parquet" ]]; then acts="$run"
        else
            for candidate in "$RUNS/20261121-101311_activations_index.tar" "$RUNS/20261121-101311_activations.tar"; do
                [[ -f "$candidate" ]] && { acts="$candidate"; break; }
            done
        fi
    fi
    [[ -e "$acts" ]] || { log "no activations of 20261121-101311: set BASE_ACTIVATIONS (directory or _activations.tar)"; return 1; }
    BASE_RUN="$run" BASE_ACTIVATIONS="$acts"
}

setup() {  # prepare every plan (idempotent) and check it against the reviewed plan
    base_inputs
    log "base run: $BASE_RUN; activations: $BASE_ACTIVATIONS"
    for profile in pilot full thinking 32b; do
        local run="$RUNS/binding-$profile"
        "$PY" -m second_order.binding verify --run "$run" >/dev/null 2>&1 || step "prepare_$profile" \
            "$PY" -m second_order.binding prepare --run "$run" --profile "$profile" \
            --base-run "$BASE_RUN" --base-activations "$BASE_ACTIVATIONS"
        step "verify_$profile" "$PY" -m second_order.binding verify --run "$run"
    done
    local run="$RUNS/20261121-101311-order-followup"
    "$PY" -m second_order.binding verify --run "$run" >/dev/null 2>&1 || step prepare_order_followup \
        "$PY" -m second_order.order_followup prepare --run "$run" --base-run "$BASE_RUN" \
        --base-activations "$BASE_ACTIVATIONS"
    step verify_order_followup "$PY" -m second_order.binding verify --run "$run"
}

part1() { session1 part1 "$RUNS/binding-pilot" qwen3_8b; }

part2() {
    local run="$RUNS/20261121-101311-order-followup" sources=()
    for prior in "$RUNS/binding-full" "$RUNS/binding-pilot"; do
        [[ -d "$prior" ]] && sources+=("$prior")
    done
    if ((${#sources[@]})); then
        step order_followup_import cpu "$PY" -m second_order.binding import-cache --run "$run" --from "${sources[@]}"
    fi
    step order_followup_status "$PY" -m second_order.order_followup status --run "$run"
    for model in qwen3_8b qwen3_14b; do
        step "order_followup_${model}_generate" "$PY" -u -m second_order.order_followup generate --run "$run" --model "$model"
        step "order_followup_${model}_capture" "$PY" -u -m second_order.order_followup capture --run "$run" --model "$model"
    done
    part2_cpu() {
        for model in qwen3_8b qwen3_14b; do
            step "order_followup_${model}_export" cpu "$PY" -u -m second_order.order_followup export --run "$run" --model "$model"
            step "order_followup_${model}_analysis" cpu "$PY" -u -m second_order.binding_analysis --run "$run" --model "$model" --order-followup
        done
        step order_followup_package cpu "$PY" -m second_order.package_run --run "$run" --out "$RESULTS" --name "part2_$(basename "$run")"
    }
    background part2 part2_cpu
}

part3a() {
    local run="$RUNS/binding-full" sources=()
    for prior in "$RUNS/binding-pilot" "$RUNS/20261121-101311-order-followup"; do
        [[ -d "$prior" ]] && sources+=("$prior")
    done
    if ((${#sources[@]})); then
        step binding-full_import cpu "$PY" -m second_order.binding import-cache --run "$run" --from "${sources[@]}"
    fi
    session1 part3a "$run" qwen3_8b qwen3_14b
}

part3b() { session1 part3b "$RUNS/binding-thinking" qwen3_8b_think qwen3_14b_think; }

part3c() { session1 part3c "$RUNS/binding-32b" qwen3_32b; }

part3d() {
    local run="$RUNS/binding-causal" source="$RUNS/binding-full"
    # needs part 3a's beliefs and lambda tables; without them, skip this part and go on with the rest
    if ! await part3a; then log "part3d skipped: part3a's analysis failed (see its logs)"; return 0; fi
    for model in qwen3_8b qwen3_14b; do
        step "causal_${model}_benchmark" "$PY" -u -m second_order.binding_causal benchmark --run "$run" --source "$source" --model "$model"
        step "causal_${model}" "$PY" -u -m second_order.binding_causal all --run "$run" --source "$source" --model "$model"
    done
    step causal_package cpu "$PY" -m second_order.package_run --run "$run" --out "$RESULTS" --name "part3d_$(basename "$run")"
}

parts=("$@")
[[ ${#parts[@]} -eq 0 || "${parts[0]}" == "all" ]] && parts=(part1 part3a part2 part3b part3d part3c)
setup  # a failed setup (no base run, a plan that differs from the reviewed one) stops everything
failed=0
for part in "${parts[@]}"; do
    log "=== $part ==="
    # Not an `if` condition: bash ignores set -e inside a subshell tested by `if`.
    set +e; ( set -e; "$part" ); status=$?; set -e
    if [[ $status == 0 ]]; then log "=== $part: GPU stages finished ==="
    else log "=== $part FAILED (see its logs); continuing with the next part ==="; failed=1; fi
done
while read -r part; do await "$part" || failed=1; done < <(sort -u "$RUNS/logs/background.list")
log "all requested parts finished; builds in $RESULTS"
exit "$failed"
