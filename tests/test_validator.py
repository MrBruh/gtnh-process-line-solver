"""Tests for the validator - the only automated correctness gate.

The parametrized accept/reject cases below *are* the in-code golden corpus: one focused
known-bad layout per violation, plus known-good layouts the validator must accept. The
headline property is that a layout which *claims* ``status=valid`` but breaks geometry is
still reported invalid (``report.ok is False``) - the validator's verdict is independent.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.adapter import Node, Plan, Recipe, adapt_file, to_input_ir
from gtnh_solver.dataset import load_physical_dataset, machine_amps_in
from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    PinnedIO,
    PipeFamily,
    PlacedHatch,
    Placement,
    Port,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)
from gtnh_solver.solver import solve
from gtnh_solver.validator import ValidationReport, validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import hatched_dataset

Mutator = Callable[[InputIR, LayoutResult], tuple[InputIR, LayoutResult]]


def _coord(x: int, y: int, z: int) -> CellCoord:
    return CellCoord(x=x, y=y, z=z)


def _item_machine(
    mid: str,
    *,
    direction: IODirection = IODirection.OUTPUT,
    port: str = "out",
) -> Machine:
    return Machine(
        id=mid,
        type="gt.macerator",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH],
        faces=FaceSpec(ports=[Port(id=port, commodity=Commodity.ITEM, direction=direction)]),
    )


def _base() -> tuple[InputIR, LayoutResult]:
    """A fresh, fully valid 2-machine item line: a producer piping to a real consumer + a pin.

    ``m1`` outputs, ``m2`` consumes (INPUT) - a routed net needs at least one consumer or its
    producers deliver nowhere, so the "valid" golden fixture must wire one (an earlier version
    routed between two OUTPUT endpoints and still passed, normalizing a consumer-less net)."""
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[
            _item_machine("m1"),
            _item_machine("m2", direction=IODirection.INPUT, port="in"),
        ],
        nets=[
            Net(
                id="n1",
                commodity=Commodity.ITEM,
                fluid_or_item="gt.dust.iron",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="m1", port_id="out"),
                    MachineFaceRef(machine_id="m2", port_id="in"),
                ],
            )
        ],
        pinned=[PinnedIO(net_id="n1", cell=_coord(1, 0, 2), kind=IODirection.OUTPUT)],
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
                terminals=[
                    # both machines dock on their south face (front is north) into the z=2 row
                    Terminal(
                        machine_id="m1", port_id="out", face=Facing.SOUTH, cell=_coord(1, 0, 2)
                    ),
                    Terminal(
                        machine_id="m2", port_id="in", face=Facing.SOUTH, cell=_coord(3, 0, 2)
                    ),
                ],
                segments=[
                    Segment(start=_coord(1, 0, 2), end=_coord(2, 0, 2), channel=0),
                    Segment(start=_coord(2, 0, 2), end=_coord(3, 0, 2), channel=0),
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


def _segment_not_unit(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # one segment jumps two cells (1,0,2)->(3,0,2): a single connected edge, so the old
    # connectivity-only check passed it - a "teleport" the unit-step check must reject.
    r = layout.routes[0].model_copy(
        update={"segments": [Segment(start=_coord(1, 0, 2), end=_coord(3, 0, 2), channel=0)]}
    )
    return p, layout.model_copy(update={"routes": [r]})


def _route_through_machine(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # a unit-step, connected route that detours through m1's own body cell (1,0,1)
    r = layout.routes[0].model_copy(
        update={
            "segments": [
                Segment(start=_coord(1, 0, 2), end=_coord(1, 0, 1), channel=0),  # into m1's body
                Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),
                Segment(start=_coord(2, 0, 1), end=_coord(2, 0, 2), channel=0),
                Segment(start=_coord(2, 0, 2), end=_coord(3, 0, 2), channel=0),
            ]
        }
    )
    return p, layout.model_copy(update={"routes": [r]})


def _route_on_reserved(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # reserve a cell the (unchanged) route already runs through
    return p.model_copy(update={"reserved_cells": [_coord(2, 0, 2)]}), layout


def _replace_first_terminal(layout: LayoutResult, **update: object) -> LayoutResult:
    r = layout.routes[0]
    bad = r.terminals[0].model_copy(update=update)
    return layout.model_copy(
        update={"routes": [r.model_copy(update={"terminals": [bad, r.terminals[1]]})]}
    )


def _missing_terminal(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    r = layout.routes[0].model_copy(update={"terminals": []})
    return p, layout.model_copy(update={"routes": [r]})


def _terminal_on_front(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, _replace_first_terminal(layout, face=Facing.NORTH)  # north is the front face


def _terminal_not_adjacent(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, _replace_first_terminal(layout, cell=_coord(5, 0, 5))


def _terminal_off_route(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # adjacent to m1 on its (non-front) east face, but not on the route running along z=2
    return p, _replace_first_terminal(layout, face=Facing.EAST, cell=_coord(2, 0, 1))


def _terminal_foreign(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # a terminal whose (machine, port) is not one of the net's endpoints - a foreign dock the
    # route must not be able to "claim" (m1 has no port 'ghost', and it is not an endpoint of n1)
    r = layout.routes[0]
    bogus = Terminal(machine_id="m1", port_id="ghost", face=Facing.EAST, cell=_coord(2, 0, 1))
    return p, layout.model_copy(
        update={"routes": [r.model_copy(update={"terminals": [*r.terminals, bogus]})]}
    )


def _terminal_duplicate(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # two terminals for the same endpoint - which one is THE dock is ambiguous; reject it
    r = layout.routes[0]
    return p, layout.model_copy(
        update={"routes": [r.model_copy(update={"terminals": [*r.terminals, r.terminals[0]]})]}
    )


# ---------------------------------------------------- placed hatches (LayoutResult v1)
#
# The record is redundant with the Terminal on purpose, so these check the redundancy holds. A
# hatch facing into its own structure is the one GT itself will not catch: the multiblock forms
# and then moves nothing.


def _good_hatch(**over: object) -> PlacedHatch:
    """The hatch m1's routed output needs: its own body cell, facing the way its terminal docks."""
    base: dict[str, object] = {
        "machine_id": "m1",
        "kind": "OutputBus",
        "cell": _coord(1, 0, 1),
        "facing": Facing.SOUTH,
        "port_id": "out",
    }
    return PlacedHatch.model_validate({**base, **over})


def _hatch_off_machine(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(update={"hatches": [_good_hatch(cell=_coord(6, 0, 6))]})


def _hatch_cell_collision(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(
        update={"hatches": [_good_hatch(), _good_hatch(kind="Maintenance", port_id=None)]}
    )


def _hatch_terminal_mismatch(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # The terminal docks south; claiming the hatch faces east describes a different build.
    return p, layout.model_copy(update={"hatches": [_good_hatch(facing=Facing.EAST)]})


def _hatch_unknown_port(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(update={"hatches": [_good_hatch(port_id="nope")]})


def _hatch_on_ghost_machine(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    return p, layout.model_copy(update={"hatches": [_good_hatch(machine_id="ghost")]})


def test_a_well_formed_hatch_passes() -> None:
    # The redundancy has to be satisfiable, not just rejectable: the hatch that agrees with its own
    # terminal must validate clean, or lane 4 would have nothing legal to emit.
    problem, layout = _base()
    report = validate(problem, layout.model_copy(update={"hatches": [_good_hatch()]}))
    assert report.ok, str(report)


def test_a_hatch_facing_into_its_own_structure_is_rejected() -> None:
    """The failure GT will not catch for us.

    ``IStructureElement.check`` takes no facing and every GT hatch returns ``isFacingValid = true``,
    so a multiblock forms happily with a hatch pointing inward and then moves nothing. Needs a
    machine more than one cell deep to express at all - on a 1x1x1 block every face points out.
    """
    machine = _item_machine("wide").model_copy(update={"footprint": CellBox(sx=2, sy=1, sz=1)})
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[machine], nets=[])
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[Placement(machine_id="wide", cell=_coord(0, 0, 0), orientation=Facing.NORTH)],
        # (0,0,0) and (1,0,0) are its body; a hatch at (0,0,0) facing EAST points at (1,0,0), which
        # is the machine's own other half.
        hatches=[
            PlacedHatch(
                machine_id="wide", kind="InputBus", cell=_coord(0, 0, 0), facing=Facing.EAST
            )
        ],
    )
    report = validate(problem, layout)
    assert not report.ok
    assert ViolationCode.HATCH_FACES_INWARD in report.codes()

    # The same hatch on the far cell faces open air, and is fine.
    ok = layout.model_copy(
        update={
            "hatches": [
                PlacedHatch(
                    machine_id="wide", kind="InputBus", cell=_coord(1, 0, 0), facing=Facing.EAST
                )
            ]
        }
    )
    assert ViolationCode.HATCH_FACES_INWARD not in validate(problem, ok).codes()


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
    ("missing_connection", _missing_route, ViolationCode.MISSING_CONNECTION),
    ("route_oob", _route_oob, ViolationCode.ROUTE_OUT_OF_BOUNDS),
    ("discontinuous", _discontinuous, ViolationCode.ROUTE_DISCONTINUOUS),
    ("segment_not_unit", _segment_not_unit, ViolationCode.ROUTE_SEGMENT_NOT_UNIT),
    ("route_through_machine", _route_through_machine, ViolationCode.ROUTE_THROUGH_MACHINE),
    ("route_on_reserved", _route_on_reserved, ViolationCode.ROUTE_ON_RESERVED),
    ("pinned_off_route", _pinned_off_route, ViolationCode.PINNED_IO_NOT_ON_ROUTE),
    ("missing_terminal", _missing_terminal, ViolationCode.MISSING_TERMINAL),
    ("terminal_foreign", _terminal_foreign, ViolationCode.TERMINAL_NOT_AN_ENDPOINT),
    ("terminal_duplicate", _terminal_duplicate, ViolationCode.DUPLICATE_TERMINAL),
    ("terminal_on_front", _terminal_on_front, ViolationCode.TERMINAL_ON_FRONT_FACE),
    ("terminal_not_adjacent", _terminal_not_adjacent, ViolationCode.TERMINAL_NOT_ADJACENT),
    ("terminal_off_route", _terminal_off_route, ViolationCode.TERMINAL_NOT_ON_ROUTE),
    ("hatch_off_machine", _hatch_off_machine, ViolationCode.HATCH_NOT_ON_MACHINE),
    ("hatch_cell_collision", _hatch_cell_collision, ViolationCode.HATCH_CELL_COLLISION),
    ("hatch_terminal_mismatch", _hatch_terminal_mismatch, ViolationCode.HATCH_TERMINAL_MISMATCH),
    ("hatch_unknown_port", _hatch_unknown_port, ViolationCode.HATCH_UNKNOWN_PORT),
    ("hatch_on_ghost_machine", _hatch_on_ghost_machine, ViolationCode.UNKNOWN_MACHINE),
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


def test_two_routes_sharing_a_cell_collide() -> None:
    # Two item nets whose routes cross at one cell: unbuildable under the crude single-channel cap
    # (one block can't be two pipes). The validator flags it independently of the router.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[
            _item_machine("a"),
            _item_machine("b", direction=IODirection.INPUT, port="in"),
            _item_machine("c"),
            _item_machine("d", direction=IODirection.INPUT, port="in"),
        ],
        nets=[
            Net(
                id="nx",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="a", port_id="out"),
                    MachineFaceRef(machine_id="b", port_id="in"),
                ],
            ),
            Net(
                id="ny",
                commodity=Commodity.ITEM,
                fluid_or_item="y",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="c", port_id="out"),
                    MachineFaceRef(machine_id="d", port_id="in"),
                ],
            ),
        ],
    )
    shared = _coord(4, 0, 4)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        routes=[
            Route(
                net_id="nx",
                commodity=Commodity.ITEM,
                segments=[Segment(start=_coord(3, 0, 4), end=shared, channel=0)],
            ),
            Route(
                net_id="ny",
                commodity=Commodity.ITEM,
                segments=[Segment(start=_coord(4, 0, 3), end=shared, channel=0)],
            ),
        ],
    )
    codes = validate(problem, layout).codes()
    assert ViolationCode.ROUTE_CELL_COLLISION in codes, codes


