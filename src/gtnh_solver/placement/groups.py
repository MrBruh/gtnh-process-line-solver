"""placement.groups - a plan node's parallel single blocks, moved as one rigid column.

A plan node that runs on N machines (``machineCount``) becomes N ``Machine``s, ``node#1`` to
``node#N`` (``adapter.core._instance_ids``; the ``#N`` suffix is the documented grouping signal,
``ir/__init__.py``). Moved one at a time, the annealer leaves the copies side by side or scattered,
and every side-by-side contact costs two usable faces, one on each machine. So the placer moves a
node's single-block copies as one rigid unit instead, laid **back to front**: each member's front,
which carries no I/O, is pressed against the previous member's back. A contact then costs one
usable face (a back), and one straight pipe or cable run along the column can serve every member.
It is the shape ``placement.banks`` lays a chain of banks in, and the maintainer's in-game
parallel-sand build::

    -z <- front     [#1][#2][#3]    facing NORTH: member j (0 for #1) at z0 + j
    head #1's front is free; each later member's front covers the previous member's back

Every group is one straight column, however long. A long group may later want another shape (a
folded or a split column); this module is where that would go.
"""

from __future__ import annotations

from gtnh_solver.ir import Facing, InputIR, Machine
from gtnh_solver.ir.geometry import Cell, Size


def parallel_groups(problem: InputIR) -> tuple[tuple[str, ...], ...]:
    """The machine ids of each group that moves as one column, members in instance order.

    A group is two or more machines whose ids differ only in a numeric ``#N`` suffix, and which are
    interchangeable blocks: every one 1x1x1, of the same ``type``, with the same
    ``orientation_options``. A set holding any machine that cannot sit in a column is left alone:
    one whose front faces outside the build (a power source, which shares the suffix as
    ``power-source:MV#2``, or a Crop Manager) has to keep that front on a region wall, and a column
    covers every front but its head's; one with a pinned port (``Port.faces``, an Item Filter) docks
    through faces a column may cover. Members are ordered by the number, since as strings ``#10``
    sorts before ``#2``. Groups come in the order ``problem.machines`` first lists them.
    """
    sets: dict[str, list[tuple[int, Machine]]] = {}
    for machine in problem.machines:
        base, sep, suffix = machine.id.rpartition("#")
        if sep and base and suffix.isascii() and suffix.isdigit():
            sets.setdefault(base, []).append((int(suffix), machine))
    groups: list[tuple[str, ...]] = []
    for members in sets.values():
        first = members[0][1]
        if len(members) >= 2 and all(_columnable(m, first) for _, m in members):
            members.sort(key=lambda member: member[0])
            groups.append(tuple(m.id for _, m in members))
    return tuple(groups)


def _columnable(machine: Machine, first: Machine) -> bool:
    """Whether ``machine`` can stand in a column with ``first``, its set's first member."""
    return (
        (machine.footprint.sx, machine.footprint.sy, machine.footprint.sz) == (1, 1, 1)
        and machine.type == first.type
        and machine.orientation_options == first.orientation_options
        and not machine.fronts_outside
        and all(port.faces is None for port in machine.faces.ports)
    )


#: Each horizontal facing's column axis as ``(x step, z step)`` from the head toward the tail: the
#: way the head's back points (``ir.geometry.FACE_DELTAS``, reversed).
_TAIL_STEP: dict[Facing, tuple[int, int]] = {
    Facing.NORTH: (0, 1),
    Facing.SOUTH: (0, -1),
    Facing.WEST: (1, 0),
    Facing.EAST: (-1, 0),
}


def column_offsets(n: int, facing: Facing) -> tuple[Cell, ...]:
    """Each of ``n`` members' origin from the column's minimum corner, head first.

    The head sits at the end its front points out of, so its front is the column's one free front:
    facing NORTH, member j is at ``(0, 0, j)``; SOUTH ``(0, 0, n - 1 - j)``; WEST ``(j, 0, 0)``;
    EAST ``(n - 1 - j, 0, 0)``. Every later member's front then lands on the cell before it.
    """
    dx, dz = _step(facing)
    x0 = (n - 1) if dx < 0 else 0
    z0 = (n - 1) if dz < 0 else 0
    return tuple((x0 + j * dx, 0, z0 + j * dz) for j in range(n))


def column_size(n: int, facing: Facing) -> Size:
    """The box a column of ``n`` single blocks facing ``facing`` fills: ``(1, 1, n)`` facing north
    or south, ``(n, 1, 1)`` facing east or west."""
    dx, _ = _step(facing)
    return (n, 1, 1) if dx else (1, 1, n)


def _step(facing: Facing) -> tuple[int, int]:
    step = _TAIL_STEP.get(facing)
    if step is None:
        raise ValueError(f"a column faces a horizontal direction, not {facing.value}")
    return step
