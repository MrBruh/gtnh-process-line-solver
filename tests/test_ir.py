"""Tests for the IR contracts (docs/IR.md).

The IR guarantees structural well-formedness + referential integrity; it deliberately
does NOT do geometric/rule checking (that is the validator's independent job). These
tests pin both the guarantees and the non-guarantees.
"""

from __future__ import annotations

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
    Placement,
    Port,
    Route,
    Segment,
)

# --------------------------------------------------------------------------- helpers


def _machine(
    mid: str = "m1",
    *,
    ports: list[Port] | None = None,
    orientations: list[Facing] | None = None,
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
        c.x = 9  # frozen


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


def test_facespec_allows_one_auto_output() -> None:
    FaceSpec(
        ports=[
            Port(
                id="o", commodity=Commodity.ITEM, direction=IODirection.OUTPUT, is_auto_output=True
            )
        ]
    )


def test_facespec_rejects_two_auto_outputs() -> None:
    with pytest.raises(ValidationError):
        FaceSpec(
            ports=[
                Port(
                    id="a",
                    commodity=Commodity.ITEM,
                    direction=IODirection.OUTPUT,
                    is_auto_output=True,
                ),
                Port(
                    id="b",
                    commodity=Commodity.FLUID,
                    direction=IODirection.OUTPUT,
                    is_auto_output=True,
                ),
            ]
        )


def test_auto_output_cannot_be_power_or_input() -> None:
    with pytest.raises(ValidationError):
        FaceSpec(
            ports=[
                Port(
                    id="p",
                    commodity=Commodity.POWER,
                    direction=IODirection.OUTPUT,
                    is_auto_output=True,
                )
            ]
        )
    with pytest.raises(ValidationError):
        FaceSpec(
            ports=[
                Port(
                    id="p",
                    commodity=Commodity.ITEM,
                    direction=IODirection.INPUT,
                    is_auto_output=True,
                )
            ]
        )


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


def test_machine_count_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        Machine(
            id="m",
            type="t",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            count=0,
        )


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
    m = LayoutMetrics(footprint=4, vertical_runs=2)  # type: ignore[call-arg]
    assert m.model_dump()["vertical_runs"] == 2


# --------------------------------------------------------------------------- serialization


def test_enums_serialize_to_doc_strings() -> None:
    assert Commodity.FLUID.value == "fluid"
    assert IODirection.OUTPUT.value == "output"
    assert LayoutStatus.PARTIAL_INVALID.value == "partial_invalid"
    assert _valid_input_ir().model_dump(mode="json")["me_toggles"]["fluids"] is True


def test_input_ir_json_round_trip() -> None:
    ir = _valid_input_ir()
    assert InputIR.model_validate_json(ir.model_dump_json()) == ir


def test_layout_result_json_round_trip() -> None:
    layout = _valid_layout()
    assert LayoutResult.model_validate_json(layout.model_dump_json()) == layout


# --------------------------------------------------------------------------- property tests


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
