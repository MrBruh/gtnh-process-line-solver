"""solver.trace - what each attempt of an optimizing solve did, for experiments on the search.

``solve(trace=callback)`` hands ``callback`` one record per event of the optimized path, in this
order, and ``gtnh-solve --trace FILE`` writes them as JSON lines (:func:`trace_json`)::

    StartTrace          the annealer's start, gated and routed as an attempt would be
    AttemptTrace  x N   each attempt, in grid order: its anneal (placement.trace), then the
                        crowding gate's verdict or the routed result and its quality key
    SolveTrace          which path the solve's layout came from, and its key

Off unless asked for, and read-only: a traced solve returns exactly the layout an untraced one
does (a test pins it). Records are built where the attempt runs, so they come back from a process
pool with the attempt's result and reach the callback in grid order, whichever process ran what.
The fast path lays no attempts, so a ``--fast`` solve that keeps its constructive layout writes no
records. Measured with this (as an instrumented branch): docs/experiments/initial-placement.md.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import ClassVar, Literal

from gtnh_solver.placement.trace import AnnealTrace

#: A routed layout's quality key (``solver._structure.structure_quality``): the objective's metric
#: plus route cells, the metric, the other metric. Smaller is better.
QualityKey = tuple[int, int, int]

#: What became of an attempt's placement (:class:`AttemptTrace`).
AttemptOutcome = Literal["infeasible", "gated", "routed"]

#: Which path a solve's layout came from (:class:`SolveTrace`).
SolvePath = Literal["valid", "fast", "partial", "gated", "infeasible"]


@dataclass(frozen=True, slots=True)
class StartTrace:
    """The annealer's start (``place(lattice=True)``) as it stands, before any walk.

    ``gated`` is how many machines the crowding gate names (0 when it passes); a gated start is not
    routed, so ``status``, ``failed_nets`` and ``key`` stay None. ``ok`` is False when the machines
    do not fit the region at all.
    """

    KIND: ClassVar[str] = "start"
    ok: bool
    gated: int = 0
    status: str | None = None
    failed_nets: int | None = None
    key: QualityKey | None = None


@dataclass(frozen=True, slots=True)
class AttemptTrace:
    """One attempt of the grid: its anneal, then what became of the placement.

    ``outcome`` is ``infeasible`` (the machines do not fit the region), ``gated`` (the crowding gate
    named ``gated`` machines, so it was never routed) or ``routed`` (``status``, ``failed_nets`` and
    ``key`` say how it came out).
    """

    KIND: ClassVar[str] = "attempt"
    seed: int
    mode: str
    outcome: AttemptOutcome
    anneal: AnnealTrace | None = None
    gated: int = 0
    status: str | None = None
    failed_nets: int | None = None
    key: QualityKey | None = None


@dataclass(frozen=True, slots=True)
class SolveTrace:
    """Where the solve's layout came from (``solver.core._search``).

    ``path`` is ``valid`` (the best VALID attempt or bank-column layout), ``fast`` (no attempt was
    VALID but the fast path's layout is), ``partial`` (the attempt that left the fewest nets
    unrouted; ``failed_nets`` says how many), ``gated`` (every attempt was gated, so the first gated
    placement was routed anyway) or ``infeasible`` (the machines do not fit the region).
    """

    KIND: ClassVar[str] = "solve"
    path: SolvePath
    status: str
    rounds: int
    failed_nets: int | None = None
    key: QualityKey | None = None


TraceRecord = StartTrace | AttemptTrace | SolveTrace


def trace_json(record: TraceRecord) -> str:
    """``record`` as one line of JSON, its kind first: ``{"kind": "attempt", ...}``."""
    return json.dumps({"kind": record.KIND, **asdict(record)})
