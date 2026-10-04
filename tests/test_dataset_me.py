"""The AE2 rule data ME support reads its numbers from (#331).

Three kinds of check, because the figures are trusted for different reasons:

1. **AE2's and GT's figures** (card ladders, cable capacities, GT ME hatch ids) are transcriptions
   of the pack's source, cited in ``docs/spikes/329-me-ae2.md``. The card tables are re-derived
   here from the Java's own arithmetic, so a typo in one cell cannot hide behind a comment that
   agrees with it; the rest are pinned to the note.
2. **The card choice** is a search over that data: property tests prove that the cards chosen keep
   up, that no fit with fewer cards would, and that more rate never asks for fewer cards.
3. **The device choice** is policy: the hatch policy by tier matrix, the single-block cases, and a
   property that every choice keeps up with its port or reports a shortfall that is real.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.dataset import me
from gtnh_solver.dataset.me import (
    CABLE_CAPACITY,
    GT_ME_HATCHES,
    MEDeviceChoice,
    MEShortfall,
    bus_cards_for,
    bus_per_operation,
    bus_rate,
    colours_connect,
    max_bus_rate,
    me_devices_for,
    network_ae_per_tick,
    output_bus_push_rate,
    reaches,
    single_block_fluid_push_rate,
    super_speed_allowed,
)
from gtnh_solver.ir import Commodity, IODirection
from gtnh_solver.ir.me import AEColor, MECableKind, MECards, MEDeviceKind, MEHatchPolicy

_MOVED = (Commodity.ITEM, Commodity.FLUID)
_TIERS = ("ULV", "LV", "MV", "HV", "EV", "IV", "LuV", "ZPM", "UV", "UHV")


# --- 1. the figures --------------------------------------------------------------------------------


def _java_item_operation(speed: int, superspeed: int) -> int:
    """``PartImportBus.calculateAmountToSend`` as written: a switch per card kind."""
    to_send = {1: 8, 2: 32, 3: 64, 4: 96}.get(speed, 1)
    if superspeed:
        to_send += 16 * 8 ** (superspeed - 1)
    return to_send


def _java_fluid_operation(speed: int, superspeed: int) -> int:
    """``PartFluidImportBus.calculateAmountToSend`` as written: two switches that fall through."""
    amount = 1000.0
    for case, factor in ((4, 1.5), (3, 2), (2, 4), (1, 8)):
        if speed >= case:
            amount *= factor
    for case, factor in ((4, 8), (3, 12), (2, 16), (1, 32)):
        if superspeed >= case:
            amount *= factor
    return math.floor(amount)


_FITS = [(a, s) for a in range(5) for s in range(5) if a + s <= me.UPGRADE_SLOTS]


@pytest.mark.parametrize(("speed", "superspeed"), _FITS)
def test_the_item_card_ladder_is_ae2s_arithmetic(speed: int, superspeed: int) -> None:
    cards = MECards(acceleration=speed, super_speed=superspeed)
    assert bus_per_operation(Commodity.ITEM, cards) == _java_item_operation(speed, superspeed)


@pytest.mark.parametrize(("speed", "superspeed"), _FITS)
def test_the_fluid_card_ladder_is_ae2fcs_arithmetic(speed: int, superspeed: int) -> None:
    cards = MECards(acceleration=speed, super_speed=superspeed)
    assert bus_per_operation(Commodity.FLUID, cards) == _java_fluid_operation(speed, superspeed)


def test_a_bus_runs_once_per_five_ticks() -> None:
    """Spike 4.1: the bottom of the bus tick range, where a bus with work every call settles."""
    assert me.BUS_PERIOD_TICKS == 5
    assert bus_rate(Commodity.ITEM, MECards(acceleration=4)) == pytest.approx(19.2)
    assert bus_rate(Commodity.FLUID, MECards(acceleration=4)) == pytest.approx(19200)


def test_no_bus_moves_power() -> None:
    with pytest.raises(ValueError, match="power"):
        bus_per_operation(Commodity.POWER, MECards())


def test_cable_capacities() -> None:
    """Spike 1.1: glass, covered and smart carry 8; the dense kinds 32."""
    assert CABLE_CAPACITY == {
        MECableKind.GLASS: 8,
        MECableKind.COVERED: 8,
        MECableKind.SMART: 8,
        MECableKind.DENSE: 32,
        MECableKind.DENSE_COVERED: 32,
    }
    assert {MECableKind.GLASS, MECableKind.COVERED, MECableKind.SMART} == me.PART_CABLES
    assert me.DEVICE_CAPACITY == 8
    assert me.ADHOC_MAX_DEVICES == 8


def test_the_gt_me_hatches_are_keyed_by_mid() -> None:
    """Spike 5.1: two pairs share a class, so the mID is the identity; tiers from the manifest."""
    assert {mid: (h.kind, h.hatch_kind, h.tier) for mid, h in GT_ME_HATCHES.items()} == {
        2710: (MEDeviceKind.GT_OUTPUT_BUS_ME, "OutputBus", "EV"),
        2713: (MEDeviceKind.GT_OUTPUT_HATCH_ME, "OutputHatch", "EV"),
        2718: (MEDeviceKind.GT_STOCKING_INPUT_BUS_ME, "InputBus", "EV"),
        2711: (MEDeviceKind.GT_STOCKING_INPUT_BUS_ME, "InputBus", "LuV"),
        2717: (MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME, "InputHatch", "UV"),
        2712: (MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME, "InputHatch", "UHV"),
    }
    # The output hatches flush their default buffer every 41 ticks; the stocking ones hold nothing.
    assert GT_ME_HATCHES[2710].per_tick == pytest.approx(1600 / 41)
    assert GT_ME_HATCHES[2713].per_tick == pytest.approx(128000 / 41)
    assert all(GT_ME_HATCHES[mid].per_tick is None for mid in (2711, 2712, 2717, 2718))


def test_every_hatch_kind_is_one_the_ir_knows() -> None:
    """A GT ME hatch takes a slot of its parent's kind, in ``ir.HATCH_KINDS``' own vocabulary."""
    from gtnh_solver.ir.input_ir import HATCH_KINDS

    for hatch in GT_ME_HATCHES.values():
        assert HATCH_KINDS[(hatch.commodity, hatch.direction)] == (hatch.hatch_kind,)


def test_power_figures() -> None:
    """Spike 6.1: the pack's x10 usage multiplier and 2 AE per EU."""
    assert me.USAGE_MULTIPLIER == 10.0
    assert me.AE_PER_EU == 2.0
    assert me.DEFAULT_IDLE_AE == 1.0
    assert me.CONTROLLER_IDLE_AE == 3.0


