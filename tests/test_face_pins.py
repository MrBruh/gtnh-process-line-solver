"""Face pins in the router and placement (#249): every stage reads ``Machine.allowed_faces``.

A port pinned to some of its machine's faces (``Port.faces``) docks and auto-outputs there and
nowhere else. The one block that has pins is the Item Filter the adapter places to sort a single
block's merged item outputs: it takes items on every face but its back (its front included) and
pushes them out of its back alone (``MTEBuffer``). Two things are pinned here:

- the pins are honoured by docking (``dock_candidates``, which both routers and the crowding gate
  share), by auto-output, by the placement cost's face term and by the single-block count;
- an unpinned machine is judged exactly as before, the front and nothing else being off limits,
  which is what keeps every layout that predates pins identical.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    Placement,
    Port,
    RelativeFace,
    Terminal,
)
from gtnh_solver.ir.geometry import (
    FACE_DELTAS,
    OPPOSITE_FACE,
    Cell,
    in_region,
    occupied_cells,
    pose_of,
)
from gtnh_solver.placement import crowded_machines, single_block_shortfalls
from gtnh_solver.placement.search import _bodies, _face_shortfall
from gtnh_solver.router import assign_auto_outputs
from gtnh_solver.router._grid import FACE_ORDER, dock_candidates, host_cells
from tests._helpers import at, consumer, machine, producer

_HORIZONTAL = (Facing.NORTH, Facing.EAST, Facing.SOUTH, Facing.WEST)
_FILTER_INPUT_FACES = (
    RelativeFace.FRONT,
    RelativeFace.LEFT,
    RelativeFace.RIGHT,
    RelativeFace.UP,
    RelativeFace.DOWN,
)


def _item_filter(mid: str = "f", resource: str = "r") -> Machine:
    """An Item Filter in exactly the shape the adapter synthesizes one (#249)."""
    return Machine(
        id=mid,
        type="Ultra Low Voltage Item Filter",
        block_key="gregtech:gt.blockmachines@9240",
        voltage_tier="ULV",
        orientation_options=list(_HORIZONTAL),
        filter_items=(resource,),
        faces=FaceSpec(
            ports=[
                Port(
                    id=f"input:{resource}",
                    commodity=Commodity.ITEM,
                    direction=IODirection.INPUT,
                    faces=_FILTER_INPUT_FACES,
                ),
                Port(
                    id=f"output:{resource}",
                    commodity=Commodity.ITEM,
                    direction=IODirection.OUTPUT,
                    faces=(RelativeFace.BACK,),
                ),
            ]
        ),
    )


def _item_net(nid: str, src: tuple[str, str], dst: tuple[str, str]) -> Net:
    return Net(
        id=nid,
        commodity=Commodity.ITEM,
        fluid_or_item="r",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=src[0], port_id=src[1]),
            MachineFaceRef(machine_id=dst[0], port_id=dst[1]),
        ],
    )


def _step(cell: tuple[int, int, int], face: Facing) -> tuple[int, int, int]:
    dx, dy, dz = FACE_DELTAS[face]
    return (cell[0] + dx, cell[1] + dy, cell[2] + dz)


# --------------------------------------------------------------------------- docking


@pytest.mark.parametrize("orientation", _HORIZONTAL)
def test_a_filters_output_docks_on_its_back_only(orientation: Facing) -> None:
    placement = at("f", 3, 1, 3, orientation=orientation)
    cands = dock_candidates(
        "output:r", placement, _item_filter(), set(), set(), CellBox(sx=8, sy=4, sz=8)
    )
    back = OPPOSITE_FACE[orientation]
    assert [t.face for t in cands] == [back]
    assert cands[0].cell.as_tuple() == _step((3, 1, 3), back)


@pytest.mark.parametrize("orientation", _HORIZONTAL)
def test_a_filters_input_may_dock_on_its_front_but_not_its_back(orientation: Facing) -> None:
    placement = at("f", 3, 1, 3, orientation=orientation)
    cands = dock_candidates(
        "input:r", placement, _item_filter(), set(), set(), CellBox(sx=8, sy=4, sz=8)
    )
    faces = {t.face for t in cands}
    assert orientation in faces
    assert faces == set(Facing) - {OPPOSITE_FACE[orientation]}


