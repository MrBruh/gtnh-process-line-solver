"""Tests for the pre-routing crowding gate (``placement.feasibility``).

Headline: the gate answers "can every connection be given a cell of its own", exactly, and it has
to agree with what the routers will actually do - which means asking the same two questions they
ask first. A net an auto-output covers needs no dock cell, and a power terminal may share one. Get
either wrong and the gate condemns layouts that build, which is worse than not checking at all: it
is what drove the sand line off its hand-built box while it was being developed (#76).
"""

from __future__ import annotations

from gtnh_solver.ir import (
    CellBox,
    Commodity,
    InputIR,
    IODirection,
    MachineFaceRef,
    METoggles,
    Net,
    Placement,
    Port,
)
from gtnh_solver.placement import crowded_machines
from gtnh_solver.router.power import reserve_power_docks
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
    # no free cell at all. Its power connection has to go somewhere, so it is named.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[
            _powered("m1"),
            producer("n0"),
            producer("n1"),
            consumer("n2"),
            consumer("n3"),
        ],
        nets=[],
    )
    placements = [
        at("m1", 1, 0, 1),
        at("n0", 0, 0, 1),
        at("n1", 2, 0, 1),
        at("n2", 1, 0, 0),
        at("n3", 1, 0, 2),
    ]
    assert "m1" in crowded_machines(problem, placements)


def test_power_terminals_may_share_a_cell_across_machines() -> None:
    # GT feeds every wired face next to a cable block, so route_power taps rather than laying a new
    # leg and two machines' hatches can sit on one cell. Two machines whose ONLY free cell is the
    # same one are therefore fine, and demanding a distinct cell each would be a false crowding.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=1),
        machines=[
            machine(
                "a",
                [Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT)],
            ),
            machine(
                "b",
                [Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT)],
            ),
        ],
        nets=[],
    )
    # a at x=0 and b at x=2 in a 3x1x1 slot: the only free cell is (1, 0, 0), reachable by both.
    assert crowded_machines(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)]) == ()


def test_two_hatches_on_one_machine_still_need_two_cells() -> None:
    # The sharing above is BETWEEN machines. Two energy hatches on one machine are two casing
    # cells, so one free cell cannot serve both.
    problem = InputIR(
        bounding_region=CellBox(sx=2, sy=1, sz=1),
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
            )
        ],
        nets=[],
    )
    assert crowded_machines(problem, [at("a", 0, 0, 0)]) == ("a",)


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


def test_reserve_power_docks_holds_one_distinct_cell_per_endpoint() -> None:
    # One cell per power endpoint, never two endpoints on the same cell: the reservation is what
    # stops the item router from taking the last face a machine had left for its energy hatch.
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=2, sz=2),
        machines=[power_source("src"), _powered("a"), _powered("b")],
        nets=[_power_net("p", "src", "a", "b")],
    )
    placements = [at("src", 0, 0, 0), at("a", 2, 0, 0), at("b", 4, 0, 0)]
    reserved = reserve_power_docks(problem, placements)
    # src's power:out plus one power:in each: three endpoints, three distinct cells.
    assert len(reserved) == 3


def test_reserve_power_docks_holds_nothing_when_power_rides_me() -> None:
    # No cable means no dock to protect, and holding cells back would only crowd the pipes.
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=2, sz=2),
        machines=[power_source("src"), _powered("a")],
        nets=[_power_net("p", "src", "a")],
        me_toggles=METoggles(power=True),
    )
    assert reserve_power_docks(problem, [at("src", 0, 0, 0), at("a", 2, 0, 0)]) == set()
