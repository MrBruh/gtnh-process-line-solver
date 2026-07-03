"""Tests for the dataset's voltage ladder + amp-load helpers (what power sizing keys off)."""

from __future__ import annotations

import subprocess
import sys
from itertools import pairwise

import pytest

from gtnh_solver.dataset import (
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


def test_cable_thickness_ladder_is_the_six_gt_sizes() -> None:
    # 1x/2x/4x/8x/12x/16x - GT ships a 12x rung between 8x and 16x (once missing here, which
    # over-thickened every 9..12-amp segment to 16x). Ascending order is load-bearing: the router
    # picks the first rung that carries the load.
    assert CABLE_THICKNESSES == (1, 2, 4, 8, 12, 16)
    assert MAX_CABLE_THICKNESS == 16


def test_voltage_ladder_starts_at_ulv_8_and_quadruples() -> None:
    assert VOLTAGE_BY_TIER["ULV"] == 8
    voltages = list(VOLTAGE_BY_TIER.values())
    assert all(b == 4 * a for a, b in pairwise(voltages))  # each step up the ladder is 4x


def test_tier_voltage_known_and_unknown() -> None:
    assert tier_voltage("LV") == 32
    assert tier_voltage("HV") == 512
    with pytest.raises(UnknownTierError):
        tier_voltage("OpV")  # a legacy/unsupported tier name not on the ladder


def test_amp_load_is_zero_for_unpowered() -> None:
    assert amp_load(0, "LV") == 0.0
    assert amp_load(-5, "MV") == 0.0  # a source/non-consumer never pulls amps


def test_amp_load_is_the_fractional_average_draw() -> None:
    # Machines buffer whole packets but AVERAGE a fraction of an amp: a sub-tier draw stays
    # fractional (16 EU/t at LV = half a packet per tick), and only aggregates round up.
    assert amp_load(16, "LV") == pytest.approx(0.5)  # half an amp, NOT a whole one
    assert amp_load(32, "LV") == pytest.approx(1.0)  # exactly at tier = 1 amp
    assert amp_load(33, "LV") == pytest.approx(33 / 32)  # just over one amp stays fractional
    assert amp_load(96, "MV") == pytest.approx(0.75)
    assert amp_load(256, "HV") == pytest.approx(0.5)


def test_whole_amps_rounds_summed_loads_up_once() -> None:
    # The packet quantization lives at the aggregate: three half-amp machines need 2 amps
    # (ceil(1.5)), not the 3 that per-machine rounding would charge (confirmed in game).
    assert whole_amps(3 * amp_load(16, "LV")) == 2
    assert whole_amps(0.0) == 0
    assert whole_amps(1.0) == 1  # an exact total is NOT ticked up...
    assert whole_amps(amp_load(16, "LV") + amp_load(16, "LV")) == 1  # ...even when summed
    assert whole_amps(1.000001) == 2  # a real excess still rounds up
    assert whole_amps(sum(amp_load(16, "LV", 2) for _ in range(30))) == 16  # float dust tolerated


def test_amp_load_unknown_tier_raises() -> None:
    with pytest.raises(UnknownTierError):
        amp_load(100, "NOPE")


def test_delivered_voltage_drops_one_per_block() -> None:
    assert CABLE_LOSS_PER_BLOCK == 1
    assert delivered_voltage("LV") == 32  # distance 0 == full tier voltage
    assert delivered_voltage("LV", 0) == 32
    assert delivered_voltage("LV", 5) == 27  # 32 - 5 blocks of 1-EU loss
    assert delivered_voltage("MV", 10) == 118  # 128 - 10
    with pytest.raises(UnknownTierError):
        delivered_voltage("NOPE", 3)


def test_amp_load_at_distance_grows_as_loss_bites() -> None:
    # a 32-EU/t LV machine is 1 amp at the source, but loss lowers the delivered voltage so the
    # same draw loads the net more farther out: 32 / (32 - d).
    assert amp_load(32, "LV", 0) == pytest.approx(1.0)  # 32 / 32
    assert amp_load(32, "LV", 2) == pytest.approx(32 / 30)  # any loss tips a full-tier draw over
    assert amp_load(32, "LV", 16) == pytest.approx(2.0)  # 32 / 16
    # a machine well under tier voltage has loss headroom: 16 EU/t stays within 1 amp while
    # 32 - d >= 16; a segment carrying it alone stays a 1x cable until the headroom is gone.
    assert whole_amps(amp_load(16, "LV", 16)) == 1  # 16 / 16 -> exactly 1 (last block)
    assert whole_amps(amp_load(16, "LV", 17)) == 2  # 16 / 15 -> over (headroom gone)


def test_amp_load_raises_when_loss_kills_the_voltage() -> None:
    # 32 blocks of 1-EU loss leaves an LV (32 V) run at 0 V: unpowerable at this tier/distance.
    with pytest.raises(UnpowerableError):
        amp_load(32, "LV", 32)
    with pytest.raises(UnpowerableError):
        amp_load(1, "LV", 40)
    # a source/unpowered block draws nothing regardless of distance (never reaches the check).
    assert amp_load(0, "LV", 999) == 0.0


def test_ir_and_dataset_import_cleanly_in_either_order() -> None:
    # ir is the package's import leaf: dataset imports ir (the cable ladder the output contract
    # enforces), never the reverse. A reintroduced ir -> dataset import would form a cycle that
    # only crashes on one import order - and the suite's own import order can mask it - so pin
    # both orders in fresh interpreters.
    for first, second in (("ir", "dataset"), ("dataset", "ir")):
        subprocess.run(
            [sys.executable, "-c", f"import gtnh_solver.{first}; import gtnh_solver.{second}"],
            check=True,
        )
