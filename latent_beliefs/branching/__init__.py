"""Reasoning-prefix branching experiments (Results, "Distinguishing models and instances";
Figure 6; Appendix "Reasoning-prefix branching experiments").

Thirty cases per domain (``assets/branching/selected_instances.csv``) are given
to Gemma 3 12B and DeepSeek V3.2.  Each model
first writes a qualitative reasoning prefix ("How likely is ...?  Provide your
reasoning below."); that exact assistant turn is then replayed before a
follow-up question.

``behavior``     20 prefixes per case; each prefix is followed by every one of
                 the domain's 16 prompts once (Figure 6c).
``probability``  5 prefixes per case; each prefix is followed by 5 independent
                 answers to the numerical-probability prompt (Figure 6a, b).

Stages, in order::

    python -m latent_beliefs.branching.prompts       # first-stage and follow-up texts
    python -m latent_beliefs.branching.collect ...   # Bedrock batches (two stages)
    python -m latent_beliefs.branching.materialize   # batch outputs -> released responses
    python -m latent_beliefs.branching.parse         # strict parsing
    python -m latent_beliefs.branching.icc           # prefix ICC (Figure 6b)
    python -m latent_beliefs.branching.prediction    # same vs different prefix (Figure 6c)
"""
