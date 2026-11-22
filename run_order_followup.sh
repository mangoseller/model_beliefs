#!/usr/bin/env bash
# The chart-order follow-up on a prepared plan (second_order.order_followup prepare):
#
#   bash run_order_followup.sh [run_dir]     # default runs/20261121-101311-order-followup
#
# Generation, activation capture and export for Qwen3 8B and 14B; each GPU stage runs in a fresh process,
# so VRAM is released between stages. PYTHON selects the interpreter (default python).
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
run="${1:-runs/20261121-101311-order-followup}"
python="${PYTHON:-python}"
mkdir -p "$run/logs"
"$python" -m second_order.order_followup status --run "$run"
for model in qwen3_8b qwen3_14b; do
    for stage in generate capture export; do
        "$python" -u -m second_order.order_followup "$stage" --run "$run" --model "$model" \
            2>&1 | tee -a "$run/logs/${model}_${stage}.log"
    done
done
"$python" -m second_order.order_followup status --run "$run"
