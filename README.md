# Second-order beliefs in language models

When a model reports what someone else believes, how much of its own private evidence leaks in?
This repository measures that *curse of knowledge* in Qwen3 8B and 14B, building on Smolin and
Wilder, *Beliefs and Behavior in Language Models*
([arXiv:2609.07943](https://arxiv.org/abs/2609.07943)).

## The experiment

The model reads a patient's record. PERSON (a named person; the name varies by patient) has a
record of the same patient that is missing some fields.

| We ask the model | Condition | Example |
| --- | --- | --- |
| Its own view, from the full record | A | 30% |
| Its own view, from PERSON's record | B | 12% |
| What PERSON thinks; both records shown, both PERSON's | M | 13% |
| What PERSON thinks; it has the full record, PERSON the partial one | C | 25% |
| What PERSON thinks; both records shown, both full | D | 28% |

**Leakage** $\lambda$ is where C falls between its two references in log-odds $\ell$: $0$ means
the model kept its private evidence out of PERSON's belief, $1$ means it all leaked in. Here
$\lambda = (\ell_C - \ell_M)/(\ell_D - \ell_M) \approx 0.84$; people score about $0.51$
(Camerer, Loewenstein and Weber, 1989). The single-record references M_single and D_single (only PERSON's record
shown) give the Methods' version of the same estimate.

![What λ, φ and τ measure](docs/curse_schematic.png)

Across cases, with $x = \ell_A - \ell_B$ and $x'$ the same gap from a second, independent run:

$$
\lambda = \frac{\beta_C}{\beta_D}, \qquad
\rho = \frac{\mathrm{Cov}(x, x')}{\mathrm{Var}(x)}, \qquad
\varphi = \frac{\beta_D}{\rho}, \qquad
\tau = \frac{\beta_D - \beta_C}{\rho},
$$

where $\beta_C$ and $\beta_D$ are the slopes of $\ell_C - \ell_M$ and $\ell_D - \ell_M$ on $x$, with M and D
the reference pair. $\rho$
corrects for noise in $x$, $\varphi$ is how strongly PERSON's belief follows the evidence (shared by
the two settings below), and $\tau$ is a theory-of-mind score: $\varphi(1 - \lambda)$ under the
model, and $\approx 0$ when PERSON's attributed belief ignores the evidence.

The study runs two settings, PERSON missing evidence and PERSON given altered evidence, on two
datasets in [`datasets/`](datasets): 150 kidney-disease patients, each asked the paper's 16
questions, and 20 fictional puzzles with known Bayes answers. The design is in
[`docs/design.md`](docs/design.md) and the data in [`docs/data.md`](docs/data.md).
[`docs/binding.md`](docs/binding.md) describes a follow-up experiment that asks where the leak comes
from: misbound ownership, evidence that is represented and overridden, or absence not read as ignorance.

## Running it

On a Linux machine with an NVIDIA GPU, clone the repo and choose a run mode.

```bash
git clone https://github.com/mangoseller/model_beliefs && cd model_beliefs

bash run_experiment.sh --smoke                 # quick check on 20 CKD patients
bash run_experiment.sh --core                  # main CKD battery, bespoke trials, estimates and figures
nohup bash run_experiment.sh > run.out 2>&1 &  # full run adds paper replication, extra batteries and activations
```

The launcher installs the pinned dependencies, and the Claude Code and Codex CLIs unless you pass
`--no-install-agents`. With dependencies installed, `python -m second_order.runner --core` runs
directly.

To resume a run, pass `--run-id <run_id>`: successful calls are reused and failed ones retried. Each
run's results, for both models, are in `RUN_REPORT.md` inside `runs/<run_id>.tar.gz`. The
[running guide](docs/running.md) covers the individual stages.

## Layout

```
second_order/      the experiment and its config.yaml, and the binding experiment (binding*.py)
datasets/          the two datasets, sent verbatim
latent_beliefs/    the paper's replication package (latent_beliefs/README.md)
first_order/       first-order belief readouts on small models (first_order/README.md)
docs/              design, data formats, running, the binding experiment
tests/             unit and end-to-end tests
requirements/      pinned dependencies for run_experiment.sh
```

`data/`, `outputs/`, `figures/` and `runs/` are created by runs and are not in git.
