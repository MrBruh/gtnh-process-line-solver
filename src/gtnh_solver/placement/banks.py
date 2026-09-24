"""placement.banks - lay a chain of banks of parallel single blocks out as columns.

A GT:NH line very often runs a stage on several identical machines (a plan node's
``machineCount``): a **bank**, every member on exactly the same nets. When the whole line is one
chain of such banks - a feed, stage 1, stage 2, ..., a drain - there is a layout the annealer does
not find. Its cost cannot see the one thing that makes the layout small: a straight run of cells
next to every machine of two banks, so that one pipe block serves a machine on each side. Measured
on parallel-sand, that cost ranks a flat row of the nine hammers level with this layout, and the
search, given the local moves it lacked, still lands in the rows.

So this builds the layout directly. It is the maintainer's parallel-sand build, derived from the
plan rather than copied from it (``tests/golden/schematic/sand-parallel-*``)::

      the columns: every cell a column along z        the cap row, on the region's far z wall

      y=2   +  3  +                                    O  .  .
      y=1   .  =  2                                    .  P  .
      y=0   +  1  +                                    I  .  .
          x=0  1  2                                    x=0 1  2

      1 2 3  one bank each: stage k's column at x = 1 + k % 2, y = k, so consecutive stages are
             diagonal and share two neighbouring columns, one of which carries their pipe
      I O    the feed and the drain cap a free column beside the stage they serve
      P      a power source caps the free column beside the most stages, front out of the wall
      + =    the pipe and cable the router then lays; this module places machines only

It is a **candidate**, not a verdict. The solver routes it like any attempt and keeps it only if
the routed structure ranks best (``solver.core``), so a line it suits gets the compact build and a
line it does not suit loses one routing attempt. That is also why it is deliberately narrow: only
single-block machines, only one linear chain, no pinned I/O. Anything else returns ``None`` and the
line is solved exactly as before.
"""

from __future__ import annotations

from gtnh_solver.ir import CellBox, CellCoord, Commodity, Facing, InputIR, Machine, Placement
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import net_sources_sinks, port_direction_map

_Column = tuple[int, int]  # a cross-section cell (x, y); the column runs along z


def bank_columns(problem: InputIR) -> tuple[Placement, ...] | None:
    """The column layout for a line that is one chain of banks of single blocks, else ``None``.

    Deterministic: bank members take their column in the order ``problem.machines`` lists them.
    """
    chain = _bank_chain(problem)
    if chain is None:
        return None
    return _lay_columns(problem, chain)


def _bank_chain(problem: InputIR) -> list[list[Machine]] | None:
    """The line's banks in flow order, when the line is exactly one chain of them.

    A bank is every machine on the same set of nets; the power sources stand apart, since they
    cap a column rather than form one. The chain has to be the whole line: every item or fluid net
    runs from one bank to the next, no bank feeds two or is fed by two, and at least one bank has
    more than one member (a chain of single machines is a line the search already lays well).
    """
    if problem.pinned or any(
        (m.footprint.sx, m.footprint.sy, m.footprint.sz) != (1, 1, 1) for m in problem.machines
    ):
        return None
    nets_of: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    for net in problem.nets:
        for end in net.endpoints:
            nets_of[end.machine_id].add(net.id)
    banks: dict[frozenset[str], list[Machine]] = {}
    for m in problem.machines:
        if not m.is_power_source:
            banks.setdefault(frozenset(nets_of[m.id]), []).append(m)
    members = list(banks.values())
    bank_of = {m.id: i for i, bank in enumerate(members) for m in bank}

    port_dir = port_direction_map(problem)
    succ: dict[int, int] = {}
    pred: dict[int, int] = {}
    for net in problem.nets:
        if net.commodity is Commodity.POWER:
            continue
        sources, sinks = net_sources_sinks(net, port_dir)
        ends = {e.machine_id for e in (*sources, *sinks)}
        if not ends <= bank_of.keys():
            return None  # a power source on a material net: not a chain of banks
        src = {bank_of[e.machine_id] for e in sources}
        dst = {bank_of[e.machine_id] for e in sinks}
        if len(src) != 1 or len(dst) != 1 or src == dst:
            return None  # a net joins more than two banks, or loops within one
        (a,), (b,) = src, dst
        if succ.setdefault(a, b) != b or pred.setdefault(b, a) != a:
            return None  # a bank feeds, or is fed by, two others: a branch, not a chain
    heads = [i for i in range(len(members)) if i not in pred]
    if len(heads) != 1:
        return None
    order = [heads[0]]
    while order[-1] in succ and len(order) <= len(members):
        order.append(succ[order[-1]])
    if len(order) != len(members) or max(len(bank) for bank in members) < 2:
        return None  # a cycle or a second, disconnected line; or no bank at all
    return [members[i] for i in order]


