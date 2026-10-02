"""Tests for the Phase 1 crude constructive placer.

Placement is orthogonal to nets, so these use net-free problems: a placement-only
``LayoutResult`` then validates cleanly (``ok``) exactly when the geometry is sound, which
gives an independent cross-check of the placer via the validator. The headline invariant
(property test) is the project's core promise: any input yields a valid placement OR an
explicit infeasibility, never a silently-overlapping/out-of-bounds one.
"""

from __future__ import annotations

from collections.abc import Sequence

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
    Net,
    Placement,
    Port,
    RelativeFace,
)
from gtnh_solver.ir.geometry import front_on_boundary, in_region, occupied_cells
from gtnh_solver.placement import PlacementResult, crowded_machines, place
from gtnh_solver.placement.constructive import _busy_blocks, _flow_order, _place, _seed_offers
from gtnh_solver.validator import validate
from tests._helpers import PLACEMENT_CODES, power_source


def _machine(
    mid: str,
    *,
    footprint: CellBox | None = None,
    orientations: list[Facing] | None = None,
) -> Machine:
    return Machine(
        id=mid,
        type="gt.machine",
        footprint=footprint if footprint is not None else CellBox(),
        voltage_tier="LV",
        orientation_options=orientations if orientations is not None else [Facing.NORTH],
    )


def _problem(
    machines: list[Machine],
    *,
    region: CellBox | None = None,
    reserved: list[CellCoord] | None = None,
) -> InputIR:
    return InputIR(
        bounding_region=region if region is not None else CellBox(sx=4, sy=2, sz=4),
        machines=machines,
        nets=[],
        reserved_cells=reserved if reserved is not None else [],
    )


def _as_layout(placements: Sequence[Placement]) -> LayoutResult:
    return LayoutResult(status=LayoutStatus.VALID, seed=0, placements=list(placements))


def _cells(result: PlacementResult) -> dict[str, tuple[int, int, int]]:
    """Each placed machine's origin, by id."""
    return {p.machine_id: (p.cell.x, p.cell.y, p.cell.z) for p in result.placements}


def test_places_all_machines_disjoint_and_in_bounds() -> None:
    problem = _problem([_machine("a"), _machine("b"), _machine("c")])
    result = place(problem)
    assert result.ok
    assert len(result.placements) == 3
    cells = [(p.cell.x, p.cell.y, p.cell.z) for p in result.placements]
    assert len(set(cells)) == 3
    assert all(in_region(c, problem.bounding_region) for c in cells)


def test_validator_certifies_placement() -> None:
    problem = _problem([_machine("a"), _machine("b")])
    result = place(problem)
    assert validate(problem, _as_layout(result.placements)).ok


def test_respects_reserved_cells() -> None:
    problem = _problem(
        [_machine("a")], region=CellBox(sx=2, sy=1, sz=1), reserved=[CellCoord(x=0, y=0, z=0)]
    )
    result = place(problem)
    assert result.ok
    assert result.placements[0].cell == CellCoord(x=1, y=0, z=0)  # avoided the reserved cell


def test_multiblock_footprint_does_not_overlap() -> None:
    problem = _problem(
        [_machine("big", footprint=CellBox(sx=2, sy=1, sz=2)), _machine("small")],
        region=CellBox(sx=4, sy=1, sz=4),
    )
    result = place(problem)
    assert result.ok
    assert validate(problem, _as_layout(result.placements)).ok  # validator confirms no overlap


def test_placement_is_deterministic() -> None:
    problem = _problem([_machine("a"), _machine("b"), _machine("c")])
    assert place(problem).placements == place(problem).placements


def test_orientation_is_the_first_legal_option() -> None:
    problem = _problem([_machine("a", orientations=[Facing.EAST, Facing.WEST])])
    assert place(problem).placements[0].orientation == Facing.EAST


def test_infeasible_when_region_too_small() -> None:
    problem = _problem([_machine("a"), _machine("b")], region=CellBox(sx=1, sy=1, sz=1))
    result = place(problem)
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "bounding_region"
    assert "b" in result.infeasibility.detail
    assert len(result.placements) == 1  # the first machine was still placed
    assert result.placements[0].machine_id == "a"


def test_empty_problem_is_ok() -> None:
    result = place(_problem([]))
    assert result.ok
    assert result.placements == ()


