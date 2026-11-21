# The bespoke dataset: item file schema

The bespoke dataset is short fictional second-order belief items whose
Bayes-optimal answers are known. The items are kept in a local file that is
not in the repository. Point the config at it (`bespoke.items` in
`configs/second_order.yaml`, default `data/bespoke/items.yaml`) or set
`SO_BESPOKE_ITEMS`. `tests/fixtures/bespoke_items.yaml` is a small example in
this format; it is used by the tests and the synthetic run, not the study.

Check a file and render its prompts with:

```sh
python -m second_order.bespoke --items data/bespoke/items.yaml --examples docs/example_prompts_bespoke.md
```

This prints every item's Bayes answers and gaps, then lists warnings: answers
outside [0.05, 0.95] (where the logit clip at 0.01/0.99 is close), zero gaps
on non-placebo items, and non-zero gaps on placebo items. It writes
`results/bespoke/items.csv`.

## Format

YAML (or JSON) with a top-level `items` list. Each item:

```yaml
items:
  - id: marbles_01                 # unique; the case id is bespoke__marbles_01
    family: marbles                # free text, reported in tables
    placebo: false                 # placebo items are reported separately, not in the estimates
    scenario: >-                   # everything both the model and the agent know
      A fair coin was tossed to choose one of two bags. Bag 1 holds 3 red marbles and 1 blue
      marble. Bag 2 holds 1 red marble and 3 blue marbles. ...
    proposition: the marbles were drawn from Bag 1   # completes "the probability that ..."
    prior: 0.5                     # P(proposition) before any evidence
    evidence:                      # E, in display order
      - {id: d1, text: "The first marble drawn was red.", p_true: 0.75, p_false: 0.25}
      - {id: d2, text: "The second marble drawn was red.", p_true: 0.75, p_false: 0.25}
      - {id: d3, text: "The third marble drawn was blue.", p_true: 0.25, p_false: 0.75}
    partial: [d3]                  # E_p: ids of the observations the agent saw
    false:                         # E_f: what the agent was told; observations replaced by id
      d1: {text: "The first marble drawn was blue.", p_true: 0.25, p_false: 0.75}
```

`p_true` and `p_false` are the probabilities of the observation if the
proposition is true or false. The Bayes answer for an evidence set is

    posterior odds = prior / (1 - prior) * prod p_true / p_false

The false-belief set keeps every observation in place and swaps the listed
ones, so the agent believes a complete but partly wrong account. A placebo
observation has `p_true == p_false` (for example "The marble was drawn on a
Tuesday."). A placebo item withholds or changes only such observations, so
all three gaps should be zero.

A bare `false:` key is read correctly even though YAML 1.1 parses it as a
boolean; `told:` is accepted as a synonym.

### Optional fields

| Field | Use |
|---|---|
| `bayes: {full: p, partial: p, false: p}` | Bayes answers given directly. Needed when the observations carry no `p_true`/`p_false` (for example, items not built from independent observations). |
| `prompts: {A: "...", B: "...", ...}` | Verbatim prompts per condition key (`A`, `B`, `C_john`, `M_S4`, `D_john`, `B_F`, `C_F`, `M_F`, `D_F`). They replace the generated ones. Each must contain the JSON probability instruction, or `python -m second_order.bespoke` warns. |
| `comprehension: {C_john: {stem, options, answer}, C_F: {...}}` | Your own follow-up question: `options` is a list, `answer` is the 0-based index of the correct option. |

## Generated prompts

Every prompt is built from these parts, in order:

1. The scenario.
2. `Evidence:` and the observations the model is shown.
3. For prompts about the agent, one agent paragraph.
4. The question.
5. The JSON probability instruction of the first-order CKD prompt, verbatim.

| Condition | Evidence shown | Agent paragraph |
|---|---|---|
| A | E | none (own belief) |
| B | E_p | none |
| B_F | E_f | none |
| D_john | E | "He saw exactly the evidence listed above." |
| M_S4 | E_p | same as D_john |
| C_john | E | "He saw only the following evidence: ..." (E_p) |
| D_F | E | "He did not see the evidence himself; someone else reported it to him. What he was told matched the evidence listed above exactly." |
| M_F | E_f | same as D_F |
| C_F | E | "... someone else reported it to him and got some of it wrong. He was told: ..." (E_f) |

M and D share one template, so they differ only in the evidence block (tested).

Answers are read from the JSON object only (`bespoke.parse_response`): the
last `{...}` with a `probability` key, or a quoted `"probability":` key. The
first-order CKD parser also accepts any number within 40 characters after
the word "probability". Bespoke propositions contain numbers ("the
probability that the marbles were drawn from Bag 1"), so with that fallback
a missing answer would be read as 1. It is therefore not used here.

After each C answer, a second turn asks which evidence the agent saw or was
told. The options are the agent's evidence, the full evidence and the other
setting's evidence (plus, for the partial setting, the withheld
observations).
