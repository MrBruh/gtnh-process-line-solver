"""Shared cell-grid primitives for the routers (generic + power).

Obstacle building, terminal docking on a usable (non-front) machine face, and A* between cells
all live here so ``router.core`` (item/fluid) and ``router.power`` route over the *same* grid
model with one implementation. The conventions (front face = placement orientation carries no
I/O; machine + reserved cells are obstacles; the validator independently re-checks every
terminal) are unchanged from the original crude router.
"""

from __future__ import annotations

import heapq
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Facing,
    HatchSlot,
    InputIR,
    Machine,
    Placement,
    Route,
    Terminal,
)
from gtnh_solver.ir.geometry import (
    FACE_DELTAS,
    FACE_OFFSETS,
    Cell,
    in_region,
    occupied_cells,
    rotated_slot,
)

# Enumeration order for the non-front faces (the front face == placement orientation is skipped
# at runtime). Both routers weigh every face against the route, so this fixes only the order
# candidates are listed in - it is a determinism aid, not a preference.
FACE_ORDER = (Facing.SOUTH, Facing.NORTH, Facing.EAST, Facing.WEST, Facing.UP, Facing.DOWN)
NEIGHBORS = FACE_OFFSETS  # the six face-adjacent unit steps A* expands into
_UNREACHABLE = 1 << 30


def obstacle_cells(
    problem: InputIR, placements: Sequence[Placement], machines: dict[str, Machine]
) -> set[Cell]:
    """Cells a route must avoid: reserved cells plus every placed machine's body."""
    obstacles: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    for placement in placements:
        machine = machines.get(placement.machine_id)
        if machine is not None:
            obstacles.update(
                occupied_cells(placement.cell, machine.footprint, placement.orientation)
            )
    return obstacles


def body_cell(terminal: Terminal) -> Cell:
    """The casing cell whose hatch a terminal docks against: one step back along its face.

    The relationship ``PlacedHatch`` documents, read the other way. A terminal sits one cell
    *outside* the machine on ``face``, so the hatch it serves occupies the cell behind it. One
    casing cell is one block, so this is also the resource two hatches on one machine compete for.
    """
    dx, dy, dz = FACE_DELTAS[terminal.face]
    return (terminal.cell.x - dx, terminal.cell.y - dy, terminal.cell.z - dz)


def claim_key(terminal: Terminal, machine: Machine) -> Cell:
    """The cell two connections on one machine may not both hold - the unit they contend over.

    For a **multiblock** that is the casing cell: a hatch IS one block of the structure, so two
    hatches cannot share it even facing two different ways, and a claim on the dock cell alone
    would miss that (one casing cell has up to five free faces).

    For a machine with **no recorded slots** it is the dock cell instead. Such a machine is a
    single block, or one the dump knows nothing about, and a single-block GT machine genuinely
    does take input on one face of that block and output on another. Claiming its one body cell
    would cap it at a single connection, which is a false infeasibility rather than a rule - the
    same "unrecorded means permissive" reasoning as
    :meth:`~gtnh_solver.ir.Machine.hatch_slots_for`.
    """
    return body_cell(terminal) if machine.hatch_slots else terminal.cell.as_tuple()


def claims_by_machine(
    routes: Iterable[Route], machines: Mapping[str, Machine]
) -> dict[str, set[Cell]]:
    """What each machine's already-routed connections hold, per :func:`claim_key`.

    Read straight off the terminals, so it needs no new bookkeeping. The solver hands it to the
    power router: a multiblock's hatch cells are one shared pool, and a cell an input bus stands
    on cannot also hold an energy hatch.
    """
    claimed: dict[str, set[Cell]] = {}
    for route in routes:
        for terminal in route.terminals:
            machine = machines.get(terminal.machine_id)
            if machine is not None:
                claimed.setdefault(terminal.machine_id, set()).add(claim_key(terminal, machine))
    return claimed


def host_cells(
    placement: Placement, machine: Machine, slots: Sequence[HatchSlot] | None
) -> list[Cell]:
    """``slots`` as world cells, ascending; the machine's whole body when ``slots`` is None.

    None means the dump recorded nothing to go on, which is "unknown", not "none": a single-block
    machine, a plan adapted without the physical dataset, and 23 of 208 controllers all land here
    and must keep every body cell as a candidate rather than lose the ability to dock at all.
    """
    origin = placement.cell
    if slots is None:
        return sorted(occupied_cells(origin, machine.footprint, placement.orientation))
    turned = (
        rotated_slot(slot.offset.as_tuple(), machine.footprint, placement.orientation)
        for slot in slots
    )
    return sorted({(origin.x + dx, origin.y + dy, origin.z + dz) for dx, dy, dz in turned})


