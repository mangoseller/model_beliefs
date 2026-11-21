"""Deprecated: the earlier RQ1-RQ3 design of the second-order experiment.

Kept runnable but not used by the current curse-of-knowledge study (Qwen3 8B and
14B; ``second_order.curse``).  Nothing outside this folder imports from it.

* ``analyze``, ``coherence``, ``rq_figures``: RQ1 (separation across nine hosted
  models), RQ2 (reports-to-decisions coherence, the evidence ladder) and RQ3
  tables and figures (``results/leakage.csv``, ``RESULTS.md``, ``figures/rq*``).
* ``activations``, ``probes``: residual-stream extraction and probes (RQ3).
* ``pilot``: the Phase 1 pilot that chose the withheld set S4.
* ``simulate``: latent-level synthetic recovery of the earlier estimators.
* ``estimator_bias_check``: idealised comparison of the original and matched-control
  estimators.

Run them as ``python -m second_order.deprecated.<module>``.  The shared code they
use (conditions, prompts, generation, fits, ``leakage``, ``beliefs``) stays in
``second_order``.
"""
