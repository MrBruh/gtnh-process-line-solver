"""Rotation-aware cell geometry: the oracle and the two-implementations agreement.

Three things are pinned here, and they exist because the failure this lane can introduce is
*silent*. A rotation bug does not raise; it produces a plausible layout on the wrong cells that
passes every other check.

1. **The oracle.** Every controller in the dump, at every facing, expanded from the raw block
   offsets the extractor recorded rather than from the solver's own box arithmetic. If the solver
   and the dump disagree about which cells a machine covers, the solver is wrong.
2. **The two implementations agree.** ``ir.geometry.occupied_cells`` (the solver) and
   ``validator._geometry.body_cells`` (the gate) are written independently, per
   docs/ARCHITECTURE.md #4. They must agree everywhere, and a property test is the only way to
   say that convincingly.
3. **The fast path equals the general path.** ``occupied_cells`` rotates a box by swapping its
   extents; lane 2's hatch slots will rotate arbitrary offsets with ``rotate_offset``. Those two
   must not drift, so the swap is checked against a from-scratch rotate-and-re-anchor. The same
   argument covers ``auto_output_faces``, which answers a body-adjacency question from the two
   rotated boxes (issue #110) where it used to expand and intersect both cell sets: the box
   arithmetic is pinned against that cell-set formulation. ``box_in_region`` is the same trade in
   the other direction - a whole body against the region wall - and is pinned against the cell walk
   it replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.dataset import MultiblockDoc, load_multiblock_doc
from gtnh_solver.ir import CellBox, CellCoord, Facing, HatchSlot
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED
from gtnh_solver.ir.geometry import (
    CW_STEPS,
    FACE_DELTAS,
    OPPOSITE_FACE,
    auto_output_faces,
    box_in_region,
    in_region,
    occupied_cells,
    rotate_offset,
    rotated_footprint,
    rotated_slot,
)
from gtnh_solver.validator._geometry import body_cells, hatch_cells

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "multiblocks"

_ORIGINS = st.builds(
    CellCoord,
    x=st.integers(min_value=-8, max_value=8),
    y=st.integers(min_value=-8, max_value=8),
    z=st.integers(min_value=-8, max_value=8),
)
_FOOTPRINTS = st.builds(
    CellBox,
    sx=st.integers(min_value=1, max_value=6),
    sy=st.integers(min_value=1, max_value=6),
    sz=st.integers(min_value=1, max_value=6),
)
_FACINGS = st.sampled_from(HORIZONTAL_FACINGS_ORDERED)


def _rotate_and_reanchor(
    origin: CellCoord, footprint: CellBox, orientation: Facing
) -> set[tuple[int, int, int]]:
    """The general path: rotate every offset, then translate the minimum corner onto ``origin``.

    This is what lane 2's hatch slots will need, and deliberately shares no code with the extent
    swap ``occupied_cells`` uses. Mirrors ``previewer/textures._place_blocks``, which has rotated
    the dump's offsets this way since the texture pass shipped.
    """
    steps = CW_STEPS[orientation]
    turned = [
        (*rotate_offset(dx, dz, steps), dy)
        for dx in range(footprint.sx)
        for dy in range(footprint.sy)
        for dz in range(footprint.sz)
    ]
    min_x = min(t[0] for t in turned)
    min_z = min(t[1] for t in turned)
    return {(origin.x + x - min_x, origin.y + dy, origin.z + z - min_z) for x, z, dy in turned}


@given(origin=_ORIGINS, footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_the_extent_swap_matches_a_from_scratch_rotation(
    origin: CellCoord, footprint: CellBox, orientation: Facing
) -> None:
    assert set(occupied_cells(origin, footprint, orientation)) == _rotate_and_reanchor(
        origin, footprint, orientation
    )


@given(origin=_ORIGINS, footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_solver_and_validator_expansions_agree(
    origin: CellCoord, footprint: CellBox, orientation: Facing
) -> None:
    # Independently written on purpose (see the module docstring); this is what makes the gate a
    # gate rather than a second copy of the solver's opinion.
    assert set(occupied_cells(origin, footprint, orientation)) == body_cells(
        origin, footprint, orientation
    )


@given(origin=_ORIGINS, footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_rotation_preserves_the_cell_count_and_the_minimum_corner(
    origin: CellCoord, footprint: CellBox, orientation: Facing
) -> None:
    cells = set(occupied_cells(origin, footprint, orientation))
    assert len(cells) == footprint.volume  # a turn moves cells, it never creates or destroys them
    assert min(cells) == (origin.x, origin.y, origin.z)  # origin stays the minimum corner


@given(footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_a_half_turn_restores_the_extents(footprint: CellBox, orientation: Facing) -> None:
    # Two quarter turns is a half turn, which any box is symmetric under.
    once = rotated_footprint(footprint, orientation)
    twice = rotated_footprint(once, orientation)
    assert twice == footprint if CW_STEPS[orientation] % 2 else once == footprint


def _controller_docs() -> list[tuple[str, MultiblockDoc]]:
    return [
        (path.stem, load_multiblock_doc(path))
        for path in sorted(_DATA_DIR.glob("*.json"))
        if path.name != "_meta.json"
    ]


@pytest.mark.parametrize(("name", "doc"), _controller_docs())
def test_oracle_every_controller_at_every_facing(name: str, doc: MultiblockDoc) -> None:
    """The reserved box must cover the machine's real blocks, turned, at all four facings.

    Re-derived from the dump's own ``blocks`` offsets rather than from the footprint arithmetic
    under test. The dump states its convention as *controller front = NORTH (-Z), offsets are
    world-space deltas from the controller block*, so a machine faced elsewhere is that block set
    turned about the vertical axis and re-anchored on its minimum corner.
    """
    for variant in doc.variants:
        offsets = [tuple(b.d) for b in variant.blocks]
        base_min = (
            min(o[0] for o in offsets),
            min(o[1] for o in offsets),
            min(o[2] for o in offsets),
        )
        size = CellBox(
            sx=max(o[0] for o in offsets) - base_min[0] + 1,
            sy=max(o[1] for o in offsets) - base_min[1] + 1,
            sz=max(o[2] for o in offsets) - base_min[2] + 1,
        )
        origin = CellCoord(x=0, y=0, z=0)
        for facing in HORIZONTAL_FACINGS_ORDERED:
            steps = CW_STEPS[facing]
            turned = [(*rotate_offset(dx, dz, steps), dy) for dx, dy, dz in offsets]
            min_x = min(t[0] for t in turned)
            min_y = min(t[2] for t in turned)
            min_z = min(t[1] for t in turned)
            real = {(x - min_x, dy - min_y, z - min_z) for x, z, dy in turned}
            reserved = set(occupied_cells(origin, size, facing))
            assert real <= reserved, f"{name} {facing.value}: blocks fall outside the reserved box"
            assert body_cells(origin, size, facing) == reserved, f"{name} {facing.value}"


@given(footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_rotated_slot_maps_a_machines_own_cells_onto_its_reserved_box(
    footprint: CellBox, orientation: Facing
) -> None:
    """Turning every cell of a machine must reproduce exactly the box it reserves.

    This is the invariant hatch placement rests on: a slot offset turned with its machine has to
    land on a cell the machine actually occupies, or a hatch would be placed outside the footprint
    the placer reserved and inside a neighbour's.
    """
    turned = {
        rotated_slot((dx, dy, dz), footprint, orientation)
        for dx in range(footprint.sx)
        for dy in range(footprint.sy)
        for dz in range(footprint.sz)
    }
    assert turned == set(occupied_cells(CellCoord(x=0, y=0, z=0), footprint, orientation))


@given(origin=_ORIGINS, footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_solver_and_validator_slot_rotations_agree(
    origin: CellCoord, footprint: CellBox, orientation: Facing
) -> None:
    """``rotated_slot`` (solver) and ``_geometry.hatch_cells`` (gate) must place a slot alike.

    The same argument as :func:`test_solver_and_validator_expansions_agree`, one level finer.
    Getting the body box right and the slots inside it wrong is exactly the silent failure the
    split exists to catch: a hatch on the wrong casing cell yields a layout that validates clean,
    forms in game, and moves nothing.
    """
    offsets = [
        (dx, dy, dz)
        for dx in range(footprint.sx)
        for dy in range(footprint.sy)
        for dz in range(footprint.sz)
    ]
    slots = [
        HatchSlot(offset=CellCoord(x=o[0], y=o[1], z=o[2]), kinds=("InputBus",)) for o in offsets
    ]
    solver = {
        (origin.x + c[0], origin.y + c[1], origin.z + c[2])
        for c in (rotated_slot(o, footprint, orientation) for o in offsets)
    }
    assert set(hatch_cells(origin, footprint, orientation, slots)) == solver


@pytest.mark.parametrize(("name", "doc"), _controller_docs())
def test_oracle_every_controllers_slots_land_inside_its_own_body(
    name: str, doc: MultiblockDoc
) -> None:
    """Every recorded hatch slot, at every facing, must land on a cell of its own machine.

    The dump measures a slot from the controller block while the solver anchors it on the
    minimum corner, so the two differ by a translation that has to be applied identically to the
    body and to the slots. This is the check that would catch them drifting apart, across all 208
    controllers rather than on generated boxes.
    """
    for variant in doc.variants:
        if not variant.hatch_slots:
            continue
        size = CellBox(sx=variant.bbox[0], sy=variant.bbox[1], sz=variant.bbox[2])
        min_corner = [min(b.d[i] for b in variant.blocks) for i in range(3)]
        slots = [
            HatchSlot(
                offset=CellCoord(
                    x=s.d[0] - min_corner[0], y=s.d[1] - min_corner[1], z=s.d[2] - min_corner[2]
                ),
                kinds=tuple(s.kinds),
            )
            for s in variant.hatch_slots
        ]
        origin = CellCoord(x=0, y=0, z=0)
        for facing in HORIZONTAL_FACINGS_ORDERED:
            body = body_cells(origin, size, facing)
            placed = hatch_cells(origin, size, facing, slots)
            assert len(placed) == len(slots), f"{name} {facing.value}: two slots collapsed"
            assert set(placed) <= body, f"{name} {facing.value}: a slot fell outside its machine"


@given(footprint=_FOOTPRINTS, orientation=_FACINGS)
def test_rotated_slot_is_injective(footprint: CellBox, orientation: Facing) -> None:
    # Two slots must never collapse onto one cell, or two hatches would claim the same casing.
    offsets = [
        (dx, dy, dz)
        for dx in range(footprint.sx)
        for dy in range(footprint.sy)
        for dz in range(footprint.sz)
    ]
    assert len({rotated_slot(o, footprint, orientation) for o in offsets}) == len(offsets)


def _auto_output_faces_by_cells(
    source_origin: CellCoord,
    source_footprint: CellBox,
    source_front: Facing,
    target_origin: CellCoord,
    target_footprint: CellBox,
    target_front: Facing,
) -> tuple[Facing, Facing] | None:
    """The cell-set formulation of :func:`auto_output_faces`: expand both bodies, then look for a
    source cell whose neighbour across the face is a target cell.

    This is what the function did before issue #110 turned it into box arithmetic. It is the
    definition the box test has to reproduce - obviously correct, and far too slow to ship (it cost
    70% of a solve), which is exactly the shape of thing that belongs in a test as the oracle.
    """
    source_cells = set(occupied_cells(source_origin, source_footprint, source_front))
    target_cells = set(occupied_cells(target_origin, target_footprint, target_front))
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_front:
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_front:
            continue
        if any((x + dx, y + dy, z + dz) in target_cells for x, y, z in source_cells):
            return face, opposite
    return None


# Deliberately tighter than _ORIGINS: two bodies drawn from +-8 with extents up to 6 are rarely
# touching, and a property test that only ever exercises the "no" answer proves nothing about the
# face it picks. At +-4 both outcomes come up constantly.
_NEAR_ORIGINS = st.builds(
    CellCoord,
    x=st.integers(min_value=-4, max_value=4),
    y=st.integers(min_value=-4, max_value=4),
    z=st.integers(min_value=-4, max_value=4),
)


@given(
    source_origin=_NEAR_ORIGINS,
    source_footprint=_FOOTPRINTS,
    source_front=_FACINGS,
    target_origin=_NEAR_ORIGINS,
    target_footprint=_FOOTPRINTS,
    target_front=_FACINGS,
)
def test_auto_output_faces_matches_the_cell_set_formulation(
    source_origin: CellCoord,
    source_footprint: CellBox,
    source_front: Facing,
    target_origin: CellCoord,
    target_footprint: CellBox,
    target_front: Facing,
) -> None:
    args = (
        source_origin,
        source_footprint,
        source_front,
        target_origin,
        target_footprint,
        target_front,
    )
    assert auto_output_faces(*args) == _auto_output_faces_by_cells(*args)


def test_auto_output_faces_matches_the_cell_sets_over_a_whole_neighbourhood() -> None:
    """The same equivalence swept exhaustively, so the corners hypothesis samples are all covered.

    A 7x7x7 grid of relative positions covers every way two non-cubic bodies can miss, touch on a
    face, meet only along an edge or a corner (which is NOT an auto-output), or overlap - at all
    sixteen facing pairs. The counts are asserted so the sweep cannot quietly degenerate into one
    answer, which is how an equivalence test stops testing anything.
    """
    source_footprint = CellBox(sx=2, sy=1, sz=3)
    target_footprint = CellBox(sx=3, sy=2, sz=1)
    origin = CellCoord(x=0, y=0, z=0)
    hits = misses = 0
    for source_front in HORIZONTAL_FACINGS_ORDERED:
        for target_front in HORIZONTAL_FACINGS_ORDERED:
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    for dz in range(-3, 4):
                        target_origin = CellCoord(x=dx, y=dy, z=dz)
                        args = (
                            origin,
                            source_footprint,
                            source_front,
                            target_origin,
                            target_footprint,
                            target_front,
                        )
                        got = auto_output_faces(*args)
                        assert got == _auto_output_faces_by_cells(*args), args
                        if got is None:
                            misses += 1
                        else:
                            hits += 1
    assert hits > 0
    assert misses > 0


# Small enough that a body drawn from _NEAR_ORIGINS lands out of bounds about as often as in, so
# the equivalence below is exercised on both answers rather than on "no" over and over.
_REGIONS = st.builds(
    CellBox,
    sx=st.integers(min_value=1, max_value=8),
    sy=st.integers(min_value=1, max_value=8),
    sz=st.integers(min_value=1, max_value=8),
)


@given(origin=_NEAR_ORIGINS, footprint=_FOOTPRINTS, orientation=_FACINGS, region=_REGIONS)
def test_box_in_region_matches_the_cell_walk(
    origin: CellCoord, footprint: CellBox, orientation: Facing, region: CellBox
) -> None:
    """``box_in_region`` is the corner test; the definition is every cell being in bounds.

    Placement leans on the two being the same predicate: it now rejects a candidate on the box and
    never expands it, so a box test that were merely *nearly* right would silently drop legal
    placements (or, worse, keep bodies that hang out of the region) with nothing to catch it.
    """
    assert box_in_region(origin, footprint, orientation, region) == all(
        in_region(c, region) for c in occupied_cells(origin, footprint, orientation)
    )
