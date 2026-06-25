"""GT:NH voltage tiers and the per-machine amperage they imply.

A machine draws ``eut`` EU/t at its voltage tier. On a shared-amperage cable the *amperage* it
pulls is ``ceil(eut / tier_voltage)`` (at least 1 for any powered machine), and those amperages
**sum** along shared cable segments to set the cable thickness (docs/DOMAIN.md, the
shared-amperage net). This module is the thin slice of the Phase 2 dataset lane that power
sizing needs now: the canonical GT:NH voltage ladder (EU/t, each tier 4x the last from ULV=8)
plus the amperage helper. Real footprints/faces/throughput caps remain Phase 2 (docs/ROADMAP.md).
"""

from __future__ import annotations

import math

#: EU/t of each GT:NH voltage tier (the cable/machine voltage). Each step is 4x the previous.
#: The ladder a machine's ``overclock_tier`` keys into; a cable serving a tier is rated >= it.
VOLTAGE_BY_TIER: dict[str, int] = {
    "ULV": 8,
    "LV": 32,
    "MV": 128,
    "HV": 512,
    "EV": 2048,
    "IV": 8192,
    "LuV": 32768,
    "ZPM": 131072,
    "UV": 524288,
    "UHV": 2097152,
    "UEV": 8388608,
    "UIV": 33554432,
    "UMV": 134217728,
    "UXV": 536870912,
    "MAX": 2147483648,
}


class UnknownTierError(KeyError):
    """A voltage-tier string not on the GT:NH ladder (a typo, or an unsupported/legacy tier)."""


def tier_voltage(tier: str) -> int:
    """The EU/t voltage of ``tier``; raises :class:`UnknownTierError` if it is not on the ladder."""
    try:
        return VOLTAGE_BY_TIER[tier]
    except KeyError:
        raise UnknownTierError(tier) from None


def amperage(eut: float, tier: str) -> int:
    """Amps a machine drawing ``eut`` pulls at ``tier``.

    ``ceil(eut / tier_voltage)`` - so a machine running at exactly its tier voltage is 1 amp, and
    a draw above the tier voltage is the proportional number of amps. Zero iff ``eut <= 0`` (an
    unpowered block, or a power *source*, draws nothing). Raises :class:`UnknownTierError` for an
    unknown tier.
    """
    if eut <= 0:
        return 0
    return math.ceil(eut / tier_voltage(tier))