def test_item_identities() -> None:
    """Spike 7.3: cable and part damages, FC's items, the cards."""
    assert me.CABLE_DAMAGE == {
        MECableKind.GLASS: 0,
        MECableKind.COVERED: 20,
        MECableKind.SMART: 40,
        MECableKind.DENSE: 60,
        MECableKind.DENSE_COVERED: 520,
    }
    assert me.PART_DAMAGE == {
        MEDeviceKind.STORAGE_BUS: 220,
        MEDeviceKind.IMPORT_BUS: 240,
        MEDeviceKind.EXPORT_BUS: 260,
        MEDeviceKind.INTERFACE: 440,
    }
    assert me.FC_PART_ITEMS[MEDeviceKind.DUAL_INTERFACE] == "ae2fc:part_fluid_interface"
    assert me.CARD_DAMAGE == {"acceleration": 30, "super_speed": 56, "capacity": 27}
    # Every AE2 or FC part is an item one way or the other, never both.
    parts = set(me.PART_DAMAGE) | set(me.FC_PART_ITEMS)
    assert not set(me.PART_DAMAGE) & set(me.FC_PART_ITEMS)
    assert parts == {k for k in MEDeviceKind if not k.value.startswith("gt_")}


def test_every_device_has_a_name() -> None:
    assert set(me.DEVICE_NAMES) == set(MEDeviceKind)


