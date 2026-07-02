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

``solve`` wraps that in the **place<->route feedback loop** (docs/ARCHITECTURE.md #1, #6): if an
attempt leaves nets unrouted, it penalizes exactly those nets (so the next placement pulls their
machines tighter - shorter routes, or adjacency that auto-outputs) and re-places with the next
seed. It keeps the best layout seen and returns the first fully-VALID one (anytime: best-so-far),
stopping early when re-placing cannot help - a non-routing defect, or the same nets failing again
(feedback not progressing). It is **deterministic** (bounded attempts keyed off ``seed`` + the
penalties, no wall-clock), so a given input always yields the same layout.

``solve(..., optimize=False)`` is the **fast** path: a single constructive placement with no
annealing and no feedback loop (near-instant, simpler layout), still validated. The two modes are
the "optimize or not" choice the planned unified site exposes to the builder.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    Infeasibility,
    InputIR,
    LayoutResult,
    LayoutStatus,
    Placement,
)
from gtnh_solver.placement import optimize_placement, place
from gtnh_solver.router import route, route_power
from gtnh_solver.validator import ValidationReport, validate

# Feedback loop bounds. Cycle detection on the failed-net set usually stops sooner; this caps the
# work. The penalty step adds to a net's wirelength weight each time it fails to route.
_MAX_FEEDBACK_PASSES = 6
_PENALTY_STEP = 2.0


def solve(problem: InputIR, *, seed: int = 0, optimize: bool = True) -> LayoutResult:
    """Produce a layout for ``problem``; deterministic for a given ``problem`` + ``seed``.

    ``optimize`` selects how hard to work (the site's "optimize or not" control):

    - ``True`` (default): the annealed placer (SA + LNS) inside the place<->route feedback loop -
      tighter layouts (more auto-output, shorter routes), at the cost of seconds of CPU. Returns
      the first fully-VALID layout, else the best partial.
    - ``False`` (**fast**): a single constructive first-fit placement, no optimization and no
      feedback loop - near-instant and simple. Its layout is still validated, so it is VALID or an
      explicit partial/infeasibility, never silently invalid; but it will not cluster machines for
      auto-output or re-place to rescue an unroutable net the way the optimizer can.
    """
    if not optimize:
        return _solve_fast(problem, seed)
    penalties: dict[str, float] = {}
    seen_failed: set[frozenset[str]] = set()
    best: LayoutResult | None = None
    best_failures = -1
    for attempt in range(_MAX_FEEDBACK_PASSES):
        attempt_seed = seed + attempt
        placement = optimize_placement(problem, seed=attempt_seed, net_penalties=penalties)
        if not placement.ok:
            # The machines do not fit the region at all - seed-independent, so retrying is futile.
            return LayoutResult(
                status=LayoutStatus.INFEASIBLE,
                seed=attempt_seed,
                infeasibility=placement.infeasibility,
            )

        layout, failed_nets = _assemble(problem, placement.placements, attempt_seed)
        if layout.status is LayoutStatus.VALID:
            return layout  # fully valid - the best possible; stop
        if best is None or len(failed_nets) < best_failures:
            best, best_failures = layout, len(failed_nets)  # fewest-unrouted partial so far

        if not failed_nets:
            break  # a non-routing defect (independent validation) - re-placing cannot help
        key = frozenset(failed_nets)
        if key in seen_failed:
            break  # the same nets keep failing - the feedback is not making progress
        seen_failed.add(key)
        for net_id in failed_nets:
            penalties[net_id] = penalties.get(net_id, 0.0) + _PENALTY_STEP

    assert best is not None  # attempt 0 always either returns or sets best
    return best


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
    item_cells = {
        (seg.start.x, seg.start.y, seg.start.z) for r in routing.routes for seg in r.segments
    } | {(seg.end.x, seg.end.y, seg.end.z) for r in routing.routes for seg in r.segments}
    power = route_power(problem, placements, extra_obstacles=item_cells)
    routes = [*routing.routes, *power.routes]
    placement_list = list(placements)

    infeasibility = routing.infeasibility or power.infeasibility
    if infeasibility is not None:
        layout = LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=infeasibility,
            placements=placement_list,
            routes=routes,
            auto_connections=autos,
        )
        return layout, (*routing.failed_nets, *power.failed_nets)

    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=seed,
        placements=placement_list,
        routes=routes,
        auto_connections=autos,
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
