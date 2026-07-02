"""dataset - the GT physical-rules data and its loader.

Footprints, machine faces (front = no I/O; five usable), pipe/wire tiers + throughputs,
voltage tiers, ME behavior, and cell->block mappings. This is the single biggest piece of
real work and is GT-version-specific. Rule RULES live here as DATA; the validator re-checks
them with independent LOGIC (docs/ARCHITECTURE.md #4). See docs/DOMAIN.md for the rules.

Shipped so far: the per-tier **voltage** ladder, the **cable loss** constant, and the amp-load
helpers (``voltage`` submodule) that the shared-amperage power feature needs - machines average
a *fractional* amp load and only aggregates round up to whole amps. Still TODO(dataset): the
footprint/face/throughput schema + loader and a starter machine set; per-material cable loss;
spot-check tiers/face-rules/throughputs in-game (docs/ROADMAP.md step 0).
"""

from __future__ import annotations

from .voltage import (
    CABLE_LOSS_PER_BLOCK,
    CABLE_THICKNESSES,
    MAX_CABLE_THICKNESS,
    VOLTAGE_BY_TIER,
    UnknownTierError,
    UnpowerableError,
    amp_load,
    delivered_voltage,
    tier_voltage,
    whole_amps,
)

__all__ = [
    "CABLE_LOSS_PER_BLOCK",
    "CABLE_THICKNESSES",
    "MAX_CABLE_THICKNESS",
    "VOLTAGE_BY_TIER",
    "UnknownTierError",
    "UnpowerableError",
    "amp_load",
    "delivered_voltage",
    "tier_voltage",
    "whole_amps",
]
