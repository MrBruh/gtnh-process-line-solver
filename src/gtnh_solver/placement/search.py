"""placement.search - Phase 2 simulated-annealing + LNS placement with a routing-aware cost.

Starts from the constructive first-fit solution and improves it under a cost that proxies
buildability: half-perimeter wirelength (HPWL) per item/fluid net pulls connected machines
together (more auto-output, shorter pipes); an auto-output reward favours orientations whose
usable (non-front) faces actually let a source eject into its sink; and compactness is two
independently weighted terms - the **footprint** (floor area, x-span times z-span, shrunk by
stacking vertically) and the total bounding-box **volume** (shrunk by staying flat/cubic) -
whose weights the selectable :data:`Objective` picks, since the two pull opposite ways
(docs/ROADMAP.md lane C). The default ``footprint`` objective drives the floor area down and
keeps volume as a mild tiebreak. The auto reward is the only orientation-dependent term, so
reorient moves carry a real cost signal - without it they were free random walk that could
finalize an orientation BLOCKING auto-output.

Power nets carry **no base cost term**: cheap center-distance proxies (HPWL, MST) cannot see
dock faces or shared cable taps, and measurably steer AWAY from low-cable layouts (a source
sitting on top of a machine row scores nearer its sinks than one whose dock cell the sinks can
tap, yet needs more cable). The real per-segment cable cost is judged where it is knowable: the
solver's feedback loop routes each candidate placement and keeps the best layout by (footprint,
cable cells, volume). What remains here is the rescue path - a power net the router could NOT
lay gets a feedback penalty, which switches on a minimum-spanning-tree pull over the net's
members (a shared-amperage trunk is a tree) until it routes.

The neighbourhood mixes small moves (relocate / swap / reorient; orientation is a search variable)
with a **large neighbourhood search (LNS) ruin-and-recreate** move: rip out a *related* cluster of
machines (a net-connected neighbourhood, the ones that want to sit together) and greedily
re-insert each at the position + orientation that minimises the cost, biased toward cells next to
its already-placed net-neighbours. One LNS step reshapes a whole cluster at once, escaping the
local optima single-cell moves plateau in. Metropolis acceptance with geometric cooling keeps the
best valid layout seen.

    initial = constructive.place      # a valid seed
    repeat for a seeded budget:
        cand = with prob p_lns:  ruin (remove a related cluster) + recreate (greedy re-insert)
               else:            relocate | swap | reorient
               (only ever a VALID candidate, else skip)
        accept if cheaper, or with prob exp(-d/T)   ; track best-so-far ; cool T

Every accepted state is overlap/bounds/reserved-clean (moves build only valid candidates, and
recreate falls back to a machine's freed origin), so the validator still independently certifies
the output. A **power source** keeps its front face - the reserved external-feed face - flush on
the region boundary through every move (relocate/swap re-orient it back onto a wall when they
can, reorient only offers wall-facing options), the same hard constraint the constructive seed
satisfies and the validator enforces. Deterministic for a given ``seed``. A true place<->route
feedback loop lives in ``solver.core`` (docs/ROADMAP.md lane C + solver).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import (
    FACE_OFFSETS,
    Cell,
    auto_output_faces,
    front_on_boundary,
    in_region,
    occupied_cells,
)
from gtnh_solver.ir.nets import net_sources_sinks, port_direction_map

from .constructive import PlacementResult, _fit, place

#: The six face-adjacent offsets, for growing LNS insertion candidates around placed neighbours.
_FACE_DELTAS = FACE_OFFSETS

# Cost weights. Wirelength (item/fluid HPWL) dominates: it drives auto-output and short pipes.
# The auto-output reward makes orientation matter (the front face carries no I/O, so the wrong
# orientation BLOCKS the free connection - reorient moves are a no-op on the other terms).
# Power nets have no weight here (the module docstring says why); their MST term activates only
# via feedback penalties. There is deliberately NO per-layer penalty: height is only ever paid
# through the volume term.
_W_WIRE = 1.0
_W_AUTO = 4.0

#: The selectable compactness objective. "Compact" is ambiguous and the two metrics pull opposite
#: ways - stacking a layer shrinks the floor but can grow the enclosing box - so the builder
#: picks: ``footprint`` = minimum floor area (stack tall; the default, the maintainer's target),
#: ``volume`` = minimum enclosing box (stay flat/cubic), ``balanced`` = both weighted.
Objective = Literal["footprint", "volume", "balanced"]

#: (footprint weight, volume weight) per objective. Each pure mode drives one term and keeps the
#: other as at most a mild tiebreak so equal winners still prefer the smaller build.
_OBJECTIVE_WEIGHTS: dict[str, tuple[float, float]] = {
    "footprint": (1.0, 0.02),
    "volume": (0.0, 1.0),
    "balanced": (0.5, 0.5),
}

# Annealing schedule (geometric cooling). Budget scales with machine count, clamped.
# _T0 is a fixed initial temperature (not scaled to the cost): at 2.0 a candidate costing +2 is
# still accepted ~e^-1 (37%) of the time, so early iterations explore before cooling tightens.
# Fixed; see #41.
_T0 = 2.0
_ALPHA = 0.995
_MIN_ITERS = 250
_PER_MACHINE = 60
_MAX_ITERS = 6000
_RELOCATE_TRIES = 20

# LNS ruin-and-recreate. ``_P_LNS`` is how often an iteration does a large move instead of a small
# one; ``_MAX_RUIN`` caps the cluster size (kept modest so recreate stays cheap and local, not a
# full re-placement); ``_LNS_RANDOM_CANDIDATES`` adds a few random insertion sites beyond the
# neighbour-adjacent ones so recreate is not purely greedy-local.
_P_LNS = 0.1
_MAX_RUIN = 6
_LNS_RANDOM_CANDIDATES = 8
# Approximate cap on neighbour-adjacent insertion sites so recreate stays cheap on hubs. It is
# checked once per placed neighbour, so one large neighbour's cells can spill a little past it -
# left approximate on purpose (enforcing it mid-neighbour would drop candidates and change results).
_MAX_CANDIDATES = 16

# Small-move mix for a non-LNS iteration: one uniform draw picks relocate below _P_RELOCATE, swap
# below _P_SWAP, else reorient - a ~1/3 : 1/3 : 1/3 split. Named so the mix lives in one place; the
# exact values are load-bearing for per-seed determinism, so keep them if you retune the split.
_P_RELOCATE = 0.34
_P_SWAP = 0.67


#: A routed net as the placement cost sees it: ``(member machine ids, weight)``. Item/fluid nets
#: weigh ``1.0`` plus any feedback penalty; a power net appears here only once a feedback penalty
#: puts it in play, carrying that penalty (module docstring).
_WeightedNet = tuple[list[str], float]


@dataclass(frozen=True)
class _SearchContext:
    """Immutable per-solve context threaded through the neighbourhood + recreate helpers.

    Built once in :func:`optimize_placement`, it bundles the read-only lookups every move shares -
    the machines by id, the bounding region, the reserved cells, the net adjacency, and the
    per-machine net/power/auto views the LNS recreate ranks insertions with - so the helpers take a
    few varying arguments (the placements, the growing occupied set, the rng) instead of threading a
    dozen constants three levels deep. Purely a container: it changes no value the cost computes.
    """

    machines: dict[str, Machine]
    region: CellBox
    reserved: set[Cell]
    adjacency: dict[str, set[str]]
    machine_nets: dict[str, list[_WeightedNet]]
    machine_power: dict[str, list[_WeightedNet]]
    machine_auto: dict[str, list[tuple[str, str]]]


def optimize_placement(
    problem: InputIR,
    *,
    seed: int = 0,
    net_penalties: dict[str, float] | None = None,
    objective: Objective = "footprint",
) -> PlacementResult:
    """Anneal the constructive placement toward a lower routing-aware cost (seeded, validated).

    ``net_penalties`` (net id -> extra weight) boosts a net's wirelength term so its machines pull
    tighter - the place<->route feedback signal: the solver penalizes the nets the router could
    not lay, so the next placement clusters them (shorter routes, or adjacency that auto-outputs).
    ``objective`` selects what "compact" means (:data:`Objective`): minimum floor area
    (``footprint``, the default - stack tall), minimum enclosing box (``volume`` - stay flat), or
    ``balanced`` (both weighted).
    """
    base = place(problem)
    if not base.ok or len(base.placements) < 2:
        return base  # infeasible, or nothing to optimize (0/1 machine)

    machines = {m.id: m for m in problem.machines}
    region = problem.bounding_region
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    penalties = net_penalties or {}
    # Nets that are physically routed (skip ME-toggled): each is (machine ids, weight), where a
    # penalized net weighs more so the optimizer shortens it preferentially. Item/fluid nets pay
    # HPWL. Power nets have NO base term (module docstring says why) - one enters the cost, as
    # an MST trunk-length pull, only once the router fails it and the feedback penalizes it.
    wire_nets: list[_WeightedNet] = []
    power_nets: list[_WeightedNet] = []
    for n in problem.nets:
        if problem.me_toggles.toggled(n.commodity):
            continue
        ids = [e.machine_id for e in n.endpoints]
        if n.commodity is Commodity.POWER:
            if penalties.get(n.id):
                power_nets.append((ids, penalties[n.id]))
        else:
            wire_nets.append((ids, 1.0 + penalties.get(n.id, 0.0)))
    # Directed source->sink pairs that COULD auto-output (simple 1->1 item/fluid nets, like the
    # router's own rule): the cost rewards each pair the current placement+orientation makes
    # face-adjacent, so orientation has a gradient toward enabling the free connection.
    auto_pairs = _auto_candidate_pairs(problem)
    # The immutable per-solve context every move + recreate helper shares, built once and threaded
    # instead of a dozen loose parameters: adjacency (LNS grows a *related* ruin cluster along the
    # net edges), and the per-machine net/power/auto views recreate ranks insertions with cheaply,
    # without a full cost recompute.
    ctx = _SearchContext(
        machines=machines,
        region=region,
        reserved=reserved,
        adjacency=_net_adjacency(problem),
        machine_nets=_machine_nets(problem, wire_nets),
        machine_power=_machine_nets(problem, power_nets),
        machine_auto=_machine_auto(problem, auto_pairs),
    )
    weights = _OBJECTIVE_WEIGHTS[objective]
    rng = random.Random(seed)

    current = list(base.placements)
    # ``current``'s occupied-cell set, maintained incrementally: relocate/swap test a candidate
    # against it (temporarily lifting the moved machine's own cells) instead of rebuilding the whole
    # set per proposal, and each accepted move folds in only its delta (see _apply_occupied_delta).
    occupied = {
        c for p in current for c in occupied_cells(p.cell, machines[p.machine_id].footprint)
    }
    current_cost = _cost(current, machines, wire_nets, power_nets, auto_pairs, weights)
    best, best_cost = current, current_cost
    iters = min(_MAX_ITERS, max(_MIN_ITERS, _PER_MACHINE * len(current)))
    temp = _T0
    for _ in range(iters):
        if rng.random() < _P_LNS:
            cand = _ruin_and_recreate(current, ctx, rng)
        else:
            cand = _move(current, ctx, occupied, rng)
        if cand is not None:
            cand_cost = _cost(cand, machines, wire_nets, power_nets, auto_pairs, weights)
            delta = cand_cost - current_cost
            if delta < 0 or rng.random() < math.exp(-delta / temp):
                _apply_occupied_delta(occupied, current, cand, machines)
                current, current_cost = cand, cand_cost
                if current_cost < best_cost:
                    best, best_cost = current, current_cost
        temp *= _ALPHA
    return PlacementResult(placements=tuple(best))


def _apply_occupied_delta(
    occupied: set[Cell],
    before: list[Placement],
    after: list[Placement],
    machines: dict[str, Machine],
) -> None:
    """Fold an accepted move into ``occupied`` in place, instead of rebuilding it.

    ``before`` and ``after`` list the same machines in the same order, so a differing ``cell`` at an
    index marks a machine that relocated (a pure reorient leaves its cells unchanged). Every vacated
    cell is removed before any new cell is added, so two machines swapping into each other's
    footprints stay occupied. The result is exactly the full-rebuild occupied set of ``after`` -
    every layout the loop holds is overlap-free - only far cheaper to reach."""
    removed: set[Cell] = set()
    added: set[Cell] = set()
    for old_p, new_p in zip(before, after, strict=True):
        if old_p.cell != new_p.cell:
            footprint = machines[old_p.machine_id].footprint
            removed.update(occupied_cells(old_p.cell, footprint))
            added.update(occupied_cells(new_p.cell, footprint))
    occupied.difference_update(removed)
    occupied.update(added)


def _net_adjacency(problem: InputIR) -> dict[str, set[str]]:
    """Machine -> the machines it shares a net with (any commodity), for LNS related removal."""
    adj: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    for net in problem.nets:
        ids = [e.machine_id for e in net.endpoints if e.machine_id in adj]
        for a in ids:
            for b in ids:
                if a != b:
                    adj[a].add(b)
    return adj


def _machine_nets(problem: InputIR, nets: list[_WeightedNet]) -> dict[str, list[_WeightedNet]]:
    """Machine -> the (routed) nets it belongs to, so LNS can score an insertion from only the
    machine's own nets instead of re-summing every net's HPWL."""
    by_machine: dict[str, list[_WeightedNet]] = {m.id: [] for m in problem.machines}
    for entry in nets:
        for mid in entry[0]:
            if mid in by_machine:
                by_machine[mid].append(entry)
    return by_machine


def _machine_auto(
    problem: InputIR, auto_pairs: list[tuple[str, str]]
) -> dict[str, list[tuple[str, str]]]:
    """Machine -> the auto-output candidate pairs it is an endpoint of (either side), so LNS can
    check just the machine's own pairs for the orientation-dependent auto-output reward."""
    by_machine: dict[str, list[tuple[str, str]]] = {m.id: [] for m in problem.machines}
    for pair in auto_pairs:
        for mid in pair:
            if mid in by_machine:
                by_machine[mid].append(pair)
    return by_machine