def test_a_filters_output_has_no_dock_when_its_back_is_taken() -> None:
    placement = at("f", 3, 1, 3, orientation=Facing.NORTH)
    blocked = {_step((3, 1, 3), Facing.SOUTH)}
    cands = dock_candidates(
        "output:r", placement, _item_filter(), blocked, set(), CellBox(sx=8, sy=4, sz=8)
    )
    assert cands == []


def _old_rule_candidates(
    port_id: str,
    placement: Placement,
    m: Machine,
    obstacles: set[Cell],
    region: CellBox,
    claimed: set[Cell],
) -> Iterator[Terminal]:
    """``_grid._dock_faces`` as it was before pins: skip the front, and nothing else."""
    body = set(occupied_cells(placement.cell, m.footprint, placement.orientation))
    slots = m.hatch_slots_for(port_id)
    hosts = host_cells(placement, m, slots)
    if slots is not None:
        hosts = [c for c in hosts if c not in claimed]
    seen: set[Cell] = set()
    for face in FACE_ORDER:
        if face is placement.orientation:
            continue
        for host in hosts:
            cand = _step(host, face)
            if cand in body or cand in seen:
                continue
            if slots is None and cand in claimed:
                continue
            if not in_region(cand, region) or cand in obstacles:
                continue
            seen.add(cand)
            yield Terminal(
                machine_id=placement.machine_id,
                port_id=port_id,
                face=face,
                cell=CellCoord(x=cand[0], y=cand[1], z=cand[2]),
            )


def _multiblock() -> Machine:
    """A 3x2x3 structure with hatch slots on its casing, some interior, of two kinds."""
    slots = [
        HatchSlot(
            offset=CellCoord(x=x, y=y, z=z), kinds=("InputBus",) if x == 0 else ("OutputBus",)
        )
        for x in range(3)
        for y in range(2)
        for z in range(3)
        if (x + y + z) % 2 == 0
    ]
    return Machine(
        id="mb",
        type="mb",
        voltage_tier="LV",
        orientation_options=list(_HORIZONTAL),
        footprint=CellBox(sx=3, sy=2, sz=3),
        hatch_slots=tuple(slots),
        faces=FaceSpec(
            ports=[
                Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            ]
        ),
    )


@pytest.mark.parametrize("orientation", _HORIZONTAL)
def test_unpinned_dock_candidates_are_exactly_the_old_front_rule(orientation: Facing) -> None:
    # REGRESSION: an unpinned port must dock exactly where it always did, in the same order, on a
    # single block and on a multiblock with slots, with obstacles and claimed cells in the way.
    region = CellBox(sx=9, sy=5, sz=9)
    single = machine(
        "s",
        [
            Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
        ],
        orientation=orientation,
    )
    cases = [
        (single, at("s", 4, 2, 4, orientation=orientation), {(5, 2, 4), (4, 3, 4)}, {(4, 2, 5)}),
        (single, at("s", 0, 0, 0, orientation=orientation), set(), set()),
        (_multiblock(), at("mb", 3, 1, 3, orientation=orientation), {(2, 1, 3)}, {(3, 1, 3)}),
    ]
    for m, placement, obstacles, claimed in cases:
        for port_id in ("in", "out"):
            got = dock_candidates(port_id, placement, m, obstacles, set(), region, claimed)
            want = list(_old_rule_candidates(port_id, placement, m, obstacles, region, claimed))
            assert got == want, (m.id, port_id, orientation)
            assert got, "the case docks nowhere, so it checks nothing"


# --------------------------------------------------------------------------- auto-output


def _turnable(mid: str, port: Port) -> Machine:
    """A single block with one port that may face any horizontal way."""
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=list(_HORIZONTAL),
        faces=FaceSpec(ports=[port]),
    )


