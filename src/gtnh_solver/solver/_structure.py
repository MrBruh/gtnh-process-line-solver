"""solver._structure - what the builder actually erects, measured one way for every consumer.

The feedback loop's compactness ranking, the metrics the previewer reports, and the power-source
repair pass all need the same two answers: how big is this build, and how much cable does it cost.
They share these helpers rather than each re-deriving the extents, so the loop cannot rank attempts
on one measure while the repair improves them toward another.

The *structure* is every machine cell plus every route cell - a trunk sprawling outside the machine
block is something the builder erects, so it counts against the layout.
"""

from __future__ import annotations

from collections.abc import Sequence

from gtnh_solver.ir import InputIR, Placement, Route
from gtnh_solver.ir.geometry import Cell, occupied_cells
from gtnh_solver.placement import Objective


def structure_cells(
    problem: InputIR, placements: Sequence[Placement], routes: Sequence[Route]
) -> set[Cell]:
    """Every grid cell the build occupies - machine footprints plus route hops."""
    machines = {m.id: m for m in problem.machines}
    cells: set[Cell] = set()
    for p in placements:
        machine = machines.get(p.machine_id)
        if machine is not None:
            cells.update(occupied_cells(p.cell, machine.footprint, p.orientation))
    for r in routes:
        cells.update(r.cells())
    return cells


def footprint_and_layers(cells: set[Cell]) -> tuple[int, int]:
    """(floor-area footprint, layer count) for a non-empty occupied-cell set. ``volume`` is
    ``footprint * layers`` - the enclosing box - since the floor area already spans x/z.
    Precondition: ``cells`` is non-empty; every caller returns early on an empty layout."""
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    zs = [c[2] for c in cells]
    footprint = (max(xs) - min(xs) + 1) * (max(zs) - min(zs) + 1)
    layers = max(ys) - min(ys) + 1
    return footprint, layers


def structure_quality(
    problem: InputIR,
    placements: Sequence[Placement],
    routes: Sequence[Route],
    objective: Objective,
) -> tuple[int, int, int]:
    """Rank an assembled structure; smaller-lexicographic is better.

    The ``objective``'s compactness metric leads (``footprint`` = floor area, ``volume`` =
    enclosing box, ``balanced`` = their sum); the real route cells come second, pipes and cable
    alike - only a routed layout knows them (placement-time proxies cannot see dock faces or shared
    taps) - and the other compactness metric breaks ties toward the smaller build.

    Pipes count as well as cable because a pipe block is built exactly like a cable block, and a
    tighter layout is one that needs fewer of either: with cable alone, two attempts of one
    footprint ranked the same however many pipes each laid. The power-source repair pass ranks on
    this key too, and there the pipes are the same for every candidate, so they cannot change which
    pose wins.
    """
    cells = structure_cells(problem, placements, routes)
    if not cells:
        return (0, 0, 0)
    route_cells: set[Cell] = set()
    for r in routes:
        route_cells.update(r.cells())
    footprint, layers = footprint_and_layers(cells)
    volume = footprint * layers
    if objective == "volume":
        return (volume, len(route_cells), footprint)
    if objective == "balanced":
        return (footprint + volume, len(route_cells), volume)
    return (footprint, len(route_cells), volume)
