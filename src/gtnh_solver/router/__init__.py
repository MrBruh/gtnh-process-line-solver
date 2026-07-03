"""router - per-commodity routing on the cell grid.

Phase 1 ships a crude A* router (:func:`route`, in ``core``). The router owns the
**auto-output vs pipe** decision: from the final placements + orientations it first assigns
GT's free auto-output connections (:func:`assign_auto_outputs`, in ``auto`` - adjacent
1-source-1-sink item/fluid nets, one auto-output per machine, never power/ME) and pipes only
the nets left uncovered. For each piped net: resolve a Terminal per net
endpoint on a usable (non-front) machine face, then A* between terminals avoiding machine and
reserved cells. Routing is **capacity-aware** - each laid route's cells become obstacles for the
routes after it, across both item/fluid and power, so no cell carries two routes (the crude
single-channel cap, one route per cell, which the validator independently enforces). Because that
makes routing order-dependent, the item/fluid router does **rip-up/reroute** - retrying with the
failed nets first - so a bad net order is not mistaken for a real infeasibility. Crude: one
channel per cell, item/fluid only (docs/ROADMAP.md).

Power is its own router (:func:`route_power`, in ``power``): each per-tier power net becomes a
shared-amperage trunk whose segment thickness is sized to the summed amperage (docs/DOMAIN.md,
docs/ARCHITECTURE.md #8). The solver passes the item/fluid cells as ``extra_obstacles`` so cables
route around pipes. Both routers share the ``_grid`` primitives.

Phase 2 lifts the single-channel cap to the full channels-per-edge cap (a routing margin hosting
several parallel channels) + cell->block realizability, replaces the crude failed-first
rip-up/reroute with negotiated-congestion routing (GitHub #7), adds ME endpoint placement, and the
shared-amperage power *optimization* (multi-source / split / upgrade) beyond Phase 1's
size-or-reject (docs/ARCHITECTURE.md #6/#7/#8). The validator independently certifies routes
either way.
"""

from __future__ import annotations

from .auto import assign_auto_outputs
from .core import RouteResult, route
from .power import PowerRouteResult, route_power

__all__ = ["PowerRouteResult", "RouteResult", "assign_auto_outputs", "route", "route_power"]
