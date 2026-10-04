"""placement.groups - which parallel copies move as one column, and how a column is laid.

A group is a plan node's single-block copies (``node#1`` .. ``node#N``); the placer moves it as one
rigid column laid back to front, each member's front against the previous member's back. These pin
which sets qualify, the member order, and the column's geometry against ``FACE_DELTAS``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    Machine,
    Port,
    RelativeFace,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, box_cells
from gtnh_solver.placement.groups import column_offsets, column_size, parallel_groups
from tests._helpers import power_source

_HORIZONTAL = [Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST]


def _machine(
    mid: str,
    *,
    type_: str = "Ore Washer",
    footprint: CellBox | None = None,
    orientations: list[Facing] | None = None,
    pinned: bool = False,
    outside: bool = False,
) -> Machine:
    port = Port(
        id="in",
        commodity=Commodity.ITEM,
        direction=IODirection.INPUT,
        faces=(RelativeFace.BACK,) if pinned else None,
    )
    return Machine(
        id=mid,
        type=type_,
        footprint=footprint if footprint is not None else CellBox(),
        voltage_tier="LV",
        orientation_options=orientations if orientations is not None else list(_HORIZONTAL),
        faces=FaceSpec(ports=[port]),
        outside_front=outside,
    )


def _groups(*machines: Machine) -> tuple[tuple[str, ...], ...]:
    problem = InputIR(bounding_region=CellBox(sx=8, sy=2, sz=8), machines=list(machines), nets=[])
    return parallel_groups(problem)


def test_members_are_ordered_by_their_number_not_their_string() -> None:
    # As strings "#10" sorts before "#2"; the column's head is #1 and its tail the highest number.
    ids = ["w#10", "w#2", "w#1", "w#3", "w#4", "w#5", "w#6", "w#7", "w#8", "w#9"]
    assert _groups(*(_machine(mid) for mid in ids)) == (tuple(f"w#{k}" for k in range(1, 11)),)


def test_each_node_is_its_own_group_in_the_order_the_machines_list_them() -> None:
    machines = [_machine("b#1"), _machine("a#1"), _machine("b#2"), _machine("a#2"), _machine("c")]
    assert _groups(*machines) == (("b#1", "b#2"), ("a#1", "a#2"))


def test_a_lone_copy_a_bare_id_or_a_non_numeric_suffix_is_no_group() -> None:
    assert _groups(_machine("a#1"), _machine("b"), _machine("c")) == ()
    assert _groups(_machine("a#x"), _machine("a#y")) == ()
    assert _groups(_machine("#1"), _machine("#2")) == ()  # no node id before the suffix
    assert _groups(_machine("a#1²"), _machine("a#2²")) == ()  # digits, but not ascii ones


def test_power_sources_are_never_grouped() -> None:
    # A split tier's sources share the suffix (power-source:MV#2), and a source's front is its feed
    # face, which has to stay on a region wall.
    sources = [power_source(f"power-source:MV#{k}", orientations=_HORIZONTAL) for k in (1, 2)]
    assert _groups(*sources) == ()


def test_a_set_that_cannot_stand_in_a_column_is_left_alone() -> None:
    cases = {
        "an outside front": [_machine("c#1", outside=True), _machine("c#2", outside=True)],
        "a pinned port": [_machine("f#1", pinned=True), _machine("f#2", pinned=True)],
        "a multiblock": [
            _machine("m#1", footprint=CellBox(sx=3, sy=3, sz=3)),
            _machine("m#2", footprint=CellBox(sx=3, sy=3, sz=3)),
        ],
        "two types": [_machine("t#1"), _machine("t#2", type_="Macerator")],
        "two facing sets": [_machine("o#1"), _machine("o#2", orientations=[Facing.NORTH])],
        "one odd member": [_machine("x#1"), _machine("x#2"), _machine("x#3", pinned=True)],
    }
    for name, machines in cases.items():
        assert _groups(*machines) == (), name


@given(n=st.integers(min_value=1, max_value=16), facing=st.sampled_from(_HORIZONTAL))
def test_a_column_is_its_box_laid_back_to_front(n: int, facing: Facing) -> None:
    offsets = column_offsets(n, facing)
    cells = set(offsets)
    assert len(offsets) == n
    assert cells == set(box_cells((0, 0, 0), column_size(n, facing)))
    fx, fy, fz = FACE_DELTAS[facing]
    bx, by, bz = FACE_DELTAS[OPPOSITE_FACE[facing]]
    for j in range(1, n):
        x, y, z = offsets[j]
        assert (x + fx, y + fy, z + fz) == offsets[j - 1], "a front covers the previous back"
    x, y, z = offsets[0]
    assert (x + fx, y + fy, z + fz) not in cells, "the head's front is the column's free one"
    x, y, z = offsets[-1]
    assert (x + bx, y + by, z + bz) not in cells, "the tail's back is free"


def test_a_column_runs_along_the_axis_it_faces() -> None:
    assert column_offsets(3, Facing.NORTH) == ((0, 0, 0), (0, 0, 1), (0, 0, 2))
    assert column_offsets(3, Facing.SOUTH) == ((0, 0, 2), (0, 0, 1), (0, 0, 0))
    assert column_offsets(3, Facing.WEST) == ((0, 0, 0), (1, 0, 0), (2, 0, 0))
    assert column_offsets(3, Facing.EAST) == ((2, 0, 0), (1, 0, 0), (0, 0, 0))
    assert column_size(3, Facing.SOUTH) == (1, 1, 3)
    assert column_size(3, Facing.EAST) == (3, 1, 1)


@pytest.mark.parametrize("facing", [Facing.UP, Facing.DOWN])
def test_a_column_faces_a_horizontal_direction(facing: Facing) -> None:
    with pytest.raises(ValueError, match="horizontal"):
        column_offsets(2, facing)
    with pytest.raises(ValueError, match="horizontal"):
        column_size(2, facing)