def _source(mid: str = "src", *, orientations: list[Facing] | None = None) -> Machine:
    """A power source (the adapter's synthesized shape), delegating the boilerplate to the shared
    ``power_source`` builder. Defaults to all four horizontal fronts so first-fit can reorient the
    reserved feed face onto the boundary."""
    return power_source(
        mid,
        orientations=(
            orientations
            if orientations is not None
            else [Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST]
        ),
    )


def test_power_source_feed_face_lands_on_the_boundary() -> None:
    # The source's front face is its reserved external-feed face: wherever first-fit seats it,
    # that face must end up flush on a region wall (validator-enforced).
    problem = _problem([_machine("a"), _machine("b"), _source()])
    result = place(problem)
    assert result.ok
    src = next(p for p in result.placements if p.machine_id == "src")
    machine = next(m for m in problem.machines if m.id == "src")
    assert front_on_boundary(src.cell, machine.footprint, src.orientation, problem.bounding_region)
    assert validate(problem, _as_layout(result.placements)).ok


def test_power_source_reorients_to_reach_the_boundary() -> None:
    # From the corner slot first-fit finds, the source's first listed orientation (south, an
    # interior-facing front there) cannot host the feed; the placer must pick the orientation
    # that puts the feed face on the wall, not blindly take the first legal one.
    problem = _problem([_source(orientations=[Facing.SOUTH, Facing.NORTH])])
    result = place(problem)
    assert result.ok
    assert result.placements[0].orientation is Facing.NORTH


def test_power_source_without_a_boundary_slot_is_power_feed_infeasible() -> None:
    # A 3x1x3 region with everything but the center reserved: the source fits only at the
    # center, where no horizontal front can touch the boundary - an explicit power_feed
    # infeasibility, never a silently-buried source.
    ring = [CellCoord(x=x, y=0, z=z) for x in range(3) for z in range(3) if (x, z) != (1, 1)]
    problem = _problem([_source()], region=CellBox(sx=3, sy=1, sz=3), reserved=ring)
    result = place(problem)
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "power_feed"


def test_non_source_machine_may_sit_mid_region() -> None:
    # The feed rule is source-specific: an ordinary machine takes the interior slot fine.
    ring = [CellCoord(x=x, y=0, z=z) for x in range(3) for z in range(3) if (x, z) != (1, 1)]
    problem = _problem([_machine("a")], region=CellBox(sx=3, sy=1, sz=3), reserved=ring)
    result = place(problem)
    assert result.ok
    assert result.placements[0].cell == CellCoord(x=1, y=0, z=1)


def _crop_manager(mid: str = "cm") -> Machine:
    """A machine facing outside the build (#282): a Crop Manager, the block a crop card's output
    comes from, its field outside the line. It has no power port; the flag alone pins its front."""
    return _machine(
        mid, orientations=[Facing.SOUTH, Facing.NORTH, Facing.EAST, Facing.WEST]
    ).model_copy(update={"type": "Basic Crop Manager", "outside_front": True})


def test_an_outside_front_lands_on_the_boundary_like_a_feed_face() -> None:
    # Its first orientation (south) faces the interior from the corner first-fit finds, so the
    # placer must turn it, exactly as it turns a power source's feed face onto the wall.
    problem = _problem([_machine("a"), _crop_manager()])
    result = place(problem)
    assert result.ok
    cm = next(p for p in result.placements if p.machine_id == "cm")
    machine = next(m for m in problem.machines if m.id == "cm")
    assert front_on_boundary(cm.cell, machine.footprint, cm.orientation, problem.bounding_region)
    assert validate(problem, _as_layout(result.placements)).ok


def test_an_outside_front_without_a_boundary_slot_is_explicitly_infeasible() -> None:
    ring = [CellCoord(x=x, y=0, z=z) for x in range(3) for z in range(3) if (x, z) != (1, 1)]
    problem = _problem([_crop_manager()], region=CellBox(sx=3, sy=1, sz=3), reserved=ring)
    result = place(problem)
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "outside_front"
    assert "Basic Crop Manager 'cm'" in result.infeasibility.detail


def test_the_lattice_seed_spaces_a_single_block_line() -> None:
    # The annealer's seed: rows of ceil(sqrt(7)) = 3 blocks, every other cell along x and every
    # third along z, all on the floor. The fast path's plain scan still packs them into a row.
    problem = _problem([_machine(f"m{i}") for i in range(7)], region=CellBox(sx=8, sy=2, sz=8))
    seeded = place(problem, lattice=True)
    assert seeded.ok
    assert [(p.cell.x, p.cell.y, p.cell.z) for p in seeded.placements] == [
        (x, 0, z) for z in (0, 3, 6) for x in (0, 2, 4)
    ][:7]
    assert {p.cell.z for p in place(problem).placements} == {0}


