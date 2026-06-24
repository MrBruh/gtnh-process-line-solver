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
from gtnh_solver.ir.geometry import in_region
from gtnh_solver.placement import place
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_PLACEMENT_CODES = {
    ViolationCode.MACHINE_OVERLAP,
    ViolationCode.MACHINE_OUT_OF_BOUNDS,
    ViolationCode.MACHINE_ON_RESERVED,
    ViolationCode.BAD_ORIENTATION,
    ViolationCode.PLACEMENT_COUNT_MISMATCH,
    ViolationCode.UNKNOWN_MACHINE,
}


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


@given(
    sx=st.integers(min_value=1, max_value=5),
    sy=st.integers(min_value=1, max_value=3),
    sz=st.integers(min_value=1, max_value=5),
    n=st.integers(min_value=0, max_value=12),
)
def test_place_yields_valid_layout_or_explicit_infeasibility(
    sx: int, sy: int, sz: int, n: int
) -> None:
    problem = _problem([_machine(f"m{i}") for i in range(n)], region=CellBox(sx=sx, sy=sy, sz=sz))
    result = place(problem)

    # With 1x1x1 machines and no reserved cells, first-fit fills one cell each: feasible iff
    # the count fits the region's cell capacity.
    capacity = sx * sy * sz
    assert result.ok is (n <= capacity)

    cells = [(p.cell.x, p.cell.y, p.cell.z) for p in result.placements]
    assert len(cells) == len(set(cells))  # never overlapping
    assert all(in_region(c, problem.bounding_region) for c in cells)  # never out of bounds

    if result.ok:
        assert len(result.placements) == n
        assert _PLACEMENT_CODES.isdisjoint(validate(problem, _as_layout(result.placements)).codes())
    else:
        assert result.infeasibility is not None