# ----------------------------------------------------------- routed-net direction / commodity


def test_routed_net_without_a_consumer_is_flagged() -> None:
    # Two producers, no INPUT endpoint: the pipe has nowhere to deliver. The auto path already
    # rejected this (wrong endpoints); the routed path used to wave it through - now it doesn't.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_item_machine("m1"), _item_machine("m2")],  # both OUTPUT
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
                terminals=[
                    Terminal(
                        machine_id="m1", port_id="out", face=Facing.SOUTH, cell=_coord(1, 0, 2)
                    ),
                    Terminal(
                        machine_id="m2", port_id="out", face=Facing.SOUTH, cell=_coord(3, 0, 2)
                    ),
                ],
                segments=[
                    Segment(start=_coord(1, 0, 2), end=_coord(2, 0, 2), channel=0),
                    Segment(start=_coord(2, 0, 2), end=_coord(3, 0, 2), channel=0),
                ],
            )
        ],
    )
    assert ViolationCode.ROUTE_NET_NO_CONSUMER in validate(problem, layout).codes()


def test_routed_net_with_multiple_same_commodity_producers_passes() -> None:
    # GT lets several machines eject into one pipe: two OUTPUT producers feeding one INPUT consumer
    # is a legitimate net the gate must accept (multiple producers are not an error).
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[
            _item_machine("p1"),
            _item_machine("p2"),
            _item_machine("c", direction=IODirection.INPUT, port="in"),
        ],
        nets=[
            Net(
                id="n1",
                commodity=Commodity.ITEM,
                fluid_or_item="gt.dust.iron",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="p1", port_id="out"),
                    MachineFaceRef(machine_id="p2", port_id="out"),
                    MachineFaceRef(machine_id="c", port_id="in"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="p1", cell=_coord(1, 0, 1), orientation=Facing.NORTH),
            Placement(machine_id="p2", cell=_coord(3, 0, 1), orientation=Facing.NORTH),
            Placement(machine_id="c", cell=_coord(5, 0, 1), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="n1",
                commodity=Commodity.ITEM,
                terminals=[
                    Terminal(
                        machine_id="p1", port_id="out", face=Facing.SOUTH, cell=_coord(1, 0, 2)
                    ),
                    Terminal(
                        machine_id="p2", port_id="out", face=Facing.SOUTH, cell=_coord(3, 0, 2)
                    ),
                    Terminal(machine_id="c", port_id="in", face=Facing.SOUTH, cell=_coord(5, 0, 2)),
                ],
                segments=[
                    Segment(start=_coord(1, 0, 2), end=_coord(2, 0, 2), channel=0),
                    Segment(start=_coord(2, 0, 2), end=_coord(3, 0, 2), channel=0),
                    Segment(start=_coord(3, 0, 2), end=_coord(4, 0, 2), channel=0),
                    Segment(start=_coord(4, 0, 2), end=_coord(5, 0, 2), channel=0),
                ],
            )
        ],
    )
    report = validate(problem, layout)
    assert report.ok, str(report)


def test_routed_net_with_mixed_commodity_endpoints_is_flagged() -> None:
    # The input IR forbids wiring a fluid port onto an item net; the validator is independent and
    # must catch it anyway, so build the mismatch past the IR's check with model_construct.
    fluid_sink = Machine(
        id="m2",
        type="gt.macerator",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)]
        ),
    )
    net = Net(
        id="n1",
        commodity=Commodity.ITEM,
        fluid_or_item="gt.dust.iron",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="m1", port_id="out"),
            MachineFaceRef(machine_id="m2", port_id="in"),
        ],
    )
    problem = InputIR.model_construct(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_item_machine("m1"), fluid_sink],
        nets=[net],
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
                terminals=[
                    Terminal(
                        machine_id="m1", port_id="out", face=Facing.SOUTH, cell=_coord(1, 0, 2)
                    ),
                    Terminal(
                        machine_id="m2", port_id="in", face=Facing.SOUTH, cell=_coord(3, 0, 2)
                    ),
                ],
                segments=[
                    Segment(start=_coord(1, 0, 2), end=_coord(2, 0, 2), channel=0),
                    Segment(start=_coord(2, 0, 2), end=_coord(3, 0, 2), channel=0),
                ],
            )
        ],
    )
    assert ViolationCode.ROUTE_NET_MIXED_COMMODITY in validate(problem, layout).codes()


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
    """A genuinely valid 1-source -> 1-sink power net: a source (POWER OUTPUT) feeding ``mp``.

    The source is what makes this valid - a power route the validator can root and amperage-check.
    (An earlier version of this fixture had only the INPUT machine and no source, which the
    validator's amperage check silently skipped: it asserted ``.ok`` on a source-less net, blessing
    the exact bad case the gate exists to catch. The source closes that hole.)"""
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            Machine(
                id="src",
                type="Power Source (LV)",
                voltage_tier="LV",
                eut=0.0,
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
                ),
            ),
            Machine(
                id="mp",
                type="gt.machine",
                voltage_tier="LV",
                eut=16.0,  # 2 blocks out (30 V after loss): ceil(16/30)=1 amp -> a 1x cable carries it
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="pwr", commodity=Commodity.POWER, direction=IODirection.INPUT)]
                ),
            ),
        ],
        nets=[
            Net(
                id="np",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="po"),
                    MachineFaceRef(machine_id="mp", port_id="pwr"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="mp", cell=_coord(2, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="np",
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(
                        machine_id="src", port_id="po", face=Facing.SOUTH, cell=_coord(0, 0, 1)
                    ),
                    Terminal(
                        machine_id="mp", port_id="pwr", face=Facing.SOUTH, cell=_coord(2, 0, 1)
                    ),
                ],
                segments=[
                    Segment(start=_coord(0, 0, 1), end=_coord(1, 0, 1), channel=0),
                    Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),
                ],
                thickness_per_segment=[1, 1],
            )
        ],
    )
    return problem, layout


def test_valid_power_route_passes() -> None:
    assert validate(*_power_pair()).ok, str(validate(*_power_pair()))


def test_power_net_without_a_source_terminal_is_flagged() -> None:
    # Drop the source terminal: the route now serves a sink with no OUTPUT terminal to root the
    # tree at. The amperage check cannot re-derive the load, so it must reject - not skip.
    problem, layout = _power_pair()
    route = layout.routes[0]
    sink_only = route.model_copy(update={"terminals": [route.terminals[1]]})
    layout = layout.model_copy(update={"routes": [sink_only]})
    assert ViolationCode.POWER_NET_NO_SINGLE_SOURCE in validate(problem, layout).codes()


def _lone_source(cell: CellCoord, orientation: Facing) -> tuple[InputIR, LayoutResult]:
    """Just a placed power source (no nets): isolates the feed-on-boundary check."""
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            Machine(
                id="src",
                type="Power Source (LV)",
                voltage_tier="LV",
                orientation_options=[Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST],
                faces=FaceSpec(
                    ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
                ),
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[Placement(machine_id="src", cell=cell, orientation=orientation)],
    )
    return problem, layout