def test_the_lattice_seed_spills_past_a_window_the_region_clips() -> None:
    # Nine blocks want a 5x7 window; a 4x4 region clips it to four lattice points, and the rest take
    # the first free cells of the plain scan, never a cell twice.
    problem = _problem([_machine(f"m{i}") for i in range(9)], region=CellBox(sx=4, sy=1, sz=4))
    seeded = place(problem, lattice=True)
    assert seeded.ok
    cells = [(p.cell.x, p.cell.z) for p in seeded.placements]
    assert cells[:4] == [(0, 0), (2, 0), (0, 3), (2, 3)]
    assert cells[4:] == [(1, 0), (3, 0), (0, 1), (1, 1), (2, 1)]
    assert validate(problem, _as_layout(seeded.placements)).ok


def _shelved(*machines: Machine, region: CellBox) -> InputIR:
    """A line with a 2x1x2 multiblock (``big``) first, then ``machines``, which the seed shelves."""
    return _problem(
        [_machine("big", footprint=CellBox(sx=2, sy=1, sz=2)), *machines], region=region
    )


def test_the_shelf_seed_spaces_a_line_with_a_multiblock() -> None:
    # A channel after each machine, and a new row once the next would cross the shelf width: 5,
    # the side of a square holding the 24 cells the three need with their gaps. The row starts an
    # aisle behind the deepest machine of the last. The plain scan packs the same three in a row.
    problem = _shelved(_machine("a"), _machine("b"), region=CellBox(sx=10, sy=1, sz=10))
    seeded = place(problem, lattice=True)
    assert seeded.ok
    assert _cells(seeded) == {"big": (0, 0, 0), "a": (3, 0, 0), "b": (0, 0, 4)}
    assert _cells(place(problem)) == {"big": (0, 0, 0), "a": (2, 0, 0), "b": (3, 0, 0)}
    assert validate(problem, _as_layout(seeded.placements)).ok


def test_a_machine_past_the_shelf_takes_the_plain_scan() -> None:
    # b's row would start at z=4, past the far edge of a region 3 deep, so it is offered no slot
    # and takes the first free cell of the plain scan: the channel after big.
    problem = _shelved(_machine("a"), _machine("b"), region=CellBox(sx=5, sy=1, sz=3))
    seeded = place(problem, lattice=True)
    assert _cells(seeded) == {"big": (0, 0, 0), "a": (3, 0, 0), "b": (2, 0, 0)}
    assert validate(problem, _as_layout(seeded.placements)).ok


def test_the_spaced_seed_falls_back_to_the_plain_scan() -> None:
    # Shelved first, a and b leave no 2x2 gap in a 4x1x2 region, so big is stranded; the plain
    # scan packs a and b into the corner and seats it. The seed is the plain scan's, whole.
    problem = _problem(
        [_machine("a"), _machine("b"), _machine("big", footprint=CellBox(sx=2, sy=1, sz=2))],
        region=CellBox(sx=4, sy=1, sz=2),
    )
    order = _flow_order(problem)
    assert not _place(problem, order, _seed_offers(problem, order)).ok
    seeded = place(problem, lattice=True)
    assert seeded.ok
    assert seeded == place(problem)


def test_a_power_source_off_the_boundary_on_the_shelf_takes_a_boundary_slot() -> None:
    # The source's shelf slot, (2, 0, 4), is inside the region, where no front reaches a wall, so
    # it takes the first slot of the plain scan that puts its feed face on one: the channel after
    # big, facing north.
    problem = _shelved(
        _machine("a"), _machine("b"), _machine("c"), _source(), region=CellBox(sx=10, sy=1, sz=10)
    )
    seeded = place(problem, lattice=True)
    assert seeded.ok
    assert _cells(seeded) == {
        "big": (0, 0, 0),
        "a": (3, 0, 0),
        "b": (5, 0, 0),
        "c": (0, 0, 4),
        "src": (2, 0, 0),
    }
    src = next(p for p in seeded.placements if p.machine_id == "src")
    assert src.orientation is Facing.NORTH
    assert validate(problem, _as_layout(seeded.placements)).ok


