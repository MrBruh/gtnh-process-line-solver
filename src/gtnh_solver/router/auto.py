"""router.auto - the auto-output vs pipe decision, made from final geometry.

Given final placements + orientations, decide which nets GT's free **auto-output** connection
covers: a source ejecting straight into an adjacent target's input face, no pipe and no cover
(docs/DOMAIN.md). The router owns this decision (docs/ROADMAP.md lane D):
:func:`~gtnh_solver.router.core.route` calls :func:`assign_auto_outputs` first and lays pipes only
for the nets it could not cover, so the optimizer's job shrinks to moving blocks and choosing
front faces.

Each side's face must be one its port may use (``Machine.allowed_faces``): any face but the front
for an unpinned port, exactly its pins for a pinned one. So an Item Filter, whose output is pinned
to its back, auto-outputs only from its back, and may be fed on its front.

Auto-output is preferred because it is what a player actually builds for a simple chain: a row of
adjacent machines feeding each other needs zero pipes. Pipes are only for what is left -
non-adjacent endpoints, fan-out, or a machine with no free face for one.

**A net with several producers and one consumer is covered producer by producer** (#270). Each
producer ejects on its own, so one standing against the consumer can auto-output into it while the
rest still share a pipe: the net is then routed over the producers left plus the consumer, and only
needs no pipe at all once every producer is covered::

    P1 -+                     P1 -+
    P2 -+--pipe--> C   ==>    P2 -+--pipe--> C <== P3   (P3 stands against C and ejects into it)
    P3 -+

Fan-out has no such split: one producer's auto-output reaches one block, so a net with several
consumers always pipes. Two more limits keep the split honest (:func:`auto_candidates`):

- **the consumer is a single block.** On a multiblock each connection is a hatch, so a consumer
  taking one producer free and the rest by pipe would need two input hatches for one port, one with
  no terminal to agree with. A consumer here is in practice a Super Chest or Super Tank anyway;
- **a single-block producer has no other output of the net's commodity.** Its auto-output face
  ejects every output slot of that kind, so a second output would ride along into the consumer (an
  empty Super Chest locks to whichever item arrives first). A multiblock ejects per hatch, so it is
  exempt.

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
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
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

    ``covered`` are the nets auto-output satisfies whole, which need no pipe. ``fed`` are the
    producers it covers on the nets it does not (#270): net id -> those endpoints, which the net's
    pipe then leaves out (:meth:`piped`).

    ``claimed`` is per machine and is what keeps a routed hatch off a cell an auto-output hatch is
    already standing on: the two are the same pool of casing blocks, and nothing else would notice,
    since a free connection lays no route cells at all.
    """

    connections: tuple[AutoConnection, ...] = ()
    covered: frozenset[str] = frozenset()
    # Factories, not plain defaults: 3.11 rejects an unhashable default at class creation (#255).
    fed: Mapping[str, frozenset[MachineFaceRef]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    claimed: Mapping[str, frozenset[Cell]] = field(default_factory=lambda: MappingProxyType({}))

    def piped(self, net: Net) -> Net:
        """``net`` as its pipe sees it: without the producers that auto-output into its consumer.

        The net itself when auto-output covers none of it. Its id is kept, so the route still
        answers for the net; only its terminals are fewer.
        """
        fed = self.fed.get(net.id)
        if not fed:
            return net
        return net.model_copy(update={"endpoints": [e for e in net.endpoints if e not in fed]})


@dataclass(frozen=True, slots=True)
class AutoCandidate:
    """One producer of a net that may auto-output into the net's one consumer.

    ``shared`` says the consumer also takes the net's other producers (#270), so it still needs a
    pipe docked on it unless every one of them is covered too.
    """

    net_id: str
    source: MachineFaceRef
    sink: MachineFaceRef
    shared: bool


def auto_candidates(problem: InputIR) -> list[AutoCandidate]:
    """Every producer that auto-output may cover, in net order and then endpoint order.

    The rule on *which nets and producers* qualify, read by the router and by the placement reward
    so they cannot drift apart (#107): a 1->1 net, or a producer of a net with several producers and
    one single-block consumer that has no other output of the net's commodity (module docstring).
    The reward takes only the 1->1 ones (``shared`` False; ``placement.search`` says why). Whether
    the geometry then allows it is :func:`_auto_faces`'s question.
    """
    machines = {m.id: m for m in problem.machines}
    port_dir = port_direction_map(problem)
    return [
        candidate
        for net in problem.nets
        for candidate in _net_candidates(problem, net, machines, port_dir)
    ]


def _net_candidates(
    problem: InputIR,
    net: Net,
    machines: Mapping[str, Machine],
    port_dir: dict[tuple[str, str], IODirection],
) -> list[AutoCandidate]:
    """:func:`auto_candidates` for one net."""
    if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
        return []
    sources, sinks = net_sources_sinks(net, port_dir)
    if len(sinks) != 1 or not sources:
        return []  # fan-out routes as a pipe: one producer's auto-output reaches one block
    (sink,) = sinks
    if len(sources) == 1:
        return [AutoCandidate(net.id, sources[0], sink, shared=False)]
    sink_m = machines.get(sink.machine_id)
    if sink_m is None or sink_m.hatch_slots:
        return []  # a multiblock consumer would need two hatches for one port
    return [
        AutoCandidate(net.id, source, sink, shared=True)
        for source in sources
        if _ejects_alone(machines.get(source.machine_id), net.commodity)
    ]


def _ejects_alone(machine: Machine | None, commodity: Commodity) -> bool:
    """Whether ``machine`` auto-outputting ``commodity`` would eject that one output and nothing else.

    A multiblock ejects per output hatch, so always. A single block ejects every output slot of
    the kind through its one auto-output face, so only when it has a single output of it.
    """
    if machine is None:
        return False
    if machine.hatch_slots:
        return True
    outputs = [
        port
        for port in machine.faces.ports
        if port.direction is IODirection.OUTPUT and port.commodity is commodity
    ]
    return len(outputs) == 1


def assign_auto_outputs(problem: InputIR, placements: Sequence[Placement]) -> AutoAssignment:
    """Connect each :func:`auto_candidates` producer by auto-output where the geometry allows one.

    "Allows one" means a touching pair of casing cells that can host the two hatches, not just two
    machines in contact - see the module docstring. A net is covered once every producer on it is;
    one with only some covered keeps a pipe for the rest (:attr:`AutoAssignment.fed`).
    """
    machines = {m.id: m for m in problem.machines}
    pose_by_id = {mid: pose_of(p) for mid, p in placement_index(placements).items()}
    port_dir = port_direction_map(problem)

    spent: set[str] = set()  # single-block sources that have used their one auto-output face
    claimed: dict[str, set[Cell]] = {}  # multiblock sources/targets: the casing cells taken
    autos: list[AutoConnection] = []
    covered: set[str] = set()
    fed: dict[str, frozenset[MachineFaceRef]] = {}
    for net in problem.nets:
        candidates = _net_candidates(problem, net, machines, port_dir)
        if not candidates:
            continue
        sources, _ = net_sources_sinks(net, port_dir)
        ejecting: list[MachineFaceRef] = []
        for candidate in candidates:
            connection = _connect(candidate, machines, pose_by_id, spent, claimed)
            if connection is not None:
                autos.append(connection)
                ejecting.append(candidate.source)
        if len(ejecting) == len(sources):
            covered.add(net.id)
        elif ejecting:
            fed[net.id] = frozenset(ejecting)
    return AutoAssignment(
        connections=tuple(autos),
        covered=frozenset(covered),
        fed=MappingProxyType(fed),
        claimed=MappingProxyType({k: frozenset(v) for k, v in claimed.items()}),
    )


def _connect(
    candidate: AutoCandidate,
    machines: Mapping[str, Machine],
    pose_by_id: Mapping[str, Pose],
    spent: set[str],
    claimed: dict[str, set[Cell]],
) -> AutoConnection | None:
    """The auto-connection ``candidate`` makes on these placements, spending what it uses; or None."""
    source, sink = candidate.source, candidate.sink
    source_m, sink_m = machines.get(source.machine_id), machines.get(sink.machine_id)
    if source_m is None or sink_m is None:
        return None
    if source.machine_id in spent and not source_m.hatch_slots:
        return None  # a single block has ONE auto-output face; the rest of its nets pipe
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
        return None
    source_face, target_face, source_cell, target_cell = found
    spent.add(source.machine_id)
    if source_m.hatch_slots:
        claimed.setdefault(source.machine_id, set()).add(source_cell)
    if sink_m.hatch_slots:
        claimed.setdefault(sink.machine_id, set()).add(target_cell)
    return AutoConnection(
        net_id=candidate.net_id,
        source_machine_id=source.machine_id,
        source_face=source_face,
        target_machine_id=sink.machine_id,
        target_face=target_face,
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
    #: The faces this port may use facing this way (``Machine.allowed_faces``), read once here so
    #: the per-face test below is a set lookup, not a scan of the machine's ports.
    faces: frozenset[Facing]
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
    host = _HOSTS[key] = _Host(
        machine,
        machine.allowed_faces(port_id, orientation),
        (box.sx, box.sy, box.sz),
        cells,
        frozenset(cells),
    )
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
    that was legal before and is still legal comes out identical. The source must eject on a face
    its port may use and the target receive on one its port may use (``Machine.allowed_faces``),
    which for two unpinned ports is exactly the old "neither side's front" rule.
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
    source_host = _host(source_m, source_port, source.orientation)
    target_host = _host(target_m, target_port, target.orientation)
    source_faces, target_faces = source_host.faces, target_host.faces
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
        if face not in source_faces:  # unpinned: its front, which carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite not in target_faces:  # unpinned: the target's input face would be its front
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
