"""router.core - the Phase 1 crude per-commodity router.

Given placed machines, connect each non-ME **item/fluid** net (power is the power router's job,
``router.power``). The router is the geometry authority for *how* a net connects: it first
decides which nets GT's free **auto-output** connection covers (``auto.assign_auto_outputs`` -
adjacent 1-source-1-sink nets, one auto-output per machine) and lays pipes only for the rest.
For each piped net: resolve a :class:`~gtnh_solver.ir.Terminal` per endpoint (a free cell just
outside a usable, non-front machine face - the front comes from the placement orientation, so no
dataset is needed), then A* between the terminals over the free cell grid (machine + reserved
cells are obstacles). Which face an endpoint docks on is itself decided by routing, not by a
face ordering: every free face cell is a candidate and multi-goal A* picks the pair (``_dock_net``). Two nets must never share a cell - the crude single-channel cap (one route
per cell), which the validator independently enforces. Several terminals of ONE net may share a
dock cell, though, when they belong to different machines: one pipe block wired to several
neighbours is how GT builds a manifold, and the maintainer's own parallel-sand build puts 20 item
terminals on 12 cells that way (#164). Rather than laying nets sequentially (and
being hostage to net order), the router runs **negotiated congestion** (the FPGA PathFinder
scheme, GitHub #7): every net first routes independently as if alone, then every cell shared by
two or more nets is *priced* - a present-sharing penalty per other user plus a history penalty
that grows each round the cell stays contested - and every net re-routes against the prices,
round after round, until no cell is shared. Prices discourage, never block, so a net abandons a
contested cell exactly when its detour is cheaper than the argument for staying - which makes
the result order-robust: an ordering-induced false infeasibility cannot happen, and what cannot
be negotiated inside the round budget is genuine congestion, reported per net. Still crude on
purpose (docs/ROADMAP.md): one channel per cell; the per-edge multi-channel cap (margin > 1 via
``Segment.channel``) is later lane-D work. The shared cell-grid primitives (obstacle building,
docking, priced A*) live in ``_grid`` so this router and ``router.power`` route over one grid
model.

Five phases (item/fluid nets; power is ``router.power``'s job)::

    nets
      |  [1] auto-assign  router.auto: adjacent 1-source-1-sink nets take GT's free auto-output
      |                   (one per machine); only the uncovered nets are piped.
      v
      |  [2] dock         a Terminal per endpoint on a usable (non-front) face, one cell out,
      |                   chosen route-aware (multi-goal A* over every free face cell, not the
      |                   first face in a tuple); terminals are then fixed for the whole
      |                   negotiation (a pipe MUST touch its dock cell, so docks are not
      |                   tradeable and foreign docks are hard). Endpoints of one net on
      |                   different machines may share a cell; two nets never do.
      v
      |  [3] route        every net independently: priced A* between its terminals; machine,
      |                   reserved, and foreign-terminal cells are hard, contested cells cost
      |                   base + present-sharing + history.
      v
      |  [4] negotiate    any cell shared by 2+ nets? raise its price (history grows every round
      |                   it stays contested) and re-route every net; repeat until collision-free
      |                   or the round budget exhausts - then keep a maximal collision-free
      |                   subset and report the rest as genuine congestion.
      v
    [5] size              each routed item pipe takes the smallest gauge whose GT insertion rate
                          reaches every endpoint on its crowded side (``_pipe_size``, #165);
                          fluid pipes stay at the normal size.

Returns the auto-connections plus the routes, or an explicit ``Infeasibility`` naming the net
that could not dock, route, or win a contested cell - never raises for the expected case,
matching the placer/validator discipline. The validator independently certifies that every
terminal is on a non-front face adjacent to its machine and lies on the route, re-checks every
auto-connection, and enforces the one-route-per-cell cap on the negotiated result.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

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
    MachineFaceRef,
    Net,
    PipeSize,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import placement_index

from ._grid import astar, astar_multi, claim_key, coord, dock_candidates, obstacle_cells
from .auto import assign_auto_outputs

#: Backstop on negotiation rounds. Convergence is normally a handful of rounds (a two-net
#: conflict resolves in 2-3); the backstop only bites under genuine congestion, where the
#: salvage step then keeps a maximal collision-free subset and fails the rest explicitly.
_MAX_ROUNDS = 32

#: Rounds the over-used cell set must stay *identical* before we test whether the remaining
#: contention is irreducible (:func:`_congestion_is_irreducible`). A stable set means history
#: bumps have stopped moving anyone; a couple of rounds' margin keeps a mid-oscillation blip
#: from triggering the (cheap) probe. Genuine congestion stabilizes from round 1, so this only
#: delays the early-out by a handful of rounds while never firing during productive rerouting.
_STALL_ROUNDS = 3

#: Price a net pays per *other* net currently using a cell (the PathFinder present-sharing
#: term). At 2.0, one contested cell is worth a 2-cell detour - strong enough that ties break
#: away from sharing immediately, weak enough that a long detour is not taken prematurely.
_PRESENT_PENALTY = 2.0

#: Price added to a cell for every round it ends over-used (the history term). It accumulates,
#: so a cell that stays contested gets monotonically less attractive and oscillation
#: ("you detour" / "no, you") cannot persist - the standard PathFinder convergence argument.
_HISTORY_STEP = 1.0


@dataclass(frozen=True)
class RouteResult:
    """Crude router output: the auto-output vs pipe decision, or why routing stalled.

    ``auto_connections`` are the nets the router satisfied with GT's free auto-output instead of
    a pipe (the router owns that decision); ``routes`` are the pipes for the rest.
    ``failed_nets`` lists the nets left unrouted (empty when ``ok``), in problem order, so the
    solver's place<->route feedback loop can penalize exactly those nets and re-place.

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
    claimed: Mapping[str, frozenset[Cell]] = MappingProxyType({})

    @property
    def ok(self) -> bool:
        """True iff every non-ME net was routed."""
        return self.infeasibility is None


