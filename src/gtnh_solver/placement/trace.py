"""placement.trace - what one anneal did to the placement it started from.

Asked for with ``optimize_placement(trace=True)``, off otherwise. An experiment on the search needs
to see what each attempt did, not only the layout the solve kept: how far the walk took the start,
whether the start's arrangement survived it, and when the cost stopped falling. The initial-
placement round (docs/experiments/initial-placement.md) measured those with an instrumented branch;
this is that instrumentation, kept so the next experiment does not rebuild it.

**It reads the walk and never steers it.** No random draw, no solver state: a traced anneal takes
exactly the path an untraced one does and returns the same placement (a test pins it). Everything
here is computed after the walk, apart from the cost read at each tenth of it.

The record, per anneal::

    start ---- walk (cost read at each tenth) ----> chosen (cheapest the crowding gate passes)
      |                                               |
      +--> start_extent, start_cost                   +--> chosen_extent, chosen_cost
                         moved_*, unmoved, turned, tau_x, tau_z  <-- start vs chosen, per machine

``tau_x`` / ``tau_z`` are Kendall's tau between the machines' order along x (z) in the start and in
the chosen placement: 1 when the walk kept the start's arrangement on that axis, about 0 when it
reshuffled it, -1 when it mirrored it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from gtnh_solver.ir.geometry import Cell, Pose, Size

#: How many times a traced walk reads its cost: at each tenth of its iterations.
CHECKPOINTS = 10


@dataclass(frozen=True, slots=True)
class Extent:
    """The bounding box of a placement's machines: its spans and its floor area (x span by z)."""

    sx: int
    sy: int
    sz: int
    floor: int


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """The walk's current and best cost after ``iteration`` iterations."""

    iteration: int
    current_cost: float
    best_cost: float


@dataclass(frozen=True, slots=True)
class AnnealTrace:
    """One anneal, start to the placement it returned (module docstring).

    ``chosen`` is the state :func:`~gtnh_solver.placement.optimize_placement` returned: the cheapest
    one the crowding gate passes, which is not always the cheapest one seen (``best_cost``);
    ``chosen_is_best`` says whether they are the same state. ``moved_mean`` / ``moved_max`` are
    each machine's origin displacement in cells (Manhattan), ``unmoved`` / ``turned`` the share
    of machines that kept their cell / changed their facing.
    """

    seed: int
    objective: str
    machines: int
    iterations: int
    accepted: int
    start_cost: float
    best_cost: float
    chosen_cost: float
    chosen_is_best: bool
    start_extent: Extent
    chosen_extent: Extent
    moved_mean: float
    moved_max: int
    unmoved: float
    turned: float
    tau_x: float | None
    tau_z: float | None
    checkpoints: tuple[Checkpoint, ...]


def checkpoint_iterations(iterations: int) -> frozenset[int]:
    """The iteration counts (1-based) after which a walk of ``iterations`` reads its cost."""
    return frozenset(max(1, iterations * k // CHECKPOINTS) for k in range(1, CHECKPOINTS + 1))


def extent(cells: Iterable[Cell]) -> Extent:
    """The :class:`Extent` of ``cells``; raises ``ValueError`` when there are none."""
    xs, ys, zs = zip(*cells, strict=True)
    sx, sy, sz = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, max(zs) - min(zs) + 1
    return Extent(sx=sx, sy=sy, sz=sz, floor=sx * sz)


def kendall_tau(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Kendall's tau-a of two paired sequences, or None with fewer than two pairs.

    Concordant pairs minus discordant, over all pairs; a pair tied in either sequence counts as
    neither, so two machines side by side in a row do not count as reordered.
    """
    n = len(a)
    if n < 2:
        return None
    score = 0
    for i in range(n):
        for j in range(i + 1, n):
            da, db = a[i] - a[j], b[i] - b[j]
            if da and db:
                score += 1 if (da > 0) == (db > 0) else -1
    return score / (n * (n - 1) // 2)


def anneal_trace(
    *,
    seed: int,
    objective: str,
    iterations: int,
    accepted: int,
    start: Sequence[Pose],
    chosen: Sequence[Pose],
    size_of: Callable[[Pose], Size],
    costs: tuple[float, float, float],
    chosen_is_best: bool,
    checkpoints: Sequence[Checkpoint],
) -> AnnealTrace:
    """Build the :class:`AnnealTrace` of a walk from ``start`` to ``chosen``.

    ``size_of`` is a pose's box as it sits in the world (rotated); ``costs`` is the walk's
    ``(start, best, chosen)`` cost. Every machine of ``chosen`` must be in ``start``.
    """
    before = {p.machine_id: p for p in start}
    moved = [
        sum(abs(u - v) for u, v in zip(before[p.machine_id].cell, p.cell, strict=True))
        for p in chosen
    ]
    turned = sum(before[p.machine_id].orientation != p.orientation for p in chosen)
    ids = sorted(before)
    after = {p.machine_id: p for p in chosen}
    c0 = [_centre(before[i], size_of) for i in ids]
    c1 = [_centre(after[i], size_of) for i in ids]
    start_cost, best_cost, chosen_cost = costs
    return AnnealTrace(
        seed=seed,
        objective=objective,
        machines=len(start),
        iterations=iterations,
        accepted=accepted,
        start_cost=start_cost,
        best_cost=best_cost,
        chosen_cost=chosen_cost,
        chosen_is_best=chosen_is_best,
        start_extent=extent(_cells(start, size_of)),
        chosen_extent=extent(_cells(chosen, size_of)),
        moved_mean=sum(moved) / len(moved),
        moved_max=max(moved),
        unmoved=sum(d == 0 for d in moved) / len(moved),
        turned=turned / len(chosen),
        tau_x=kendall_tau([c[0] for c in c0], [c[0] for c in c1]),
        tau_z=kendall_tau([c[1] for c in c0], [c[1] for c in c1]),
        checkpoints=tuple(checkpoints),
    )


def _centre(pose: Pose, size_of: Callable[[Pose], Size]) -> tuple[float, float]:
    """A pose's box centre on the floor plane, ``(x, z)``."""
    sx, _, sz = size_of(pose)
    return pose.cell[0] + sx / 2, pose.cell[2] + sz / 2


def _cells(poses: Sequence[Pose], size_of: Callable[[Pose], Size]) -> Iterable[Cell]:
    """The two opposite corner cells of every pose's box: all :func:`extent` needs."""
    for p in poses:
        x, y, z = p.cell
        sx, sy, sz = size_of(p)
        yield x, y, z
        yield x + sx - 1, y + sy - 1, z + sz - 1