def test_colour_ordinals() -> None:
    """Spike 7.3: White 0 through Black 15, Fluix (AE2's Transparent) 16."""
    assert [c.ordinal for c in AEColor] == list(range(17))
    assert AEColor.WHITE.ordinal == 0
    assert AEColor.BLACK.ordinal == 15
    assert AEColor.FLUIX.ordinal == 16


@given(st.sampled_from(list(AEColor)), st.sampled_from(list(AEColor)))
def test_colours_connect_when_one_is_fluix_or_both_match(a: AEColor, b: AEColor) -> None:
    assert colours_connect(a, b) is (a is AEColor.FLUIX or b is AEColor.FLUIX or a is b)
    assert colours_connect(a, b) is colours_connect(b, a)


def test_cards_validate_and_count() -> None:
    assert MECards(acceleration=2, super_speed=1, capacity=1).count == 4
    with pytest.raises(ValueError, match="less than or equal to 4"):
        MECards(acceleration=5)


# --- tiers -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "floor", "expected"),
    [("EV", "EV", True), ("IV", "EV", True), ("HV", "EV", False), ("??", "EV", False)],
)
def test_reaches(tier: str, floor: str, expected: bool) -> None:
    assert reaches(tier, floor) is expected


def test_super_speed_waits_for_luv() -> None:
    """A maintainer decision on #329: Hyper-Acceleration Cards need an LuV machine to craft."""
    assert not super_speed_allowed("IV")
    assert super_speed_allowed("LuV")
    assert super_speed_allowed("UV")
    assert not super_speed_allowed("UV", enabled=False)


# --- 2. the card choice ----------------------------------------------------------------------------


@given(
    st.sampled_from(_MOVED),
    st.floats(min_value=0, max_value=1e9, allow_nan=False),
    st.booleans(),
)
def test_chosen_cards_keep_up_and_are_the_fewest(
    commodity: Commodity, rate: float, super_speed: bool
) -> None:
    cards = bus_cards_for(commodity, rate, super_speed=super_speed)
    ceiling = max_bus_rate(commodity, super_speed=super_speed)
    if cards is None:
        assert rate > ceiling
        return
    assert bus_rate(commodity, cards) >= rate
    assert cards.capacity == 0
    assert cards.count <= me.UPGRADE_SLOTS
    if not super_speed:
        assert cards.super_speed == 0
    # Minimal: no fit with fewer cards keeps up, nor one as small with fewer Hyper-Acceleration.
    for a, s in _FITS:
        if s and not super_speed:
            continue
        fewer = a + s < cards.count or (a + s == cards.count and s < cards.super_speed)
        if fewer:
            assert bus_rate(commodity, MECards(acceleration=a, super_speed=s)) < rate


@given(
    st.sampled_from(_MOVED),
    st.floats(min_value=0, max_value=1e9, allow_nan=False),
    st.floats(min_value=0, max_value=1e9, allow_nan=False),
    st.booleans(),
)
def test_more_rate_never_takes_fewer_cards(
    commodity: Commodity, a: float, b: float, super_speed: bool
) -> None:
    low, high = sorted((a, b))
    at_low = bus_cards_for(commodity, low, super_speed=super_speed)
    at_high = bus_cards_for(commodity, high, super_speed=super_speed)
    if at_high is None:
        return
    assert at_low is not None
    assert at_low.count <= at_high.count


def test_a_bus_needs_no_card_for_its_base_rate() -> None:
    assert bus_cards_for(Commodity.ITEM, 0.2) == MECards()
    assert bus_cards_for(Commodity.ITEM, 0.21) == MECards(acceleration=1)
    assert bus_cards_for(Commodity.ITEM, 20, super_speed=False) is None


# --- pushers and power -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "items"), [("ULV", 8), ("LV", 32), ("MV", 72), ("HV", 128), ("EV", 200), ("??", 8)]
)
def test_a_normal_output_bus_pushes_its_slots_every_eight_ticks(tier: str, items: float) -> None:
    """Spike 4.8: ``(tier + 1)^2`` slots of 64 every 8 ticks; an unknown tier reads as the least."""
    assert output_bus_push_rate(tier) == items


