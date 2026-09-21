"""Tests for the Phase 1 crude router.

Headline: the real sand line now goes export -> place -> route -> validator.ok, the whole
thin slice end to end. The rest are synthetic cases for the routing/docking branches and the
never-silently-invalid promise (incompleteness is always an explicit infeasibility).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    PipeSize,
    Placement,
    Port,
    Route,
)
from gtnh_solver.placement import place
from gtnh_solver.router import route, route_power
from gtnh_solver.router.core import _pipe_size
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import at, machine, net

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


def _item_pair(region: CellBox, *, source_orientation: Facing = Facing.NORTH) -> InputIR:
    a = machine(
        "a",
        [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
        orientation=source_orientation,
    )
    b = machine("b", [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)])
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
    ir = adapt_file(_SAND).model_copy(update={"bounding_region": CellBox(sx=14, sy=4, sz=20)})
    # 6 machines (order: 3 hammers, input chest, output buffer #16, power source). Roomy enough for
    # the four item pipes, but the LV power trunk (source->h0->h1->h2) must stay compact: cable loss
    # is 1 V/block and LV is 32 V, so a trunk spanning more than ~31 blocks would leave a far hammer
    # under 0 V (unpowerable). The hammers sit in a short column near the source, so the whole cable
    # run is a couple dozen blocks - within reach - while the pipes keep their room. The source sits
    # on the z=0 row so its north-facing front (the reserved external-feed face) is on the boundary.
    coords = [(6, 0, 6), (6, 0, 10), (6, 0, 14), (2, 0, 14), (2, 0, 6), (10, 0, 0)]
    placements = [
        Placement(machine_id=m.id, cell=CellCoord(x=x, y=y, z=z), orientation=Facing.NORTH)
        for m, (x, y, z) in zip(ir.machines, coords, strict=True)
    ]
    rr = route(ir, placements)
    item_cells = _route_cells_of(rr.routes)
    pwr = route_power(ir, placements, extra_obstacles=item_cells)
    assert rr.ok
    assert pwr.ok
    assert rr.auto_connections == ()  # nothing is adjacent, so every item net is piped
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
        status=LayoutStatus.VALID,
        seed=0,
        placements=list(pr.placements),
        routes=list(rr.routes),
        auto_connections=list(rr.auto_connections),
    )
    assert _MALFORMED_ROUTE_CODES.isdisjoint(validate(ir, layout).codes())
    if not rr.ok:
        assert rr.infeasibility is not None


# --------------------------------------------------------------- synthetic


def test_route_two_machines_ok_and_validates() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    placements = [at("a", 1, 0, 1), at("b", 3, 0, 1)]
    result = route(problem, placements)
    assert result.ok
    assert len(result.routes) == 1
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    assert validate(problem, layout).ok


def test_route_auto_connects_an_adjacent_pair_instead_of_piping() -> None:
    # The router owns the auto-output vs pipe decision: a and b touch east/west with both fronts
    # north, so route() assigns GT's free auto-output itself and lays no pipe - the decision rides
    # RouteResult.auto_connections, and the assembled layout passes the independent gate.
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    placements = [at("a", 1, 0, 1), at("b", 2, 0, 1)]
    result = route(problem, placements)
    assert result.ok
    assert result.routes == ()
    assert [ac.net_id for ac in result.auto_connections] == ["n"]
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        auto_connections=list(result.auto_connections),
    )
    assert validate(problem, layout).ok


def test_route_two_crossing_nets_do_not_share_a_cell() -> None:
    # Two item nets whose shortest paths cross near the centre. Routing is capacity-aware - the
    # first net's cells become obstacles for the second - so they never share a cell (which would
    # be unbuildable single-channel). Routed independently they overlap at the crossing.
    def m(mid: str, port: str, direction: IODirection) -> Machine:
        return machine(mid, [Port(id=port, commodity=Commodity.ITEM, direction=direction)])

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
    placements = [at("a", 0, 0, 3), at("b", 6, 0, 3), at("c", 3, 0, 1), at("d", 3, 0, 5)]
    result = route(problem, placements)
    assert result.ok
    assert len(result.routes) == 2
    assert _route_cells(result.routes[0]).isdisjoint(_route_cells(result.routes[1]))
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    report = validate(problem, layout)
    assert ViolationCode.ROUTE_CELL_COLLISION not in report.codes()


def test_negotiation_routes_an_ordering_hostile_pocket() -> None:
    # A wall at z=3 with two gaps (x=1 and x=5); x=2 is walled for z<3, making a top-left pocket
    # (x=0..1, z=0..2) whose only exit down is gap x=1. net2 (c in the pocket -> d below) can ONLY
    # cross via gap x=1; net1 (a top-right -> b below) prefers gap x=1 but can detour to gap x=5.
    # Sequentially laid in problem order, net1 grabbed the pocket's only exit and wedged net2 out
    # (the failure that used to need failed-first reordering); negotiation instead prices the
    # contested gap cells up until net1's detour via x=5 is the cheaper argument - and the result
    # cannot depend on net order at all (asserted below by flipping it).
    def m(mid: str, direction: IODirection) -> Machine:
        return machine(mid, [Port(id="p", commodity=Commodity.ITEM, direction=direction)])

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
    placements = [at("a", 3, 0, 0), at("b", 0, 0, 5), at("c", 0, 0, 0), at("d", 0, 0, 4)]

    result = route(problem, placements)
    assert result.ok, result.infeasibility
    assert len(result.routes) == 2
    assert _route_cells(result.routes[0]).isdisjoint(_route_cells(result.routes[1]))
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    report = validate(problem, layout)
    assert report.ok, str(report)
    # Order-robust: the reversed net order routes just as cleanly (with sequential laying, one of
    # the two orders wedged; negotiation gives neither order a first-grab advantage).
    flipped = problem.model_copy(update={"nets": list(reversed(problem.nets))})
    result2 = route(flipped, placements)
    assert result2.ok, result2.infeasibility
    assert len(result2.routes) == 2


def test_negotiation_is_deterministic() -> None:
    # Same input twice -> identical routes (terminals, segments, order). Prices are pure
    # functions of the round state and the priced A* breaks ties on cost then cell, so the
    # negotiation has no hidden nondeterminism for the feedback loop to trip over.
    ir_path = _SAND
    from gtnh_solver.adapter import adapt_file

    problem = adapt_file(ir_path)
    placements = place(problem).placements
    assert route(problem, list(placements)) == route(problem, list(placements))


def test_negotiation_reports_genuine_congestion_explicitly() -> None:
    # Two nets MUST cross the same single-cell gap: region 5x1x3 with column x=2 walled except
    # (2, 0, 1). Both nets' every path runs (1,0,1)->(2,0,1)->(3,0,1), so no pricing can pull
    # them apart - negotiation exhausts its rounds, keeps a maximal collision-free subset (net1,
    # first in problem order), and fails net2 with an explicit congestion infeasibility (never a
    # silently-overlapping layout).
    def m(mid: str, direction: IODirection) -> Machine:
        return machine(mid, [Port(id="p", commodity=Commodity.ITEM, direction=direction)])

    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=1, sz=3),
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
        reserved_cells=[CellCoord(x=2, y=0, z=0), CellCoord(x=2, y=0, z=2)],
    )
    placements = [at("a", 0, 0, 0), at("b", 4, 0, 0), at("c", 0, 0, 2), at("d", 4, 0, 2)]
    result = route(problem, placements)
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "congestion"
    assert result.failed_nets == ("n2",)  # net1 salvaged (problem order), net2 reported
    assert len(result.routes) == 1  # the salvaged subset is still emitted, collision-free
    assert result.routes[0].net_id == "n1"


def _two_item_nets(
    region: CellBox, reserved: list[CellCoord], placements: list[Placement]
) -> tuple[InputIR, list[Placement]]:
    """A 2-net item problem (n1: a->b, n2: c->d) plus its placements, for the early-out tests."""

    def m(mid: str, direction: IODirection) -> Machine:
        return machine(mid, [Port(id="p", commodity=Commodity.ITEM, direction=direction)])

    def n(nid: str, src: str, dst: str, fluid: str) -> Net:
        return Net(
            id=nid,
            commodity=Commodity.ITEM,
            fluid_or_item=fluid,
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id=src, port_id="p"),
                MachineFaceRef(machine_id=dst, port_id="p"),
            ],
        )

    problem = InputIR(
        bounding_region=region,
        machines=[
            m("a", IODirection.OUTPUT),
            m("b", IODirection.INPUT),
            m("c", IODirection.OUTPUT),
            m("d", IODirection.INPUT),
        ],
        nets=[n("n1", "a", "b", "x"), n("n2", "c", "d", "y")],
        reserved_cells=reserved,
    )
    return problem, placements


def test_negotiation_bails_early_once_congestion_is_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The single-cell-gap congestion is proven irreducible, so negotiation salvages within a few
    # rounds rather than grinding the whole round budget. Spy on the proof: it fires and returns
    # True (which is the only thing that breaks the round loop early), and the reported verdict is
    # unchanged from exhausting the budget - net1 salvaged, net2 congested.
    from gtnh_solver.router import core

    verdicts: list[bool] = []
    real = core._congestion_is_irreducible

    def spy(*args: object, **kwargs: object) -> bool:
        verdict = real(*args, **kwargs)  # type: ignore[arg-type]
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(core, "_congestion_is_irreducible", spy)

    problem, placements = _two_item_nets(
        CellBox(sx=5, sy=1, sz=3),
        [CellCoord(x=2, y=0, z=0), CellCoord(x=2, y=0, z=2)],
        [at("a", 0, 0, 0), at("b", 4, 0, 0), at("c", 0, 0, 2), at("d", 4, 0, 2)],
    )
    result = route(problem, placements)

    assert True in verdicts  # the proof fired and certified the contention irreducible -> bailed
    assert result.failed_nets == ("n2",)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "congestion"


def test_negotiation_early_out_never_reports_a_resolvable_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Safety net for the early-out: force the proof to run on the very first overlap (stall gate
    # dropped to 0) of a genuinely routable case. It must DECLINE - return False - so the case
    # still resolves. A proof that fired True on an escapable cell would fabricate a congestion
    # infeasibility here, the exact regression an early-out risks.
    from gtnh_solver.router import core

    verdicts: list[bool] = []
    real = core._congestion_is_irreducible

    def spy(*args: object, **kwargs: object) -> bool:
        verdict = real(*args, **kwargs)  # type: ignore[arg-type]
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(core, "_congestion_is_irreducible", spy)
    monkeypatch.setattr(core, "_STALL_ROUNDS", 0)  # probe every overlap, not just a stalled one

    # A partial wall (z=3, x=1..7): both nets round the same open end, so their independent paths
    # overlap up front, then negotiation prices one aside - a routable contention, not congestion.
    problem, placements = _two_item_nets(
        CellBox(sx=11, sy=1, sz=7),
        [CellCoord(x=x, y=0, z=3) for x in range(1, 8)],
        [at("a", 1, 0, 0), at("b", 1, 0, 6), at("c", 2, 0, 0), at("d", 2, 0, 6)],
    )
    result = route(problem, placements)

    assert verdicts  # the proof actually ran (there was an overlap to test)
    assert not any(verdicts)  # ... and never falsely certified the escapable cell
    assert result.ok
    assert len(result.routes) == 2
    assert _route_cells(result.routes[0]).isdisjoint(_route_cells(result.routes[1]))


def test_congestion_proof_ignores_bystander_nets() -> None:
    # The proof weighs only the nets ON a contested cell. Here n1/n2 fight over the single-cell
    # gap in the x=2 wall while a third net (n3) routes its own lane on the far side, never
    # crossing. The proof must still certify the gap irreducible (n1 and n2 both forced) with n3 -
    # a non-user of the gap - skipped, not counted: n2 is reported congested, and n1 AND the
    # bystander n3 both route.
    def m(mid: str, direction: IODirection) -> Machine:
        return machine(mid, [Port(id="p", commodity=Commodity.ITEM, direction=direction)])

    def n(nid: str, src: str, dst: str, fluid: str) -> Net:
        return Net(
            id=nid,
            commodity=Commodity.ITEM,
            fluid_or_item=fluid,
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id=src, port_id="p"),
                MachineFaceRef(machine_id=dst, port_id="p"),
            ],
        )

    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=1, sz=7),
        machines=[
            m("a", IODirection.OUTPUT),
            m("b", IODirection.INPUT),
            m("c", IODirection.OUTPUT),
            m("d", IODirection.INPUT),
            m("e", IODirection.OUTPUT),
            m("f", IODirection.INPUT),
        ],
        # n3 is listed first on purpose: the proof then visits this non-user of the gap before
        # the two forced nets, so it must be skipped (not counted) for the gap to still read as
        # irreducible - the exact ordering that catches a miscounted bystander.
        nets=[n("n3", "e", "f", "z"), n("n1", "a", "b", "x"), n("n2", "c", "d", "y")],
        # The whole x=2 column is walled but the single gap at z=1, so n1 and n2 (which cross the
        # wall) are both forced through it; n3 stays on the far side and never crosses.
        reserved_cells=[CellCoord(x=2, y=0, z=z) for z in range(7) if z != 1],
    )
    placements = [
        at("a", 0, 0, 0),
        at("b", 4, 0, 0),
        at("c", 0, 0, 2),
        at("d", 4, 0, 2),
        at("e", 3, 0, 4),
        at("f", 4, 0, 6),  # n3's own lane, all x >= 3, so it never touches the gap
    ]
    result = route(problem, placements)

    assert result.failed_nets == ("n2",)  # only the loser of the gap is congested
    assert {r.net_id for r in result.routes} == {"n1", "n3"}  # the bystander routes untouched


def test_route_terminals_avoid_the_front_face() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [at("a", 1, 0, 1), at("b", 3, 0, 1)])
    faces = [t.face for r in result.routes for t in r.terminals]
    assert faces  # there are terminals
    assert all(face is not Facing.NORTH for face in faces)  # north is the front (orientation)


def test_route_docks_on_the_face_the_route_wants_not_the_first_in_face_order() -> None:
    # Two machines on the x axis with a 2-cell gap. FACE_ORDER puts SOUTH first, so first-fit
    # docking would put both terminals in the +z row and pay 3 hops to cross; the shortest
    # connection is the facing pair EAST/WEST at 1 hop. Docking is a routing decision, so the
    # router must choose the latter - it is the whole point of route-aware docking.
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [at("a", 0, 0, 0), at("b", 3, 0, 0)])

    assert result.ok
    (laid,) = result.routes
    assert [t.face for t in laid.terminals] == [Facing.EAST, Facing.WEST]
    assert len(laid.segments) == 1  # (1,0,0) -> (2,0,0); first-fit SOUTH would have paid 3


def test_route_chains_a_multi_endpoint_net_leg_by_leg() -> None:
    # Three endpoints: the first leg picks the opening PAIR of faces together (multi-source and
    # multi-goal), and each later leg starts from the cell already chosen. Nothing here may fall
    # back to the FACE_ORDER tiebreak - every endpoint's face comes from a laid leg.
    a = machine("a", [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)])
    b = machine("b", [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)])
    c = machine("c", [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)])
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[a, b, c],
        nets=[net("n", "a", "b", "c")],
    )
    result = route(problem, [at("a", 0, 0, 0), at("b", 3, 0, 0), at("c", 6, 0, 0)])

    assert result.ok
    (laid,) = result.routes
    assert len(laid.terminals) == 3
    # a and b face each other across the first gap, exactly as the two-endpoint case does; c is
    # reached by a second leg that starts where the first one ended.
    assert [t.face for t in laid.terminals[:2]] == [Facing.EAST, Facing.WEST]
    assert laid.terminals[2].cell.as_tuple() in _route_cells(laid)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("a", 0, 0, 0), at("b", 3, 0, 0), at("c", 6, 0, 0)],
        routes=list(result.routes),
        auto_connections=list(result.auto_connections),
    )
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_route_infeasible_when_both_endpoints_want_the_only_free_cell() -> None:
    # A 3x1x1 corridor: the lone free cell (1,0,0) is the ONLY dock candidate of both machines.
    # One pipe block wired to both would be a real build, but it lays no segment, and a route with
    # none is not a route (ROUTE_DISCONTINUOUS). The chain cannot even start (a leg's goals exclude
    # its own start), so docking falls back to first-fit, which never shares a cell (#164) - it
    # seats the first endpoint and leaves the second with nothing.
    problem = _item_pair(CellBox(sx=3, sy=1, sz=1))
    result = route(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)])

    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"
    assert "'b'" in result.infeasibility.detail  # the endpoint left without a cell, not the first


# ------------------------------------------ #164: several terminals of one net on one dock cell


def _stage(mid: str, direction: IODirection, front: Facing) -> Machine:
    """A single-block machine with one item port, ``out`` or ``in`` by ``direction``."""
    port = "out" if direction is IODirection.OUTPUT else "in"
    return machine(
        mid, [Port(id=port, commodity=Commodity.ITEM, direction=direction)], orientation=front
    )


def _manifold() -> tuple[InputIR, list[Placement]]:
    """Two producers feeding two consumers of ONE net, the cobblestone net of parallel-sand in small.

    A 2x2x2 region, four machines and four free cells::

        y=0   a0 P        y=1   U0 b0        P, Q sit beside the producers and under the consumers
              a1 Q              U1 b1        U0, U1 sit over the producers and beside the consumers

    P-Q and U0-U1 are two separate pairs, so no four free cells are connected: with one terminal
    per cell this net cannot route at all. Two terminals per cell, one from each stage, is exactly
    how the maintainer's build wires it.
    """
    machines = [
        _stage("a0", IODirection.OUTPUT, Facing.WEST),
        _stage("a1", IODirection.OUTPUT, Facing.WEST),
        _stage("b0", IODirection.INPUT, Facing.EAST),
        _stage("b1", IODirection.INPUT, Facing.EAST),
    ]
    manifold = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="a0", port_id="out"),
            MachineFaceRef(machine_id="a1", port_id="out"),
            MachineFaceRef(machine_id="b0", port_id="in"),
            MachineFaceRef(machine_id="b1", port_id="in"),
        ],
    )
    placements = [
        at("a0", 0, 0, 0, orientation=Facing.WEST),
        at("a1", 0, 0, 1, orientation=Facing.WEST),
        at("b0", 1, 1, 0, orientation=Facing.EAST),
        at("b1", 1, 1, 1, orientation=Facing.EAST),
    ]
    problem = InputIR(bounding_region=CellBox(sx=2, sy=2, sz=2), machines=machines, nets=[manifold])
    return problem, placements


def test_terminals_of_one_net_share_a_dock_cell() -> None:
    problem, placements = _manifold()
    result = route(problem, placements)

    assert result.ok, result.infeasibility
    (laid,) = result.routes
    by_cell: dict[tuple[int, int, int], list[str]] = {}
    for terminal in laid.terminals:
        by_cell.setdefault(terminal.cell.as_tuple(), []).append(terminal.machine_id)
    # Four terminals on two pipe blocks, each block wired to one producer and one consumer.
    assert len(laid.terminals) == 4
    assert len(by_cell) == 2
    assert all(len(set(owners)) == 2 for owners in by_cell.values()), by_cell
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_two_nets_still_never_share_a_dock_cell() -> None:
    # The relaxation is within ONE net. X=(1,0,0) is the only dock cell of both a (net n1) and b
    # (net n2), and a pipe delivers to any inventory wired to it whatever the plan meant it to
    # carry, so letting n2 dock there would feed n1's items into b. n2 must fail, naming b.
    #
    #   z=0   a  X  b      a, b front SOUTH: X is all either has left
    #   z=1   .  .  .
    #   z=2   c  .  e      c consumes n1, e produces n2
    a = _stage("a", IODirection.OUTPUT, Facing.SOUTH)
    b = _stage("b", IODirection.INPUT, Facing.SOUTH)
    c = _stage("c", IODirection.INPUT, Facing.WEST)
    e = _stage("e", IODirection.OUTPUT, Facing.EAST)
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[a, b, c, e],
        nets=[net("n1", "a", "c"), net("n2", "e", "b")],
    )
    placements = [
        at("a", 0, 0, 0, orientation=Facing.SOUTH),
        at("b", 2, 0, 0, orientation=Facing.SOUTH),
        at("c", 0, 0, 2, orientation=Facing.WEST),
        at("e", 2, 0, 2, orientation=Facing.EAST),
    ]
    result = route(problem, placements)

    assert not result.ok
    assert result.failed_nets == ("n2",)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"
    assert "'b'" in result.infeasibility.detail
    (n1,) = result.routes
    assert (1, 0, 0) in n1.cells()  # n1 kept X; nothing of n2 was laid on it


def test_a_shared_dock_cell_is_never_handed_to_another_net() -> None:
    # The re-seat rescue frees a cell a stranded net needs by moving whoever holds it. A cell two
    # endpoints of one net share cannot be freed that way: moving one leaves the other on it, so
    # handing it over would put two nets on one pipe block.
    #
    # The manifold above, one layer taller. Net n docks on U0 and U1 (asserted below, since the
    # test means nothing otherwise), and c's only dock cell is U0: its front is SOUTH, d sits
    # EAST, and every other face is off the region. U0's holders a0 and b0 could each move to P,
    # so a rescue that re-seated one of them would "succeed" and give U0 to net m.
    problem, placements = _manifold()
    c = _stage("c", IODirection.OUTPUT, Facing.SOUTH)
    d = _stage("d", IODirection.INPUT, Facing.WEST)  # fronting c, so no free auto-output
    problem = problem.model_copy(
        update={
            "bounding_region": CellBox(sx=2, sy=3, sz=2),
            "machines": [*problem.machines, c, d],
            "nets": [*problem.nets, net("m", "c", "d")],
        }
    )
    placements = [
        *placements,
        at("c", 0, 2, 0, orientation=Facing.SOUTH),
        at("d", 1, 2, 0, orientation=Facing.WEST),
    ]
    result = route(problem, placements)

    manifold = next((r for r in result.routes if r.net_id == "n"), None)
    assert manifold is not None, f"the manifold lost its cell to net m: {result.failed_nets}"
    assert {t.cell.as_tuple() for t in manifold.terminals} == {(0, 1, 0), (0, 1, 1)}
    assert result.failed_nets == ("m",)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"
    assert "'c'" in result.infeasibility.detail


def test_route_skips_me_toggled_commodity() -> None:
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8)).model_copy(
        update={"me_toggles": METoggles(items=True)}
    )
    result = route(problem, [at("a", 1, 0, 1), at("b", 3, 0, 1)])
    assert result.ok
    assert result.routes == ()  # the item net is ME-toggled, not physically routed


def test_route_infeasible_when_a_machine_cannot_dock() -> None:
    # 2x1x1: the two machines fill the region. The source fronts EAST - straight into the sink -
    # so the one touching face carries no I/O (auto-output cannot cover the net), and every other
    # face cell lies outside the region, leaving no free non-front face to dock a pipe terminal.
    problem = _item_pair(CellBox(sx=2, sy=1, sz=1), source_orientation=Facing.EAST)
    placements = [
        Placement(machine_id="a", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.EAST),
        at("b", 1, 0, 0),
    ]
    result = route(problem, placements)
    assert not result.ok
    assert result.auto_connections == ()  # the source's front blocks the free connection
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
    result = route(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)])
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
    a = machine("a", [Port(id="pa", commodity=Commodity.POWER, direction=IODirection.OUTPUT)])
    b = machine("b", [Port(id="pb", commodity=Commodity.POWER, direction=IODirection.INPUT)])
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
    result = route(problem, [at("a", 1, 0, 1), at("b", 3, 0, 1)])
    assert result.ok
    assert result.routes == ()  # the power net is left for the power router


# ------------------------------------------------------------------------ pipe size (#165)


def _fan(
    sources: int,
    sinks: int,
    *,
    source_rate: float | None,
    sink_rate: float | None,
    commodity: Commodity = Commodity.ITEM,
    throughput: float = 0.3,
) -> tuple[Net, dict[str, Machine]]:
    """A net from ``sources`` machines into ``sinks`` machines, each port at the given rate."""
    machines: dict[str, Machine] = {}
    endpoints: list[MachineFaceRef] = []
    for kind, count, direction, rate in (
        ("src", sources, IODirection.OUTPUT, source_rate),
        ("dst", sinks, IODirection.INPUT, sink_rate),
    ):
        for i in range(count):
            mid = f"{kind}{i}"
            port = Port(id="p", commodity=commodity, direction=direction, rate=rate)
            machines[mid] = machine(mid, [port])
            endpoints.append(MachineFaceRef(machine_id=mid, port_id="p"))
    fan = Net(
        id="n", commodity=commodity, fluid_or_item="x", throughput=throughput, endpoints=endpoints
    )
    return fan, machines


@pytest.mark.parametrize(
    ("sources", "sinks", "size"),
    [
        (1, 1, PipeSize.NORMAL),  # one endpoint each side: the plain pipe, as every line had
        (1, 2, PipeSize.LARGE),
        (1, 3, PipeSize.HUGE),  # the stone run: one chest into three hammers
        (3, 1, PipeSize.HUGE),  # the sand run: three hammers into one chest
        (3, 3, PipeSize.HUGE),  # the stages between: every stream can meet at one point
    ],
)
def test_an_item_pipe_is_sized_by_its_crowded_side(
    sources: int, sinks: int, size: PipeSize
) -> None:
    """The parallel sand line's four runs, each at 0.3 items/t split evenly. GT spends one
    insertion per inventory reached and a normal tin pipe makes one per 40 ticks, which in game fed
    one hammer of three; so every endpoint on the crowded side costs an insertion per interval."""
    fan, machines = _fan(sources, sinks, source_rate=0.3 / sources, sink_rate=0.3 / sinks)
    assert _pipe_size(fan, machines) is size


def test_an_endpoint_moving_more_than_a_stack_per_interval_needs_a_second_insertion() -> None:
    """One insertion carries one stack at most, so a fast 1-to-1 run outgrows the plain pipe."""
    fan, machines = _fan(1, 1, source_rate=2.0, sink_rate=2.0, throughput=2.0)
    assert _pipe_size(fan, machines) is PipeSize.LARGE


def test_an_unrated_port_takes_an_even_share_of_the_net() -> None:
    fan, machines = _fan(1, 2, source_rate=None, sink_rate=None, throughput=4.0)
    # Each sink's 2 items/t is past a stack per interval: 2 insertions apiece, 4 in all.
    assert _pipe_size(fan, machines) is PipeSize.HUGE


def test_a_demand_past_the_largest_pipe_is_laid_at_the_largest() -> None:
    """Nothing thicker exists in the stand-in material. The validator, not the router, is the gate
    that refuses a run too thin for its net (#190)."""
    fan, machines = _fan(1, 6, source_rate=0.6, sink_rate=0.1, throughput=0.6)
    assert _pipe_size(fan, machines) is PipeSize.HUGE


def test_an_endpoint_the_router_cannot_resolve_is_not_counted() -> None:
    fan, machines = _fan(1, 3, source_rate=0.3, sink_rate=0.1)
    del machines["dst2"]  # its machine is gone
    machines["dst1"] = machine("dst1", [])  # and this one no longer has the port
    assert _pipe_size(fan, machines) is PipeSize.NORMAL


def test_a_fluid_pipe_is_not_sized_yet() -> None:
    fan, machines = _fan(
        1, 3, source_rate=300.0, sink_rate=100.0, commodity=Commodity.FLUID, throughput=300.0
    )
    assert _pipe_size(fan, machines) is PipeSize.NORMAL


def test_a_routed_item_pipe_publishes_its_size() -> None:
    """End to end through ``route``: the size reaches the contract, not just the helper."""
    problem = _item_pair(CellBox(sx=8, sy=4, sz=8))
    result = route(problem, [at("a", 1, 0, 1), at("b", 5, 0, 1)])
    assert result.ok
    (piped,) = result.routes
    assert piped.material is not None
    assert piped.material.size is PipeSize.NORMAL
