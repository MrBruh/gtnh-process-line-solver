"""router - per-commodity routing on the cell grid.

The item/fluid router is :func:`route` (in ``core``). It owns the **auto-output vs pipe**
decision: from the final placements + orientations it first assigns GT's free auto-output
connections (:func:`assign_auto_outputs`, in ``auto`` - adjacent 1-source-1-sink item/fluid nets,
and each producer standing against the one consumer of a net it shares with others; one
auto-output per machine, never power/ME) and routes only what is left uncovered. Those nets,
**power included**, are routed together by negotiated congestion (PathFinder, GitHub #7): each net
is a group Steiner tree whose dock cells are chosen by the same search that lays its path
(``steiner``), contested cells are priced up round by round, and no cell ends up carrying two nets
(the single-channel cap the validator independently enforces). Several terminals of one net may
share a cell, which is how one pipe block serves several machines (#164).

Power is laid by its own router (:func:`route_power`, in ``power``): each per-tier power net becomes
a shared-amperage trunk whose segment thickness is sized to the summed amperage (docs/DOMAIN.md,
docs/ARCHITECTURE.md #8), in the space the negotiated pipes left for it. The solver passes the
item/fluid cells as ``extra_obstacles`` so cables route around pipes. Both routers share the
``_grid`` primitives.

The ME router (:func:`route_me`, in ``me``, #334) lays each AE2 network a line's nets ride as a
forest of cable from where its channels enter (an attach stub, a controller, or ad hoc), with the
ME devices on it, keeping a halo of free cells between networks because an AE cable joins every
neighbour it touches. It is pure and not wired into the solve yet (#335).

Still ahead (docs/ROADMAP.md): the per-edge multi-channel cap, cell->block realizability, wiring
the ME router into the solve (#335), and shared-amperage power *optimization* (multi-source /
split / upgrade) beyond size-or-reject. The validator independently certifies routes either way.
"""

from __future__ import annotations

from ._grid import claims_by_machine, vent_cells
from .auto import AutoAssignment, assign_auto_outputs, auto_output_possible
from .core import RouteResult, route
from .hatches import HatchPlan, place_hatches
from .me import MERouteResult, route_me
from .power import PowerRouteResult, route_power

__all__ = [
    "AutoAssignment",
    "HatchPlan",
    "MERouteResult",
    "PowerRouteResult",
    "RouteResult",
    "assign_auto_outputs",
    "auto_output_possible",
    "claims_by_machine",
    "place_hatches",
    "route",
    "route_me",
    "route_power",
    "vent_cells",
]
