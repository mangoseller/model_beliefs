#!/usr/bin/env Rscript
# Extract the released condition-level effect estimates of the Voelkel et al.
# (2024) Strengthening Democracy Challenge (dataset "SDC") from the authors'
# megastudy archive.  Effect IDs number the rows of the full archive, so the
# 50 SDC effects (25 treatments x 2 outcomes) are mega_001 to mega_050.
#
# Usage: Rscript megastudy_effects.R <megastudies.RDS> <output csv>

args <- commandArgs(trailingOnly = TRUE)
rds_path <- args[[1]]
out_path <- args[[2]]

clean <- function(x) {
  x <- as.character(x)
  x <- gsub("<U+200B>", "", x, fixed = TRUE)
  x <- gsub("<U+2013>", "-", x, fixed = TRUE)
  x <- gsub("<U+2014>", "-", x, fixed = TRUE)
  trimws(gsub("[[:space:]]+", " ", x))
}
pretty_label <- function(x) clean(gsub("[_-]+", " ", sub("_Treatment$", "", x)))

outcome_text <- function(outcome) {
  if (outcome == "PA") return("Partisan animosity; effect coded in the direction used by the source analysis")
  if (outcome == "ADA") return("Anti-democratic attitudes; effect coded in the direction used by the source analysis")
  stop("Unexpected SDC outcome ", outcome)
}

x <- readRDS(rds_path)
first_effect <- cumsum(c(0L, head(vapply(x$df, nrow, 1L), -1L))) + 1L
rows <- list()
for (i in which(as.character(x$dataset) == "SDC")) {
  d <- as.data.frame(x$df[[i]], stringsAsFactors = FALSE)
  outcome <- ifelse(is.na(x$outcome[i]), "", as.character(x$outcome[i]))
  issue <- ifelse(is.na(x$issue[i]), "", as.character(x$issue[i]))
  top_side <- ifelse(is.na(x$side[i]), "", as.character(x$side[i]))
  side <- if ("side" %in% names(d)) as.character(d$side) else rep(top_side, nrow(d))
  original <- clean(d$condition.name)
  title <- description <- pretty_label(original)
  rows[[length(rows) + 1L]] <- data.frame(
    effect_id = sprintf("mega_%03d", first_effect[i] + seq_len(nrow(d)) - 1L),
    study_key = "voelkel_2024", dataset = "SDC", outcome_key = outcome, issue = issue,
    advocated_side = side, condition_label_original = original,
    treatment_title = clean(title), treatment_description = clean(description),
    treatment_description_level = "full_protocol",
    outcome_description = outcome_text(outcome),
    effect_estimate = as.numeric(d$estimate.rct),
    prediction_gpt4_released = if ("prediction.gpt-4" %in% names(d)) as.numeric(d$`prediction.gpt-4`) else NA_real_,
    source_url = "https://osf.io/jzbnt/",
    local_material = "materials/megastudies_public/voelkel_2024/supplement.pdf",
    stringsAsFactors = FALSE
  )
}
effects <- do.call(rbind, rows)
if (nrow(effects) != 50L || effects$effect_id[[1]] != "mega_001") stop("Unexpected SDC effects")
write.csv(effects, out_path, row.names = FALSE, na = "")
message("Wrote ", nrow(effects), " SDC effects")
