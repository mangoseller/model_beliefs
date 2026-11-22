#!/usr/bin/env bash
# Uses an already prepared plan. Each GPU stage runs in a fresh process to release VRAM.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
followup_run="${1:-runs/20261121-101311-order-followup}"
followup_python="${PYTHON:-python}"
mkdir -p "$followup_run/logs"
"$followup_python" -m second_order.order_followup status --run "$followup_run"
for followup_model in qwen3_8b qwen3_14b; do
    for followup_stage in generate capture export; do
        "$followup_python" -u -m second_order.order_followup "$followup_stage" \
            --run "$followup_run" --model "$followup_model" \
            2>&1 | tee -a "$followup_run/logs/${followup_model}_${followup_stage}.log"
    done
done
"$followup_python" -m second_order.order_followup status --run "$followup_run"
