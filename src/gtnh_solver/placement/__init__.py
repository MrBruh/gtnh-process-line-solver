"""placement - machine placement over the coarse cell grid.

Phase 1 ships a crude deterministic first-fit placer (:func:`place`, in ``constructive``);
Phase 2 replaces it with SA/LNS + a cheap routing-aware cost and orientation as a search
variable (docs/ROADMAP.md, docs/ARCHITECTURE.md #1). Both produce ``Placement``s the validator
independently certifies, so the optimizer can be swapped in behind the same contract.
"""

from __future__ import annotations

from .constructive import PlacementResult, place

__all__ = ["PlacementResult", "place"]
