"""ir - the two versioned data contracts everything couples to.

``InputIR`` (the problem) and ``LayoutResult`` (the solution, consumed by previewer,
build guide, and later export). Full spec: docs/IR.md. Implemented as Pydantic v2 models,
split across submodules. The models + versions are re-exported here as the package's public
surface, but the low-level cell-grid *helpers* in ``geometry`` (``FACE_DELTAS``,
``occupied_cells``, ``in_region``, ...) are **not** - consumers deep-import those from
``ir.geometry`` directly, a convention applied consistently across the placement, router, and
validator lanes. Only the value types (``CellCoord``, ``CellBox``) surface here. The submodules:

- ``enums``      - Commodity, IODirection, Facing, LayoutStatus
- ``geometry``   - CellCoord, CellBox (integer cell-grid value types)
- ``input_ir``   - Port, FaceSpec, Machine, MachineFaceRef, Net, METoggles, PinnedIO,
                   InputIR  (+ INPUT_IR_VERSION)
- ``output``     - Placement, Segment, Terminal, Route, LayoutMetrics, Infeasibility,
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
    AutoConnection,
    Infeasibility,
    LayoutMetrics,
    LayoutResult,
    Placement,
    Route,
    Segment,
    Terminal,
)

__all__ = [  # noqa: RUF022 - grouped by section (mirrors definition order), not alphabetized
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
    "Terminal",
    "Route",
    "AutoConnection",
    "LayoutMetrics",
    "Infeasibility",
    "LayoutResult",
]


# ---------------------------------------------------------------------------
# Contract changelog (bump the relevant *_VERSION on any breaking change):
#
# InputIR v0 / LayoutResult v0 - initial implementation of the docs/IR.md draft.
#   Concretizations made where the doc left shapes open (reconciled into docs/IR.md):
#   - FaceSpec is a list of `Port` (id/commodity/direction/cover); the physical face is a
#     solver decision, so FaceSpec is a port catalog, not a face map.
#   - MachineFaceRef references a machine + port_id (resolved to a face by the solver).
#   - Geometry `Box`/`CellBox` unified into one `CellBox` (a size, each dim >= 1).
#   - Segment fields named `start`/`end` (doc's `from` is a Python keyword).
#
# LayoutResult v0 (additive, no version bump) - added `Route.terminals: list[Terminal]`
#   (machine_id/port_id/face/cell) so a route records where it docks on each machine
#   endpoint. Existing consumers default to an empty list; the router fills it and the
#   validator checks face/adjacency/on-route reachability.
#
# LayoutResult v0 (additive, no version bump) - added `LayoutResult.auto_connections:
#   list[AutoConnection]`. A net is satisfied by EITHER a pipe `Route` OR an
#   `AutoConnection` (adjacent machines auto-feeding, no pipe). Machine `orientation` is
#   horizontal-only (GT machines never face up/down).
#
# InputIR v1 (BREAKING) - dropped `Machine.count`. Multi-instance machine groups are not
#   supported until instance-aware routing exists (Phase 2): the placer expanded `count` into
#   N placements sharing one machine id, but the router/solver/validator collapsed them via
#   `setdefault` and a `MachineFaceRef` cannot address a specific instance - counted machines
#   were placed yet silently left unwired. Each `Machine` is now exactly one instance; the
#   adapter rejects an export `machineCount > 1` with an explicit `AdapterError`. `count`
#   returns once routing is instance-aware.
#
# InputIR v1 (additive, no version bump) - added `Machine.eut: float` (EU/t draw). With
#   `voltage_tier` it gives the amperage a machine pulls on a shared-amperage cable
#   (dataset.amperage); the adapter sets it from the recipe and synthesizes a power source +
#   net per voltage tier. 0 for unpowered blocks / sources. Existing consumers default to 0.
#
# InputIR v2 (BREAKING) - dropped `Port.is_auto_output` (and its FaceSpec validation). It was a
#   dead, contradictory field: the adapter never set it and the solver auto-connects any adjacent
#   output regardless. Whether a port is satisfied by auto-output is a SOLVER DECISION, not a
#   problem input - it lives in the output's `AutoConnection`, and the "one auto-output per
#   machine, items-xor-fluids, never power" rule is enforced there by the validator
#   (DUPLICATE_AUTO_OUTPUT / AUTO_OUTPUT_ILLEGAL_COMMODITY), not on the input contract.
#
# InputIR v2 (additive, no version bump) - added `Port.rate: float | None` (items/t or mB/t moved
#   through the port; None for power or when unknown). The adapter computes it from the recipe;
#   `system_io` + the previewer use it to surface boundary input/output rates - notably a dangling
#   OUTPUT port, whose product rate lives nowhere else (no consuming net). Existing consumers
#   default to None. (GitHub #16.)
#
# InputIR v2 (additive, no version bump) - added `Machine.block_key: str | None`, the GT controller
#   block as "<registry_name>@<meta>". `Machine.type` is the exporter's localized recipe-map name,
#   which for GT++ machines is NOT the controller block's name the structure dataset is keyed by
#   ("Chemical Plant" vs "ExxonMobil Chemical Plant"), so name-only lookups silently missed and
#   those machines fell back to a 1x1x1 footprint. The adapter sets it from the export's
#   `recipe.source.machineBlock` (gtnh-factory-flow #25); consumers that have it join exactly and
#   fall back to `type` when it is None, so pre-#25 plans behave exactly as before. (GitHub #98.)
# ---------------------------------------------------------------------------
