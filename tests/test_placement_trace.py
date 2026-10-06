"""placement.trace - what one anneal did to its start (``optimize_placement(trace=True)``).

The trace is for experiments on the search, so two things matter: it must never change what the
anneal returns (an experiment that perturbs what it measures measures nothing), and its numbers
must describe the walk that actually ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import Facing
from gtnh_solver.ir.geometry import Pose, Size
from gtnh_solver.placement import Objective, optimize_placement, place
from gtnh_solver.placement.search import _MAX_ITERS, _MIN_ITERS, _PER_MACHINE
from gtnh_solver.placement.trace import (
    CHECKPOINTS,
    Checkpoint,
    Extent,
    anneal_trace,
    checkpoint_iterations,
    extent,
    kendall_tau,
)
from tests._helpers import hub_line, property_examples

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _unit(_: Pose) -> Size:
    return (1, 1, 1)


# ------------------------------------------------------------------------------ the helpers


def test_kendall_tau_reads_a_kept_order_as_one_and_a_mirrored_one_as_minus_one() -> None:
    assert kendall_tau([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert kendall_tau([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_kendall_tau_needs_two_pairs() -> None:
    assert kendall_tau([], []) is None
    assert kendall_tau([1.0], [2.0]) is None


def test_kendall_tau_counts_a_tie_as_neither_kept_nor_reordered() -> None:
    # Two machines side by side in a row before the walk: their x order is not an order.
    assert kendall_tau([1, 1, 2], [5, 6, 7]) == pytest.approx(2 / 3)


@settings(max_examples=property_examples(200), deadline=None)
@given(st.lists(st.tuples(st.integers(-20, 20), st.integers(-20, 20)), min_size=2, max_size=12))
def test_kendall_tau_is_symmetric_and_bounded(pairs: list[tuple[int, int]]) -> None:
    a, b = [p[0] for p in pairs], [p[1] for p in pairs]
    tau = kendall_tau(a, b)
    assert tau is not None
    assert -1.0 <= tau <= 1.0
    assert tau == kendall_tau(b, a)


def test_extent_spans_the_cells_and_floors_x_by_z() -> None:
    assert extent([(0, 0, 0), (3, 1, 0), (1, 0, 4)]) == Extent(sx=4, sy=2, sz=5, floor=20)


def test_checkpoints_split_the_walk_into_tenths() -> None:
    reads = checkpoint_iterations(250)
    assert len(reads) == CHECKPOINTS
    assert max(reads) == 250
    assert min(reads) == 25


def test_a_walk_shorter_than_ten_iterations_reads_every_step_it_has() -> None:
    assert checkpoint_iterations(4) == frozenset({1, 2, 3, 4})


def test_a_walk_that_moved_nothing_reads_as_unmoved_and_in_order() -> None:
    poses = [
        Pose("a", (0, 0, 0), Facing.NORTH),
        Pose("b", (2, 0, 1), Facing.EAST),
        Pose("c", (4, 0, 3), Facing.NORTH),
    ]
    trace = anneal_trace(
        seed=0,
        objective="footprint",
        iterations=10,
        accepted=0,
        start=poses,
        chosen=poses,
        size_of=_unit,
        costs=(5.0, 5.0, 5.0),
        chosen_is_best=True,
        checkpoints=[Checkpoint(10, 5.0, 5.0)],
    )
    assert (trace.moved_mean, trace.moved_max, trace.unmoved, trace.turned) == (0, 0, 1, 0)
    assert (trace.tau_x, trace.tau_z) == (1.0, 1.0)
    assert trace.start_extent == trace.chosen_extent == Extent(sx=5, sy=1, sz=4, floor=20)


def test_moves_and_turns_are_counted_per_machine() -> None:
    start = [Pose("a", (0, 0, 0), Facing.NORTH), Pose("b", (3, 0, 0), Facing.NORTH)]
    chosen = [Pose("a", (0, 0, 0), Facing.SOUTH), Pose("b", (1, 0, 2), Facing.NORTH)]
    trace = anneal_trace(
        seed=0,
        objective="footprint",
        iterations=1,
        accepted=1,
        start=start,
        chosen=chosen,
        size_of=_unit,
        costs=(9.0, 4.0, 4.0),
        chosen_is_best=True,
        checkpoints=[],
    )
    assert (trace.moved_mean, trace.moved_max) == (2.0, 4)  # a stays, b moves 2 + 2
    assert (trace.unmoved, trace.turned) == (0.5, 0.5)


# ------------------------------------------------------------------------ the traced anneal


@pytest.mark.parametrize("objective", ["footprint", "volume", "balanced"])
@pytest.mark.parametrize("seed", [0, 1, 7])
def test_tracing_never_changes_the_placement(objective: Objective, seed: int) -> None:
    problem = hub_line(4)
    plain = optimize_placement(problem, seed=seed, objective=objective)
    traced = optimize_placement(problem, seed=seed, objective=objective, trace=True)
    assert traced.placements == plain.placements
    assert plain.trace is None
    assert traced.trace is not None


def test_tracing_never_changes_the_placement_of_a_real_line() -> None:
    problem = adapt_file(_SAND)
    plain = optimize_placement(problem, seed=3, max_iterations=250)
    traced = optimize_placement(problem, seed=3, max_iterations=250, trace=True)
    assert traced.placements == plain.placements


def test_the_trace_describes_the_walk_that_ran() -> None:
    problem = hub_line(4)
    trace = optimize_placement(problem, seed=2, trace=True).trace
    assert trace is not None
    n = len(problem.machines)
    assert trace.machines == n
    assert trace.iterations == min(_MAX_ITERS, max(_MIN_ITERS, _PER_MACHINE * n))
    assert 0 <= trace.accepted <= trace.iterations
    # The walk starts at the constructive seed, and its best never costs more than its start.
    start = place(problem, lattice=True).placements
    cells = [(p.cell.x, p.cell.y, p.cell.z) for p in start]
    assert trace.start_extent == extent(cells)
    assert trace.best_cost <= trace.start_cost
    assert trace.best_cost <= trace.chosen_cost
    if trace.chosen_is_best:
        assert trace.chosen_cost == trace.best_cost
    # Read at each tenth, the best cost so far only ever falls, and the last read is the end.
    assert len(trace.checkpoints) == CHECKPOINTS
    assert trace.checkpoints[-1].iteration == trace.iterations
    bests = [c.best_cost for c in trace.checkpoints]
    assert bests == sorted(bests, reverse=True)
    assert trace.checkpoints[-1].best_cost == trace.best_cost


def test_a_capped_walk_traces_its_capped_length() -> None:
    trace = optimize_placement(hub_line(4), seed=0, max_iterations=40, trace=True).trace
    assert trace is not None
    assert trace.iterations == 40
    assert trace.checkpoints[-1].iteration == 40


def test_nothing_to_anneal_traces_nothing() -> None:
    # One machine: the constructive seed is the answer and no walk runs.
    problem = hub_line(0)
    assert len(problem.machines) == 1
    assert optimize_placement(problem, trace=True).trace is None
