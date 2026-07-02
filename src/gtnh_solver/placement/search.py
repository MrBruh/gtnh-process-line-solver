"""placement.search - Phase 2 simulated-annealing + LNS placement with a routing-aware cost.

Starts from the constructive first-fit solution and improves it under a cost that proxies
buildability: half-perimeter wirelength (HPWL) per net pulls connected machines together (more
auto-output, shorter pipes), an auto-output reward favours orientations whose usable (non-front)
faces actually let a source eject into its sink, plus a mild compactness bias on the total
bounding-box volume (no separate per-layer term: volume already counts height, so the optimizer
minimises the whole box, not the layer count). The auto reward is the only orientation-dependent
term, so reorient moves carry a real cost signal -
without it they were free random walk that could finalize an orientation BLOCKING auto-output.

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
the output. Deterministic for a given ``seed``. A true place<->route feedback loop lives in
``solver.core`` (docs/ROADMAP.md lane C + solver).
"""

from __future__ import annotations

import math
import random

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import (
    FACE_DELTAS,
    Cell,
    auto_output_faces,
    in_region,
    occupied_cells,
)

from .constructive import PlacementResult, _first_fit, place

#: The six face-adjacent offsets, for growing LNS insertion candidates around placed neighbours.
_FACE_DELTAS = tuple(FACE_DELTAS.values())

# Cost weights: wirelength dominates (it drives auto-output + short pipes); an auto-output reward
# makes orientation matter (the front face carries no I/O, so the wrong orientation BLOCKS the
# free connection - reorient moves are a no-op on the other terms); a compactness term on the
# total bounding-box volume breaks ties without overriding routability. There is deliberately NO
# separate per-layer penalty: the bbox volume already counts the y-extent, so the optimizer trades
# height against footprint purely by which yields the smaller box - it optimizes total volume, not
# layer count (docs/ROADMAP.md lane C).
_W_WIRE = 1.0
_W_COMPACT = 0.02
_W_AUTO = 4.0

# Annealing schedule (geometric cooling). Budget scales with machine count, clamped.
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
_MAX_CANDIDATES = 16  # cap neighbour-adjacent insertion sites so recreate stays cheap on hubs


def optimize_placement(
    problem: InputIR, *, seed: int = 0, net_penalties: dict[str, float] | None = None
) -> PlacementResult:
    """Anneal the constructive placement toward a lower routing-aware cost (seeded, validated).

    ``net_penalties`` (net id -> extra weight) boosts a net's wirelength term so its machines pull
    tighter - the place<->route feedback signal: the solver penalizes the nets the router could
    not lay, so the next placement clusters them (shorter routes, or adjacency that auto-outputs).
    """
    base = place(problem)
    if not base.ok or len(base.placements) < 2:
        return base  # infeasible, or nothing to optimize (0/1 machine)

    machines = {m.id: m for m in problem.machines}
    region = problem.bounding_region
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    penalties = net_penalties or {}
    # Nets that are physically routed (skip ME-toggled): each is (machine ids, wirelength weight),
    # where a penalized net weighs more so the optimizer shortens it preferentially.
    nets = [
        ([e.machine_id for e in n.endpoints], 1.0 + penalties.get(n.id, 0.0))
        for n in problem.nets
        if not problem.me_toggles.toggled(n.commodity)
    ]
    # Directed source->sink pairs that COULD auto-output (simple 1->1 item/fluid nets, like the
    # solver's own rule): the cost rewards each pair the current placement+orientation makes
    # face-adjacent, so orientation has a gradient toward enabling the free connection.
    auto_pairs = _auto_candidate_pairs(problem)
    # Which machines share a net (any commodity): the LNS ruin step grows a *related* cluster along
    # these edges, and recreate biases each insertion toward its net-neighbours' cells.
    adjacency = _net_adjacency(problem)
    # Per-machine views the LNS recreate ranks candidate insertions with, cheaply and without a full
    # cost recompute: the nets each machine is in, and the auto-output pairs it participates in.
    machine_nets = _machine_nets(problem, nets)
    machine_auto = _machine_auto(problem, auto_pairs)
    rng = random.Random(seed)

    current = list(base.placements)
    current_cost = _cost(current, machines, nets, auto_pairs)
    best, best_cost = current, current_cost
    iters = min(_MAX_ITERS, max(_MIN_ITERS, _PER_MACHINE * len(current)))
    temp = _T0
    for _ in range(iters):
        if rng.random() < _P_LNS:
            cand = _ruin_and_recreate(
                current, machines, region, reserved, adjacency, machine_nets, machine_auto, rng
            )
        else:
            cand = _move(current, machines, region, reserved, rng)
        if cand is not None:
            cand_cost = _cost(cand, machines, nets, auto_pairs)
            delta = cand_cost - current_cost
            if delta < 0 or rng.random() < math.exp(-delta / temp):
                current, current_cost = cand, cand_cost
                if current_cost < best_cost:
                    best, best_cost = current, current_cost
        temp *= _ALPHA
    return PlacementResult(placements=tuple(best))


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


