# Second-order belief experiment: results

Generated 2026-11-11 by `python -m second_order.analyze`; figures by `python -m second_order.figures`.  Numbers only; no interpretation.

## Settings used (decisions for Sam; defaults unless changed)

- Withheld set: S4 = ridageyr, diabetes_self_reported, hypertension_self_reported
- C/D prompt layout: plan
- Agent framings: John (third person) and user (first person)
- Capability floor: 0.65
- Bootstrap: paired over cases, 2000 resamples (layer sweeps: 500), 95% percentile
- Models analysed here: Gemma 3 4B (local 4-bit)
- Second domain: none

## Retention (figure 4)

| model | condition | cases_generated | cases_complete | retention | parse_failures | call_errors | refusal_like_responses |
|---|---|---|---|---|---|---|---|
| gemma3_4b_mlx | A | 24 | 24 | 1.000 | 0 | 0 | 7 |
| gemma3_4b_mlx | B | 24 | 24 | 1.000 | 0 | 0 | 3 |
| gemma3_4b_mlx | C_john | 24 | 24 | 1.000 | 0 | 0 | 6 |
| gemma3_4b_mlx | C_user | 24 | 24 | 1.000 | 0 | 0 | 0 |
| gemma3_4b_mlx | D_john | 24 | 24 | 1.000 | 0 | 0 | 3 |
| gemma3_4b_mlx | D_user | 24 | 24 | 1.000 | 0 | 0 | 1 |

## Coherence (figure 1)

Mean c-index on the test split; reports -> decisions unless stated.  A_minus_D and D_minus_C are paired contrasts.

| model | method | framing | condition | direction | estimate | ci_lower | ci_upper | n_test_cases | variant |
|---|---|---|---|---|---|---|---|---|---|
| gemma3_4b_mlx | direct | self | A | reports_to_decisions | 0.406 | 0.000 | 0.500 | 4 | direct |
| gemma3_4b_mlx | direct | self | B | reports_to_decisions | 0.500 | 0.333 | 1.000 | 4 | direct |
| gemma3_4b_mlx | direct | john | C_john | reports_to_decisions | 0.500 | 0.000 | 1.000 | 4 | direct |
| gemma3_4b_mlx | direct | user | C_user | reports_to_decisions | 0.500 | 0.000 | 1.000 | 4 | direct |
| gemma3_4b_mlx | direct | john | D_john | reports_to_decisions | 0.625 | 0.375 | 1.000 | 4 | direct |
| gemma3_4b_mlx | direct | user | D_user | reports_to_decisions | 0.583 | 0.000 | 1.000 | 4 | direct |
| gemma3_4b_mlx | direct | john | A_minus_D | reports_to_decisions | -0.219 | -0.667 | 0.125 | 4 | direct |
| gemma3_4b_mlx | direct | john | D_minus_C | reports_to_decisions | 0.125 | -0.250 | 1.000 | 4 | direct |
| gemma3_4b_mlx | direct | user | A_minus_D | reports_to_decisions | -0.177 | -0.667 | 0.500 | 4 | direct |
| gemma3_4b_mlx | direct | user | D_minus_C | reports_to_decisions | 0.083 | -0.500 | 0.500 | 4 | direct |
| gemma3_4b_mlx | latent | self | A | reports_to_decisions | 0.385 | 0.000 | 0.500 | 4 | nan |
| gemma3_4b_mlx | latent | self | A | decisions_to_reports | 0.433 | 0.000 | 0.500 | 4 | nan |
| gemma3_4b_mlx | latent | self | B | reports_to_decisions | 0.750 | 0.667 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | self | B | decisions_to_reports | 0.750 | 0.500 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | C_john | reports_to_decisions | 0.500 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | C_john | decisions_to_reports | 0.500 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | user | C_user | reports_to_decisions | 0.333 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | user | C_user | decisions_to_reports | 0.200 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | D_john | reports_to_decisions | 0.692 | 0.483 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | D_john | decisions_to_reports | 0.600 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | user | D_user | reports_to_decisions | 0.583 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | user | D_user | decisions_to_reports | 0.400 | 0.000 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | A_minus_D | decisions_to_reports | -0.167 | -1.000 | 0.500 | 4 | nan |
| gemma3_4b_mlx | latent | john | D_minus_C | decisions_to_reports | 0.100 | -0.500 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | A_minus_D | reports_to_decisions | -0.306 | -0.667 | 0.000 | 4 | nan |
| gemma3_4b_mlx | latent | john | D_minus_C | reports_to_decisions | 0.192 | -0.125 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | user | A_minus_D | decisions_to_reports | 0.033 | -0.500 | 0.500 | 4 | nan |
| gemma3_4b_mlx | latent | user | D_minus_C | decisions_to_reports | 0.200 | -0.333 | 1.000 | 4 | nan |
| gemma3_4b_mlx | latent | user | A_minus_D | reports_to_decisions | -0.198 | -0.667 | 0.500 | 4 | nan |
| gemma3_4b_mlx | latent | user | D_minus_C | reports_to_decisions | 0.250 | -0.500 | 0.667 | 4 | nan |

