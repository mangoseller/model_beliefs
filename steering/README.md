# Steering: the lookup circuit behind the false-evidence leak

When the model reasons about what PERSON believes from two records of one patient (its own and the one PERSON was
shown), it often answers from the wrong record. The stages here find the computation that decides which record a
value is read from, test each step by intervention, and show that changing it induces or removes the leak in sampled
answers. Models: Qwen3 8B and 14B, through `transformers` with forward hooks.

## What is here

| File | Holds |
|---|---|
| `run.py` | The command line, the default settings, and the stages on the stated probability: unedited read-outs, the mean vectors, the slot vector at a range of strengths, sampled answers under edits |
| `circuit.py` | The lookup circuit: which heads carry a record lookup, what they read and write, tests of each step, and the same heads while a reply is sampled |
| `circuit_steps.py` | Single steps taken apart: how a record gets its place, where the owner is read, queries and keys exchanged |
| `program.py` | The lookup as a program of four variables (owner, place, field, value), the stages that give the model the program's answer, invented records, and the redirect that removes the leak |
| `head_tables.py` | Heads written from tables of the program's variables |
| `engine.py` | The model wrapper: chunked forward passes over a shared cache, residual edits, attention knockouts, head and MLP patches, sampling |
| `interventions.py` | Mean vectors and the edits built from them |
| `prompts.py`, `transcripts.py` | Token layout of a prompt; which record's values a reply states |
| `analysis.py` | The leak estimator and the summary tables |
| `scripts/` | CPU analyses of finished runs |
| `configs/` | Settings of each pass, named stage, variant, model |

The invented-record stages sample their fields, values and owner names from the shared dataset.
[Invented record definitions](../datasets/steering/invented_records.json)

## Stages

A stage writes one file per patient under `<out>/<model>/<stage>/` and skips patients it has already written.

| Stage | Measures |
|---|---|
| `baseline`, `vectors` | The belief with no reasoning and direct lookups for every prompt; the mean vectors from the fit half of the patients |
| `dose`, `controls`, `probe` | The slot vector added at the records, without reasoning: a range of strengths, both signs, random directions of its size, the M and D references under the edit |
| `reason` | Sampled answers with reasoning, unedited and under edits (a config names the cells) |
| `references` | The reference conditions sampled with this sampler |
| `circuit`, `circuit_fine`, `circuit_algorithm`, `circuit_concrete` | The heads that carry a lookup (ranked on fit patients, tested on held-out ones), what they attend to and write, and tests of each step |
| `circuit_tag`, `circuit_phrase`, `circuit_match` | The place tag, the owner phrase, and the readers' query and key match |
| `circuit_program`, `circuit_tables` | The program's address given to the reader heads; upstream heads written from tables of place, owner and field |
| `circuit_reply`, `circuit_tables_reply`, `decision`, `branch` | The same lookup at the token where a sampled reply writes a disputed value |
| `circuit_steer`, `circuit_reply_headers` | Sampled answers with head sets barred from a record or from the headers; which tokens carry the labels' owner signal |
| `circuit_paired`, `circuit_redirect`, `program_reason` | Removing the leak: a head set reads other records while the reply is sampled; stored values exchanged; the same on invented records |
| `analyse` | Summary tables and `RESULTS.md` from whatever stages have run |

Run `baseline` and `vectors` first, then `dose`, `controls`, `probe` and the `reason` passes; then `circuit`,
`circuit_fine`, `circuit_algorithm`, `circuit_concrete`; the other stages read the head sets those four write, and the
stages on replies read the `reason` folders.

## Running

    pip install -r requirements/steering.txt
    python -m steering.run <stage> --model qwen3_8b --out runs/steering [--config steering/configs/<file>.json]
    python -m steering.run analyse --model qwen3_8b --out runs/steering --session1 <beliefs_direct.parquet>

The leak of a sampled answer is measured against reference beliefs from the binding experiment
(`second_order.binding_analysis` writes `beliefs_direct.parquet`). Float32 weights take about 32 GB for 8B and 59 GB
for 14B; sampled stages run in bfloat16.

## Tests

    PYTHONPATH=. python -m unittest tests.test_steering

22 tests on a tiny random Qwen3 (about a minute on a CPU; needs the Qwen3-0.6B tokenizer in the Hugging Face cache):
the token layouts, every engine operation against a plain forward pass, the vectors, the leak estimator on a planted
leak, the program, and every stage end to end.
