"""router - per-commodity routing on the cell grid.

Phase 1 ships a crude A* router (:func:`route`, in ``core``): resolve a Terminal per net
endpoint on a usable (non-front) machine face, then A* between terminals avoiding machine and
reserved cells. Crude - one channel, no capacity, item/fluid only (docs/ROADMAP.md).

Phase 2 adds the channels-per-edge cap + cell->block realizability, rip-up-and-reroute, ME
endpoint placement, and the shared-amperage power primitive (see ``router/power.py``,
docs/ARCHITECTURE.md #6/#7/#8). The validator independently certifies routes either way.
"""

from __future__ import annotations

from .core import RouteResult, route

__all__ = ["RouteResult", "route"]
