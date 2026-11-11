#!/usr/bin/env bash
# Local GPU queue (Apple silicon, MLX), run after the withheld set is fixed.
# Each step is resumable; rerunning skips completed items.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=/Users/sam/miniforge3/envs/latent-beliefs/bin/python
export PYTHONPATH=build/so_dependencies:build/pilot_dependencies:.
LOG=build/second_order_logs
step() { echo "=== $(date '+%H:%M:%S') $*" ; }

step "smoke generation (gemma3_4b_mlx, all six conditions)"
$PY -u -m second_order.run_generation --model gemma3_4b_mlx --smoke --approve > $LOG/smoke_generation.log 2>&1
step "smoke phase 3"
$PY -u -m second_order.phase3 --model gemma3_4b_mlx --smoke > $LOG/smoke_phase3.log 2>&1
step "activations: smoke model, all conditions with responses"
$PY -u -m second_order.activations extract --model gemma3_4b_mlx --act-model gemma3_4b_mlx \
    --conditions A B C_john D_john C_user D_user --cases smoke > $LOG/act_smoke.log 2>&1
$PY -u -m second_order.activations pack --model gemma3_4b_mlx >> $LOG/act_smoke.log 2>&1
step "activations: hosted gemma3_4b responses (A with P3; B/C/D prompt-only)"
$PY -u -m second_order.activations extract --model gemma3_4b --act-model gemma3_4b_mlx \
    --conditions A B C_john D_john C_user D_user --cases all > $LOG/act_gemma3_4b.log 2>&1
$PY -u -m second_order.activations pack --model gemma3_4b >> $LOG/act_gemma3_4b.log 2>&1
step "done"
