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
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import front_on_boundary, in_region
from gtnh_solver.placement import place
from gtnh_solver.placement.constructive import _place_plain
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


def test_the_lattice_seed_leaves_a_line_with_a_multiblock_alone() -> None:
    problem = _problem(
        [_machine("big", footprint=CellBox(sx=2, sy=1, sz=2)), _machine("a"), _machine("b")],
        region=CellBox(sx=6, sy=1, sz=6),
    )
    assert place(problem, lattice=True) == place(problem)


# --- the annealer's seed with groups of parallel single blocks (placement.groups) ---------------


def _cells(placements: Sequence[Placement]) -> dict[str, tuple[int, int, int]]:
    return {p.machine_id: (p.cell.x, p.cell.y, p.cell.z) for p in placements}


def test_the_lattice_seed_lays_a_group_as_one_column_on_a_shelf() -> None:
    # Units a, w (a column of three) and b: three to a row is the squarest floor (5 by 3), each
    # unit two cells along x from the last, and the column runs back to front along z, #1 at its
    # head where its front (north) is free.
    machines = [_machine("a"), _machine("w#1"), _machine("w#2"), _machine("w#3"), _machine("b")]
    problem = _problem(machines, region=CellBox(sx=10, sy=2, sz=10))
    seeded = place(problem, lattice=True)
    assert seeded.ok
    assert _cells(seeded.placements) == {
        "a": (0, 0, 0),
        "w#1": (2, 0, 0),
        "w#2": (2, 0, 1),
        "w#3": (2, 0, 2),
        "b": (4, 0, 0),
    }
    assert {p.orientation for p in seeded.placements} == {Facing.NORTH}
    assert validate(problem, _as_layout(seeded.placements)).ok


def test_the_shelf_starts_a_new_row_past_its_deepest_unit() -> None:
    # Two columns of four and four single blocks: three units a row is the squarest floor (5 by 7),
    # and the first row is as deep as its columns, so the second starts past them and the aisle.
    machines = [
        *(_machine(f"p#{k}") for k in range(1, 5)),
        *(_machine(f"q#{k}") for k in range(1, 5)),
        *(_machine(f"s{k}") for k in range(4)),
    ]
    seeded = place(_problem(machines, region=CellBox(sx=12, sy=1, sz=16)), lattice=True)
    cells = _cells(seeded.placements)
    assert (cells["p#1"], cells["q#1"], cells["s0"]) == ((0, 0, 0), (2, 0, 0), (4, 0, 0))
    assert (cells["p#4"], cells["q#4"]) == ((0, 0, 3), (2, 0, 3))
    assert (cells["s1"], cells["s2"], cells["s3"]) == ((0, 0, 6), (2, 0, 6), (4, 0, 6))


def test_a_group_on_a_multiblock_line_takes_the_first_free_column() -> None:
    # No shelf with a multiblock: each unit takes the first slot of the plain scan its box fits,
    # so the column lands beside the multiblock where the plain seed put its members side by side.
    machines = [
        _machine("big", footprint=CellBox(sx=2, sy=1, sz=2)),
        _machine("w#1"),
        _machine("w#2"),
    ]
    problem = _problem(machines, region=CellBox(sx=6, sy=1, sz=6))
    assert _cells(place(problem, lattice=True).placements) == {
        "big": (0, 0, 0),
        "w#1": (2, 0, 0),
        "w#2": (2, 0, 1),
    }
    assert _cells(place(problem).placements)["w#2"] == (3, 0, 0)


def test_a_column_with_no_room_is_dissolved_into_its_members() -> None:
    # A north-facing column of three needs a free run of three along z, and the reserved row at
    # z=1 leaves none: its members are fitted one at a time instead, and the line still seeds.
    machines = [_machine(f"w#{k}") for k in (1, 2, 3)]
    reserved = [CellCoord(x=x, y=0, z=1) for x in range(4)]
    problem = _problem(machines, region=CellBox(sx=4, sy=1, sz=3), reserved=reserved)
    seeded = place(problem, lattice=True)
    assert seeded.ok
    assert sorted(_cells(seeded.placements).values()) == [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
    assert validate(problem, _as_layout(seeded.placements)).ok


def test_a_source_goes_after_the_shelf_with_its_feed_face_out() -> None:
    machines = [_source(), _machine("w#1"), _machine("w#2"), _machine("a")]
    problem = _problem(machines, region=CellBox(sx=6, sy=1, sz=6))
    seeded = place(problem, lattice=True)
    cells = _cells(seeded.placements)
    assert (cells["w#1"], cells["w#2"], cells["a"]) == ((0, 0, 0), (0, 0, 1), (2, 0, 0))
    assert validate(problem, _as_layout(seeded.placements)).ok


def test_a_grouped_seed_that_cannot_place_the_line_falls_back_to_the_plain_one() -> None:
    # A 3x1x1 corridor: the column cannot stand (it needs two cells of z), so its members are
    # fitted from the west end, and the west-facing source that goes last finds its only feed cell
    # taken. The plain seed seats the source first and places the line.
    machines = [_source(orientations=[Facing.WEST]), _machine("w#1"), _machine("w#2")]
    problem = _problem(machines, region=CellBox(sx=3, sy=1, sz=1))
    plain = _place_plain(problem, lattice=True)
    assert plain.ok
    assert place(problem, lattice=True) == plain


def test_only_the_annealers_seed_groups() -> None:
    # The fast path and the solver's fallback keep the plain row, whose neighbours touch.
    machines = [_machine(f"w#{k}") for k in (1, 2, 3)]
    problem = _problem(machines, region=CellBox(sx=6, sy=1, sz=6))
    assert place(problem) == _place_plain(problem, lattice=False)
    assert {p.cell.z for p in place(problem).placements} == {0}


@given(
    sx=st.integers(min_value=1, max_value=6),
    sz=st.integers(min_value=1, max_value=6),
    singles=st.integers(min_value=0, max_value=6),
    sizes=st.lists(st.integers(min_value=2, max_value=5), max_size=3),
    reserved=st.sets(st.tuples(st.integers(0, 5), st.integers(0, 5)), max_size=6),
)
def test_grouping_never_changes_whether_the_seed_is_feasible(
    sx: int, sz: int, singles: int, sizes: list[int], reserved: set[tuple[int, int]]
) -> None:
    # The grouped seed falls back to the plain one, so it can never lose a line the plain seed
    # places; without an outside front, both place the line exactly when it has the cells for it.
    machines = [_machine(f"s{i}") for i in range(singles)]
    for g, size in enumerate(sizes):
        machines += [_machine(f"g{g}#{k}") for k in range(1, size + 1)]
    blocked = [CellCoord(x=x, y=0, z=z) for x, z in reserved if x < sx and z < sz]
    problem = _problem(machines, region=CellBox(sx=sx, sy=1, sz=sz), reserved=blocked)

    grouped = place(problem, lattice=True)
    assert grouped.ok is _place_plain(problem, lattice=True).ok
    assert grouped.ok is (len(machines) <= sx * sz - len(blocked))
    if grouped.ok:
        assert len(grouped.placements) == len(machines)
        assert PLACEMENT_CODES.isdisjoint(validate(problem, _as_layout(grouped.placements)).codes())


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
