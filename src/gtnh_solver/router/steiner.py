"""router.steiner - one net as a group Steiner tree over a priced cell grid.

The negotiation in ``router.core`` asks one question per net per round: given what every cell
currently costs, what is the cheapest tree that docks every endpoint of this net? Each endpoint is
a *group* of cells (every free cell just outside a usable face of its machine, ``_grid``'s
``dock_candidates``), and the tree only has to touch one cell of each group. Which one is decided
here, by the same search that lays the path, so a dock is never chosen blind to where its pipe
has to go.

::

    for each start (all of the first endpoint's dock cells at once, then a few of them alone):
      tree = {start}
      while an endpoint is unconnected:
        priced multi-source Dijkstra outward from every tree cell
        stop at the dock cell with the lowest  (path cost + casing prices) / endpoints it serves
        add the path; every endpoint that cell serves is now connected, on that one cell
    keep the cheapest tree (then the one with fewer cells)

**Why cost per endpoint.** A cell next to several machines of one net connects them all for one
path: one pipe block wired to several neighbours is how GT builds a manifold, and it is how the
maintainer's parallel-sand build puts 20 item terminals on 12 cells (#164). Ranking the next step
by plain cost would take the nearest single dock every time and never find it; ranking by cost per
endpoint served is the classic greedy for group Steiner trees. A dock cell already on the tree
costs nothing, so it is a free tap.

**Why several starts.** The first terminal decides which pocket of free space the whole tree grows
in, and the greedy growth cannot undo that choice. The multi-source start (the first leg run from
every dock cell of the first endpoint at once) is exact for a two-endpoint net; a larger net also
tries up to ``STARTS`` single cells, and a start whose distance bound cannot beat the best tree so
far is skipped without being grown.

**What a tree may not do.** Two connections of one machine never share a claim key
(``_grid.claim_key``: the dock cell of a single block, the casing cell of a multiblock), and a
tree always has a segment, since a route with no segment is not a route (``ROUTE_DISCONTINUOUS``):
if every endpoint lands on one cell, a pipe gets a one-block stub beside it, and a power net's
tree gets the leg ``router.power`` would lay its last sink instead.

Cells are packed into ints, ``(x * sy + y) * sz + z``, which orders exactly as the ``(x, y, z)``
tuples do, so every tie breaks as it would on tuples and the search is deterministic.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field

from gtnh_solver.ir import CellBox, Terminal
from gtnh_solver.ir.geometry import Cell

#: Single-cell starts a tree of three or more endpoints tries in a round where it has no previous
#: tree to go on (round 1, or a failure probe): the first endpoint's cheapest dock cells.
STARTS = 8

#: Single-cell starts a net tries once it has a tree from an earlier round: its cheapest dock cells,
#: plus the start its last tree grew from. Measured on 16 parallel-sand seeds, this keeps every
#: seed valid for about half the routing time of trying all ``STARTS`` every round.
LATE_STARTS = 2

#: A claim key: (machine id, the cell two of its connections may not both hold).
Key = tuple[str, Cell]


class Grid:
    """The bounding region as packed int cells, with a cached free-neighbour tuple per cell."""

    def __init__(self, region: CellBox, hard: Collection[Cell]) -> None:
        self.sx, self.sy, self.sz = region.sx, region.sy, region.sz
        self.hard = {self.enc(c) for c in hard if self.inside(c)}
        self._neighbours: dict[int, tuple[int, ...]] = {}

    def inside(self, c: Cell) -> bool:
        return 0 <= c[0] < self.sx and 0 <= c[1] < self.sy and 0 <= c[2] < self.sz

    def enc(self, c: Cell) -> int:
        return (c[0] * self.sy + c[1]) * self.sz + c[2]

    def dec(self, i: int) -> Cell:
        rest, z = divmod(i, self.sz)
        x, y = divmod(rest, self.sy)
        return (x, y, z)

    def adj(self, i: int) -> tuple[int, ...]:
        """The face neighbours of ``i`` that are in the region and not hard, ascending."""
        got = self._neighbours.get(i)
        if got is None:
            x, y, z = self.dec(i)
            step_x, step_y = self.sy * self.sz, self.sz
            steps = (
                (x > 0, -step_x),
                (y > 0, -step_y),
                (z > 0, -1),
                (z < self.sz - 1, 1),
                (y < self.sy - 1, step_y),
                (x < self.sx - 1, step_x),
            )
            got = tuple(i + d for ok, d in steps if ok and i + d not in self.hard)
            self._neighbours[i] = got
        return got


@dataclass
class Endpoint:
    """One endpoint of a net: its machine and every cell it may dock on."""

    machine_id: str
    port_id: str
    #: ``(packed dock cell, terminal)``, in ``dock_candidates`` order.
    cands: list[tuple[int, Terminal]]
    #: Packed dock cell -> its claim key on this machine (``_grid.claim_key``).
    key_of: dict[int, Cell]
    #: Only a multiblock's key (a casing cell) is contended between nets; a single block's key is
    #: its dock cell, which the cell prices already keep to one net.
    multiblock: bool = False


@dataclass
class Tree:
    """A net's routed tree: a terminal per endpoint, its cells, and the paths that laid them."""

    #: Endpoint index -> ``(packed dock cell, terminal)``.
    terminals: dict[int, tuple[int, Terminal]] = field(default_factory=dict)
    cells: set[int] = field(default_factory=set)
    legs: list[list[int]] = field(default_factory=list)

    def keys(self, eps: Sequence[Endpoint]) -> set[Key]:
        """The casing keys this tree's multiblock terminals hold (a resource other nets want)."""
        return {
            (eps[i].machine_id, eps[i].key_of[c])
            for i, (c, _) in self.terminals.items()
            if eps[i].multiblock
        }