def test_a_single_block_pushes_a_bucket_per_completion_and_every_twenty_ticks() -> None:
    """Spike 4.7: a 20-tick recipe pushes 1000 mB on completion and 1000 mB every 20 ticks."""
    assert single_block_fluid_push_rate(20) == pytest.approx(100)
    assert single_block_fluid_push_rate(100) == pytest.approx(60)


def test_network_power_is_the_hand_trace() -> None:
    """Spike 2.7's controller trace: channel load 88, so 0.6875 AE/t of channels before x10."""
    assert network_ae_per_tick(idle=0, channel_load=88) == pytest.approx(6.875)
    assert network_ae_per_tick(idle=3 + 9) == pytest.approx(120)
    assert network_ae_per_tick(idle=0, items_per_tick=2) == pytest.approx(20)
    assert network_ae_per_tick(idle=0, fluid_operations_per_tick=0.5) == pytest.approx(5)
    assert me.ae_to_eu(120) == pytest.approx(60)


# --- 3. the device choice --------------------------------------------------------------------------


def _choose(
    commodity: Commodity,
    direction: IODirection,
    rate: float | None,
    **kw: object,
) -> tuple[MEDeviceChoice, ...]:
    defaults: dict[str, object] = {"multiblock": True, "machine_tier": "EV", "line_tier": "EV"}
    chosen = me_devices_for(commodity, direction, rate, **{**defaults, **kw})  # type: ignore[arg-type]
    assert isinstance(chosen, tuple), chosen
    return chosen


@pytest.mark.parametrize(
    ("policy", "line_tier", "commodity", "direction", "kind"),
    [
        # tier_aware: the GT hatch once the line reaches its tier, a normal hatch with a part before
        (
            MEHatchPolicy.TIER_AWARE,
            "HV",
            Commodity.ITEM,
            IODirection.OUTPUT,
            MEDeviceKind.INTERFACE,
        ),
        (
            MEHatchPolicy.TIER_AWARE,
            "EV",
            Commodity.ITEM,
            IODirection.OUTPUT,
            MEDeviceKind.GT_OUTPUT_BUS_ME,
        ),
        (
            MEHatchPolicy.TIER_AWARE,
            "EV",
            Commodity.FLUID,
            IODirection.OUTPUT,
            MEDeviceKind.GT_OUTPUT_HATCH_ME,
        ),
        (
            MEHatchPolicy.TIER_AWARE,
            "EV",
            Commodity.ITEM,
            IODirection.INPUT,
            MEDeviceKind.GT_STOCKING_INPUT_BUS_ME,
        ),
        # the stocking input hatch is UV
        (
            MEHatchPolicy.TIER_AWARE,
            "ZPM",
            Commodity.FLUID,
            IODirection.INPUT,
            MEDeviceKind.FLUID_EXPORT_BUS,
        ),
        (
            MEHatchPolicy.TIER_AWARE,
            "UV",
            Commodity.FLUID,
            IODirection.INPUT,
            MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME,
        ),
        # always: whatever the tier
        (
            MEHatchPolicy.ALWAYS,
            "LV",
            Commodity.FLUID,
            IODirection.OUTPUT,
            MEDeviceKind.GT_OUTPUT_HATCH_ME,
        ),
        (
            MEHatchPolicy.ALWAYS,
            "LV",
            Commodity.FLUID,
            IODirection.INPUT,
            MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME,
        ),
        # never: a normal hatch and the part in front of it
        (MEHatchPolicy.NEVER, "UHV", Commodity.ITEM, IODirection.OUTPUT, MEDeviceKind.INTERFACE),
        (
            MEHatchPolicy.NEVER,
            "UHV",
            Commodity.FLUID,
            IODirection.OUTPUT,
            MEDeviceKind.DUAL_INTERFACE,
        ),
        (MEHatchPolicy.NEVER, "UHV", Commodity.ITEM, IODirection.INPUT, MEDeviceKind.EXPORT_BUS),
        (
            MEHatchPolicy.NEVER,
            "UHV",
            Commodity.FLUID,
            IODirection.INPUT,
            MEDeviceKind.FLUID_EXPORT_BUS,
        ),
    ],
)
def test_the_hatch_policy_by_tier(
    policy: MEHatchPolicy,
    line_tier: str,
    commodity: Commodity,
    direction: IODirection,
    kind: MEDeviceKind,
) -> None:
    (device,) = _choose(commodity, direction, 1.0, line_tier=line_tier, hatches=policy)
    assert device.kind is kind
    # On a multiblock every device takes a hatch slot: its own, or the normal hatch it faces.
    hatch = GT_ME_HATCHES.get(device.gt_mid) if device.gt_mid is not None else None
    expected_slot = {
        (Commodity.ITEM, IODirection.OUTPUT): "OutputBus",
        (Commodity.FLUID, IODirection.OUTPUT): "OutputHatch",
        (Commodity.ITEM, IODirection.INPUT): "InputBus",
        (Commodity.FLUID, IODirection.INPUT): "InputHatch",
    }[(commodity, direction)]
    assert device.hatch_kind == expected_slot
    assert (hatch is not None) is device.kind.value.startswith("gt_")


