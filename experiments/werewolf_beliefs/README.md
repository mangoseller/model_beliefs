# Werewolf joint-belief experiment

Completes the remaining main domain after CKD and scientific forecasting, using the same one-dimensional joint model for Gemma 3 4B and 12B and all 16 question types. Branching files are separate reasoning-prefix follow-ups on the same three domains; this experiment uses the main response datasets.

The primary analysis intersects the models' complete cases: 405 of 500 original cases (4B has 407 complete cases, 12B has 494). It preserves the 4B split for the paired cases: 260 training, 65 validation and 80 test. Every retained case is from a distinct game, with no replay overlap across splits. Results condition on successful completion/parsing in both models; omitted cases are not regenerated.

Neutral extraction preserves the entire player-visible game context and adds the focal proposition that the target player is a Werewolf. It removes the elicitation request and excludes hidden target-role metadata. The focal player's private information and publicly revealed roles remain legitimate evidence. MLX extraction uses 512-token chunked KV prefill without truncation, all post-block residual vectors at the final assistant-generation-prefix token, float32 storage, strict cache provenance, and a first-new-case forward identity check. Prompts range from 856 to 8,824 tokens, totaling 1,267,393 tokens across the paired cases per model.

Selection uses eight layers, four L2 penalties, original and monotone-spline response curves, and training/validation joint likelihood. Controls include behavior only, a scalar readout from training-fitted TF-IDF of game evidence, one matched shuffled activation fit, and an activation-only ablation of the jointly fitted model. The input benchmark has at most 2,048 unigram/bigram features and does not identify the existence or absence of beliefs. No post-test calibration or evidence tempering is added.

Each selected cell is locked before test scoring. Final refits use train plus validation and validation-selected epoch counts. Completed 4B or original-curve results can be reported while the other fixed searches continue. Numerical integration audits and withheld-target checks accompany all evaluations; uncertainty uses 10,000 paired case bootstrap draws. Game-cluster intervals are also reported; each game supplies one case.

Run from the repository root with `/Users/sam/miniforge3/envs/latent-beliefs/bin/python`:

```sh
PYTHONPATH=. python -m unittest experiments.werewolf_beliefs.test_model experiments.werewolf_beliefs.test_run experiments.werewolf_beliefs.test_report
PYTHONPATH=. python -m experiments.werewolf_beliefs.audit_responses
PYTHONPATH=. python -u -m experiments.werewolf_beliefs.orchestrate
```

The coordinator resumes completed work, runs GPU extractions sequentially, overlaps CPU fits, evaluates locked cells as they finish, and builds both the Werewolf and three-domain reports. It keeps the machine awake during work and stops its owned compute processes if battery power falls to 10%; rerunning on external power resumes saved progress.

Artifacts:

- `build/werewolf_beliefs/full_experiment/report.html`: complete Werewolf report.
- `build/werewolf_beliefs/full_experiment/{4b,12b}/interim_report.html`: completed per-size reports.
- `build/cross_domain_beliefs/report.html`: comparison with the unchanged CKD and forecasting primary experiments, including the activation-only ablation.
- `build/werewolf_beliefs/orchestrator_status.json`: active/completed jobs.

The cross-domain comparison keeps the original CKD joint likelihood separate from its later calibration follow-up. It reports each domain's uncertainty separately because case populations, response distributions, and input benchmarks differ.