def route_tree(
    eps: Sequence[Endpoint],
    grid: Grid,
    extra: Mapping[int, float],
    key_extra: Mapping[Key, float],
    *,
    blocked: frozenset[int] = frozenset(),
    prefer: int | None = None,
    trunk: bool = False,
) -> Tree | None:
    """The cheapest tree docking every endpoint of ``eps``, or ``None`` if none exists.

    A cell costs 1 plus ``extra`` (its congestion price); a multiblock terminal also pays its
    casing key's ``key_extra``. ``blocked`` cells are walls on top of the grid's own. ``prefer`` is
    the start this net's previous tree grew from: when given, only ``LATE_STARTS`` single starts
    are tried besides it, instead of ``STARTS``. ``trunk`` marks a power net, whose tree is a
    reservation for the cable ``router.power`` will lay, so it follows that router's one extra rule
    (see :func:`_grow`).

    ``None`` only when some endpoint cannot be reached at all: prices discourage, never block, so
    a net that has any tree gets one whatever the prices are.
    """
    first = eps[0]

    def kp(i: int, c: int) -> float:
        ep = eps[i]
        return key_extra.get((ep.machine_id, ep.key_of[c]), 0.0) if ep.multiblock else 0.0

    starts = sorted((extra.get(c, 0.0) + kp(0, c), c) for c, _ in first.cands if c not in blocked)
    options: list[Mapping[int, float]] = [{c: cost for cost, c in starts}]
    if len(eps) > 2:
        if prefer is None:
            singles = {c for _, c in starts[:STARTS]}
        else:
            singles = {c for _, c in starts[:LATE_STARTS]} | {prefer}
        options += [{c: cost} for cost, c in starts if c in singles]

    # Any tree grown from one cell holds a path from it to a dock cell of every endpoint, so it has
    # at least 1 + (the largest of those distances) cells, each costing at least 1.
    far: list[list[Cell]] = [[grid.dec(c) for c, _ in ep.cands] for ep in eps[1:]]

    def fewest_cells(start: int) -> int:
        x, y, z = grid.dec(start)
        return 1 + max(
            (min(abs(x - q[0]) + abs(y - q[1]) + abs(z - q[2]) for q in cells) for cells in far),
            default=0,
        )

    best: tuple[float, int, Tree] | None = None
    for seeds in options:
        if best is not None:  # every option after the first is a single start
            (start,) = seeds
            bound = fewest_cells(start)
            if bound > best[0] or (bound == best[0] and bound >= best[1]):
                continue  # cannot beat the best tree so far, even on its bound
        tree = _grow(eps, seeds, grid, extra, kp, blocked, trunk)
        if tree is None:
            continue
        cost = sum(1.0 + extra.get(c, 0.0) for c in tree.cells) + sum(
            kp(i, c) for i, (c, _) in tree.terminals.items()
        )
        if best is None or (cost, len(tree.cells)) < (best[0], best[1]):
            best = (cost, len(tree.cells), tree)
    return best[2] if best is not None else None


