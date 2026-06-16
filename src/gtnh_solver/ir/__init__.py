"""ir — the two versioned data contracts everything couples to.

``InputIR`` (the problem) and ``LayoutResult`` (the solution, consumed by previewer,
build guide, and later export). Full spec: docs/IR.md. Implemented as Pydantic v2 models,
split across submodules and re-exported here as the package's public surface:

- ``enums``      — Commodity, IODirection, Facing, LayoutStatus
- ``geometry``   — CellCoord, CellBox (integer cell-grid value types)
- ``input_ir``   — Port, FaceSpec, Machine, MachineFaceRef, Net, METoggles, PinnedIO,
                   InputIR  (+ INPUT_IR_VERSION)
- ``output``     — Placement, Segment, Route, LayoutMetrics, Infeasibility,
                   LayoutResult  (+ LAYOUT_RESULT_VERSION)

Both roots carry an int ``version``. Additive fields can land without a bump; any change
that breaks an existing consumer bumps the relevant ``*_VERSION`` and updates all
consumers in the same PR. Keep the changelog at the bottom of this file current.
"""

from __future__ import annotations

from .enums import Commodity, Facing, IODirection, LayoutStatus
from .geometry import CellBox, CellCoord
from .input_ir import (
    INPUT_IR_VERSION,
    FaceSpec,
    InputIR,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    PinnedIO,
    Port,
)
from .output import (
    LAYOUT_RESULT_VERSION,
    Infeasibility,
    LayoutMetrics,
    LayoutResult,
    Placement,
    Route,
    Segment,
)

__all__ = [
    # versions
    "INPUT_IR_VERSION",
    "LAYOUT_RESULT_VERSION",
    # enums
    "Commodity",
    "IODirection",
    "Facing",
    "LayoutStatus",
    # geometry
    "CellCoord",
    "CellBox",
    # input IR
    "Port",
    "FaceSpec",
    "Machine",
    "MachineFaceRef",
    "Net",
    "METoggles",
    "PinnedIO",
    "InputIR",
    # output schema
    "Placement",
    "Segment",
    "Route",
    "LayoutMetrics",
    "Infeasibility",
    "LayoutResult",
]


# ---------------------------------------------------------------------------
# Contract changelog (bump the relevant *_VERSION on any breaking change):
#
# InputIR v0 / LayoutResult v0 — initial implementation of the docs/IR.md draft.
#   Concretizations made where the doc left shapes open (reconciled into docs/IR.md):
#   - FaceSpec is a list of `Port` (id/commodity/direction/is_auto_output/cover); the
#     physical face is a solver decision, so FaceSpec is a port catalog, not a face map.
#   - MachineFaceRef references a machine + port_id (resolved to a face by the solver).
#   - Geometry `Box`/`CellBox` unified into one `CellBox` (a size, each dim >= 1).
#   - Segment fields named `start`/`end` (doc's `from` is a Python keyword).
# ---------------------------------------------------------------------------