def _dock_faces(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
    claimed: Collection[Cell] = (),
) -> Iterator[Terminal]:
    """Free cells just outside a hatch-capable casing cell, one Terminal per face+cell.

    The single scan behind :func:`dock_candidates`. A candidate must clear four things:

    - its **host** cell accepts this port's hatch kind
      (:meth:`~gtnh_solver.ir.Machine.hatch_slots_for`, permissive wherever the dump is silent)
      and is not already ``claimed`` by another connection on this machine. What "already held"
      means differs by machine and :func:`claim_key` decides it: a multiblock contends over casing
      cells, a single block over faces;
    - the face is not the machine's front, which carries no I/O;
    - the face is **exposed**: the cell one step out is not another cell of this machine's own
      body. That is what keeps a hatch off an interior slot - 29% of all slots dataset-wide - which
      would be walled inside the structure and could reach nothing;
    - that outward cell is in-region, free of obstacles, and unclaimed by another net.

    Walks ``FACE_ORDER`` (front skipped) and, within each, ascending host cell, deduping a cell
    already yielded from an earlier face so each appears exactly once. Order is deterministic and
    total; no caller may read anything into the *first* yield, which is the ``FACE_ORDER`` tiebreak
    and not a decision.
    """
    body = set(occupied_cells(placement.cell, machine.footprint, placement.orientation))
    slots = machine.hatch_slots_for(port_id)
    hosts = host_cells(placement, machine, slots)
    if slots is not None:  # a multiblock contends over casing cells (see :func:`claim_key`)
        hosts = [c for c in hosts if c not in claimed]
    seen: set[Cell] = set()
    for face in FACE_ORDER:
        if face is placement.orientation:  # front face carries no I/O
            continue
        dx, dy, dz = FACE_DELTAS[face]
        for bx, by, bz in hosts:
            cand = (bx + dx, by + dy, bz + dz)
            if cand in body or cand in seen:  # walled in by its own machine, or already yielded
                continue
            if slots is None and cand in claimed:  # ...a single block contends over faces
                continue
            if not in_region(cand, region) or cand in obstacles or cand in docked:
                continue
            seen.add(cand)
            yield Terminal(
                machine_id=placement.machine_id,
                port_id=port_id,
                face=face,
                cell=CellCoord(x=cand[0], y=cand[1], z=cand[2]),
            )


def dock_candidates(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
    claimed: Collection[Cell] = (),
) -> list[Terminal]:
    """Every free cell outside a hatch-capable, exposed, usable face; one Terminal per face+cell.

    Returning *all* the options is what lets both routers choose a face from where the route has
    to go rather than from a tuple ordering: the power router docks on whichever face yields the
    shortest cable, and the item/fluid router chains its endpoints with multi-goal A*
    (``core._dock_net``). ``claimed`` are the casing cells this machine's other hatches already
    hold. Deterministic order: ``FACE_ORDER``, then ascending host cell.
    """
    return list(_dock_faces(port_id, placement, machine, obstacles, docked, region, claimed))


def astar(
    start: Cell,
    goal: Cell,
    obstacles: set[Cell],
    region: CellBox,
    cell_cost: Mapping[Cell, float] | None = None,
) -> list[Cell] | None:
    """Cheapest in-bounds, obstacle-free cell path from ``start`` to ``goal``.

    Each hop costs 1 plus the entered cell's ``cell_cost`` (0 where absent), so with no
    ``cell_cost`` this is the plain shortest path. The negotiated-congestion router prices
    contested cells through it: a priced cell is *discouraged*, never blocked - only
    ``obstacles`` are hard. Manhattan distance stays an admissible heuristic because every
    extra cost is non-negative on top of the unit base.
    """
    prices: Mapping[Cell, float] = cell_cost if cell_cost is not None else {}
    heap: list[tuple[float, float, Cell]] = [(float(manhattan(start, goal)), 0.0, start)]
    came_from: dict[Cell, Cell] = {}
    best: dict[Cell, float] = {start: 0.0}
    visited: set[Cell] = set()
    while heap:
        _, g, cur = heapq.heappop(heap)
        if cur == goal:
            return _reconstruct(came_from, cur)
        if cur in visited:
            continue
        visited.add(cur)
        for dx, dy, dz in NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if not in_region(nxt, region) or nxt in obstacles:
                continue
            ng = g + 1 + prices.get(nxt, 0.0)
            if ng < best.get(nxt, _UNREACHABLE):
                best[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(heap, (ng + manhattan(nxt, goal), ng, nxt))
    return None


def astar_multi(
    starts: Collection[Cell], goals: set[Cell], obstacles: set[Cell], region: CellBox
) -> list[Cell] | None:
    """Shortest obstacle-free path from any cell in ``starts`` to any cell in ``goals``.

    Multi-source, multi-goal A* (the heuristic is the Manhattan distance to the nearest goal). The
    power router uses it to dock a cable on whichever usable face gives the shortest run: ``goals``
    are all of a machine's free non-front dock cells, so routing - not a fixed face order - picks
    the terminal. Like :func:`astar`, ``starts`` are seeded at cost 0 even if they lie in
    ``obstacles`` (a leg begins on the previous leg's end cell, already part of the laid trunk).
    Returns the path (``path[0] in starts``, ``path[-1] in goals``), or ``None`` if none is
    reachable. ``goals`` must be non-empty and disjoint from ``starts`` (a zero-length trunk is not
    a valid cable); the caller guarantees this.
    """
    if not goals:
        return None
    heap: list[tuple[int, int, Cell]] = [(_nearest_goal(s, goals), 0, s) for s in starts]
    heapq.heapify(heap)
    came_from: dict[Cell, Cell] = {}
    best: dict[Cell, int] = dict.fromkeys(starts, 0)
    visited: set[Cell] = set()
    while heap:
        _, g, cur = heapq.heappop(heap)
        if cur in goals:
            return _reconstruct(came_from, cur)
        if cur in visited:
            continue
        visited.add(cur)
        for dx, dy, dz in NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if not in_region(nxt, region) or nxt in obstacles:
                continue
            ng = g + 1
            if ng < best.get(nxt, _UNREACHABLE):
                best[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(heap, (ng + _nearest_goal(nxt, goals), ng, nxt))
    return None


def _nearest_goal(cell: Cell, goals: set[Cell]) -> int:
    return min(manhattan(cell, g) for g in goals)


def _reconstruct(came_from: dict[Cell, Cell], cur: Cell) -> list[Cell]:
    path = [cur]
    while cur in came_from:
        cur = came_from[cur]
        path.append(cur)
    path.reverse()
    return path


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def coord(c: Cell) -> CellCoord:
    return CellCoord(x=c[0], y=c[1], z=c[2])
