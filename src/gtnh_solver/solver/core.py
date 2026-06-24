"""solver.core - compose placement + auto-output + routing into a LayoutResult.

The Phase 1 orchestration (docs/ROADMAP.md):
  1. place the machines (flow order, so producers land next to consumers);
  2. assign **auto-output** connections - a source machine ejecting straight into an adjacent
     target's input face, no pipe and no cover (GT's free connection; one auto-output per
     machine, items XOR fluids - docs/DOMAIN.md);
  3. route pipes for the nets auto-output could NOT cover;
  4. assemble the LayoutResult (or surface the placement/routing infeasibility);
  5. **validate the assembled layout against the independent validator** and downgrade a
     VALID result to ``partial_invalid`` if it proves any violation. The validator's logic is
     written independently of the placer/router precisely to catch their bugs, so running it on
     our own output is what makes the "never returns a silently-invalid layout" promise true
     end to end (docs/ARCHITECTURE.md #4) - not just an internal `place.ok && route.ok`.

Auto-output is preferred because it is what a player actually builds for a simple chain: a row
of adjacent machines feeding each other needs zero pipes. Pipes are only for what is left -
non-adjacent endpoints, fan-out, or a machine whose single auto-output is already spent.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    AutoConnection,
    Commodity,
    Facing,
    Infeasibility,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, occupied_cells
from gtnh_solver.placement import place
from gtnh_solver.router import route
from gtnh_solver.validator import ValidationReport, validate


def solve(problem: InputIR, *, seed: int = 0) -> LayoutResult:
    """Produce a layout for ``problem``: placements + auto-output connections + pipe routes."""
    placement = place(problem)
    if not placement.ok:
        return LayoutResult(
            status=LayoutStatus.INFEASIBLE, seed=seed, infeasibility=placement.infeasibility
        )

    autos, auto_net_ids = _assign_auto_outputs(problem, placement.placements)
    routing = route(problem, placement.placements, skip_nets=auto_net_ids)
    if not routing.ok:
        return LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=routing.infeasibility,
            placements=list(placement.placements),
            routes=list(routing.routes),
            auto_connections=autos,
        )

    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=seed,
        placements=list(placement.placements),
        routes=list(routing.routes),
        auto_connections=autos,
    )
    # The placer and router each report success on their own terms; the validator is the only
    # gate written independently of them, so run it on the assembled layout before claiming
    # VALID. If it proves a violation, that is a bug in our own output - surface it as
    # partial_invalid rather than handing back a silently-invalid layout.
    report = validate(problem, layout)
    if not report.ok:
        return LayoutResult(
            status=LayoutStatus.PARTIAL_INVALID,
            seed=seed,
            infeasibility=_validation_infeasibility(report),
            placements=list(placement.placements),
            routes=list(routing.routes),
            auto_connections=autos,
        )
    return layout


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


def _assign_auto_outputs(
    problem: InputIR, placements: tuple[Placement, ...]
) -> tuple[list[AutoConnection], set[str]]:
    """Connect each simple 1-source-1-sink net by auto-output where the machines are adjacent.

    Returns the auto-connections plus the set of net ids they cover (so the router skips them).
    """
    machines = {m.id: m for m in problem.machines}
    placement_of: dict[str, Placement] = {}
    for p in placements:
        placement_of.setdefault(p.machine_id, p)
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}

    spent: set[str] = set()  # source machines that have used their single auto-output face
    autos: list[AutoConnection] = []
    covered: set[str] = set()
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
        if len(sources) != 1 or len(sinks) != 1:
            continue  # crude: only simple 1->1 nets auto-output; fan-out routes as pipes
        source, sink = sources[0], sinks[0]
        if source in spent:
            continue  # this machine's one auto-output is already used; the rest pipe

        faces = _auto_faces(
            placement_of.get(source),
            machines.get(source),
            placement_of.get(sink),
            machines.get(sink),
        )
        if faces is None:
            continue
        source_face, target_face = faces
        spent.add(source)
        covered.add(net.id)
        autos.append(
            AutoConnection(
                net_id=net.id,
                source_machine_id=source,
                source_face=source_face,
                target_machine_id=sink,
                target_face=target_face,
            )
        )
    return autos, covered


def _auto_faces(
    source_p: Placement | None,
    source_m: Machine | None,
    target_p: Placement | None,
    target_m: Machine | None,
) -> tuple[Facing, Facing] | None:
    """The (source_face, target_face) if the two machines touch on a usable pair of faces."""
    if source_p is None or source_m is None or target_p is None or target_m is None:
        return None
    source_cells = set(occupied_cells(source_p.cell, source_m.footprint))
    target_cells = set(occupied_cells(target_p.cell, target_m.footprint))
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_p.orientation:  # source's front carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_p.orientation:  # the target's input face would be its front
            continue
        if any((x + dx, y + dy, z + dz) in target_cells for x, y, z in source_cells):
            return face, opposite
    return None