def test_tier_aware_prefers_one_normal_hatch_over_two_gt_hatches() -> None:
    """A GT Output Bus (ME) flushes 39 items/t; a normal EV bus pushes 200 into an interface."""
    (device,) = _choose(Commodity.ITEM, IODirection.OUTPUT, 50.0)
    assert (device.kind, device.hatch_kind, device.per_tick) == (
        MEDeviceKind.INTERFACE,
        "OutputBus",
        200,
    )
    assert device.label == "Output Bus + ME Interface"


def test_always_tries_two_gt_hatches_before_a_normal_one() -> None:
    devices = _choose(Commodity.ITEM, IODirection.OUTPUT, 50.0, hatches=MEHatchPolicy.ALWAYS)
    assert [d.gt_mid for d in devices] == [2710, 2710]
    (device,) = _choose(Commodity.ITEM, IODirection.OUTPUT, 100.0, hatches=MEHatchPolicy.ALWAYS)
    assert device.kind is MEDeviceKind.INTERFACE


def test_a_single_block_input_is_an_export_bus_carded_to_its_rate() -> None:
    (device,) = _choose(Commodity.ITEM, IODirection.INPUT, 5.0, multiblock=False, machine_tier="LV")
    assert device.kind is MEDeviceKind.EXPORT_BUS
    assert device.hatch_kind is None
    assert device.cards == MECards(acceleration=2)
    assert device.label == "ME Export Bus (2 x Acceleration Card)"


def test_an_lv_line_splits_across_two_buses_rather_than_take_hyper_cards() -> None:
    devices = _choose(Commodity.ITEM, IODirection.INPUT, 25.0, multiblock=False, line_tier="LV")
    assert [d.cards for d in devices] == [MECards(acceleration=3)] * 2
    (device,) = _choose(Commodity.ITEM, IODirection.INPUT, 25.0, multiblock=False, line_tier="LuV")
    assert device.cards == MECards(super_speed=2)
    assert device.label == "ME Export Bus (2 x Hyper-Acceleration Card)"
    turned_off = _choose(
        Commodity.ITEM,
        IODirection.INPUT,
        25.0,
        multiblock=False,
        line_tier="LuV",
        super_speed=False,
    )
    assert len(turned_off) == 2


def test_an_auto_output_goes_into_an_interface() -> None:
    (items,) = _choose(Commodity.ITEM, IODirection.OUTPUT, 3.0, multiblock=False, auto_output=True)
    assert (items.kind, items.per_tick) == (MEDeviceKind.INTERFACE, None)
    (fluid,) = _choose(
        Commodity.FLUID, IODirection.OUTPUT, 30.0, multiblock=False, auto_output=True
    )
    assert fluid.kind is MEDeviceKind.DUAL_INTERFACE


