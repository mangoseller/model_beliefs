# Scientific forecasting joint-belief replication

Applies the CKD shared-scalar model to all 500 scientific forecasting scenarios and all 16 response types for Gemma 3 4B and 12B. Both sizes use the 4B scenario split: 320 training, 80 validation, 100 test. Existing hosted responses are reused; no behavioral generation calls are made.

The activation input contains the complete study materials and a neutral focal proposition, with the elicitation request removed. Extraction uses local 4-bit MLX checkpoints, chunked KV-cache prefill without truncation, and post-block residuals at the final assistant-generation-prefix token. The first newly extracted case is checked against the model's standard forward pass. Caches include prompt, token IDs, code/input fingerprint and float32 vectors.

The experiment retains the original joint likelihood, eight layers, four penalties, two response-curve families, a behavior-only baseline, one matched shuffled-activation fit, and an activation-only ablation. The input benchmark is training-fitted TF-IDF over visible study materials (unigrams/bigrams, min_df=2, maximum 2,048 features) feeding the same scalar model. Matching this benchmark is compatible with evidence-based beliefs. Gold experimental outcomes and study identifiers are not features.

Each cell is locked before its test evaluation. An execution-order amendment evaluates 4B while the unchanged 12B protocol continues, to provide interim held-out results. Final fits use train plus validation and validation-selected epoch counts. Baseline numerical repairs start fresh at each precision; selected candidate repairs retain the selected layer and penalty. Numerical audits and withheld-target checks accompany every evaluation. The unchanged joint likelihood is the primary replication; evidence tempering and smoothing are not added after viewing test scores.

Scenarios share studies: 94 of 100 test scenarios have a study represented in the fitting cases. This measures held-out-scenario prediction, not generalization to new studies. Reports include both paired scenario and paired study-cluster bootstrap intervals, conditional on the fitted models.

Run from the repository root with the latent-beliefs Python environment:

```sh
PYTHONPATH=. python -m unittest experiments.forecast_beliefs.test_model experiments.forecast_beliefs.test_run experiments.forecast_beliefs.test_report
PYTHONPATH=. python -m experiments.forecast_beliefs.run initialize
PYTHONPATH=build/pilot_dependencies:. python -m experiments.forecast_beliefs.extract --size 4b
PYTHONPATH=build/pilot_dependencies:. python -m experiments.forecast_beliefs.extract --size 12b
PYTHONPATH=. python -m experiments.forecast_beliefs.run search --size 4b
PYTHONPATH=. python -m experiments.forecast_beliefs.run search --size 12b
PYTHONPATH=. python -m experiments.forecast_beliefs.run lock
PYTHONPATH=. python -m experiments.forecast_beliefs.run final --size 4b
PYTHONPATH=. python -m experiments.forecast_beliefs.run final --size 12b
PYTHONPATH=. python -m experiments.forecast_beliefs.report
```

Artifacts are under `build/forecast_beliefs/`; the final report is `full_experiment/report.html`. The coordinator is for this already initialized run: it waits for externally started 4B extraction and both baseline jobs, starts 12B extraction and searches, then locks selections and completes final fits and reporting. Fit/cache writes are resumable; source fingerprints reject changed inputs.
