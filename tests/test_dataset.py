"""Tests for the dataset's voltage ladder + amperage helper (what power sizing keys off)."""

from __future__ import annotations

from itertools import pairwise

import pytest

from gtnh_solver.dataset import VOLTAGE_BY_TIER, UnknownTierError, amperage, tier_voltage


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
