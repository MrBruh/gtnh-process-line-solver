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
    Pose,
    Size,
    pose_of,
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
    pose_by_id = {mid: pose_of(p) for mid, p in placement_index(placements).items()}
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
            pose_by_id.get(source.machine_id),
            source_m,
            source.port_id,
            pose_by_id.get(sink.machine_id),
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
    source: Pose | None,
    source_m: Machine,
    source_port: str,
    target: Pose | None,
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
        _auto_faces(source, source_m, source_port, target, target_m, target_port, _NOTHING_CLAIMED)
        is not None
    )


@dataclass(frozen=True, slots=True)
class _Host:
    """One port of one machine, turned one way: everything :func:`_auto_faces` reads of a side.

    Which casing cells can host a port does not depend on *where* the machine stands, only on how
    it is turned - so the rotation is the whole computation, and the position is an addition. The
    placement cost re-asks this question for the same machine in thousands of positions per solve,
    and the uncached form rebuilt both sets every time: 16M ``rotated_slot`` calls and 26M
    ``occupied_cells`` yields on one nitrobenzene solve, two thirds of its runtime (#107). That is
    the same shape as the enumeration #110 took off this path, one level down.

    The rotated footprint rides along for the same reason, plus one more: it is otherwise read off
    two pydantic models per call, which on 3.14 cannot take the fast attribute path (#256).
    """

    #: Kept so the machine's ``id`` cannot be recycled onto a different object while the entry
    #: lives, and so a hit can verify identity rather than trust the key.
    machine: Machine
    size: Size  # the footprint as it sits facing this way
    offsets: tuple[Cell, ...]  # host cells relative to the origin; the source side iterates these
    offset_set: frozenset[Cell]  # the same cells, for the target side's "does it contain" test


#: ``(id(machine), port, orientation)`` -> :class:`_Host`. Bounded by machines x ports x facings.
_HOSTS: dict[tuple[int, str, Facing], _Host] = {}


def _host(machine: Machine, port_id: str, orientation: Facing) -> _Host:
    """The :class:`_Host` for ``machine``'s ``port_id`` facing ``orientation``, built once."""
    key = (id(machine), port_id, orientation)
    hit = _HOSTS.get(key)
    if hit is not None and hit.machine is machine:
        return hit
    origin = CellCoord(x=0, y=0, z=0)
    cells = tuple(
        hatches.port_cells(
            Placement(machine_id=machine.id, cell=origin, orientation=orientation), machine, port_id
        )
    )
    box = rotated_footprint(machine.footprint, orientation)
    host = _HOSTS[key] = _Host(machine, (box.sx, box.sy, box.sz), cells, frozenset(cells))
    return host


def _auto_faces(
    source: Pose | None,
    source_m: Machine,
    source_port: str,
    target: Pose | None,
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
    if source is None or target is None:
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
    source_front, target_front = source.orientation, target.orientation
    source_host = _host(source_m, source_port, source_front)
    target_host = _host(target_m, target_port, target_front)
    source_offsets, target_offsets = source_host.offsets, target_host.offset_set
    ssx, ssy, ssz = source_host.size
    tsx, tsy, tsz = target_host.size
    ox, oy, oz = source.cell
    tx, ty, tz = target.cell
    tx_max, ty_max, tz_max = tx + tsx, ty + tsy, tz + tsz
    # source origin - target origin: added to a source offset it lands in the target's frame.
    bx, by, bz = ox - tx, oy - ty, oz - tz
    taken_source = claimed.get(source.machine_id, ())
    taken_target = claimed.get(target.machine_id, ())
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_front:  # the source's front carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_front:  # the target's input face would be its front
            continue
        # The stepped source box against the target box, half-open on both sides.
        sx0, sy0, sz0 = ox + dx, oy + dy, oz + dz
        if not (
            sx0 < tx_max
            and tx < sx0 + ssx
            and sy0 < ty_max
            and ty < sy0 + ssy
            and sz0 < tz_max
            and tz < sz0 + ssz
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
