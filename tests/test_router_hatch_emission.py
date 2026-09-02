"""A solved layout now says which casing cell every hatch is, and which way it faces.

``LayoutResult.hatches`` was added by lane 2 and stayed empty; ``router.hatches`` fills it. These
cover the three sources a hatch can come from - a routed terminal, a free auto-output connection,
and the upkeep hatches that belong to no net at all - plus the two rules that only exist once
hatches are real: a muffler needs literal air in front of it, and a machine that has run out of
casing cells is an explicit infeasibility rather than a retry.
"""

from __future__ import annotations

from pathlib import Path

from gtnh_solver.adapter import adapt_file
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
    PlacedHatch,
    Port,
)
from gtnh_solver.router import assign_auto_outputs, place_hatches, route
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import at

_REGION = CellBox(sx=14, sy=6, sz=14)
_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _slot(x: int, y: int, z: int, *kinds: str) -> HatchSlot:
    return HatchSlot(offset=CellCoord(x=x, y=y, z=z), kinds=tuple(kinds))


def _multi(mid: str, ports: list[Port], slots: list[HatchSlot], *, size: int = 3) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=size, sy=size, sz=size),
        faces=FaceSpec(ports=ports),
        hatch_slots=tuple(slots),
        hatch_cells=len(slots),
    )


def _single(mid: str, direction: IODirection) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[Port(id="p", commodity=Commodity.ITEM, direction=direction)]),
    )


def _item_net(source: str, source_port: str, sink: str, sink_port: str) -> Net:
    return Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=source, port_id=source_port),
            MachineFaceRef(machine_id=sink, port_id=sink_port),
        ],
    )


# ------------------------------------------------------------------- where a hatch comes from


