"""Tests for the dataset's voltage ladder + amperage helper (what power sizing keys off)."""

from __future__ import annotations

from itertools import pairwise

import pytest

from gtnh_solver.dataset import (
    CABLE_LOSS_PER_BLOCK,
    VOLTAGE_BY_TIER,
    UnknownTierError,
    UnpowerableError,
    amperage,
    delivered_voltage,
    tier_voltage,
)


def test_voltage_ladder_starts_at_ulv_8_and_quadruples() -> None:
    assert VOLTAGE_BY_TIER["ULV"] == 8
    voltages = list(VOLTAGE_BY_TIER.values())
    assert all(b == 4 * a for a, b in pairwise(voltages))  # each step up the ladder is 4x


def test_tier_voltage_known_and_unknown() -> None:
    assert tier_voltage("LV") == 32
    assert tier_voltage("HV") == 512
    with pytest.raises(UnknownTierError):
        tier_voltage("OpV")  # a legacy/unsupported tier name not on the ladder


def test_amperage_is_zero_for_unpowered() -> None:
    assert amperage(0, "LV") == 0
    assert amperage(-5, "MV") == 0  # a source/non-consumer never pulls amps


def test_amperage_rounds_up_to_whole_amps() -> None:
    assert amperage(16, "LV") == 1  # below tier voltage (32) still draws a whole amp
    assert amperage(32, "LV") == 1  # exactly at tier = 1 amp
    assert amperage(33, "LV") == 2  # just over one amp -> two
    assert amperage(96, "MV") == 1  # 96 / 128 -> 1
    assert amperage(256, "HV") == 1  # 256 / 512 -> 1


def test_amperage_unknown_tier_raises() -> None:
    with pytest.raises(UnknownTierError):
        amperage(100, "NOPE")


def test_delivered_voltage_drops_one_per_block() -> None:
    assert CABLE_LOSS_PER_BLOCK == 1
    assert delivered_voltage("LV") == 32  # distance 0 == full tier voltage
    assert delivered_voltage("LV", 0) == 32
    assert delivered_voltage("LV", 5) == 27  # 32 - 5 blocks of 1-EU loss
    assert delivered_voltage("MV", 10) == 118  # 128 - 10
    with pytest.raises(UnknownTierError):
        delivered_voltage("NOPE", 3)


def test_amperage_at_distance_costs_more_amps_as_loss_bites() -> None:
    # a 32-EU/t LV machine is 1 amp at the source, but loss lowers the delivered voltage so the
    # same draw needs more amps farther out: ceil(32 / (32 - d)).
    assert amperage(32, "LV", 0) == 1  # 32 / 32
    assert amperage(32, "LV", 2) == 2  # 32 / 30 -> 2 (any loss tips a full-tier draw to 2 amps)
    assert amperage(32, "LV", 16) == 2  # 32 / 16 -> 2
    # a machine well under tier voltage has loss headroom: 16 EU/t stays 1 amp while 32 - d >= 16.
    assert amperage(16, "LV", 15) == 1  # 16 / 17 -> 1
    assert amperage(16, "LV", 16) == 1  # 16 / 16 -> 1 (last block of headroom)
    assert amperage(16, "LV", 17) == 2  # 16 / 15 -> 2 (headroom gone)


def test_amperage_raises_when_loss_kills_the_voltage() -> None:
    # 32 blocks of 1-EU loss leaves an LV (32 V) run at 0 V: unpowerable at this tier/distance.
    with pytest.raises(UnpowerableError):
        amperage(32, "LV", 32)
    with pytest.raises(UnpowerableError):
        amperage(1, "LV", 40)
    # a source/unpowered block draws nothing regardless of distance (never reaches the check).
    assert amperage(0, "LV", 999) == 0
