"""router - per-commodity routing on the cell grid.

Phase 1 ships a crude A* router (:func:`route`, in ``core``): resolve a Terminal per net
endpoint on a usable (non-front) machine face, then A* between terminals avoiding machine and
reserved cells. Crude - one channel, no capacity, item/fluid only (docs/ROADMAP.md).

Power is its own router (:func:`route_power`, in ``power``): each per-tier power net becomes a
shared-amperage trunk whose segment thickness is sized to the summed amperage (docs/DOMAIN.md,
docs/ARCHITECTURE.md #8). Both routers share the ``_grid`` primitives.

Phase 2 adds the channels-per-edge cap + cell->block realizability, rip-up-and-reroute, ME
endpoint placement, and the shared-amperage power *optimization* (multi-source / split / upgrade)
beyond Phase 1's size-or-reject (docs/ARCHITECTURE.md #6/#7/#8). The validator independently
certifies routes either way.
"""

from __future__ import annotations

from .core import RouteResult, route
from .power import PowerRouteResult, route_power

__all__ = ["PowerRouteResult", "RouteResult", "route", "route_power"]
