"""Tests for the dataset's voltage ladder and the helpers power sizing keys off.

Covers the amp-load arithmetic, the energy-hatch count a draw implies (and the tier ladder a
too-hungry machine escalates up), and the hatch-cell capacity ``to_physical`` derives from an
extractor dump - the ceiling those hatches have to fit inside.
"""

from __future__ import annotations

import subprocess
import sys
from itertools import pairwise

import pytest

from gtnh_solver.dataset import (
    CABLE_LOSS_PER_BLOCK,
    CABLE_THICKNESSES,
    DESIGN_RUN_BLOCKS,
    ENERGY_HATCH_AMPS,
    MAX_CABLE_THICKNESS,
    VOLTAGE_BY_TIER,
    MachinePhysical,
    MultiblockDoc,
    UnknownTierError,
    UnpowerableError,
    amp_load,
    delivered_voltage,
    energy_hatches_for,
    tier_voltage,
    tiers_above,
    to_physical,
    whole_amps,
)
from gtnh_solver.ir import CellBox


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


def test_energy_hatch_sizing_constants_are_the_gt_values() -> None:
    # A standard GT energy hatch takes 2 A (MTEHatchEnergy.maxAmperesIn), and hatches are sized
    # against a 16-block design run because a hatch, unlike a cable, cannot be thickened later.
    assert ENERGY_HATCH_AMPS == 2
    assert DESIGN_RUN_BLOCKS == 16


def test_energy_hatches_for_a_zero_draw_machine_is_none() -> None:
    assert energy_hatches_for(0, "LV") == 0
    assert energy_hatches_for(-5, "MV") == 0  # a power source / unpowered block needs no hatch


def test_energy_hatches_for_a_normal_machine_is_one() -> None:
    # 32 EU/t at LV is exactly 2 A at the 16-block design distance (32 / 16), which is exactly one
    # 2 A hatch - the rounding slack must not tip an exact fit into a second, wasted cell.
    assert energy_hatches_for(32, "LV") == 1
    assert energy_hatches_for(16, "LV") == 1  # a sub-tier draw is still one hatch, never zero


def test_energy_hatches_for_a_heavy_machine_is_several() -> None:
    # A machine's intake is the SUM over its hatches, so a draw no single hatch covers needs
    # several: 128 EU/t at LV is 8 A after 16 blocks of loss (128 / 16), and 8 / 2 is four hatches.
    assert energy_hatches_for(128, "LV") == 4
    assert energy_hatches_for(480, "MV") == 3  # 480 / 112 = 4.29 A -> ceil(4.29 / 2)
    # a bigger hatch covers the same draw with fewer cells (TecTech's, not modelled by default)
    assert energy_hatches_for(128, "LV", hatch_amps=4) == 2


def test_energy_hatches_for_grows_with_the_design_distance() -> None:
    # Cable loss raises the amps a given EU/t costs, so the SAME machine needs more hatches the
    # farther out it is designed for: 64 EU/t is 2 A at the source but 4 A after 16 blocks.
    assert energy_hatches_for(64, "LV", distance=0) == 1
    assert energy_hatches_for(64, "LV", distance=16) == 2
    assert energy_hatches_for(64, "LV") == 2  # the default IS the 16-block design run


def test_energy_hatches_for_unknown_tier_raises() -> None:
    with pytest.raises(UnknownTierError):
        energy_hatches_for(100, "OpV")


def test_energy_hatches_for_a_run_the_tier_cannot_survive_raises() -> None:
    # 40 blocks of 1-EU loss leaves an LV (32 V) run at nothing: no number of hatches powers a
    # machine there, so sizing must fail loudly rather than return a hatch count that cannot work.
    with pytest.raises(UnpowerableError):
        energy_hatches_for(32, "LV", distance=40)


def test_tiers_above_is_the_rest_of_the_ladder_lowest_first() -> None:
    # What a machine too hungry for its own tier escalates through, cheapest step first.
    assert tiers_above("LV") == list(VOLTAGE_BY_TIER)[2:]  # everything past ULV, LV
    assert tiers_above("LV")[:3] == ["MV", "HV", "EV"]
    assert "LV" not in tiers_above("LV")  # the tier itself is not one of its own upgrades


def test_tiers_above_the_top_of_the_ladder_is_empty() -> None:
    assert tiers_above("MAX") == []  # nothing left to escalate to; a caller must not loop forever


def test_tiers_above_unknown_tier_raises() -> None:
    with pytest.raises(UnknownTierError):
        tiers_above("OpV")


# ------------------------------------------------------------------ hatch-cell capacity
#
# A GT multiblock builds its casing shell out of interchangeable cells: an input bus, an output
# hatch and an energy hatch all compete for the same pool. What the dump records about that pool
# is the ceiling the adapter's hatch allocation has to fit inside.


