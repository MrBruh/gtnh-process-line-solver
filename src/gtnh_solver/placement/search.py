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
tap, yet needs more cable). Re-measured under #123 at weights down to 0.1, the shipped sand line's
cable still went UP at every one (3 -> 4..6 cells), so this stands. The real per-segment cable cost
is judged where it is knowable, on a routed layout: the solver's feedback loop routes each
candidate placement and keeps the best by (footprint, cable cells, volume), and its
``solver.repair`` pass relocates each power source by really routing every candidate cell around
the sinks it feeds - which is how a source gets positioned without a proxy having to guess.
What remains here is the rescue path - a power net the router could NOT lay gets a feedback
penalty, which switches on a minimum-spanning-tree pull over the net's members (a shared-amperage
trunk is a tree) until it routes.

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
from typing import Literal, NamedTuple

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
    box_in_region,
    front_on_boundary,
    in_region,
    occupied_cells,
    rotated_footprint,
)
from gtnh_solver.ir.nets import net_sources_sinks, port_direction_map
from gtnh_solver.router.auto import auto_output_possible

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
_Centroid = tuple[float, float, float]


class _NetBox(NamedTuple):
    """One wire net's HPWL bounding box over the members already placed, and the net's weight.

    The half-perimeter of a net is the bounding box of *all* its members' centroids. During an
    insertion every member but the candidate is fixed, so their box is the same for every
    candidate origin and orientation: precompute it once and each candidate only has to widen it.
    """

    weight: float
    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float


class _PowerAttach(NamedTuple):
    """One penalized power net's weight and the centroids of its already-placed members.

    Unlike :class:`_NetBox` this cannot collapse to a box - the term is the distance to the
    *nearest* member (the increment Prim would pay), not a span - but the centroids themselves are
    still invariant across candidates, so they are computed once rather than per candidate.
    """

    weight: float
    centroids: list[_Centroid]


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
    machine_auto: dict[str, list[_AutoPair]]


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
        c
        for p in current
        for c in occupied_cells(p.cell, machines[p.machine_id].footprint, p.orientation)
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

    ``before`` and ``after`` hold the same machines but not necessarily in the same order (an
    accepted LNS move lists the kept machines first and the reinserted ones last), so the diff is
    keyed by machine id: a machine whose **pose** changed vacates its old footprint and claims the
    new one. Pose, not cell: a reorient leaves the cell alone and still moves the cells a non-cubic
    machine covers, so keying on the cell would leave ``occupied`` drifting out of sync with the
    layout for the rest of the anneal. Every vacated cell is removed before any new cell is added,
    so two machines swapping into each other's footprints stay occupied. The result is exactly the
    full-rebuild occupied set of ``after`` - every layout the loop holds is overlap-free - only far
    cheaper to reach."""
    before_pose = {p.machine_id: (p.cell, p.orientation) for p in before}
    removed: set[Cell] = set()
    added: set[Cell] = set()
    for new_p in after:
        old_cell, old_orientation = before_pose[new_p.machine_id]
        if (old_cell, old_orientation) != (new_p.cell, new_p.orientation):
            footprint = machines[new_p.machine_id].footprint
            removed.update(occupied_cells(old_cell, footprint, old_orientation))
            added.update(occupied_cells(new_p.cell, footprint, new_p.orientation))
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


def _machine_auto(problem: InputIR, auto_pairs: list[_AutoPair]) -> dict[str, list[_AutoPair]]:
    """Machine -> the auto-output candidates it is an endpoint of (either side), so LNS can
    check just the machine's own pairs for the orientation-dependent auto-output reward."""
    by_machine: dict[str, list[_AutoPair]] = {m.id: [] for m in problem.machines}
    for pair in auto_pairs:
        for mid in (pair.source_id, pair.sink_id):
            if mid in by_machine:
                by_machine[mid].append(pair)
    return by_machine


