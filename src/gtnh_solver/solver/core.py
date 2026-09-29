"""solver.core - the multi-start of place-and-route attempts that yields a LayoutResult.

One *attempt* assembles a layout (docs/ROADMAP.md):
  1. place the machines (simulated annealing over a routing-aware cost, seeded from the
     constructive first-fit solution - so connected machines cluster for auto-output);
  2. route the nets (router.route): the **router** decides from the final geometry which nets
     GT's free **auto-output** connection covers (router.auto) and negotiates every other net
     together - the pipes, and each power net as a reserved tree so the pipes leave its cable room;
  3. lay the synthesized power nets as shared-amperage cable trunks (router.power) - through
     the **repair pass** (solver.repair), which first relocates each power source onto the
     cheapest cable a real routing can find for it, since the placement cost cannot see cable;
  4. assemble the LayoutResult (or surface the placement/routing/power infeasibility);
  5. **validate the assembled layout against the independent validator** and downgrade a
     VALID result to ``partial_invalid`` if it proves any violation. The validator's logic is
     written independently of the placer/router precisely to catch their bugs, so running it on
     our own output is what makes the "never returns a silently-invalid layout" promise true
     end to end (docs/ARCHITECTURE.md #4) - not just an internal `place.ok && route.ok`.

One attempt, as ``_assemble`` runs it::

    placements
      |  route          auto-output, then every other net negotiated together - pipes, plus a
      |                 reserved tree per power net so the pipes leave its cable room (#164)
      |  power          repair_power_sources (optimize) or route_power (fast): the real cable
      |  place_hatches  a hatch per connection, plus maintenance and muffler
      |  validate       VALID, or downgraded to partial_invalid
      v
    the layout, and the nets it names as failed (how a partial layout is ranked)

There is no second pass. Power used to route after the pipes with one dock cell held per energy
port, and a pipe could wall that cell into a pocket, so a failed power net got a power-first
recovery pass (#226). The power nets now take part in the pipes' negotiation, which leaves their
cable room, and the recovery never rescued an attempt after that (#164), so it is gone.

``solve`` wraps that in a bounded **multi-start** (docs/ARCHITECTURE.md #1, #6), which is also
where layout *quality* is judged: cheap placement-time proxies cannot see dock faces or shared cable
taps, so the real per-segment cable cost is only knowable on a routed layout. The attempts form a
grid - SA weight modes x seeds - where every attempt is fully routed + validated and the best VALID
layout by the requested objective's quality ranking (compactness metric, then real route cells -
pipes and cable - then the other metric) is kept, not first-valid-wins. The footprint weighting
always participates as the explorer: it generates the stacked, cable-dense candidates whose routed
structure often wins the volume/balanced rankings too.

The attempts are **independent**: each anneals under its own seed and nothing one attempt learns
reaches another. They used to feed each other - a net one attempt left unrouted was penalized in
every later attempt's cost, and the loop stopped early when the same nets kept failing - but that
feedback measured as no better than none: over 53 solves on five lines, their seeds spaced so that
no two solves share an attempt, 45 returned the same layout without it, 6 a better one and 2 a
worse one. Dropping it is what lets the attempts run side by side::

    the grid of (weighting, seed) attempts         each one: anneal -> gate -> _assemble
      attempt 0, in this process, timed
      the rest: in a pool of ``jobs`` processes when attempt 0 took longer than a pool takes to
                start (_POOL_AFTER_S), else in turn, in this process
    rank in grid order: the best VALID layout, else the fewest unrouted nets, else lay the first
    placement the gate turned away
    a partial layout's reason names any single block with more connections than faces, the cause
    no placement can fix (placement.single_block_shortfalls), ahead of the routers' own words

An attempt returns the same thing whichever process runs it, and the ranking reads the attempts in
grid order, so a given input and ``seed`` yields the same layout whatever ``jobs`` is and however
the timing falls. The clock only decides whether a pool is worth starting.

One candidate comes from outside the grid. A line that is one chain of banks of parallel single
blocks has a compact layout the annealer does not reach, each bank a column and each pair of stages
sharing one straight pipe run (``placement.banks``). It is routed first and ranked with the rest,
so it wins only where the routed structure really is smaller; on any other line there is no such
candidate and the loop is exactly the grid.

``solve(..., optimize=False)`` is the **fast** path: a single constructive placement with no
annealing and no multi-start (near-instant, simpler layout), still validated. The two modes are
the "optimize or not" choice the planned unified site exposes to the builder.

**Effort** sets how hard the optimized path works, not what it does. ``full`` (the default) is the
search described above, the one layout quality is judged on. ``minimal`` runs every stage once on
small budgets: one attempt, a short anneal, and a cap on the router's negotiation rounds, which are
what a line that cannot route spends its time on (about a second a round on nitrobenzene). It is
for checking that a line solves, the test suite's default, and a quick preview; its layouts are
worse, and just as validated::

    effort    attempts   anneal iterations       negotiation rounds
    full      8          the schedule (by size)  the router's backstop (32)
    minimal   1          at most 250             at most 8
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Literal

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
from gtnh_solver.placement import (
    SINGLE_BLOCK_IO_FACES,
    Objective,
    bank_columns,
    crowded_machines,
    optimize_placement,
    place,
    single_block_shortfalls,
)
from gtnh_solver.router import (
    claims_by_machine,
    place_hatches,
    route,
    route_power,
)
from gtnh_solver.validator import ValidationReport, ViolationCode, validate

from ._structure import footprint_and_layers, structure_cells, structure_quality
from .repair import repair_power_sources

#: How hard the optimized path works (module docstring): ``full`` searches for the best layout,
#: ``minimal`` checks that the line solves.
Effort = Literal["minimal", "full"]

DEFAULT_EFFORT: Effort = "full"
"""The effort a caller that names none gets. Read at call time rather than bound as a default, so
the test suite can make every solve ``minimal`` for a session (tests/conftest.py), the way it pins
``dataset.roots.DEFAULT_DATA``."""


@dataclass(frozen=True)
class _Budget:
    """What an effort buys. ``None`` leaves a stage to its own schedule."""

    attempts: int  # of the multi-start grid (weighting modes x seeds)
    anneal_iterations: int | None  # a cap on each attempt's annealing schedule
    negotiation_rounds: int | None  # a cap on the router's rounds


_BUDGETS: dict[Effort, _Budget] = {
    "full": _Budget(attempts=8, anneal_iterations=None, negotiation_rounds=None),
    "minimal": _Budget(attempts=1, anneal_iterations=250, negotiation_rounds=8),
}

#: How long attempt 0 has to take before the rest are worth a process pool. Starting one costs
#: about a second on the maintainer's 4-core machine, since each process imports the solver afresh.
#: A pool of 4 started for every solve made parallel-sand's (attempts of about 0.4 s) 1.5x slower
#: and nitrobenzene's (1 to 2 s) 2.4x faster. With this threshold, parallel-sand and sand never start
#: one, and nitrobenzene and ev-nitrobenzene solve 2.0x and 1.8x faster than one attempt at a time.
_POOL_AFTER_S = 1.0


def solve(
    problem: InputIR,
    *,
    seed: int = 0,
    optimize: bool = True,
    objective: Objective = "footprint",
    jobs: int = 1,
    effort: Effort | None = None,
) -> LayoutResult:
    """Produce a layout for ``problem``; deterministic for a given ``problem`` + ``seed``.

    ``optimize`` selects how hard to work (the site's "optimize or not" control):

    - ``True`` (default): the annealed placer (SA + LNS) in a multi-start of independent
      attempts. Every bounded attempt is fully routed, and the best VALID layout by the
      ``objective``'s quality ranking is returned - tighter, lower-wire layouts at the cost of
      seconds of CPU. If no attempt is fully valid, the best partial is returned.
    - ``False`` (**fast**): a single constructive first-fit placement, no optimization and no
      multi-start - near-instant and simple. Its layout is still validated, so it is VALID or an
      explicit partial/infeasibility, never silently invalid; but it will not cluster machines for
      auto-output, relocate a power source onto shorter cable, or try another placement for an
      unroutable net the way the optimizer can.

    ``objective`` selects what "compact" means (the site's *second* control, next to optimize or
    not): ``footprint`` (default) minimizes the floor area and stacks tall, ``volume`` minimizes
    the enclosing box and stays flat/cubic, ``balanced`` weighs both. It drives the placement
    cost and the quality ranking; the fast path ignores it (constructive placement is floor-first
    by construction).

    ``jobs`` is how many processes the attempts may run in (module docstring). It changes how long
    a solve takes, never what it returns; ``1`` runs every attempt in this process.

    ``effort`` sets the optimized path's budgets (module docstring): ``full`` searches for the best
    layout, ``minimal`` runs every stage once on small budgets. None takes :data:`DEFAULT_EFFORT`.
    The fast path ignores it, having only the one attempt.
    """
    if not optimize:
        return _with_shortfall_reason(problem, _solve_fast(problem, seed, objective))
    budget = _BUDGETS[effort or DEFAULT_EFFORT]
    # The first placement the gate turned away, kept as a parachute. The gate is a heuristic about
    # geometry and the routers are the authority, so it is only ever allowed to pick BETTER
    # attempts - never to declare a line unsolvable that the routers would in fact have solved.
    gated: tuple[Placement, ...] | None = None
    best_valid: LayoutResult | None = None
    best_quality: tuple[int, int, int] | None = None
    best_partial: LayoutResult | None = None
    best_failures = -1
    # The bank-column candidate (module docstring). Only a VALID result is kept: it is not an
    # annealed placement, and a partial one would only compete with the attempts for last place.
    columns = bank_columns(problem)
    if columns is not None and not crowded_machines(problem, columns):
        layout, _ = _assemble(
            problem, columns, seed, objective, max_rounds=budget.negotiation_rounds
        )
        if layout.status is LayoutStatus.VALID:
            best_valid, best_quality = layout, _quality(problem, layout, objective)
    # The multi-start grid: SA weight modes x seeds, always ranked by the REQUESTED objective's
    # quality. The footprint weighting is the universal explorer - it is what generates stacked,
    # dense candidates, whose routed structure often wins the volume/balanced rankings too (a
    # pure-volume weighting minimises the machine box and cannot reach them, because the cable
    # space they save is invisible until routing). For the footprint objective the two coincide,
    # so all attempts go to its own weighting across more seeds. A budget smaller than one seed of
    # every mode (minimal's single attempt) keeps the requested objective's own weighting.
    sa_modes: tuple[Objective, ...] = (
        ("footprint",) if objective == "footprint" else (objective, "footprint")
    )
    seeds = -(-budget.attempts // len(sa_modes))  # ceiling division
    grid = [(mode, seed + i) for i in range(seeds) for mode in sa_modes][: budget.attempts]
    # Read in grid order, whichever process ran what, so ties keep the earliest attempt.
    for attempt in _run_attempts(problem, grid, objective, jobs, budget):
        if attempt.infeasibility is not None:
            # The machines do not fit the region at all - seed-independent, so no attempt can.
            return LayoutResult(
                status=LayoutStatus.INFEASIBLE,
                seed=attempt.seed,
                infeasibility=attempt.infeasibility,
            )
        if attempt.layout is None:
            gated = gated or attempt.gated
            continue
        if attempt.layout.status is LayoutStatus.VALID:
            # Valid, but maybe not the best the other seeds found: rank it on the real, routed
            # structure (ties keep the earliest attempt).
            quality = _quality(problem, attempt.layout, objective)
            if best_quality is None or quality < best_quality:
                best_valid, best_quality = attempt.layout, quality
        elif best_partial is None or len(attempt.failed_nets) < best_failures:
            best_partial, best_failures = attempt.layout, len(attempt.failed_nets)

    if best_valid is not None:
        return best_valid
    if best_partial is None:
        # Every attempt was turned away, so nothing was ever routed. Do NOT report the crowding as
        # the verdict: the gate is a model of the routers' docking rules, and it has been wrong
        # about them before (#164). Lay the first placement it rejected and let the routers say -
        # they are the authority, and a real shortage still surfaces as their own infeasibility
        # (face_reachability, routing or congestion). The gate has then cost an attempt and
        # changed nothing else.
        assert gated is not None  # the only path that skips every attempt sets it
        layout, _ = _assemble(problem, gated, seed, objective, max_rounds=budget.negotiation_rounds)
        return _with_shortfall_reason(problem, layout)
    return _with_shortfall_reason(problem, best_partial)


def _with_shortfall_reason(problem: InputIR, layout: LayoutResult) -> LayoutResult:
    """``layout``, its reason restated when a single block has more connections than faces.

    The routers report such a line by whichever net lost the race for the last free face, often as
    congestion with advice to spread the machines apart, which no amount of room satisfies. The
    shortfall is the cause, so the reason names it first and keeps the routers' words after it. Only
    a partial layout is restated: a VALID one needs no reason, and an INFEASIBLE one (machines that
    do not fit the region at all) has a different cause.
    """
    if layout.status is not LayoutStatus.PARTIAL_INVALID:
        return layout
    shortfalls = single_block_shortfalls(problem)
    if not shortfalls:
        return layout
    types = {m.id: m.type for m in problem.machines}
    named = ", ".join(f"{mid!r} ({types[mid]}, {n})" for mid, n in shortfalls.items())
    routers = layout.infeasibility.detail if layout.infeasibility is not None else "no reason"
    return layout.model_copy(
        update={
            "infeasibility": Infeasibility(
                constraint="single_block_faces",
                detail=(
                    f"more connections than a single block has faces for: {named}. The solver gives "
                    f"each connection a face of its own, and a single block has "
                    f"{SINGLE_BLOCK_IO_FACES} that can carry one (the front carries none), or only "
                    f"the faces its ports are pinned to, so no placement lays these. It sends a single block's item outputs out of one face "
                    f"to Item Filters only when it can prove the machine is a single block: its "
                    f"handler says so, or a census dataset for the plan's pack lacks it. The "
                    f"routers stopped at: {routers}"
                ),
                suggested_relaxation=(
                    "if a machine is a multiblock, load a dataset with its structure so it is "
                    "placed at its real size (run the extractor for the plan's pack); if it is a "
                    "single block, load a census dataset for the plan's pack so the machine can be "
                    "proven a single block and its item outputs sorted by Item Filters; otherwise "
                    "move one of its commodities to ME, or split its work across more machines"
                ),
            )
        }
    )


@dataclass(frozen=True)
class _Attempt:
    """What one attempt came to. At most one of the three is set: the machines did not fit the
    region, the crowding gate turned the placement away before routing, or it was routed."""

    seed: int
    infeasibility: Infeasibility | None = None
    gated: tuple[Placement, ...] | None = None
    layout: LayoutResult | None = None
    failed_nets: tuple[str, ...] = ()


def _attempt(
    problem: InputIR, sa_mode: Objective, attempt_seed: int, objective: Objective, budget: _Budget
) -> _Attempt:
    """One attempt of the grid: anneal, gate, and if the gate lets it through, route and validate.

    A function of its arguments alone, so it returns the same thing in a pool process as here. The
    budget is one of them rather than read from :data:`DEFAULT_EFFORT`, which a pool process
    imports afresh.
    """
    placement = optimize_placement(
        problem, seed=attempt_seed, objective=sa_mode, max_iterations=budget.anneal_iterations
    )
    if not placement.ok:
        return _Attempt(attempt_seed, infeasibility=placement.infeasibility)
    # Can every machine dock every connection it carries? Checked before routing, naming a machine
    # only on proof: a crowded placement cannot route, and routing it only to watch an arbitrary
    # net lose the race for the last free face costs an attempt and reports the wrong machine (#76).
    if crowded_machines(problem, placement.placements):
        return _Attempt(attempt_seed, gated=placement.placements)
    layout, failed_nets = _assemble(
        problem,
        placement.placements,
        attempt_seed,
        objective,
        max_rounds=budget.negotiation_rounds,
    )
    return _Attempt(attempt_seed, layout=layout, failed_nets=failed_nets)


def _run_attempts(
    problem: InputIR,
    grid: list[tuple[Objective, int]],
    objective: Objective,
    jobs: int,
    budget: _Budget,
) -> list[_Attempt]:
    """Every attempt of ``grid``, in grid order (module docstring).

    Attempt 0 runs here and is timed. When it took longer than a pool takes to start
    (``_POOL_AFTER_S``) and ``jobs`` allows, the rest run in a pool of up to ``jobs`` processes;
    otherwise they run here in turn. A line whose machines do not fit the region stops at attempt 0,
    since every seed starts from the same constructive placement.
    """
    (first_mode, first_seed), rest = grid[0], grid[1:]
    started = time.perf_counter()
    first = _attempt(problem, first_mode, first_seed, objective, budget)
    if first.infeasibility is not None or not rest:
        return [first]
    if jobs > 1 and time.perf_counter() - started > _POOL_AFTER_S:
        with ProcessPoolExecutor(max_workers=min(jobs, len(rest))) as pool:
            futures = [
                pool.submit(_attempt, problem, mode, s, objective, budget) for mode, s in rest
            ]
            return [first, *(future.result() for future in futures)]
    return [first, *(_attempt(problem, mode, s, objective, budget) for mode, s in rest)]


def _layout_metrics(
    problem: InputIR, placements: list[Placement], routes: list[Route]
) -> LayoutMetrics:
    """Advisory compactness metrics for an assembled layout: floor-area ``footprint`` and
    ``layers`` (vertical extent), the two the previewer surfaces (previewer/scene.py). An empty
    layout (nothing placed, e.g. an infeasible result) leaves them ``None``. ``buildability`` /
    ``congestion`` stay ``None`` too - they need a defined scoring model (docs/ROADMAP.md), so
    they are left deferred rather than faked."""
    cells = structure_cells(problem, placements, routes)
    if not cells:
        return LayoutMetrics()
    footprint, layers = footprint_and_layers(cells)
    return LayoutMetrics(footprint=footprint, layers=layers)


def _quality(problem: InputIR, layout: LayoutResult, objective: Objective) -> tuple[int, int, int]:
    """Rank a VALID layout for the multi-start; smaller-lexicographic is better
    (``_structure.structure_quality`` - the same key the power-source repair pass ranks its own
    candidates on, so the two cannot pull against each other)."""
    return structure_quality(problem, layout.placements, layout.routes, objective)


def _solve_fast(problem: InputIR, seed: int, objective: Objective) -> LayoutResult:
    """One deterministic attempt over the constructive placement - the fast (no-optimize) path.

    Constructive placement is seed-independent, so there is no annealing to run and no point in
    more attempts (every one would get the same layout back); a single assemble+validate is the
    whole job. The result is validated like any other, so it is VALID, an explicit
    partial_invalid, or an explicit infeasibility.
    """
    placement = place(problem)
    if not placement.ok:
        return LayoutResult(
            status=LayoutStatus.INFEASIBLE, seed=seed, infeasibility=placement.infeasibility
        )
    layout, _ = _assemble(problem, placement.placements, seed, objective, repair=False)
    return layout


def _assemble(
    problem: InputIR,
    placements: tuple[Placement, ...],
    seed: int,
    objective: Objective = "footprint",
    *,
    repair: bool = True,
    max_rounds: int | None = None,
) -> tuple[LayoutResult, tuple[str, ...]]:
    """Route, validate, and compose the layout; return it plus the unrouted net ids.

    ``max_rounds`` is the router's negotiation cap (``router.route``); None keeps its backstop.

    The router owns the auto-output vs pipe decision (router.auto), so its result carries both
    the auto-connections and the pipes. The unrouted ids rank a partial layout against the other
    attempts' (empty when fully routed). A layout that routes everything yet fails independent
    validation returns ``partial_invalid`` with *no* failed nets: that is a solver/router bug, not a
    routability problem.

    The placements it returns are not always the ones handed in: with ``repair`` (the optimize
    path) laying power is the repair pass (solver.repair), which may relocate a power source onto
    shorter cable, ranked by ``objective`` - the same key the loop ranks whole attempts on. The
    fast path passes ``repair=False`` and keeps the constructive placement exactly as it is.

    **One violation is the exception** (:func:`_starved_machines`): a machine too far from its
    power source to take in its draw is a *placement* defect, not a bug - every cable is correctly
    thick and only the distance is wrong - so its power net is named as a failed net, and the
    layout ranks as one that left that net unrouted, which another attempt placing the machine
    nearer can beat.
    """
    # Auto-output, then every other net negotiated together: the pipes, and a tree per power net
    # that keeps them off the space its cable needs (router.core, #164).
    routing = route(problem, placements, max_rounds=max_rounds)
    autos = list(routing.auto_connections)
    # Power cables route around the item/fluid pipes already laid, so no cell carries two routes
    # (the crude single-channel capacity the validator enforces). docs/ARCHITECTURE.md #7 - and
    # around the CASING cells those pipes' hatches occupy, which is a different resource: a
    # machine's hatch cells are one shared pool an input bus and an energy hatch compete for, and
    # a single casing cell has up to five free faces, so the pipe cells cannot stand in for it.
    claims = claims_by_machine(routing.routes, {m.id: m for m in problem.machines})
    # The free connections spent casing cells too, and they own no Route to read that off. Without
    # this the power router is the one pass that never hears about them: an energy hatch lands on
    # the cell an auto-output reserved, `place_hatches` then finds no unclaimed touching pair, and
    # the certified connection ends up with no output bus and no input bus - two multiblocks that
    # form correctly and move nothing (#131).
    for machine_id, cells in routing.claimed.items():
        claims.setdefault(machine_id, set()).update(cells)
    if repair:
        # The power router runs inside the repair pass, which relocates each source to the cell
        # its really-routed cable likes best (solver.repair, #123) and hands back that routing.
        placement_list, power = repair_power_sources(
            problem,
            placements,
            item_routes=routing.routes,
            claimed_cells=claims,
            objective=objective,
        )
    else:
        # The fast path is a single constructive placement by definition, so it routes power
        # where the placer put the sources and relocates nothing.
        placement_list = list(placements)
        power = route_power(
            problem,
            placement_list,
            extra_obstacles={cell for r in routing.routes for cell in r.cells()},
            claimed_cells=claims,
        )
    routes = [*routing.routes, *power.routes]
    metrics = _layout_metrics(problem, placement_list, routes)  # footprint/layers for every result

    # Which casing cell each connection turns into a hatch, plus the maintenance hatch and muffler
    # that belong to no net. Last, because a muffler needs empty air in front of it and only a
    # finished routing knows which cells are still empty.
    plan = place_hatches(
        problem, placement_list, routes, autos, structure_cells(problem, placement_list, routes)
    )

    infeasibility = routing.infeasibility or power.infeasibility or plan.infeasibility
    if infeasibility is not None:
        layout = LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=infeasibility,
            placements=placement_list,
            routes=routes,
            auto_connections=autos,
            hatches=list(plan.hatches),
            metrics=metrics,
        )
        return layout, (*routing.failed_nets, *power.failed_nets)

    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=seed,
        placements=placement_list,
        routes=routes,
        auto_connections=autos,
        hatches=list(plan.hatches),
        metrics=metrics,
    )
    # The placer and router each report success on their own terms; the validator is the only
    # gate written independently of them, so run it on the assembled layout before claiming VALID.
    # If it proves a violation, surface it as partial_invalid rather than handing back a
    # silently-invalid layout: a bug in our own output, or - the one steerable case - a machine
    # the placement left too far from its power source.
    report = validate(problem, layout)
    if not report.ok:
        starved = _starved_machines(report)
        downgraded = LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=_validation_infeasibility(report, starved),
            placements=placement_list,
            routes=routes,
            auto_connections=autos,
            hatches=list(plan.hatches),
            metrics=metrics,
        )
        # A starved machine is a placement defect: hand back the power nets it sits on, so this
        # layout ranks as one that left them unrouted and an attempt placing it nearer can win.
        return downgraded, tuple(
            n.id
            for n in problem.nets
            if n.commodity is Commodity.POWER
            and not {e.machine_id for e in n.endpoints}.isdisjoint(starved)
        )
    return layout, ()


def _starved_machines(report: ValidationReport) -> tuple[str, ...]:
    """The machines ``report`` proves starved of power - but only when that is ALL it proves.

    A starve is distance-driven: the cable loss over the run this placement implied leaves the
    machine's hatches unable to take in its ``eut``, though every segment is correctly thick. So
    a placement with the machine nearer its source is the fix, and the attempt ranks as having left
    its power net unrouted. Any *other* violation alongside it is a genuine placer/router bug,
    which no placement fixes - so a mixed report names no net, as before.
    """
    starved = tuple(
        v.machine_id
        for v in report.violations
        if v.code is ViolationCode.POWER_SUPPLY_INSUFFICIENT and v.machine_id is not None
    )
    return starved if len(starved) == len(report.violations) else ()


def _validation_infeasibility(report: ValidationReport, starved: tuple[str, ...]) -> Infeasibility:
    """An Infeasibility describing why our own assembled layout failed independent validation."""
    if starved:
        # Nothing else is wrong with this layout: the machines are simply too far from their power
        # source. No attempt placed them close enough, so the advice is about the geometry the
        # input allows, not about reporting a bug.
        return Infeasibility(
            constraint="power_supply",
            detail="; ".join(v.message for v in report.violations),
            suggested_relaxation=(
                "shorten the power run - a smaller bounding region, or a power source nearer the "
                "load; no attempt could place these machines close enough"
            ),
        )
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