def _auto_candidate_pairs(problem: InputIR) -> list[tuple[str, str]]:
    """Directed (source, sink) machine pairs for the simple 1->1 item/fluid nets that can
    auto-output - the same nets the router's auto-output assignment covers (power/ME never
    auto-feed). This is the *cheap proxy* of that decision the placement cost keeps so
    orientation still has a gradient; the router is the authority on the final layout.
    """
    port_dir = port_direction_map(problem)
    pairs: list[tuple[str, str]] = []
    for net in problem.nets:
        if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
            continue
        sources, sinks = net_sources_sinks(net, port_dir)
        if len(sources) == 1 and len(sinks) == 1:
            pairs.append((sources[0].machine_id, sinks[0].machine_id))
    return pairs


def _cost(
    placements: list[Placement],
    machines: dict[str, Machine],
    wire_nets: list[_WeightedNet],
    power_nets: list[_WeightedNet],
    auto_pairs: list[tuple[str, str]],
    weights: tuple[float, float],
) -> float:
    """Routing-aware cost: weighted item/fluid HPWL + compactness per the objective, minus an
    auto-output reward (the only orientation-dependent term, so reorient moves are not free),
    plus an MST pull for each feedback-penalized power net.

    Compactness is two independently weighted terms - ``weights`` is the objective's
    ``(footprint weight, volume weight)`` pair (:data:`_OBJECTIVE_WEIGHTS`): the floor area
    (x-span times z-span, shrunk by stacking) and the full bounding-box volume (shrunk by staying
    flat/cubic). ``power_nets`` holds only the nets the router failed and the solver penalized:
    each pays its penalty times the minimum-spanning-tree length over the member centers (a
    shared-amperage trunk is a tree), pulling the net tight until it routes. Un-penalized power
    nets cost nothing here - the real cable cost is judged on routed layouts by the solver
    (module docstring)."""
    pos = {p.machine_id: p for p in placements}
    wire = 0.0
    for machine_ids, weight in wire_nets:
        centers = [_center(pos[mid], machines[mid]) for mid in machine_ids if mid in pos]
        if len(centers) < 2:
            continue
        for axis in range(3):
            coords = [c[axis] for c in centers]
            wire += weight * (max(coords) - min(coords))

    cable = 0.0
    for machine_ids, weight in power_nets:
        centers = [_center(pos[mid], machines[mid]) for mid in machine_ids if mid in pos]
        cable += weight * _mst_length(centers)

    # Bounding box from each footprint's two extreme corners (its origin and origin+size-1) rather
    # than enumerating every occupied cell: for axis-aligned footprints the min/max over the corners
    # equals the min/max over all their cells, so footprint area and volume are bit-identical - but
    # this is O(machines), not O(total cell volume), on the hottest path in the solver.
    min_x = min(p.cell.x for p in placements)
    max_x = max(p.cell.x + machines[p.machine_id].footprint.sx - 1 for p in placements)
    min_y = min(p.cell.y for p in placements)
    max_y = max(p.cell.y + machines[p.machine_id].footprint.sy - 1 for p in placements)
    min_z = min(p.cell.z for p in placements)
    max_z = max(p.cell.z + machines[p.machine_id].footprint.sz - 1 for p in placements)
    footprint = (max_x - min_x + 1) * (max_z - min_z + 1)
    volume = footprint * (max_y - min_y + 1)

    auto = 0
    for source_id, sink_id in auto_pairs:
        sp, tp = pos.get(source_id), pos.get(sink_id)
        if sp is None or tp is None:
            continue
        if (
            auto_output_faces(
                sp.cell,
                machines[source_id].footprint,
                sp.orientation,
                tp.cell,
                machines[sink_id].footprint,
                tp.orientation,
            )
            is not None
        ):
            auto += 1
    w_footprint, w_volume = weights
    return _W_WIRE * wire + cable + w_footprint * footprint + w_volume * volume - _W_AUTO * auto


