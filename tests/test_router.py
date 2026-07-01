"""Tests for the Phase 1 crude router.

Headline: the real sand line now goes export -> place -> route -> validator.ok, the whole
thin slice end to end. The rest are synthetic cases for the routing/docking branches and the
never-silently-invalid promise (incompleteness is always an explicit infeasibility).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from gtnh_solver.adapter import adapt_file
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
    Placement,
    Port,
    Route,
)
from gtnh_solver.placement import place
from gtnh_solver.router import route, route_power
from gtnh_solver.router.core import _route_pass
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"

_MALFORMED_ROUTE_CODES = {
    ViolationCode.ROUTE_OUT_OF_BOUNDS,
    ViolationCode.ROUTE_DISCONTINUOUS,
    ViolationCode.ROUTE_COMMODITY_MISMATCH,
    ViolationCode.MISSING_TERMINAL,
    ViolationCode.TERMINAL_ON_FRONT_FACE,
    ViolationCode.TERMINAL_NOT_ADJACENT,
    ViolationCode.TERMINAL_NOT_ON_ROUTE,
}


def _machine(mid: str, ports: list[Port], *, orientation: Facing = Facing.NORTH) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[orientation],
        faces=FaceSpec(ports=ports),
    )


def _item_pair(region: CellBox) -> InputIR:
    a = _machine("a", [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)])
    b = _machine("b", [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)])
    net = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="out"),
            MachineFaceRef(machine_id="b", port_id="in"),
        ],
    )
    return InputIR(bounding_region=region, machines=[a, b], nets=[net])


def _at(mid: str, x: int, y: int, z: int) -> Placement:
    return Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=Facing.NORTH)


def _route_cells(route: Route) -> set[tuple[int, int, int]]:
    cells: set[tuple[int, int, int]] = set()
    for seg in route.segments:
        cells.add((seg.start.x, seg.start.y, seg.start.z))
        cells.add((seg.end.x, seg.end.y, seg.end.z))
    return cells


def _route_cells_of(routes: Iterable[Route]) -> set[tuple[int, int, int]]:
    cells: set[tuple[int, int, int]] = set()
    for r in routes:
        cells |= _route_cells(r)
    return cells


# --------------------------------------------------------------- real fixtures


def test_route_sand_full_slice_validates() -> None:
    # The generic router handles item/fluid; the power router handles power. Composed
    # capacity-aware (the item cells become obstacles for the cables, as the solver does), they
    # cover every net of the sand line collision-free, and the combined layout validates.
    #
    # A roomy hand-placement is used on purpose: the constructive packing is built for AUTO-OUTPUT
    # (zero pipes), so forcing every item net through a *pipe* needs routing room. Under the
    # single-channel capacity the packed row cannot host four non-overlapping pipes - it used to
    # "validate" only because the old router silently overlapped them (now ROUTE_CELL_COLLISION).
    ir = adapt_file(_SAND).model_copy(update={"bounding_region": CellBox(sx=20, sy=4, sz=20)})
    # 6 machines now: 3 hammers, the input chest, the synthesized output buffer (#16), the power
    # source. Placed interior and well-spaced so every machine keeps free faces to dock its ports
    # and the four item pipes + the power trunk have room to route without collision.
    coords = [(2, 0, 2), (8, 0, 2), (14, 0, 2), (14, 0, 8), (2, 0, 8), (8, 0, 14)]
    placements = [
        Placement(machine_id=m.id, cell=CellCoord(x=x, y=y, z=z), orientation=Facing.NORTH)
        for m, (x, y, z) in zip(ir.machines, coords, strict=True)
    ]
    rr = route(ir, placements)
    item_cells = _route_cells_of(rr.routes)
    pwr = route_power(ir, placements, extra_obstacles=item_cells)
    assert rr.ok
    assert pwr.ok
    assert all(r.commodity is not Commodity.POWER for r in rr.routes)  # power is not its job
    assert all(r.commodity is Commodity.POWER for r in pwr.routes)
    assert len(rr.routes) + len(pwr.routes) == len(ir.nets)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        routes=[*rr.routes, *pwr.routes],
    )
    assert validate(ir, layout).ok, str(validate(ir, layout))


def test_route_emits_only_valid_routes_even_when_incomplete() -> None:
    # nitrobenzene's many-port multiblocks overflow crude 1x1x1 faces, so routing is
    # incomplete - but every route it DOES emit is sound, and incompleteness is explicit.
    ir = adapt_file(_NITROBENZENE)
    pr = place(ir)
    rr = route(ir, pr.placements)
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=list(pr.placements), routes=list(rr.routes)
    )
    assert _MALFORMED_ROUTE_CODES.isdisjoint(validate(ir, layout).codes())
    if not rr.ok:
        assert rr.infeasibility is not None


# --------------------------------------------------------------- synthetic


def test_route_two_machines_ok_and_validates() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    placements = [_at("a", 1, 0, 1), _at("b", 3, 0, 1)]
    result = route(problem, placements)
    assert result.ok
    assert len(result.routes) == 1
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    assert validate(problem, layout).ok


def test_route_two_crossing_nets_do_not_share_a_cell() -> None:
    # Two item nets whose shortest paths cross near the centre. Routing is capacity-aware - the
    # first net's cells become obstacles for the second - so they never share a cell (which would
    # be unbuildable single-channel). Routed independently they overlap at the crossing.
    def m(mid: str, port: str, direction: IODirection) -> Machine:
        return _machine(mid, [Port(id=port, commodity=Commodity.ITEM, direction=direction)])

    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=4, sz=7),
        machines=[
            m("a", "o", IODirection.OUTPUT),
            m("b", "i", IODirection.INPUT),
            m("c", "o", IODirection.OUTPUT),
            m("d", "i", IODirection.INPUT),
        ],
        nets=[
            Net(
                id="n1",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="a", port_id="o"),
                    MachineFaceRef(machine_id="b", port_id="i"),
                ],
            ),
            Net(
                id="n2",
                commodity=Commodity.ITEM,
                fluid_or_item="y",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="c", port_id="o"),
                    MachineFaceRef(machine_id="d", port_id="i"),
                ],
            ),
        ],
    )
    placements = [_at("a", 0, 0, 3), _at("b", 6, 0, 3), _at("c", 3, 0, 1), _at("d", 3, 0, 5)]
    result = route(problem, placements)
    assert result.ok
    assert len(result.routes) == 2
    assert _route_cells(result.routes[0]).isdisjoint(_route_cells(result.routes[1]))
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    report = validate(problem, layout)
    assert ViolationCode.ROUTE_CELL_COLLISION not in report.codes()


def test_rip_up_reroute_fixes_an_ordering_induced_failure() -> None:
    # A wall at z=3 with two gaps (x=1 and x=5); x=2 is walled for z<3, making a top-left pocket
    # (x=0..1, z=0..2) whose only exit down is gap x=1. net2 (c in the pocket -> d below) can ONLY
    # cross via gap x=1; net1 (a top-right -> b below) prefers gap x=1 but can detour to gap x=5.
    # In problem order net1 grabs gap x=1 first and wedges net2 out; rip-up/reroute reorders net2
    # first and net1 detours - so capacity-aware routing is not hostage to net order.
    def m(mid: str, direction: IODirection) -> Machine:
        return _machine(mid, [Port(id="p", commodity=Commodity.ITEM, direction=direction)])

    reserved = [CellCoord(x=x, y=0, z=3) for x in range(7) if x not in (1, 5)] + [
        CellCoord(x=2, y=0, z=z) for z in range(3)
    ]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=6),
        machines=[
            m("a", IODirection.OUTPUT),
            m("b", IODirection.INPUT),
            m("c", IODirection.OUTPUT),
            m("d", IODirection.INPUT),
        ],
        nets=[
            Net(
                id="n1",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="a", port_id="p"),
                    MachineFaceRef(machine_id="b", port_id="p"),
                ],
            ),
            Net(
                id="n2",
                commodity=Commodity.ITEM,
                fluid_or_item="y",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="c", port_id="p"),
                    MachineFaceRef(machine_id="d", port_id="p"),
                ],
            ),
        ],
        reserved_cells=reserved,
    )
    placements = [_at("a", 3, 0, 0), _at("b", 0, 0, 5), _at("c", 0, 0, 0), _at("d", 0, 0, 4)]

    # One greedy pass in problem order wedges net2 out (net1 took the pocket's only exit)...
    _, failures = _route_pass(problem, placements, problem.nets)
    assert failures, "expected the problem-order pass to fail a net"
    # ...but rip-up/reroute reorders (failed net first) and routes both, collision-free.
    result = route(problem, placements)
    assert result.ok, result.infeasibility
    assert len(result.routes) == 2
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    report = validate(problem, layout)
    assert report.ok, str(report)


def test_route_terminals_avoid_the_front_face() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [_at("a", 1, 0, 1), _at("b", 3, 0, 1)])
    faces = [t.face for r in result.routes for t in r.terminals]
    assert faces  # there are terminals
    assert all(face is not Facing.NORTH for face in faces)  # north is the front (orientation)


def test_route_skips_me_toggled_commodity() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8)).model_copy(
        update={"me_toggles": METoggles(items=True)}
    )
    result = route(problem, [_at("a", 1, 0, 1), _at("b", 3, 0, 1)])
    assert result.ok
    assert result.routes == ()  # the item net is ME-toggled, not physically routed


def test_route_infeasible_when_a_machine_cannot_dock() -> None:
    # 2x1x1: the two machines fill the region, leaving no free non-front face to dock.
    problem = _item_pair(CellBox(sx=2, sy=1, sz=1))
    result = route(problem, [_at("a", 0, 0, 0), _at("b", 1, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"


def test_route_infeasible_when_no_path_between_terminals() -> None:
    # A reserved wall at x=1 splits the single-layer region; terminals can't connect.
    problem = _item_pair(CellBox(sx=3, sy=1, sz=3)).model_copy(
        update={
            "reserved_cells": [
                CellCoord(x=1, y=0, z=0),
                CellCoord(x=1, y=0, z=1),
                CellCoord(x=1, y=0, z=2),
            ]
        }
    )
    result = route(problem, [_at("a", 0, 0, 0), _at("b", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "routing"
    assert result.failed_nets == ("n",)  # the unrouted net, for the solver's feedback loop


def test_route_infeasible_when_endpoint_has_no_placement() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [])  # nothing placed
    assert not result.ok
    assert result.infeasibility is not None


def test_route_skips_power_commodity() -> None:
    # The generic router no longer routes power - that is router.power's job (router.power).
    a = _machine("a", [Port(id="pa", commodity=Commodity.POWER, direction=IODirection.OUTPUT)])
    b = _machine("b", [Port(id="pb", commodity=Commodity.POWER, direction=IODirection.INPUT)])
    net = Net(
        id="p",
        commodity=Commodity.POWER,
        throughput=32.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="pa"),
            MachineFaceRef(machine_id="b", port_id="pb"),
        ],
    )
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[a, b], nets=[net])
    result = route(problem, [_at("a", 1, 0, 1), _at("b", 3, 0, 1)])
    assert result.ok
    assert result.routes == ()  # the power net is left for the power router
