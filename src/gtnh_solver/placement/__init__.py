"""placement - machine placement over the coarse cell grid.

Two placers behind one ``PlacementResult`` contract (the validator independently certifies
either): :func:`place` is the crude deterministic first-fit constructive placer (``constructive``)
and :func:`optimize_placement` is the Phase 2 simulated-annealing + LNS optimizer (``search``) that
seeds from it and improves a routing-aware cost, with orientation as a search variable and a
large-neighbourhood ruin-and-recreate move that reshuffles net-connected clusters
(docs/ROADMAP.md lane C, docs/ARCHITECTURE.md #1). The solver uses the optimizer; the constructive
placer remains the SA seed (and a simple fallback). The place<->route feedback loop that re-places
on unrouted nets lives in ``solver.core``.
"""

from __future__ import annotations

from .constructive import PlacementResult, place
from .search import Objective, optimize_placement

__all__ = ["Objective", "PlacementResult", "optimize_placement", "place"]
