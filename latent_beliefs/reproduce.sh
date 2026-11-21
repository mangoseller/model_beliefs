#!/usr/bin/env bash
# Run the replication pipeline, stage by stage, from the repository root.
#
#   latent_beliefs/reproduce.sh analysis          # released responses + fits -> every figure and table
#   latent_beliefs/reproduce.sh sources prompts   # public datasets -> the exact prompts sent to the models
#   latent_beliefs/reproduce.sh refit             # refit every latent model (GPU recommended), then
#                                    # LB_FITS_DIR=build/fits latent_beliefs/reproduce.sh evaluate figures
#
# Stages: sources prompts unpack parse datasets refit evaluate figures
#         analysis (= unpack parse datasets evaluate figures)
# LLM collection is not scripted here because it calls paid APIs; see latent_beliefs/README.md.
# Runs inside the environment built from latent_beliefs/environment.yml.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
WORKERS="${WORKERS:-4}"

run() { echo "+ python -m latent_beliefs.$*"; "$PY" -m "latent_beliefs.$@"; }

stage_sources() {
  run ckd.download
  run ckd.nhanes
  run ckd.risk
  run ckd.cases
  run werewolf.download
  run werewolf.cases
  run forecasting.download
  run forecasting.sources
}

stage_prompts() {
  run ckd.prompts
  run werewolf.prompts
  run forecasting.prompts
}

stage_unpack() {
  run release verify
  run release unpack
}

stage_parse() {
  run ckd.parse
  run werewolf.parse
  run forecasting.parse
  run branching.parse
}

stage_datasets() {
  run datasets
  run tables.retention
}

stage_refit() {
  run latent.fit
  run latent.fit --response-curve monotone_spline
  run latent.two_d
  run holdout.domain
  run holdout.joint
  run holdout.framing
  run holdout.report_domain
}

stage_evaluate() {
  run evaluation.leave_one_out --fit main
  run evaluation.leave_one_out --fit spline
  run evaluation.reciprocal
  run evaluation.probability_sufficiency
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 run evaluation.outcome_sufficiency --workers "$WORKERS"
  run holdout.grid --evidence eight-decision
  run holdout.grid --evidence reports
  run branching.icc
  run branching.prediction
}

stage_figures() {
  run figures
}

[ $# -gt 0 ] || { sed -n '2,13p' "$0"; exit 1; }
for stage in "$@"; do
  case "$stage" in
    analysis) stage_unpack; stage_parse; stage_datasets; stage_evaluate; stage_figures ;;
    sources|prompts|unpack|parse|datasets|refit|evaluate|figures) "stage_$stage" ;;
    *) echo "unknown stage: $stage" >&2; exit 1 ;;
  esac
done
