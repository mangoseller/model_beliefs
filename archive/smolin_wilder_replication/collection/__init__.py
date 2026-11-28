"""LLM response collection for the main 16-output experiment (Methods, "Action spaces").

``bedrock`` and ``openai`` prepare, submit, and download batch jobs;
``materialize`` turns the downloaded batch outputs into one response file per
domain, task, and model.
"""