def test_a_fluid_output_faster_than_gt_pushes_it_is_pulled_by_a_bus() -> None:
    """Spike 4.7: a single block pushes 1000 mB a time, so a faster output is drained instead."""
    push = single_block_fluid_push_rate(16)
    (slow,) = _choose(
        Commodity.FLUID,
        IODirection.OUTPUT,
        100.0,
        multiblock=False,
        auto_output=True,
        push_rate=push,
    )
    assert slow.kind is MEDeviceKind.DUAL_INTERFACE
    (fast,) = _choose(
        Commodity.FLUID,
        IODirection.OUTPUT,
        125.0,
        multiblock=False,
        auto_output=True,
        push_rate=push,
    )
    assert fast.kind is MEDeviceKind.FLUID_IMPORT_BUS


def test_an_output_that_does_not_auto_output_is_pulled_by_an_import_bus() -> None:
    (device,) = _choose(Commodity.ITEM, IODirection.OUTPUT, 1.0, multiblock=False)
    assert device.kind is MEDeviceKind.IMPORT_BUS


def test_an_unrated_port_gets_the_plain_device() -> None:
    (device,) = _choose(Commodity.ITEM, IODirection.INPUT, None, multiblock=False)
    assert device.cards == MECards()


def test_a_port_two_devices_cannot_serve_is_a_shortfall() -> None:
    short = me_devices_for(
        Commodity.ITEM,
        IODirection.OUTPUT,
        1000.0,
        multiblock=True,
        machine_tier="LV",
        line_tier="LV",
        hatches=MEHatchPolicy.ALWAYS,
    )
    assert isinstance(short, MEShortfall)
    assert short.capacity == pytest.approx(2 * 1600 / 41)
    assert "1000 items/t" in short.detail


def test_power_is_never_an_me_port() -> None:
    with pytest.raises(ValueError, match="never power"):
        me_devices_for(
            Commodity.POWER,
            IODirection.INPUT,
            1.0,
            multiblock=False,
            machine_tier="LV",
            line_tier="LV",
        )


@given(
    commodity=st.sampled_from(_MOVED),
    direction=st.sampled_from(list(IODirection)),
    rate=st.floats(min_value=0, max_value=1e8, allow_nan=False),
    multiblock=st.booleans(),
    machine_tier=st.sampled_from(_TIERS),
    line_tier=st.sampled_from(_TIERS),
    hatches=st.sampled_from(list(MEHatchPolicy)),
    auto_output=st.booleans(),
    push_rate=st.none() | st.floats(min_value=0, max_value=1e4, allow_nan=False),
)
def test_every_choice_keeps_up_or_is_a_real_shortfall(
    commodity: Commodity,
    direction: IODirection,
    rate: float,
    multiblock: bool,
    machine_tier: str,
    line_tier: str,
    hatches: MEHatchPolicy,
    auto_output: bool,
    push_rate: float | None,
) -> None:
    chosen = me_devices_for(
        commodity,
        direction,
        rate,
        multiblock=multiblock,
        machine_tier=machine_tier,
        line_tier=line_tier,
        hatches=hatches,
        auto_output=auto_output,
        push_rate=push_rate,
    )
    if isinstance(chosen, MEShortfall):
        assert chosen.capacity < rate
        return
    assert 1 <= len(chosen) <= me.MAX_DEVICES_PER_PORT
    assert len(set(chosen)) == 1  # equal devices, each an equal share
    device = chosen[0]
    if device.per_tick is not None:
        assert device.per_tick * len(chosen) >= rate * (1 - 1e-12)
    if device.cards.super_speed:
        assert super_speed_allowed(line_tier)
    # A multiblock's every connection takes a hatch slot; a single block's none.
    assert (device.hatch_kind is not None) is multiblock
    if hatches is MEHatchPolicy.NEVER:
        assert device.gt_mid is None
    if device.gt_mid is not None and hatches is MEHatchPolicy.TIER_AWARE:
        assert reaches(line_tier, GT_ME_HATCHES[device.gt_mid].tier)
