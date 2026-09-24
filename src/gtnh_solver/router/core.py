"""router.core - negotiated routing with the docks inside the negotiation.

Given placed machines, connect every non-ME net. The router is the geometry authority for *how* a
net connects: it first decides which nets GT's free **auto-output** connection covers
(``auto.assign_auto_outputs`` - adjacent 1-source-1-sink nets, one auto-output per machine) and
routes only the rest.

Every routed net, **power included**, takes part in one **negotiated congestion** (the FPGA
PathFinder scheme, GitHub #7), and each net is re-routed every round as a group Steiner tree whose
dock cells are chosen by the same search that lays its path (``router.steiner``). Nothing is fixed
up front: a dock cell another net holds is a priced cell like any other, not a wall, so a net can
move its dock off a contested cell the way it moves its path. That is what the parked #164 work was
missing. The old router docked every net greedily before negotiating, and held one dock cell per
power endpoint before that, so on a tight placement both choices were made blind and could not be
undone: on the maintainer's proven parallel-sand build the power hold took a hammer's last dock
cells and gravel could not dock at all.

Power is negotiated as a *reservation*. Its tree keeps the pipes out of the space a cable needs,
and ``router.power`` then lays the real trunk, sized per segment, in the space the pipes leave. So
an energy port no longer holds a cell of its own before routing starts: a power net holds a tree,
and one cable block beside several machines serves them all, as three cables serve ten machines in
the maintainer's build.

::

    nets
      |  [1] auto-assign  router.auto: adjacent 1-source-1-sink nets take GT's free auto-output
      |                   (one per machine); only the uncovered nets are routed.
      v
      |  [2] round 1      every net (item, fluid and power) routes alone: the cheapest tree
      |                   docking all its endpoints, no other net priced in
      v
      |  [3] price        a cell or casing key used by 2+ nets is over-used: its history rises;
      |                   present price = history-scaled, times the other users, growing each round
      v
      |  [4] re-route     every net on an over-used cell or key is ripped up and re-routed as a
      |                   tree against the prices (docks included); the rest keep their trees.
      |                   Repeat until nothing is shared, or the overlap is proven geometric
      |                   (``_congestion_is_irreducible``), or the round budget runs out - then a
      |                   collision-free subset is kept, power first, and the rest fail as
      |                   congestion.
      v
    [5] emit              item and fluid trees become routes; each item pipe takes the smallest
                          gauge whose GT insertion rate reaches every endpoint on its crowded side
                          (``_pipe_size``, #165). Power trees are dropped: router.power lays cable.

Two nets never share a cell, the crude single-channel cap the validator enforces as
``ROUTE_CELL_COLLISION``. Several terminals of ONE net may share a dock cell when they belong to
different machines (#164): one pipe block wired to several neighbours is how GT builds a manifold,
and the tree search prefers it, ranking each step by cost per endpoint served. When every terminal
of a pipe net lands on one cell, that block is the whole route and it has no segment. Two
connections of one machine never share a claim key (``_grid.claim_key``), and two nets never share
a multiblock's casing cell, which is negotiated as a resource of its own because a casing cell has
up to five free faces and a cell price alone cannot see it.

Returns the auto-connections plus the item/fluid routes, or an explicit ``Infeasibility`` naming
the net that could not dock, route, or win a contested cell - never raises for the expected case,
matching the placer/validator discipline. The validator independently certifies every terminal,
re-checks every auto-connection, and enforces the one-route-per-cell cap on the result.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from types import MappingProxyType
from typing import TypeVar

from gtnh_solver.dataset import (
    DEFAULT_PIPE_SIZE,
    ROUTED_PIPE_SIZES,
    endpoint_insertions,
    item_pipe_size_for,
    route_material,
)
from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    Commodity,
    Infeasibility,
    InputIR,
    IODirection,
    Machine,
    Net,
    PipeSize,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import placement_index

from ._grid import claim_key, coord, dock_candidates, obstacle_cells
from .auto import assign_auto_outputs
from .steiner import Endpoint, Grid, Key, Tree, route_tree

#: Backstop on negotiation rounds. Most placements converge in 4 to 13 rounds, and the tightest
#: parallel-sand placements this router exists for need up to 28; the backstop only bites under
#: congestion the early-out cannot prove, where the salvage step then keeps a collision-free subset
#: and fails the rest explicitly. It is what those stuck negotiations cost: on 16 parallel-sand
#: seeds, dropping it to 24 saves 0.4 s a seed and loses two of the tight placements.
_MAX_ROUNDS = 32

#: Rounds the over-used set must stay *identical* before we test whether the remaining contention is
#: irreducible (:func:`_congestion_is_irreducible`). A stable set means history has stopped moving
#: anyone; the margin keeps a mid-oscillation blip from triggering the probe.
_STALL_ROUNDS = 3

#: PathFinder's present-sharing factor for round 2 (round 1 prices nothing), and how much it grows
#: each round after. A cell's price is ``(1 + history) * (1 + present * other users) - 1`` on top
#: of the unit step, so history multiplies with sharing: a cell that stays contested keeps getting
#: dearer relative to its neighbours, which is what breaks a stand-off between two nets.
_PRESENT_FIRST = 0.5
_PRESENT_GROWTH = 1.5

#: Added to a cell's (or casing key's) history every round it ends over-used.
_HISTORY_STEP = 1.0

#: A negotiated resource: a packed cell, or a multiblock casing key.
_R = TypeVar("_R", int, Key)


@dataclass(frozen=True)
class RouteResult:
    """Router output: the auto-output vs pipe decision, or why routing stalled.

    ``auto_connections`` are the nets the router satisfied with GT's free auto-output instead of
    a pipe (the router owns that decision); ``routes`` are the pipes for the rest (never power:
    ``router.power`` lays cable). ``failed_nets`` lists the item/fluid nets left unrouted (empty
    when ``ok``), in problem order, so the solver's place<->route feedback loop can penalize
    exactly those nets and re-place.

    ``claimed`` is the casing cells those free connections reserved, per machine. It has to travel
    with the result because a free connection spends a hatch cell on each side while owning no
    ``Route`` to read it back off: pipes are seeded with it here, but the POWER router is a
    separate pass that only ever saw the item routes' terminals, so an energy hatch could land on
    a cell an auto-output had already taken and the connection ended up with no hatch at all
    (#131).
    """

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None
    failed_nets: tuple[str, ...] = ()
    auto_connections: tuple[AutoConnection, ...] = ()
    # A factory, not a plain default: 3.11 rejects an unhashable default at class creation (#255).
    claimed: Mapping[str, frozenset[Cell]] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def ok(self) -> bool:
        """True iff every non-ME item/fluid net was routed."""
        return self.infeasibility is None


def route(problem: InputIR, placements: Sequence[Placement]) -> RouteResult:
    """Connect each non-ME net of ``problem`` over the given placements: auto-output, then pipes.

    The router first decides, from the final placements + orientations, which nets a free
    auto-output connection covers (``auto.assign_auto_outputs``); every other net, power included,
    is negotiated (module docstring). Only the item/fluid routes are returned; a power net's tree
    is a reservation that keeps the pipes off the cable's space, and ``router.power`` lays the
    cable. What still fails is real: an undockable endpoint, a walled-off path, or congestion the
    negotiation could not price apart (then a collision-free subset is kept and the rest reported).
    """
    assignment = assign_auto_outputs(problem, placements)
    nets = [
        net
        for net in problem.nets
        if net.id not in assignment.covered  # satisfied by auto-output, no pipe needed
        and not problem.me_toggles.toggled(net.commodity)
    ]
    # A free connection still costs its two machines a casing cell each (an output hatch ejects
    # through its own front face), so no terminal may dock onto one of those blocks.
    trees, failures = _negotiate(problem, placements, nets, assignment.claimed)
    machines = {m.id: m for m in problem.machines}
    routes = tuple(
        _as_route(net, trees[net.id], machines)
        for net in nets
        if net.id in trees and net.commodity is not Commodity.POWER
    )
    # A power net's failure here is not reported: router.power lays its cable afterwards, in
    # whatever space the pipes leave, and is the one entitled to say it cannot.
    still_failing = tuple(
        net.id for net in nets if net.id in failures and net.commodity is not Commodity.POWER
    )
    return RouteResult(
        routes=routes,
        infeasibility=failures[still_failing[0]] if still_failing else None,
        failed_nets=still_failing,
        auto_connections=assignment.connections,
        claimed=assignment.claimed,
    )


@dataclass(frozen=True)
class _Laid:
    """A net's routed tree, decoded back to cells: a terminal per endpoint in order, and its legs."""

    terminals: tuple[Terminal, ...]
    legs: tuple[tuple[Cell, ...], ...]


def _as_route(net: Net, laid: _Laid, machines: Mapping[str, Machine]) -> Route:
    return Route(
        net_id=net.id,
        commodity=net.commodity,
        terminals=list(laid.terminals),
        segments=[
            Segment(start=coord(a), end=coord(b), channel=0)
            for leg in laid.legs
            for a, b in pairwise(leg)
        ],
        # One representative material per family (docs/DOMAIN.md), at the size the run needs: a
        # pipe carries no tier, but it does carry a gauge, and too thin a one starves (#165).
        material=route_material(net.commodity, size=_pipe_size(net, machines)),
    )


def _pipe_size(net: Net, machines: Mapping[str, Machine]) -> PipeSize:
    """The size ``net``'s pipe is laid at, chosen from what the run has to carry (#165).

    GT counts an item pipe's capacity in *insertions*, each landing one stack in one inventory, and
    tries the nearest inventory first (``dataset/pipe_capacity.py``). So a run must reach every
    endpoint on its crowded side within each service interval, and the nearest one always has room
    for a few more: a pipe that cannot make that many insertions feeds the near machines and starves
    the far ones, which is exactly the one-hammer-in-three the maintainer saw in game. Both sides
    count. Its sinks each need topping up; its sources each fill a pipe block that GT refuses to
    refill until it is empty (``MTEItemPipe.allowPutStack``), so each needs an insertion to drain it.
    The demand is the larger side's sum of :func:`~gtnh_solver.dataset.endpoint_insertions`, which
    is where the rate enters: an endpoint moving more than a stack per interval needs a second.

    Sized per net rather than per segment, unlike a cable. A cable's load sums along a known tree;
    where GT's nearest-first routing sends items depends on buffers the layout does not model, so
    the whole run takes the size of its busiest possible point, which is where every stream on the
    crowded side meets. That can over-size a run whose endpoints pair off along it, which is the
    safe direction: a bigger pipe never starves anything.

    Fluid routes are not sized yet and keep the normal size (``dataset/pipes.py``). A demand beyond
    the largest size of the stand-in material is laid at that largest size: nothing thicker exists,
    choosing a faster material is not a policy this solver makes, and the validator is the gate
    that must refuse a run too thin for its net (#190).
    """
    if net.commodity is not Commodity.ITEM:
        return DEFAULT_PIPE_SIZE
    sides: dict[IODirection, list[float | None]] = {IODirection.OUTPUT: [], IODirection.INPUT: []}
    for endpoint in net.endpoints:
        machine = machines.get(endpoint.machine_id)
        port = (
            next((p for p in machine.faces.ports if p.id == endpoint.port_id), None)
            if machine is not None
            else None
        )
        if port is not None:
            sides[port.direction].append(port.rate)
    demand = 0
    for rates in sides.values():
        # A port with no recorded rate takes an even share of the net's throughput, which is what
        # the adapter would have written for a node of identical machines.
        share = net.throughput / max(len(rates), 1)
        demand = max(demand, sum(endpoint_insertions(share if r is None else r) for r in rates))
    size = item_pipe_size_for(max(demand, 1))  # a routed net always has an endpoint to reach
    return size if size is not None else ROUTED_PIPE_SIZES[Commodity.ITEM][-1]


def _negotiate(
    problem: InputIR,
    placements: Sequence[Placement],
    nets: Sequence[Net],
    spent: Mapping[str, Collection[Cell]],
) -> tuple[dict[str, _Laid], dict[str, Infeasibility]]:
    """Negotiate every net's tree; return ``({net_id: its tree}, {net_id: why it failed})``.

    Round 1 routes every net alone. Each later round rips up and re-routes only the nets on an
    over-used cell or casing key, against prices that grow with how contested each one has been
    (module docstring, and the constants above). A net that has a tree from an earlier round keeps
    to the start that tree grew from plus its two cheapest (``steiner.LATE_STARTS``), since the
    pocket a net settles in rarely changes after round 1.

    Deterministic: nets go in the given order, every search breaks ties on cost then cell, prices
    are pure functions of the round state, and the early-out is a proof with no dependence on order.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine = placement_index(placements)
    region = problem.bounding_region
    hard = obstacle_cells(problem, placements, machines)
    grid = Grid(region, hard)

    failures: dict[str, Infeasibility] = {}
    eps_by_net: dict[str, list[Endpoint]] = {}
    for net in nets:
        eps = _endpoints(net, placement_by_machine, machines, hard, spent, region, grid)
        if isinstance(eps, Infeasibility):
            failures[net.id] = eps
        else:
            eps_by_net[net.id] = eps

    active = [net for net in nets if net.id in eps_by_net]
    trees: dict[str, Tree] = {}
    history: dict[int, float] = {}
    key_history: dict[Key, float] = {}
    usage: dict[int, int] = {}  # packed cell -> how many nets' current trees hold it
    key_usage: dict[Key, int] = {}  # casing key -> how many nets' current trees hold it
    over: list[int] = []
    over_keys: list[Key] = []
    present = 0.0  # round 1 prices nothing: every net routes as if alone
    prev_over: tuple[frozenset[int], frozenset[Key]] | None = None
    stall = 0
    converged = False
    for _ in range(_MAX_ROUNDS):
        contested, contested_keys = set(over), set(over_keys)
        for net in list(active):
            eps = eps_by_net[net.id]
            old = trees.get(net.id)
            if old is not None:
                if old.cells.isdisjoint(contested) and old.keys(eps).isdisjoint(contested_keys):
                    continue  # not in any conflict: it keeps its tree, and its cells stay priced
                del trees[net.id]
                _count(usage, old.cells, -1)
                _count(key_usage, old.keys(eps), -1)
            extra = _prices(history, usage, present)
            key_extra = _prices(key_history, key_usage, present)
            prefer = old.terminals[0][0] if old is not None else None
            tree = route_tree(
                eps, grid, extra, key_extra, prefer=prefer, trunk=net.commodity is Commodity.POWER
            )
            if tree is None:
                # Prices never block, so this is geometry: some endpoint is walled off from the
                # rest whatever the other nets do. It fails now and holds nothing.
                failures[net.id] = _no_path(net.id)
                active.remove(net)
                continue
            trees[net.id] = tree
            _count(usage, tree.cells, 1)
            _count(key_usage, tree.keys(eps), 1)
        over = sorted(c for c, users in usage.items() if users > 1)
        over_keys = sorted(k for k, users in key_usage.items() if users > 1)
        if not over and not over_keys:
            converged = True
            break
        # Genuine congestion settles into a fixed contested set that history cannot move. Once that
        # set has repeated for _STALL_ROUNDS, prove whether it is irreducible, and if so stop now
        # rather than grind out the budget for the identical result. Only a proof may stop it, so
        # an escapable contention is never reported as congestion.
        now = (frozenset(over), frozenset(over_keys))
        stall, prev_over = (stall + 1, prev_over) if now == prev_over else (0, now)
        if stall == _STALL_ROUNDS and _congestion_is_irreducible(
            over, over_keys, active, trees, eps_by_net, grid
        ):
            break
        for c in over:
            history[c] = history.get(c, 0.0) + _HISTORY_STEP
        for k in over_keys:
            key_history[k] = key_history.get(k, 0.0) + _HISTORY_STEP
        present = _PRESENT_FIRST if present == 0.0 else present * _PRESENT_GROWTH

    kept = [net for net in active if net.id in trees]
    if not converged:
        # Out of rounds, or proven stuck: keep a collision-free subset, power first (every machine
        # on the line needs it, and its cable is laid after the pipes, in what they leave), then in
        # problem order. The rest are genuine congestion, reported so the feedback loop can
        # penalize them.
        taken: set[int] = set()
        taken_keys: set[Key] = set()
        salvaged = []
        for net in sorted(kept, key=lambda n: n.commodity is not Commodity.POWER):
            tree = trees[net.id]
            keys = tree.keys(eps_by_net[net.id])
            if tree.cells.isdisjoint(taken) and keys.isdisjoint(taken_keys):
                salvaged.append(net)
                taken |= tree.cells
                taken_keys |= keys
            else:
                failures[net.id] = _congested(net.id)
        kept = salvaged
    laid = {
        net.id: _Laid(
            terminals=tuple(t for _, (_, t) in sorted(trees[net.id].terminals.items())),
            legs=tuple(tuple(grid.dec(c) for c in leg) for leg in trees[net.id].legs),
        )
        for net in kept
    }
    return laid, failures


def _endpoints(
    net: Net,
    placement_by_machine: Mapping[str, Placement],
    machines: Mapping[str, Machine],
    hard: set[Cell],
    spent: Mapping[str, Collection[Cell]],
    region: CellBox,
    grid: Grid,
) -> list[Endpoint] | Infeasibility:
    """Every endpoint of ``net`` with the cells it may dock on, or why one has none."""
    eps: list[Endpoint] = []
    for endpoint in net.endpoints:
        placement = placement_by_machine.get(endpoint.machine_id)
        machine = machines.get(endpoint.machine_id)
        cands = (
            dock_candidates(
                endpoint.port_id,
                placement,
                machine,
                hard,
                set(),
                region,
                spent.get(endpoint.machine_id, ()),
            )
            if placement is not None and machine is not None
            else []
        )
        if machine is None or not cands:
            return _no_dock(net.id, endpoint.machine_id)
        packed = [(grid.enc(t.cell.as_tuple()), t) for t in cands]
        eps.append(
            Endpoint(
                machine_id=endpoint.machine_id,
                port_id=endpoint.port_id,
                cands=packed,
                key_of={c: claim_key(t, machine) for c, t in packed},
                multiblock=bool(machine.hatch_slots),
            )
        )
    return eps


def _prices(
    history: Mapping[_R, float], usage: Mapping[_R, int], present: float
) -> dict[_R, float]:
    """Each resource's price on top of its unit step: ``(1 + history) * (1 + present * users) - 1``.

    ``usage`` counts the other nets on it: the net being priced has been ripped up already.
    """
    out = dict(history)
    for r, users in usage.items():
        out[r] = (1.0 + history.get(r, 0.0)) * (1.0 + present * users) - 1.0
    return out


def _count(counter: dict[_R, int], resources: Collection[_R], step: int) -> None:
    for r in resources:
        counter[r] = counter.get(r, 0) + step
        if not counter[r]:
            del counter[r]


def _congestion_is_irreducible(
    over: Sequence[int],
    over_keys: Sequence[Key],
    active: Sequence[Net],
    trees: Mapping[str, Tree],
    eps_by_net: Mapping[str, list[Endpoint]],
    grid: Grid,
) -> bool:
    """Would no further negotiation round reduce the overlap? True only when *proven* so.

    A contested cell is *forced* on a net when the net has no tree at all with that one cell walled,
    ignoring every other net - a geometric fact prices cannot change, since prices only discourage.
    A contested casing key is forced the same way, with its dock cells withdrawn. When every
    contested cell and key is forced on two or more of the nets holding it, those nets share it in
    every routing, so more rounds reproduce the same overlap and the caller may stop.

    The moment one contested resource has a net that could leave it, this returns False: another
    round of history could price that net away. Conservative by construction, it answers True only
    on proof, so it never turns a routable problem into a false congestion. It proves a lone
    bottleneck, not capacity spread over parallel openings (those are left to the round budget).
    """
    for cell in over:
        forced = 0
        for net in active:
            tree = trees.get(net.id)
            if tree is None or cell not in tree.cells:
                continue  # not a user of this cell, so not what keeps it over-used
            trunk = net.commodity is Commodity.POWER
            if (
                route_tree(eps_by_net[net.id], grid, {}, {}, blocked=frozenset({cell}), trunk=trunk)
                is None
            ):
                forced += 1
        if forced < 2:
            return False
    for key in over_keys:
        forced = 0
        for net in active:
            tree = trees.get(net.id)
            eps = eps_by_net[net.id]
            if tree is None or key not in tree.keys(eps):
                continue
            without = [
                Endpoint(
                    machine_id=e.machine_id,
                    port_id=e.port_id,
                    cands=[(c, t) for c, t in e.cands if (e.machine_id, e.key_of[c]) != key],
                    key_of=e.key_of,
                    multiblock=e.multiblock,
                )
                for e in eps
            ]
            trunk = net.commodity is Commodity.POWER
            if (
                any(not e.cands for e in without)
                or route_tree(without, grid, {}, {}, trunk=trunk) is None
            ):
                forced += 1
        if forced < 2:
            return False
    return True


def _no_dock(net_id: str, machine_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="face_reachability",
        detail=f"net {net_id!r} could not dock a terminal on machine {machine_id!r} "
        f"(no free non-front face cell)",
        suggested_relaxation="free up adjacent cells, or leave routing gaps around machines",
    )


def _no_path(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="routing",
        detail=f"net {net_id!r} has no free cell path between its terminals",
        suggested_relaxation="enlarge the bounding region or reduce obstacles",
    )


def _congested(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="congestion",
        detail=f"net {net_id!r} still shares cells with another route after negotiation "
        f"(too little free space for every net to own its cells)",
        suggested_relaxation="enlarge the bounding region or spread the machines apart",
    )
