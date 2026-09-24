"""Tests for the IR contracts (docs/IR.md).

The IR guarantees structural well-formedness + referential integrity; it deliberately
does NOT do geometric/rule checking (that is the validator's independent job). These
tests pin both the guarantees and the non-guarantees.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from gtnh_solver.ir import (
    INPUT_IR_VERSION,
    LAYOUT_RESULT_VERSION,
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    Infeasibility,
    InputIR,
    IODirection,
    LayoutMetrics,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    PinnedIO,
    PipeFamily,
    PipeSize,
    Placement,
    Port,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)
from gtnh_solver.ir._base import FrozenModel, StrictModel

# --------------------------------------------------------------------------- helpers


def _machine(
    mid: str = "m1",
    *,
    ports: list[Port] | None = None,
    orientations: list[Facing] | None = None,
    eut: float = 0.0,
    hatch_cells: int | None = None,
) -> Machine:
    if ports is None:
        ports = [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
    if orientations is None:
        orientations = [Facing.NORTH, Facing.SOUTH]
    return Machine(
        id=mid,
        type="gt.macerator",
        footprint=CellBox(sx=1, sy=1, sz=1),
        faces=FaceSpec(ports=ports),
        voltage_tier="LV",
        orientation_options=orientations,
        eut=eut,
        hatch_cells=hatch_cells,
    )


def _hatch(pid: str, *, rate: float | None = None, max_amps: float | None = None) -> Port:
    """One energy hatch: a power INPUT port, optionally carrying its share of the draw."""
    return Port(
        id=pid,
        commodity=Commodity.POWER,
        direction=IODirection.INPUT,
        rate=rate,
        max_amps=max_amps,
    )


def _valid_input_ir() -> InputIR:
    return InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_machine("m1"), _machine("m2")],
        nets=[
            Net(
                id="n1",
                commodity=Commodity.ITEM,
                fluid_or_item="gt.dust.iron",
                throughput=2.0,
                endpoints=[
                    MachineFaceRef(machine_id="m1", port_id="out"),
                    MachineFaceRef(machine_id="m2", port_id="out"),
                ],
            )
        ],
        pinned=[PinnedIO(net_id="n1", cell=CellCoord(x=0, y=0, z=0), kind=IODirection.OUTPUT)],
        reserved_cells=[CellCoord(x=7, y=0, z=7)],
        me_toggles=METoggles(fluids=True),
    )


def _valid_layout() -> LayoutResult:
    return LayoutResult(
        status=LayoutStatus.VALID,
        placements=[
            Placement(machine_id="m1", cell=CellCoord(x=1, y=0, z=1), orientation=Facing.NORTH)
        ],
        routes=[
            Route(
                net_id="n1",
                commodity=Commodity.ITEM,
                segments=[
                    Segment(start=CellCoord(x=1, y=0, z=1), end=CellCoord(x=2, y=0, z=1), channel=0)
                ],
            )
        ],
        metrics=LayoutMetrics(footprint=4, layers=1),
        seed=1234,
    )


# --------------------------------------------------------------------------- geometry


def test_cellbox_dims_must_be_positive() -> None:
    assert CellBox(sx=2, sy=3, sz=4).volume == 24
    with pytest.raises(ValidationError):
        CellBox(sx=0, sy=1, sz=1)


def test_cellcoord_is_frozen_and_hashable() -> None:
    c = CellCoord(x=1, y=2, z=3)
    assert c in {CellCoord(x=1, y=2, z=3)}  # value equality + hashability
    with pytest.raises(ValidationError):
        c.x = 9  # type: ignore[misc]  # frozen: mypy agrees statically, this pins it at runtime


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CellCoord(x=1, y=2, z=3, w=4)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- faces / ports


def test_facespec_rejects_duplicate_port_ids() -> None:
    with pytest.raises(ValidationError):
        FaceSpec(
            ports=[
                Port(id="p", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
                Port(id="p", commodity=Commodity.FLUID, direction=IODirection.INPUT),
            ]
        )


def test_port_rejects_unknown_field() -> None:
    # is_auto_output was dropped in InputIR v2 (auto-output is a solver decision, recorded in the
    # output's AutoConnection); StrictModel forbids extras, so passing it now fails loud.
    with pytest.raises(ValidationError):
        Port(
            id="o",
            commodity=Commodity.ITEM,
            direction=IODirection.OUTPUT,
            is_auto_output=True,  # type: ignore[call-arg]
        )


def test_port_rate_is_optional_and_non_negative() -> None:
    # additive in InputIR v2: the throughput a port moves, for boundary I/O reporting (#16)
    c, d = Commodity.ITEM, IODirection.OUTPUT
    assert Port(id="o", commodity=c, direction=d).rate is None  # defaults to unknown
    assert Port(id="o", commodity=c, direction=d, rate=2.5).rate == 2.5
    with pytest.raises(ValidationError):
        Port(id="o", commodity=c, direction=d, rate=-1.0)  # a rate cannot be negative


def test_port_max_amps_is_optional_and_strictly_positive() -> None:
    # additive in InputIR v3: a GT energy hatch takes 2 A, a per-connection ceiling the machine's
    # whole-machine eut cannot express. A hatch that accepted 0 A would take in no power at all,
    # so zero is a malformed ceiling rather than a tight one.
    assert _hatch("pi").max_amps is None  # defaults to no per-connection ceiling
    assert _hatch("pi", max_amps=2.0).max_amps == 2.0
    with pytest.raises(ValidationError):
        _hatch("pi", max_amps=0.0)
    with pytest.raises(ValidationError):
        _hatch("pi", max_amps=-2.0)


# --------------------------------------------------------------------------- machine


def test_machine_requires_an_orientation_option() -> None:
    with pytest.raises(ValidationError):
        _machine(orientations=[])


def test_machine_rejects_duplicate_orientations() -> None:
    with pytest.raises(ValidationError):
        _machine(orientations=[Facing.NORTH, Facing.NORTH])


def test_machine_orientation_must_be_horizontal() -> None:
    # GT machines never face up/down; the front is always a horizontal direction.
    with pytest.raises(ValidationError):
        _machine(orientations=[Facing.UP])
    with pytest.raises(ValidationError):
        _machine(orientations=[Facing.NORTH, Facing.DOWN])


def test_machine_no_longer_accepts_count() -> None:
    # `count` was dropped in InputIR v1 (multi-instance machines are Phase 2); StrictModel
    # forbids unknown fields, so a stray `count=` is now a loud error, not silently ignored.
    with pytest.raises(ValidationError):
        Machine(  # type: ignore[call-arg]  # `count` is gone from the model, which is the point
            id="m",
            type="t",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            count=1,
        )


def test_machine_hatch_cells_is_optional_and_non_negative() -> None:
    # additive in InputIR v3. None is "unknown" (a single-block machine, or a plan adapted without
    # the physical dataset) and imposes no ceiling - a distinct state from a structure with zero
    # cells, which can host nothing at all.
    assert _machine().hatch_cells is None
    assert _machine(hatch_cells=0).hatch_cells == 0
    with pytest.raises(ValidationError):
        _machine(hatch_cells=-1)


def test_power_input_ports_are_the_energy_hatches_in_declaration_order() -> None:
    m = _machine(
        ports=[
            Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            _hatch("pi2"),
            Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT),
            _hatch("pi1"),
        ]
    )
    # only the power INPUTs: an item port is not a hatch, and a source's power OUTPUT supplies
    # power rather than taking it in. Declaration order, so a caller can zip rates onto them.
    assert [p.id for p in m.power_input_ports] == ["pi2", "pi1"]


def test_port_eut_falls_back_to_the_whole_draw_for_an_unrated_port() -> None:
    # Every pre-v3 problem left power ports unrated, and a single-hatch machine still does: the
    # one connection carries the whole draw, so it must size from ``eut`` rather than from nothing.
    m = _machine(ports=[_hatch("pi")], eut=48.0)
    assert m.port_eut("pi") == 48.0


def test_port_eut_returns_the_ports_own_share_when_rated() -> None:
    # A machine spreading its draw over several hatches charges each cable only that hatch's
    # share; billing every one the whole eut would multiply the net's load by the hatch count.
    m = _machine(ports=[_hatch("pi1", rate=30.0), _hatch("pi2", rate=18.0)], eut=48.0)
    assert m.port_eut("pi1") == 30.0
    assert m.port_eut("pi2") == 18.0


def test_port_eut_of_an_unknown_port_is_zero() -> None:
    # A stale or foreign port id draws nothing, rather than silently billing the whole machine to
    # a connection that does not exist.
    assert _machine(ports=[_hatch("pi")], eut=48.0).port_eut("ghost") == 0.0


def test_power_input_rates_must_be_all_set_or_none() -> None:
    # A hatch left off the books would be a cable nothing sizes, so the half-rated machine is
    # rejected rather than silently sized from the one rate it does carry.
    with pytest.raises(ValidationError):
        _machine(ports=[_hatch("pi1", rate=48.0), _hatch("pi2")], eut=48.0)


def test_power_input_rates_must_sum_to_the_machines_draw() -> None:
    with pytest.raises(ValidationError):
        # the two hatches account for only 36 of the 48 EU/t: 12 EU/t would arrive over no cable
        _machine(ports=[_hatch("pi1", rate=24.0), _hatch("pi2", rate=12.0)], eut=48.0)
    with pytest.raises(ValidationError):
        # and over-declaring double-charges the net, which is just as wrong as under-declaring
        _machine(ports=[_hatch("pi1", rate=48.0), _hatch("pi2", rate=48.0)], eut=48.0)


def test_a_correctly_split_power_draw_is_accepted() -> None:
    # Three hatches carrying a third of the draw each. 100/3 does not sum back to 100 exactly in
    # binary, so the rule has to tolerate float dust instead of demanding equality - constructing
    # the machine without a ValidationError is the assertion.
    m = _machine(ports=[_hatch(f"pi{i}", rate=100 / 3) for i in range(3)], eut=100.0)
    assert m.port_eut("pi0") == pytest.approx(100 / 3)


def test_a_power_sources_output_rate_is_not_a_draw_to_account_for() -> None:
    # The rule polices energy hatches (power INPUTs). A source's OUTPUT rate is the power it
    # supplies, which has nothing to sum to its own (zero) draw.
    m = _machine(
        ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT, rate=512.0)],
        eut=0.0,
    )
    assert m.power_input_ports == []


# --------------------------------------------------------------------------- net


def test_power_net_must_not_name_a_commodity() -> None:
    with pytest.raises(ValidationError):
        Net(
            id="p",
            commodity=Commodity.POWER,
            fluid_or_item="oops",
            throughput=32.0,
            endpoints=[MachineFaceRef(machine_id="m", port_id="pwr")],
        )


def test_item_net_must_name_a_commodity() -> None:
    with pytest.raises(ValidationError):
        Net(
            id="i",
            commodity=Commodity.ITEM,
            fluid_or_item=None,
            throughput=1.0,
            endpoints=[MachineFaceRef(machine_id="m", port_id="out")],
        )


def test_net_requires_an_endpoint() -> None:
    with pytest.raises(ValidationError):
        Net(id="n", commodity=Commodity.ITEM, fluid_or_item="x", throughput=1.0, endpoints=[])


def test_net_throughput_non_negative() -> None:
    with pytest.raises(ValidationError):
        Net(
            id="n",
            commodity=Commodity.ITEM,
            fluid_or_item="x",
            throughput=-1.0,
            endpoints=[MachineFaceRef(machine_id="m", port_id="out")],
        )


# --------------------------------------------------------------- InputIR referential integrity


def test_valid_input_ir_builds_and_defaults_version() -> None:
    ir = _valid_input_ir()
    assert ir.version == INPUT_IR_VERSION


@pytest.mark.parametrize("version", [INPUT_IR_VERSION - 1, INPUT_IR_VERSION + 1])
def test_input_ir_rejects_a_payload_from_another_contract_version(version: int) -> None:
    """A mismatched ``version`` is refused rather than read as if it agreed (#38).

    ``extra="forbid"`` catches a payload carrying fields this build does not know; it cannot catch
    a bump that changed what an existing field MEANS. InputIR v3 is exactly that case - a power
    port's ``rate`` became its own connection's share of the draw rather than nothing - so a v2
    payload and a v3 consumer disagree by a multiple with every field present and well-typed. A
    *newer* version is refused just as firmly: not knowing what changed is the reason to refuse,
    not a reason to hope.
    """
    payload = _valid_input_ir().model_dump()
    payload["version"] = version
    with pytest.raises(ValidationError, match=f"contract version {version}"):
        InputIR.model_validate(payload)


def test_input_ir_round_trips_through_its_own_serialization() -> None:
    # The guard must not reject this build's own output. The round trip is what every consumer
    # actually does, so a version check that broke it would be worse than no check at all.
    ir = _valid_input_ir()
    assert InputIR.model_validate_json(ir.model_dump_json()) == ir


def test_duplicate_machine_id_rejected() -> None:
    with pytest.raises(ValidationError):
        InputIR(bounding_region=CellBox(sx=2, sy=2, sz=2), machines=[_machine("m"), _machine("m")])


def test_duplicate_net_id_rejected() -> None:
    n = Net(
        id="dup",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[MachineFaceRef(machine_id="m1", port_id="out")],
    )
    with pytest.raises(ValidationError):
        InputIR(bounding_region=CellBox(sx=2, sy=2, sz=2), machines=[_machine("m1")], nets=[n, n])


def test_net_referencing_unknown_machine_rejected() -> None:
    with pytest.raises(ValidationError):
        InputIR(
            bounding_region=CellBox(sx=2, sy=2, sz=2),
            machines=[_machine("m1")],
            nets=[
                Net(
                    id="n",
                    commodity=Commodity.ITEM,
                    fluid_or_item="x",
                    throughput=1.0,
                    endpoints=[MachineFaceRef(machine_id="ghost", port_id="out")],
                )
            ],
        )


def test_net_referencing_unknown_port_rejected() -> None:
    with pytest.raises(ValidationError):
        InputIR(
            bounding_region=CellBox(sx=2, sy=2, sz=2),
            machines=[_machine("m1")],
            nets=[
                Net(
                    id="n",
                    commodity=Commodity.ITEM,
                    fluid_or_item="x",
                    throughput=1.0,
                    endpoints=[MachineFaceRef(machine_id="m1", port_id="nope")],
                )
            ],
        )


def test_net_commodity_must_match_port() -> None:
    fluid_machine = _machine(
        "m1", ports=[Port(id="out", commodity=Commodity.FLUID, direction=IODirection.OUTPUT)]
    )
    with pytest.raises(ValidationError):
        InputIR(
            bounding_region=CellBox(sx=2, sy=2, sz=2),
            machines=[fluid_machine],
            nets=[
                Net(
                    id="n",
                    commodity=Commodity.ITEM,  # port is FLUID
                    fluid_or_item="x",
                    throughput=1.0,
                    endpoints=[MachineFaceRef(machine_id="m1", port_id="out")],
                )
            ],
        )


def test_pinned_io_referencing_unknown_net_rejected() -> None:
    with pytest.raises(ValidationError):
        InputIR(
            bounding_region=CellBox(sx=2, sy=2, sz=2),
            machines=[_machine("m1")],
            pinned=[
                PinnedIO(net_id="ghost", cell=CellCoord(x=0, y=0, z=0), kind=IODirection.INPUT)
            ],
        )


# --------------------------------------------------------------------------- output schema


def test_power_route_requires_aligned_thickness() -> None:
    segs = [Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=0)]
    Route(net_id="p", commodity=Commodity.POWER, segments=segs, thickness_per_segment=[4])
    with pytest.raises(ValidationError):  # missing thickness
        Route(net_id="p", commodity=Commodity.POWER, segments=segs)
    with pytest.raises(ValidationError):  # misaligned length
        Route(net_id="p", commodity=Commodity.POWER, segments=segs, thickness_per_segment=[4, 8])
    with pytest.raises(ValidationError):  # not a power-of-two-ish tier
        Route(net_id="p", commodity=Commodity.POWER, segments=segs, thickness_per_segment=[3])


def test_non_power_route_must_not_carry_thickness() -> None:
    segs = [Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=0)]
    with pytest.raises(ValidationError):
        Route(net_id="i", commodity=Commodity.ITEM, segments=segs, thickness_per_segment=[1])


def test_route_cells_are_its_segment_ends_or_else_its_terminals_cell() -> None:
    """A one-block pipe (v3) has no segment, so its block is the cell its terminals share; every
    consumer reads blocks through ``cells()``, and one that walked ``segments`` would build nothing.
    With segments, the terminals add nothing: the validator already holds them on the route."""
    shared = CellCoord(x=1, y=0, z=0)
    terminals = [
        Terminal(machine_id="a", port_id="out", face=Facing.EAST, cell=shared),
        Terminal(machine_id="b", port_id="in", face=Facing.WEST, cell=shared),
    ]
    one_block = Route(net_id="i", commodity=Commodity.ITEM, terminals=terminals)
    assert one_block.cells() == {(1, 0, 0)}

    segs = [Segment(start=CellCoord(x=0, y=0, z=0), end=shared, channel=0)]
    far = Terminal(machine_id="c", port_id="in", face=Facing.UP, cell=CellCoord(x=5, y=5, z=5))
    run = Route(net_id="i", commodity=Commodity.ITEM, terminals=[far], segments=segs)
    assert run.cells() == {(0, 0, 0), (1, 0, 0)}
    assert Route(net_id="i", commodity=Commodity.ITEM).cells() == set()


def _cable(**over: object) -> RouteMaterial:
    kwargs: dict[str, object] = {"family": PipeFamily.CABLE, "material": "tin", "tier": "LV"}
    kwargs.update(over)
    return RouteMaterial(**kwargs)


def _power(material: RouteMaterial | None) -> Route:
    return Route(net_id="p", commodity=Commodity.POWER, thickness_per_segment=[], material=material)


def test_route_material_is_optional_and_absent_means_unspecified() -> None:
    """The additive rule's own test: a route without one is exactly as valid as it always was.

    Every hand-built ``Route`` in this suite, the golden corpus, and any layout produced before the
    field existed rely on this - which is the argument for not bumping LAYOUT_RESULT_VERSION.
    """
    assert _power(None).material is None
    assert Route(net_id="i", commodity=Commodity.ITEM).material is None


def _pipe(family: PipeFamily = PipeFamily.ITEM_PIPE, **over: object) -> RouteMaterial:
    kwargs: dict[str, object] = {"family": family, "material": "tin", "size": PipeSize.NORMAL}
    kwargs.update(over)
    return RouteMaterial(**kwargs)


def test_route_material_family_must_match_its_commodity() -> None:
    assert _power(_cable()).material == _cable()
    item = _pipe()
    assert Route(net_id="i", commodity=Commodity.ITEM, material=item).material is item
    fluid = _pipe(PipeFamily.FLUID_PIPE, material="bronze")
    assert Route(net_id="f", commodity=Commodity.FLUID, material=fluid).material is fluid
    with pytest.raises(ValidationError):  # an item pipe cannot carry power
        _power(_pipe())
    with pytest.raises(ValidationError):  # nor a cable items
        Route(net_id="i", commodity=Commodity.ITEM, material=_cable())


def test_route_material_tier_is_a_cable_fact_only() -> None:
    """A tier rates a cable's gauge ladder. A pipe has no voltage, so a tier on one is a producer
    filling the field in by rote rather than because it knew something."""
    with pytest.raises(ValidationError):
        _power(_cable(tier=None))
    with pytest.raises(ValidationError, match="tier"):
        Route(net_id="i", commodity=Commodity.ITEM, material=_pipe(tier="LV"))


def test_route_material_size_is_a_pipe_fact_only() -> None:
    """The mirror of ``tier`` (LayoutResult v2, #165). A pipe with no size is the silent "normal"
    every v1 route built, which starved two machines of three in game, so a producer must name one;
    a cable's gauge is its per-segment thickness, so a size on one is filled in by rote."""
    with pytest.raises(ValidationError, match="size is required on pipes"):
        Route(net_id="i", commodity=Commodity.ITEM, material=_pipe(size=None))
    with pytest.raises(ValidationError, match="size is required on pipes"):
        Route(
            net_id="f",
            commodity=Commodity.FLUID,
            material=_pipe(PipeFamily.FLUID_PIPE, material="bronze", size=None),
        )
    with pytest.raises(ValidationError, match="invalid on cables"):
        _power(_cable(size=PipeSize.NORMAL))
    with pytest.raises(ValidationError):  # the ladder is closed: no size GT does not build
        _pipe(size="quadruple")


def test_route_material_size_round_trips_as_its_plain_name() -> None:
    route = Route(net_id="i", commodity=Commodity.ITEM, material=_pipe(size=PipeSize.HUGE))
    assert '"size":"huge"' in route.model_dump_json()
    again = Route.model_validate_json(route.model_dump_json())
    assert again.material is not None
    assert again.material.size is PipeSize.HUGE
    assert again == route


def test_route_material_must_admit_it_is_a_stand_in() -> None:
    """v1 chooses a representative material and never claims a real one - the flag is what a
    ``.schematic`` exporter refuses on, so a producer cannot quietly clear it."""
    assert _power(_cable()).material is not None
    with pytest.raises(ValidationError):
        _power(_cable(stand_in=False))


def test_route_material_round_trips() -> None:
    route = _power(_cable(material="niobiumtitanium", tier="LuV"))
    again = Route.model_validate_json(route.model_dump_json())
    assert again.material is not None
    assert again.material.material == "niobiumtitanium"
    assert again.material.family is PipeFamily.CABLE
    assert again == route


def test_segment_channel_non_negative() -> None:
    with pytest.raises(ValidationError):
        Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=-1)


def test_valid_layout_has_no_infeasibility() -> None:
    assert _valid_layout().version == LAYOUT_RESULT_VERSION
    with pytest.raises(ValidationError):  # valid + infeasibility is contradictory
        LayoutResult(
            status=LayoutStatus.VALID,
            seed=1,
            infeasibility=Infeasibility(constraint="c", detail="d"),
        )


@pytest.mark.parametrize("version", [LAYOUT_RESULT_VERSION - 1, LAYOUT_RESULT_VERSION + 1])
def test_layout_result_rejects_a_payload_from_another_contract_version(version: int) -> None:
    # The output schema is a first-class contract with its own consumers (previewer, the
    # .schematic export, and any script reading the CLI's JSON), so it gets the same guard as the
    # input - and needs it for the same reason: LayoutResult v1 added `hatches`, a bump precisely
    # because a consumer that ignores a field can describe a structure that will not form, with
    # nothing raising.
    payload = _valid_layout().model_dump()
    payload["version"] = version
    with pytest.raises(ValidationError, match=f"contract version {version}"):
        LayoutResult.model_validate(payload)


def test_layout_result_round_trips_through_its_own_serialization() -> None:
    layout = _valid_layout()
    assert LayoutResult.model_validate_json(layout.model_dump_json()) == layout


def test_infeasible_layout_requires_infeasibility() -> None:
    with pytest.raises(ValidationError):
        LayoutResult(status=LayoutStatus.INFEASIBLE, seed=1)
    LayoutResult(
        status=LayoutStatus.INFEASIBLE,
        seed=1,
        infeasibility=Infeasibility(
            constraint="bounding_region",
            detail="machines do not fit",
            suggested_relaxation="grow region to 10x10",
        ),
    )


def test_metrics_allow_extra_fields() -> None:
    m = LayoutMetrics(footprint=4, vertical_runs=2)
    assert m.model_dump()["vertical_runs"] == 2


# --------------------------------------------------------------------------- serialization


def test_enums_serialize_to_doc_strings() -> None:
    assert Commodity.FLUID.value == "fluid"
    assert IODirection.OUTPUT.value == "output"
    assert LayoutStatus.PARTIAL_INVALID.value == "partial_invalid"
    assert _valid_input_ir().model_dump(mode="json")["me_toggles"]["fluids"] is True


# Both models' JSON round trips are asserted where they earn their keep, next to the contract
# version guard that could break them: test_input_ir_round_trips_through_its_own_serialization and
# test_layout_result_round_trips_through_its_own_serialization (GitHub #94).


# --------------------------------------------------------------------------- property tests


def test_a_non_finite_quantity_is_refused_by_the_contract() -> None:
    # Pydantic accepts inf for a float field by default, and inf passes every bound this IR
    # states: inf >= 0.0 is True, so Machine.eut's ge=0.0 let it through and power synthesis died
    # on it much later as an OverflowError - an ArithmeticError, which is not a ValueError and so
    # was not caught as a load failure at all (#115). A quantity no arithmetic can use is a
    # contract violation, named here where the field can still be pointed at.
    with pytest.raises(ValidationError, match="finite"):
        _machine(eut=math.inf)
    with pytest.raises(ValidationError, match="finite"):
        Port(id="p", commodity=Commodity.POWER, direction=IODirection.INPUT, rate=math.inf)


def test_nan_is_refused_for_the_same_reason_rather_than_by_luck() -> None:
    # nan was already rejected, but only incidentally: nan >= 0.0 is False, so it failed the bound
    # rather than the type. Any field without a lower bound took it. It is now refused as what it
    # is, which is what keeps a later unbounded quantity from re-opening the hole.
    with pytest.raises(ValidationError, match="finite"):
        Net(
            id="n",
            commodity=Commodity.FLUID,
            fluid_or_item="water",
            throughput=math.nan,
            endpoints=[MachineFaceRef(machine_id="m", port_id="p")],
        )


def test_both_bases_refuse_non_finite_floats() -> None:
    # The value types carry no float field today, so the config would otherwise go untested on
    # FrozenModel and quietly diverge from StrictModel the first time one gains a coordinate or a
    # weight. Pinned on the bases themselves rather than on whichever model happens to use them.
    class _Weighted(FrozenModel):
        w: float

    class _Sized(StrictModel):
        w: float

    for model in (_Weighted, _Sized):
        with pytest.raises(ValidationError, match="finite"):
            model(w=math.inf)


@given(
    x=st.integers(min_value=-1000, max_value=1000),
    y=st.integers(min_value=-1000, max_value=1000),
    z=st.integers(min_value=-1000, max_value=1000),
)
def test_cellcoord_round_trips(x: int, y: int, z: int) -> None:
    c = CellCoord(x=x, y=y, z=z)
    assert CellCoord.model_validate_json(c.model_dump_json()) == c
    assert hash(c) == hash(CellCoord(x=x, y=y, z=z))


@given(
    throughput=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    n_endpoints=st.integers(min_value=1, max_value=5),
)
def test_net_round_trips_for_any_nonneg_throughput(throughput: float, n_endpoints: int) -> None:
    net = Net(
        id="n",
        commodity=Commodity.FLUID,
        fluid_or_item="water",
        throughput=throughput,
        endpoints=[MachineFaceRef(machine_id=f"m{i}", port_id="p") for i in range(n_endpoints)],
    )
    assert Net.model_validate_json(net.model_dump_json()) == net
