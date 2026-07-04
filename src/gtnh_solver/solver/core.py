"""solver.core - the place<->route feedback loop that yields a LayoutResult.

One *attempt* assembles a layout (docs/ROADMAP.md):
  1. place the machines (simulated annealing over a routing-aware cost, seeded from the
     constructive first-fit solution - so connected machines cluster for auto-output);
  2. route the item/fluid nets (router.route): the **router** decides from the final geometry
     which nets GT's free **auto-output** connection covers (router.auto) and lays pipes only
     for the rest;
  3. route the synthesized power nets as shared-amperage cable trunks (router.power);
  4. assemble the LayoutResult (or surface the placement/routing/power infeasibility);
  5. **validate the assembled layout against the independent validator** and downgrade a
     VALID result to ``partial_invalid`` if it proves any violation. The validator's logic is
     written independently of the placer/router precisely to catch their bugs, so running it on
     our own output is what makes the "never returns a silently-invalid layout" promise true
     end to end (docs/ARCHITECTURE.md #4) - not just an internal `place.ok && route.ok`.

``solve`` wraps that in the **place<->route feedback loop** (docs/ARCHITECTURE.md #1, #6), which
is also where layout *quality* is judged: cheap placement-time proxies cannot see dock faces or
shared cable taps, so the real per-segment cable cost is only knowable on a routed layout. The
loop is a bounded **multi-start grid** - SA weight modes x seeds - where every attempt is fully
routed + validated and the best VALID layout by the requested objective's quality ranking
(compactness metric, then real power cable cells, then the other metric) is kept, not
first-valid-wins. The footprint weighting always participates as the explorer: it generates the
stacked, cable-dense candidates whose routed structure often wins the volume/balanced rankings
too. If an attempt leaves nets unrouted, it penalizes exactly those nets (so the next placement
pulls their machines tighter - shorter routes, adjacency that auto-outputs, or an MST pull for a
failed power trunk); with no valid layout yet in hand, it stops early when re-placing cannot
help (a non-routing defect, or the same nets failing again). It is **deterministic** (a bounded
grid keyed off ``seed`` + the penalties, no wall-clock), so a given input always yields the same
layout.

``solve(..., optimize=False)`` is the **fast** path: a single constructive placement with no
annealing and no feedback loop (near-instant, simpler layout), still validated. The two modes are
the "optimize or not" choice the planned unified site exposes to the builder.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    Commodity,
    Infeasibility,
    InputIR,
    LayoutMetrics,
    LayoutResult,
    LayoutStatus,
    Placement,
    Route,
)
from gtnh_solver.ir.geometry import occupied_cells
from gtnh_solver.placement import Objective, optimize_placement, place
from gtnh_solver.router import route, route_power
from gtnh_solver.validator import ValidationReport, validate

# Feedback loop bounds. Cycle detection on the failed-net set usually stops sooner when nothing
# routes; this caps the work. The penalty step adds to a net's weight each time it fails to route.
_MAX_FEEDBACK_PASSES = 8
_PENALTY_STEP = 2.0


def solve(
    problem: InputIR,
    *,
    seed: int = 0,
    optimize: bool = True,
    objective: Objective = "footprint",
) -> LayoutResult:
    """Produce a layout for ``problem``; deterministic for a given ``problem`` + ``seed``.

    ``optimize`` selects how hard to work (the site's "optimize or not" control):

    - ``True`` (default): the annealed placer (SA + LNS) inside the place<->route feedback loop.
      Every bounded attempt is fully routed, and the best VALID layout by the ``objective``'s
      quality ranking is returned - tighter, lower-wire layouts at the cost of seconds of CPU. If
      no attempt is fully valid, the best partial is returned.
    - ``False`` (**fast**): a single constructive first-fit placement, no optimization and no
      feedback loop - near-instant and simple. Its layout is still validated, so it is VALID or an
      explicit partial/infeasibility, never silently invalid; but it will not cluster machines for
      auto-output or re-place to rescue an unroutable net the way the optimizer can.

    ``objective`` selects what "compact" means (the site's *second* control, next to optimize or
    not): ``footprint`` (default) minimizes the floor area and stacks tall, ``volume`` minimizes
    the enclosing box and stays flat/cubic, ``balanced`` weighs both. It drives the placement
    cost and the quality ranking; the fast path ignores it (constructive placement is floor-first
    by construction).
    """
    if not optimize:
        return _solve_fast(problem, seed)
    penalties: dict[str, float] = {}
    seen_failed: set[frozenset[str]] = set()
    best_valid: LayoutResult | None = None
    best_quality: tuple[int, int, int] | None = None
    best_partial: LayoutResult | None = None
    best_failures = -1
    # The multi-start grid: SA weight modes x seeds, always ranked by the REQUESTED objective's
    # quality. The footprint weighting is the universal explorer - it is what generates stacked,
    # dense candidates, whose routed structure often wins the volume/balanced rankings too (a
    # pure-volume weighting minimises the machine box and cannot reach them, because the cable
    # space they save is invisible until routing). For the footprint objective the two coincide,
    # so all passes go to its own weighting across more seeds.
    sa_modes: tuple[Objective, ...] = (
        ("footprint",) if objective == "footprint" else (objective, "footprint")
    )
    grid = [
        (mode, seed + i) for i in range(_MAX_FEEDBACK_PASSES // len(sa_modes)) for mode in sa_modes
    ]
    for sa_mode, attempt_seed in grid:
        placement = optimize_placement(
            problem, seed=attempt_seed, net_penalties=penalties, objective=sa_mode
        )
        if not placement.ok:
            # The machines do not fit the region at all - seed-independent, so retrying is futile.
            return LayoutResult(
                status=LayoutStatus.INFEASIBLE,
                seed=attempt_seed,
                infeasibility=placement.infeasibility,
            )

        layout, failed_nets = _assemble(problem, placement.placements, attempt_seed)
        if layout.status is LayoutStatus.VALID:
            # Valid, but maybe not the best the remaining seeds can do: rank it on the real,
            # routed structure and keep exploring (ties keep the earliest attempt).
            quality = _quality(problem, layout, objective)
            if best_quality is None or quality < best_quality:
                best_valid, best_quality = layout, quality
            continue
        if best_partial is None or len(failed_nets) < best_failures:
            best_partial, best_failures = layout, len(failed_nets)  # fewest-unrouted so far

        if best_valid is None:
            # No valid layout in hand: stop early when re-placing cannot possibly help. (Once one
            # exists the remaining attempts are pure multi-start exploration, so keep going.)
            if not failed_nets:
                break  # a non-routing defect (independent validation) - re-placing cannot help
            key = frozenset(failed_nets)
            if key in seen_failed:
                break  # the same nets keep failing - the feedback is not making progress
            seen_failed.add(key)
        for net_id in failed_nets:
            penalties[net_id] = penalties.get(net_id, 0.0) + _PENALTY_STEP

    if best_valid is not None:
        return best_valid
    assert best_partial is not None  # attempt 0 always either returns or sets one of the two
    return best_partial


def _occupied_cells(
    problem: InputIR, placements: list[Placement], routes: list[Route]
) -> set[tuple[int, int, int]]:
    """Every grid cell the build occupies - machine footprints plus route hops. The shared basis
    for the compactness ranking (``_quality``) and the reported metrics (``_layout_metrics``), so
    the previewer's footprint and the loop's ranking footprint are computed the same way."""
    machines = {m.id: m for m in problem.machines}
    cells: set[tuple[int, int, int]] = set()
    for p in placements:
        machine = machines.get(p.machine_id)
        if machine is not None:
            cells.update(occupied_cells(p.cell, machine.footprint))
    for r in routes:
        cells.update(r.cells())
    return cells


def _footprint_and_layers(cells: set[tuple[int, int, int]]) -> tuple[int, int]:
    """(floor-area footprint, layer count) for a non-empty occupied-cell set. ``volume`` is
    ``footprint * layers`` - the enclosing box - since the floor area already spans x/z.
    Precondition: ``cells`` is non-empty; both callers return early on an empty layout."""
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    zs = [c[2] for c in cells]
    footprint = (max(xs) - min(xs) + 1) * (max(zs) - min(zs) + 1)
    layers = max(ys) - min(ys) + 1
    return footprint, layers


def _layout_metrics(
    problem: InputIR, placements: list[Placement], routes: list[Route]
) -> LayoutMetrics:
    """Advisory compactness metrics for an assembled layout: floor-area ``footprint`` and
    ``layers`` (vertical extent), the two the previewer surfaces (previewer/scene.py). An empty
    layout (nothing placed, e.g. an infeasible result) leaves them ``None``. ``buildability`` /
    ``congestion`` stay ``None`` too - they need a defined scoring model (docs/ROADMAP.md), so
    they are left deferred rather than faked."""
    cells = _occupied_cells(problem, placements, routes)
    if not cells:
        return LayoutMetrics()
    footprint, layers = _footprint_and_layers(cells)
    return LayoutMetrics(footprint=footprint, layers=layers)


def _quality(problem: InputIR, layout: LayoutResult, objective: Objective) -> tuple[int, int, int]:
    """Rank a VALID layout for the feedback loop; smaller-lexicographic is better.

    The *structure* is every machine and route cell - what the builder actually erects, so a
    trunk sprawling outside the machine block counts against the layout. The ``objective``'s
    compactness metric leads (``footprint`` = floor area, ``volume`` = enclosing box,
    ``balanced`` = their sum); real power cable cells come second - only a routed layout knows
    them (placement-time proxies cannot see dock faces or shared taps) - and the other
    compactness metric breaks ties toward the smaller build.
    """
    cells = _occupied_cells(problem, layout.placements, layout.routes)
    power_cells: set[tuple[int, int, int]] = set()
    for r in layout.routes:
        if r.commodity is Commodity.POWER:
            power_cells.update(r.cells())
    if not cells:
        return (0, 0, 0)
    footprint, layers = _footprint_and_layers(cells)
    volume = footprint * layers
    if objective == "volume":
        return (volume, len(power_cells), footprint)
    if objective == "balanced":
        return (footprint + volume, len(power_cells), volume)
    return (footprint, len(power_cells), volume)


def _solve_fast(problem: InputIR, seed: int) -> LayoutResult:
    """One deterministic attempt over the constructive placement - the fast (no-optimize) path.

    Constructive placement is seed-independent, so there is no annealing to run and no point re-
    placing (the feedback loop would just get the same layout back); a single assemble+validate is
    the whole job. The result is validated like any other, so it is VALID, an explicit
    partial_invalid, or an explicit infeasibility.
    """
    placement = place(problem)
    if not placement.ok:
        return LayoutResult(
            status=LayoutStatus.INFEASIBLE, seed=seed, infeasibility=placement.infeasibility
        )
    layout, _ = _assemble(problem, placement.placements, seed)
    return layout


def _assemble(
    problem: InputIR, placements: tuple[Placement, ...], seed: int
) -> tuple[LayoutResult, tuple[str, ...]]:
    """Route, validate, and compose the layout; return it plus the unrouted net ids.

    The router owns the auto-output vs pipe decision (router.auto), so its result carries both
    the auto-connections and the pipes. The unrouted ids are the feedback signal (empty when
    fully routed). A layout that routes everything yet fails independent validation returns
    ``partial_invalid`` with *no* failed nets: that is a solver/router bug, not a routability
    problem, so re-placing would not help.
    """
    routing = route(problem, placements)  # auto-output where geometry allows + item/fluid pipes
    autos = list(routing.auto_connections)
    # Power cables route around the item/fluid pipes already laid, so no cell carries two routes
    # (the crude single-channel capacity the validator enforces). docs/ARCHITECTURE.md #7.
    item_cells = {cell for r in routing.routes for cell in r.cells()}
    power = route_power(problem, placements, extra_obstacles=item_cells)
    routes = [*routing.routes, *power.routes]
    placement_list = list(placements)
    metrics = _layout_metrics(problem, placement_list, routes)  # footprint/layers for every result

    infeasibility = routing.infeasibility or power.infeasibility
    if infeasibility is not None:
        layout = LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=infeasibility,
            placements=placement_list,
            routes=routes,
            auto_connections=autos,
            metrics=metrics,
        )
        return layout, (*routing.failed_nets, *power.failed_nets)

    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=seed,
        placements=placement_list,
        routes=routes,
        auto_connections=autos,
        metrics=metrics,
    )
    # The placer and router each report success on their own terms; the validator is the only
    # gate written independently of them, so run it on the assembled layout before claiming VALID.
    # If it proves a violation, that is a bug in our own output - surface it as partial_invalid
    # rather than handing back a silently-invalid layout.
    report = validate(problem, layout)
    if not report.ok:
        downgraded = LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=_validation_infeasibility(report),
            placements=placement_list,
            routes=routes,
            auto_connections=autos,
            metrics=metrics,
        )
        return downgraded, ()
    return layout, ()


def _validation_infeasibility(report: ValidationReport) -> Infeasibility:
    """An Infeasibility describing why our own assembled layout failed independent validation."""
    codes = ", ".join(v.code.value for v in report.violations)
    return Infeasibility(
        constraint="validation",
        detail=(
            f"the assembled layout failed independent validation "
            f"({len(report.violations)} violation(s): {codes})"
        ),
        suggested_relaxation=(
            "this indicates a solver/router bug - the placement or routes are geometrically "
            "invalid; report it with the failing input"
        ),
    )
