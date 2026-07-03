"""router.core - the Phase 1 crude per-commodity router.

Given placed machines, connect each non-ME **item/fluid** net (power is the power router's job,
``router.power``). The router is the geometry authority for *how* a net connects: it first
decides which nets GT's free **auto-output** connection covers (``auto.assign_auto_outputs`` -
adjacent 1-source-1-sink nets, one auto-output per machine) and lays pipes only for the rest.
For each piped net: resolve a :class:`~gtnh_solver.ir.Terminal` per endpoint (a free cell just
outside a usable, non-front machine face - the front comes from the placement orientation, so no
dataset is needed), then A* between the terminals over the free cell grid (machine + reserved
cells are obstacles). Two nets must never share a cell - the crude single-channel cap (one route
per cell), which the validator independently enforces. Rather than laying nets sequentially (and
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

Four phases (item/fluid nets; power is ``router.power``'s job)::

    nets
      |  [1] auto-assign  router.auto: adjacent 1-source-1-sink nets take GT's free auto-output
      |                   (one per machine); only the uncovered nets are piped.
      v
      |  [2] dock         a Terminal per endpoint on a usable (non-front) face, one cell out;
      |                   terminals are fixed for the whole negotiation (a pipe MUST touch its
      |                   dock cell, so docks are not tradeable and foreign docks are hard).
      v
      |  [3] route        every net independently: priced A* between its terminals; machine,
      |                   reserved, and foreign-terminal cells are hard, contested cells cost
      |                   base + present-sharing + history.
      v
    [4] negotiate         any cell shared by 2+ nets? raise its price (history grows every round
                          it stays contested) and re-route every net; repeat until collision-free
                          or the round budget exhausts - then keep a maximal collision-free
                          subset and report the rest as genuine congestion.

Returns the auto-connections plus the routes, or an explicit ``Infeasibility`` naming the net
that could not dock, route, or win a contested cell - never raises for the expected case,
matching the placer/validator discipline. The validator independently certifies that every
terminal is on a non-front face adjacent to its machine and lies on the route, re-checks every
auto-connection, and enforces the one-route-per-cell cap on the negotiated result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    Commodity,
    Infeasibility,
    InputIR,
    Net,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import placement_index

from ._grid import astar, coord, dock, obstacle_cells
from .auto import assign_auto_outputs

#: Backstop on negotiation rounds. Convergence is normally a handful of rounds (a two-net
#: conflict resolves in 2-3); the backstop only bites under genuine congestion, where the
#: salvage step then keeps a maximal collision-free subset and fails the rest explicitly.
_MAX_ROUNDS = 32

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
    """

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None
    failed_nets: tuple[str, ...] = ()
    auto_connections: tuple[AutoConnection, ...] = ()

    @property
    def ok(self) -> bool:
        """True iff every non-ME net was routed."""
        return self.infeasibility is None


def route(problem: InputIR, placements: Sequence[Placement]) -> RouteResult:
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
    """
    autos, covered = assign_auto_outputs(problem, placements)
    auto_connections = tuple(autos)
    nets = [
        net
        for net in problem.nets
        if net.id not in covered  # satisfied by auto-output, no pipe needed
        and not problem.me_toggles.toggled(net.commodity)
        and net.commodity is not Commodity.POWER  # power is the power router's job (router.power)
    ]
    if not nets:
        return RouteResult(auto_connections=auto_connections)

    routes, failures = _negotiate(problem, placements, nets)
    if not failures:
        return RouteResult(routes=tuple(routes), auto_connections=auto_connections)

    # Exhausted: report the first net still failing (in original order), with its specific reason,
    # plus every still-failing net so the solver's feedback loop can penalize them all.
    still_failing = tuple(net.id for net in nets if net.id in failures)
    return RouteResult(
        routes=tuple(routes),
        infeasibility=failures[still_failing[0]],
        failed_nets=still_failing,
        auto_connections=auto_connections,
    )


def _negotiate(
    problem: InputIR, placements: Sequence[Placement], nets: Sequence[Net]
) -> tuple[list[Route], dict[str, Infeasibility]]:
    """Route ``nets`` by negotiated congestion; return ``(routes, {net_id: why it failed})``.

    Terminals are docked once, up front (a pipe MUST touch its dock cell, so docks are not
    tradeable: an undockable net fails outright, and every other net treats foreign dock cells
    as hard). Then the rounds: each net in turn is ripped up and re-routed with priced A* -
    contested cells cost ``_PRESENT_PENALTY`` per other current user plus the accumulated
    history - and each round every cell still shared by 2+ nets has its history raised by
    ``_HISTORY_STEP``. No overlap left means convergence: the per-net cheapest paths are
    mutually disjoint. If ``_MAX_ROUNDS`` runs out first, the contention is genuine (not an
    ordering accident): a maximal collision-free subset (in problem order) is kept and every
    other net fails with a ``congestion`` infeasibility the solver's feedback loop can penalize.

    Deterministic: nets are processed in the given order, the priced A* breaks ties on cost
    then cell, and prices are pure functions of the round state.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine = placement_index(placements)
    region = problem.bounding_region
    hard = obstacle_cells(problem, placements, machines)

    # Dock every net first, against a shared claim set so no two nets dock the same cell. A net
    # that cannot dock fails now and leaves no trace (its partial docks are not folded in).
    failures: dict[str, Infeasibility] = {}
    terminals_by_net: dict[str, list[Terminal]] = {}
    term_cells_by_net: dict[str, set[Cell]] = {}
    docked: set[Cell] = set()
    for net in nets:
        chosen: set[Cell] = set()  # this net's own terminals, so two of its ports don't co-locate
        terminals: list[Terminal] = []
        for endpoint in net.endpoints:
            ep_placement = placement_by_machine.get(endpoint.machine_id)
            ep_machine = machines.get(endpoint.machine_id)
            terminal = (
                dock(endpoint.port_id, ep_placement, ep_machine, hard, docked | chosen, region)
                if ep_placement is not None and ep_machine is not None
                else None
            )
            if terminal is None:
                failures[net.id] = _no_dock(net.id, endpoint.machine_id)
                break
            chosen.add(terminal.cell.as_tuple())
            terminals.append(terminal)
        else:
            terminals_by_net[net.id] = terminals
            term_cells_by_net[net.id] = chosen
            docked |= chosen

    active = [net for net in nets if net.id not in failures]
    all_terms: set[Cell] = set().union(*term_cells_by_net.values()) if term_cells_by_net else set()

    history: dict[Cell, float] = {}
    usage: dict[Cell, int] = {}  # cell -> how many active nets' current paths include it
    cells_by_net: dict[str, set[Cell]] = {}
    segments_by_net: dict[str, list[Segment]] = {}
    converged = False
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
        )
        for net in nets
        if net.id in routed_ids
    ]
    return routes, failures


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
