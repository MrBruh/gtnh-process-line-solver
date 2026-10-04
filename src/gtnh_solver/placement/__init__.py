"""placement - machine placement over the coarse cell grid.

Two placers behind one ``PlacementResult`` contract (the validator independently certifies
either): :func:`place` is the crude deterministic first-fit constructive placer (``constructive``)
and :func:`optimize_placement` is the Phase 2 simulated-annealing + LNS optimizer (``search``) that
seeds from it and improves a routing-aware cost, with orientation as a search variable and a
large-neighbourhood ruin-and-recreate move that reshuffles net-connected clusters
(docs/ROADMAP.md lane C, docs/ARCHITECTURE.md #1); it moves a node's parallel single blocks as one
rigid column, back to front (``groups``). The solver uses the optimizer; the constructive
placer remains the SA seed (and a simple fallback). :func:`bank_columns` (``banks``) is a third,
narrow constructor: a line that is one chain of banks of parallel single blocks gets them laid as
columns sharing straight pipe runs, a candidate the solver routes alongside its annealed attempts.
The multi-start that routes and ranks the annealed attempts lives in ``solver.core``.
"""

from __future__ import annotations

from gtnh_solver.ir.nets import SINGLE_BLOCK_IO_FACES

from .banks import bank_columns
from .constructive import PlacementResult, place
from .feasibility import crowded_machines, single_block_shortfalls
from .search import Objective, optimize_placement

__all__ = [
    "SINGLE_BLOCK_IO_FACES",
    "Objective",
    "PlacementResult",
    "bank_columns",
    "crowded_machines",
    "optimize_placement",
    "place",
    "single_block_shortfalls",
]