def test_power_source_feed_face_on_the_boundary_passes() -> None:
    # Front north at z=0: the feed face is flush on the region wall - the external feed can enter.
    assert validate(*_lone_source(_coord(1, 0, 0), Facing.NORTH)).ok


def test_power_source_buried_mid_region_is_flagged() -> None:
    # The source's front face is its reserved external-feed face; facing an in-region cell there
    # is nowhere for the external feed to come in from, so the layout is not buildable as claimed.
    problem, layout = _lone_source(_coord(1, 0, 1), Facing.NORTH)
    assert ViolationCode.POWER_FEED_NOT_ON_BOUNDARY in validate(problem, layout).codes()


def test_power_source_at_a_wall_facing_the_interior_is_flagged() -> None:
    # Touching the boundary is not enough: the FRONT face is the reserved feed entry, so a source
    # on the west wall facing east (into the room) still has no external feed face.
    problem, layout = _lone_source(_coord(0, 0, 1), Facing.EAST)
    assert ViolationCode.POWER_FEED_NOT_ON_BOUNDARY in validate(problem, layout).codes()


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


def _power_trunk() -> tuple[InputIR, LayoutResult]:
    """A valid source -> m0 power cable; m0 draws 2 amps (LV, eut 64), so the trunk must be 2x."""
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[
            Machine(
                id="src",
                type="Power Source (LV)",
                voltage_tier="LV",
                eut=0.0,
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
                ),
            ),
            Machine(
                id="m0",
                type="M",
                voltage_tier="LV",
                eut=48.0,  # docked 2 blocks out (30 V after loss): ceil(48 / 30) = 2 amps
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT)]
                ),
            ),
        ],
        nets=[
            Net(
                id="pw",
                commodity=Commodity.POWER,
                throughput=64.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="m0", cell=_coord(2, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="pw",
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(
                        machine_id="src", port_id="po", face=Facing.SOUTH, cell=_coord(0, 0, 1)
                    ),
                    Terminal(
                        machine_id="m0", port_id="pi", face=Facing.SOUTH, cell=_coord(2, 0, 1)
                    ),
                ],
                segments=[
                    Segment(start=_coord(0, 0, 1), end=_coord(1, 0, 1), channel=0),
                    Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),
                ],
                thickness_per_segment=[2, 2],
            )
        ],
    )
    return problem, layout


def test_valid_power_trunk_amperage_passes() -> None:
    assert validate(*_power_trunk()).ok, str(validate(*_power_trunk()))


def test_power_cable_thinner_than_its_load_is_flagged() -> None:
    # the 2-amp trunk re-cabled as 1x: the validator re-derives the load and rejects it
    problem, layout = _power_trunk()
    thin = layout.routes[0].model_copy(update={"thickness_per_segment": [1, 1]})
    layout = layout.model_copy(update={"routes": [thin]})
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT in validate(problem, layout).codes()


def test_power_load_over_16x_has_no_sufficient_cable() -> None:
    # eut 544 at LV, 2 blocks out (30 V): ceil(544/30)=19 amps exceeds the 16x cap - even a maxed
    # cable is too thin
    problem, layout = _power_trunk()
    big = problem.machines[1].model_copy(update={"eut": 544.0})
    problem = problem.model_copy(update={"machines": [problem.machines[0], big]})
    maxed = layout.routes[0].model_copy(update={"thickness_per_segment": [16, 16]})
    layout = layout.model_copy(update={"routes": [maxed]})
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT in validate(problem, layout).codes()


def test_power_unknown_tier_cannot_be_amperage_verified() -> None:
    # An off-ladder tier is unverifiable (not undersized): the validator can't compute amperage for
    # it, so it declines to certify under the dedicated POWER_TIER_UNKNOWN code - not the
    # thickness-insufficient one, whose meaning is "cable thinner than the summed amps".
    problem, layout = _power_trunk()
    weird = problem.machines[1].model_copy(update={"voltage_tier": "NOPE"})
    problem = problem.model_copy(update={"machines": [problem.machines[0], weird]})
    codes = validate(problem, layout).codes()
    assert ViolationCode.POWER_TIER_UNKNOWN in codes, codes
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT not in codes, codes


def test_power_route_with_a_cycle_is_rejected_not_skipped() -> None:
    # A tangled (non-tree) trunk has an undefined per-segment amperage. Re-cable the valid
    # source->m0 run as a square loop touching both terminals: the cable graph now has a cycle
    # (edges == nodes, not nodes-1), so the gate must reject it rather than decline to certify -
    # an unverified trunk is exactly the silently-invalid case this check exists to catch.
    problem, _ = _power_trunk()
    loop = Route(
        net_id="pw",
        commodity=Commodity.POWER,
        terminals=[
            Terminal(machine_id="src", port_id="po", face=Facing.SOUTH, cell=_coord(0, 0, 1)),
            Terminal(machine_id="m0", port_id="pi", face=Facing.SOUTH, cell=_coord(2, 0, 1)),
        ],
        segments=[
            Segment(start=_coord(0, 0, 1), end=_coord(1, 0, 1), channel=0),
            Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),
            Segment(start=_coord(2, 0, 1), end=_coord(2, 0, 2), channel=0),
            Segment(start=_coord(2, 0, 2), end=_coord(1, 0, 2), channel=0),
            Segment(start=_coord(1, 0, 2), end=_coord(0, 0, 2), channel=0),
            Segment(start=_coord(0, 0, 2), end=_coord(0, 0, 1), channel=0),
        ],
        thickness_per_segment=[2, 2, 2, 2, 2, 2],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="m0", cell=_coord(2, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[loop],
    )
    assert ViolationCode.POWER_ROUTE_NOT_A_TREE in validate(problem, layout).codes()


def test_power_run_too_long_for_its_tier_is_flagged_as_voltage_drop() -> None:
    # A 40-block LV cable: loss (1 V/block) drops the 32 V tier below 0 long before the machine, so
    # no thickness can power it. The validator re-derives the distance from the cable tree and
    # rejects the run as unpowerable rather than certifying an unbuildable layout.
    src = Machine(
        id="src",
        type="Power Source (LV)",
        voltage_tier="LV",
        eut=0.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
    m0 = Machine(
        id="m0",
        type="M",
        voltage_tier="LV",
        eut=32.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT)]
        ),
    )
    n = 40
    problem = InputIR(
        bounding_region=CellBox(sx=n + 4, sy=4, sz=4),
        machines=[src, m0],
        nets=[
            Net(
                id="pw",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi"),
                ],
            )
        ],
    )
    route = Route(
        net_id="pw",
        commodity=Commodity.POWER,
        terminals=[
            Terminal(machine_id="src", port_id="po", face=Facing.SOUTH, cell=_coord(0, 0, 1)),
            Terminal(machine_id="m0", port_id="pi", face=Facing.SOUTH, cell=_coord(n, 0, 1)),
        ],
        segments=[
            Segment(start=_coord(i, 0, 1), end=_coord(i + 1, 0, 1), channel=0) for i in range(n)
        ],
        thickness_per_segment=[16] * n,  # even a maxed cable cannot rescue a collapsed voltage
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="m0", cell=_coord(n, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[route],
    )
    assert ViolationCode.POWER_VOLTAGE_DROP_EXCESSIVE in validate(problem, layout).codes()

    # The same run with a rated hatch. A collapsed voltage delivers nothing whatever the ceiling,
    # so the machine must be reported unpowerable rather than measured for intake against 0 volts
    # (which would bill it a 0 EU/t supply and report a shortfall on top of the real violation).
    hatch = Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT, max_amps=2.0)
    rated = m0.model_copy(update={"faces": FaceSpec(ports=[hatch])})
    codes = validate(problem.model_copy(update={"machines": [src, rated]}), layout).codes()
    assert ViolationCode.POWER_VOLTAGE_DROP_EXCESSIVE in codes
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT not in codes


def _power_summed_trunk() -> tuple[InputIR, LayoutResult]:
    """A source feeding TWO sinks on a shared trunk, so the root segment carries their summed load.

    src -> mA (1 block out, 31 V) -> mB (2 blocks out, 30 V), all LV. mA draws 62/31 = 2.0 A and mB
    48/30 = 1.6 A, so the root segment (both downstream) needs ceil(2.0 + 1.6) = 4 A -> 4x, while
    the far segment (mB only) needs ceil(1.6) = 2 A -> 2x. The validator must SUM the two loads
    itself to get 4x - a single-sink fixture never exercises that aggregation - so this pins the
    independent summation, not just a per-machine number.
    """
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            Machine(
                id="src",
                type="Power Source (LV)",
                voltage_tier="LV",
                eut=0.0,
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
                ),
            ),
            Machine(
                id="mA",
                type="M",
                voltage_tier="LV",
                eut=62.0,  # 1 block out (31 V): 62 / 31 = 2.0 A exactly
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT)]
                ),
            ),
            Machine(
                id="mB",
                type="M",
                voltage_tier="LV",
                eut=48.0,  # 2 blocks out (30 V): 48 / 30 = 1.6 A
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(
                    ports=[Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT)]
                ),
            ),
        ],
        nets=[
            Net(
                id="pw",
                commodity=Commodity.POWER,
                throughput=110.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="po"),
                    MachineFaceRef(machine_id="mA", port_id="pi"),
                    MachineFaceRef(machine_id="mB", port_id="pi"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="mA", cell=_coord(1, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="mB", cell=_coord(2, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="pw",
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(
                        machine_id="src", port_id="po", face=Facing.SOUTH, cell=_coord(0, 0, 1)
                    ),
                    Terminal(
                        machine_id="mA", port_id="pi", face=Facing.SOUTH, cell=_coord(1, 0, 1)
                    ),
                    Terminal(
                        machine_id="mB", port_id="pi", face=Facing.SOUTH, cell=_coord(2, 0, 1)
                    ),
                ],
                segments=[
                    Segment(start=_coord(0, 0, 1), end=_coord(1, 0, 1), channel=0),  # both sinks
                    Segment(start=_coord(1, 0, 1), end=_coord(2, 0, 1), channel=0),  # mB only
                ],
                thickness_per_segment=[4, 2],
            )
        ],
    )
    return problem, layout


