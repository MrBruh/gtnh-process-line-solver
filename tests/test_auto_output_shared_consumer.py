"""A net with several producers and one consumer, covered producer by producer (#270).

Each producer of such a net ejects on its own, so one standing against the consumer auto-outputs
into it and the rest share the net's pipe; once every producer is covered the net needs no pipe.
These pin the rule end to end: which producers qualify (``router.auto.auto_candidates``), what the
router lays, the hatch a multiblock producer still gets, what the placement reward and the docking
gate count, and the validator's per-producer reading of a net that is both routed and
auto-connected.
"""

from __future__ import annotations

from gtnh_solver.ir import (
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
    Net,
    Placement,
    Port,
    Terminal,
)
from gtnh_solver.placement.feasibility import crowded_machines
from gtnh_solver.placement.search import _auto_candidate_pairs
from gtnh_solver.router import assign_auto_outputs, place_hatches, route
from gtnh_solver.router.auto import auto_candidates
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import at, consumer, machine, producer

_REGION = CellBox(sx=12, sy=6, sz=12)


def _ref(mid: str, port: str) -> MachineFaceRef:
    return MachineFaceRef(machine_id=mid, port_id=port)


def _shared(*producers: str, consumers: tuple[str, ...] = ("s",), nid: str = "n") -> Net:
    """An item net from each producer's ``out`` into each consumer's ``in``."""
    return Net(
        id=nid,
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            *(_ref(p, "out") for p in producers),
            *(_ref(c, "in") for c in consumers),
        ],
    )


def _three_into_one() -> tuple[InputIR, list[Placement]]:
    """Hammers ``a`` and ``b`` stand against chest ``s`` (west and east of it); ``c`` is far off."""
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), producer("b"), producer("c"), consumer("s")],
        nets=[_shared("a", "b", "c")],
    )
    placements = [at("a", 3, 0, 4), at("b", 5, 0, 4), at("c", 9, 0, 9), at("s", 4, 0, 4)]
    return problem, placements


def _layout(problem: InputIR, placements: list[Placement]) -> LayoutResult:
    """Route ``placements`` and assemble the layout the validator judges."""
    routing = route(problem, placements)
    assert routing.ok, routing.infeasibility
    plan = place_hatches(problem, placements, routing.routes, routing.auto_connections)
    return LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        routes=list(routing.routes),
        auto_connections=list(routing.auto_connections),
        hatches=list(plan.hatches),
    )


# ------------------------------------------------------------------- which producers qualify


def test_every_producer_of_a_net_into_one_single_block_consumer_is_a_candidate() -> None:
    problem, _ = _three_into_one()
    candidates = auto_candidates(problem)
    assert [c.source.machine_id for c in candidates] == ["a", "b", "c"]
    assert all(c.shared and c.sink == _ref("s", "in") for c in candidates)


def test_a_one_to_one_net_is_a_candidate_that_needs_its_consumer_alone() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("s")],
        nets=[_shared("a")],
    )
    (candidate,) = auto_candidates(problem)
    assert not candidate.shared


def test_a_net_with_several_consumers_is_never_split() -> None:
    # One producer's auto-output reaches one block, so it could feed only one of the two.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), producer("b"), consumer("s"), consumer("t")],
        nets=[_shared("a", "b", consumers=("s", "t"))],
    )
    assert auto_candidates(problem) == []


def test_a_multiblock_consumer_keeps_a_many_into_one_net_piped() -> None:
    # Taking one producer free and the rest by pipe would need two input hatches for one port.
    chest = Machine(
        id="s",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=3, sy=3, sz=3),
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
        ),
        hatch_slots=(HatchSlot(offset=CellCoord(x=0, y=1, z=1), kinds=("InputBus",)),),
        hatch_cells=1,
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), producer("b"), chest],
        nets=[_shared("a", "b")],
    )
    assert auto_candidates(problem) == []


def test_a_single_block_producer_with_another_output_of_the_kind_keeps_to_the_pipe() -> None:
    # Its auto-output face would eject the other output too, into a chest that locks to the first
    # item it is given.
    two_outputs = machine(
        "a",
        [
            Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            Port(id="spare", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
        ],
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[two_outputs, producer("b"), consumer("s"), consumer("t")],
        nets=[
            _shared("a", "b"),
            Net(
                id="spare",
                commodity=Commodity.ITEM,
                fluid_or_item="y",
                throughput=1.0,
                endpoints=[_ref("a", "spare"), _ref("t", "in")],
            ),
        ],
    )
    shared = [c.source.machine_id for c in auto_candidates(problem) if c.net_id == "n"]
    assert shared == ["b"]


# ------------------------------------------------------------------------ what the router lays


def test_producers_against_the_consumer_auto_output_and_the_rest_share_a_pipe() -> None:
    problem, placements = _three_into_one()
    assigned = assign_auto_outputs(problem, placements)

    assert assigned.covered == frozenset()  # c still needs the pipe
    assert assigned.fed == {"n": frozenset({_ref("a", "out"), _ref("b", "out")})}
    faces = {ac.source_machine_id: (ac.source_face, ac.target_face) for ac in assigned.connections}
    assert faces == {"a": (Facing.EAST, Facing.WEST), "b": (Facing.WEST, Facing.EAST)}

    routing = route(problem, placements)
    (pipe,) = routing.routes
    assert pipe.net_id == "n"
    assert {(t.machine_id, t.port_id) for t in pipe.terminals} == {("c", "out"), ("s", "in")}


def test_a_many_into_one_net_with_every_producer_against_the_consumer_needs_no_pipe() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), producer("b"), consumer("s")],
        nets=[_shared("a", "b")],
    )
    placements = [at("a", 3, 0, 4), at("b", 5, 0, 4), at("s", 4, 0, 4)]
    assigned = assign_auto_outputs(problem, placements)
    assert assigned.covered == {"n"}
    assert assigned.fed == {}
    assert route(problem, placements).routes == ()


