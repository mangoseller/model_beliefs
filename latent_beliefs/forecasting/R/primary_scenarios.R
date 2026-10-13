#!/usr/bin/env Rscript
# Build the 469 primary-archive scenarios: within each study outcome, every
# condition is compared with one reference condition (the alphabetically
# first).  Condition materials are the authors' GPT-4 prompt transcripts with
# the synthetic participant profile removed.
#
# Usage: Rscript primary_scenarios.R <codeocean data dir> <output jsonl>

suppressPackageStartupMessages({
  library(dplyr)
  library(jsonlite)
  library(purrr)
  library(stringr)
  library(tibble)
})

args <- commandArgs(trailingOnly = TRUE)
data_dir <- args[[1]]
out_path <- args[[2]]

strip_profile <- function(prompt) {
  marker <- "> What is your partisan affiliation?"
  marker_pos <- regexpr(marker, prompt, fixed = TRUE)
  if (marker_pos < 0) return(NA_character_)
  after_marker <- substring(prompt, marker_pos + nchar(marker))
  experiment_pos <- regexpr("\n\nThe next page of the survey says:", after_marker, fixed = TRUE)
  if (experiment_pos < 0) return(NA_character_)
  transcript <- substring(after_marker, experiment_pos + 2L)
  str_trim(sub("\n\nYou choose: .[[:space:]]*$", "", transcript, perl = TRUE))
}

llm <- readRDS(file.path(data_dir, "llm_responses.RDS"))
rct <- readRDS(file.path(data_dir, "rct_responses.RDS"))

prompt_materials <- llm %>%
  filter(model == "gpt-4") %>%
  group_by(study, outcome.name, condition.name) %>%
  slice_head(n = 1) %>%
  ungroup() %>%
  transmute(
    study,
    outcome_name = outcome.name,
    condition_name = condition.name,
    transcript = map_chr(prompt, strip_profile),
    scale_flip
  ) %>%
  filter(!is.na(transcript))

make_scenarios <- function(study, outcome.name, data, ...) {
  target_study <- study
  target_outcome <- outcome.name
  conditions <- data %>%
    filter(!is.na(y), !is.na(condition.name)) %>%
    distinct(condition.name) %>%
    inner_join(
      prompt_materials %>%
        filter(.data$study == target_study, outcome_name == target_outcome),
      by = c("condition.name" = "condition_name")
    ) %>%
    arrange(condition.name)
  if (nrow(conditions) < 2) return(tibble())
  reference <- conditions[1, ]
  alternatives <- conditions[-1, ]
  if (any(alternatives$scale_flip != reference$scale_flip[[1]])) {
    stop("Scale orientation mismatch in ", target_study, " / ", target_outcome)
  }
  tibble(
    archive = "primary",
    study = target_study,
    study_outcome_group = paste(target_study, target_outcome, sep = "__"),
    outcome = target_outcome,
    condition_a = alternatives$condition.name,
    condition_b = reference$condition.name[[1]],
    condition_a_material = alternatives$transcript,
    condition_b_material = reference$transcript[[1]],
    material_status = "verbatim_transcripts",
    materials_ready = TRUE
  )
}

scenarios <- pmap_dfr(rct, make_scenarios) %>%
  arrange(study, outcome, condition_a) %>%
  mutate(scenario_id = sprintf("primary_%03d", row_number()), .before = 1)

if (nrow(scenarios) != 469L || n_distinct(scenarios$study_outcome_group) != 129L) {
  stop("Unexpected primary frame: ", nrow(scenarios), " scenarios across ",
       n_distinct(scenarios$study_outcome_group), " groups")
}

lines <- map_chr(seq_len(nrow(scenarios)), function(i) {
  toJSON(as.list(scenarios[i, ]), auto_unbox = TRUE, na = "null")
})
writeLines(lines, out_path, useBytes = TRUE)
cat("Wrote", nrow(scenarios), "primary scenarios\n")