def test_valid_summed_amperage_trunk_passes() -> None:
    # The validator, deriving amperage on its own code path (no dataset.amp_load / whole_amps),
    # must agree the correctly-sized [4x, 2x] trunk is valid - the shared rule DATA (voltage ladder,
    # loss, rounding epsilon) keeps its rounding identical to the router's on a valid layout.
    assert validate(*_power_summed_trunk()).ok, str(validate(*_power_summed_trunk()))


def test_power_segment_one_step_too_thin_for_summed_load_is_flagged() -> None:
    # Independence proof: the root segment carries 3.6 A (4x). Re-cable it one ladder step thinner
    # (4x -> 2x), a mistake a buggy router could make; the validator has no router number to lean on,
    # so it must SUM the two machines' loads itself to know 2x is short - and it does.
    problem, layout = _power_summed_trunk()
    thin = layout.routes[0].model_copy(update={"thickness_per_segment": [2, 2]})  # seg 0 too thin
    layout = layout.model_copy(update={"routes": [thin]})
    codes = validate(problem, layout).codes()
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT in codes, codes


def test_zero_draw_power_sink_loads_no_amps() -> None:
    # A power sink drawing 0 EU/t loads nothing on the trunk (like dataset.amp_load, the validator
    # short-circuits eut <= 0 BEFORE reading its tier/voltage): the tier is never resolved, so even
    # an off-ladder tier here is not flagged - the two agree on a zero-draw machine. The 0-amp trunk
    # is valid at any thickness.
    problem, layout = _power_trunk()
    quiet = problem.machines[1].model_copy(update={"eut": 0.0, "voltage_tier": "NOPE"})
    problem = problem.model_copy(update={"machines": [problem.machines[0], quiet]})
    report = validate(problem, layout)
    assert report.ok, str(report)


# --------------------------------------------------- energy hatches: cells + power actually in


def _hatch_pair(cells: int | None) -> tuple[InputIR, LayoutResult]:
    """The valid power pair, with ``mp`` also carrying an unrouted input bus and ``cells`` cells.

    Two connections on one machine (an energy hatch and an item bus, which compete for the same
    interchangeable casing cells) is the smallest case where the structural ceiling can bind.
    """
    problem, layout = _power_pair()
    mp = problem.machines[1]
    ports = [*mp.faces.ports, Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
    wired = mp.model_copy(update={"faces": FaceSpec(ports=ports), "hatch_cells": cells})
    return problem.model_copy(update={"machines": [problem.machines[0], wired]}), layout


def test_hatch_cells_that_fit_every_connection_pass() -> None:
    report = validate(*_hatch_pair(2))  # two connections, two cells that can host one
    assert report.ok, str(report)


def test_more_connections_than_hatch_cells_is_flagged() -> None:
    # The energy hatch and the input bus want the same casing cell, so a structure offering one
    # cell cannot host both - a layout that wires them anyway is not buildable in game.
    codes = validate(*_hatch_pair(1)).codes()
    assert ViolationCode.HATCH_CELLS_EXCEEDED in codes, codes


def test_unknown_hatch_cells_imposes_no_ceiling() -> None:
    # None is "unknown" (a single-block machine, or a problem built without the physical dataset),
    # not "no cells": the same two connections a ceiling of 1 rejects must pass unmeasured here.
    report = validate(*_hatch_pair(None))
    assert report.ok, str(report)


def _rated_trunk(max_amps: float, eut: float | None = None) -> tuple[InputIR, LayoutResult]:
    """The valid 2x trunk, with m0's energy hatch declaring a per-tick amp ceiling.

    ``eut`` re-states m0's draw; the trunk stays correctly sized for anything up to 60 EU/t at the
    2 cable-blocks m0 sits at (30 V after loss, so 2x carries it).
    """
    problem, layout = _power_trunk()
    m0 = problem.machines[1]
    hatch = m0.faces.ports[0].model_copy(update={"max_amps": max_amps})
    update = {"faces": FaceSpec(ports=[hatch])} | ({} if eut is None else {"eut": eut})
    rated = m0.model_copy(update=update)
    return problem.model_copy(update={"machines": [problem.machines[0], rated]}), layout


def test_a_hatch_that_can_take_the_whole_draw_passes() -> None:
    # 2 A through a hatch 2 blocks out (30 V after loss) is 60 EU/t arriving, comfortably over the
    # 48 EU/t the machine draws - so the supply direction of the power check has nothing to say.
    report = validate(*_rated_trunk(2.0))
    assert report.ok, str(report)


def test_an_unrated_hatch_is_not_measured_for_supply() -> None:
    # Every pre-v3 problem leaves max_amps unset. With no per-connection ceiling there is nothing
    # to measure, so the check must stay silent rather than read the absence as a 0 A hatch and
    # declare every machine in every older problem starved. The machine is marked unverifiable
    # rather than passed over silently (#114); the difference shows on a machine whose OTHER
    # hatches are rated, below.
    codes = validate(*_power_trunk()).codes()
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT not in codes, codes


def test_a_machine_with_one_unrated_hatch_is_not_judged_on_the_others() -> None:
    """An unmeasurable connection makes the whole MACHINE unmeasurable, not a smaller machine.

    ``mp`` draws 48 EU/t through two hatches. Drop the ceiling from the second and the first alone
    takes in 29 EU/t, which was enough to report the machine starved - on part of its intake,
    against a connection that states no limit at all. That is a false infeasibility, and the
    validator's own rule is that a machine it could not verify on some route is skipped rather
    than reported on partial evidence (#114).
    """
    problem, layout = _split_hatch_pair(1.0)
    mp = problem.machines[1]
    ports = [mp.faces.ports[0], mp.faces.ports[1].model_copy(update={"max_amps": None})]
    partial = mp.model_copy(update={"faces": FaceSpec(ports=ports)})
    problem = problem.model_copy(update={"machines": [problem.machines[0], partial]})
    codes = validate(problem, layout).codes()
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT not in codes, codes


def test_gts_own_ceiling_feeds_a_basic_machine_a_flat_one_amp_would_starve() -> None:
    """Why the ``MTEBasicMachine`` formula and not ``MetaTileEntity``'s flat 1 A, pinned.

    A 31 EU/t LV machine 2 cable-blocks out receives 30 V a packet. At one amp that is 30 EU/t
    against a 31 EU/t draw and the machine is reported starved; at the ceiling GT actually gives a
    basic machine's own energy input - ``(mEUt * 2) / V + 1``, so 2 A here - it takes in 60 EU/t
    and runs. The flat 1 is ``MetaTileEntity``'s bare-block default, which every basic processing
    machine overrides, so modelling these at one amp would manufacture that shortfall.
    """
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT in validate(*_rated_trunk(1.0, 31.0)).codes()
    gt_ceiling = machine_amps_in(31.0, "LV")
    assert gt_ceiling == 2
    report = validate(*_rated_trunk(float(gt_ceiling), 31.0))
    assert report.ok, str(report)
    assert report.unverified_power_intake == ()  # measured, not merely un-flagged


# ------------------------------- the adapter -> validator path, end to end (#114)


def _adapted_single_block(eut: float, *, census: bool) -> InputIR:
    """The adapter's REAL output for one machine of a type the dataset does not carry.

    With ``census=True`` the dump enumerates every multiblock controller, so the miss proves the
    machine is a single block and the adapter states GT's ``maxAmperesIn`` ceiling on its one
    connection. With ``census=False`` the dump is only a sample (what the committed fixtures are),
    the machine's class is unknown, and the adapter states nothing. No hand-set ``max_amps``
    anywhere: this is the path a real run takes.
    """
    plan = Plan(
        schema_version=1,
        recipes=[Recipe(id="r0", machine_type="M", duration_ticks=10.0, eut=eut)],
        nodes=[Node(id="n0", recipe_id="r0", overclock_tier="LV")],
    )
    dataset = hatched_dataset(key="Some Other Multiblock", census=census)
    problem = to_input_ir(plan, physical=dataset)
    return problem.model_copy(update={"bounding_region": CellBox(sx=32, sy=4, sz=4)})


def _long_power_run(problem: InputIR, distance: int) -> LayoutResult:
    """A valid straight cable from the synthesized source to ``n0``, ``distance`` blocks long."""
    net = next(n for n in problem.nets if n.commodity is Commodity.POWER)
    source_id = next(e.machine_id for e in net.endpoints if e.machine_id != "n0")
    machine = next(m for m in problem.machines if m.id == "n0")
    volts = 32 - distance  # LV, one EU of cable loss a block
    amps = math.ceil(machine.eut / volts)  # what the trunk must carry, in whole amps
    thickness = next(t for t in (1, 2, 4, 8, 12, 16) if t >= amps)  # the legal cable ladder
    return LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id=source_id, cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="n0", cell=_coord(distance, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id=net.id,
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(
                        machine_id=source_id,
                        port_id="power:out",
                        face=Facing.SOUTH,
                        cell=_coord(0, 0, 1),
                    ),
                    Terminal(
                        machine_id="n0",
                        port_id="power:in",
                        face=Facing.SOUTH,
                        cell=_coord(distance, 0, 1),
                    ),
                ],
                segments=[
                    Segment(start=_coord(i, 0, 1), end=_coord(i + 1, 0, 1), channel=0)
                    for i in range(distance)
                ],
                thickness_per_segment=[thickness] * distance,
            )
        ],
    )