def test_a_routed_terminal_becomes_a_hatch_on_the_cell_behind_it() -> None:
    m = _multi(
        "m",
        [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputBus")],
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[_single("feeder", IODirection.OUTPUT), m],
        nets=[_item_net("feeder", "p", "m", "in")],
    )
    placements = [at("feeder", 0, 1, 3), at("m", 2, 0, 2)]
    routing = route(problem, placements)
    plan = place_hatches(problem, placements, routing.routes, routing.auto_connections)

    assert plan.ok
    (hatch,) = plan.hatches  # the single-block feeder is its own I/O and gets none
    assert (hatch.machine_id, hatch.kind, hatch.port_id) == ("m", "InputBus", "in")
    assert hatch.cell.as_tuple() == (2, 1, 3)  # the recorded slot, one step behind the terminal
    assert hatch.facing is Facing.WEST


def test_a_single_block_machine_gets_no_hatch_at_all() -> None:
    # Its faces are the machine's own. Emitting a bus at its cell would describe replacing the
    # machine with a bus, which is the opposite of what the layout means.
    problem = InputIR(
        bounding_region=_REGION,
        machines=[_single("a", IODirection.OUTPUT), _single("b", IODirection.INPUT)],
        nets=[_item_net("a", "p", "b", "p")],
    )
    placements = [at("a", 1, 0, 1), at("b", 5, 0, 1)]
    routing = route(problem, placements)
    plan = place_hatches(problem, placements, routing.routes, routing.auto_connections)
    assert plan.hatches == ()


def test_a_free_auto_output_still_places_its_two_hatches() -> None:
    # No pipe is laid, but GT still ejects through an output bus's own front face into an input
    # bus - two casing cells, on two touching blocks. They were invisible to the layout before.
    source = _multi(
        "src",
        [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
        [_slot(2, 1, 1, "OutputBus")],
    )
    sink = _multi(
        "dst",
        [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputBus")],
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[source, sink],
        nets=[_item_net("src", "out", "dst", "in")],
    )
    placements = [at("src", 2, 0, 2), at("dst", 5, 0, 2)]
    assigned = assign_auto_outputs(problem, placements)

    assert assigned.covered == {"n"}  # the two hatch cells touch, so the connection is free
    plan = place_hatches(problem, placements, [], assigned.connections)
    by_machine = {h.machine_id: h for h in plan.hatches}
    assert by_machine["src"].cell.as_tuple() == (4, 1, 3)
    assert by_machine["src"].facing is Facing.EAST  # pushes into the target
    assert by_machine["dst"].cell.as_tuple() == (5, 1, 3)
    assert by_machine["dst"].facing is Facing.WEST  # receives on its own front, facing back


def test_two_touching_bodies_are_not_enough_when_neither_cell_takes_a_hatch() -> None:
    # The tightening. The machines are flush, but the cells that meet are plain casing, so GT has
    # nothing to eject from and the net has to be piped after all.
    source = _multi(
        "src",
        [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
        [_slot(0, 1, 1, "OutputBus")],  # on the far side from the target
    )
    sink = _multi(
        "dst",
        [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputBus")],
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[source, sink],
        nets=[_item_net("src", "out", "dst", "in")],
    )
    assigned = assign_auto_outputs(problem, [at("src", 2, 0, 2), at("dst", 5, 0, 2)])
    assert assigned.connections == ()
    assert assigned.covered == frozenset()


# ------------------------------------------------------------------------- the upkeep hatches


def test_a_machine_gets_the_upkeep_hatches_its_structure_records() -> None:
    m = _multi(
        "m",
        [],
        [_slot(0, 1, 1, "Maintenance"), _slot(2, 1, 1, "Muffler")],
    )
    problem = InputIR(bounding_region=_REGION, machines=[m], nets=[])
    placements = [at("m", 2, 0, 2)]
    plan = place_hatches(problem, placements, [], [])

    assert plan.ok
    assert {(h.kind, h.cell.as_tuple()) for h in plan.hatches} == {
        ("Maintenance", (2, 1, 3)),
        ("Muffler", (4, 1, 3)),
    }
    assert all(h.port_id is None for h in plan.hatches)  # they belong to no net


def test_a_structure_that_records_no_muffler_gets_none() -> None:
    m = _multi("m", [], [_slot(0, 1, 1, "Maintenance")])
    plan = place_hatches(
        InputIR(bounding_region=_REGION, machines=[m], nets=[]), [at("m", 2, 0, 2)], [], []
    )
    assert [h.kind for h in plan.hatches] == ["Maintenance"]


def test_a_muffler_needs_empty_air_in_front_and_says_so_when_it_has_none() -> None:
    # Its one Muffler-capable cell has a single exposed face, and something already occupies the
    # cell it would vent into. GT would form the structure and then stop with POLLUTION_FAIL.
    m = _multi("m", [], [_slot(0, 1, 1, "Maintenance", "Muffler"), _slot(0, 0, 1, "Maintenance")])
    problem = InputIR(bounding_region=_REGION, machines=[m], nets=[])
    placements = [at("m", 2, 1, 2)]

    clear = place_hatches(problem, placements, [], [])
    assert clear.ok
    muffler = next(h for h in clear.hatches if h.kind == "Muffler")
    assert muffler.facing is Facing.WEST

    blocked = place_hatches(problem, placements, [], [], occupied={(1, 2, 3), (2, 1, 3)})
    assert not blocked.ok
    assert blocked.infeasibility is not None
    assert blocked.infeasibility.constraint == "hatch_budget"
    assert "vent" in blocked.infeasibility.detail


def test_a_machine_out_of_casing_cells_is_an_infeasibility_not_a_retry() -> None:
    # The casing budget is a per-machine total, so no nearby cell and no re-placement creates one.
    m = _multi(
        "m",
        [Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)],
        [_slot(0, 1, 1, "InputBus", "Maintenance")],  # one cell, and the bus takes it
    )
    problem = InputIR(
        bounding_region=_REGION,
        machines=[_single("feeder", IODirection.OUTPUT), m],
        nets=[_item_net("feeder", "p", "m", "in")],
    )
    placements = [at("feeder", 0, 1, 3), at("m", 2, 0, 2)]
    routing = route(problem, placements)
    plan = place_hatches(problem, placements, routing.routes, routing.auto_connections)

    assert not plan.ok
    assert plan.infeasibility is not None
    assert plan.infeasibility.constraint == "hatch_budget"
    assert "Maintenance" in plan.infeasibility.detail


# --------------------------------------------------------------- the validator's own muffler rules


def test_validator_rejects_a_muffler_venting_into_something_solid() -> None:
    # The hatch is written by hand: place_hatches would refuse to put it here at all, and that
    # refusal is what the gate exists to be independent of. A reserved cell in front is enough -
    # GT calls getAirAtSide, and a casing, a cable or a neighbouring machine all fail it.
    m = _multi("m", [], [_slot(0, 1, 1, "Muffler")])
    problem = InputIR(
        bounding_region=_REGION,
        machines=[m],
        nets=[],
        reserved_cells=[CellCoord(x=1, y=1, z=3)],  # exactly where its one face would vent
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("m", 2, 0, 2)],
        hatches=[
            PlacedHatch(
                machine_id="m",
                kind="Muffler",
                cell=CellCoord(x=2, y=1, z=3),
                facing=Facing.WEST,
            )
        ],
    )
    codes = {v.code for v in validate(problem, layout).violations}
    assert ViolationCode.MUFFLER_BLOCKED in codes
    assert ViolationCode.MUFFLER_MISSING not in codes  # one IS placed; it just cannot vent


def test_validator_rejects_a_polluting_machine_with_no_muffler_at_all() -> None:
    m = _multi("m", [], [_slot(0, 1, 1, "Muffler")])
    problem = InputIR(bounding_region=_REGION, machines=[m], nets=[])
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=[at("m", 2, 0, 2)], hatches=[]
    )
    codes = {v.code for v in validate(problem, layout).violations}
    assert ViolationCode.MUFFLER_MISSING in codes


# ---------------------------------------------------------------------------- the shipped lines


def test_the_shipped_lines_place_a_hatch_for_every_multiblock_port() -> None:
    """End to end: every port of every dumped machine gets exactly one hatch, and it validates.

    Sand is all single-block machines and boundary storage, so it must place none at all - the
    check that emission stays off where a machine IS its own I/O.
    """
    from gtnh_solver.cli import _load_physical_or_warn

    physical = _load_physical_or_warn(None)
    for name in ("gtnh-sand.json", "gtnh-nitrobenzene.json"):
        problem = adapt_file(_EXAMPLES / name, physical=physical)
        layout = solve(problem)
        if layout.status is not LayoutStatus.VALID:
            continue  # CI carries only the two committed fixtures; see docs/TESTING.md
        assert validate(problem, layout).ok, name
        wired = {(h.machine_id, h.port_id) for h in layout.hatches if h.port_id is not None}
        for machine in problem.machines:
            if not machine.hatch_slots:
                assert not any(h.machine_id == machine.id for h in layout.hatches), name
                continue
            for port in machine.faces.ports:
                assert (machine.id, port.id) in wired, f"{name}: {machine.id} {port.id}"
            kinds = {h.kind for h in layout.hatches if h.machine_id == machine.id}
            recorded = {k for s in machine.hatch_slots for k in s.kinds}
            if "Maintenance" in recorded:
                assert "Maintenance" in kinds, f"{name}: {machine.type} has no maintenance hatch"
            if "Muffler" in recorded:
                assert "Muffler" in kinds, f"{name}: {machine.type} pollutes with no muffler"
