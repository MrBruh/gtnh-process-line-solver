"""adapter - gtnh-factory-flow exported plan JSON -> InputIR.

Parses the documented export (typed view in ``plan``) and maps it to the solver's input
contract (``core``). No upstream code is vendored; the consumed shape is pinned by the
committed fixtures in ``examples/`` (docs/ARCHITECTURE.md decision #3).

Crude for Phase 1 (docs/ROADMAP.md): single-block footprints, default orientations, and no
synthesized power nets yet. Real footprints/faces arrive with the dataset lane.
"""

from __future__ import annotations

from .core import AdapterError, adapt_file, load_plan, to_input_ir
from .plan import Edge, Node, Plan, Recipe, Resource, Storage

__all__ = [
    "AdapterError",
    "Edge",
    "Node",
    "Plan",
    "Recipe",
    "Resource",
    "Storage",
    "adapt_file",
    "load_plan",
    "to_input_ir",
]
