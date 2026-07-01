"""GT:NH voltage tiers, cable loss, and the per-machine amperage they imply.

A machine draws ``eut`` EU/t at its voltage tier. GT cables **lose voltage over distance** - a
packet drops ``CABLE_LOSS_PER_BLOCK`` EU per cable block it travels - so a machine ``d`` blocks
from the source receives ``tier_voltage - d`` volts, not the full tier voltage (docs/DOMAIN.md).
On a shared-amperage cable the *amperage* it pulls is ``ceil(eut / delivered_voltage)``: because
loss lowers the arriving voltage, a machine farther from the source draws **more** amps for the
same ``eut`` (the source stays at tier and the cable is thickened to compensate). Those amperages
**sum** along shared cable segments to set the cable thickness (docs/DOMAIN.md, the shared-amperage
net). This module is the thin slice of the Phase 2 dataset lane that power sizing needs now: the
canonical GT:NH voltage ladder (EU/t, each tier 4x the last from ULV=8), the cable loss, and the
amperage helper. Real footprints/faces/throughput caps remain Phase 2 (docs/ROADMAP.md).
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

#: EU lost per cable block a power packet travels. GT cables lose voltage over distance; the
#: voltage a machine receives is the source voltage minus this loss times the block distance
#: (docs/DOMAIN.md). Simplifying assumption for now (maintainer call): every tier has a 1-loss
#: cable available, so loss is a flat 1 EU/block regardless of tier. Per-material loss is Phase 2
#: dataset work (docs/ROADMAP.md).
CABLE_LOSS_PER_BLOCK = 1


class UnknownTierError(KeyError):
    """A voltage-tier string not on the GT:NH ladder (a typo, or an unsupported/legacy tier)."""


class UnpowerableError(ValueError):
    """A machine so far down a cable that loss has dropped the delivered voltage to <= 0 - no cable
    thickness can power it at this tier (docs/DOMAIN.md voltage loss). The run must be shorter, the
    net split, or the tier raised (Phase 2 multi-source optimization)."""


def tier_voltage(tier: str) -> int:
    """The EU/t voltage of ``tier``; raises :class:`UnknownTierError` if it is not on the ladder."""
    try:
        return VOLTAGE_BY_TIER[tier]
    except KeyError:
        raise UnknownTierError(tier) from None


def delivered_voltage(tier: str, distance: int = 0) -> int:
    """Voltage reaching a machine ``distance`` cable-blocks from the source at ``tier``.

    ``tier_voltage(tier) - CABLE_LOSS_PER_BLOCK * distance`` (docs/DOMAIN.md). ``distance=0`` is
    the at-source (lossless) voltage. The result can be <= 0 for a run longer than the tier voltage
    survives; the caller treats that as unpowerable. Raises :class:`UnknownTierError` for an
    unknown tier.
    """
    return tier_voltage(tier) - CABLE_LOSS_PER_BLOCK * distance


def amperage(eut: float, tier: str, distance: int = 0) -> int:
    """Amps a machine drawing ``eut`` pulls at ``tier``, ``distance`` cable-blocks from the source.

    ``ceil(eut / delivered_voltage(tier, distance))`` - so a machine running at exactly its
    delivered voltage is 1 amp, and a draw above it is the proportional number of amps. Because
    cable loss lowers the delivered voltage, the same ``eut`` costs *more* amps the farther the
    machine sits from the source (docs/DOMAIN.md); ``distance=0`` is the at-source (lossless) draw.
    Zero iff ``eut <= 0`` (an unpowered block, or a power *source*, draws nothing). Raises
    :class:`UnknownTierError` for an unknown tier and :class:`UnpowerableError` when loss has
    dropped the delivered voltage to <= 0 (the run is too long to power at this tier).
    """
    if eut <= 0:
        return 0
    volts = delivered_voltage(tier, distance)
    if volts <= 0:
        raise UnpowerableError(
            f"{tier} voltage does not survive {distance} blocks of cable loss "
            f"({tier_voltage(tier)} - {CABLE_LOSS_PER_BLOCK * distance} <= 0)"
        )
    return math.ceil(eut / volts)
