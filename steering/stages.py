"""Every stage of the steering experiment and every table made from the stages' folders, by the module that
holds it. A module is imported when one of its stages or tables is first asked for, so that importing
``steering.run`` or ``steering.analysis`` loads none of the circuit and program modules."""
from __future__ import annotations

from importlib import import_module
from typing import NamedTuple


class Stage(NamedTuple):
    """A stage's function, "<module>.<function>" in ``steering``, called as f(engine, run_dir, cfg, cases), and
    whether the stage starts in the precision its settings name ("dtype"). Every other stage starts in the
    run's; the sampled stages of the other modules switch to theirs themselves."""
    function: str
    own_dtype: bool = False


class Tables(NamedTuple):
    """A table function, "<module>.<function>" called as f(run_dir, cfg) -> {name: table}, made when any of
    ``folders`` exists in the run (and, if ``setting`` is named, the settings have that entry)."""
    function: str
    folders: tuple
    setting: str | None = None


# The stages the command line takes, in the order it lists them; "all" runs the first four.
STAGES = {
    "baseline": Stage("run.stage_baseline"),
    "vectors": Stage("run.stage_vectors"),
    "dose": Stage("run.stage_dose"),
    "reason": Stage("run.stage_reason", own_dtype=True),
    "controls": Stage("run.stage_controls"),
    "probe": Stage("run.stage_probe"),
    "branch": Stage("run.stage_branch", own_dtype=True),
    "decision": Stage("run.stage_decision"),
    "circuit": Stage("circuit.stage_circuit"),
    "circuit_reply": Stage("circuit.stage_reply"),
    "circuit_fine": Stage("circuit.stage_fine"),
    "circuit_algorithm": Stage("circuit.stage_algorithm"),
    "circuit_concrete": Stage("circuit.stage_concrete"),
    "circuit_steer": Stage("circuit.stage_steer"),
    "circuit_paired": Stage("circuit.stage_paired"),
    "references": Stage("circuit.stage_references"),
    "circuit_program": Stage("program.stage_program"),
    "program_reason": Stage("program.stage_program_reason"),
    "circuit_tables": Stage("head_tables.stage_tables"),
    "circuit_tables_reply": Stage("head_tables.stage_tables_reply"),
    "circuit_match": Stage("circuit_steps.stage_match"),
    "circuit_tag": Stage("circuit_steps.stage_tag"),
    "circuit_phrase": Stage("circuit_steps.stage_phrase"),
    "circuit_redirect": Stage("program.stage_redirect"),
    "circuit_reply_headers": Stage("program.stage_reply_headers"),
}
ALL = ("baseline", "vectors", "dose", "reason")

# The tables of the circuit and program modules (``steering.analysis`` makes those of ``steering.run``).
TABLES = (
    Tables("circuit.tables", ("circuit",)),
    Tables("circuit.reply_tables", ("circuit",)),
    Tables("circuit.algorithm_tables", ("circuit",)),
    Tables("circuit.concrete_tables", ("circuit",)),
    Tables("program.tables", ("circuit_program", "program_reason", "circuit_reply_headers",
                              "circuit_reply_headers_cross", "circuit_reply_headers_lines"), setting="head_sets"),
    Tables("head_tables.tables", ("circuit_tables",)),
    Tables("head_tables.reply_tables", ("circuit_tables_reply",)),
    Tables("circuit_steps.tables", ("circuit_match",)),
    Tables("circuit_steps.phrase_tables", ("circuit_phrase",)),
    Tables("circuit_steps.tag_tables", ("circuit_tag",)),
)


def function(name: str):
    """The function a ``Stage`` or ``Tables`` entry names, its module imported if it is not yet."""
    module, attribute = name.split(".")
    return getattr(import_module(f"steering.{module}"), attribute)