def _mst_length(centers: list[tuple[float, float, float]]) -> float:
    """Manhattan minimum-spanning-tree length over ``centers`` (Prim, O(n^2); n is a power net's
    member count, so small). The trunk-length proxy for a shared-amperage power net: the router
    grows the trunk as a tree, so its cable count scales with the Steiner tree over the members,
    which the MST approximates from above (within 1.5x for Manhattan metrics)."""
    n = len(centers)
    if n < 2:
        return 0.0
    dist = [_manhattan(centers[0], c) for c in centers]
    in_tree = [False] * n
    in_tree[0] = True
    total = 0.0
    for _ in range(n - 1):
        best_i = min((i for i in range(n) if not in_tree[i]), key=lambda i: dist[i])
        total += dist[best_i]
        in_tree[best_i] = True
        for i in range(n):
            if not in_tree[i]:
                d = _manhattan(centers[best_i], centers[i])
                if d < dist[i]:
                    dist[i] = d
    return total


def _manhattan(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _center(p: Placement, m: Machine) -> tuple[float, float, float]:
    return (
        p.cell.x + m.footprint.sx / 2,
        p.cell.y + m.footprint.sy / 2,
        p.cell.z + m.footprint.sz / 2,
    )


def _feed_ok(machine: Machine, origin: CellCoord, orientation: Facing, region: CellBox) -> bool:
    """Whether placing ``machine`` here honors the power-source feed rule (trivially true for
    non-sources): a source's front face is its reserved external-feed face and must lie flush on
    the region boundary (docs/DOMAIN.md; validator-enforced)."""
    return not machine.is_power_source or front_on_boundary(
        origin, machine.footprint, orientation, region
    )


def _feed_orientation(
    machine: Machine, origin: CellCoord, current: Facing, region: CellBox
) -> Facing | None:
    """The orientation ``machine`` should take at ``origin``: ``current`` when it is legal there,
    else the first option that puts a source's feed face back on the boundary, else ``None``
    (the move cannot place this machine here)."""
    if _feed_ok(machine, origin, current, region):
        return current
    return next(
        (o for o in machine.orientation_options if _feed_ok(machine, origin, o, region)), None
    )


def _move(
    placements: list[Placement],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    """Propose one small move; return a VALID candidate layout, or None if it could not be made.

    ``occupied`` is ``placements``' occupied-cell set (owned by the annealing loop): relocate and
    swap borrow it to test a candidate and restore it before returning, so the caller keeps the
    single incrementally-maintained copy."""
    roll = rng.random()
    if roll < _P_RELOCATE:
        return _relocate(placements, ctx, occupied, rng)
    if roll < _P_SWAP:
        return _swap(placements, ctx, occupied, rng)
    return _reorient(placements, ctx, rng)


def _relocate(
    placements: list[Placement],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    i = rng.randrange(len(placements))
    p = placements[i]
    m = ctx.machines[p.machine_id]
    # Lift machine i's own cells out of the shared occupied set so a candidate may reuse them; the
    # remainder is exactly the other machines' cells (what ``others`` was). Restored in the finally.
    own = set(occupied_cells(p.cell, m.footprint))
    occupied.difference_update(own)
    try:
        for _ in range(_RELOCATE_TRIES):
            origin = _rand_origin(m, ctx.region, rng)
            if origin is None:
                return None
            cells = list(occupied_cells(origin, m.footprint))
            if (
                all(in_region(c, ctx.region) for c in cells)
                and ctx.reserved.isdisjoint(cells)
                and occupied.isdisjoint(cells)
            ):
                orientation = _feed_orientation(m, origin, p.orientation, ctx.region)
                if orientation is None:
                    continue  # a source relocated off the boundary: no legal feed face, keep trying
                new = list(placements)
                new[i] = p.model_copy(update={"cell": origin, "orientation": orientation})
                return new
        return None
    finally:
        occupied.update(own)


def _swap(
    placements: list[Placement],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    i, j = rng.sample(range(len(placements)), 2)
    pi, pj = placements[i], placements[j]
    mi, mj = ctx.machines[pi.machine_id], ctx.machines[pj.machine_id]
    # Lift both machines' current cells so the remainder is the other machines' (what ``others``
    # was); restored in the finally on every exit.
    own = set(occupied_cells(pi.cell, mi.footprint)) | set(occupied_cells(pj.cell, mj.footprint))
    occupied.difference_update(own)
    try:
        moved = list(occupied_cells(pj.cell, mi.footprint)) + list(
            occupied_cells(pi.cell, mj.footprint)
        )
        if (
            not all(in_region(c, ctx.region) for c in moved)
            or len(set(moved)) != len(moved)  # the two swapped bodies overlap each other
            or not ctx.reserved.isdisjoint(moved)
            or not occupied.isdisjoint(moved)
        ):
            return None
        oi = _feed_orientation(mi, pj.cell, pi.orientation, ctx.region)
        oj = _feed_orientation(mj, pi.cell, pj.orientation, ctx.region)
        if oi is None or oj is None:
            return None  # the swap would strand a source's feed face off the boundary
        new = list(placements)
        new[i] = pi.model_copy(update={"cell": pj.cell, "orientation": oi})
        new[j] = pj.model_copy(update={"cell": pi.cell, "orientation": oj})
        return new
    finally:
        occupied.update(own)


def _reorient(
    placements: list[Placement],
    ctx: _SearchContext,
    rng: random.Random,
) -> list[Placement] | None:
    candidates: list[tuple[int, list[Facing]]] = []
    for k, p in enumerate(placements):
        m = ctx.machines[p.machine_id]
        # A source only reorients among feed-legal facings (its front must stay on the boundary).
        alts = [
            o
            for o in m.orientation_options
            if o != p.orientation and _feed_ok(m, p.cell, o, ctx.region)
        ]
        if alts:
            candidates.append((k, alts))
    if not candidates:
        return None
    k, alts = candidates[rng.randrange(len(candidates))]
    p = placements[k]
    new = list(placements)
    new[k] = p.model_copy(update={"orientation": rng.choice(alts)})
    return new


def _ruin_and_recreate(
    placements: list[Placement],
    ctx: _SearchContext,
    rng: random.Random,
) -> list[Placement] | None:
    """Ruin a related cluster of machines and greedily re-insert them (the LNS large move).

    Removes a net-connected cluster (2..``_MAX_RUIN`` machines), then re-inserts each at the
    position + orientation minimising its marginal cost, preferring cells beside its already-placed
    net-neighbours. Every insertion is validity-checked, so the result is a complete,
    overlap/bounds/reserved-clean placement in the original machine order - or ``None`` if a
    machine cannot be re-placed at all (a freed origin can be retaken by an earlier re-insert), in
    which case the caller just skips the move.
    """
    n = len(placements)
    if n < 2:
        return None
    ruined = _related_cluster(placements, ctx.adjacency, rng.randint(2, min(n, _MAX_RUIN)), rng)
    kept = [p for i, p in enumerate(placements) if i not in ruined]

    occupied: set[Cell] = set()
    for p in kept:
        occupied.update(occupied_cells(p.cell, ctx.machines[p.machine_id].footprint))

    placed = list(kept)
    placed_ids = {p.machine_id for p in placed}
    # Re-insert the most-constrained first (most already-placed net-neighbours) so the strongest
    # pulls choose their spot before the freer machines fill in around them.
    to_insert = sorted(
        (placements[i] for i in sorted(ruined)),
        key=lambda p: -_placed_neighbor_count(p.machine_id, ctx.adjacency, placed_ids),
    )
    for p in to_insert:
        m = ctx.machines[p.machine_id]
        spot = _best_insertion(p, placed, occupied, ctx, rng)
        if spot is None:
            return None  # could not re-place this machine; abandon the move, the loop skips it
        origin, orientation = spot
        placed.append(p.model_copy(update={"cell": origin, "orientation": orientation}))
        placed_ids.add(p.machine_id)
        occupied.update(occupied_cells(origin, m.footprint))

    by_id = {p.machine_id: p for p in placed}
    return [by_id[p.machine_id] for p in placements]  # preserve the original ordering


def _related_cluster(
    placements: list[Placement], adjacency: dict[str, set[str]], k: int, rng: random.Random
) -> set[int]:
    """Indices of a net-connected cluster of ``k`` machines grown from a random seed; padded with
    random machines when the seed's net-component is smaller than ``k`` (e.g. isolated machines)."""
    idx_of = {p.machine_id: i for i, p in enumerate(placements)}
    seed_i = rng.randrange(len(placements))
    chosen = {seed_i}
    frontier = [placements[seed_i].machine_id]
    while len(chosen) < k and frontier:
        neighbors = sorted(adjacency.get(frontier.pop(0), set()))
        rng.shuffle(neighbors)
        for nb in neighbors:
            if len(chosen) >= k:
                break
            j = idx_of.get(nb)
            if j is not None and j not in chosen:
                chosen.add(j)
                frontier.append(nb)
    if len(chosen) < k:  # the seed's component is smaller than k: pad with random other machines
        rest = [i for i in range(len(placements)) if i not in chosen]
        rng.shuffle(rest)
        chosen.update(rest[: k - len(chosen)])
    return chosen


def _placed_neighbor_count(
    machine_id: str, adjacency: dict[str, set[str]], placed_ids: set[str]
) -> int:
    """How many of ``machine_id``'s net-neighbours are already placed (its re-insertion priority)."""
    return sum(1 for nb in adjacency.get(machine_id, set()) if nb in placed_ids)


def _best_insertion(
    p: Placement,
    placed: list[Placement],
    occupied: set[Cell],
    ctx: _SearchContext,
    rng: random.Random,
) -> tuple[CellCoord, Facing] | None:
    """The valid (origin, orientation) for ``p``'s machine that minimises its *marginal* cost, over
    candidate cells beside its placed net-neighbours plus a few random ones; falls back to any
    first-fit free slot, or ``None`` if the machine cannot be placed at all.

    Ranking uses only the terms that depend on this machine (its nets + auto pairs), so an insertion
    costs O(machine degree), not a full O(all nets) recompute - the loop's ``_cost`` still gates
    acceptance globally.
    """
    m = ctx.machines[p.machine_id]
    placed_pos = {q.machine_id: q for q in placed}
    best: tuple[CellCoord, Facing] | None = None
    best_cost = math.inf
    for origin in _candidate_origins(p, m, placed, ctx, rng):
        cells = list(occupied_cells(origin, m.footprint))
        if (
            not all(in_region(c, ctx.region) for c in cells)
            or not ctx.reserved.isdisjoint(cells)
            or not occupied.isdisjoint(cells)
        ):
            continue
        for orientation in m.orientation_options:
            if not _feed_ok(m, origin, orientation, ctx.region):
                continue  # a source's feed face must stay on the boundary
            cost = _marginal_insertion_cost(p.machine_id, origin, orientation, m, placed_pos, ctx)
            if cost < best_cost:
                best_cost, best = cost, (origin, orientation)
    if best is not None:
        return best
    # Last resort: any free slot in first-fit order (a source additionally requires a slot +
    # orientation with its feed face on the boundary - the same rule the constructive seed used).
    return _fit(m, ctx.region, occupied | ctx.reserved)


def _marginal_insertion_cost(
    machine_id: str,
    origin: CellCoord,
    orientation: Facing,
    m: Machine,
    placed_pos: dict[str, Placement],
    ctx: _SearchContext,
) -> float:
    """The cost terms that change with where ``machine_id`` goes: the weighted HPWL of its own
    item/fluid nets over their already-placed members (this candidate included), plus for each of
    its feedback-penalized power nets the Manhattan distance to the nearest placed member (the
    increment Prim would pay to attach this machine to the trunk MST), minus the auto-output
    reward for the pairs the candidate makes face-adjacent. A cheap marginal proxy of ``_cost``
    for ranking candidate insertions; the annealing loop's full ``_cost`` still gates acceptance
    (the footprint/volume terms, which this per-machine view cannot see, included)."""
    cx = origin.x + m.footprint.sx / 2
    cy = origin.y + m.footprint.sy / 2
    cz = origin.z + m.footprint.sz / 2
    wire = 0.0
    for ids, weight in ctx.machine_nets[machine_id]:
        xs, ys, zs = [cx], [cy], [cz]
        for mid in ids:
            if mid != machine_id and mid in placed_pos:
                ox, oy, oz = _center(placed_pos[mid], ctx.machines[mid])
                xs.append(ox)
                ys.append(oy)
                zs.append(oz)
        if len(xs) > 1:
            wire += weight * (max(xs) - min(xs) + max(ys) - min(ys) + max(zs) - min(zs))
    cable = 0.0
    for ids, weight in ctx.machine_power[machine_id]:
        attach = min(
            (
                _manhattan((cx, cy, cz), _center(placed_pos[mid], ctx.machines[mid]))
                for mid in ids
                if mid != machine_id and mid in placed_pos
            ),
            default=0.0,
        )
        cable += weight * attach
    auto = 0
    for src, sink in ctx.machine_auto[machine_id]:
        other = sink if src == machine_id else src
        op = placed_pos.get(other)
        if op is None:
            continue
        om = ctx.machines[other]
        if src == machine_id:
            faces = auto_output_faces(
                origin, m.footprint, orientation, op.cell, om.footprint, op.orientation
            )
        else:
            faces = auto_output_faces(
                op.cell, om.footprint, op.orientation, origin, m.footprint, orientation
            )
        if faces is not None:
            auto += 1
    return _W_WIRE * wire + cable - _W_AUTO * auto


def _candidate_origins(
    p: Placement,
    m: Machine,
    placed: list[Placement],
    ctx: _SearchContext,
    rng: random.Random,
) -> list[CellCoord]:
    """Origins to try inserting ``m`` at: its freed origin, the cells face-adjacent to each placed
    net-neighbour (to cluster / auto-output), and a few random free origins - deduped, in-region."""
    neighbor_ids = ctx.adjacency.get(p.machine_id, set())
    origins: list[CellCoord] = []
    seen: set[Cell] = set()

    def add(c: Cell) -> None:
        if c not in seen and in_region(c, ctx.region):
            seen.add(c)
            origins.append(CellCoord(x=c[0], y=c[1], z=c[2]))

    add((p.cell.x, p.cell.y, p.cell.z))  # freed origin (may be retaken; validity checked by caller)
    for q in placed:
        if len(origins) >= _MAX_CANDIDATES:
            break  # enough neighbour-adjacent sites; keep recreate cheap
        if q.machine_id in neighbor_ids:
            for bx, by, bz in occupied_cells(q.cell, ctx.machines[q.machine_id].footprint):
                for dx, dy, dz in _FACE_DELTAS:
                    add((bx + dx, by + dy, bz + dz))
    for _ in range(_LNS_RANDOM_CANDIDATES):
        origin = _rand_origin(m, ctx.region, rng)
        if origin is not None:
            add((origin.x, origin.y, origin.z))
    return origins


def _rand_origin(m: Machine, region: CellBox, rng: random.Random) -> CellCoord | None:
    fp = m.footprint
    if fp.sx > region.sx or fp.sy > region.sy or fp.sz > region.sz:
        return None
    return CellCoord(
        x=rng.randrange(region.sx - fp.sx + 1),
        y=rng.randrange(region.sy - fp.sy + 1),
        z=rng.randrange(region.sz - fp.sz + 1),
    )