def _grow(
    eps: Sequence[Endpoint],
    seeds: Mapping[int, float],
    grid: Grid,
    extra: Mapping[int, float],
    kp: Callable[[int, int], float],
    blocked: frozenset[int],
    trunk: bool = False,
) -> Tree | None:
    """Grow one tree from ``seeds`` (dock cells of the first endpoint, with their entry costs).

    If every endpoint lands on one cell, a pipe gets a one-block stub beside it; a ``trunk`` gets a
    real leg from that cell to another dock cell of its last endpoint instead, because that is what
    ``router.power`` will lay (its last sink never taps a trunk that has no segment yet)."""
    tree = Tree()
    mine: dict[str, set[Cell]] = {}  # machine -> the claim keys this net already holds on it
    machine_of = [ep.machine_id for ep in eps]

    def keep(i: int, c: int) -> None:
        ep = eps[i]
        tree.terminals[i] = (c, next(t for cc, t in ep.cands if cc == c))
        mine.setdefault(ep.machine_id, set()).add(ep.key_of[c])

    def goals_for(
        pending: Collection[int], banned: Collection[Cell]
    ) -> dict[int, list[tuple[float, int]]]:
        """Every dock cell a pending endpoint may still take, with its casing price."""
        goals: dict[int, list[tuple[float, int]]] = {}
        for i in sorted(pending):
            ep = eps[i]
            held = mine.get(ep.machine_id, set())
            for c, _ in ep.cands:
                key = ep.key_of[c]
                if (
                    c in blocked
                    or key in held
                    or (ep.machine_id == first.machine_id and key in banned)
                ):
                    continue
                goals.setdefault(c, []).append((kp(i, c), i))
        return goals

    first = eps[0]
    pending = set(range(1, len(eps)))
    if not seeds or not pending:
        return None  # the first endpoint's every dock cell is blocked, or there is nothing to join
    if len(seeds) == 1:
        (start,) = seeds
        keep(0, start)
        tree.cells.add(start)
        pathed: dict[int, float] = {start: 0.0}
        banned: set[Cell] = set()
    else:
        # The first endpoint's terminal is whichever seed the first path leaves from, which is not
        # known yet; another connection of its machine may therefore hold none of the seeds' keys.
        pathed = dict(seeds)
        banned = {first.key_of[s] for s in seeds}
    while pending:
        goals = goals_for(pending, banned)
        found = _search(pathed, goals, grid, extra, machine_of, blocked) if goals else None
        if found is None:
            return None
        path, served = found
        if not tree.terminals:
            keep(0, path[0])
            tree.cells.add(path[0])
            banned = set()
        for i in served:
            keep(i, path[-1])
            pending.discard(i)
        if len(path) > 1:
            tree.cells.update(path)
            tree.legs.append(path)
        pathed = dict.fromkeys(tree.cells, 0.0)
    if tree.legs:
        return tree
    # Every endpoint landed on one cell, and a route needs a segment.
    (only,) = tree.cells
    if not trunk:
        # A pipe block may simply have a one-block stub beside it.
        stubs = [(extra.get(n, 0.0), n) for n in grid.adj(only) if n not in blocked]
        if not stubs:
            return None
        stub = min(stubs)[1]
        tree.cells.add(stub)
        tree.legs.append([only, stub])
        return tree
    # A cable trunk may not: router.power never lets its last sink tap a trunk with no segment,
    # it lays that sink a leg to a dock cell of its own. Reserve what that leg will need.
    last = len(eps) - 1
    ep = eps[last]
    mine[ep.machine_id].discard(ep.key_of[tree.terminals.pop(last)[0]])
    held = mine.get(ep.machine_id, set())
    goals = {
        c: [(kp(last, c), last)]
        for c, _ in ep.cands
        if c != only and c not in blocked and ep.key_of[c] not in held
    }
    found = _search({only: 0.0}, goals, grid, extra, machine_of, blocked) if goals else None
    if found is None:
        return None
    path, _ = found
    keep(last, path[-1])
    tree.cells.update(path)
    tree.legs.append(path)
    return tree