def _physical(*, hatch_cells: int, energy_hatch_cells: int, upkeep: int) -> MachinePhysical:
    """A bare physical record carrying just the hatch-capacity counts the budget reads."""
    return MachinePhysical(
        key="M",
        registry_name="r",
        meta=0,
        source_class="C",
        footprint=CellBox(sx=3, sy=3, sz=3),
        io_faces=frozenset(),
        hint_layers=frozenset(),
        coil_layer_count=0,
        variant_count=1,
        hatch_cells=hatch_cells,
        energy_hatch_cells=energy_hatch_cells,
        upkeep_hatch_count=upkeep,
    )


def test_energy_hatch_budget_gives_up_cells_to_upkeep_and_routed_ports() -> None:
    record = _physical(hatch_cells=10, energy_hatch_cells=10, upkeep=2)
    assert record.energy_hatch_budget() == 8  # the maintenance + muffler cells are spoken for
    assert record.energy_hatch_budget(routed_ports=3) == 5  # three buses/hatches take theirs too


def test_energy_hatch_budget_is_capped_by_the_energy_capable_cells() -> None:
    # A machine CAN restrict where power enters, so free cells in the shared pool are not enough:
    # 6 are free but only 4 will hold an energy hatch.
    record = _physical(hatch_cells=10, energy_hatch_cells=4, upkeep=2)
    assert record.energy_hatch_budget() == 4
    assert record.energy_hatch_budget(routed_ports=5) == 3  # the shared pool binds again below 4


def test_energy_hatch_budget_never_goes_negative() -> None:
    # An over-subscribed structure has no room left, not negative room - callers compare the
    # budget against a hatch count and would read a negative as "unlimited" the wrong way round.
    record = _physical(hatch_cells=4, energy_hatch_cells=4, upkeep=1)
    assert record.energy_hatch_budget(routed_ports=20) == 0


def _hatch_doc(slots: list[dict[str, object]]) -> MultiblockDoc:
    """A 3x3x3 casing shell whose recorded cells accept the given hatch kinds (raw dump facts)."""
    blocks = [
        {"d": [x, y, z], "block": "casing", "meta": 0}
        for x in range(3)
        for y in range(3)
        for z in range(3)
    ]
    return MultiblockDoc.model_validate(
        {
            "schema": 2,
            "controller": {
                "registry_name": "r",
                "meta": 0,
                "display_name": "Hatchy",
                "source_class": "C",
            },
            "variants": [
                {"trigger_stack_size": 1, "blocks": blocks, "hatch_slots": slots, "bbox": [3, 3, 3]}
            ],
        }
    )


def test_to_physical_counts_hatch_cells_energy_cells_and_upkeep() -> None:
    record = to_physical(
        _hatch_doc(
            [
                {"d": [0, 0, 0], "kinds": ["InputBus", "Energy"]},
                {"d": [1, 0, 0], "kinds": ["InputBus", "Energy"]},
                {"d": [2, 0, 0], "kinds": ["OutputHatch"]},
                {"d": [0, 0, 1], "kinds": ["Maintenance"]},
                {"d": [1, 0, 1], "kinds": ["Muffler"]},
            ]
        )
    )
    assert record.hatch_cells == 5  # every recorded cell, whatever kind it happens to accept
    assert record.energy_hatch_cells == 2  # only the two that name Energy
    assert record.upkeep_hatch_count == 2  # a maintenance hatch and a muffler, one cell each
    # 5 cells less the 2 upkeep ones leaves 3 free, but only 2 will take power, so power binds
    assert record.energy_hatch_budget() == 2
    assert record.energy_hatch_budget(routed_ports=2) == 1  # now the shared pool is the tighter one


def test_to_physical_records_a_machine_no_cell_of_which_takes_power() -> None:
    # The counts must not assume a cell that takes a bus also takes an energy hatch, or the
    # adapter would allocate hatches to cells that will not hold one.
    record = to_physical(
        _hatch_doc(
            [
                {"d": [0, 0, 0], "kinds": ["InputBus"]},
                {"d": [1, 0, 0], "kinds": ["OutputBus", "InputHatch"]},
            ]
        )
    )
    assert record.hatch_cells == 2
    assert record.energy_hatch_cells == 0
    assert record.energy_hatch_budget() == 0


def test_upkeep_is_counted_once_per_kind_not_once_per_cell() -> None:
    # Any casing cell may host the maintenance hatch, but the machine needs exactly ONE. Counting
    # the offering cells instead would budget most of the structure away.
    record = to_physical(
        _hatch_doc([{"d": [x, 0, 0], "kinds": ["Maintenance", "Energy"]} for x in range(3)])
    )
    assert record.upkeep_hatch_count == 1  # maintenance only - this machine has no muffler
    assert record.energy_hatch_budget() == 2  # 3 cells, one of them spent on maintenance


def test_a_dump_without_hatch_slots_records_no_capacity() -> None:
    # A pre-v2 dump recorded no hatch data at all. The zeros mean "unknown", which callers read as
    # "impose no ceiling" - they must not be invented from the block count.
    record = to_physical(_hatch_doc([]))
    assert (record.hatch_cells, record.energy_hatch_cells, record.upkeep_hatch_count) == (0, 0, 0)
    assert record.energy_hatch_budget() == 0


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