def test_the_split_layout_validates() -> None:
    problem, placements = _three_into_one()
    report = validate(problem, _layout(problem, placements))
    assert report.ok, str(report)


def test_a_multiblock_producer_feeding_a_shared_consumer_gets_its_output_hatch() -> None:
    source = Machine(
        id="m",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=3, sy=3, sz=3),
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
        hatch_slots=(HatchSlot(offset=CellCoord(x=2, y=1, z=1), kinds=("OutputBus",)),),
        hatch_cells=1,
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[source, producer("c"), consumer("s")],
        nets=[_shared("m", "c")],
    )
    placements = [at("m", 2, 0, 2), at("c", 9, 0, 9), at("s", 5, 1, 3)]
    layout = _layout(problem, placements)

    (auto,) = layout.auto_connections
    assert (auto.source_machine_id, auto.source_face) == ("m", Facing.EAST)
    (hatch,) = layout.hatches  # the chest and the far producer are single blocks: no hatch
    assert (hatch.machine_id, hatch.kind, hatch.port_id) == ("m", "OutputBus", "out")
    assert (hatch.cell.as_tuple(), hatch.facing) == ((4, 1, 3), Facing.EAST)
    assert validate(problem, layout).ok, str(validate(problem, layout))


# ----------------------------------------------------------------- what placement counts


def test_the_placement_reward_leaves_a_shared_consumer_to_the_router() -> None:
    # Rewarding it moved ev-nitrobenzene's layouts both ways and helped none on balance, so a
    # producer standing against a shared consumer is the router's find, not the placer's aim.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), producer("b"), producer("d"), consumer("s"), consumer("t")],
        nets=[_shared("a", "b"), _shared("d", consumers=("t",), nid="solo")],
    )
    assert [(p.source_id, p.sink_id) for p in _auto_candidate_pairs(problem)] == [("d", "t")]


def test_a_producer_ejecting_into_its_consumer_needs_no_dock_cell() -> None:
    # Hammer a is walled in on every side but the chest's: the region's edge west and north, a
    # reserved cell south, one layer high. It docks nothing, so the gate must not call it crowded.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=1, sz=8),
        machines=[producer("a"), producer("c"), consumer("s")],
        nets=[_shared("a", "c")],
        reserved_cells=[CellCoord(x=0, y=0, z=1)],
    )
    placements = [at("a", 0, 0, 0), at("c", 5, 0, 5), at("s", 1, 0, 0)]
    assert assign_auto_outputs(problem, placements).fed == {"n": frozenset({_ref("a", "out")})}
    assert crowded_machines(problem, placements) == ()


# ----------------------------------------------------------------------- what the gate refuses


def test_a_producer_left_unfed_with_no_pipe_reaches_nothing() -> None:
    problem, placements = _three_into_one()
    layout = _layout(problem, placements).model_copy(update={"routes": []})
    report = validate(problem, layout)
    assert ViolationCode.MISSING_CONNECTION in report.codes()
    assert ViolationCode.NET_DOUBLE_CONNECTED not in report.codes()


def test_a_producer_both_auto_outputting_and_docked_on_the_pipe_is_refused() -> None:
    problem, placements = _three_into_one()
    layout = _layout(problem, placements)
    (pipe,) = layout.routes
    # a's south face, one step out of its body: a terminal the router never lays for it.
    extra = Terminal(
        machine_id="a", port_id="out", face=Facing.SOUTH, cell=CellCoord(x=3, y=0, z=5)
    )
    doubled = pipe.model_copy(update={"terminals": [*pipe.terminals, extra]})
    report = validate(problem, layout.model_copy(update={"routes": [doubled]}))
    assert ViolationCode.NET_DOUBLE_CONNECTED in report.codes()


def test_a_split_net_with_two_consumers_is_refused() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), producer("c"), consumer("s"), consumer("t")],
        nets=[_shared("a", "c", consumers=("s", "t"))],
    )
    placements = [at("a", 3, 0, 4), at("c", 9, 0, 9), at("s", 4, 0, 4), at("t", 7, 0, 7)]
    layout = _layout(problem, placements)
    assert layout.auto_connections == []  # the router never splits it
    (pipe,) = layout.routes
    # Hand-split it anyway: a ejects into s, and the pipe forgets a.
    pipe = pipe.model_copy(update={"terminals": [t for t in pipe.terminals if t.machine_id != "a"]})
    auto = assign_auto_outputs(
        problem.model_copy(update={"nets": [_shared("a", consumers=("s",))]}), placements
    ).connections
    split = layout.model_copy(update={"routes": [pipe], "auto_connections": list(auto)})
    assert ViolationCode.NET_DOUBLE_CONNECTED in validate(problem, split).codes()


def test_a_one_to_one_net_both_routed_and_auto_connected_is_still_refused() -> None:
    problem = InputIR(
        bounding_region=_REGION,
        machines=[producer("a"), consumer("s")],
        nets=[_shared("a")],
    )
    placements = [at("a", 3, 0, 4), at("s", 4, 0, 4)]
    auto = assign_auto_outputs(problem, placements).connections
    far = [at("a", 3, 0, 4), at("s", 8, 0, 4)]
    piped = route(problem, far).routes
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        routes=list(piped),
        auto_connections=list(auto),
    )
    assert ViolationCode.NET_DOUBLE_CONNECTED in validate(problem, layout).codes()
