"""dataset - the GT physical-rules data and its loader.

Footprints, machine faces (front = no I/O; five usable), pipe/wire tiers + throughputs,
voltage tiers, ME behavior, and cell->block mappings. This is the single biggest piece of
real work and is GT-version-specific. Rule RULES live here as DATA; the validator re-checks
them with independent LOGIC (docs/ARCHITECTURE.md #4). See docs/DOMAIN.md for the rules.

Shipped so far: the per-tier **voltage** ladder + amperage helper (``voltage`` submodule) that
the shared-amperage power feature needs. Still TODO(dataset): the footprint/face/throughput
schema + loader and a starter machine set; spot-check tiers/face-rules/throughputs in-game
(docs/ROADMAP.md step 0).
"""

from __future__ import annotations

from .voltage import VOLTAGE_BY_TIER, UnknownTierError, amperage, tier_voltage

__all__ = ["VOLTAGE_BY_TIER", "UnknownTierError", "amperage", "tier_voltage"]