## Leakage (figures 2, 3, 5, 6)

Primary case set first; `test_only` and `pilot_excluded` are secondary; `separate` is the per-condition-fit robustness check.

| model | method | case_set | fit | statistic | estimate | ci_lower | ci_upper | n_cases |
|---|---|---|---|---|---|---|---|---|
| gemma3_4b_mlx | direct | all_complete | none | manipulation_median_abs_A_minus_B | 1.025 | 0.648 | 1.253 | 24 |
| gemma3_4b_mlx | direct | all_complete | none | C_john_two_weight_wA | 0.417 | 0.046 | 0.824 | 24 |
| gemma3_4b_mlx | direct | all_complete | none | C_john_two_weight_wB | 0.235 | -0.525 | 1.036 | 24 |
| gemma3_4b_mlx | direct | all_complete | none | C_user_two_weight_wA | 0.434 | 0.106 | 0.844 | 24 |
| gemma3_4b_mlx | direct | all_complete | none | C_user_two_weight_wB | 0.614 | -0.002 | 1.261 | 24 |
| gemma3_4b_mlx | direct | all_complete | none | mean_shift_C_user_minus_john | 0.251 | -0.151 | 0.607 | 24 |
| gemma3_4b_mlx | direct | pilot_excluded | none | manipulation_median_abs_A_minus_B | 1.025 | 0.648 | 1.253 | 24 |
| gemma3_4b_mlx | direct | pilot_excluded | none | C_john_two_weight_wA | 0.417 | 0.046 | 0.824 | 24 |
| gemma3_4b_mlx | direct | pilot_excluded | none | C_john_two_weight_wB | 0.235 | -0.525 | 1.036 | 24 |
| gemma3_4b_mlx | direct | pilot_excluded | none | C_user_two_weight_wA | 0.434 | 0.106 | 0.844 | 24 |
| gemma3_4b_mlx | direct | pilot_excluded | none | C_user_two_weight_wB | 0.614 | -0.002 | 1.261 | 24 |
| gemma3_4b_mlx | direct | pilot_excluded | none | mean_shift_C_user_minus_john | 0.251 | -0.151 | 0.607 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | manipulation_median_abs_A_minus_B | 0.510 | 0.368 | 0.633 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | reliability | 0.504 | -0.023 | 0.650 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | lambda_0 | -0.306 | -0.656 | 0.183 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | c_C_john | -0.177 | -0.455 | 0.123 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | lambda_C_john | 0.205 | -0.066 | 0.518 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | lambda_D_john | 0.926 | 0.533 | 1.319 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | normalised_lambda_john | 0.221 | -0.108 | 0.444 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | C_john_two_weight_wA | 0.251 | 0.030 | 0.471 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | C_john_two_weight_wB | 0.534 | -0.089 | 1.107 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | closer_to_B_share_john | 0.667 | 0.333 | 0.909 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | c_C_user | 0.285 | 0.025 | 0.555 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | lambda_C_user | 0.689 | 0.350 | 1.034 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | lambda_D_user | 0.550 | 0.116 | 0.968 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | normalised_lambda_user | 1.254 | 0.909 | 3.404 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | C_user_two_weight_wA | 0.652 | 0.441 | 0.895 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | C_user_two_weight_wB | 0.387 | -0.020 | 0.863 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | closer_to_B_share_user | 0.500 | 0.250 | 0.833 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | mean_shift_C_user_minus_john | 0.204 | 0.032 | 0.400 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | diff_user_minus_john_lambda_C | 0.484 | 0.250 | 0.685 | 24 |
| gemma3_4b_mlx | latent | all_complete | pooled | diff_user_minus_john_c_C | 0.462 | 0.223 | 0.682 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | manipulation_median_abs_A_minus_B | 0.523 | 0.377 | 0.689 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | reliability | 0.640 | 0.324 | 0.751 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | lambda_0 | -0.185 | -0.442 | 0.215 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | c_C_john | 0.202 | -0.130 | 0.556 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | lambda_C_john | 0.327 | -0.037 | 0.685 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | lambda_D_john | 0.821 | 0.491 | 1.173 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | normalised_lambda_john | 0.398 | -0.068 | 0.681 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | C_john_two_weight_wA | 0.280 | 0.020 | 0.494 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | C_john_two_weight_wB | 0.767 | 0.095 | 1.621 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | closer_to_B_share_john | 0.917 | 0.750 | 1.000 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | c_C_user | 0.354 | 0.165 | 0.570 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | lambda_C_user | 0.633 | 0.409 | 0.873 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | lambda_D_user | 0.562 | 0.219 | 1.013 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | normalised_lambda_user | 1.127 | 0.725 | 2.411 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | C_user_two_weight_wA | 0.536 | 0.340 | 0.725 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | C_user_two_weight_wB | 0.983 | 0.317 | 1.665 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | closer_to_B_share_user | 0.583 | 0.250 | 0.833 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | mean_shift_C_user_minus_john | -0.016 | -0.178 | 0.168 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | diff_user_minus_john_lambda_C | 0.307 | 0.073 | 0.584 | 24 |
| gemma3_4b_mlx | latent | all_complete | separate | diff_user_minus_john_c_C | 0.151 | -0.101 | 0.394 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | manipulation_median_abs_A_minus_B | 0.510 | 0.368 | 0.633 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | reliability | 0.504 | -0.023 | 0.650 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | lambda_0 | -0.306 | -0.656 | 0.183 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | c_C_john | -0.177 | -0.455 | 0.123 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | lambda_C_john | 0.205 | -0.066 | 0.518 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | lambda_D_john | 0.926 | 0.533 | 1.319 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | normalised_lambda_john | 0.221 | -0.108 | 0.444 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | C_john_two_weight_wA | 0.251 | 0.030 | 0.471 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | C_john_two_weight_wB | 0.534 | -0.089 | 1.107 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | closer_to_B_share_john | 0.667 | 0.333 | 0.909 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | c_C_user | 0.285 | 0.025 | 0.555 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | lambda_C_user | 0.689 | 0.350 | 1.034 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | lambda_D_user | 0.550 | 0.116 | 0.968 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | normalised_lambda_user | 1.254 | 0.909 | 3.404 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | C_user_two_weight_wA | 0.652 | 0.441 | 0.895 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | C_user_two_weight_wB | 0.387 | -0.020 | 0.863 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | closer_to_B_share_user | 0.500 | 0.250 | 0.833 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | mean_shift_C_user_minus_john | 0.204 | 0.032 | 0.400 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | diff_user_minus_john_lambda_C | 0.484 | 0.250 | 0.685 | 24 |
| gemma3_4b_mlx | latent | pilot_excluded | pooled | diff_user_minus_john_c_C | 0.462 | 0.223 | 0.682 | 24 |