def test_an_adapter_stated_ceiling_can_actually_fire_the_supply_check() -> None:
    """The headline claim, driven end to end: no hand-set ``max_amps`` anywhere in this test.

    A 32 EU/t LV machine that a census dump proves is a single block gets GT's own ceiling,
    ``(32 * 2) / 32 + 1 = 3 A``. 22 cable-blocks out each packet is down to 10 V, so three of them
    are 30 EU/t against a 32 EU/t draw and the machine cannot run its recipe - reported, and named,
    so the solver can pull it back toward its source.
    """
    problem = _adapted_single_block(32.0, census=True)
    assert next(p for p in problem.machines[0].faces.ports if p.id == "power:in").max_amps == 3.0
    report = validate(problem, _long_power_run(problem, 22))
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT in report.codes(), str(report)
    assert [v.machine_id for v in report.violations] == ["n0"]
    assert report.unverified_power_intake == ()


def test_the_same_machine_on_a_short_run_is_measured_and_passes() -> None:
    # The other half: measured-and-fine has to be distinguishable from unmeasured. 16 blocks out
    # (the far side of the same run) 3 A x 16 V is 48 EU/t against 32, so the layout is clean AND
    # the machine is reported as verified rather than skipped.
    problem = _adapted_single_block(32.0, census=True)
    report = validate(problem, _long_power_run(problem, 16))
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT not in report.codes(), str(report)
    assert report.unverified_power_intake == ()


def test_without_a_census_the_same_layout_is_reported_unmeasured_not_clean() -> None:
    """The abstention, and the reason it has to be visible.

    Nothing about the machine changed - same draw, same 22-block run that starves it above. Only
    the evidence did: a sample dump cannot say whether this is a basic machine (ceiling
    ``maxAmperesIn``) or a multiblock (2 A per energy hatch), so the adapter states no ceiling and
    the check cannot run. The verdict must therefore NOT read as a pass: ``ok`` stays true, and
    the report says plainly which machine went unmeasured (#114).
    """
    problem = _adapted_single_block(32.0, census=False)
    assert next(p for p in problem.machines[0].faces.ports if p.id == "power:in").max_amps is None
    report = validate(problem, _long_power_run(problem, 22))
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT not in report.codes(), str(report)
    assert report.ok, str(report)
    assert report.unverified_power_intake == ("n0",)
    assert "1 machine(s) unmeasured for power intake" in str(report)


def test_an_unrated_hatch_is_reported_unmeasured_rather_than_skipped() -> None:
    # The hand-built form of the same thing, on the trunk fixture: a machine whose connection
    # states no ceiling comes back named, not silently absent.
    report = validate(*_power_trunk())
    assert report.ok, str(report)
    assert report.unverified_power_intake == ("m0",)


def test_a_machine_on_no_power_route_at_all_is_reported_unmeasured() -> None:
    # The other silent skip #114 names: a machine that never reaches a measured power route never
    # entered the supply sum either, so dropping its route must not read as checked-and-fine.
    problem, layout = _rated_trunk(2.0)
    report = validate(problem, layout.model_copy(update={"routes": []}))
    assert report.unverified_power_intake == ("m0",)


def test_hatches_that_cannot_take_the_draw_in_after_loss_are_flagged() -> None:
    # 1 A at 30 V is 30 EU/t arriving against a 48 EU/t draw: the machine cannot run the recipe the
    # plan balanced. The cable is thick enough for the 1.6 A it would pull, so nothing is wrong
    # with the trunk itself - only the supply direction may fire.
    codes = validate(*_rated_trunk(1.0)).codes()
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT in codes, codes
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT not in codes, codes


def _split_hatch_pair(second_hatch_amps: float) -> tuple[InputIR, LayoutResult]:
    """One machine drawing 48 EU/t through TWO energy hatches on two separate power routes.

    ``mp`` splits its draw 24/24 between ``pi1`` (docked south, on the z=1 run) and ``pi2`` (docked
    up, on the y=1 run), both 3 cable-blocks from ``src`` (29 V after loss). Neither hatch could
    feed it alone at 1 A - 29 EU/t against a 48 EU/t draw - so only the sum ACROSS routes clears
    the bar, which a per-route check would miss. The 24 EU/t share is likewise what keeps each run
    under one amp; billed the whole 48 they would need 2 A and the 1x cables would be too thin.
    """
    src = Machine(
        id="src",
        type="Power Source (LV)",
        voltage_tier="LV",
        eut=0.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(id="po1", commodity=Commodity.POWER, direction=IODirection.OUTPUT),
                Port(id="po2", commodity=Commodity.POWER, direction=IODirection.OUTPUT),
            ]
        ),
    )
    mp = Machine(
        id="mp",
        type="M",
        voltage_tier="LV",
        eut=48.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(
                    id="pi1",
                    commodity=Commodity.POWER,
                    direction=IODirection.INPUT,
                    rate=24.0,
                    max_amps=1.0,
                ),
                Port(
                    id="pi2",
                    commodity=Commodity.POWER,
                    direction=IODirection.INPUT,
                    rate=24.0,
                    max_amps=second_hatch_amps,
                ),
            ]
        ),
    )
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=4, sz=6),
        machines=[src, mp],
        nets=[
            Net(
                id=f"pw{i}",
                commodity=Commodity.POWER,
                throughput=24.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id=f"po{i}"),
                    MachineFaceRef(machine_id="mp", port_id=f"pi{i}"),
                ],
            )
            for i in (1, 2)
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="mp", cell=_coord(3, 0, 0), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="pw1",
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(
                        machine_id="src", port_id="po1", face=Facing.SOUTH, cell=_coord(0, 0, 1)
                    ),
                    Terminal(
                        machine_id="mp", port_id="pi1", face=Facing.SOUTH, cell=_coord(3, 0, 1)
                    ),
                ],
                segments=[
                    Segment(start=_coord(i, 0, 1), end=_coord(i + 1, 0, 1), channel=0)
                    for i in range(3)
                ],
                thickness_per_segment=[1, 1, 1],
            ),
            Route(
                net_id="pw2",
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(machine_id="src", port_id="po2", face=Facing.UP, cell=_coord(0, 1, 0)),
                    Terminal(machine_id="mp", port_id="pi2", face=Facing.UP, cell=_coord(3, 1, 0)),
                ],
                segments=[
                    Segment(start=_coord(i, 1, 0), end=_coord(i + 1, 1, 0), channel=0)
                    for i in range(3)
                ],
                thickness_per_segment=[1, 1, 1],
            ),
        ],
    )
    return problem, layout


def test_energy_hatches_on_separate_routes_sum_their_supply() -> None:
    # 29 + 29 = 58 EU/t reaches a machine that draws 48. Measuring either route on its own would
    # see 29 and call it starved, so passing here is the accumulation across routes working.
    report = validate(*_split_hatch_pair(1.0))
    assert report.ok, str(report)


def test_a_split_machine_is_still_starved_when_the_hatches_together_fall_short() -> None:
    # Halve the second hatch: 29 + 14.5 = 43.5 EU/t against the same 48 EU/t draw. Both cables are
    # still comfortably thick, so the shortfall shows up only in the supply direction.
    codes = validate(*_split_hatch_pair(0.5)).codes()
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT in codes, codes
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT not in codes, codes


def test_an_unrated_split_machine_charges_each_cable_its_whole_draw() -> None:
    # Drop the per-hatch rates from the fixture above and nothing tells the validator a hatch takes
    # only a share, so it falls back to billing all 48 EU/t (1.66 A) to each 1x cable and rejects
    # both. That fallback is what the IR v3 rates replace - and it proves the split fixture passes
    # BECAUSE of them, not because a 1x cable would have carried the whole draw anyway.
    problem, layout = _split_hatch_pair(1.0)
    mp = problem.machines[1]
    ports = [p.model_copy(update={"rate": None}) for p in mp.faces.ports]
    unrated = mp.model_copy(update={"faces": FaceSpec(ports=ports)})
    problem = problem.model_copy(update={"machines": [problem.machines[0], unrated]})
    codes = validate(problem, layout).codes()
    assert ViolationCode.POWER_THICKNESS_INSUFFICIENT in codes, codes


def _summed_trunk_with_a_short_hatch() -> tuple[InputIR, LayoutResult]:
    """The valid summed trunk, with mA's hatch too small: 1 A at 31 V feeds 31 of its 62 EU/t."""
    problem, layout = _power_summed_trunk()
    src, m_a, m_b = problem.machines
    hatch = m_a.faces.ports[0].model_copy(update={"max_amps": 1.0})
    starved = m_a.model_copy(update={"faces": FaceSpec(ports=[hatch])})
    return problem.model_copy(update={"machines": [src, starved, m_b]}), layout


def test_a_short_hatch_on_a_shared_trunk_is_flagged() -> None:
    report = validate(*_summed_trunk_with_a_short_hatch())
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT in report.codes(), report.codes()
    # The starved machine travels structurally, not just in the prose: the shortfall is driven by
    # how far the cable ran, so the solver re-places that machine and needs to know which it is.
    starved = [v for v in report.violations if v.code is ViolationCode.POWER_SUPPLY_INSUFFICIENT]
    assert [v.machine_id for v in starved] == ["mA"]


