"""adapter - gtnh-factory-flow exported plan JSON -> InputIR.

Parses the documented export (typed view in ``plan``) and maps it to the solver's input
contract (``core``). No upstream code is vendored; the consumed shape is pinned by the
committed fixtures in ``examples/`` and ``tests/fixtures/`` (docs/ARCHITECTURE.md decision #3).

Crude for Phase 1 (docs/ROADMAP.md): single-block footprints and default orientations (real
footprints/faces arrive with the dataset lane). It DOES synthesize the power network the export
omits - a source + shared-amperage net per voltage tier (``power`` submodule) - and, on a
schema-v2 export, trusts the ``resolved`` throughput block for each machine's EU/t draw,
cross-checking it against the recipe-derived synthesis (mismatch -> ``AdapterWarning``, #2).
"""

from __future__ import annotations

from ._errors import AdapterError, AdapterWarning
from .core import adapt_file, load_plan, to_input_ir
from .plan import (
    AppInfo,
    Edge,
    MachineBlock,
    Node,
    Plan,
    Recipe,
    RecipeSource,
    ResolvedBlock,
    ResolvedExternalIO,
    ResolvedFlow,
    ResolvedMachine,
    ResolvedNet,
    ResolvedPower,
    Resource,
    Storage,
)

__all__ = [
    "AdapterError",
    "AdapterWarning",
    "AppInfo",
    "Edge",
    "MachineBlock",
    "Node",
    "Plan",
    "Recipe",
    "RecipeSource",
    "ResolvedBlock",
    "ResolvedExternalIO",
    "ResolvedFlow",
    "ResolvedMachine",
    "ResolvedNet",
    "ResolvedPower",
    "Resource",
    "Storage",
    "adapt_file",
    "load_plan",
    "to_input_ir",
]
