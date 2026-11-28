# Second-order beliefs in language models

When a model reports what someone else believes, how much of its own private evidence leaks in?
This repository measures that *curse of knowledge* in Qwen3 8B and 14B, building on Smolin and
Wilder, *Beliefs and Behavior in Language Models*
([arXiv:2609.07943](https://arxiv.org/abs/2609.07943)).

## The experiment

The model reads a patient's record. PERSON has a record of the same patient that is missing some
fields.

| We ask the model | Condition | Example |
| --- | --- | --- |
| Its own view, from the full record | A | 30% |
| Its own view, from PERSON's record | B | 12% |
| What PERSON thinks; it sees only PERSON's record | M | 13% |
| What PERSON thinks; it has the full record, PERSON the partial one | C | 25% |
| What PERSON thinks, if PERSON had the full record | D | 28% |

The study runs two settings, PERSON missing evidence and PERSON given altered evidence.

The steering experiments trace how the record-lookup circuit maps the owner named in a question
to a place on the page, where reader heads fetch the requested field’s value. Each value the
model writes in a reasoned reply is a fresh lookup, and correcting what the lookup heads read
brings leakage close to zero in both models.

## Running it

On a Linux machine with an NVIDIA GPU:

```bash
git clone https://github.com/mangoseller/model_beliefs && cd model_beliefs
./entrypoint.sh leakage --smoke                    # quick check
nohup ./entrypoint.sh leakage > run.out 2>&1 &     # the full leakage run
```

This installs everything, runs the leakage experiment on both models, and packages the results as
`runs/<run_id>.tar.gz`. Start with `RUN_REPORT.md` inside it.

For steering, run the four default stages (baseline, vectors, dose and reason) or name a stage.
Results go in `runs/steering/<model>/`. See the [steering stage guide](steering/README.md).

```bash
./entrypoint.sh steering
./entrypoint.sh steering analyse --model qwen3_8b
```

Both commands use Python 3.11. Leakage uses `.venv-experiment` and steering uses `.venv-steering`.
Set `VENV` to use another environment. Set `CPU_ONLY=1` for CPU PyTorch. For a full leakage run on GPU,
allow about 120 GB of disk and 48 GB of GPU memory for Qwen3 14B. Leakage also installs the Claude
Code and Codex CLIs by default. Pass `--no-install-agents` to skip them.

## Layout

```
belief_leakage/      the experiment, and its config.yaml
steering/          the lookup circuit behind the false-evidence leak: steering, patching, the lookup as a program (steering/README.md)
datasets/          the experiment's input data
archive/
  first_order_toolkit/         standalone first-order belief experiments
  smolin_wilder_replication/   paper reproduction and latent model used by belief_leakage/
tests/             unit and end-to-end tests
requirements/      pinned dependencies for the leakage and steering experiments
```