class _AutoPair(NamedTuple):
    """One directed auto-output candidate: which port on which machine, on each side.

    The ports are the part that used to be dropped. They decide which casing cells could host the
    two hatches, and therefore whether the connection is possible at all on a multiblock, so a
    (machine, machine) pair cannot answer the question the router actually asks (#107).
    """

    source_id: str
    source_port: str
    sink_id: str
    sink_port: str


def _auto_candidate_pairs(problem: InputIR) -> list[_AutoPair]:
    """Directed auto-output candidates for the simple 1->1 item/fluid nets - the same nets the
    router's auto-output assignment covers (power/ME never auto-feed).

    Which of them is *possible* is then asked of the router itself
    (``router.auto.auto_output_possible``), so the reward and the decision share one rule.
    """
    port_dir = port_direction_map(problem)
    pairs: list[_AutoPair] = []
    for net in problem.nets:
        if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
            continue
        sources, sinks = net_sources_sinks(net, port_dir)
        if len(sources) == 1 and len(sinks) == 1:
            pairs.append(
                _AutoPair(
                    sources[0].machine_id,
                    sources[0].port_id,
                    sinks[0].machine_id,
                    sinks[0].port_id,
                )
            )
    return pairs


def _cost(
    placements: list[Placement],
    machines: dict[str, Machine],
    wire_nets: list[_WeightedNet],
    power_nets: list[_WeightedNet],
    auto_pairs: list[_AutoPair],
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
    nets cost nothing here - the real cable cost is judged on routed layouts by the solver, whose
    repair pass also places the sources themselves (module docstring)."""
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
    # Rotated extents: a turned non-cubic machine reaches a different distance along each axis, so
    # the declared footprint would misreport the floor area and volume these objectives rank on.
    boxes = [
        (p, rotated_footprint(machines[p.machine_id].footprint, p.orientation)) for p in placements
    ]
    min_x = min(p.cell.x for p, _ in boxes)
    max_x = max(p.cell.x + b.sx - 1 for p, b in boxes)
    min_y = min(p.cell.y for p, _ in boxes)
    max_y = max(p.cell.y + b.sy - 1 for p, b in boxes)
    min_z = min(p.cell.z for p, _ in boxes)
    max_z = max(p.cell.z + b.sz - 1 for p, b in boxes)
    footprint = (max_x - min_x + 1) * (max_z - min_z + 1)
    volume = footprint * (max_y - min_y + 1)

    auto = 0
    for pair in auto_pairs:
        sp, tp = pos.get(pair.source_id), pos.get(pair.sink_id)
        if sp is None or tp is None:
            continue
        if auto_output_possible(
            sp,
            machines[pair.source_id],
            pair.source_port,
            tp,
            machines[pair.sink_id],
            pair.sink_port,
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
    # Rotated: a wrong centroid feeds HPWL and the power MST, so it would steer the search.
    box = rotated_footprint(m.footprint, p.orientation)
    return (
        p.cell.x + box.sx / 2,
        p.cell.y + box.sy / 2,
        p.cell.z + box.sz / 2,
    )


def _feed_ok(machine: Machine, origin: CellCoord, orientation: Facing, region: CellBox) -> bool:
    """Whether placing ``machine`` here honors the power-source feed rule (trivially true for
    non-sources): a source's front face is its reserved external-feed face and must lie flush on
    the region boundary (docs/DOMAIN.md; validator-enforced)."""
    return _feed_ok_for(machine.is_power_source, machine, origin, orientation, region)


def _feed_ok_for(
    is_source: bool, machine: Machine, origin: CellCoord, orientation: Facing, region: CellBox
) -> bool:
    """:func:`_feed_ok` with the source test already answered, for callers that ask in a loop.

    ``Machine.is_power_source`` is a Pydantic property that rescans ``faces.ports`` on every read,
    and it depends on the machine alone - never on where the machine is put. ``_best_insertion``
    asks it once per (origin, orientation) pair, which measured at ~4% of a nitrobenzene solve for
    an answer that cannot change inside the loop. Splitting the flag out keeps the rule itself in
    one place rather than inlining ``front_on_boundary`` at the hot call site.
    """
    return not is_source or front_on_boundary(origin, machine.footprint, orientation, region)


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
    return _reorient(placements, ctx, occupied, rng)


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
    own = set(occupied_cells(p.cell, m.footprint, p.orientation))
    occupied.difference_update(own)
    try:
        for _ in range(_RELOCATE_TRIES):
            origin = _rand_origin(m, ctx.region, p.orientation, rng)
            if origin is None:
                return None
            # Orientation first: which cells a turned non-cubic machine covers depends on it, so
            # the fit test cannot run before it is known. _feed_orientation draws no randomness, so
            # hoisting it above the test leaves the RNG trajectory (and every existing layout)
            # exactly as it was.
            orientation = _feed_orientation(m, origin, p.orientation, ctx.region)
            if orientation is None:
                continue  # a source relocated off the boundary: no legal feed face, keep trying
            if not box_in_region(origin, m.footprint, orientation, ctx.region):
                continue  # the body would hang off the region: cheaper to reject than to expand
            cells = list(occupied_cells(origin, m.footprint, orientation))
            if ctx.reserved.isdisjoint(cells) and occupied.isdisjoint(cells):
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
    own = set(occupied_cells(pi.cell, mi.footprint, pi.orientation)) | set(
        occupied_cells(pj.cell, mj.footprint, pj.orientation)
    )
    occupied.difference_update(own)
    try:
        # Orientation first, as in _relocate: each swapped body's cells depend on the orientation
        # it lands with, and neither call draws randomness.
        oi = _feed_orientation(mi, pj.cell, pi.orientation, ctx.region)
        oj = _feed_orientation(mj, pi.cell, pj.orientation, ctx.region)
        if oi is None or oj is None:
            return None  # the swap would strand a source's feed face off the boundary
        # Each body's own in-region test first, off the boxes: a swap that lands a bigger machine
        # in a smaller one's slot usually fails right here, and then neither body is ever expanded.
        if not box_in_region(pj.cell, mi.footprint, oi, ctx.region) or not box_in_region(
            pi.cell, mj.footprint, oj, ctx.region
        ):
            return None
        moved = list(occupied_cells(pj.cell, mi.footprint, oi)) + list(
            occupied_cells(pi.cell, mj.footprint, oj)
        )
        if (
            len(set(moved)) != len(moved)  # the two swapped bodies overlap each other
            or not ctx.reserved.isdisjoint(moved)
            or not occupied.isdisjoint(moved)
        ):
            return None
        new = list(placements)
        new[i] = pi.model_copy(update={"cell": pj.cell, "orientation": oi})
        new[j] = pj.model_copy(update={"cell": pi.cell, "orientation": oj})
        return new
    finally:
        occupied.update(own)


def _turn_fits(
    m: Machine, p: Placement, orientation: Facing, ctx: _SearchContext, occupied: set[Cell]
) -> bool:
    """Whether ``m`` still fits at its own origin once turned to ``orientation``.

    Short-circuits the common case: a turn that leaves the extents alone cannot change which cells
    are covered, so every 1x1x1 block and every square-base multiblock skips the test and the hot
    path is untouched.
    """
    if rotated_footprint(m.footprint, orientation) == rotated_footprint(m.footprint, p.orientation):
        return True
    if not box_in_region(p.cell, m.footprint, orientation, ctx.region):
        return False  # the turn swings the body out of the region; no need to expand either set
    own = set(occupied_cells(p.cell, m.footprint, p.orientation))
    cells = list(occupied_cells(p.cell, m.footprint, orientation))
    return ctx.reserved.isdisjoint(cells) and (occupied - own).isdisjoint(cells)


def _reorient(
    placements: list[Placement],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    candidates: list[tuple[int, list[Facing]]] = []
    for k, p in enumerate(placements):
        m = ctx.machines[p.machine_id]
        # A source only reorients among feed-legal facings (its front must stay on the boundary),
        # and ANY machine only among facings it still fits at. A quarter turn swaps a non-cubic
        # machine's horizontal extents, so a turn can push it out of the region, onto a reserved
        # cell or into a neighbour. Nothing checked that while rotation was a no-op, and an
        # accepted state that broke it would violate this loop's overlap-free invariant in silence.
        alts = [
            o
            for o in m.orientation_options
            if o != p.orientation
            and _feed_ok(m, p.cell, o, ctx.region)
            and _turn_fits(m, p, o, ctx, occupied)
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
        occupied.update(occupied_cells(p.cell, ctx.machines[p.machine_id].footprint, p.orientation))
    # One grid for the whole recreate, updated in step with `occupied` as each machine lands,
    # rather than rebuilt per insertion: `_best_insertion` only reads it.
    grid = _occupancy_grid(ctx.region, occupied, ctx.reserved)
    row, plane = ctx.region.sx, ctx.region.sx * ctx.region.sy

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
        spot = _best_insertion(p, placed, occupied, grid, ctx, rng)
        if spot is None:
            return None  # could not re-place this machine; abandon the move, the loop skips it
        origin, orientation = spot
        placed.append(p.model_copy(update={"cell": origin, "orientation": orientation}))
        placed_ids.add(p.machine_id)
        for cell in occupied_cells(origin, m.footprint, orientation):
            occupied.add(cell)
            grid[cell[0] + cell[1] * row + cell[2] * plane] = 1

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


def _placed_invariants(
    machine_id: str, placed_pos: dict[str, Placement], ctx: _SearchContext
) -> tuple[list[_NetBox], list[_PowerAttach]]:
    """Everything :func:`_marginal_insertion_cost` needs that does NOT depend on the candidate.

    An insertion evaluates ~50 (origin, orientation) pairs, and the wire and cable terms were
    re-deriving the same already-placed centroids for every one of them. Both terms reduce to a
    fixed summary of the placed members - a bounding box for HPWL, the centroid list for the MST
    pull - so this computes each once per insertion and the candidate loop just reads them.
    """
    centroids = {mid: _center(q, ctx.machines[mid]) for mid, q in placed_pos.items()}

    def placed_centroids(ids: list[str]) -> list[_Centroid]:
        return [centroids[mid] for mid in ids if mid != machine_id and mid in placed_pos]

    net_boxes = []
    for ids, weight in ctx.machine_nets[machine_id]:
        pts = placed_centroids(ids)
        if not pts:
            continue  # nothing placed to span yet: the net costs nothing until one of them is
        net_boxes.append(
            _NetBox(
                weight,
                min(c[0] for c in pts),
                max(c[0] for c in pts),
                min(c[1] for c in pts),
                max(c[1] for c in pts),
                min(c[2] for c in pts),
                max(c[2] for c in pts),
            )
        )
    power = [
        _PowerAttach(weight, placed_centroids(ids)) for ids, weight in ctx.machine_power[machine_id]
    ]
    return net_boxes, power


def _occupancy_grid(region: CellBox, occupied: set[Cell], reserved: set[Cell]) -> bytearray:
    """``occupied | reserved`` as a flat byte per region cell, indexed ``x + y*sx + z*sx*sy``.

    The fit test in :func:`_best_insertion` asked ``isdisjoint`` of a freshly materialised cell
    list, which meant building one tuple per cell of the body per candidate. That is cheap when a
    machine is 1x1x1 and ruinous when the real dataset gives it a 7x7x7 body: it was 90% of all
    ``occupied_cells`` yields in a solve and the single hottest line in the solver. A byte grid
    answers the same question by indexing, with no allocation and no hashing per candidate.

    Unpadded on purpose. ``box_in_region`` already gates every test with six comparisons on the
    rotated box's corners, so an index built from a passing origin is always in range; a padded
    border would buy a bounds check that has already been paid for.
    """
    sx, sy, sz = region.sx, region.sy, region.sz
    plane = sx * sy
    grid = bytearray(plane * sz)
    for x, y, z in occupied:
        grid[x + y * sx + z * plane] = 1  # every placed body cleared box_in_region to get here
    for x, y, z in reserved:  # caller-supplied, so this one is guarded
        if 0 <= x < sx and 0 <= y < sy and 0 <= z < sz:
            grid[x + y * sx + z * plane] = 1
    return grid


def _box_offsets(box: CellBox, region: CellBox) -> tuple[int, ...]:
    """Flat offsets from an origin to every cell a rotated ``box`` covers, in this region."""
    sx, plane = region.sx, region.sx * region.sy
    return tuple(
        dx + dy * sx + dz * plane
        for dz in range(box.sz)
        for dy in range(box.sy)
        for dx in range(box.sx)
    )


def _best_insertion(
    p: Placement,
    placed: list[Placement],
    occupied: set[Cell],
    grid: bytearray,
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
    is_source = m.is_power_source  # fixed for this machine; see _feed_ok_for
    placed_pos = {q.machine_id: q for q in placed}
    net_boxes, power_attach = _placed_invariants(p.machine_id, placed_pos, ctx)
    region = ctx.region
    offsets_by_box: dict[tuple[int, int, int], tuple[int, ...]] = {}
    row, plane = region.sx, region.sx * region.sy
    best: tuple[CellCoord, Facing] | None = None
    best_cost = math.inf
    for origin in _candidate_origins(p, m, placed, ctx, rng):
        # The fit test moved inside the orientation loop, because which cells the machine covers
        # depends on how it is turned - but it depends ONLY on the rotated box, and four facings
        # yield at most two of those. Memoizing per box keeps this at one test per origin for a
        # square-base machine (every machine in both shipped examples) instead of four.
        fits: dict[tuple[int, int, int], bool] = {}
        for orientation in m.orientation_options:
            if not _feed_ok_for(is_source, m, origin, orientation, ctx.region):
                continue  # a source's feed face must stay on the boundary
            box = rotated_footprint(m.footprint, orientation)
            key = (box.sx, box.sy, box.sz)
            ok = fits.get(key)
            if ok is None:
                # In-region off the box first: this is the hottest fit test in the solve (one per
                # candidate origin, and _candidate_origins offers plenty that hang off the region),
                # and only a candidate that clears it is worth expanding into cells.
                ok = box_in_region(origin, m.footprint, orientation, ctx.region)
                if ok:
                    offsets = offsets_by_box.get(key)
                    if offsets is None:
                        offsets = offsets_by_box[key] = _box_offsets(box, region)
                    base = origin.x + origin.y * row + origin.z * plane
                    for off in offsets:
                        if grid[base + off]:
                            ok = False
                            break
                fits[key] = ok
            if not ok:
                continue
            cost = _marginal_insertion_cost(
                p.machine_id,
                origin,
                orientation,
                m,
                placed_pos,
                net_boxes,
                power_attach,
                ctx,
                bound=best_cost,
            )
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
    net_boxes: list[_NetBox],
    power_attach: list[_PowerAttach],
    ctx: _SearchContext,
    bound: float = math.inf,
) -> float:
    """The cost terms that change with where ``machine_id`` goes: the weighted HPWL of its own
    item/fluid nets over their already-placed members (this candidate included), plus for each of
    its feedback-penalized power nets the Manhattan distance to the nearest placed member (the
    increment Prim would pay to attach this machine to the trunk MST), minus the auto-output
    reward for the pairs the candidate makes face-adjacent. A cheap marginal proxy of ``_cost``
    for ranking candidate insertions; the annealing loop's full ``_cost`` still gates acceptance
    (the footprint/volume terms, which this per-machine view cannot see, included).

    ``net_boxes`` and ``power_attach`` come from :func:`_placed_invariants` and summarise the
    members that are already placed - the part of both terms that is the same for every candidate.
    Only the auto reward is irreducibly per-candidate: face adjacency depends on this origin and
    orientation, which is what the term is there to measure.

    ``bound`` is the incumbent's cost, and returning ``inf`` above it is a pure early-out: the
    caller only keeps a strictly cheaper candidate, so a candidate that provably cannot get there
    need not be scored exactly. **The bound has to account for the auto reward being subtracted.**
    The running ``wire + cable`` total is an *upper* bound on the result, not a lower one, so
    comparing it against ``bound`` directly would discard candidates the reward would have made
    best. Subtracting the largest reward still available - every remaining pair scoring - makes the
    test admissible, and the layouts it produces are identical to scoring every candidate in full.
    What it skips is the auto term entirely: the ``Placement`` this function would have to build
    to ask with, and a ``router.auto.auto_output_possible`` call per pair. That rule got
    substantially dearer when it started asking the question the router actually answers (#107),
    which is what makes the early-out worth having - it takes a nitrobenzene solve from 6.32s to
    4.36s."""
    box = rotated_footprint(m.footprint, orientation)  # same centroid rule as _center
    cx = origin.x + box.sx / 2
    cy = origin.y + box.sy / 2
    cz = origin.z + box.sz / 2
    # Widening the precomputed box with this candidate's centroid *is* the HPWL, so the span is
    # identical to taking min/max over the members plus the candidate - the same two operands,
    # just not rebuilt per candidate. Inlined conditionals rather than min()/max(): this runs
    # ~236k times a solve and the call overhead was measurable.
    wire = 0.0
    for weight, x0, x1, y0, y1, z0, z1 in net_boxes:
        wire += weight * (
            (cx if cx > x1 else x1)
            - (cx if cx < x0 else x0)
            + (cy if cy > y1 else y1)
            - (cy if cy < y0 else y0)
            + (cz if cz > z1 else z1)
            - (cz if cz < z0 else z0)
        )
    cable = 0.0
    for weight, centroids in power_attach:
        attach = min((_manhattan((cx, cy, cz), c) for c in centroids), default=0.0)
        cable += weight * attach
    pairs = ctx.machine_auto[machine_id]
    if _W_WIRE * wire + cable - _W_AUTO * len(pairs) >= bound:
        return math.inf  # even every pair scoring cannot beat the incumbent
    auto = 0
    here = Placement(machine_id=machine_id, cell=origin, orientation=orientation)
    for pair in pairs:
        is_source = pair.source_id == machine_id
        other = pair.sink_id if is_source else pair.source_id
        op = placed_pos.get(other)
        if op is None:
            continue
        om = ctx.machines[other]
        if is_source:
            possible = auto_output_possible(here, m, pair.source_port, op, om, pair.sink_port)
        else:
            possible = auto_output_possible(op, om, pair.source_port, here, m, pair.sink_port)
        if possible:
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
            for bx, by, bz in occupied_cells(
                q.cell, ctx.machines[q.machine_id].footprint, q.orientation
            ):
                for dx, dy, dz in _FACE_DELTAS:
                    add((bx + dx, by + dy, bz + dz))
    for _ in range(_LNS_RANDOM_CANDIDATES):
        origin = _rand_origin(m, ctx.region, p.orientation, rng)
        if origin is not None:
            add((origin.x, origin.y, origin.z))
    return origins


def _rand_origin(
    m: Machine, region: CellBox, orientation: Facing, rng: random.Random
) -> CellCoord | None:
    # The rotated extents, not the declared ones: an east-facing 5x1x2 needs 2 of x and 5 of z, so
    # bounding by the declared box would both reject origins that fit and offer origins that do
    # not - and the draws below consume randomness, so a wrong bound shifts every later draw.
    fp = rotated_footprint(m.footprint, orientation)
    if fp.sx > region.sx or fp.sy > region.sy or fp.sz > region.sz:
        return None
    return CellCoord(
        x=rng.randrange(region.sx - fp.sx + 1),
        y=rng.randrange(region.sy - fp.sy + 1),
        z=rng.randrange(region.sz - fp.sz + 1),
    )
