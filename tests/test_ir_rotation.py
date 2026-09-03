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
   must not drift, so the swap is checked against a from-scratch rotate-and-re-anchor.
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