def test_a_machine_on_an_unverifiable_route_is_not_reported_as_starved() -> None:
    # Same short hatch, but mB further along the same trunk now has an off-ladder tier, so the
    # route's arithmetic stopped before it finished measuring. The gate reports what it could not
    # verify and withholds the supply verdict rather than issuing one on partial evidence.
    problem, layout = _summed_trunk_with_a_short_hatch()
    src, m_a, m_b = problem.machines
    weird = m_b.model_copy(update={"voltage_tier": "NOPE"})
    problem = problem.model_copy(update={"machines": [src, m_a, weird]})
    codes = validate(problem, layout).codes()
    assert ViolationCode.POWER_TIER_UNKNOWN in codes, codes
    assert ViolationCode.POWER_SUPPLY_INSUFFICIENT not in codes, codes


# --------------------------------------------------------------- auto-output connections


def _auto_machine(mid: str, port: str, direction: IODirection) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[Port(id=port, commodity=Commodity.ITEM, direction=direction)]),
    )


def _auto_base() -> tuple[InputIR, LayoutResult]:
    """m1 (out) auto-feeds adjacent m2 (in) on east/west - a fully valid auto-connection."""
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            _auto_machine("m1", "out", IODirection.OUTPUT),
            _auto_machine("m2", "in", IODirection.INPUT),
        ],
        nets=[
            Net(
                id="n",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="m1", port_id="out"),
                    MachineFaceRef(machine_id="m2", port_id="in"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="m1", cell=_coord(0, 0, 0), orientation=Facing.NORTH),
            Placement(machine_id="m2", cell=_coord(1, 0, 0), orientation=Facing.NORTH),
        ],
        auto_connections=[
            AutoConnection(
                net_id="n",
                source_machine_id="m1",
                source_face=Facing.EAST,
                target_machine_id="m2",
                target_face=Facing.WEST,
            )
        ],
    )
    return problem, layout


def test_auto_connection_valid_passes() -> None:
    assert validate(*_auto_base()).ok


