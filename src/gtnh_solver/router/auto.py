"""router.auto - the auto-output vs pipe decision, made from final geometry.

Given final placements + orientations, decide which nets GT's free **auto-output** connection
covers: a source ejecting straight into an adjacent target's input face, no pipe and no cover
(docs/DOMAIN.md). The router owns this decision (docs/ROADMAP.md lane D):
:func:`~gtnh_solver.router.core.route` calls :func:`assign_auto_outputs` first and lays pipes only
for the nets it could not cover, so the optimizer's job shrinks to moving blocks and choosing
front faces.

Auto-output is preferred because it is what a player actually builds for a simple chain: a row of
adjacent machines feeding each other needs zero pipes. Pipes are only for what is left -
non-adjacent endpoints, fan-out, or a machine with no free face for one.

**Two faces touching is not enough for a multiblock.** A multiblock ejects through an output
hatch's own front face, and receives through an input bus's, so a free connection needs a
*touching pair of casing cells* that can host those two hatches - not merely two bodies in
contact. That tightening is why this searches faces itself instead of calling
``ir.geometry.auto_output_faces``, which models the loose "any touching body cell" rule and is
still right for the placement cost that rewards adjacency (a soft preference that may be
optimistic) and for a single-block machine, whose one cell is its own hatch.

The one-auto-output-per-machine rule follows the same split. It is a real GT limit on a
**single-block** machine - one auto-output face, items XOR fluids - and simply wrong for a
multiblock, where every output hatch ejects on its own front face independently. So the "spent"
set binds only where no structure was dumped; a multiblock instead spends casing *cells*, and the
claims it accumulates are handed on so a routed hatch cannot reuse one.

The validator independently re-derives every rule enforced here (docs/ARCHITECTURE.md #4).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from gtnh_solver.ir import (
    AutoConnection,
    Commodity,
    Facing,
    InputIR,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell
from gtnh_solver.ir.nets import net_sources_sinks, placement_index, port_direction_map

from . import hatches


@dataclass(frozen=True)
class AutoAssignment:
    """What auto-output covered, and the casing cells it spent doing so.

    ``claimed`` is per machine and is what keeps a routed hatch off a cell an auto-output hatch is
    already standing on: the two are the same pool of casing blocks, and nothing else would notice,
    since a free connection lays no route cells at all.
    """

    connections: tuple[AutoConnection, ...] = ()
    covered: frozenset[str] = frozenset()
    claimed: Mapping[str, frozenset[Cell]] = MappingProxyType({})


def assign_auto_outputs(problem: InputIR, placements: Sequence[Placement]) -> AutoAssignment:
    """Connect each simple 1-source-1-sink net by auto-output where the geometry allows one.

    "Allows one" means a touching pair of casing cells that can host the two hatches, not just two
    machines in contact - see the module docstring.
    """
    machines = {m.id: m for m in problem.machines}
    placement_of = placement_index(placements)
    port_dir = port_direction_map(problem)

    spent: set[str] = set()  # single-block sources that have used their one auto-output face
    claimed: dict[str, set[Cell]] = {}  # multiblock sources/targets: the casing cells taken
    autos: list[AutoConnection] = []
    covered: set[str] = set()
    for net in problem.nets:
        if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
            continue
        sources, sinks = net_sources_sinks(net, port_dir)
        if len(sources) != 1 or len(sinks) != 1:
            continue  # crude: only simple 1->1 nets auto-output; fan-out routes as pipes
        source, sink = sources[0], sinks[0]
        source_m, sink_m = machines.get(source.machine_id), machines.get(sink.machine_id)
        if source_m is None or sink_m is None:
            continue
        if source.machine_id in spent and not source_m.hatch_slots:
            continue  # a single block has ONE auto-output face; the rest of its nets pipe

        found = _auto_faces(
            placement_of.get(source.machine_id),
            source_m,
            source.port_id,
            placement_of.get(sink.machine_id),
            sink_m,
            sink.port_id,
            claimed,
        )
        if found is None:
            continue
        source_face, target_face, source_cell, target_cell = found
        spent.add(source.machine_id)
        covered.add(net.id)
        if source_m.hatch_slots:
            claimed.setdefault(source.machine_id, set()).add(source_cell)
        if sink_m.hatch_slots:
            claimed.setdefault(sink.machine_id, set()).add(target_cell)
        autos.append(
            AutoConnection(
                net_id=net.id,
                source_machine_id=source.machine_id,
                source_face=source_face,
                target_machine_id=sink.machine_id,
                target_face=target_face,
            )
        )
    return AutoAssignment(
        connections=tuple(autos),
        covered=frozenset(covered),
        claimed=MappingProxyType({k: frozenset(v) for k, v in claimed.items()}),
    )


def _auto_faces(
    source_p: Placement | None,
    source_m: Machine,
    source_port: str,
    target_p: Placement | None,
    target_m: Machine,
    target_port: str,
    claimed: Mapping[str, Collection[Cell]],
) -> tuple[Facing, Facing, Cell, Cell] | None:
    """The faces and the touching pair of casing cells a free connection would run through.

    A cell qualifies on each side when it can host that side's hatch - ``hatches.port_cells``,
    which is the machine's whole body where no structure was dumped, so a single-block machine
    behaves exactly as it did under the old any-touching-cell rule. Faces are tried in
    ``FACE_DELTAS`` order, the same order ``ir.geometry.auto_output_faces`` used, so an assignment
    that was legal before and is still legal comes out identical.
    """
    if source_p is None or target_p is None:
        return None
    source_hosts = hatches.port_cells(source_p, source_m, source_port)
    target_hosts = set(hatches.port_cells(target_p, target_m, target_port))
    taken_source = claimed.get(source_p.machine_id, ())
    taken_target = claimed.get(target_p.machine_id, ())
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_p.orientation:  # the source's front carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_p.orientation:  # the target's input face would be its front
            continue
        for cell in source_hosts:
            neighbour = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
            if neighbour not in target_hosts:
                continue
            if cell in taken_source or neighbour in taken_target:
                continue  # that casing block already holds another hatch
            return face, opposite, cell, neighbour
    return None