def _filter_line(
    sink_at: Placement, *, feed_at: Placement | None = None
) -> tuple[InputIR, list[Placement]]:
    """A producer feeding a filter feeding a consumer; the filter sits at (3,0,3) facing north.

    The producer and consumer may face any way, so each test turns them so that their own front
    is never the face that touches the filter: what refuses a connection here is the filter's pins.
    """
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=2, sz=8),
        machines=[
            _turnable("p", Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)),
            _item_filter(),
            _turnable("c", Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)),
        ],
        nets=[
            _item_net("feed", ("p", "out"), ("f", "input:r")),
            _item_net("sorted", ("f", "output:r"), ("c", "in")),
        ],
    )
    placements = [
        feed_at if feed_at is not None else at("p", 0, 0, 0),
        at("f", 3, 0, 3, orientation=Facing.NORTH),
        sink_at,
    ]
    return problem, placements


def test_a_filter_auto_outputs_from_its_back_into_an_adjacent_sink() -> None:
    # South of the filter, behind it, and turned so that its own north face is not its front.
    problem, placements = _filter_line(at("c", 3, 0, 4, orientation=Facing.SOUTH))
    assigned = assign_auto_outputs(problem, placements)
    assert "sorted" in assigned.covered
    (auto,) = [a for a in assigned.connections if a.net_id == "sorted"]
    assert (auto.source_face, auto.target_face) == (Facing.SOUTH, Facing.NORTH)


@pytest.mark.parametrize("side", [(4, 0, 3), (2, 0, 3), (3, 1, 3)])
def test_a_filter_does_not_auto_output_from_a_side(side: tuple[int, int, int]) -> None:
    # East, west and above the filter: faces the unpinned rule would allow, but a filter pushes out
    # of its back and nowhere else, so the net has to be piped.
    problem, placements = _filter_line(at("c", *side))
    assert "sorted" not in assign_auto_outputs(problem, placements).covered


def test_a_filter_may_be_fed_on_its_front() -> None:
    # The producer sits north of the filter, which faces north: it ejects into the filter's front.
    problem, placements = _filter_line(at("c", 6, 0, 6), feed_at=at("p", 3, 0, 2))
    assigned = assign_auto_outputs(problem, placements)
    (auto,) = [a for a in assigned.connections if a.net_id == "feed"]
    assert (auto.source_face, auto.target_face) == (Facing.SOUTH, Facing.NORTH)


def test_nothing_auto_feeds_a_filter_through_its_back() -> None:
    # The producer sits behind the filter, turned so its north face may eject: the filter's back
    # takes nothing in (MTEBuffer.allowPutStack), so the feed has to be piped.
    feed_at = at("p", 3, 0, 4, orientation=Facing.SOUTH)
    problem, placements = _filter_line(at("c", 6, 0, 6), feed_at=feed_at)
    assert "feed" not in assign_auto_outputs(problem, placements).covered


# ------------------------------------------------------------- cost and gate agree on pins


def _walled_filter(*, back_free: bool) -> tuple[InputIR, list[Placement]]:
    """A filter whose back is either free or taken by an unrelated block, all else roomy.

    One layer high, so up and down are out of reach; the producer and consumer stand well apart,
    so no auto-output exempts anything and both filter ports need a dock.
    """
    blocker = Machine(id="b", type="b", voltage_tier="LV", orientation_options=[Facing.NORTH])
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=1, sz=8),
        machines=[producer("p"), _item_filter(), consumer("c"), blocker],
        nets=[
            _item_net("feed", ("p", "out"), ("f", "input:r")),
            _item_net("sorted", ("f", "output:r"), ("c", "in")),
        ],
    )
    placements = [
        at("p", 0, 0, 0),
        at("f", 3, 0, 3, orientation=Facing.NORTH),
        at("c", 7, 0, 7),
        at("b", 0, 0, 7) if back_free else at("b", 3, 0, 4),
    ]
    return problem, placements


def _shortfall(problem: InputIR, placements: list[Placement]) -> float:
    region = problem.bounding_region
    return _face_shortfall(
        [pose_of(p) for p in placements],
        _bodies(problem),
        (region.sx, region.sy, region.sz),
        set(),
    )


def test_a_filter_walled_at_its_back_is_short_to_the_cost_and_the_gate() -> None:
    # Five faces are free for its two connections, so a count of cells would call it fine; but its
    # output may use only the back, and the back is taken. Both the cheap term and the exact gate
    # must say so, or the search keeps layouts the gate then turns away.
    problem, placements = _walled_filter(back_free=False)
    assert crowded_machines(problem, placements) == ("f",)
    assert _shortfall(problem, placements) == pytest.approx(1.0)


