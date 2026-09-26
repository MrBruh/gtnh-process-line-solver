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
local optima single-cell moves plateau in. The re-insertion prices compactness too (how much a
spot grows the build), which is what lets it fold the long strip the first-fit start lays a big
multiblock line out in (#254). Metropolis acceptance with geometric cooling keeps the best valid
layout seen.

    initial = constructive.place      # a valid seed
    repeat for a seeded budget:
        cand = with prob p_lns:  ruin (remove a related cluster) + recreate (greedy re-insert,
                                 priced on nets, auto-output and how much it grows the build)
               else:            relocate | nudge (single-block lines only) | swap | reorient
               (only ever a VALID candidate, else skip)
        accept if cheaper, or with prob exp(-d/T)   ; track best-so-far ; cool T

**The nudge** shifts one machine by one cell. Relocate draws a cell anywhere in the region, which
almost never lands anywhere useful (under 2% of relocates are accepted), so without a nudge the
search has no way to slide a machine along a wall or tuck it into a gap beside its neighbours. On a
line of single blocks the nudge takes half of relocate's share, and on parallel-sand with the bank
template off it improves 15 of 16 solves (median box 128 to 92). On a line with any multiblock it
is left out and the mix is exactly as it was: there it measured as noise at best, and nudging every
machine cost nitrobenzene a valid layout on one seed in eight (#246 saw the same on
ev-nitrobenzene).

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
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from itertools import product
from types import MappingProxyType
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
    FACE_DELTAS,
    FACE_OFFSETS,
    Cell,
    Pose,
    Size,
    box_cells,
    box_front_on_boundary,
    box_within,
    pose_of,
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
#: Weight on the face-shortfall term. Large on purpose, and only safe to be large because the
#: term no longer charges auto-output ports for cells they do not use: with that phantom gone it
#: reads zero on every layout that is genuinely fine, so it costs those nothing at all.
#:
#: Sized by measurement, not taste. The parallel line needs the equivalent of ~9x the old weight
#: of 8 before the placer will stop packing its nine machines into a wall, and pushing past that
#: buys nothing: at 48 it still lays 60 pipe cells, at 72 it lays 27, at 120 it drifts back up.
#: Re-measure over the solver's whole seed grid, never one seed, before changing it.
_W_FACES = 8.0

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
#: On a line of single blocks only, relocate keeps the draws below this and the one-cell nudge
#: takes the rest of its share, up to ``_P_RELOCATE`` (module docstring). Half of relocate's share.
_P_RELOCATE_WHEN_NUDGING = 0.17


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


class _Extent(NamedTuple):
    """The box the machines already placed span, in whole cells, bounds inclusive.

    What an insertion is measured against for compactness: the candidate grows it or it does not.
    Cells rather than centroids, because the floor area and volume ``_cost`` ranks on are counted
    in cells.
    """

    x0: int
    x1: int
    y0: int
    y1: int
    z0: int
    z1: int


class _PowerAttach(NamedTuple):
    """One penalized power net's weight and the centroids of its already-placed members.

    Unlike :class:`_NetBox` this cannot collapse to a box - the term is the distance to the
    *nearest* member (the increment Prim would pay), not a span - but the centroids themselves are
    still invariant across candidates, so they are computed once rather than per candidate.
    """

    weight: float
    centroids: list[_Centroid]


@dataclass(frozen=True, slots=True)
class _Body:
    """What the search reads of one machine, as plain values, gathered once per solve.

    ``Machine`` is a pydantic model, and on Python 3.14+ every field read off one takes the slow,
    unspecialized path (#256), while the search reads a machine's footprint, facings and ports per
    candidate and per cost evaluation. So it reads them from here instead. ``is_power_source`` is
    the extreme case: a property that rescans the machine's ports on every read, for an answer that
    never changes during a solve.

    ``machine`` is kept for the callers that genuinely need the model: the auto-output rule, which
    keys its caches on it, and the first-fit fallback.
    """

    machine: Machine
    #: The footprint as it sits facing each way (:func:`rotated_footprint`), for every facing.
    sizes: Mapping[Facing, Size]
    #: :func:`_shell_offsets` of each of those sizes, facing that way.
    shells: Mapping[Facing, tuple[Cell, ...]]
    orientations: tuple[Facing, ...]
    is_power_source: bool
    port_ids: tuple[str, ...]


def _body(machine: Machine) -> _Body:
    sizes: dict[Facing, Size] = {}
    for facing in Facing:
        box = rotated_footprint(machine.footprint, facing)
        sizes[facing] = (box.sx, box.sy, box.sz)
    return _Body(
        machine=machine,
        sizes=sizes,
        shells={facing: _shell_offsets(*size, facing) for facing, size in sizes.items()},
        orientations=tuple(machine.orientation_options),
        is_power_source=machine.is_power_source,
        port_ids=tuple(port.id for port in machine.faces.ports),
    )


def _placement(pose: Pose) -> Placement:
    """``pose`` back as the contract's ``Placement``, for the result."""
    x, y, z = pose.cell
    return Placement(
        machine_id=pose.machine_id, cell=CellCoord(x=x, y=y, z=z), orientation=pose.orientation
    )


@dataclass(frozen=True)
class _SearchContext:
    """Immutable per-solve context threaded through the neighbourhood + recreate helpers.

    Built once in :func:`optimize_placement`, it bundles the read-only lookups every move shares -
    the machines by id, the bounding region, the reserved cells, the net adjacency, and the
    per-machine net/power/auto views the LNS recreate ranks insertions with - so the helpers take a
    few varying arguments (the placements, the growing occupied set, the rng) instead of threading a
    dozen constants three levels deep. Purely a container: it changes no value the cost computes.
    """

    bodies: dict[str, _Body]
    region: CellBox
    bounds: Size  # ``region``'s extents as plain ints, for the per-candidate bounds tests
    reserved: set[Cell]
    adjacency: dict[str, set[str]]
    machine_nets: dict[str, list[_WeightedNet]]
    machine_power: dict[str, list[_WeightedNet]]
    machine_auto: dict[str, list[_AutoPair]]
    #: The objective's ``(footprint weight, volume weight)`` (:data:`_OBJECTIVE_WEIGHTS`), so the
    #: LNS recreate can price what an insertion does to the build's size, as ``_cost`` does.
    weights: tuple[float, float]
    #: Whether the small moves include the one-cell nudge: only when every machine is a single
    #: block (module docstring).
    nudges: bool = False


def optimize_placement(
    problem: InputIR,
    *,
    seed: int = 0,
    net_penalties: dict[str, float] | None = None,
    face_penalties: dict[str, float] | None = None,
    objective: Objective = "footprint",
) -> PlacementResult:
    """Anneal the constructive placement toward a lower routing-aware cost (seeded, validated).

        ``net_penalties`` (net id -> extra weight) boosts a net's wirelength term so its machines pull
        tighter - the place<->route feedback signal: the solver penalizes the nets the router could
        not lay, so the next placement clusters them (shorter routes, or adjacency that auto-outputs).
        ``objective`` selects what "compact" means (:data:`Objective`): minimum floor area
        (``footprint``, the default - stack tall), minimum enclosing box (``volume`` - stay flat), or
        ``balanced`` (both weighted).

    ``face_penalties`` (machine id -> extra weight) is the same signal for crowding: the machines
        the solver's crowding gate found nowhere to put a connection, weighted so the next attempt
        gives *them* room.

        Per machine, and emphatically not a global dial. An earlier version scaled the whole face term
        up on every rejection, which quietly broke the multi-start: the attempts are independent seeds
        meant to be ranked against **one** objective, and re-weighting between them meant the layouts
        reaching the end were whichever had been annealed under the most distorted setting. The
        parallel line came out 57% larger that way than the same search at a flat weight (footprint 84
        against 36), because only the sprawled late attempts survived to be ranked.
    """
    base = place(problem)
    if not base.ok or len(base.placements) < 2:
        return base  # infeasible, or nothing to optimize (0/1 machine)

    bodies = {m.id: _body(m) for m in problem.machines}
    region = problem.bounding_region
    bounds = (region.sx, region.sy, region.sz)
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
        bodies=bodies,
        region=region,
        bounds=bounds,
        reserved=reserved,
        adjacency=_net_adjacency(problem),
        machine_nets=_machine_nets(problem, wire_nets),
        machine_power=_machine_nets(problem, power_nets),
        machine_auto=_machine_auto(problem, auto_pairs),
        weights=_OBJECTIVE_WEIGHTS[objective],
        nudges=all(body.sizes[Facing.NORTH] == (1, 1, 1) for body in bodies.values()),
    )
    weights = ctx.weights
    rng = random.Random(seed)

    # The anneal holds plain poses and hands back ``Placement``s only at the end (see Pose).
    current = [pose_of(p) for p in base.placements]
    # ``current``'s occupied-cell set, maintained incrementally: relocate/swap test a candidate
    # against it (temporarily lifting the moved machine's own cells) instead of rebuilding the whole
    # set per proposal, and each accepted move folds in only its delta (see _apply_occupied_delta).
    occupied = _occupied(current, bodies)
    faces_penalty = face_penalties or {}
    current_cost = _cost(
        current,
        bodies,
        wire_nets,
        power_nets,
        auto_pairs,
        weights,
        bounds,
        reserved,
        faces_penalty,
    )
    best, best_cost = current, current_cost
    iters = min(_MAX_ITERS, max(_MIN_ITERS, _PER_MACHINE * len(current)))
    temp = _T0
    for _ in range(iters):
        if rng.random() < _P_LNS:
            cand = _ruin_and_recreate(current, ctx, rng)
        else:
            cand = _move(current, ctx, occupied, rng)
        if cand is not None:
            cand_cost = _cost(
                cand,
                bodies,
                wire_nets,
                power_nets,
                auto_pairs,
                weights,
                bounds,
                reserved,
                faces_penalty,
            )
            delta = cand_cost - current_cost
            if delta < 0 or rng.random() < math.exp(-delta / temp):
                _apply_occupied_delta(occupied, current, cand, bodies)
                current, current_cost = cand, cand_cost
                if current_cost < best_cost:
                    best, best_cost = current, current_cost
        temp *= _ALPHA
    return PlacementResult(placements=tuple(_placement(p) for p in best))