_FACINGS = st.sampled_from([Facing.NORTH, Facing.EAST])


@given(
    boxes=st.lists(
        st.tuples(st.integers(1, 3), st.integers(1, 2), st.integers(1, 3), _FACINGS), max_size=8
    ),
    region=st.builds(CellBox, sx=st.integers(1, 6), sy=st.integers(1, 3), sz=st.integers(1, 6)),
    reserved=st.sets(
        st.tuples(st.integers(0, 5), st.integers(0, 2), st.integers(0, 5)), max_size=6
    ),
    source=st.booleans(),
)
def test_the_spaced_seed_places_whatever_the_plain_scan_places(
    boxes: list[tuple[int, int, int, Facing]],
    region: CellBox,
    reserved: set[tuple[int, int, int]],
    source: bool,
) -> None:
    # Any mix of footprints, turned or not, around reserved cells: the spaced seed never refuses a
    # line the plain scan places, and what it lays is valid or explicitly infeasible.
    machines = [
        _machine(f"m{i}", footprint=CellBox(sx=x, sy=y, sz=z), orientations=[facing])
        for i, (x, y, z, facing) in enumerate(boxes)
    ]
    held = sorted(c for c in reserved if in_region(c, region))
    problem = _problem(
        [*machines, _source()] if source else machines,
        region=region,
        reserved=[CellCoord(x=x, y=y, z=z) for x, y, z in held],
    )
    seeded = place(problem, lattice=True)
    if place(problem).ok:
        assert seeded.ok

    footprints = {m.id: m.footprint for m in problem.machines}
    cells = [
        c
        for p in seeded.placements
        for c in occupied_cells(p.cell, footprints[p.machine_id], p.orientation)
    ]
    assert len(cells) == len(set(cells))  # never overlapping
    assert all(in_region(c, region) for c in cells)  # never out of bounds
    assert set(held).isdisjoint(cells)  # never on a reserved cell
    if seeded.ok:
        assert PLACEMENT_CODES.isdisjoint(validate(problem, _as_layout(seeded.placements)).codes())
    else:
        assert seeded.infeasibility is not None


def _busy_star(
    spokes: int,
    *,
    region: CellBox,
    hub: IODirection = IODirection.OUTPUT,
    pinned: tuple[RelativeFace, ...] | None = None,
) -> InputIR:
    """A single-block hub with one item connection to each of ``spokes`` single blocks.

    The hub's port runs ``hub`` (an output feeds the spokes, so the hub comes first in flow
    order; an input is fed by them, so it comes last), and may be ``pinned`` to some faces."""
    spoke = IODirection.INPUT if hub is IODirection.OUTPUT else IODirection.OUTPUT

    def block(mid: str, direction: IODirection, faces: tuple[RelativeFace, ...] | None) -> Machine:
        port = Port(id="io", commodity=Commodity.ITEM, direction=direction, faces=faces)
        return Machine(
            id=mid,
            type="gt.machine",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(ports=[port]),
        )

    return InputIR(
        bounding_region=region,
        machines=[block("hub", hub, pinned), *(block(f"s{i}", spoke, None) for i in range(spokes))],
        nets=[
            Net(
                id=f"n{i}",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="hub", port_id="io"),
                    MachineFaceRef(machine_id=f"s{i}", port_id="io"),
                ],
            )
            for i in range(spokes)
        ],
    )


_ROOM = CellBox(sx=8, sy=2, sz=8)


def test_the_spaced_seed_lifts_a_busy_single_block_off_the_floor() -> None:
    # Four connections, and four faces for them on the floor (no down, no I/O on the front), so
    # none to spare: the hub takes the first lattice point, moves one cell up, and the point stays
    # empty under it for its down face. The spokes take the points they take without the lift, not
    # the held one.
    seeded = place(_busy_star(4, region=_ROOM), lattice=True)
    assert _cells(seeded) == {
        "hub": (0, 1, 0),
        "s0": (2, 0, 0),
        "s1": (4, 0, 0),
        "s2": (0, 0, 3),
        "s3": (2, 0, 3),
    }


def test_a_block_with_three_connections_stays_on_the_floor() -> None:
    # Four faces leave one to spare for three connections.
    assert _cells(place(_busy_star(3, region=_ROOM), lattice=True))["hub"] == (0, 0, 0)


