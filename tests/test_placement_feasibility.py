"""Tests for the pre-routing crowding gate (``placement.feasibility``).

Headline: the gate names a machine only when it can prove the machine cannot dock every connection
it carries, and it has to agree with what the routers will actually do - which means asking the
questions they ask first. A net an auto-output covers, or the ME network carries, needs no dock
cell; connections of one net on different machines may share one (#164); two of one machine, or
two on different nets, may not. Get the permissive half wrong and the gate condemns layouts that
build, which is worse than not checking at all: it is what drove the sand line off its hand-built
box while it was being developed, and what turned the maintainer's proven parallel-sand build away.
Get the strict half wrong and it stops catching the solid row of #76.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    MachineFaceRef,
    METoggles,
    Net,
    Placement,
    Port,
)
from gtnh_solver.placement import crowded_machines
from gtnh_solver.router import route
from tests._helpers import at, consumer, machine, net, power_source, producer


def _power_net(nid: str, source: str, *sinks: str) -> Net:
    """A power net from ``source``'s ``power:out`` to each sink's first energy hatch."""
    return Net(
        id=nid,
        commodity=Commodity.POWER,
        fluid_or_item=None,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=source, port_id="power:out"),
            *(MachineFaceRef(machine_id=s, port_id="power:in0") for s in sinks),
        ],
    )


def _powered(mid: str, *, hatches: int = 1) -> object:
    """A machine with one item input, one item output, and ``hatches`` power inputs."""
    return machine(
        mid,
        [
            Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            *(
                Port(id=f"power:in{i}", commodity=Commodity.POWER, direction=IODirection.INPUT)
                for i in range(hatches)
            ),
        ],
    )


def test_a_roomy_placement_hosts_everything() -> None:
    # Two machines with space around them: an assignment obviously exists, and an empty result is
    # the gate's positive statement that it found one - not merely that it declined to look.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m0"), consumer("m1")],
        nets=[net("n", "m0", "m1")],
    )
    assert crowded_machines(problem, [at("m0", 0, 0, 0), at("m1", 5, 0, 5)]) == ()


def test_a_walled_in_machine_has_nowhere_to_take_power() -> None:
    # m1 is boxed in on all four horizontals, and a one-layer region removes up and down, so it has
    # no free cell at all. Its power connection has to go somewhere, so it is named. (The source in
    # the corner is walled in too; which of the two the test is about is m1.)
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[
            _powered("m1"),
            producer("n0"),
            producer("n1"),
            consumer("n2"),
            consumer("n3"),
            power_source("src"),
        ],
        nets=[_power_net("p", "src", "m1")],
    )
    placements = [
        at("m1", 1, 0, 1),
        at("n0", 0, 0, 1),
        at("n1", 2, 0, 1),
        at("n2", 1, 0, 0),
        at("n3", 1, 0, 2),
        at("src", 0, 0, 0),
    ]
    assert "m1" in crowded_machines(problem, placements)


def test_power_terminals_may_share_a_cell_across_machines() -> None:
    # GT feeds every wired face next to a cable block, so route_power taps rather than laying a new
    # leg and two machines' hatches can sit on one cell. Two machines whose ONLY free cell is the
    # same one are therefore fine, and demanding a distinct cell each would be a false crowding.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[
            machine(
                "a",
                [Port(id="power:in0", commodity=Commodity.POWER, direction=IODirection.INPUT)],
            ),
            machine(
                "b",
                [Port(id="power:in0", commodity=Commodity.POWER, direction=IODirection.INPUT)],
            ),
            producer("wall0"),  # on no net, so only a wall
            producer("wall1"),
            power_source("src"),
        ],
        nets=[_power_net("p", "src", "a", "b")],
    )
    # a at x=0 and b at x=2, walled from below: the only free cell either has is (1, 0, 0).
    placements = [
        at("a", 0, 0, 0),
        at("b", 2, 0, 0),
        at("wall0", 0, 0, 1),
        at("wall1", 2, 0, 1),
        at("src", 1, 0, 2, orientation=Facing.SOUTH),
    ]
    assert crowded_machines(problem, placements) == ()


def test_two_hatches_on_one_machine_still_need_two_cells() -> None:
    # The sharing above is BETWEEN machines. Two energy hatches on one machine are two casing
    # cells, so one free cell cannot serve both.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=1),
        machines=[
            machine(
                "a",
                [
                    Port(
                        id=f"power:in{i}",
                        commodity=Commodity.POWER,
                        direction=IODirection.INPUT,
                    )
                    for i in range(2)
                ],
            ),
            power_source("src"),
        ],
        nets=[
            Net(
                id="p",
                commodity=Commodity.POWER,
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="power:out"),
                    *(MachineFaceRef(machine_id="a", port_id=f"power:in{i}") for i in range(2)),
                ],
            )
        ],
    )
    # One free cell, (1, 0, 0), which the source may share with a hatch but not a hatch with a hatch.
    assert crowded_machines(problem, [at("a", 0, 0, 0), at("src", 2, 0, 0)]) == ("a",)


def test_an_auto_output_pair_needs_no_dock_cells() -> None:
    # The regression that matters: a free auto-output connection ejects straight from one machine
    # into the next, so neither port needs a cell. Counting them as needing one condemns exactly
    # the tight, zero-pipe layouts the placer is supposed to find - the sand line's whole shape.
    problem = InputIR(
        bounding_region=CellBox(sx=2, sy=1, sz=1),
        machines=[producer("m0"), consumer("m1")],
        nets=[net("n", "m0", "m1")],
    )
    placements = [at("m0", 0, 0, 0), at("m1", 1, 0, 0)]
    # Face to face with no spare cell anywhere in the region, and still buildable.
    assert crowded_machines(problem, placements) == ()


def test_an_me_net_needs_no_dock_cell_either() -> None:
    # Same reasoning as auto-output: an ME-toggled commodity is not piped, so its ports cost
    # nothing. Without this the toggle would make layouts look MORE crowded, not less.
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=1, sz=1),
        machines=[producer("m0"), consumer("m1"), consumer("m2")],
        nets=[net("n0", "m0", "m1"), net("n1", "m0", "m2")],
        me_toggles=METoggles(items=True),
    )
    placements = [at("m0", 0, 0, 0), at("m1", 1, 0, 0), at("m2", 2, 0, 0)]
    assert crowded_machines(problem, placements) == ()


def test_a_placement_naming_an_unknown_machine_is_skipped() -> None:
    # Placements and machines are separate lists; a placement with no machine behind it is the
    # caller's bug to report, not something to crash on mid-check.
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[producer("m0")],
        nets=[],
    )
    ghost = Placement.model_validate(at("m0", 1, 0, 1).model_dump() | {"machine_id": "ghost"})
    assert crowded_machines(problem, [at("m0", 0, 0, 0), ghost]) == ()


def test_a_net_reaching_an_unplaced_machine_is_judged_on_what_is_placed() -> None:
    # The other half of that bookkeeping: a machine the placement leaves out has no cells to count
    # and nothing to name. Missing placements are the placer's to report (and the validator's).
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[producer("m0"), consumer("m1")],
        nets=[net("n", "m0", "m1")],
    )
    assert crowded_machines(problem, [at("m0", 0, 0, 0)]) == ()


def test_a_machine_walled_into_a_solid_row_stays_crowded_though_its_nets_can_share() -> None:
    # #76's pathology, which letting one net share a dock cell (#164) must not excuse. Three
    # machines of one stage sit in a solid row along the region's north and bottom edges, so the
    # middle one has neighbours east and west, walls north and down, and TWO free cells for its
    # THREE connections. Each of those two cells is also reachable by another machine on the same
    # net as one of them - the feed chest from above one, the power source from above the other -
    # so a gate that let a shared cell count once per machine that could use it would pass this.
    # But sharing is between machines. One machine's own three connections still need three cells.
    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=3, sz=3),
        machines=[
            _powered("west"),
            _powered("mid"),
            _powered("east"),
            producer("feed"),
            consumer("sink"),
            power_source("src"),
        ],
        nets=[
            net("stone", "feed", "west", "mid", "east"),
            Net(
                id="gravel",
                commodity=Commodity.ITEM,
                fluid_or_item="gravel",
                throughput=1.0,
                endpoints=[
                    *(MachineFaceRef(machine_id=m, port_id="out") for m in ("west", "mid", "east")),
                    MachineFaceRef(machine_id="sink", port_id="in"),
                ],
            ),
            _power_net("p", "src", "west", "mid", "east"),
        ],
    )
    placements = [
        at("west", 1, 0, 0),
        at("mid", 2, 0, 0),  # free cells: (2, 0, 1) south of it and (2, 1, 0) above it
        at("east", 3, 0, 0),
        at("feed", 2, 1, 1),  # docks down onto (2, 0, 1), on mid's stone net
        at("src", 2, 2, 0),  # docks down onto (2, 1, 0), on mid's power net
        at("sink", 0, 0, 2),
    ]
    assert crowded_machines(problem, placements) == ("mid",)


def _two_machines_one_cell(*, same_net: bool) -> tuple[InputIR, list[Placement]]:
    """A producer ``a`` and a consumer ``b`` whose ONLY free cell is the same one, ``(1, 0, 0)``.

    The region's north wall, their fronts and the walls below them take every other side. With
    ``same_net`` both are on one net, ``a`` feeding ``c`` and ``b``; otherwise ``a`` feeds ``c`` and
    ``b`` is fed by ``d`` on a net of its own. ``c`` and ``d`` have a cell each to spare.
    """
    machines = [
        producer("a"),
        consumer("b"),
        consumer("c"),
        producer("d"),
        producer("wall0"),  # on no net, so only a wall
        producer("wall1"),
    ]
    nets = (
        [net("n", "a", "c", "b")]
        if same_net
        else [net("n1", "a", "c"), net("n2", "d", "b", fluid="y")]
    )
    placements = [
        at("a", 0, 0, 0, orientation=Facing.WEST),
        at("b", 2, 0, 0, orientation=Facing.EAST),
        at("c", 1, 0, 3, orientation=Facing.SOUTH),
        at("d", 2, 0, 3, orientation=Facing.SOUTH),
        at("wall0", 0, 0, 1),
        at("wall1", 2, 0, 1),
    ]
    region = CellBox(sx=3, sy=1, sz=4)
    return InputIR(bounding_region=region, machines=machines, nets=nets), placements


def test_machines_on_one_net_may_dock_on_the_same_cell() -> None:
    # One pipe block wired to several neighbours is how GT builds a manifold, and since #164 the
    # router docks two machines of one net on one cell. So two machines of one net whose only free
    # cell is the same one are fine, and demanding a cell each is the false crowding that turned
    # away the maintainer's own build. The router agrees: it lays this net with a and b both on
    # (1, 0, 0), which is what makes the gate's silence here a fact rather than a blind spot.
    problem, placements = _two_machines_one_cell(same_net=True)
    assert crowded_machines(problem, placements) == ()
    routed = route(problem, placements)
    assert routed.ok, routed.infeasibility
    (pipe,) = routed.routes
    assert {t.cell.as_tuple() for t in pipe.terminals if t.machine_id in {"a", "b"}} == {(1, 0, 0)}


def test_machines_on_different_nets_may_not_dock_on_the_same_cell() -> None:
    # The other half, and what keeps the relaxation honest: a cell carries one net
    # (``ROUTE_CELL_COLLISION``), so two machines on DIFFERENT nets cannot share their only cell.
    # Both are named, since each is what leaves the other nowhere to go.
    problem, placements = _two_machines_one_cell(same_net=False)
    assert crowded_machines(problem, placements) == ("a", "b")


def test_power_is_not_starved_by_whichever_cell_a_pipe_happened_to_take() -> None:
    # ``b``'s energy hatch has one cell it can use, (0, 0, 1). ``a``'s pipe could take that cell or
    # (1, 0, 0). Settling the pipes first and power on the leftovers made the verdict depend on which
    # of those the pipe matching happened to pick, and it picked (0, 0, 1) - naming ``b`` crowded on
    # a placement where giving the pipe the other cell hosts everything. That is how the proven
    # build's power source came to be named (#164). Deciding both together has no such order.
    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=1, sz=3),
        machines=[producer("a"), _powered("b"), consumer("d"), power_source("src")],
        nets=[net("n", "a", "d"), _power_net("p", "src", "b")],
    )
    placements = [
        at("a", 0, 0, 0, orientation=Facing.WEST),  # reaches (1, 0, 0) and (0, 0, 1)
        at("b", 0, 0, 2, orientation=Facing.EAST),  # reaches only (0, 0, 1)
        at("d", 4, 0, 1, orientation=Facing.EAST),
        at("src", 2, 0, 2, orientation=Facing.SOUTH),
    ]
    assert crowded_machines(problem, placements) == ()


def test_power_on_the_me_network_needs_no_dock_cell() -> None:
    # No cable, so no energy hatch to find a face for - the same reasoning as an ME item net. The
    # routers dock nothing for it (``route`` leaves an ME net out, ``route_power`` returns early),
    # so charging it a cell would make ME power look MORE crowded than cable, not less.
    problem = InputIR(
        bounding_region=CellBox(sx=2, sy=1, sz=1),
        machines=[power_source("src"), _powered("a")],
        nets=[_power_net("p", "src", "a")],
        me_toggles=METoggles(power=True),
    )
    # Face to face with no free cell anywhere in the region.
    assert crowded_machines(problem, [at("src", 0, 0, 0), at("a", 1, 0, 0)]) == ()