def route(
    problem: InputIR,
    placements: Sequence[Placement],
    *,
    reserved: Collection[Cell] = (),
) -> RouteResult:
    """Connect each non-ME net of ``problem`` over the given placements: auto-output, then pipes.

    The router first decides, from the final placements + orientations, which nets a free
    auto-output connection covers (``auto.assign_auto_outputs``); only the uncovered nets are
    piped. Piping is **negotiated congestion** (module docstring): every net routes as if alone,
    contested cells are priced up round by round (present sharing + accumulating history), and
    every net re-routes against the prices until no cell is shared - order-robust, so a false
    infeasibility cannot come from net order. What still fails is real: an undockable terminal,
    a hard-walled path, or genuine congestion the round budget could not price apart (then a
    maximal collision-free subset is kept and the rest reported). Crude: one channel per cell;
    the per-edge multi-channel cap (``Segment.channel``) is later lane-D work.

    ``reserved`` are cells another router has been promised and this one may not spend -
    in practice the power docks held back by :func:`router.power.reserve_power_docks`,
    because power routes after the pipes and cannot outbid a frozen dock (#76), and, when a
    power net failed anyway, the whole trunk the solver's power-first recovery laid for it
    (#226). They are hard here in both senses: no pipe crosses one and no terminal docks on one.
    """
    assignment = assign_auto_outputs(problem, placements)
    auto_connections = assignment.connections
    nets = [
        net
        for net in problem.nets
        if net.id not in assignment.covered  # satisfied by auto-output, no pipe needed
        and not problem.me_toggles.toggled(net.commodity)
        and net.commodity is not Commodity.POWER  # power is the power router's job (router.power)
    ]
    if not nets:
        return RouteResult(auto_connections=auto_connections, claimed=assignment.claimed)

    # A free connection still costs its two machines a casing cell each (an output hatch ejects
    # through its own front face), so a pipe must not dock onto one of those blocks.
    routes, failures = _negotiate(problem, placements, nets, assignment.claimed, reserved)
    if not failures:
        return RouteResult(
            routes=tuple(routes),
            auto_connections=auto_connections,
            claimed=assignment.claimed,
        )

    # Exhausted: report the first net still failing (in original order), with its specific reason,
    # plus every still-failing net so the solver's feedback loop can penalize them all.
    still_failing = tuple(net.id for net in nets if net.id in failures)
    return RouteResult(
        routes=tuple(routes),
        infeasibility=failures[still_failing[0]],
        failed_nets=still_failing,
        auto_connections=auto_connections,
        claimed=assignment.claimed,
    )


