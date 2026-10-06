"""solver.trace - the records ``solve(trace=...)`` hands out, and that they change nothing.

A trace is read by experiments on the search, so it has to be the solve that ran: the same layout
as an untraced solve, one record per attempt in grid order (from a process pool too), and a last
record naming the path the layout came from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import CellBox, InputIR, LayoutResult, LayoutStatus
from gtnh_solver.placement import bank_columns
from gtnh_solver.solver import core as solver_core
from gtnh_solver.solver import solve
from gtnh_solver.solver.trace import (
    AttemptTrace,
    SolveTrace,
    StartTrace,
    TraceRecord,
    trace_json,
)
from tests._helpers import hub_line

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


@pytest.fixture(scope="module")
def sand() -> InputIR:
    return adapt_file(_SAND)


def _traced(problem: InputIR, **kwargs: Any) -> tuple[LayoutResult, list[TraceRecord]]:
    records: list[TraceRecord] = []
    layout = solve(problem, trace=records.append, **kwargs)
    return layout, records


def _split(
    records: list[TraceRecord],
) -> tuple[StartTrace, list[AttemptTrace], SolveTrace]:
    """The start record, the attempt records and the solve record, asserting that order."""
    start, *middle, end = records
    assert isinstance(start, StartTrace)
    assert isinstance(end, SolveTrace)
    attempts = [a for a in middle if isinstance(a, AttemptTrace)]
    assert len(attempts) == len(middle)
    return start, attempts, end


@pytest.mark.parametrize("effort", ["minimal", "full"])
def test_tracing_never_changes_the_layout(
    sand: InputIR, effort: Literal["minimal", "full"]
) -> None:
    layout, _ = _traced(sand, seed=5, effort=effort)
    assert layout == solve(sand, seed=5, effort=effort)


def test_a_full_solve_traces_its_start_every_attempt_in_grid_order_then_its_path(
    sand: InputIR,
) -> None:
    layout, records = _traced(sand, seed=5, effort="full")
    start, attempts, end = _split(records)
    budget = solver_core._BUDGETS["full"]
    assert [a.seed for a in attempts] == list(range(5, 5 + budget.attempts))
    assert all(a.mode == "footprint" for a in attempts)
    for a in attempts:
        assert a.anneal is not None
        assert a.anneal.seed == a.seed
    assert start.ok
    assert end.rounds == 1
    assert end.path == "valid"
    assert end.status == layout.status.value
    assert end.key == solver_core._quality(sand, layout, "footprint")
    # The layout is the best routed attempt: sand has no bank-column candidate to beat them.
    assert bank_columns(sand) is None
    routed = [a.key for a in attempts if a.status == "valid" and a.key is not None]
    assert end.key == min(routed)


def test_every_round_traces_its_own_attempts(sand: InputIR) -> None:
    _, records = _traced(sand, seed=0, effort="minimal", rounds=3)
    _, attempts, end = _split(records)
    assert [a.seed for a in attempts] == [0, 1, 2]
    assert end.rounds == 3


def test_attempts_traced_in_a_process_pool_come_back_the_same(
    sand: InputIR, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)  # any attempt is slow enough
    pooled_layout, pooled = _traced(sand, seed=1, jobs=2, effort="full")
    alone_layout, alone = _traced(sand, seed=1, jobs=1, effort="full")
    assert pooled_layout == alone_layout
    assert pooled == alone


def test_a_line_every_attempt_is_gated_on_traces_the_gated_path() -> None:
    # Six connections on a single block: no placement docks them all, so the gate turns every
    # attempt away and the solve routes the first gated placement anyway.
    layout, records = _traced(hub_line(6), effort="minimal")
    start, attempts, end = _split(records)
    assert start.gated > 0
    assert start.status is None
    assert [a.outcome for a in attempts] == ["gated"]
    assert attempts[0].gated > 0
    assert attempts[0].key is None
    assert end.path == "gated"
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert end.status == LayoutStatus.PARTIAL_INVALID.value


def test_machines_that_do_not_fit_trace_the_infeasible_path() -> None:
    layout, records = _traced(hub_line(2, region=CellBox(sx=1, sy=1, sz=1)), effort="full")
    start, attempts, end = _split(records)
    assert not start.ok
    assert [a.outcome for a in attempts] == ["infeasible"]  # the first attempt ends the search
    assert attempts[0].anneal is None
    assert end.path == "infeasible"
    assert end.key is None
    assert layout.status is LayoutStatus.INFEASIBLE


def test_the_fast_path_traces_nothing(sand: InputIR) -> None:
    _, records = _traced(sand, optimize=False)
    assert records == []


def test_a_record_is_one_line_of_json_led_by_its_kind(sand: InputIR) -> None:
    _, records = _traced(sand, effort="minimal")
    lines = [trace_json(r) for r in records]
    assert all("\n" not in line for line in lines)
    parsed = [json.loads(line) for line in lines]
    assert [p["kind"] for p in parsed] == ["start", "attempt", "solve"]
    assert next(iter(parsed[0])) == "kind"
    anneal = parsed[1]["anneal"]
    assert set(anneal["start_extent"]) == {"sx", "sy", "sz", "floor"}
    assert len(anneal["checkpoints"]) == 10
