"""Tests for router.auto - the router-owned auto-output vs pipe decision.

Moved with the logic from the solver: given final placements + orientations,
``assign_auto_outputs`` greedily connects each adjacent 1-source-1-sink item/fluid net by GT's
free auto-output (one auto-output per source machine; power/ME never auto-feed). These unit
tests pin the greedy rules the validator independently re-checks.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    METoggles,
    Port,
)
from gtnh_solver.router import assign_auto_outputs
from tests._helpers import at, consumer, machine, net, producer

_REGION = CellBox(sx=8, sy=4, sz=8)


def test_adjacent_pair_auto_connects_on_the_touching_faces() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    autos, covered = assign_auto_outputs(problem, [at("a", 1, 0, 1), at("b", 2, 0, 1)])
    assert covered == {"n"}
    (ac,) = autos
    assert (ac.source_machine_id, ac.target_machine_id) == ("a", "b")
    assert (ac.source_face, ac.target_face) == (Facing.EAST, Facing.WEST)


def test_non_adjacent_machines_do_not_auto_connect() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    autos, covered = assign_auto_outputs(problem, [at("a", 1, 0, 1), at("b", 4, 0, 1)])
    assert autos == []
    assert covered == set()


def test_source_front_face_blocks_the_auto_output() -> None:
    # a fronts EAST, straight into b: the only touching face carries no I/O, so the net pipes.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[
            machine(
                "a",
                [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
                orientation=Facing.EAST,
            ),
            consumer("b"),
        ],
        nets=[net("n", "a", "b")],
    )
    placements = [at("a", 1, 0, 1, orientation=Facing.EAST), at("b", 2, 0, 1)]
    autos, covered = assign_auto_outputs(problem, placements)
    assert autos == []
    assert covered == set()


def test_an_unplaced_endpoint_machine_never_auto_connects() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    autos, covered = assign_auto_outputs(problem, [at("a", 1, 0, 1)])  # b is not placed
    assert autos == []
    assert covered == set()


def test_source_spends_its_single_auto_output_on_the_first_net() -> None:
    # a feeds b and c, both adjacent; only the first net (problem order) gets the free
    # connection - a machine has one auto-output face - and the second is left to pipe.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("b"), consumer("c")],
        nets=[net("n1", "a", "b"), net("n2", "a", "c")],
    )
    placements = [at("a", 1, 0, 1), at("b", 2, 0, 1), at("c", 0, 0, 1)]
    autos, covered = assign_auto_outputs(problem, placements)
    assert [ac.net_id for ac in autos] == ["n1"]
    assert covered == {"n1"}


def test_fan_out_net_is_not_eligible() -> None:
    # one source, two sinks on a single net: only simple 1->1 nets auto-output; fan-out pipes.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("b"), consumer("c")],
        nets=[net("n", "a", "b", "c")],
    )
    placements = [at("a", 1, 0, 1), at("b", 2, 0, 1), at("c", 0, 0, 1)]
    autos, covered = assign_auto_outputs(problem, placements)
    assert autos == []
    assert covered == set()


def test_power_and_me_toggled_nets_never_auto_feed() -> None:
    # power is a shared-amperage net and ME-toggled commodities are not physically connected,
    # so neither is eligible even with the machines touching.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[
            producer("pa", commodity=Commodity.POWER),
            consumer("pb", commodity=Commodity.POWER),
            producer("ia"),
            consumer("ib"),
        ],
        nets=[
            net("np", "pa", "pb", commodity=Commodity.POWER),
            net("ni", "ia", "ib"),
        ],
        me_toggles=METoggles(items=True),
    )
    placements = [
        at("pa", 1, 0, 1),
        at("pb", 2, 0, 1),
        at("ia", 1, 0, 4),
        at("ib", 2, 0, 4),
    ]
    autos, covered = assign_auto_outputs(problem, placements)
    assert autos == []
    assert covered == set()
