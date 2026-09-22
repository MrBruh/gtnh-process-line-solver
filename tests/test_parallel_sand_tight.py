"""Tight parallel-sand placements the old router could not route, and this one does (#164).

``tests/fixtures/parallel-sand-tight-placements.json`` holds the 23 placements ``solve()`` handed
the router on ``examples/gtnh-parallel-sand.json``, seeds 0 to 7, once the crowding gate stopped
turning away placements whose connections share pipe blocks. The router of the time routed none of
them. Of the 23, 18 failed because ``reserve_power_docks`` held a dock cell per energy port before
any pipe was laid and took a cell an item net needed, 2 because a pipe walled a power dock into a
pocket, and 3 because greedy docking left an item net no path. They are exactly the placements the
negotiated router exists for, so every one must now come out VALID, through the same assembly the
solver runs (router, power router, hatches, validator).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import CellCoord, Facing, InputIR, LayoutStatus, Placement
from gtnh_solver.solver.core import _assemble
from gtnh_solver.validator import validate

_ROOT = Path(__file__).resolve().parents[1]
_PLAN = _ROOT / "examples" / "gtnh-parallel-sand.json"
_CASES: list[dict[str, Any]] = json.loads(
    (_ROOT / "tests" / "fixtures" / "parallel-sand-tight-placements.json").read_text()
)


def _placements(case: dict[str, Any]) -> tuple[Placement, ...]:
    return tuple(
        Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=Facing(facing))
        for mid, x, y, z, facing in case["placements"]
    )


@pytest.fixture(scope="module")
def problem() -> InputIR:
    return adapt_file(_PLAN)


def test_the_fixture_is_the_whole_failure_set() -> None:
    # 23 attempts over seeds 0 to 7, each a full placement of the line's 12 machines.
    assert len(_CASES) == 23
    assert {c["solve_seed"] for c in _CASES} == set(range(8))
    assert all(len(c["placements"]) == 12 for c in _CASES)


@pytest.mark.parametrize(
    "case", _CASES, ids=[f"seed{c['solve_seed']}-attempt{c['attempt_seed']}" for c in _CASES]
)
def test_a_tight_placement_routes_valid(problem: InputIR, case: dict[str, Any]) -> None:
    layout, failed = _assemble(problem, _placements(case), case["attempt_seed"])
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    assert validate(problem, layout).ok