def test_a_filter_with_its_back_free_is_fine_to_the_cost_and_the_gate() -> None:
    problem, placements = _walled_filter(back_free=True)
    assert crowded_machines(problem, placements) == ()
    assert _shortfall(problem, placements) == 0.0


def test_the_face_term_charges_no_port_the_gate_exempts() -> None:
    # A machine hemmed in to one free cell carries one item net and three fluid nets that ride ME.
    # The gate charges only the item net, so the cost must not count the ME ports (nor a port on no
    # net) as connections short of a cell.
    hub = machine(
        "h",
        [
            Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            *(
                Port(id=f"f{i}", commodity=Commodity.FLUID, direction=IODirection.OUTPUT)
                for i in range(3)
            ),
            Port(id="spare", commodity=Commodity.ITEM, direction=IODirection.INPUT),
        ],
    )
    walls = [
        Machine(id=f"w{i}", type="w", voltage_tier="LV", orientation_options=[Facing.NORTH])
        for i in range(2)
    ]
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[
            hub,
            consumer("c"),
            *(consumer(f"d{i}", commodity=Commodity.FLUID) for i in range(3)),
            *walls,
        ],
        nets=[
            _item_net("item", ("h", "out"), ("c", "in")),
            *(
                Net(
                    id=f"fl{i}",
                    commodity=Commodity.FLUID,
                    fluid_or_item=f"x{i}",
                    throughput=1.0,
                    endpoints=[
                        MachineFaceRef(machine_id="h", port_id=f"f{i}"),
                        MachineFaceRef(machine_id=f"d{i}", port_id="in"),
                    ],
                )
                for i in range(3)
            ),
        ],
        me_toggles=METoggles(fluids=True),
    )
    # h stands mid-row against the region's south wall facing it, walls on either side, so its
    # one free cell is north of it; the consumers fill the rest of the region.
    placements = [
        at("h", 1, 0, 2, orientation=Facing.SOUTH),
        at("w0", 0, 0, 2),
        at("w1", 2, 0, 2),
        at("c", 0, 0, 0),
        at("d0", 2, 0, 0),
        at("d1", 0, 0, 1),
        at("d2", 2, 0, 1),
    ]
    assert crowded_machines(problem, placements) == ()
    bodies = _bodies(problem)
    assert bodies["h"].port_ids == ("out",)
    assert _shortfall(problem, placements) == 0.0


# --------------------------------------------------------------------------- the face count


def _pinned_block(pins: list[tuple[RelativeFace, ...]]) -> InputIR:
    """A single block whose i-th output is pinned to ``pins[i]``, each feeding a consumer."""
    hub = Machine(
        id="hub",
        type="hub",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(id=f"out{i}", commodity=Commodity.ITEM, direction=IODirection.OUTPUT, faces=p)
                for i, p in enumerate(pins)
            ]
        ),
    )
    return InputIR(
        bounding_region=CellBox(sx=12, sy=4, sz=12),
        machines=[hub, *(consumer(f"c{i}") for i in range(len(pins)))],
        nets=[_item_net(f"n{i}", ("hub", f"out{i}"), (f"c{i}", "in")) for i in range(len(pins))],
    )


def test_a_filter_is_never_a_single_block_short_of_faces() -> None:
    problem, _ = _walled_filter(back_free=True)
    assert single_block_shortfalls(problem) == {}


def test_two_connections_pinned_to_one_face_are_short_of_faces() -> None:
    # Two connections, well under five, and still unbuildable: they both need the back.
    problem = _pinned_block([(RelativeFace.BACK,), (RelativeFace.BACK,)])
    assert single_block_shortfalls(problem) == {"hub": 2}


def test_six_connections_fit_when_the_pins_include_the_front() -> None:
    # The count would refuse six, since an unpinned block has five usable faces; a pinned block
    # may use its front, so six distinct pins fit.
    problem = _pinned_block([(face,) for face in RelativeFace])
    assert single_block_shortfalls(problem) == {}
