"""solver - composes placement + routing into a LayoutResult.

:func:`solve` (in ``core``) runs the place<->route feedback loop (docs/ARCHITECTURE.md
#1/#6): place (SA over a routing-aware cost) -> route (the router decides auto-output vs
pipe from the final geometry - router.auto - and pipes the rest) -> assemble -> validate
against the independent validator, so it never returns a silently-invalid layout. When an attempt leaves nets unrouted it penalizes
exactly those nets and re-places, keeping the best layout seen and returning the first
fully-valid one (anytime best-so-far). Deterministic: bounded attempts keyed off ``seed``
plus the penalties, no wall-clock. Phase 2 layers a wall-clock anytime budget on top (return
the best valid layout on timeout, never hang).
"""

from __future__ import annotations

from .core import solve

__all__ = ["solve"]