def _search(
    seeds: Mapping[int, float],
    goals: Mapping[int, list[tuple[float, int]]],
    grid: Grid,
    extra: Mapping[int, float],
    machine_of: Sequence[str],
    blocked: frozenset[int],
) -> tuple[list[int], list[int]] | None:
    """Priced multi-source Dijkstra to the goal cell with the lowest cost per endpoint it serves.

    ``goals`` maps a dock cell to ``(casing price, endpoint)`` for every pending endpoint that may
    take it. A cell serves at most one endpoint per machine (one face does one thing). Returns the
    path (``path[0]`` a seed, ``path[-1]`` the goal) and the endpoints the goal serves.

    It stops as soon as nothing unexplored can win: a goal serving ``k`` endpoints can still beat
    the best ratio ``r`` only while its cost is at most ``r * k``, and not at all if even its
    straight-line distance from the seeds is more than that. So the search runs out to the largest
    such ``r * k`` over the goals still alive, and no farther.
    """
    grouped: dict[int, list[tuple[float, int]]] = {}
    for c, entries in goals.items():
        per_machine: dict[str, tuple[float, int]] = {}
        for price, i in sorted(entries):
            per_machine.setdefault(machine_of[i], (price, i))
        grouped[c] = list(per_machine.values())

    seed_cells = [grid.dec(q) for q in seeds]
    alive: dict[int, tuple[int, int]] = {}  # multi-endpoint goal -> (endpoints, distance bound)
    for c, group in grouped.items():
        if len(group) > 1:
            x, y, z = grid.dec(c)
            near = min(abs(x - q[0]) + abs(y - q[1]) + abs(z - q[2]) for q in seed_cells)
            alive[c] = (len(group), near)

    def reach(ratio: float) -> float:
        out = ratio
        for k, near in alive.values():
            if near <= ratio * k:
                out = max(out, ratio * k)
        return out

    heap: list[tuple[float, int]] = [(cost, c) for c, cost in sorted(seeds.items())]
    heapq.heapify(heap)
    best: dict[int, float] = dict(seeds)
    came: dict[int, int] = {}
    found: tuple[float, int, int] | None = None  # (cost per endpoint, -endpoints, cell)
    limit = float("inf")
    cost_of = extra.get
    while heap:
        g, cur = heapq.heappop(heap)
        if g > limit:
            break
        if g > best[cur]:
            continue  # a stale entry: this cell was reached more cheaply already
        served = grouped.get(cur)
        if served:
            cand = ((g + sum(p for p, _ in served)) / len(served), -len(served), cur)
            was_alive = alive.pop(cur, None) is not None
            if found is None or cand < found:
                found = cand
                limit = reach(found[0])
            elif was_alive:
                limit = reach(found[0])
        for nxt in grid.adj(cur):
            if nxt in blocked:
                continue
            ng = g + 1.0 + cost_of(nxt, 0.0)
            if ng < best.get(nxt, float("inf")):
                best[nxt] = ng
                came[nxt] = cur
                heapq.heappush(heap, (ng, nxt))
    if found is None:
        return None
    cur = found[2]
    path = [cur]
    while cur in came:
        cur = came[cur]
        path.append(cur)
    path.reverse()
    return path, [i for _, i in grouped[found[2]]]
