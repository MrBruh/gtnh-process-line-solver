"""GT:NH voltage tiers, cable loss, and the per-machine amp load they imply.

A machine draws ``eut`` EU/t at its voltage tier. GT cables **lose voltage over distance** - a
packet drops ``CABLE_LOSS_PER_BLOCK`` EU per cable block it travels - so a machine ``d`` blocks
from the source receives ``tier_voltage - d`` volts, not the full tier voltage (docs/DOMAIN.md).

A machine's draw on a shared-amperage cable is **fractional on average**: GT machines pull whole
packets (1 amp = one packet of up to tier voltage) into an internal buffer only when it has room,
so a 16 EU/t LV machine takes a 32-EU packet every other tick - an average of 0.5 amps, not a
whole amp. Its ``amp_load`` is therefore ``eut / delivered_voltage`` un-rounded: loss lowers the
arriving voltage, so a machine farther from the source draws proportionally more (the source
stays at tier and the cable is thickened to compensate). Loads **sum** along shared cable
segments, and only the aggregate is rounded up to whole amps (``whole_amps``) - per segment for
cable thickness, per tier for the source feed. Rounding per machine instead would overstate the
draw (three 0.5-amp hammers need 2 amps, not 3 - confirmed in game, docs/DOMAIN.md).

This module is the thin slice of the Phase 2 dataset lane that power sizing needs now: the
canonical GT:NH voltage ladder (EU/t, each tier 4x the last from ULV=8), the cable loss, and the
amp-load helpers. Real footprints/faces/throughput caps remain Phase 2 (docs/ROADMAP.md).
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


#: Slack for float dust when fractional amp loads are summed: a true integer total (e.g. two
#: exact half-amp machines) must not tick over to the next whole amp through rounding error.
_AMP_EPSILON = 1e-9


def amp_load(eut: float, tier: str, distance: int = 0) -> float:
    """Average amps a machine drawing ``eut`` pulls at ``tier``, ``distance`` blocks from the
    source: ``eut / delivered_voltage(tier, distance)``, un-rounded.

    Fractional on purpose - a machine buffers whole packets but *averages* a fraction of an amp
    (module docstring), and only the summed load of a shared segment or a source feed is rounded
    up to whole amps (:func:`whole_amps`). Because cable loss lowers the delivered voltage, the
    same ``eut`` loads the net *more* the farther the machine sits from the source
    (docs/DOMAIN.md); ``distance=0`` is the at-source (lossless) load. Zero iff ``eut <= 0`` (an
    unpowered block, or a power *source*, draws nothing). Raises :class:`UnknownTierError` for an
    unknown tier and :class:`UnpowerableError` when loss has dropped the delivered voltage to
    <= 0 (the run is too long to power at this tier).
    """
    if eut <= 0:
        return 0.0
    volts = delivered_voltage(tier, distance)
    if volts <= 0:
        raise UnpowerableError(
            f"{tier} voltage does not survive {distance} blocks of cable loss "
            f"({tier_voltage(tier)} - {CABLE_LOSS_PER_BLOCK * distance} <= 0)"
        )
    return eut / volts


def whole_amps(load: float) -> int:
    """The whole amps a summed fractional ``load`` needs: ``ceil``, with epsilon slack so float
    dust from summing (e.g. many ``x/31`` terms) never rounds an exact integer total upward.

    This is where the packet quantization lives: individual machines average fractional amps
    (:func:`amp_load`), but a cable segment is rated - and a source feeds - in whole packets.
    """
    return max(0, math.ceil(load - _AMP_EPSILON))