def _cells(pose: Pose, body: _Body) -> Iterator[Cell]:
    """Every cell ``pose``'s body covers - ``occupied_cells`` for a pose and its body."""
    return box_cells(pose.cell, body.sizes[pose.orientation])


def _occupied(poses: list[Pose], bodies: Mapping[str, _Body]) -> set[Cell]:
    """Every cell the bodies at ``poses`` cover."""
    occupied: set[Cell] = set()
    for p in poses:
        occupied.update(_cells(p, bodies[p.machine_id]))
    return occupied


def _apply_occupied_delta(
    occupied: set[Cell],
    before: list[Pose],
    after: list[Pose],
    bodies: Mapping[str, _Body],
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
    before_pose = {p.machine_id: p for p in before}
    removed: set[Cell] = set()
    added: set[Cell] = set()
    for new_p in after:
        old_p = before_pose[new_p.machine_id]
        if (old_p.cell, old_p.orientation) != (new_p.cell, new_p.orientation):
            body = bodies[new_p.machine_id]
            removed.update(_cells(old_p, body))
            added.update(_cells(new_p, body))
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


@dataclass(frozen=True, slots=True)
class _AutoPair:
    """One directed auto-output candidate: which port on which machine, on each side.

    The ports are the part that used to be dropped. They decide which casing cells could host the
    two hatches, and therefore whether the connection is possible at all on a multiblock, so a
    (machine, machine) pair cannot answer the question the router actually asks (#107).

    Slotted rather than a ``NamedTuple``: the cost reads these fields by name per evaluation, and
    a named-tuple field is a descriptor the interpreter will not specialize the load of.
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
    placements: list[Pose],
    bodies: Mapping[str, _Body],
    wire_nets: list[_WeightedNet],
    power_nets: list[_WeightedNet],
    auto_pairs: list[_AutoPair],
    weights: tuple[float, float],
    bounds: Size,
    reserved: set[Cell],
    face_penalties: Mapping[str, float],
) -> float:
    """Routing-aware cost: weighted item/fluid HPWL + compactness per the objective, minus an
    auto-output reward (the only orientation-dependent term, so reorient moves are not free),
    plus an MST pull for each feedback-penalized power net, plus a face-shortfall penalty.

    The shortfall term (:func:`_face_shortfall`) is what keeps the search from packing machines
    into a wall where they have no room left for their own connections - the failure #76 hit,
    where nine machines each needed three connections and a solid row left them two free cells.
    No routing order can rescue that, so it has to be priced here, where the geometry is chosen.

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
        centers = [_center(pos[mid], bodies[mid]) for mid in machine_ids if mid in pos]
        if len(centers) < 2:
            continue
        for axis in range(3):
            coords = [c[axis] for c in centers]
            wire += weight * (max(coords) - min(coords))

    cable = 0.0
    for machine_ids, weight in power_nets:
        centers = [_center(pos[mid], bodies[mid]) for mid in machine_ids if mid in pos]
        cable += weight * _mst_length(centers)

    # Bounding box from each footprint's two extreme corners (its origin and origin+size-1) rather
    # than enumerating every occupied cell: for axis-aligned footprints the min/max over the corners
    # equals the min/max over all their cells, so footprint area and volume are bit-identical - but
    # this is O(machines), not O(total cell volume), on the hottest path in the solver.
    # Rotated extents: a turned non-cubic machine reaches a different distance along each axis, so
    # the declared footprint would misreport the floor area and volume these objectives rank on.
    boxes = [(p.cell, bodies[p.machine_id].sizes[p.orientation]) for p in placements]
    min_x = min(c[0] for c, _ in boxes)
    max_x = max(c[0] + s[0] - 1 for c, s in boxes)
    min_y = min(c[1] for c, _ in boxes)
    max_y = max(c[1] + s[1] - 1 for c, s in boxes)
    min_z = min(c[2] for c, _ in boxes)
    max_z = max(c[2] + s[2] - 1 for c, s in boxes)
    footprint = (max_x - min_x + 1) * (max_z - min_z + 1)
    volume = footprint * (max_y - min_y + 1)

    auto = 0
    free_ports: set[tuple[str, str]] = set()
    for pair in auto_pairs:
        sp, tp = pos.get(pair.source_id), pos.get(pair.sink_id)
        if sp is None or tp is None:
            continue
        if auto_output_possible(
            sp,
            bodies[pair.source_id].machine,
            pair.source_port,
            tp,
            bodies[pair.sink_id].machine,
            pair.sink_port,
        ):
            auto += 1
            # This pair ejects straight across, so neither end needs a cell to dock a pipe on.
            free_ports.add((pair.source_id, pair.source_port))
            free_ports.add((pair.sink_id, pair.sink_port))
    faces = _face_shortfall(placements, bodies, bounds, reserved, free_ports, face_penalties)
    w_footprint, w_volume = weights
    return (
        _W_WIRE * wire
        + cable
        + w_footprint * footprint
        + w_volume * volume
        - _W_AUTO * auto
        + _W_FACES * faces
    )


def _dockable_cells(
    pose: Pose,
    body: _Body,
    occupied: set[Cell],
    bounds: Size,
    reserved: set[Cell],
) -> set[Cell]:
    """The free cells this machine could put a connection on, front face excluded.

    The *cells*, not the faces: two faces of one body cell reach two different cells, and two body
    cells can reach the same cell from different sides, so a cell set is the honest account of
    what a hatch could dock onto. Returned as the set rather than its size because neighbouring
    machines share candidates, and :func:`_face_shortfall` has to see the overlap to price it. Deliberately **generous**, the same way the validator's
    hatch-cell ceiling is (``validator.core._check_hatch_cells``): it ignores which slots accept
    which hatch kind, so it only ever over-counts. An over-count means the penalty fires strictly
    less often than it could - it never invents a shortfall that is not real.
    """
    ox, oy, oz = pose.cell
    rx, ry, rz = bounds
    cells: set[Cell] = set()
    for dx, dy, dz in body.shells[pose.orientation]:
        x, y, z = cand = (ox + dx, oy + dy, oz + dz)
        if cand in occupied or cand in reserved:
            continue
        if 0 <= x < rx and 0 <= y < ry and 0 <= z < rz:
            cells.add(cand)
    return cells


@cache
def _shell_offsets(sx: int, sy: int, sz: int, front: Facing) -> tuple[Cell, ...]:
    """Offsets from a body's origin to the cells one step outside it through a non-front face.

    What :func:`_dockable_cells` scans, worked out once per rotated box rather than per call.
    Stepping every body cell through every face mostly lands back inside the body (on a 3x3x3, 90
    of 135 steps), and those cells are always occupied - by the machine itself - so they are dropped
    here instead of being built and hashed to be rejected. A cell two body cells reach turns up once.

    **The order is load-bearing.** It is each cell's first appearance in the face-major,
    body-cell-minor scan the dockable set used to be built from, so filtering this sequence inserts
    the same cells into the set in the same order, and the set iterates identically. That matters
    because :func:`_face_shortfall` sums floats over it: a different order can round differently,
    and a cost that moves in its last bit can flip an annealing decision.
    """
    seen: set[Cell] = set()
    shell: list[Cell] = []
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is front:  # front face carries no I/O
            continue
        for bx, by, bz in product(range(sx), range(sy), range(sz)):
            x, y, z = cell = (bx + dx, by + dy, bz + dz)
            if 0 <= x < sx and 0 <= y < sy and 0 <= z < sz:
                continue  # inside the machine's own body, so never a free cell
            if cell not in seen:
                seen.add(cell)
                shell.append(cell)
    return tuple(shell)


def _face_shortfall(
    placements: list[Pose],
    bodies: Mapping[str, _Body],
    bounds: Size,
    reserved: set[Cell],
    free_ports: Collection[tuple[str, str]] = (),
    penalties: Mapping[str, float] = MappingProxyType({}),
) -> float:
    """Connections with nowhere to sit, summed over every machine: the unbuildability measure.

    A machine needs one free adjacent cell per connection (item in, item out, power in, ...) and
    its front face carries none of them. Pack it so that neighbours and region walls leave it
    fewer free cells than it has ports and the layout cannot be built - the routers then report
    whichever net happens to lose the race for the last face, which names the wrong machine and
    reads like a routing bug. Priced here instead, where the packing decision is actually made.

    ``penalties`` (machine id -> extra weight) scales one machine's own shortfall, so the solver
    can lean on exactly the machines its crowding gate named without re-weighting the whole layout
    (see :func:`optimize_placement`).

    ``free_ports`` are the ``(machine, port)`` pairs an auto-output covers on this placement, and
    they are **not** demand: the connection ejects straight from one machine into the next, so no
    cell is docked at either end. Leaving them in was a standing tax on exactly the tight,
    zero-pipe layouts this search is supposed to find - it charged the sand line's fully
    auto-fed chain a phantom shortfall of 3.00 on a layout the exact gate agrees is fine, which
    forced the weight down, which in turn forced the solver to escalate its way out on lines that
    were really crowded. The over-exemption is deliberate and safe: a single block has only one
    auto-output face, so a machine with two possible pairs has both exempted here though only one
    can really be free. That under-reports rather than invents, and the exact gate
    (``placement.feasibility``) is what actually rules.

    Counting each machine's candidates in isolation is not enough, and #76 is exactly where that
    shows: every machine can clear its own bar while two neighbours are counting **the same** free
    cell, which can host only one of them. So a contested cell is shared out rather than credited
    to each in full.

    Only machines with **no slack** contend for it. Dividing a cell among every machine that could
    reach it is far too pessimistic - a neighbour with five candidates for three ports is never
    going to fight over this one, and counting it as a rival manufactured a shortfall on layouts
    that were perfectly buildable, which cost the sand line a third of its compactness. A machine
    that already has more candidates than ports is therefore not a contender; one that is exactly
    at its limit is, because every cell it can reach is a cell it needs.

    That makes this a heuristic, not a decision procedure: the real question is whether a system
    of distinct representatives exists (each connection needing its own cell), and settling that
    means a matching, which is far too slow per annealing step. It is tuned to stay silent on
    layouts that build - a false shortfall costs compactness on every line - and to speak up on
    the crowding that #76 hit.
    """
    occupied = _occupied(placements, bodies)
    exempt = set(free_ports)
    demand: list[tuple[str, int, set[Cell]]] = []
    for p in placements:
        body = bodies[p.machine_id]
        needed = sum(1 for port_id in body.port_ids if (p.machine_id, port_id) not in exempt)
        if needed:
            demand.append(
                (p.machine_id, needed, _dockable_cells(p, body, occupied, bounds, reserved))
            )
    contenders: dict[Cell, int] = {}
    for _mid, needed, cells in demand:
        if len(cells) > needed:
            continue  # has room to spare, so it will not be fighting anyone for a particular cell
        for cell in cells:
            contenders[cell] = contenders.get(cell, 0) + 1
    short = 0.0
    for mid, needed, cells in demand:
        share = sum(1.0 / max(1, contenders.get(cell, 0)) for cell in cells)
        short += (1.0 + penalties.get(mid, 0.0)) * max(0.0, needed - share)
    return short


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


def _center(p: Pose, body: _Body) -> tuple[float, float, float]:
    # Rotated: a wrong centroid feeds HPWL and the power MST, so it would steer the search.
    x, y, z = p.cell
    sx, sy, sz = body.sizes[p.orientation]
    return (x + sx / 2, y + sy / 2, z + sz / 2)


def _feed_ok(body: _Body, origin: Cell, orientation: Facing, bounds: Size) -> bool:
    """Whether placing ``body`` here honors the power-source feed rule (trivially true for
    non-sources): a source's front face is its reserved external-feed face and must lie flush on
    the region boundary (docs/DOMAIN.md; validator-enforced)."""
    return not body.is_power_source or box_front_on_boundary(
        origin, body.sizes[orientation], orientation, bounds
    )


def _feed_orientation(body: _Body, origin: Cell, current: Facing, bounds: Size) -> Facing | None:
    """The orientation ``body`` should take at ``origin``: ``current`` when it is legal there,
    else the first option that puts a source's feed face back on the boundary, else ``None``
    (the move cannot place this machine here)."""
    if _feed_ok(body, origin, current, bounds):
        return current
    return next((o for o in body.orientations if _feed_ok(body, origin, o, bounds)), None)


def _move(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    """Propose one small move; return a VALID candidate layout, or None if it could not be made.

    ``occupied`` is ``placements``' occupied-cell set (owned by the annealing loop): relocate and
    swap borrow it to test a candidate and restore it before returning, so the caller keeps the
    single incrementally-maintained copy."""
    roll = rng.random()
    if roll < _P_RELOCATE:
        if ctx.nudges and roll >= _P_RELOCATE_WHEN_NUDGING:
            return _nudge(placements, ctx, occupied, rng)
        return _relocate(placements, ctx, occupied, rng)
    if roll < _P_SWAP:
        return _swap(placements, ctx, occupied, rng)
    return _reorient(placements, ctx, occupied, rng)


def _relocate(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    i = rng.randrange(len(placements))
    p = placements[i]
    body = ctx.bodies[p.machine_id]
    # Lift machine i's own cells out of the shared occupied set so a candidate may reuse them; the
    # remainder is exactly the other machines' cells (what ``others`` was). Restored in the finally.
    own = set(_cells(p, body))
    occupied.difference_update(own)
    try:
        for _ in range(_RELOCATE_TRIES):
            origin = _rand_origin(body, ctx.bounds, p.orientation, rng)
            if origin is None:
                return None
            # Orientation first: which cells a turned non-cubic machine covers depends on it, so
            # the fit test cannot run before it is known. _feed_orientation draws no randomness, so
            # hoisting it above the test leaves the RNG trajectory (and every existing layout)
            # exactly as it was.
            orientation = _feed_orientation(body, origin, p.orientation, ctx.bounds)
            if orientation is None:
                continue  # a source relocated off the boundary: no legal feed face, keep trying
            size = body.sizes[orientation]
            if not box_within(origin, size, ctx.bounds):
                continue  # the body would hang off the region: cheaper to reject than to expand
            cells = list(box_cells(origin, size))
            if ctx.reserved.isdisjoint(cells) and occupied.isdisjoint(cells):
                new = list(placements)
                new[i] = Pose(p.machine_id, origin, orientation)
                return new
        return None
    finally:
        occupied.update(own)


#: The six one-cell steps a nudge may take, in ``FACE_DELTAS`` order (the nudge shuffles them).
_NUDGE_STEPS = tuple(FACE_DELTAS.values())


def _nudge(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    """Shift one machine by one cell, trying the six directions in a random order.

    The local move relocate is not (module docstring). The machine keeps its orientation, except
    that a power source turns back onto the boundary the way relocate turns it; the first direction
    that fits wins, and a machine boxed in on every side yields ``None``.
    """
    i = rng.randrange(len(placements))
    p = placements[i]
    body = ctx.bodies[p.machine_id]
    # As in _relocate: lift the machine's own cells so it may step into one it covers now.
    own = set(_cells(p, body))
    occupied.difference_update(own)
    try:
        steps = list(_NUDGE_STEPS)
        rng.shuffle(steps)
        x, y, z = p.cell
        for dx, dy, dz in steps:
            origin = (x + dx, y + dy, z + dz)
            orientation = _feed_orientation(body, origin, p.orientation, ctx.bounds)
            if orientation is None:
                continue  # a source stepped off the boundary with no feed-legal facing there
            size = body.sizes[orientation]
            if not box_within(origin, size, ctx.bounds):
                continue
            cells = list(box_cells(origin, size))
            if ctx.reserved.isdisjoint(cells) and occupied.isdisjoint(cells):
                new = list(placements)
                new[i] = Pose(p.machine_id, origin, orientation)
                return new
        return None
    finally:
        occupied.update(own)


def _swap(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    i, j = rng.sample(range(len(placements)), 2)
    pi, pj = placements[i], placements[j]
    bi, bj = ctx.bodies[pi.machine_id], ctx.bodies[pj.machine_id]
    # Lift both machines' current cells so the remainder is the other machines' (what ``others``
    # was); restored in the finally on every exit.
    own = set(_cells(pi, bi)) | set(_cells(pj, bj))
    occupied.difference_update(own)
    try:
        # Orientation first, as in _relocate: each swapped body's cells depend on the orientation
        # it lands with, and neither call draws randomness.
        oi = _feed_orientation(bi, pj.cell, pi.orientation, ctx.bounds)
        oj = _feed_orientation(bj, pi.cell, pj.orientation, ctx.bounds)
        if oi is None or oj is None:
            return None  # the swap would strand a source's feed face off the boundary
        # Each body's own in-region test first, off the boxes: a swap that lands a bigger machine
        # in a smaller one's slot usually fails right here, and then neither body is ever expanded.
        size_i, size_j = bi.sizes[oi], bj.sizes[oj]
        if not box_within(pj.cell, size_i, ctx.bounds) or not box_within(
            pi.cell, size_j, ctx.bounds
        ):
            return None
        moved = list(box_cells(pj.cell, size_i)) + list(box_cells(pi.cell, size_j))
        if (
            len(set(moved)) != len(moved)  # the two swapped bodies overlap each other
            or not ctx.reserved.isdisjoint(moved)
            or not occupied.isdisjoint(moved)
        ):
            return None
        new = list(placements)
        new[i] = Pose(pi.machine_id, pj.cell, oi)
        new[j] = Pose(pj.machine_id, pi.cell, oj)
        return new
    finally:
        occupied.update(own)


def _turn_fits(
    body: _Body, p: Pose, orientation: Facing, ctx: _SearchContext, occupied: set[Cell]
) -> bool:
    """Whether ``body`` still fits at its own origin once turned to ``orientation``.

    Short-circuits the common case: a turn that leaves the extents alone cannot change which cells
    are covered, so every 1x1x1 block and every square-base multiblock skips the test and the hot
    path is untouched.
    """
    size = body.sizes[orientation]
    if size == body.sizes[p.orientation]:
        return True
    if not box_within(p.cell, size, ctx.bounds):
        return False  # the turn swings the body out of the region; no need to expand either set
    own = set(_cells(p, body))
    cells = list(box_cells(p.cell, size))
    return ctx.reserved.isdisjoint(cells) and (occupied - own).isdisjoint(cells)


def _reorient(
    placements: list[Pose],
    ctx: _SearchContext,
    occupied: set[Cell],
    rng: random.Random,
) -> list[Pose] | None:
    candidates: list[tuple[int, list[Facing]]] = []
    for k, p in enumerate(placements):
        body = ctx.bodies[p.machine_id]
        # A source only reorients among feed-legal facings (its front must stay on the boundary),
        # and ANY machine only among facings it still fits at. A quarter turn swaps a non-cubic
        # machine's horizontal extents, so a turn can push it out of the region, onto a reserved
        # cell or into a neighbour. Nothing checked that while rotation was a no-op, and an
        # accepted state that broke it would violate this loop's overlap-free invariant in silence.
        alts = [
            o
            for o in body.orientations
            if o != p.orientation
            and _feed_ok(body, p.cell, o, ctx.bounds)
            and _turn_fits(body, p, o, ctx, occupied)
        ]
        if alts:
            candidates.append((k, alts))
    if not candidates:
        return None
    k, alts = candidates[rng.randrange(len(candidates))]
    p = placements[k]
    new = list(placements)
    new[k] = Pose(p.machine_id, p.cell, rng.choice(alts))
    return new


def _ruin_and_recreate(
    placements: list[Pose],
    ctx: _SearchContext,
    rng: random.Random,
) -> list[Pose] | None:
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

    occupied = _occupied(kept, ctx.bodies)
    # One grid for the whole recreate, updated in step with `occupied` as each machine lands,
    # rather than rebuilt per insertion: `_best_insertion` only reads it.
    grid = _occupancy_grid(ctx.region, occupied, ctx.reserved)
    row, plane = ctx.bounds[0], ctx.bounds[0] * ctx.bounds[1]

    placed = list(kept)
    placed_ids = {p.machine_id for p in placed}
    # Re-insert the most-constrained first (most already-placed net-neighbours) so the strongest
    # pulls choose their spot before the freer machines fill in around them.
    to_insert = sorted(
        (placements[i] for i in sorted(ruined)),
        key=lambda p: -_placed_neighbor_count(p.machine_id, ctx.adjacency, placed_ids),
    )
    for p in to_insert:
        spot = _best_insertion(p, placed, occupied, grid, ctx, rng)
        if spot is None:
            return None  # could not re-place this machine; abandon the move, the loop skips it
        landed = Pose(p.machine_id, *spot)
        placed.append(landed)
        placed_ids.add(p.machine_id)
        for cell in _cells(landed, ctx.bodies[p.machine_id]):
            occupied.add(cell)
            grid[cell[0] + cell[1] * row + cell[2] * plane] = 1

    by_id = {p.machine_id: p for p in placed}
    return [by_id[p.machine_id] for p in placements]  # preserve the original ordering


def _related_cluster(
    placements: list[Pose], adjacency: dict[str, set[str]], k: int, rng: random.Random
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
    machine_id: str, placed_pos: dict[str, Pose], ctx: _SearchContext
) -> tuple[list[_NetBox], list[_PowerAttach]]:
    """Everything :func:`_marginal_insertion_cost` needs that does NOT depend on the candidate.

    An insertion evaluates ~50 (origin, orientation) pairs, and the wire and cable terms were
    re-deriving the same already-placed centroids for every one of them. Both terms reduce to a
    fixed summary of the placed members - a bounding box for HPWL, the centroid list for the MST
    pull - so this computes each once per insertion and the candidate loop just reads them.
    """
    centroids = {mid: _center(q, ctx.bodies[mid]) for mid, q in placed_pos.items()}

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


def _placed_extent(placed: list[Pose], ctx: _SearchContext) -> _Extent | None:
    """The :class:`_Extent` of ``placed``, or ``None`` when nothing is placed yet.

    Corner-based like ``_cost``'s own box, so the two agree on what "the build" measures.
    """
    if not placed:
        return None
    boxes = [(p.cell, ctx.bodies[p.machine_id].sizes[p.orientation]) for p in placed]
    return _Extent(
        min(c[0] for c, _ in boxes),
        max(c[0] + s[0] - 1 for c, s in boxes),
        min(c[1] for c, _ in boxes),
        max(c[1] + s[1] - 1 for c, s in boxes),
        min(c[2] for c, _ in boxes),
        max(c[2] + s[2] - 1 for c, s in boxes),
    )


def _occupancy_grid(region: CellBox, occupied: set[Cell], reserved: set[Cell]) -> bytearray:
    """``occupied | reserved`` as a flat byte per region cell, indexed ``x + y*sx + z*sx*sy``.

    The fit test in :func:`_best_insertion` asked ``isdisjoint`` of a freshly materialised cell
    list, which meant building one tuple per cell of the body per candidate. That is cheap when a
    machine is 1x1x1 and ruinous when the real dataset gives it a 7x7x7 body: it was 90% of all
    ``occupied_cells`` yields in a solve and the single hottest line in the solver. A byte grid
    answers the same question by indexing, with no allocation and no hashing per candidate.

    Unpadded on purpose. ``box_within`` already gates every test with six comparisons on the
    rotated box's corners, so an index built from a passing origin is always in range; a padded
    border would buy a bounds check that has already been paid for.
    """
    sx, sy, sz = region.sx, region.sy, region.sz
    plane = sx * sy
    grid = bytearray(plane * sz)
    for x, y, z in occupied:
        grid[x + y * sx + z * plane] = 1  # every placed body cleared box_within to get here
    for x, y, z in reserved:  # caller-supplied, so this one is guarded
        if 0 <= x < sx and 0 <= y < sy and 0 <= z < sz:
            grid[x + y * sx + z * plane] = 1
    return grid


def _box_offsets(size: Size, bounds: Size) -> tuple[int, ...]:
    """Flat offsets from an origin to every cell a box of the (rotated) ``size`` covers, in a
    region of extents ``bounds``."""
    sx, sy, sz = size
    row, plane = bounds[0], bounds[0] * bounds[1]
    return tuple(
        dx + dy * row + dz * plane for dz in range(sz) for dy in range(sy) for dx in range(sx)
    )


def _best_insertion(
    p: Pose,
    placed: list[Pose],
    occupied: set[Cell],
    grid: bytearray,
    ctx: _SearchContext,
    rng: random.Random,
) -> tuple[Cell, Facing] | None:
    """The valid (origin, orientation) for ``p``'s machine that minimises its *marginal* cost, over
    candidate cells beside its placed net-neighbours plus a few random ones; falls back to any
    first-fit free slot, or ``None`` if the machine cannot be placed at all.

    Ranking uses only the terms that depend on this machine (its nets + auto pairs), so an insertion
    costs O(machine degree), not a full O(all nets) recompute - the loop's ``_cost`` still gates
    acceptance globally.
    """
    body = ctx.bodies[p.machine_id]
    placed_pos = {q.machine_id: q for q in placed}
    net_boxes, power_attach = _placed_invariants(p.machine_id, placed_pos, ctx)
    extent = _placed_extent(placed, ctx)
    bounds = ctx.bounds
    offsets_by_size: dict[Size, tuple[int, ...]] = {}
    row, plane = bounds[0], bounds[0] * bounds[1]
    best: tuple[Cell, Facing] | None = None
    best_cost = math.inf
    for origin in _candidate_origins(p, body, placed, ctx, rng):
        # The fit test moved inside the orientation loop, because which cells the machine covers
        # depends on how it is turned - but it depends ONLY on the rotated box, and four facings
        # yield at most two of those. Memoizing per box keeps this at one test per origin for a
        # square-base machine (every machine in both shipped examples) instead of four.
        fits: dict[Size, bool] = {}
        for orientation in body.orientations:
            if not _feed_ok(body, origin, orientation, bounds):
                continue  # a source's feed face must stay on the boundary
            size = body.sizes[orientation]
            ok = fits.get(size)
            if ok is None:
                # In-region off the box first: this is the hottest fit test in the solve (one per
                # candidate origin, and _candidate_origins offers plenty that hang off the region),
                # and only a candidate that clears it is worth expanding into cells.
                ok = box_within(origin, size, bounds)
                if ok:
                    offsets = offsets_by_size.get(size)
                    if offsets is None:
                        offsets = offsets_by_size[size] = _box_offsets(size, bounds)
                    x, y, z = origin
                    base = x + y * row + z * plane
                    for off in offsets:
                        if grid[base + off]:
                            ok = False
                            break
                fits[size] = ok
            if not ok:
                continue
            cost = _marginal_insertion_cost(
                p.machine_id,
                origin,
                orientation,
                body,
                placed_pos,
                net_boxes,
                power_attach,
                ctx,
                bound=best_cost,
                extent=extent,
            )
            if cost < best_cost:
                best_cost, best = cost, (origin, orientation)
    if best is not None:
        return best
    # Last resort: any free slot in first-fit order (a source additionally requires a slot +
    # orientation with its feed face on the boundary - the same rule the constructive seed used).
    fit = _fit(body.machine, ctx.region, occupied | ctx.reserved)
    return None if fit is None else (fit[0].as_tuple(), fit[1])


def _marginal_insertion_cost(
    machine_id: str,
    origin: Cell,
    orientation: Facing,
    body: _Body,
    placed_pos: dict[str, Pose],
    net_boxes: list[_NetBox],
    power_attach: list[_PowerAttach],
    ctx: _SearchContext,
    bound: float = math.inf,
    extent: _Extent | None = None,
) -> float:
    """The cost terms that change with where ``machine_id`` goes: the weighted HPWL of its own
    item/fluid nets over their already-placed members (this candidate included), plus for each of
    its feedback-penalized power nets the Manhattan distance to the nearest placed member (the
    increment Prim would pay to attach this machine to the trunk MST), plus how much the candidate
    grows the build (``extent``, below), minus the auto-output reward for the pairs the candidate
    makes face-adjacent. A cheap marginal proxy of ``_cost`` for ranking candidate insertions; the
    annealing loop's full ``_cost`` still gates acceptance.

    **The growth term is what makes the LNS move compact anything** (#254). ``extent`` is the box
    the machines already placed span, and a candidate pays the objective's own compactness
    weights on whatever it adds to that box's floor area and volume, the same terms ``_cost``
    ranks on. Without it an insertion that extends the build costs the same as one tucked inside
    it, so the recreate put a machine wherever its nets were cheapest, often at the far end of the
    strip the first-fit start lays down, and the loop's acceptance could only reject that. Measured
    over eight solve seeds, ev-nitrobenzene's median floor went from 508.5 to 441 with this term,
    with fewer pipe and cable blocks, and no example line lost a valid seed. ``None`` (nothing
    placed yet) adds nothing, since the first machine back defines the box.

    ``net_boxes`` and ``power_attach`` come from :func:`_placed_invariants` and summarise the
    members that are already placed - the part of both terms that is the same for every candidate.
    Only the auto reward is irreducibly per-candidate: face adjacency depends on this origin and
    orientation, which is what the term is there to measure.

    ``bound`` is the incumbent's cost, and returning ``inf`` above it is a pure early-out: the
    caller only keeps a strictly cheaper candidate, so a candidate that provably cannot get there
    need not be scored exactly. **The bound has to account for the auto reward being subtracted.**
    The running ``wire + cable + growth`` total is an *upper* bound on the result, not a lower one, so
    comparing it against ``bound`` directly would discard candidates the reward would have made
    best. Subtracting the largest reward still available - every remaining pair scoring - makes the
    test admissible, and the layouts it produces are identical to scoring every candidate in full.
    What it skips is the auto term entirely: the ``Pose`` this function would have to build to ask
    with, and a ``router.auto.auto_output_possible`` call per pair. That rule got substantially
    dearer when it started asking the question the router actually answers (#107), which is what
    makes the early-out worth having - it takes a nitrobenzene solve from 6.32s to 4.36s."""
    x, y, z = origin
    sx, sy, sz = body.sizes[orientation]  # same centroid rule as _center
    cx = x + sx / 2
    cy = y + sy / 2
    cz = z + sz / 2
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
    growth = 0.0
    if extent is not None:
        # The placed box with this candidate's corners folded in, against the box without it.
        ex0, ex1, ey0, ey1, ez0, ez1 = extent
        wx, wy, wz = ex1 - ex0 + 1, ey1 - ey0 + 1, ez1 - ez0 + 1
        nx = (x + sx - 1 if x + sx - 1 > ex1 else ex1) - (x if x < ex0 else ex0) + 1
        ny = (y + sy - 1 if y + sy - 1 > ey1 else ey1) - (y if y < ey0 else ey0) + 1
        nz = (z + sz - 1 if z + sz - 1 > ez1 else ez1) - (z if z < ez0 else ez0) + 1
        w_footprint, w_volume = ctx.weights
        growth = w_footprint * (nx * nz - wx * wz) + w_volume * (nx * ny * nz - wx * wy * wz)
    # Summed once, in this order, for both uses below: the early-out and the result must agree.
    rest = cable + growth
    pairs = ctx.machine_auto[machine_id]
    if _W_WIRE * wire + rest - _W_AUTO * len(pairs) >= bound:
        return math.inf  # even every pair scoring cannot beat the incumbent
    auto = 0
    here = Pose(machine_id, origin, orientation)
    m = body.machine
    for pair in pairs:
        is_source = pair.source_id == machine_id
        other = pair.sink_id if is_source else pair.source_id
        op = placed_pos.get(other)
        if op is None:
            continue
        om = ctx.bodies[other].machine
        if is_source:
            possible = auto_output_possible(here, m, pair.source_port, op, om, pair.sink_port)
        else:
            possible = auto_output_possible(op, om, pair.source_port, here, m, pair.sink_port)
        if possible:
            auto += 1
    return _W_WIRE * wire + rest - _W_AUTO * auto


def _candidate_origins(
    p: Pose,
    body: _Body,
    placed: list[Pose],
    ctx: _SearchContext,
    rng: random.Random,
) -> list[Cell]:
    """Origins to try inserting ``body`` at: its freed origin, the cells face-adjacent to each
    placed net-neighbour (to cluster / auto-output), and a few random free origins - deduped,
    in-region."""
    neighbor_ids = ctx.adjacency.get(p.machine_id, set())
    rx, ry, rz = ctx.bounds
    origins: list[Cell] = []
    seen: set[Cell] = set()

    def add(c: Cell) -> None:
        x, y, z = c
        if c not in seen and 0 <= x < rx and 0 <= y < ry and 0 <= z < rz:
            seen.add(c)
            origins.append(c)

    add(p.cell)  # freed origin (may be retaken; validity checked by caller)
    for q in placed:
        if len(origins) >= _MAX_CANDIDATES:
            break  # enough neighbour-adjacent sites; keep recreate cheap
        if q.machine_id in neighbor_ids:
            for bx, by, bz in _cells(q, ctx.bodies[q.machine_id]):
                for dx, dy, dz in _FACE_DELTAS:
                    add((bx + dx, by + dy, bz + dz))
    for _ in range(_LNS_RANDOM_CANDIDATES):
        origin = _rand_origin(body, ctx.bounds, p.orientation, rng)
        if origin is not None:
            add(origin)
    return origins


def _rand_origin(body: _Body, bounds: Size, orientation: Facing, rng: random.Random) -> Cell | None:
    # The rotated extents, not the declared ones: an east-facing 5x1x2 needs 2 of x and 5 of z, so
    # bounding by the declared box would both reject origins that fit and offer origins that do
    # not - and the draws below consume randomness, so a wrong bound shifts every later draw.
    sx, sy, sz = body.sizes[orientation]
    rx, ry, rz = bounds
    if sx > rx or sy > ry or sz > rz:
        return None
    # A tuple display evaluates left to right, so the draws come x, y, z, as they always have.
    return (rng.randrange(rx - sx + 1), rng.randrange(ry - sy + 1), rng.randrange(rz - sz + 1))