def _lay_columns(problem: InputIR, chain: list[list[Machine]]) -> tuple[Placement, ...] | None:
    """Place ``chain`` as columns against the region's far z wall (module docstring), or ``None``
    when it does not fit the region or a column would land on a reserved cell."""
    region = problem.bounding_region
    # A single machine at either end of the chain is its feed or its drain: it caps a column.
    feed = chain[0][0] if len(chain) > 1 and len(chain[0]) == 1 else None
    drain = chain[-1][0] if len(chain) > 1 and len(chain[-1]) == 1 else None
    stages = chain[feed is not None : len(chain) - (drain is not None)]
    length = max(len(bank) for bank in stages)
    z0, cap = region.sz - 1 - length, region.sz - 1
    if z0 < 0 or len(stages) > region.sy or region.sx < 3:
        return None

    column = [(1 + k % 2, k) for k in range(len(stages))]
    used: set[_Column] = set(column)
    for k in range(len(stages) - 1):
        # Consecutive stages share two neighbouring columns. Their pipe prefers the one away from
        # the middle, where a cable can reach every stage; the only column already taken is the
        # previous pair's, so one of the two is always free.
        outer, inner = (2 - k % 2, k), (1 + k % 2, k + 1)
        first, second = (outer, inner) if k % 2 == 0 else (inner, outer)
        used.add(second if first in used else first)

    placed: list[Placement] = []
    for (x, y), bank in zip(column, stages, strict=True):
        for j, m in enumerate(bank):
            placed.append(_at(m, (x, y, z0 + j), Facing.NORTH))
    for cap_machine, stage in ((feed, column[0]), (drain, column[-1])):
        if cap_machine is not None:
            side = _free_side(stage, used, region)
            if side is None:
                return None
            used.add(side)
            placed.append(_at(cap_machine, (*side, cap), Facing.SOUTH))
    for source in (m for m in problem.machines if m.is_power_source):
        # Facing south on the cap row puts its front, the feed face, flush on the far z wall.
        spine = _spine(column, used, region)
        if spine is None or Facing.SOUTH not in source.orientation_options:
            return None
        used.add(spine)
        placed.append(_at(source, (*spine, cap), Facing.SOUTH))

    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    if not reserved.isdisjoint((p.cell.x, p.cell.y, p.cell.z) for p in placed):
        return None
    by_id = {p.machine_id: p for p in placed}
    return tuple(by_id[m.id] for m in problem.machines)


def _free_side(stage: _Column, used: set[_Column], region: CellBox) -> _Column | None:
    """The first free column beside ``stage``: its own pipe run, next to every member."""
    x, y = stage
    for side in ((x - 1, y), (x + 1, y), (x, y + 1), (x, y - 1)):
        if side not in used and 0 <= side[0] < region.sx and 0 <= side[1] < region.sy:
            return side
    return None


def _spine(column: list[_Column], used: set[_Column], region: CellBox) -> _Column | None:
    """The free column beside the most stages, lowest first: where one cable feeds them all."""
    stages = set(column)
    best: tuple[int, int, int] | None = None
    for y in range(min(region.sy, len(column) + 1)):
        for x in range(min(region.sx, 4)):
            if (x, y) in used:
                continue
            touching = sum(
                1 for n in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)) if n in stages
            )
            key = (-touching, y, x)
            if best is None or key < best:
                best = key
    return None if best is None else (best[2], best[1])


def _at(m: Machine, cell: Cell, preferred: Facing) -> Placement:
    """``m`` at ``cell``, facing ``preferred`` when it may, else its first legal facing."""
    facing = preferred if preferred in m.orientation_options else m.orientation_options[0]
    return Placement(machine_id=m.id, cell=_coord(cell), orientation=facing)


def _coord(cell: Cell) -> CellCoord:
    return CellCoord(x=cell[0], y=cell[1], z=cell[2])
