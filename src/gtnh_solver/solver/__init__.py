"""solver - composes placement + auto-output + routing into a LayoutResult.

Phase 1 ships a single-pass :func:`solve` (in ``core``): place (flow order) -> assign
auto-output connections for adjacent machines -> route pipes for the rest -> assemble. No
feedback loop yet. Phase 2 adds the place<->route<->retry loop and the anytime budget
(docs/ARCHITECTURE.md #1/#6): perturb placement when a net is unroutable, return the best
valid layout on timeout, never hang, never return a silently-invalid layout.
"""

from __future__ import annotations

from .core import solve

__all__ = ["solve"]
