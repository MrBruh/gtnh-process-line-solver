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
contact. That tightening is why this searches faces itself instead of relying on
``ir.geometry.auto_output_faces``, which models the loose "any touching body cell" rule. For a
single-block machine the two rules agree - its one cell is its own hatch - so the loose rule is
exactly right there, and it survives here as a cheap *necessary condition*: bodies that do not
touch cannot have touching casing cells either, so it rejects most candidates for six integer
comparisons before any cell set is built (:func:`_auto_faces`).

**The placement cost asks this module, not the loose rule** (:func:`auto_output_possible`). It
used to ask the loose one and call the difference a soft preference, but an optimistic reward is
not a harmless one: the annealer kept arrangements *because* of a discount for connections the
router then refused, so its budget went on connections it would not get and its top-ranked
layouts were ranked on a promise that was not kept (issue #107). What the cost still cannot see
is routing-time contention - which casing cell some other hatch will take - so it asks with an
empty ``claimed`` and remains an upper bound, now on structure the machine actually has.

The one-auto-output-per-machine rule follows the same split. It is a real GT limit on a
**single-block** machine - one auto-output face, items XOR fluids - and simply wrong for a
multiblock, where every output hatch ejects on its own front face independently. So the "spent"
set binds only where no structure was dumped; a multiblock instead spends casing *cells*, and the
claims it accumulates are handed on so a routed hatch cannot reuse one.

The validator independently re-derives every rule enforced here (docs/ARCHITECTURE.md #4).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from gtnh_solver.ir import (
    AutoConnection,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import (
    FACE_DELTAS,
    OPPOSITE_FACE,
    Cell,
    rotated_footprint,
)
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
    # A factory, not a plain default: 3.11 rejects an unhashable default at class creation (#255).
    claimed: Mapping[str, frozenset[Cell]] = field(default_factory=lambda: MappingProxyType({}))


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


#: No casing cell is spoken for yet - the placement cost cannot know routing-time contention.
_NOTHING_CLAIMED: Mapping[str, Collection[Cell]] = MappingProxyType({})


def auto_output_possible(
    source_p: Placement | None,
    source_m: Machine,
    source_port: str,
    target_p: Placement | None,
    target_m: Machine,
    target_port: str,
) -> bool:
    """Whether a free auto-output connection between these two placed ports is structurally possible.

    The placement cost's question, answered by the same :func:`_auto_faces` that decides the real
    layout, so the reward and the router cannot drift apart again (#107). It is deliberately the
    router's rule minus one term: ``claimed`` is routing-time state - which casing cell some other
    hatch ends up taking - that does not exist while machines are still being placed. So this stays
    an upper bound on what the router will grant, but an upper bound over the casing cells the
    machine *has*, rather than over any two bodies in contact.

    A single-block machine is unaffected either way: its one cell is its own hatch, so this and the
    loose body rule agree on it exactly.
    """
    return (
        _auto_faces(
            source_p, source_m, source_port, target_p, target_m, target_port, _NOTHING_CLAIMED
        )
        is not None
    )


#: ``(id(machine), port, orientation)`` -> that machine and its host cells as offsets from the
#: placement origin. The machine is kept beside the offsets so its ``id`` cannot be recycled onto a
#: different object while the entry lives, and so a hit can verify identity rather than trust the
#: key. Bounded by machines x ports x 4 orientations.
_HOST_OFFSETS: dict[tuple[int, str, Facing], tuple[Machine, tuple[Cell, ...]]] = {}


def _host_offsets(machine: Machine, port_id: str, orientation: Facing) -> tuple[Cell, ...]:
    """``port_cells`` relative to the placement origin, computed once per orientation.

    Which casing cells can host a port does not depend on *where* the machine stands, only on how
    it is turned - so the rotation is the whole computation, and the position is an addition. The
    placement cost re-asks this question for the same machine in thousands of positions per solve,
    and the uncached form rebuilt both sets every time: 16M ``rotated_slot`` calls and 26M
    ``occupied_cells`` yields on one nitrobenzene solve, two thirds of its runtime (#107). That is
    the same shape as the enumeration #110 took off this path, one level down.
    """
    key = (id(machine), port_id, orientation)
    hit = _HOST_OFFSETS.get(key)
    if hit is not None and hit[0] is machine:
        return hit[1]
    origin = CellCoord(x=0, y=0, z=0)
    cells = hatches.port_cells(
        Placement(machine_id=machine.id, cell=origin, orientation=orientation), machine, port_id
    )
    offsets = tuple(cells)
    _HOST_OFFSETS[key] = (machine, offsets)
    return offsets


#: The same offsets as a set, for the target side, which is asked "does it contain" rather than
#: iterated. Kept apart from the tuple so neither side pays for the other's shape.
_HOST_OFFSET_SETS: dict[tuple[int, str, Facing], tuple[Machine, frozenset[Cell]]] = {}


def _host_offset_set(machine: Machine, port_id: str, orientation: Facing) -> frozenset[Cell]:
    """:func:`_host_offsets` as a frozenset - the membership form the target side needs."""
    key = (id(machine), port_id, orientation)
    hit = _HOST_OFFSET_SETS.get(key)
    if hit is not None and hit[0] is machine:
        return hit[1]
    offsets = frozenset(_host_offsets(machine, port_id, orientation))
    _HOST_OFFSET_SETS[key] = (machine, offsets)
    return offsets


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
    # Per face, cheapest test first. The BOX overlap is six integer comparisons and rejects most
    # candidates outright; only a face whose bodies actually touch is worth scanning casing cells
    # for, and bodies that do not touch cannot have touching cells inside them. Fused into one
    # loop rather than run as a separate pre-pass, so a face that fails the box test costs nothing
    # further - the placement cost asks this ~1.5M times a solve (#107), and the box form of the
    # test is itself what took this path off a 70%-of-a-solve profile (#110).
    #
    # The scan itself runs in OFFSET space: which cells can host a hatch depends on how a machine
    # is turned, not where it stands (:func:`_host_offsets`), so position enters only as the vector
    # between the two origins. World cells are built solely when a pair matches.
    source_box = rotated_footprint(source_m.footprint, source_p.orientation)
    target_box = rotated_footprint(target_m.footprint, target_p.orientation)
    source_offsets = _host_offsets(source_m, source_port, source_p.orientation)
    target_offsets = _host_offset_set(target_m, target_port, target_p.orientation)
    ox, oy, oz = source_p.cell.x, source_p.cell.y, source_p.cell.z
    tx, ty, tz = target_p.cell.x, target_p.cell.y, target_p.cell.z
    tx_max, ty_max, tz_max = tx + target_box.sx, ty + target_box.sy, tz + target_box.sz
    # source origin - target origin: added to a source offset it lands in the target's frame.
    bx, by, bz = ox - tx, oy - ty, oz - tz
    taken_source = claimed.get(source_p.machine_id, ())
    taken_target = claimed.get(target_p.machine_id, ())
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_p.orientation:  # the source's front carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_p.orientation:  # the target's input face would be its front
            continue
        # The stepped source box against the target box, half-open on both sides.
        sx0, sy0, sz0 = ox + dx, oy + dy, oz + dz
        if not (
            sx0 < tx_max
            and tx < sx0 + source_box.sx
            and sy0 < ty_max
            and ty < sy0 + source_box.sy
            and sz0 < tz_max
            and tz < sz0 + source_box.sz
        ):
            continue
        ax, ay, az = bx + dx, by + dy, bz + dz
        for sx, sy, sz in source_offsets:
            if (sx + ax, sy + ay, sz + az) not in target_offsets:
                continue
            cell = (sx + ox, sy + oy, sz + oz)
            neighbour = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
            if cell in taken_source or neighbour in taken_target:
                continue  # that casing block already holds another hatch
            return face, opposite, cell, neighbour
    return None
