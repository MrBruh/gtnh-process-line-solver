"""placement._diag - SPIKE ONLY: initial-placement diagnostics, written to stderr.

Never merged. Each record is one stderr line, ``DIAG {json}``, so a lab run's log carries it and
``GET /api/agent/runs/<id>/log?grep=^DIAG`` pulls it back. Nothing here reads or advances an RNG or
touches solver state: a solve with these records must lay exactly the layout it lays without them.

Records (``kind``):

- ``anneal``: one per attempt, from ``optimize_placement`` - the start and the state it returned
  (cost, extent), how far the machines moved, whether the start's arrangement survived (Kendall tau
  of the machines' x and z order), and the cost along the walk.
- ``attempt``: one per attempt, from ``solver.core._attempt`` - gated, routed status, failed nets,
  and the routed quality key.
- ``start``: once per solve at seed 0 - the start itself, gated and routed as an attempt would be.
- ``solve``: once per solve - which path the result came from and its key.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from gtnh_solver.ir import Facing
from gtnh_solver.ir.geometry import Cell, Pose, Size


class _Sized(Protocol):
    @property
    def sizes(self) -> Mapping[Facing, Size]: ...


def emit(kind: str, **fields: object) -> None:
    """Write one ``DIAG`` record to stderr."""
    print(
        "DIAG " + json.dumps({"kind": kind, **fields}, sort_keys=True), file=sys.stderr, flush=True
    )


def extent(cells: Iterable[Cell]) -> dict[str, int]:
    """The x, y, z spans of ``cells`` and their floor area (x span times z span)."""
    xs, ys, zs = zip(*cells, strict=True)
    sx, sy, sz = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, max(zs) - min(zs) + 1
    return {"sx": sx, "sy": sy, "sz": sz, "floor": sx * sz}


def _centers(poses: Sequence[Pose], bodies: Mapping[str, _Sized]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for p in poses:
        sx, _, sz = bodies[p.machine_id].sizes[p.orientation]
        out[p.machine_id] = (p.cell[0] + sx / 2, p.cell[2] + sz / 2)
    return out


def _tau(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Kendall tau-a of two paired sequences; None when there are fewer than two pairs."""
    n = len(a)
    if n < 2:
        return None
    score = 0
    for i in range(n):
        for j in range(i + 1, n):
            da, db = a[i] - a[j], b[i] - b[j]
            if da and db:
                score += 1 if (da > 0) == (db > 0) else -1
    return round(score / (n * (n - 1) / 2), 4)


def movement(
    start: Sequence[Pose], end: Sequence[Pose], bodies: Mapping[str, _Sized]
) -> dict[str, object]:
    """How far the machines moved from ``start`` to ``end``, and whether their order survived."""
    before = {p.machine_id: p for p in start}
    dist = [
        sum(abs(u - v) for u, v in zip(before[p.machine_id].cell, p.cell, strict=True)) for p in end
    ]
    turned = sum(before[p.machine_id].orientation != p.orientation for p in end)
    c0, c1 = _centers(start, bodies), _centers(end, bodies)
    ids = sorted(c0)
    return {
        "moved_mean": round(sum(dist) / len(dist), 3),
        "moved_max": max(dist),
        "unmoved": round(sum(d == 0 for d in dist) / len(dist), 4),
        "turned": round(turned / len(end), 4),
        "tau_x": _tau([c0[i][0] for i in ids], [c1[i][0] for i in ids]),
        "tau_z": _tau([c0[i][1] for i in ids], [c1[i][1] for i in ids]),
    }
