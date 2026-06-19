"""Tests for the validator - the only automated correctness gate.

The parametrized accept/reject cases below *are* the in-code golden corpus: one focused
known-bad layout per violation, plus known-good layouts the validator must accept. The
headline property is that a layout which *claims* ``status=valid`` but breaks geometry is
still reported invalid (``report.ok is False``) - the validator's verdict is independent.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
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
from gtnh_solver.validator import ValidationReport, validate
from gtnh_solver.validator.report import ViolationCode

Mutator = Callable[[InputIR, LayoutResult], tuple[InputIR, LayoutResult]]


def _coord(x: int, y: int, z: int) -> CellCoord:
    return CellCoord(x=x, y=y, z=z)


def _item_machine(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="gt.macerator",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH],
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )


def _base() -> tuple[InputIR, LayoutResult]:
    """A fresh, fully valid 2-machine item line with a routed net + honored pin."""
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_item_machine("m1"), _item_machine("m2")],
        nets=[
            Net(
                id="n1",
                commodity=Commodity.ITEM,
                fluid_or_item="gt.dust.iron",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="m1", port_id="out"),
                    MachineFaceRef(machine_id="m2", port_id="out"),
                ],
            )
        ],
        pinned=[PinnedIO(net_id="n1", cell=_coord(1, 0, 1), kind=IODirection.OUTPUT)],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="m1", cell=_coord(1, 0, 1), orientation=Facing.NORTH),
            Placement(machine_id="m2", cell=_coord(3, 0, 1), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="n1",
                commodity=Commodity.ITEM,
                segments=[
                    Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),
                    Segment(start=_coord(2, 0, 1), end=_coord(3, 0, 1), channel=0),
                ],
            )
        ],
    )
    return problem, layout


def test_valid_layout_passes() -> None:
    problem, layout = _base()
    report = validate(problem, layout)
    assert report.ok, str(report)


# --------------------------------------------------------------- known-bad mutators


def _overlap(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    m2 = layout.placements[1].model_copy(update={"cell": _coord(1, 0, 1)})
    return p, layout.model_copy(update={"placements": [layout.placements[0], m2]})


def _machine_oob(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    m1 = layout.placements[0].model_copy(update={"cell": _coord(8, 0, 0)})
    return p, layout.model_copy(update={"placements": [m1, layout.placements[1]]})


def _on_reserved(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p.model_copy(update={"reserved_cells": [_coord(1, 0, 1)]}), layout


def _bad_orientation(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    m1 = layout.placements[0].model_copy(update={"orientation": Facing.EAST})
    return p, layout.model_copy(update={"placements": [m1, layout.placements[1]]})


def _unknown_machine(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    ghost = Placement(machine_id="ghost", cell=_coord(5, 0, 5), orientation=Facing.NORTH)
    return p, layout.model_copy(update={"placements": [*layout.placements, ghost]})


def _count_mismatch(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(update={"placements": [layout.placements[0]]})


def _unknown_net(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    ghost = Route(
        net_id="ghost",
        commodity=Commodity.ITEM,
        segments=[Segment(start=_coord(5, 0, 5), end=_coord(6, 0, 5), channel=0)],
    )
    return p, layout.model_copy(update={"routes": [*layout.routes, ghost]})


def _duplicate_route(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(update={"routes": [layout.routes[0], layout.routes[0]]})


def _commodity_mismatch(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    r = layout.routes[0].model_copy(update={"commodity": Commodity.FLUID})
    return p, layout.model_copy(update={"routes": [r]})


def _missing_route(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(update={"routes": []})


def _route_oob(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    r = layout.routes[0].model_copy(
        update={"segments": [Segment(start=_coord(1, 0, 1), end=_coord(9, 0, 1), channel=0)]}
    )
    return p, layout.model_copy(update={"routes": [r]})


def _discontinuous(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    r = layout.routes[0].model_copy(
        update={
            "segments": [
                Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),
                Segment(start=_coord(5, 0, 1), end=_coord(6, 0, 1), channel=0),
            ]
        }
    )
    return p, layout.model_copy(update={"routes": [r]})


def _pinned_off_route(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    pin = p.pinned[0].model_copy(update={"cell": _coord(5, 0, 5)})
    return p.model_copy(update={"pinned": [pin]}), layout


BAD_CASES: list[tuple[str, Mutator, ViolationCode]] = [
    ("overlap", _overlap, ViolationCode.MACHINE_OVERLAP),
    ("machine_oob", _machine_oob, ViolationCode.MACHINE_OUT_OF_BOUNDS),
    ("on_reserved", _on_reserved, ViolationCode.MACHINE_ON_RESERVED),
    ("bad_orientation", _bad_orientation, ViolationCode.BAD_ORIENTATION),
    ("unknown_machine", _unknown_machine, ViolationCode.UNKNOWN_MACHINE),
    ("count_mismatch", _count_mismatch, ViolationCode.PLACEMENT_COUNT_MISMATCH),
    ("unknown_net", _unknown_net, ViolationCode.UNKNOWN_NET),
    ("duplicate_route", _duplicate_route, ViolationCode.DUPLICATE_ROUTE),
    ("commodity_mismatch", _commodity_mismatch, ViolationCode.ROUTE_COMMODITY_MISMATCH),
    ("missing_route", _missing_route, ViolationCode.MISSING_ROUTE),
    ("route_oob", _route_oob, ViolationCode.ROUTE_OUT_OF_BOUNDS),
    ("discontinuous", _discontinuous, ViolationCode.ROUTE_DISCONTINUOUS),
    ("pinned_off_route", _pinned_off_route, ViolationCode.PINNED_IO_NOT_ON_ROUTE),
]


@pytest.mark.parametrize(
    ("mutate", "code"), [(m, c) for _, m, c in BAD_CASES], ids=[n for n, _, _ in BAD_CASES]
)
def test_known_bad_layout_is_flagged(mutate: Mutator, code: ViolationCode) -> None:
    problem, layout = mutate(*_base())
    report = validate(problem, layout)
    assert not report.ok
    assert code in report.codes(), f"expected {code} in {report.codes()}"


def test_layout_claiming_valid_is_still_independently_rejected() -> None:
    # The layout's own status says VALID; the validator must not take its word for it.
    problem, layout = _overlap(*_base())
    assert layout.status is LayoutStatus.VALID
    assert validate(problem, layout).ok is False


# --------------------------------------------------------------- ME toggles & power


def test_me_toggled_net_correctly_omitted_is_ok() -> None:
    problem, layout = _base()
    problem = problem.model_copy(update={"me_toggles": METoggles(items=True), "pinned": []})
    layout = layout.model_copy(update={"routes": []})  # ME-toggled item net must not be routed
    assert validate(problem, layout).ok


def test_me_toggled_net_that_is_routed_is_flagged() -> None:
    problem, layout = _base()
    problem = problem.model_copy(update={"me_toggles": METoggles(items=True), "pinned": []})
    report = validate(problem, layout)  # route still present
    assert ViolationCode.UNEXPECTED_ME_ROUTE in report.codes()


def _power_pair() -> tuple[InputIR, LayoutResult]:
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            Machine(
                id="mp",
                type="gt.machine",
                voltage_tier="LV",
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="pwr", commodity=Commodity.POWER, direction=IODirection.INPUT)]
                ),
            )
        ],
        nets=[
            Net(
                id="np",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[MachineFaceRef(machine_id="mp", port_id="pwr")],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[Placement(machine_id="mp", cell=_coord(0, 0, 0), orientation=Facing.NORTH)],
        routes=[
            Route(
                net_id="np",
                commodity=Commodity.POWER,
                segments=[Segment(start=_coord(0, 0, 0), end=_coord(1, 0, 0), channel=0)],
                thickness_per_segment=[8],
            )
        ],
    )
    return problem, layout


def test_valid_power_route_passes() -> None:
    assert validate(*_power_pair()).ok


def test_power_thickness_defect_caught_even_when_model_validation_bypassed() -> None:
    # A buggy producer using model_construct() can skip the IR's own thickness check; the
    # validator is independent and must still catch the malformed power route.
    problem, layout = _power_pair()
    bad_route = Route.model_construct(
        net_id="np",
        commodity=Commodity.POWER,
        segments=layout.routes[0].segments,
        thickness_per_segment=[3],  # not a real cable tier
    )
    broken = layout.model_copy(update={"routes": [bad_route]})
    assert ViolationCode.POWER_THICKNESS_INVALID in validate(problem, broken).codes()


# --------------------------------------------------------------- robustness / property


def test_validate_never_raises_on_a_mismatched_layout() -> None:
    problem, _ = _base()
    empty = LayoutResult(status=LayoutStatus.VALID, seed=0)
    report = validate(problem, empty)
    assert isinstance(report, ValidationReport)
    assert not report.ok  # machines unplaced, net unrouted - reported, not raised


@given(
    region=st.integers(min_value=1, max_value=10),
    x=st.integers(min_value=-3, max_value=12),
)
def test_out_of_bounds_detected_iff_machine_outside_region(region: int, x: int) -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=region, sy=region, sz=region),
        machines=[_item_machine("m")],
        nets=[],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[Placement(machine_id="m", cell=_coord(x, 0, 0), orientation=Facing.NORTH)],
    )
    in_bounds = 0 <= x < region
    has_oob = ViolationCode.MACHINE_OUT_OF_BOUNDS in validate(problem, layout).codes()
    assert has_oob is not in_bounds