def _auto_front(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    ac = layout.auto_connections[0].model_copy(update={"source_face": Facing.NORTH})  # front
    return p, layout.model_copy(update={"auto_connections": [ac]})


def _auto_not_adjacent(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    ac = layout.auto_connections[0].model_copy(
        update={"source_face": Facing.SOUTH}
    )  # not toward m2
    return p, layout.model_copy(update={"auto_connections": [ac]})


def _auto_duplicate(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    ac = layout.auto_connections[0]
    return p, layout.model_copy(update={"auto_connections": [ac, ac]})  # m1 auto-outputs twice


def _auto_wrong_endpoints(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # geometry is fine (m2 auto-outputs west into the adjacent m1), but that reverses the net's
    # real output->input direction: m1 is the source, m2 the sink. Two unrelated adjacent
    # machines must not be able to "claim" a net they are not the endpoints of.
    ac = layout.auto_connections[0].model_copy(
        update={
            "source_machine_id": "m2",
            "source_face": Facing.WEST,
            "target_machine_id": "m1",
            "target_face": Facing.EAST,
        }
    )
    return p, layout.model_copy(update={"auto_connections": [ac]})


def _auto_illegal_commodity(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    # an ME-routed commodity lives on the ME network; it must not be physically auto-connected
    return p.model_copy(update={"me_toggles": METoggles(items=True)}), layout


def _auto_unknown_net(p: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    ac = layout.auto_connections[0].model_copy(update={"net_id": "ghost"})
    return p, layout.model_copy(update={"auto_connections": [ac]})


AUTO_BAD_CASES: list[tuple[str, Mutator, ViolationCode]] = [
    ("auto_front", _auto_front, ViolationCode.AUTO_OUTPUT_ON_FRONT_FACE),
    ("auto_not_adjacent", _auto_not_adjacent, ViolationCode.AUTO_OUTPUT_NOT_ADJACENT),
    ("auto_duplicate", _auto_duplicate, ViolationCode.DUPLICATE_AUTO_OUTPUT),
    ("auto_wrong_endpoints", _auto_wrong_endpoints, ViolationCode.AUTO_OUTPUT_WRONG_ENDPOINTS),
    (
        "auto_illegal_commodity",
        _auto_illegal_commodity,
        ViolationCode.AUTO_OUTPUT_ILLEGAL_COMMODITY,
    ),
    ("auto_unknown_net", _auto_unknown_net, ViolationCode.UNKNOWN_NET),
]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [(m, c) for _, m, c in AUTO_BAD_CASES],
    ids=[n for n, _, _ in AUTO_BAD_CASES],
)
def test_known_bad_auto_connection_is_flagged(mutate: Mutator, code: ViolationCode) -> None:
    problem, layout = mutate(*_auto_base())
    report = validate(problem, layout)
    assert not report.ok
    assert code in report.codes(), f"expected {code} in {report.codes()}"


def test_net_with_both_route_and_auto_connection_is_flagged() -> None:
    problem, layout = _auto_base()
    route = Route(
        net_id="n",
        commodity=Commodity.ITEM,
        terminals=[
            Terminal(machine_id="m1", port_id="out", face=Facing.SOUTH, cell=_coord(0, 0, 1)),
            Terminal(machine_id="m2", port_id="in", face=Facing.SOUTH, cell=_coord(1, 0, 1)),
        ],
        segments=[Segment(start=_coord(0, 0, 1), end=_coord(1, 0, 1), channel=0)],
    )
    layout = layout.model_copy(update={"routes": [route]})
    assert ViolationCode.NET_DOUBLE_CONNECTED in validate(problem, layout).codes()


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


# ------------------------------------------------------------------------------------------------
# Route material: the gate re-derives the stand-in from the layout, not from the router (#4)
# ------------------------------------------------------------------------------------------------


def _sand_power_layout() -> tuple[InputIR, LayoutResult]:
    """A solved sand line - the smallest real layout that publishes a cable material."""
    problem = adapt_file("examples/gtnh-sand.json", physical=load_physical_dataset())
    return problem, solve(problem, optimize=False)


def _codes(problem: InputIR, layout: LayoutResult) -> set[ViolationCode]:
    return {v.code for v in validate(problem, layout).violations}


def _with_material(layout: LayoutResult, material: RouteMaterial | None) -> LayoutResult:
    power = next(r for r in layout.routes if r.commodity is Commodity.POWER)
    others = [r for r in layout.routes if r is not power]
    return layout.model_copy(
        update={"routes": [*others, power.model_copy(update={"material": material})]}
    )


def test_a_real_solve_publishes_a_material_the_validator_accepts() -> None:
    problem, layout = _sand_power_layout()
    power = next(r for r in layout.routes if r.commodity is Commodity.POWER)
    assert power.material is not None
    assert power.material.material == "tin"
    assert power.material.tier == "LV"
    assert not _codes(problem, layout) & {
        ViolationCode.ROUTE_MATERIAL_TIER_MISMATCH,
        ViolationCode.ROUTE_MATERIAL_UNKNOWN,
    }


def test_no_material_is_not_a_violation() -> None:
    """``None`` is what every route said before the field existed, so it stays valid - otherwise
    the golden corpus and every hand-built Route in this suite would start failing."""
    problem, layout = _sand_power_layout()
    assert not _codes(problem, _with_material(layout, None)) & {
        ViolationCode.ROUTE_MATERIAL_TIER_MISMATCH,
        ViolationCode.ROUTE_MATERIAL_UNKNOWN,
    }


def test_a_cable_rated_for_the_wrong_tier_is_caught() -> None:
    """The check with teeth: the tier is re-derived from the machines the route terminates at, so a
    route whose terminals moved without its material following is caught even though the router and
    the contract both think it is fine."""
    problem, layout = _sand_power_layout()
    hv = RouteMaterial(family=PipeFamily.CABLE, material="gold", tier="HV")
    assert ViolationCode.ROUTE_MATERIAL_TIER_MISMATCH in _codes(problem, _with_material(layout, hv))


def test_an_invented_material_is_caught() -> None:
    """A cable drawn in a material outside the policy is the unrecoverable failure - plausible,
    confident and wrong - so the gate refuses it rather than trusting the producer."""
    problem, layout = _sand_power_layout()
    invented = RouteMaterial(family=PipeFamily.CABLE, material="cobalt", tier="LV")
    assert ViolationCode.ROUTE_MATERIAL_UNKNOWN in _codes(problem, _with_material(layout, invented))


# ------------------------------------------- the hatches a layout NEEDS (as opposed to has)
#
# ``_check_hatches`` above validates the hatches a layout *records*. These cover the other
# direction: a connection, or a machine's own upkeep, that needs a block and was given none. A
# multiblock does no I/O itself - the connection IS a hatch - so a route docked against plain
# casing forms a structure that then moves nothing, and a controller with no maintenance hatch
# does not form at all.


def _slot(x: int, y: int, z: int, *kinds: str) -> HatchSlot:
    return HatchSlot(offset=_coord(x, y, z), kinds=kinds)


def _multiblock(mid: str, ports: list[Port], slots: list[HatchSlot], *, width: int) -> Machine:
    """A ``width`` x 1 x 1 multiblock whose structure records ``slots`` (so it wants hatches)."""
    return Machine(
        id=mid,
        type="gt.large_chemical_reactor",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=width, sy=1, sz=1),
        faces=FaceSpec(ports=ports),
        hatch_slots=tuple(slots),
        hatch_cells=len(slots),
    )


#: The three ports of ``_multiblock_line``'s multiblock, in the order their hatches are emitted.
_MB_PORTS = ("in1", "in2", "out")


def _multiblock_line() -> tuple[InputIR, LayoutResult]:
    """A valid line whose one multiblock is wired through three hatches, plus its maintenance one.

    ``mb`` spans (2..5, 0, 2) facing north, with a recorded casing cell per connection and a
    fourth that accepts a ``Maintenance`` hatch. Each of its three single-block partners docks on
    its own column, so no two routes share a cell.
    """
    partners = [
        _item_machine("p1"),
        _item_machine("p2"),
        _item_machine("p3", direction=IODirection.INPUT, port="in"),
    ]
    mb = _multiblock(
        "mb",
        [
            Port(id="in1", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="in2", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
        ],
        [
            _slot(0, 0, 0, "InputBus"),
            _slot(1, 0, 0, "InputBus"),
            _slot(2, 0, 0, "OutputBus"),
            _slot(3, 0, 0, "Maintenance"),
        ],
        width=4,
    )
    wiring = [("n1", "p1", "out", "mb", "in1", 2), ("n2", "p2", "out", "mb", "in2", 3)]
    wiring.append(("n3", "mb", "out", "p3", "in", 4))
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[*partners, mb],
        nets=[
            Net(
                id=nid,
                commodity=Commodity.ITEM,
                fluid_or_item="gt.dust.iron",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id=src, port_id=src_port),
                    MachineFaceRef(machine_id=dst, port_id=dst_port),
                ],
            )
            for nid, src, src_port, dst, dst_port, _ in wiring
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            # the partners face south, so their usable north face looks back at mb
            Placement(machine_id="p1", cell=_coord(2, 0, 5), orientation=Facing.SOUTH),
            Placement(machine_id="p2", cell=_coord(3, 0, 5), orientation=Facing.SOUTH),
            Placement(machine_id="p3", cell=_coord(4, 0, 5), orientation=Facing.SOUTH),
            Placement(machine_id="mb", cell=_coord(2, 0, 2), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id=nid,
                commodity=Commodity.ITEM,
                terminals=[
                    Terminal(
                        machine_id=partner,
                        port_id=partner_port,
                        face=Facing.NORTH,
                        cell=_coord(x, 0, 4),
                    ),
                    Terminal(
                        machine_id="mb", port_id=mb_port, face=Facing.SOUTH, cell=_coord(x, 0, 3)
                    ),
                ],
                segments=[Segment(start=_coord(x, 0, 4), end=_coord(x, 0, 3), channel=0)],
            )
            for nid, partner, partner_port, mb_port, x in (
                ("n1", "p1", "out", "in1", 2),
                ("n2", "p2", "out", "in2", 3),
                ("n3", "p3", "in", "out", 4),
            )
        ],
        hatches=[
            PlacedHatch(
                machine_id="mb",
                kind=kind,
                cell=_coord(x, 0, 2),
                facing=Facing.SOUTH,
                port_id=port_id,
            )
            for kind, x, port_id in (
                ("InputBus", 2, "in1"),
                ("InputBus", 3, "in2"),
                ("OutputBus", 4, "out"),
                ("Maintenance", 5, None),
            )
        ],
    )
    return problem, layout


def _without_hatches(layout: LayoutResult, *port_ids: str | None) -> LayoutResult:
    return layout.model_copy(
        update={"hatches": [h for h in layout.hatches if h.port_id not in port_ids]}
    )


def test_a_multiblock_wired_through_every_hatch_it_needs_passes() -> None:
    # The satisfiable direction first: the gate has to accept the layout a correct producer emits,
    # or the check it adds is a false infeasibility rather than a safety net.
    problem, layout = _multiblock_line()
    report = validate(problem, layout)
    assert report.ok, str(report)


def test_a_routed_port_whose_hatch_was_removed_is_rejected() -> None:
    """The gap #119 describes: routing docks a pipe there, and nothing is there to receive it.

    Every other hatch check passes on this layout - the remaining hatches sit on real body cells,
    face outward, collide with nothing, and agree with their terminals. Only the absence is wrong.
    """
    problem, layout = _multiblock_line()
    report = validate(problem, _without_hatches(layout, "in2"))
    assert not report.ok
    assert ViolationCode.PORT_HATCH_MISSING in report.codes()
    assert "'in2'" in str(report)


@given(st.lists(st.booleans(), min_size=3, max_size=3))
def test_exactly_the_ports_left_without_a_hatch_are_reported(dropped: list[bool]) -> None:
    """Both directions at once, over every subset: each hatch removed is reported once, and each
    hatch kept is reported not at all. A check that fired on a port that HAS its hatch would turn
    valid layouts infeasible, which is the failure the solver's contract cannot absorb."""
    problem, layout = _multiblock_line()
    gone = {port for port, drop in zip(_MB_PORTS, dropped, strict=True) if drop}
    report = validate(problem, _without_hatches(layout, *gone))
    missing = [v for v in report.violations if v.code is ViolationCode.PORT_HATCH_MISSING]
    assert len(missing) == len(gone)
    assert all(v.machine_id == "mb" for v in missing)
    for port in gone:
        assert any(f"'{port}'" in v.message for v in missing)
    if not gone:
        assert report.ok, str(report)


def test_a_single_block_machines_port_needs_no_hatch() -> None:
    """A single-block machine IS its own I/O: its faces are the machine's, not a hatch's, so a
    bus at its cell would describe replacing the machine with a bus. ``_base`` records no hatch
    slots and no hatches, and must stay clean - as must every plan adapted with no dataset."""
    problem, layout = _base()
    assert layout.hatches == []
    assert ViolationCode.PORT_HATCH_MISSING not in _codes(problem, layout)


def test_an_me_toggled_port_needs_no_hatch() -> None:
    """A toggled commodity is removed from physical routing, so its ports attach to nothing.

    Demanding a hatch here would reject a layout that is not merely valid but *required* to look
    like this (a routed ME net is ``UNEXPECTED_ME_ROUTE``).
    """
    problem, layout = _multiblock_line()
    toggled = problem.model_copy(update={"me_toggles": METoggles(items=True)})
    unrouted = _without_hatches(layout, *_MB_PORTS).model_copy(update={"routes": []})
    report = validate(toggled, unrouted)
    assert report.ok, str(report)


def _mixed_commodity_line() -> tuple[InputIR, LayoutResult]:
    """One multiblock taking ME-toggled items on one port and a piped fluid on another.

    The shape above is degenerate: toggling items there also strips every route and every hatch,
    so the machine keeps no physical connection of any kind and "no hatch is needed" holds for the
    trivial reason that nothing is routed. This is the production shape instead - a line does not
    go all-ME or all-pipe, it puts one commodity on the ME network and pipes the rest - and it is
    what the per-net commodity filter in ``_connected_ports`` actually implements: the exemption
    is per (machine, port), not per machine.
    """
    mb = _multiblock(
        "mb",
        [
            Port(id="items", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="fluid", commodity=Commodity.FLUID, direction=IODirection.INPUT),
        ],
        [_slot(0, 0, 0, "InputBus"), _slot(1, 0, 0, "InputHatch"), _slot(2, 0, 0, "Maintenance")],
        width=3,
    )
    feeder = _item_machine("p-item")
    tank = Machine(
        id="p-fluid",
        type="gt.tank",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH],
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.FLUID, direction=IODirection.OUTPUT)]
        ),
    )
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[feeder, tank, mb],
        nets=[
            Net(
                id=nid,
                commodity=commodity,
                fluid_or_item=resource,
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id=src, port_id="out"),
                    MachineFaceRef(machine_id="mb", port_id=port),
                ],
            )
            for nid, commodity, resource, src, port in (
                ("n-item", Commodity.ITEM, "gt.dust.iron", "p-item", "items"),
                ("n-fluid", Commodity.FLUID, "water", "p-fluid", "fluid"),
            )
        ],
        me_toggles=METoggles(items=True),  # items ride the ME network; the fluid is piped
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            # the partners face south, so their usable north face looks back at mb
            Placement(machine_id="p-item", cell=_coord(2, 0, 5), orientation=Facing.SOUTH),
            Placement(machine_id="p-fluid", cell=_coord(3, 0, 5), orientation=Facing.SOUTH),
            Placement(machine_id="mb", cell=_coord(2, 0, 2), orientation=Facing.NORTH),
        ],
        # only the fluid is physically routed; the ME-toggled item net is not, and must not be
        routes=[
            Route(
                net_id="n-fluid",
                commodity=Commodity.FLUID,
                terminals=[
                    Terminal(
                        machine_id="p-fluid", port_id="out", face=Facing.NORTH, cell=_coord(3, 0, 4)
                    ),
                    Terminal(
                        machine_id="mb", port_id="fluid", face=Facing.SOUTH, cell=_coord(3, 0, 3)
                    ),
                ],
                segments=[Segment(start=_coord(3, 0, 4), end=_coord(3, 0, 3), channel=0)],
            )
        ],
        hatches=[
            PlacedHatch(
                machine_id="mb",
                kind="InputHatch",
                cell=_coord(3, 0, 2),
                facing=Facing.SOUTH,
                port_id="fluid",
            ),
            PlacedHatch(
                machine_id="mb", kind="Maintenance", cell=_coord(4, 0, 2), facing=Facing.SOUTH
            ),
        ],
    )
    return problem, layout


def test_one_commodity_on_me_beside_another_routed_on_the_same_multiblock_is_clean() -> None:
    """Neither port is reported: not the ME-toggled one (nothing is built for it) and not the
    piped one (its hatch is there). Getting this wrong in either direction breaks the solver's
    contract - a false infeasibility on the toggled port, or a silent pass on the piped one."""
    problem, layout = _mixed_commodity_line()
    report = validate(problem, layout)
    assert report.ok, str(report)
    assert ViolationCode.PORT_HATCH_MISSING not in report.codes()


