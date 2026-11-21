#!/usr/bin/env Rscript
# Ground truth for every scenario: the realized A-minus-B difference in mean
# outcome and its sign (gold_positive).  Primary scenarios use the released
# participant-level RCT responses; megastudy scenarios use the released
# condition-level effect estimates.
#
# The source study showed its LLM a reversed response scale for outcomes whose
# original scale ran in an unintuitive direction (scale_flip in
# llm_responses.RDS), and the condition transcripts in our prompts are those
# reversed-scale transcripts.  Primary-scenario differences are therefore
# oriented to the response scale shown in the prompt.
#
# Usage: Rscript gold.R <scenarios.jsonl> <rct_responses.RDS> <megastudy effects csv> <llm_responses.RDS> <output csv>

suppressPackageStartupMessages({
  library(dplyr)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
inventory <- stream_in(file(args[[1]]), verbose = FALSE)
rct <- readRDS(args[[2]])
effects <- read.csv(args[[3]], stringsAsFactors = FALSE, check.names = FALSE)
scale_flips <- readRDS(args[[4]]) %>% distinct(study, outcome = outcome.name, scale_flip)
out_path <- args[[5]]

primary_gold <- inventory %>%
  filter(archive == "primary") %>%
  rowwise() %>%
  do({
    scenario <- .
    matches <- which(rct$study == scenario$study & rct$outcome.name == scenario$outcome)
    if (length(matches) != 1L) {
      stop("Expected one RCT row for ", scenario$scenario_id, "; found ", length(matches))
    }
    dat <- as.data.frame(rct$data[[matches]])
    a <- dat$y[dat$condition.name == scenario$condition_a & !is.na(dat$y)]
    b <- dat$y[dat$condition.name == scenario$condition_b & !is.na(dat$y)]
    if (!length(a) || !length(b)) stop("Missing condition data for ", scenario$scenario_id)
    data.frame(
      scenario_id = scenario$scenario_id,
      gold_source = "Primary-archive RCT participant responses",
      gold_mean_a = mean(a),
      gold_mean_b = mean(b),
      gold_mean_difference = mean(a) - mean(b),
      gold_n_a = length(a),
      gold_n_b = length(b),
      source_effect_id_a = NA_character_,
      source_effect_id_b = NA_character_
    )
  }) %>%
  ungroup()

effect_lookup <- setNames(effects$effect_estimate, effects$effect_id)
mega <- inventory %>% filter(archive == "megastudy")
mega_a <- unname(effect_lookup[mega$source_effect_id_a])
mega_b <- unname(effect_lookup[mega$source_effect_id_b])
if (anyNA(mega_a) || anyNA(mega_b)) stop("Missing megastudy effect estimates")
mega_gold <- data.frame(
  scenario_id = mega$scenario_id,
  gold_source = "Released megastudy effect estimates",
  gold_mean_a = mega_a,
  gold_mean_b = mega_b,
  gold_mean_difference = mega_a - mega_b,
  gold_n_a = NA_integer_,
  gold_n_b = NA_integer_,
  source_effect_id_a = mega$source_effect_id_a,
  source_effect_id_b = mega$source_effect_id_b,
  stringsAsFactors = FALSE
)

flips <- inventory %>%
  filter(archive == "primary") %>%
  select(scenario_id, study, outcome) %>%
  left_join(scale_flips, by = c("study", "outcome"), relationship = "many-to-one")
if (anyNA(flips$scale_flip)) stop("Missing scale_flip for a primary scenario")
primary_gold <- primary_gold %>%
  left_join(flips %>% select(scenario_id, scale_flip), by = "scenario_id") %>%
  mutate(gold_mean_difference = ifelse(scale_flip, -gold_mean_difference, gold_mean_difference))
mega_gold$scale_flip <- FALSE

gold <- bind_rows(primary_gold, mega_gold) %>%
  mutate(gold_positive = as.integer(gold_mean_difference > 0)) %>%
  arrange(scenario_id)
if (nrow(gold) != nrow(inventory) || n_distinct(gold$scenario_id) != nrow(inventory)) {
  stop("Gold table does not match scenario inventory")
}
write.csv(gold, out_path, row.names = FALSE, na = "")
cat("Wrote", nrow(gold), "scenario gold rows\n")
