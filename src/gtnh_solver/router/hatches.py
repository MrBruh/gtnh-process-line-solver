"""router.hatches - turn a routed layout into the concrete hatch blocks a builder places.

A multiblock does no I/O of its own. Every connection is a **hatch or bus**: one casing cell of
the structure replaced by a different block, facing a chosen way (docs/DOMAIN.md). Routing decides
*where* a net attaches; this decides *what block goes there*, and it is the last step because two
of its inputs only exist once routing is done - the cells the pipes occupy (a muffler needs air in
front of it) and which nets ended up on a free auto-output rather than a pipe.

::

    routes ------> a hatch per terminal, at the casing cell behind it, facing the way it docked
    autos -------> a hatch per side of each free connection, on the two touching casing cells
    the machine -> one maintenance hatch, and one muffler if its structure accepts one
                       |
                       v
                   LayoutResult.hatches   (+ an Infeasibility if a machine has no room left)

Three rules decide whether a cell may hold a given hatch, all of them ours to enforce because GT
checks none of them (``IStructureElement.check`` takes no facing and every hatch returns
``isFacingValid = true``, so a structure forms happily with every hatch pointing into itself and
then moves nothing):

- **the cell must accept the kind** (``Machine.hatch_slots_for``, permissive where the dump is
  silent - see that method for the three levels);
- **the facing must point out of the structure.** An interior casing cell therefore hosts nothing
  at all, not even a maintenance hatch: it has no outward face to give;
- **one casing cell is one block**, so no two hatches share one.

Only machines whose structure was dumped get hatches. A single-block machine *is* its own I/O -
its faces are the machine's, not a hatch's - so emitting a bus at its cell would describe
replacing the machine with a bus. Those are skipped, which is also every plan adapted without the
physical dataset.

A machine that needs more hatches than its structure has room for is an explicit
:class:`~gtnh_solver.ir.Infeasibility`, never a retry: the casing budget is a per-machine total, so
no nearby cell and no re-placement can create one (docs/hatch-placement/implementation.md 4e).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from gtnh_solver.ir import (
    AutoConnection,
    Facing,
    Infeasibility,
    InputIR,
    Machine,
    PlacedHatch,
    Placement,
    Route,
    Terminal,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, Cell, occupied_cells, rotated_slot
from gtnh_solver.ir.nets import placement_index

from ._grid import FACE_ORDER, body_cell, coord, host_cells

#: The hatches a multiblock needs that serve no net. A machine wants one of each kind its own
#: structure records a cell for: GT only offers the ``Muffler`` element on a controller that
#: pollutes, and the ``Maintenance`` element on one that runs maintenance checks, so the dump's own
#: vocabulary is the requirement. (It over-places on the handful of controllers that accept a
#: muffler without asserting one - the Implosion Compressor - but an unneeded muffler is harmless,
#: while a missing one shuts the machine down with ``POLLUTION_FAIL`` once pollution accumulates.)
UPKEEP_KINDS: tuple[str, ...] = ("Maintenance", "Muffler")

#: What a hatch is called when the port names no kind at all - an endpoint the IR does not
#: describe. It cannot arise from a well-formed problem; the validator reports the port, not this.
_UNKNOWN_KIND = "Unknown"


@dataclass(frozen=True)
class HatchPlan:
    """Every hatch the build needs, or why one machine could not be given the hatches it needs."""

    hatches: tuple[PlacedHatch, ...] = ()
    infeasibility: Infeasibility | None = None

    @property
    def ok(self) -> bool:
        return self.infeasibility is None


def place_hatches(
    problem: InputIR,
    placements: Sequence[Placement],
    routes: Iterable[Route],
    autos: Iterable[AutoConnection],
    occupied: Collection[Cell] = (),
) -> HatchPlan:
    """Emit a :class:`PlacedHatch` for every connection and upkeep hatch the layout needs.

    ``occupied`` are the cells already spoken for by machines and routes; the muffler is the only
    hatch that cares, because GT refuses to vent through anything but literal air
    (``MTEHatchMuffler.polluteEnvironment`` calls ``getAirAtSide``), and a machine that cannot vent
    stops with ``POLLUTION_FAIL``. So the cell in front of a muffler is a **keep-out**: pick a
    facing whose outward cell is empty, or report the machine.

    Deterministic throughout: routed and auto hatches take the cells routing already chose, and the
    upkeep hatches take the first free legal cell in ``FACE_ORDER``-then-ascending-cell order.
    """
    machines = {m.id: m for m in problem.machines}
    by_machine = placement_index(placements)
    port_of_auto = _auto_ports(problem)

    hatches: list[PlacedHatch] = []
    claimed: dict[str, set[Cell]] = {}

    for terminal in _terminals(routes):
        placed = _routed_hatch(terminal, machines, by_machine, claimed)
        if placed is not None:
            hatches.append(placed)

    for auto in autos:
        hatches.extend(_auto_hatches(auto, port_of_auto, machines, by_machine, claimed))

    blocked = set(occupied) | {h.cell.as_tuple() for h in hatches}
    for machine_id in sorted({p.machine_id for p in placements}):
        machine, placement = machines.get(machine_id), by_machine.get(machine_id)
        if machine is None or placement is None or not machine.hatch_slots:
            continue
        upkeep, shortfall = _upkeep_hatches(machine, placement, claimed, blocked, problem)
        if shortfall is not None:
            return HatchPlan(hatches=tuple(hatches), infeasibility=shortfall)
        hatches.extend(upkeep)
    return HatchPlan(hatches=tuple(hatches))


def _terminals(routes: Iterable[Route]) -> list[Terminal]:
    """Every route terminal, deduped by (machine, port): a power sink may tap a shared trunk cell,
    and a machine whose two ports were docked separately still gets one hatch each."""
    seen: dict[tuple[str, str], Terminal] = {}
    for route in routes:
        for terminal in route.terminals:
            seen.setdefault((terminal.machine_id, terminal.port_id), terminal)
    return list(seen.values())


def _routed_hatch(
    terminal: Terminal,
    machines: Mapping[str, Machine],
    by_machine: Mapping[str, Placement],
    claimed: dict[str, set[Cell]],
) -> PlacedHatch | None:
    """The hatch a routed terminal implies: the casing cell behind it, facing the way it docked."""
    machine = machines.get(terminal.machine_id)
    placement = by_machine.get(terminal.machine_id)
    if machine is None or placement is None or not machine.hatch_slots:
        return None  # a single block is its own I/O; it has no hatch to place
    cell = body_cell(terminal)
    if cell in claimed.setdefault(terminal.machine_id, set()):
        return None  # already emitted for another port; the validator reports the contention
    claimed[terminal.machine_id].add(cell)
    return PlacedHatch(
        machine_id=terminal.machine_id,
        kind=_kind_at(machine, placement, terminal.port_id, cell),
        cell=coord(cell),
        facing=terminal.face,
        port_id=terminal.port_id,
    )


def _auto_ports(problem: InputIR) -> dict[str, tuple[str, str]]:
    """Net id -> ``(source port id, target port id)``, for the nets an auto-output can cover.

    ``AutoConnection`` names the two machines and faces but not the ports, since a free connection
    needs no terminal. A hatch does need one: the block that ejects is an *output* bus on the
    source and the block that receives is an *input* bus on the target, and which port each is
    decides the kind. Only 1-source-1-sink nets are ever auto-assigned, so the pair is unambiguous.
    """
    ports: dict[str, tuple[str, str]] = {}
    for net in problem.nets:
        if len(net.endpoints) == 2:
            ports[net.id] = (net.endpoints[0].port_id, net.endpoints[1].port_id)
    return ports


def _auto_hatches(
    auto: AutoConnection,
    port_of_auto: Mapping[str, tuple[str, str]],
    machines: Mapping[str, Machine],
    by_machine: Mapping[str, Placement],
    claimed: dict[str, set[Cell]],
) -> list[PlacedHatch]:
    """The two hatches a free auto-output connection needs, where either side is a multiblock.

    GT's output bus pushes into whatever inventory sits on **its own front face**, every 8 ticks,
    so the pair is two adjacent casing cells: the source's output hatch facing the target, and the
    target's input hatch facing back. ``auto.assign_auto_outputs`` has already proved such a pair
    exists on these faces (that is what it now requires before covering a net), so this re-derives
    the same cells rather than searching.
    """
    ports = port_of_auto.get(auto.net_id)
    if ports is None:
        return []
    out: list[PlacedHatch] = []
    pair = _auto_pair(auto, ports, machines, by_machine, claimed)
    if pair is None:
        return []
    source_cell, target_cell = pair
    for machine_id, port_id, cell, face in (
        (auto.source_machine_id, ports[0], source_cell, auto.source_face),
        (auto.target_machine_id, ports[1], target_cell, auto.target_face),
    ):
        machine, placement = machines.get(machine_id), by_machine.get(machine_id)
        if machine is None or placement is None or not machine.hatch_slots or cell is None:
            continue
        claimed.setdefault(machine_id, set()).add(cell)
        out.append(
            PlacedHatch(
                machine_id=machine_id,
                kind=_kind_at(machine, placement, port_id, cell),
                cell=coord(cell),
                facing=face,
                port_id=port_id,
            )
        )
    return out


def _auto_pair(
    auto: AutoConnection,
    ports: tuple[str, str],
    machines: Mapping[str, Machine],
    by_machine: Mapping[str, Placement],
    claimed: Mapping[str, Collection[Cell]],
) -> tuple[Cell | None, Cell | None] | None:
    """The touching ``(source cell, target cell)`` the connection runs through, unclaimed.

    ``None`` on a side means that machine places no hatch there (it is a single block, whose own
    face does the work). ``None`` for the whole pair means no unclaimed pair is left, which the
    caller treats as "emit nothing" - the connection itself was already certified by
    ``assign_auto_outputs``.
    """
    source_m, source_p = (
        machines.get(auto.source_machine_id),
        by_machine.get(auto.source_machine_id),
    )
    target_m, target_p = (
        machines.get(auto.target_machine_id),
        by_machine.get(auto.target_machine_id),
    )
    if source_m is None or source_p is None or target_m is None or target_p is None:
        return None
    dx, dy, dz = FACE_DELTAS[auto.source_face]
    taken_source = claimed.get(auto.source_machine_id, ())
    taken_target = claimed.get(auto.target_machine_id, ())
    target_hosts = set(port_cells(target_p, target_m, ports[1]))
    for cell in port_cells(source_p, source_m, ports[0]):
        neighbour = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
        if neighbour not in target_hosts or cell in taken_source or neighbour in taken_target:
            continue
        return (
            cell if source_m.hatch_slots else None,
            neighbour if target_m.hatch_slots else None,
        )
    return None


def _upkeep_hatches(
    machine: Machine,
    placement: Placement,
    claimed: dict[str, set[Cell]],
    blocked: set[Cell],
    problem: InputIR,
) -> tuple[list[PlacedHatch], Infeasibility | None]:
    """One maintenance hatch, and one muffler where the structure records one.

    They belong to no net and have no route, which is why ``LayoutResult`` needed a place to put
    them at all. Both still spend a casing cell, so leaving them out would leave the casing budget
    optimistic by up to two cells on every machine - and that budget is exactly what silently
    un-forms a multiblock.

    The muffler is the one with a spatial rule of its own: it vents only through literal air on its
    front face, so a cable, a pipe, a casing or a neighbouring machine in that cell makes
    ``polluteEnvironment`` return false and the machine shuts down with ``POLLUTION_FAIL``. Its
    outward cell is therefore required to be empty. A maintenance hatch has no such rule, but still
    needs an outward face like every hatch: an interior casing cell can host nothing.
    """
    out: list[PlacedHatch] = []
    recorded = {kind for slot in machine.hatch_slots for kind in slot.kinds}
    for kind in UPKEEP_KINDS:
        if kind not in recorded:
            continue  # this structure does not take one, so it does not need one
        spot = _free_face(
            machine, placement, kind, claimed, blocked, problem, air=kind == "Muffler"
        )
        if spot is None:
            return out, _no_room(machine, kind)
        cell, face = spot
        claimed.setdefault(machine.id, set()).add(cell)
        blocked.add(cell)
        out.append(PlacedHatch(machine_id=machine.id, kind=kind, cell=coord(cell), facing=face))
    return out, None


def _free_face(
    machine: Machine,
    placement: Placement,
    kind: str,
    claimed: Mapping[str, Collection[Cell]],
    blocked: Collection[Cell],
    problem: InputIR,
    *,
    air: bool,
) -> tuple[Cell, Facing] | None:
    """The first unclaimed casing cell accepting ``kind`` that has an outward face, if any.

    ``FACE_ORDER`` then ascending cell, the same total order docking uses, so the choice is
    reproducible. ``air`` additionally demands that the outward cell be empty - the muffler's vent
    rule - where an ordinary hatch only needs the face to point out of its own structure.
    """
    body = set(occupied_cells(placement.cell, machine.footprint, placement.orientation))
    slots = [s for s in machine.hatch_slots if kind in s.kinds]
    taken = claimed.get(machine.id, ())
    hosts = [c for c in host_cells(placement, machine, slots) if c not in taken]
    reserved = {c.as_tuple() for c in problem.reserved_cells}
    for face in FACE_ORDER:
        if face is placement.orientation:
            continue
        dx, dy, dz = FACE_DELTAS[face]
        for cell in hosts:
            outward = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
            if outward in body:
                continue  # facing into its own structure: it would move nothing
            if air and (outward in blocked or outward in reserved):
                continue  # a muffler vents through literal air or not at all
            return cell, face
    return None


def port_cells(placement: Placement, machine: Machine, port_id: str) -> list[Cell]:
    """The machine's casing cells that could host ``port_id``'s hatch, ascending.

    The whole body for a machine whose slots were never dumped, which is what keeps a single-block
    machine - and every plan adapted without the physical dataset - behaving exactly as before.
    """
    return host_cells(placement, machine, machine.hatch_slots_for(port_id))


def _kind_at(machine: Machine, placement: Placement, port_id: str, cell: Cell) -> str:
    """Which ``HatchElement`` this port's hatch is, preferring a spelling the cell records.

    A power input can be an ``Energy``, ``ExoticEnergy`` or ``MultiAmpEnergy`` hatch and the cell
    knows which of them it accepts. Where the dump names none of the port's kinds - the permissive
    case that lets a Chemical Plant be powered at all - the port's first kind is the plain one and
    the right default.
    """
    kinds = machine.hatch_kinds_for(port_id)
    if not kinds:
        return _UNKNOWN_KIND
    accepted = _kinds_by_cell(placement, machine).get(cell, frozenset())
    return next((kind for kind in kinds if kind in accepted), kinds[0])


def _kinds_by_cell(placement: Placement, machine: Machine) -> dict[Cell, frozenset[str]]:
    """This machine's placed casing cells to the kinds each records. Empty when nothing was dumped.

    ``host_cells`` sorts, so it cannot be zipped back onto the slot list; the offsets are turned
    here directly instead. The turn is injective (property-tested), so no two slots collide.
    """
    turned: dict[Cell, frozenset[str]] = {}
    origin = placement.cell
    for slot in machine.hatch_slots:
        dx, dy, dz = rotated_slot(slot.offset.as_tuple(), machine.footprint, placement.orientation)
        turned[(origin.x + dx, origin.y + dy, origin.z + dz)] = frozenset(slot.kinds)
    return turned


def _no_room(machine: Machine, kind: str) -> Infeasibility:
    return Infeasibility(
        constraint="hatch_budget",
        detail=(
            f"machine {machine.id!r} ({machine.type}) has no casing cell left for its {kind} "
            f"hatch: its {len(machine.hatch_slots)} hatch cell(s) are all spent on its "
            f"{len(machine.faces.ports)} connection(s)"
            + (", or none has empty air in front to vent through" if kind == "Muffler" else "")
        ),
        suggested_relaxation=(
            "leave routing gaps around the machine so a face stays clear"
            if kind == "Muffler"
            else "reduce the machine's connections, or split the recipe over more machines"
        ),
    )