def _negotiate(
    problem: InputIR,
    placements: Sequence[Placement],
    nets: Sequence[Net],
    spent: Mapping[str, Collection[Cell]] = MappingProxyType({}),
    reserved: Collection[Cell] = (),
) -> tuple[list[Route], dict[str, Infeasibility]]:
    """Route ``nets`` by negotiated congestion; return ``(routes, {net_id: why it failed})``.

    Terminals are docked once, up front, route-aware (:func:`_dock_net`) and against a shared
    claim set, since a pipe MUST touch its dock cell, so docks are not tradeable: an undockable
    net fails outright, and every other net treats foreign dock cells as hard. Then the rounds: each net in turn is ripped up and re-routed with priced A* -
    contested cells cost ``_PRESENT_PENALTY`` per other current user plus the accumulated
    history - and each round every cell still shared by 2+ nets has its history raised by
    ``_HISTORY_STEP``. No overlap left means convergence: the per-net cheapest paths are
    mutually disjoint. If ``_MAX_ROUNDS`` runs out first, the contention is genuine (not an
    ordering accident): a maximal collision-free subset (in problem order) is kept and every
    other net fails with a ``congestion`` infeasibility the solver's feedback loop can penalize.

    Genuine congestion need not wait out the whole budget. When the over-used set stops
    changing for ``_STALL_ROUNDS`` (history has stopped moving anyone), the same salvage runs
    early - but only once :func:`_congestion_is_irreducible` has *proven* no further round could
    help. That proof, never a count heuristic, is what preserves the order-robustness guarantee:
    an escapable contention is left to resolve, so it is never misreported as congestion.

    Deterministic: nets are processed in the given order, the priced A* breaks ties on cost
    then cell, and prices are pure functions of the round state, and the early-out is gated on a
    proof with no dependence on iteration order.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine = placement_index(placements)
    region = problem.bounding_region
    # ``reserved`` joins the machine bodies rather than the prices: a promise another router
    # is owed is not tradeable, and folding it in here covers docking and pathing at once,
    # since _dock_net takes this same set as its obstacles.
    hard = obstacle_cells(problem, placements, machines) | set(reserved)

    # Dock every net first, against a shared claim set so no two nets dock the same cell. A net
    # that cannot dock fails now and leaves no trace (its partial docks are not folded in).
    failures: dict[str, Infeasibility] = {}
    terminals_by_net: dict[str, list[Terminal]] = {}
    term_cells_by_net: dict[str, set[Cell]] = {}
    docked: set[Cell] = set()
    # Casing cells already spoken for, per machine, seeded with the ones the free auto-output
    # connections took. One cell is one block, so a cell holding an input bus cannot also hold an
    # output hatch - not even by facing the other way, which a claim on the outward cell alone
    # (``docked``) does not catch. Power docks against this same pool, seeded from the terminals
    # below, so every commodity competes for one budget rather than three.
    claimed: dict[str, set[Cell]] = {k: set(v) for k, v in spent.items()}
    state = _DockState(
        docked=docked,
        claimed=claimed,
        owner={},
        terminals_by_net=terminals_by_net,
        term_cells_by_net=term_cells_by_net,
        nets_by_id={n.id: n for n in nets},
    )
    for net in nets:
        picked = _dock_net(net, placement_by_machine, machines, hard, docked, region, claimed)
        if isinstance(picked, Infeasibility):
            # Greedy order, not geometry, may be what stranded it: ask the holders of the cells
            # it wanted to shuffle along, then dock it again (:func:`_make_room`).
            starved = _starved_endpoint(
                net, placement_by_machine, machines, hard, docked, region, claimed
            )
            if starved is not None and _make_room(
                starved.machine_id,
                starved.port_id,
                placement_by_machine,
                machines,
                hard,
                region,
                state,
                _MAX_RESEAT_DEPTH,
            ):
                picked = _dock_net(
                    net, placement_by_machine, machines, hard, docked, region, claimed
                )
        if isinstance(picked, Infeasibility):
            failures[net.id] = picked
            continue
        chosen = {t.cell.as_tuple() for t in picked}
        terminals_by_net[net.id] = picked
        term_cells_by_net[net.id] = chosen
        docked |= chosen
        for index, terminal in enumerate(picked):
            claimed.setdefault(terminal.machine_id, set()).add(
                claim_key(terminal, machines[terminal.machine_id])
            )
            state.owner.setdefault(terminal.cell.as_tuple(), []).append((net.id, index))

    active = [net for net in nets if net.id not in failures]
    all_terms: set[Cell] = set().union(*term_cells_by_net.values()) if term_cells_by_net else set()

    history: dict[Cell, float] = {}
    usage: dict[Cell, int] = {}  # cell -> how many active nets' current paths include it
    cells_by_net: dict[str, set[Cell]] = {}
    segments_by_net: dict[str, list[Segment]] = {}
    converged = False
    prev_overused: frozenset[Cell] | None = None
    stall = 0
    for _ in range(_MAX_ROUNDS):
        hard_failed: list[Net] = []
        for net in active:
            # Rip this net up: its own cells stop counting while it chooses anew.
            for cell in cells_by_net.get(net.id, ()):
                usage[cell] -= 1
                if not usage[cell]:
                    del usage[cell]
            prices = dict(history)
            for cell, users in usage.items():
                prices[cell] = prices.get(cell, 0.0) + _PRESENT_PENALTY * users
            # Foreign dock cells are hard: they cannot be traded away by any price.
            foreign_terms = all_terms - term_cells_by_net[net.id]
            laid = _lay_legs(terminals_by_net[net.id], hard | foreign_terms, prices, region)
            if laid is None:
                # Hard-blocked (prices never block): a genuine no-path, not congestion.
                failures[net.id] = _no_path(net.id)
                hard_failed.append(net)
                cells_by_net.pop(net.id, None)
                segments_by_net.pop(net.id, None)
                continue
            segments_by_net[net.id], cells_by_net[net.id] = laid
            for cell in cells_by_net[net.id]:
                usage[cell] = usage.get(cell, 0) + 1
        for net in hard_failed:
            active.remove(net)
            # A failed net lays nothing, so its dock cells stop blocking the survivors.
            all_terms -= term_cells_by_net.pop(net.id)
        overused = [cell for cell, users in usage.items() if users > 1]
        if not overused:
            converged = True
            break
        # Genuine congestion stabilizes into a fixed contested set that history cannot move.
        # Once that set has repeated for _STALL_ROUNDS, prove whether it is irreducible; if so,
        # bail to the salvage path now instead of grinding the rest of the round budget for the
        # identical result. The proof (never a heuristic on the count) is what keeps an escapable
        # - and therefore resolvable - contention from being misreported as congestion.
        overused_key = frozenset(overused)
        if overused_key == prev_overused:
            stall += 1
        else:
            stall, prev_overused = 0, overused_key
        if stall == _STALL_ROUNDS and _congestion_is_irreducible(
            overused,
            active,
            cells_by_net,
            hard,
            all_terms,
            term_cells_by_net,
            terminals_by_net,
            region,
        ):
            break
        for cell in overused:
            history[cell] = history.get(cell, 0.0) + _HISTORY_STEP

    routed_ids: set[str]
    if converged:
        routed_ids = {net.id for net in active}
    else:
        # Budget exhausted on real contention: keep a maximal collision-free subset in problem
        # order; the rest are genuine congestion, reported so the feedback loop can penalize them.
        accepted: set[Cell] = set()
        routed_ids = set()
        for net in active:
            cells = cells_by_net[net.id]
            if cells.isdisjoint(accepted):
                routed_ids.add(net.id)
                accepted |= cells
            else:
                failures[net.id] = _congested(net.id)

    routes = [
        Route(
            net_id=net.id,
            commodity=net.commodity,
            terminals=terminals_by_net[net.id],
            segments=segments_by_net[net.id],
            # One representative material per family (docs/DOMAIN.md), at the size the run needs:
            # a pipe carries no tier, but it does carry a gauge, and too thin a one starves (#165).
            material=route_material(net.commodity, size=_pipe_size(net, machines)),
        )
        for net in nets
        if net.id in routed_ids
    ]
    return routes, failures


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


#: How many docks deep the re-seat search will look to free a contested cell. Depth 1 already
#: covers "a neighbour is sitting on the only cell I can use but has three of its own"; the extra
#: levels cover a chain of those. Bounded because this runs per stranded net, and an unbounded
#: search would pay a lot to rescue a layout the placer should not have produced.
_MAX_RESEAT_DEPTH = 3


@dataclass
class _DockState:
    """The mutable docking bookkeeping shared by the greedy pass and the re-seat rescue.

    ``owner`` is the inverse of the assignment - which net's which endpoints hold a given cell -
    which is what lets a stranded net find out who to ask to move. A list, because several
    endpoints of one net may share a cell (#164).
    """

    docked: set[Cell]
    claimed: dict[str, set[Cell]]
    owner: dict[Cell, list[tuple[str, int]]]
    terminals_by_net: dict[str, list[Terminal]]
    term_cells_by_net: dict[str, set[Cell]]
    nets_by_id: dict[str, Net]


def _starved_endpoint(
    net: Net,
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    hard: set[Cell],
    docked: set[Cell],
    region: CellBox,
    claimed: Mapping[str, Collection[Cell]],
) -> MachineFaceRef | None:
    """The first endpoint of ``net`` with no free cell left to dock on, if any."""
    for endpoint in net.endpoints:
        placement = placement_by_machine.get(endpoint.machine_id)
        machine = machines.get(endpoint.machine_id)
        if placement is None or machine is None:
            continue
        if not dock_candidates(
            endpoint.port_id,
            placement,
            machine,
            hard,
            docked,
            region,
            claimed.get(endpoint.machine_id, ()),
        ):
            return endpoint
    return None


def _make_room(
    machine_id: str,
    port_id: str,
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    hard: set[Cell],
    region: CellBox,
    state: _DockState,
    depth: int,
) -> bool:
    """Free one cell this port could dock on, by re-seating whoever holds it. True if it worked.

    Docking is greedy and net-by-net, so an early net can take the one cell a later net needed
    while having somewhere else to go itself - the assignment exists, the order just missed it,
    and the later net is reported as ``face_reachability`` on a machine that is not actually
    short of room (#76). This is the augmenting path that repairs exactly that: ask each holder
    of a wanted cell to move aside, recursively, and take the first one that can.

    Runs **only after a net has already failed** to dock, so a layout whose greedy pass succeeds
    docks precisely as it did before - this cannot change a working line, only rescue a stuck one.
    """
    placement = placement_by_machine.get(machine_id)
    machine = machines.get(machine_id)
    if placement is None or machine is None:
        return False
    # The cells this port could use if they were not already spoken for, in _grid's deterministic
    # order. ``docked`` is passed empty precisely so the held cells are the ones we see.
    for terminal in dock_candidates(
        port_id, placement, machine, hard, set(), region, state.claimed.get(machine_id, ())
    ):
        cell = terminal.cell.as_tuple()
        held = state.owner.get(cell, [])
        # Empty: free already, or held by nothing this pass placed. More than one: shared by
        # several endpoints of one net (#164), and moving one of them would leave the rest on the
        # cell, so it could never be handed over - re-seating it would only put two nets on it.
        if len(held) != 1:
            continue
        if _reseat(held[0], cell, placement_by_machine, machines, hard, region, state, depth):
            return True
    return False


def _reseat(
    held: tuple[str, int],
    cell: Cell,
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    hard: set[Cell],
    region: CellBox,
    state: _DockState,
    depth: int,
) -> bool:
    """Move the terminal holding ``cell`` somewhere else, so the caller can have it.

    Lifts the terminal first - its own cell and casing claim would otherwise hide its
    alternatives from :func:`dock_candidates` - and puts it straight back if nothing is found, so
    a failed attempt leaves the assignment exactly as it was.
    """
    net_id, index = held
    terminal = state.terminals_by_net[net_id][index]
    endpoint = state.nets_by_id[net_id].endpoints[index]
    placement = placement_by_machine.get(endpoint.machine_id)
    machine = machines.get(endpoint.machine_id)
    if placement is None or machine is None:
        return False
    key = claim_key(terminal, machine)
    state.docked.discard(cell)
    state.claimed.get(endpoint.machine_id, set()).discard(key)

    def elsewhere() -> Terminal | None:
        for option in dock_candidates(
            endpoint.port_id,
            placement,
            machine,
            hard,
            state.docked,
            region,
            state.claimed.get(endpoint.machine_id, ()),
        ):
            if option.cell.as_tuple() != cell:
                return option
        return None

    moved = elsewhere()
    # Nowhere for it either - so ask ITS neighbours to shuffle, one level further out.
    if (
        moved is None
        and depth > 0
        and _make_room(
            endpoint.machine_id,
            endpoint.port_id,
            placement_by_machine,
            machines,
            hard,
            region,
            state,
            depth - 1,
        )
    ):
        moved = elsewhere()
    if moved is None:
        state.docked.add(cell)  # untouched: put the terminal back exactly where it was
        state.claimed.setdefault(endpoint.machine_id, set()).add(key)
        return False

    new_cell = moved.cell.as_tuple()
    state.terminals_by_net[net_id][index] = moved
    state.term_cells_by_net[net_id].discard(cell)
    state.term_cells_by_net[net_id].add(new_cell)
    state.docked.add(new_cell)
    state.claimed.setdefault(moved.machine_id, set()).add(claim_key(moved, machine))
    del state.owner[cell]
    state.owner[new_cell] = [(net_id, index)]  # ``docked`` excluded it, so nobody else holds it
    return True


def _dock_net(
    net: Net,
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    hard: set[Cell],
    docked: set[Cell],
    region: CellBox,
    claimed: Mapping[str, Collection[Cell]] = MappingProxyType({}),
) -> list[Terminal] | Infeasibility:
    """Choose one Terminal per endpoint **route-aware**: chain the endpoints with multi-goal A*.

    A pipe connects to any face but the front, so which face an endpoint docks on is a routing
    decision, not a tuple ordering. Committing to the first free face in ``FACE_ORDER`` - blind
    to where the route then has to go - is what made nitrobenzene's item/fluid terminals pile
    onto SOUTH while route-aware power spread over four faces. So this mirrors ``router.power``:
    take *every* free cell outside a usable face (``_grid.dock_candidates``) and let the path
    pick, leg by leg, in endpoint order (``_lay_legs`` chains the same consecutive pairs).

    The first leg runs multi-source **and** multi-goal, so the opening pair of faces is chosen
    together rather than the first endpoint guessing before it knows the second; each later leg
    starts from the cell already chosen. Selection routes against ``hard | docked`` only - other
    nets' *paths* do not exist yet, and pricing them apart is the negotiation's job - so the
    terminals this returns stay fixed for the whole negotiation, exactly as before (a pipe MUST
    touch its dock cell; see the module docstring).

    Falls back to the first free candidate for any endpoint the chain could not reach, which
    keeps the failure taxonomy unchanged: an endpoint with no free face at all is
    ``face_reachability`` here, while a docked-but-unreachable net fails as ``routing`` in the
    round, as it always has.

    ``claimed`` are the casing cells each machine's already-placed hatches hold. This net adds its
    own as it goes, so two of its endpoints landing on one machine still take a cell each.

    **Endpoints of this net may share a dock cell** when they are on different machines (#164): a
    leg may end on a cell an earlier endpoint of the same net already docked on, which is one pipe
    block wired to two machines. Three things keep that sound. Two endpoints of *one* machine still
    cannot share, because ``mine`` holds each machine's :func:`_grid.claim_key` and for a single
    block that IS the dock cell (the validator re-checks it, ``TERMINAL_FACE_CONTENTION``). Other
    nets' docks stay out of reach through ``docked``, since a pipe delivers to any inventory wired
    to it and nothing in the IR says two nets carry the same item. And a shared cell is only ever
    reached by a laid leg: a leg's goals exclude its own start, so consecutive endpoints never
    share and the net always spans two cells, and the first-free fallback below does not share at
    all. A net collapsed onto one cell would lay no segment, which is not a route.
    """
    candidates: list[list[Terminal]] = []
    for endpoint in net.endpoints:
        ep_placement = placement_by_machine.get(endpoint.machine_id)
        ep_machine = machines.get(endpoint.machine_id)
        cand = (
            dock_candidates(
                endpoint.port_id,
                ep_placement,
                ep_machine,
                hard,
                docked,
                region,
                claimed.get(endpoint.machine_id, ()),
            )
            if ep_placement is not None and ep_machine is not None
            else []
        )
        if not cand:
            return _no_dock(net.id, endpoint.machine_id)
        candidates.append(cand)

    blocked = hard | docked
    terminals: list[Terminal] = []
    taken: set[Cell] = set()  # this net's own dock cells, which only a laid leg may share
    mine: dict[str, set[Cell]] = {}  # its own claim keys per machine, which nothing may share
    for leg, cand in enumerate(candidates[1:]):
        starts = (
            {t.cell.as_tuple() for t in candidates[0]}
            if not terminals
            else {terminals[-1].cell.as_tuple()}
        )
        goals = {t.cell.as_tuple() for t in _free(cand, mine, machines)} - starts
        path = astar_multi(starts, goals, blocked, region) if goals else None
        if path is None:
            break  # unreachable from here on; the fallback below picks the remaining faces
        if not terminals:
            _keep(_at_cell(candidates[0], path[0]), terminals, taken, mine, machines)
        _keep(_at_cell(candidates[leg + 1], path[-1]), terminals, taken, mine, machines)

    for i in range(len(terminals), len(candidates)):
        # No leg reached this endpoint, so it may not share: first-fit onto one of this net's own
        # cells could seat two consecutive endpoints on one cell, or the whole net on one.
        unshared = (
            t for t in _free(candidates[i], mine, machines) if t.cell.as_tuple() not in taken
        )
        free = next(unshared, None)
        if free is None:
            return _no_dock(net.id, net.endpoints[i].machine_id)
        _keep(free, terminals, taken, mine, machines)
    return terminals


def _free(
    candidates: Sequence[Terminal],
    mine: dict[str, set[Cell]],
    machines: dict[str, Machine],
) -> list[Terminal]:
    """``candidates`` minus what this net already holds on the candidate's own machine.

    Not minus this net's dock cells: another machine's endpoint of this net may share one (#164).
    """
    return [
        t
        for t in candidates
        if claim_key(t, machines[t.machine_id]) not in mine.get(t.machine_id, frozenset())
    ]


def _keep(
    terminal: Terminal,
    terminals: list[Terminal],
    taken: set[Cell],
    mine: dict[str, set[Cell]],
    machines: dict[str, Machine],
) -> None:
    """Accept ``terminal`` for this net, spending its dock cell and its hatch cell."""
    terminals.append(terminal)
    taken.add(terminal.cell.as_tuple())
    mine.setdefault(terminal.machine_id, set()).add(
        claim_key(terminal, machines[terminal.machine_id])
    )


def _at_cell(candidates: Sequence[Terminal], cell: Cell) -> Terminal:
    """The candidate Terminal sitting on ``cell`` (``_dock_faces`` yields each cell once)."""
    return next(t for t in candidates if t.cell.as_tuple() == cell)


def _lay_legs(
    terminals: Sequence[Terminal],
    hard: set[Cell],
    prices: dict[Cell, float],
    region: CellBox,
) -> tuple[list[Segment], set[Cell]] | None:
    """Chain consecutive terminals with priced A*; the union is a single connected subgraph.

    Returns ``(segments, every cell the path touches)``, or ``None`` if some leg is hard-blocked
    (prices only discourage; ``hard`` is what blocks)."""
    segments: list[Segment] = []
    cells: set[Cell] = {terminals[0].cell.as_tuple()} if terminals else set()
    for a, b in pairwise([t.cell for t in terminals]):
        path = astar(a.as_tuple(), b.as_tuple(), hard, region, prices)
        if path is None:
            return None
        for c0, c1 in pairwise(path):
            segments.append(Segment(start=coord(c0), end=coord(c1), channel=0))
        cells.update(path)
    return segments, cells


def _congestion_is_irreducible(
    overused: Sequence[Cell],
    active: Sequence[Net],
    cells_by_net: dict[str, set[Cell]],
    hard: set[Cell],
    all_terms: set[Cell],
    term_cells_by_net: dict[str, set[Cell]],
    terminals_by_net: dict[str, list[Terminal]],
    region: CellBox,
) -> bool:
    """Would no further negotiation round reduce the overlap? True only when *proven* so.

    A cell is *unavoidable* for a net when the net cannot route between its terminals with that
    one cell blocked (its own hard obstacles + foreign docks + the cell), ignoring every other
    net - a purely geometric fact prices cannot change, since prices only discourage. When some
    over-used cell is unavoidable for two or more of the nets on it, those nets must share it in
    *every* routing, so it stays over-used no matter how many more rounds run. When that holds for
    every over-used cell, the whole residual overlap is permanent: the salvage subset is already
    final and grinding the rest of the round budget reproduces the identical result, so the caller
    may bail now.

    The moment one over-used cell still has a net that could detour off it, this returns False -
    another round of history could price that net away, so the contention is resolvable and must
    not be reported as congestion. Conservative by construction: it answers True only on proof, so
    the early-out can never turn a routable problem into a false infeasibility. It proves a lone
    bottleneck (or narrow corridor), not capacity spread across several parallel openings - those
    have no single unavoidable cell and are left to the round budget, still correctly. Pure A* on
    fixed obstacle sets, so the verdict does not depend on iteration order.
    """
    for cell in overused:
        forced = 0
        for net in active:
            if cell not in cells_by_net.get(net.id, frozenset()):
                continue  # not a user of this cell, so not what keeps it over-used
            blocked = hard | (all_terms - term_cells_by_net[net.id]) | {cell}
            if _lay_legs(terminals_by_net[net.id], blocked, {}, region) is None:
                forced += 1
                if forced >= 2:
                    break
        if forced < 2:
            return False  # this cell is escapable for a net on it - keep negotiating
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
