"""placement.search - Phase 2 simulated-annealing placement with a routing-aware cost.

Starts from the constructive first-fit solution and improves it under a cost that proxies
buildability: half-perimeter wirelength (HPWL) per net pulls connected machines together (more
auto-output, shorter pipes), an auto-output reward favours orientations whose usable (non-front)
faces actually let a source eject into its sink, plus a mild compactness + flat-build bias. The
auto reward is the only orientation-dependent term, so reorient moves carry a real cost signal -
without it they were free random walk that could finalize an orientation BLOCKING auto-output.
Moves are relocate / swap / reorient (orientation is a search variable); Metropolis acceptance
with geometric cooling keeps the best valid layout seen.

    initial = constructive.place      # a valid seed
    repeat for a seeded budget:
        cand = relocate | swap | reorient   (only ever a VALID candidate, else skip)
        accept if cheaper, or with prob exp(-d/T)   ; track best-so-far ; cool T

Every accepted state is overlap/bounds/reserved-clean, so the validator still independently
certifies the output. Deterministic for a given ``seed``. LNS rip-and-reinsert and a true
place<->route feedback loop are the next steps (docs/ROADMAP.md lanes C + solver).
"""

from __future__ import annotations

import math
import random

from gtnh_solver.ir import CellBox, CellCoord, Commodity, InputIR, IODirection, Machine, Placement
from gtnh_solver.ir.geometry import Cell, auto_output_faces, in_region, occupied_cells

from .constructive import PlacementResult, place

# Cost weights: wirelength dominates (it drives auto-output + short pipes); an auto-output reward
# makes orientation matter (the front face carries no I/O, so the wrong orientation BLOCKS the
# free connection - reorient moves are a no-op on the other terms); compactness and a flat-build
# bias break ties without overriding routability.
_W_WIRE = 1.0
_W_COMPACT = 0.02
_W_LAYERS = 1.0
_W_AUTO = 4.0

# Annealing schedule (geometric cooling). Budget scales with machine count, clamped.
_T0 = 2.0
_ALPHA = 0.995
_MIN_ITERS = 250
_PER_MACHINE = 60
_MAX_ITERS = 6000
_RELOCATE_TRIES = 20


def optimize_placement(problem: InputIR, *, seed: int = 0) -> PlacementResult:
    """Anneal the constructive placement toward a lower routing-aware cost (seeded, validated)."""
    base = place(problem)
    if not base.ok or len(base.placements) < 2:
        return base  # infeasible, or nothing to optimize (0/1 machine)

    machines = {m.id: m for m in problem.machines}
    region = problem.bounding_region
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    # Nets that are physically routed (skip ME-toggled commodities); just the machine ids per net.
    nets = [
        [e.machine_id for e in n.endpoints]
        for n in problem.nets
        if not problem.me_toggles.toggled(n.commodity)
    ]
    # Directed source->sink pairs that COULD auto-output (simple 1->1 item/fluid nets, like the
    # solver's own rule): the cost rewards each pair the current placement+orientation makes
    # face-adjacent, so orientation has a gradient toward enabling the free connection.
    auto_pairs = _auto_candidate_pairs(problem)
    rng = random.Random(seed)

    current = list(base.placements)
    current_cost = _cost(current, machines, nets, auto_pairs)
    best, best_cost = current, current_cost
    iters = min(_MAX_ITERS, max(_MIN_ITERS, _PER_MACHINE * len(current)))
    temp = _T0
    for _ in range(iters):
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
    nets: list[list[str]],
    auto_pairs: list[tuple[str, str]],
) -> float:
    """Routing-aware cost: net HPWL + compactness (bbox volume) + layer count, minus an
    auto-output reward (the only orientation-dependent term, so reorient moves are not free)."""
    pos = {p.machine_id: p for p in placements}
    wire = 0.0
    for machine_ids in nets:
        centers = [_center(pos[mid], machines[mid]) for mid in machine_ids if mid in pos]
        if len(centers) < 2:
            continue
        for axis in range(3):
            coords = [c[axis] for c in centers]
            wire += max(coords) - min(coords)

    cells = [
        c for p in placements for c in occupied_cells(p.cell, machines[p.machine_id].footprint)
    ]
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    zs = [c[2] for c in cells]
    volume = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1) * (max(zs) - min(zs) + 1)
    layers = len(set(ys))

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
    return _W_WIRE * wire + _W_COMPACT * volume + _W_LAYERS * layers - _W_AUTO * auto


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
