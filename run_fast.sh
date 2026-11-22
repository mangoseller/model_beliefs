#!/usr/bin/env bash
# Speed-only settings for run_binding_experiment.sh (docs/binding_experiment.md, Speed); neither enters any settings
# signature, so they change throughput only:
# - CUDA graphs for decode batches up to 1024 (vLLM 0.8.5 records them only up to 512; max_num_seqs is 1024)
# - SO_FAST_SEEDED_SAMPLING: the bit-identical seeded sampler (second_order/run_generation.py)
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
CG="{\"compilation_config\": {\"cudagraph_capture_sizes\": [1, 2, 4, 8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 136, 144, 152, 160, 168, 176, 184, 192, 200, 208, 216, 224, 232, 240, 248, 256, 264, 272, 280, 288, 296, 304, 312, 320, 328, 336, 344, 352, 360, 368, 376, 384, 392, 400, 408, 416, 424, 432, 440, 448, 456, 464, 472, 480, 488, 496, 504, 512, 520, 528, 536, 544, 552, 560, 568, 576, 584, 592, 600, 608, 616, 624, 632, 640, 648, 656, 664, 672, 680, 688, 696, 704, 712, 720, 728, 736, 744, 752, 760, 768, 776, 784, 792, 800, 808, 816, 824, 832, 840, 848, 856, 864, 872, 880, 888, 896, 904, 912, 920, 928, 936, 944, 952, 960, 968, 976, 984, 992, 1000, 1008, 1016, 1024]}}"
export ENGINE_QWEN3_8B="$CG" ENGINE_QWEN3_14B="$CG" SO_FAST_SEEDED_SAMPLING=1
exec bash run_binding_experiment.sh "$@"
