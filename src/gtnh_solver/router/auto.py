"""router.auto - the auto-output vs pipe decision, made from final geometry.

Given final placements + orientations, decide which nets GT's free **auto-output** connection
covers: a source machine ejecting straight into an adjacent target's input face, no pipe and
no cover (one auto-output per machine, items XOR fluids - docs/DOMAIN.md). The router owns
this decision (docs/ROADMAP.md lane D): :func:`~gtnh_solver.router.core.route` calls
:func:`assign_auto_outputs` first and lays pipes only for the nets it could not cover, so the
optimizer's job shrinks to moving blocks and choosing front faces.

Auto-output is preferred because it is what a player actually builds for a simple chain: a row
of adjacent machines feeding each other needs zero pipes. Pipes are only for what is left -
non-adjacent endpoints, fan-out, or a machine whose single auto-output is already spent. The
face geometry is the shared ``ir.geometry.auto_output_faces`` (the placement cost rewards the
same adjacency); the validator independently re-derives every rule enforced here
(adjacency/faces, one auto-output per machine, never power/ME - docs/ARCHITECTURE.md #4).
"""

from __future__ import annotations

from collections.abc import Sequence

from gtnh_solver.ir import (
    AutoConnection,
    Commodity,
    Facing,
    InputIR,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import auto_output_faces
from gtnh_solver.ir.nets import net_sources_sinks, placement_index, port_direction_map


def assign_auto_outputs(
    problem: InputIR, placements: Sequence[Placement]
) -> tuple[list[AutoConnection], set[str]]:
    """Connect each simple 1-source-1-sink net by auto-output where the machines are adjacent.

    Returns the auto-connections plus the set of net ids they cover (so the router lays pipes
    only for the rest).
    """
    machines = {m.id: m for m in problem.machines}
    placement_of = placement_index(placements)
    port_dir = port_direction_map(problem)

    spent: set[str] = set()  # source machines that have used their single auto-output face
    autos: list[AutoConnection] = []
    covered: set[str] = set()
    for net in problem.nets:
        if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
            continue
        sources, sinks = net_sources_sinks(net, port_dir)
        if len(sources) != 1 or len(sinks) != 1:
            continue  # crude: only simple 1->1 nets auto-output; fan-out routes as pipes
        source, sink = sources[0].machine_id, sinks[0].machine_id
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
    """The (source_face, target_face) if the two machines touch on a usable pair of faces.

    Thin wrapper over the shared ``ir.geometry.auto_output_faces`` (the placement cost rewards the
    same adjacency, so the geometry lives in one place); guards the unplaced/unknown-machine case.
    """
    if source_p is None or source_m is None or target_p is None or target_m is None:
        return None
    return auto_output_faces(
        source_p.cell,
        source_m.footprint,
        source_p.orientation,
        target_p.cell,
        target_m.footprint,
        target_p.orientation,
    )