def test_a_busy_block_stays_on_the_floor_of_a_region_one_cell_high() -> None:
    flat = CellBox(sx=8, sy=1, sz=8)
    assert _cells(place(_busy_star(5, region=flat), lattice=True))["hub"] == (0, 0, 0)


def test_the_plain_scan_never_lifts() -> None:
    assert {p.cell.y for p in place(_busy_star(5, region=_ROOM)).placements} == {0}


def test_a_busy_block_pinned_off_its_down_face_stays_on_the_floor() -> None:
    # Lifted, it would gain a face its port may not use.
    problem = _busy_star(5, region=_ROOM, pinned=(RelativeFace.UP, RelativeFace.BACK))
    assert _cells(place(problem, lattice=True))["hub"] == (0, 0, 0)


def test_a_lifted_busy_block_is_not_crowded_where_on_the_floor_it_is() -> None:
    # Fed by its spokes, the hub comes last and takes an inner lattice point. Lifted, it docks its
    # five connections on its four sides and below; on the floor, the gate proves it cannot.
    problem = _busy_star(5, region=CellBox(sx=8, sy=3, sz=8), hub=IODirection.INPUT)
    seeded = place(problem, lattice=True)
    assert _cells(seeded)["hub"] == (4, 1, 3)
    assert crowded_machines(problem, seeded.placements) == ()
    floor = [
        p.model_copy(update={"cell": CellCoord(x=4, y=0, z=3)}) if p.machine_id == "hub" else p
        for p in seeded.placements
    ]
    assert crowded_machines(problem, floor) == ("hub",)


@given(
    spokes=st.integers(0, 6),
    region=st.builds(CellBox, sx=st.integers(1, 4), sy=st.integers(1, 4), sz=st.integers(1, 4)),
    hub=st.sampled_from([IODirection.OUTPUT, IODirection.INPUT]),
)
def test_the_lift_raises_only_the_busy_block_and_holds_its_cell(
    spokes: int, region: CellBox, hub: IODirection
) -> None:
    # The seed is valid or explicitly infeasible, and places whatever the plain scan places. On
    # the spaced path the lift moves the hub, and only ever one cell up from the slot it takes
    # without the lift, leaving that slot empty.
    problem = _busy_star(spokes, region=region, hub=hub)
    seeded = place(problem, lattice=True)
    assert seeded.ok is place(problem).ok
    cells = _cells(seeded)
    assert len(set(cells.values())) == len(cells)  # never overlapping (every block is 1x1x1)
    assert all(in_region(c, region) for c in cells.values())
    if seeded.ok:
        assert PLACEMENT_CODES.isdisjoint(validate(problem, _as_layout(seeded.placements)).codes())
    else:
        assert seeded.infeasibility is not None

    order = _flow_order(problem)
    offers = _seed_offers(problem, order)
    lifted = _cells(_place(problem, order, offers, _busy_blocks(problem)))
    level = _cells(_place(problem, order, offers))
    if "hub" in lifted and lifted["hub"] != level["hub"]:
        x, y, z = level["hub"]
        assert lifted["hub"] == (x, y + 1, z)
        assert (x, y, z) not in lifted.values()


@given(
    sx=st.integers(min_value=1, max_value=5),
    sy=st.integers(min_value=1, max_value=3),
    sz=st.integers(min_value=1, max_value=5),
    n=st.integers(min_value=0, max_value=12),
    lattice=st.booleans(),
)
def test_place_yields_valid_layout_or_explicit_infeasibility(
    sx: int, sy: int, sz: int, n: int, lattice: bool
) -> None:
    problem = _problem([_machine(f"m{i}") for i in range(n)], region=CellBox(sx=sx, sy=sy, sz=sz))
    result = place(problem, lattice=lattice)

    # With 1x1x1 machines and no reserved cells, first-fit fills one cell each: feasible iff
    # the count fits the region's cell capacity.
    capacity = sx * sy * sz
    assert result.ok is (n <= capacity)

    cells = [(p.cell.x, p.cell.y, p.cell.z) for p in result.placements]
    assert len(cells) == len(set(cells))  # never overlapping
    assert all(in_region(c, problem.bounding_region) for c in cells)  # never out of bounds

    if result.ok:
        assert len(result.placements) == n
        assert PLACEMENT_CODES.isdisjoint(validate(problem, _as_layout(result.placements)).codes())
    else:
        assert result.infeasibility is not None