## Method agreement

| model | analysis | method | condition | estimate | n_cases | detail |
|---|---|---|---|---|---|---|
| gemma3_4b_mlx | spearman_between_methods | direct~latent | A | 0.963 | 24.000 | nan |
| gemma3_4b_mlx | spearman_between_methods | direct~latent | B | 0.875 | 24.000 | nan |
| gemma3_4b_mlx | spearman_between_methods | direct~latent | C_john | 0.760 | 24.000 | nan |
| gemma3_4b_mlx | spearman_between_methods | direct~latent | D_john | 0.905 | 24.000 | nan |
| gemma3_4b_mlx | spearman_between_methods | direct~latent | C_user | 0.866 | 24.000 | nan |
| gemma3_4b_mlx | spearman_between_methods | direct~latent | D_user | 0.916 | 24.000 | nan |
| gemma3_4b_mlx | sign_agreement | direct|latent | A_minus_D_john | 1.000 |  | direct:-1; latent:-1 |
| gemma3_4b_mlx | sign_agreement | direct|latent | D_minus_C_john | 1.000 |  | direct:1; latent:1 |
| gemma3_4b_mlx | sign_agreement | direct|latent | A_minus_D_user | 1.000 |  | direct:-1; latent:-1 |
| gemma3_4b_mlx | sign_agreement | direct|latent | D_minus_C_user | 1.000 |  | direct:1; latent:1 |
| gemma3_4b_mlx | sign_agreement | latent | user_minus_john_lambda_C | 1.000 |  | latent:1 |

## Phase 6: predicting other AI models

_(no E-condition data yet)_

## Figures

1. `figures/fig1_coherence.pdf`
2. `figures/fig2_lambda_vs_capability.pdf`
3. `figures/fig3_scatter.pdf`
4. `figures/fig4_retention.pdf`
5. `figures/fig5_lambda_by_method.pdf`
6. `figures/fig6_probe_layers.pdf`

## Anomalies

- Refusal-like responses: gemma3_4b_mlx A: 7.
- Refusal-like responses: gemma3_4b_mlx B: 3.
- Refusal-like responses: gemma3_4b_mlx C_john: 6.
- Refusal-like responses: gemma3_4b_mlx D_john: 3.
- Refusal-like responses: gemma3_4b_mlx D_user: 1.
- lambda_D interval excludes 1: gemma3_4b_mlx latent user (fit pooled): 0.550 [0.116, 0.968].