def _machine_nets(
    problem: InputIR, nets: list[tuple[list[str], float]]
) -> dict[str, list[tuple[list[str], float]]]:
    """Machine -> the (routed) nets it belongs to, so LNS can score an insertion from only the
    machine's own nets instead of re-summing every net's HPWL."""
    by_machine: dict[str, list[tuple[list[str], float]]] = {m.id: [] for m in problem.machines}
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
    auto-output - the same nets ``solver._assign_auto_outputs`` covers (power/ME never auto-feed).
    """
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}
    pairs: list[tuple[str, str]] = []
    for net in problem.nets:
        if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
            continue
        sources = [
            e.machine_id
            for e in net.endpoints
            if port_dir.get((e.machine_id, e.port_id)) is IODirection.OUTPUT
        ]
        sinks = [
            e.machine_id
            for e in net.endpoints
            if port_dir.get((e.machine_id, e.port_id)) is IODirection.INPUT
        ]
        if len(sources) == 1 and len(sinks) == 1:
            pairs.append((sources[0], sinks[0]))
    return pairs


def _cost(
    placements: list[Placement],
    machines: dict[str, Machine],
    nets: list[tuple[list[str], float]],
    auto_pairs: list[tuple[str, str]],
) -> float:
    """Routing-aware cost: weighted net HPWL + compactness (bbox volume), minus an auto-output
    reward (the only orientation-dependent term, so reorient moves are not free).

    Compactness is the total bounding-box volume, which already accounts for the y-extent, so there
    is no separate per-layer term - the optimizer minimises the whole box, not the number of layers
    (docs/ROADMAP.md lane C). Each net's HPWL is scaled by its weight (1.0, or more for a
    feedback-penalized net)."""
    pos = {p.machine_id: p for p in placements}
    wire = 0.0
    for machine_ids, weight in nets:
        centers = [_center(pos[mid], machines[mid]) for mid in machine_ids if mid in pos]
        if len(centers) < 2:
            continue
        for axis in range(3):
            coords = [c[axis] for c in centers]
            wire += weight * (max(coords) - min(coords))

    cells = [
        c for p in placements for c in occupied_cells(p.cell, machines[p.machine_id].footprint)
    ]
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    zs = [c[2] for c in cells]
    volume = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1) * (max(zs) - min(zs) + 1)

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
    return _W_WIRE * wire + _W_COMPACT * volume - _W_AUTO * auto


def _center(p: Placement, m: Machine) -> tuple[float, float, float]:
    return (
        p.cell.x + m.footprint.sx / 2,
        p.cell.y + m.footprint.sy / 2,
        p.cell.z + m.footprint.sz / 2,
    )


def _move(
    placements: list[Placement],
    machines: dict[str, Machine],
    region: CellBox,
    reserved: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    """Propose one move; return a VALID candidate layout, or None if it could not be made."""
    roll = rng.random()
    if roll < 0.34:
        return _relocate(placements, machines, region, reserved, rng)
    if roll < 0.67:
        return _swap(placements, machines, region, reserved, rng)
    return _reorient(placements, machines, rng)


def _relocate(
    placements: list[Placement],
    machines: dict[str, Machine],
    region: CellBox,
    reserved: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    i = rng.randrange(len(placements))
    p = placements[i]
    m = machines[p.machine_id]
    others = _cells_except(placements, machines, {i})
    for _ in range(_RELOCATE_TRIES):
        origin = _rand_origin(m, region, rng)
        if origin is None:
            return None
        cells = list(occupied_cells(origin, m.footprint))
        if (
            all(in_region(c, region) for c in cells)
            and reserved.isdisjoint(cells)
            and others.isdisjoint(cells)
        ):
            new = list(placements)
            new[i] = p.model_copy(update={"cell": origin})
            return new
    return None


def _swap(
    placements: list[Placement],
    machines: dict[str, Machine],
    region: CellBox,
    reserved: set[Cell],
    rng: random.Random,
) -> list[Placement] | None:
    i, j = rng.sample(range(len(placements)), 2)
    pi, pj = placements[i], placements[j]
    mi, mj = machines[pi.machine_id], machines[pj.machine_id]
    others = _cells_except(placements, machines, {i, j})
    moved = list(occupied_cells(pj.cell, mi.footprint)) + list(
        occupied_cells(pi.cell, mj.footprint)
    )
    if (
        not all(in_region(c, region) for c in moved)
        or len(set(moved)) != len(moved)  # the two swapped bodies overlap each other
        or not reserved.isdisjoint(moved)
        or not others.isdisjoint(moved)
    ):
        return None
    new = list(placements)
    new[i] = pi.model_copy(update={"cell": pj.cell})
    new[j] = pj.model_copy(update={"cell": pi.cell})
    return new


def _reorient(
    placements: list[Placement], machines: dict[str, Machine], rng: random.Random
) -> list[Placement] | None:
    options = [
        k for k, p in enumerate(placements) if len(machines[p.machine_id].orientation_options) > 1
    ]
    if not options:
        return None
    k = rng.choice(options)
    p = placements[k]
    new_o = rng.choice(
        [o for o in machines[p.machine_id].orientation_options if o != p.orientation]
    )
    new = list(placements)
    new[k] = p.model_copy(update={"orientation": new_o})
    return new


def _ruin_and_recreate(
    placements: list[Placement],
    machines: dict[str, Machine],
    region: CellBox,
    reserved: set[Cell],
    adjacency: dict[str, set[str]],
    machine_nets: dict[str, list[tuple[list[str], float]]],
    machine_auto: dict[str, list[tuple[str, str]]],
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
    ruined = _related_cluster(placements, adjacency, rng.randint(2, min(n, _MAX_RUIN)), rng)
    kept = [p for i, p in enumerate(placements) if i not in ruined]

    occupied: set[Cell] = set()
    for p in kept:
        occupied.update(occupied_cells(p.cell, machines[p.machine_id].footprint))

    placed = list(kept)
    placed_ids = {p.machine_id for p in placed}
    # Re-insert the most-constrained first (most already-placed net-neighbours) so the strongest
    # pulls choose their spot before the freer machines fill in around them.
    to_insert = sorted(
        (placements[i] for i in sorted(ruined)),
        key=lambda p: -_placed_neighbor_count(p.machine_id, adjacency, placed_ids),
    )
    for p in to_insert:
        m = machines[p.machine_id]
        spot = _best_insertion(
            p,
            m,
            placed,
            occupied,
            region,
            reserved,
            machines,
            adjacency,
            machine_nets,
            machine_auto,
            rng,
        )
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
    m: Machine,
    placed: list[Placement],
    occupied: set[Cell],
    region: CellBox,
    reserved: set[Cell],
    machines: dict[str, Machine],
    adjacency: dict[str, set[str]],
    machine_nets: dict[str, list[tuple[list[str], float]]],
    machine_auto: dict[str, list[tuple[str, str]]],
    rng: random.Random,
) -> tuple[CellCoord, Facing] | None:
    """The valid (origin, orientation) for ``m`` that minimises its *marginal* cost, over candidate
    cells beside its placed net-neighbours plus a few random ones; falls back to any first-fit free
    slot, or ``None`` if the machine cannot be placed at all.

    Ranking uses only the terms that depend on this machine (its nets + auto pairs + flat bias), so
    an insertion costs O(machine degree), not a full O(all nets) recompute - the loop's ``_cost``
    still gates acceptance globally.
    """
    placed_pos = {q.machine_id: q for q in placed}
    best: tuple[CellCoord, Facing] | None = None
    best_cost = math.inf
    for origin in _candidate_origins(p, m, placed, region, adjacency, machines, rng):
        cells = list(occupied_cells(origin, m.footprint))
        if (
            not all(in_region(c, region) for c in cells)
            or not reserved.isdisjoint(cells)
            or not occupied.isdisjoint(cells)
        ):
            continue
        for orientation in m.orientation_options:
            cost = _marginal_insertion_cost(
                p.machine_id,
                origin,
                orientation,
                m,
                placed_pos,
                machines,
                machine_nets,
                machine_auto,
            )
            if cost < best_cost:
                best_cost, best = cost, (origin, orientation)
    if best is not None:
        return best
    fallback = _first_fit(m, region, occupied | reserved)  # last resort: any free slot (in-order)
    return (fallback, m.orientation_options[0]) if fallback is not None else None


def _marginal_insertion_cost(
    machine_id: str,
    origin: CellCoord,
    orientation: Facing,
    m: Machine,
    placed_pos: dict[str, Placement],
    machines: dict[str, Machine],
    machine_nets: dict[str, list[tuple[list[str], float]]],
    machine_auto: dict[str, list[tuple[str, str]]],
) -> float:
    """The cost terms that change with where ``machine_id`` goes: the weighted HPWL of its own nets
    over their already-placed members (this candidate included), minus the auto-output reward for
    the pairs the candidate makes face-adjacent. A cheap marginal proxy of ``_cost`` for ranking
    candidate insertions; the annealing loop's full ``_cost`` still gates acceptance (the
    bounding-box volume term, which this per-machine view cannot see, included)."""
    cx = origin.x + m.footprint.sx / 2
    cy = origin.y + m.footprint.sy / 2
    cz = origin.z + m.footprint.sz / 2
    wire = 0.0
    for ids, weight in machine_nets[machine_id]:
        xs, ys, zs = [cx], [cy], [cz]
        for mid in ids:
            if mid != machine_id and mid in placed_pos:
                ox, oy, oz = _center(placed_pos[mid], machines[mid])
                xs.append(ox)
                ys.append(oy)
                zs.append(oz)
        if len(xs) > 1:
            wire += weight * (max(xs) - min(xs) + max(ys) - min(ys) + max(zs) - min(zs))
    auto = 0
    for src, sink in machine_auto[machine_id]:
        other = sink if src == machine_id else src
        op = placed_pos.get(other)
        if op is None:
            continue
        om = machines[other]
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
    return _W_WIRE * wire - _W_AUTO * auto


def _candidate_origins(
    p: Placement,
    m: Machine,
    placed: list[Placement],
    region: CellBox,
    adjacency: dict[str, set[str]],
    machines: dict[str, Machine],
    rng: random.Random,
) -> list[CellCoord]:
    """Origins to try inserting ``m`` at: its freed origin, the cells face-adjacent to each placed
    net-neighbour (to cluster / auto-output), and a few random free origins - deduped, in-region."""
    neighbor_ids = adjacency.get(p.machine_id, set())
    origins: list[CellCoord] = []
    seen: set[Cell] = set()

    def add(c: Cell) -> None:
        if c not in seen and in_region(c, region):
            seen.add(c)
            origins.append(CellCoord(x=c[0], y=c[1], z=c[2]))

    add((p.cell.x, p.cell.y, p.cell.z))  # freed origin (may be retaken; validity checked by caller)
    for q in placed:
        if len(origins) >= _MAX_CANDIDATES:
            break  # enough neighbour-adjacent sites; keep recreate cheap
        if q.machine_id in neighbor_ids:
            for bx, by, bz in occupied_cells(q.cell, machines[q.machine_id].footprint):
                for dx, dy, dz in _FACE_DELTAS:
                    add((bx + dx, by + dy, bz + dz))
    for _ in range(_LNS_RANDOM_CANDIDATES):
        origin = _rand_origin(m, region, rng)
        if origin is not None:
            add((origin.x, origin.y, origin.z))
    return origins


def _cells_except(
    placements: list[Placement], machines: dict[str, Machine], exclude: set[int]
) -> set[Cell]:
    cells: set[Cell] = set()
    for k, p in enumerate(placements):
        if k not in exclude:
            cells.update(occupied_cells(p.cell, machines[p.machine_id].footprint))
    return cells


def _rand_origin(m: Machine, region: CellBox, rng: random.Random) -> CellCoord | None:
    fp = m.footprint
    if fp.sx > region.sx or fp.sy > region.sy or fp.sz > region.sz:
        return None
    return CellCoord(
        x=rng.randrange(region.sx - fp.sx + 1),
        y=rng.randrange(region.sy - fp.sy + 1),
        z=rng.randrange(region.sz - fp.sz + 1),
    )