def test_the_routed_commodity_is_still_checked_when_its_neighbour_is_me_toggled() -> None:
    """The teeth half of the same layout: the exemption covers the toggled commodity only, so the
    piped port on that very machine is still required to have its hatch. Were the filter written
    per machine rather than per port, this would pass silently."""
    problem, layout = _mixed_commodity_line()
    report = validate(problem, _without_hatches(layout, "fluid"))
    assert report.codes() == (ViolationCode.PORT_HATCH_MISSING,)
    assert "'fluid'" in str(report)
    assert "'items'" not in str(report)


def test_a_port_no_net_names_needs_no_hatch() -> None:
    """Having a port does not imply needing a hatch: an output no net consumes is closed by a
    boundary storage rather than a route, and a feed the plan never drew is wired by hand."""
    problem, layout = _multiblock_line()
    mb = next(m for m in problem.machines if m.id == "mb")
    spare = Port(id="spare", commodity=Commodity.FLUID, direction=IODirection.OUTPUT)
    widened = mb.model_copy(
        update={
            "faces": FaceSpec(ports=[*mb.faces.ports, spare]),
            "hatch_cells": len(mb.hatch_slots),
        }
    )
    others = [m for m in problem.machines if m.id != "mb"]
    report = validate(problem.model_copy(update={"machines": [*others, widened]}), layout)
    assert report.ok, str(report)


def test_a_net_that_was_never_connected_reports_the_connection_not_the_hatch() -> None:
    """Nothing was built for an unrealized net, so there is no connection to host: reporting a
    missing hatch on top would double-report ``MISSING_CONNECTION`` and point at the wrong fix."""
    problem, layout = _multiblock_line()
    kept = [r for r in layout.routes if r.net_id != "n2"]
    codes = _codes(problem, _without_hatches(layout, "in2").model_copy(update={"routes": kept}))
    assert ViolationCode.MISSING_CONNECTION in codes
    assert ViolationCode.PORT_HATCH_MISSING not in codes


def _auto_multiblocks() -> tuple[InputIR, LayoutResult]:
    """Two multiblocks that eject into each other with no pipe: an output bus meeting an input bus.

    GT's output bus pushes into whatever inventory sits on its own front face, so a free
    connection is still two casing cells spent - which is exactly what a producer could forget.
    """
    source = _multiblock(
        "src",
        [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
        [_slot(1, 0, 0, "OutputBus")],
        width=2,
    )
    target = _multiblock(
        "dst",
        [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)],
        [_slot(0, 0, 0, "InputBus")],
        width=2,
    )
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[source, target],
        nets=[
            Net(
                id="n",
                commodity=Commodity.ITEM,
                fluid_or_item="gt.dust.iron",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="out"),
                    MachineFaceRef(machine_id="dst", port_id="in"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=_coord(2, 0, 2), orientation=Facing.NORTH),
            Placement(machine_id="dst", cell=_coord(4, 0, 2), orientation=Facing.NORTH),
        ],
        auto_connections=[
            AutoConnection(
                net_id="n",
                source_machine_id="src",
                source_face=Facing.EAST,
                target_machine_id="dst",
                target_face=Facing.WEST,
            )
        ],
        hatches=[
            PlacedHatch(
                machine_id="src",
                kind="OutputBus",
                cell=_coord(3, 0, 2),
                facing=Facing.EAST,
                port_id="out",
            ),
            PlacedHatch(
                machine_id="dst",
                kind="InputBus",
                cell=_coord(4, 0, 2),
                facing=Facing.WEST,
                port_id="in",
            ),
        ],
    )
    return problem, layout


def test_a_free_auto_output_between_multiblocks_still_needs_both_hatches() -> None:
    problem, layout = _auto_multiblocks()
    assert validate(problem, layout).ok


@pytest.mark.parametrize("port", ["out", "in"])
def test_an_auto_output_side_with_no_hatch_is_rejected(port: str) -> None:
    # No pipe is laid, so no terminal exists to check - the ports come from the net's own
    # endpoints. Either side left as plain casing means the ejection has nothing to push into.
    problem, layout = _auto_multiblocks()
    report = validate(problem, _without_hatches(layout, port))
    assert ViolationCode.PORT_HATCH_MISSING in report.codes()
    assert f"'{port}'" in str(report)


def test_an_auto_output_naming_a_machine_the_problem_lacks_asks_for_no_hatch() -> None:
    """A ghost endpoint is ``AUTO_OUTPUT_WRONG_ENDPOINTS``' to report. There is no structure to
    read a port off, so the hatch check has nothing to require and must not invent one."""
    problem, layout = _auto_multiblocks()
    ghosted = layout.auto_connections[0].model_copy(update={"target_machine_id": "ghost"})
    codes = _codes(problem, layout.model_copy(update={"auto_connections": [ghosted]}))
    assert ViolationCode.AUTO_OUTPUT_WRONG_ENDPOINTS in codes
    assert ViolationCode.PORT_HATCH_MISSING not in codes


# ------------------------------------------------------- the upkeep hatches (maintenance/muffler)


def test_a_multiblock_with_no_maintenance_hatch_is_rejected() -> None:
    """``mMaintenanceHatches.size() == 1`` is asserted in a dozen-odd ``checkMachine``
    implementations, so this structure never forms - the failure the gate exists to prevent, and
    the one it used to check for the muffler only (#116)."""
    problem, layout = _multiblock_line()
    report = validate(problem, _without_hatches(layout, None))
    assert not report.ok
    assert ViolationCode.MAINTENANCE_MISSING in report.codes()
    assert [v.machine_id for v in report.violations] == ["mb"]


def test_a_structure_that_records_no_maintenance_cell_is_not_asked_for_one() -> None:
    """The permissive half, and the one that matters most: a slot's kinds are a lower bound, and
    35 of 208 dumped controllers record no ``Maintenance`` cell at all. Reading that silence as a
    prohibition would manufacture a false infeasibility across a sixth of the dataset."""
    problem, layout = _auto_multiblocks()  # records only OutputBus / InputBus cells
    assert ViolationCode.MAINTENANCE_MISSING not in _codes(problem, layout)


def test_a_machine_with_no_recorded_structure_is_asked_for_no_upkeep_hatch() -> None:
    # A single block has no casing cells to spend, so neither upkeep hatch applies to it.
    problem, layout = _base()
    assert not _codes(problem, layout) & {
        ViolationCode.MAINTENANCE_MISSING,
        ViolationCode.MUFFLER_MISSING,
    }


def _upkeep_only(*kinds: str, width: int = 4) -> tuple[InputIR, LayoutResult]:
    """One multiblock and nothing else: every casing cell accepts ``kinds``, no net, no route.

    An upkeep hatch serves no port, so a machine with no connections at all is where counting them
    is cleanest - nothing else in the report can be mistaken for the count's own verdict.
    """
    mb = _multiblock("mb", [], [_slot(i, 0, 0, *kinds) for i in range(width)], width=width)
    return (
        InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[mb], nets=[]),
        LayoutResult(
            status=LayoutStatus.VALID,
            seed=0,
            placements=[Placement(machine_id="mb", cell=_coord(2, 0, 2), orientation=Facing.NORTH)],
        ),
    )


def _with_upkeep(layout: LayoutResult, kind: str, count: int) -> LayoutResult:
    """``count`` hatches of ``kind``, each on its own casing cell, each facing free air.

    Distinct cells on purpose: that is what makes the count the *only* thing wrong. Stacked on one
    cell they would be ``HATCH_CELL_COLLISION``, which is a different defect with a different fix.
    """
    return layout.model_copy(
        update={
            "hatches": [
                PlacedHatch(
                    machine_id="mb", kind=kind, cell=_coord(2 + i, 0, 2), facing=Facing.SOUTH
                )
                for i in range(count)
            ]
        }
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, {ViolationCode.MAINTENANCE_MISSING}),
        (1, set()),
        (2, {ViolationCode.MAINTENANCE_DUPLICATE}),
        (3, {ViolationCode.MAINTENANCE_DUPLICATE}),
    ],
)
def test_a_structure_needs_exactly_one_maintenance_hatch(
    count: int, expected: set[ViolationCode]
) -> None:
    """GT reads the count, not the presence: 57 of the 64 controllers that touch
    ``mMaintenanceHatches`` assert ``size() == 1`` and the remaining 7 demand ``<= 1``, so none of
    them forms with two. Every one of these layouts satisfies every other hatch check - real body
    cells, outward facings, one hatch per cell - so the count is the only thing separating them."""
    problem, layout = _upkeep_only("Maintenance")
    assert _codes(problem, _with_upkeep(layout, "Maintenance", count)) == expected


def test_the_duplicate_maintenance_report_names_how_many_there_are() -> None:
    # "place one" and "remove two" are different fixes, so the report has to say which it is.
    problem, layout = _upkeep_only("Maintenance")
    report = validate(problem, _with_upkeep(layout, "Maintenance", 3))
    assert "carries 3 Maintenance hatches" in str(report)


def test_several_mufflers_are_not_a_violation() -> None:
    """The muffler deliberately keeps the weaker rule. ``MTEMultiBlockBase.polluteEnvironment``
    divides the vent batch across however many mufflers the controller has, and controllers assert
    2 of them (Nuclear Salt Processing Plant) or 4 (Nuclear Reactor, the larger turbines), so
    demanding exactly one here would reject structures GT requires."""
    problem, layout = _upkeep_only("Muffler")
    assert _codes(problem, _with_upkeep(layout, "Muffler", 3)) == set()
