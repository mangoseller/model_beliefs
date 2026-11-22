#!/usr/bin/env bash
# run_binding_experiment.sh with throughput-only settings; neither enters any settings signature:
# - CUDA graphs for decode batches up to 1024 (vLLM 0.8.5 records them only up to 512; max_num_seqs is 1024)
# - SO_FAST_SEEDED_SAMPLING: the bit-identical seeded sampler (second_order/run_generation.py)
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
sizes="1, 2, 4$(printf ', %d' $(seq 8 8 1024))"  # 1, 2, 4, then every multiple of 8 up to 1024
CG="{\"compilation_config\": {\"cudagraph_capture_sizes\": [$sizes]}}"
export ENGINE_QWEN3_8B="$CG" ENGINE_QWEN3_14B="$CG" SO_FAST_SEEDED_SAMPLING=1
exec bash run_binding_experiment.sh "$@"
