"""validator.me - the ME (AE2) gate: would AE run every ME network the layout builds? (#333)

The rules are docs/DOMAIN.md, "What a valid ME build is", each read from AE2's source and cited in
``docs/spikes/329-me-ae2.md`` ("spike 2.4" below). Written the way the rest of the validator is
(docs/ARCHITECTURE.md #4): it shares the rule DATA in ``dataset/me.py`` (capacities, card ladders,
GT's ME hatches) and nothing the router computes. Above all it never reads which blocks the router
*meant* to join: AE2 cables join every compatible neighbour on their own, so the graph AE builds is
rebuilt here from the blocks the layout places, and AE's own channel pathing is run on it::

    layout.me_networks + placements + hatches
        |
        |-- endpoints     each MEEndpoint built once, as specified           ME_ENDPOINT_MISSING
        |                                                                     ME_DEVICE_MISMATCH
        |-- placement     a part on a part-taking cable, facing its block;   ME_DEVICE_PLACEMENT
        |                 a GT ME hatch in a slot of its kind, front out      ME_PART_ON_DENSE
        |                 a single block's interfaces on one face             ME_AUTO_OUTPUT_FACES
        |-- rates         each device keeps up with its share                ME_DEVICE_RATE_SHORT
        |                                                                     ME_UPGRADE_SLOTS
        |-- ground        cables in the region, on nothing else              ROUTE_* (as pipes)
        |                 a stub or link is a cable of its network           ME_INFRASTRUCTURE
        v
    the AE graph (cables, parts, block devices, GT ME hatch fronts; colours; part sides)
        |-- pieces        one network a piece, one piece a network           ME_NETWORK_MERGE / SPLIT
        |-- sources       a stub, a valid controller, or ad hoc (<= 8)       ME_CHANNEL_STARVED
        |                                                                     ME_CONTROLLER_CONFLICT
        |                                                                     ME_ADHOC_OVERFLOW
        |-- pathing       AE's 3-queue BFS: every block carries no more      ME_CABLE_OVERLOAD
        |                 devices than its capacity; the budget holds        ME_ATTACH_BUDGET
        v
    violations, plus two abstentions: nets on ME nothing serves yet, subnet blocks on the edge

**The pathing is exact on a tree and sound on a cycle** (spike 2.4, 2.5). AE hands out channels
by a BFS from the channel sources over three queues (dense, then other cables, then everything
else, an item's queue never lower than its parent's), deciding each device once, along its one
path. Each item's place in that order is a label: the counts of path items in each queue, compared
most significant queue first. Every node's parent is an incident connection with the least label,
so this computes the labels (a Dijkstra over items), keeps every tied least-label connection as a
*possible* parent, and charges each block every channel device reachable below it through possible
parents. Where no node has a tie, that is AE's one tree and the count is exact, whatever order AE
visits blocks in; where ties exist, every tree AE could pick has its subtrees inside these, so a
block within capacity here is within capacity in game.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from gtnh_solver.dataset.me import (
    ADHOC_MAX_DEVICES,
    BUS_PERIOD_TICKS,
    CABLE_CAPACITY,
    DEVICE_CAPACITY,
    FLUID_ACCELERATION,
    FLUID_BASE_MB,
    FLUID_SUPER_SPEED,
    GT_ME_HATCHES,
    ITEM_ACCELERATION,
    ITEM_SUPER_SPEED,
    OUTPUT_BUS_PUSH_TICKS,
    PART_CABLES,
    UPGRADE_SLOTS,
)
from gtnh_solver.dataset.voltage import VOLTAGE_BY_TIER
from gtnh_solver.ir import (
    AEColor,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    Machine,
    MECableKind,
    MEDeviceKind,
    MEEndpoint,
    MEMode,
    MENetworkLayout,
    MEPlacedDevice,
    MERole,
    Placement,
    Port,
)
from gtnh_solver.ir.nets import placement_index

from ._geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, body_cells, in_region, usable_faces
from .report import Violation, ViolationCode

# --- the validator's own reading of each device (DATA: what it moves; LOGIC: kept here) -----------

#: What each device moves, and which way, from the port's side: an OUTPUT port gives to the
#: network, an INPUT port takes from it. A storage bus serves a storage block or a link, never a
#: machine's port, so it is listed by commodity alone (direction ``None``).
_SERVES: dict[MEDeviceKind, tuple[frozenset[Commodity], IODirection | None]] = {
    MEDeviceKind.IMPORT_BUS: (frozenset({Commodity.ITEM}), IODirection.OUTPUT),
    MEDeviceKind.EXPORT_BUS: (frozenset({Commodity.ITEM}), IODirection.INPUT),
    MEDeviceKind.INTERFACE: (frozenset({Commodity.ITEM}), IODirection.OUTPUT),
    MEDeviceKind.FLUID_IMPORT_BUS: (frozenset({Commodity.FLUID}), IODirection.OUTPUT),
    MEDeviceKind.FLUID_EXPORT_BUS: (frozenset({Commodity.FLUID}), IODirection.INPUT),
    MEDeviceKind.DUAL_INTERFACE: (
        frozenset({Commodity.ITEM, Commodity.FLUID}),
        IODirection.OUTPUT,
    ),
    MEDeviceKind.STORAGE_BUS: (frozenset({Commodity.ITEM}), None),
    MEDeviceKind.FLUID_STORAGE_BUS: (frozenset({Commodity.FLUID}), None),
    MEDeviceKind.GT_OUTPUT_BUS_ME: (frozenset({Commodity.ITEM}), IODirection.OUTPUT),
    MEDeviceKind.GT_OUTPUT_HATCH_ME: (frozenset({Commodity.FLUID}), IODirection.OUTPUT),
    MEDeviceKind.GT_STOCKING_INPUT_BUS_ME: (frozenset({Commodity.ITEM}), IODirection.INPUT),
    MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME: (frozenset({Commodity.FLUID}), IODirection.INPUT),
}
#: The devices a single block's auto-output pushes into: they share its one auto-output face.
_RECEIVERS = frozenset({MEDeviceKind.INTERFACE, MEDeviceKind.DUAL_INTERFACE})
#: The devices whose rate their cards set, by the commodity they move.
_CARDED: dict[MEDeviceKind, Commodity] = {
    MEDeviceKind.IMPORT_BUS: Commodity.ITEM,
    MEDeviceKind.EXPORT_BUS: Commodity.ITEM,
    MEDeviceKind.FLUID_IMPORT_BUS: Commodity.FLUID,
    MEDeviceKind.FLUID_EXPORT_BUS: Commodity.FLUID,
}
_STORAGE_BUSES = frozenset({MEDeviceKind.STORAGE_BUS, MEDeviceKind.FLUID_STORAGE_BUS})


def _is_gt_hatch(kind: MEDeviceKind) -> bool:
    return kind.value.startswith("gt_")


def _colours_connect(a: AEColor, b: AEColor) -> bool:
    """AE2's ``AEColor.matches``, on the validator's own arithmetic: Fluix joins every colour, and
    two others join only when equal (spike 3)."""
    return a is AEColor.FLUIX or b is AEColor.FLUIX or a is b


def _step(cell: Cell, face: Facing) -> Cell:
    dx, dy, dz = FACE_DELTAS[face]
    return (cell[0] + dx, cell[1] + dy, cell[2] + dz)


def _tier_index(tier: str) -> int:
    ladder = list(VOLTAGE_BY_TIER)
    return ladder.index(tier) if tier in ladder else 0


# --- the result the solver reads -------------------------------------------------------------------


@dataclass(frozen=True)
class MEAbstentions:
    """What the gate could not judge, reported beside the verdict rather than as violations.

    ``unbuilt_me_nets``: nets riding ME with a port no ME endpoint serves, so nothing in the layout
    says how that port reaches the network (every net on ME until the end-to-end build, #335).
    ``boundary_exposure``: a subnet's AE blocks on the region's edge, which an AE block the player
    builds just outside would join; nothing in the layout can rule that out.
    """

    unbuilt_me_nets: tuple[str, ...] = ()
    boundary_exposure: tuple[Cell, ...] = ()


# --- the AE graph -----------------------------------------------------------------------------------

#: A node's class: which of AE's three queues it enters (spike 2.1). Dense cables 0, other
#: cables 1 (``PREFERRED``), everything else 2.
_DENSE, _PREFERRED, _OTHER = 0, 1, 2


@dataclass
class _Node:
    """One AE grid node the layout builds."""

    key: tuple[object, ...]
    network: str
    colour: AEColor
    capacity: int
    cls: int
    channel: bool  # it needs a channel of its own
    cell: Cell | None = None  # where it is, for messages; None for a block spanning several
    label: str = ""
    root: bool = False  # an attach stub: channels arrive here from the main network
    controller: bool = False  # never carries channels; its neighbours are the roots
    links: set[int] = field(default_factory=set)  # indices of joined nodes


@dataclass
class _Graph:
    nodes: list[_Node] = field(default_factory=list)
    by_key: dict[tuple[object, ...], int] = field(default_factory=dict)

    def add(self, node: _Node) -> int:
        if node.key in self.by_key:
            return self.by_key[node.key]
        self.by_key[node.key] = len(self.nodes)
        self.nodes.append(node)
        return len(self.nodes) - 1

    def join(self, a: int, b: int) -> None:
        if a != b:
            self.nodes[a].links.add(b)
            self.nodes[b].links.add(a)


# --- entry point ------------------------------------------------------------------------------------


def check_me(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> MEAbstentions:
    """Hold every ME network ``layout`` builds to AE's own rules; return what could not be judged."""
    machines = {m.id: m for m in problem.machines}
    placements = placement_index(layout.placements)
    specs = {n.id: n for n in problem.me.networks}
    built = {n.id: n for n in layout.me_networks}

    for network in layout.me_networks:
        if network.id not in specs:
            out.append(
                Violation(
                    ViolationCode.ME_UNKNOWN_NETWORK,
                    f"the layout builds ME network {network.id!r}, which the problem does not have",
                )
            )
        elif network.colour is not problem.me.colour(network.id):
            out.append(
                Violation(
                    ViolationCode.ME_NETWORK_COLOUR,
                    f"ME network {network.id!r} is built {network.colour.value}, but it is "
                    f"{problem.me.colour(network.id).value}",
                )
            )

    devices = _check_endpoints(problem, layout, machines, out)
    _check_placement(problem, layout, machines, placements, devices, out)
    _check_rates(problem, machines, devices, out)
    _check_ground(problem, layout, machines, placements, out)
    graph = _build_graph(problem, layout, machines, placements, built)
    _check_channels(problem, graph, out)
    return MEAbstentions(
        unbuilt_me_nets=_unbuilt(problem),
        boundary_exposure=_exposure(problem, layout, machines, placements),
    )


def _unbuilt(problem: InputIR) -> tuple[str, ...]:
    """The nets riding ME with a machine port no ME endpoint serves (an abstention)."""
    served = {(m.id, port) for m in problem.machines for e in m.me_endpoints for port in e.ports}
    return tuple(
        net.id
        for net in problem.nets
        if net.rides_me and any((ep.machine_id, ep.port_id) not in served for ep in net.endpoints)
    )


# --- 1. each endpoint built once, as specified -----------------------------------------------------


def _check_endpoints(
    problem: InputIR, layout: LayoutResult, machines: Mapping[str, Machine], out: list[Violation]
) -> dict[tuple[str, str], tuple[MEPlacedDevice, MEEndpoint, str]]:
    """Pair every placed device with the endpoint it builds; return ``(machine, endpoint) ->
    (device, endpoint, network it was listed under)`` for the ones that pair cleanly."""
    endpoints = {(m.id, e.id): e for m in problem.machines for e in m.me_endpoints}
    paired: dict[tuple[str, str], tuple[MEPlacedDevice, MEEndpoint, str]] = {}
    for network in layout.me_networks:
        for device in network.devices:
            key = (device.machine_id, device.endpoint_id)
            endpoint = endpoints.get(key)
            if endpoint is None:
                out.append(
                    Violation(
                        ViolationCode.ME_DEVICE_MISMATCH,
                        f"ME device {device.kind.value} at {device.cell.as_tuple()} builds "
                        f"endpoint {device.endpoint_id!r} of {device.machine_id!r}, which the "
                        f"problem does not ask for",
                        machine_id=device.machine_id,
                    )
                )
                continue
            if key in paired:
                out.append(
                    Violation(
                        ViolationCode.ME_DEVICE_MISMATCH,
                        f"ME endpoint {device.endpoint_id!r} of {device.machine_id!r} is built "
                        f"twice",
                        machine_id=device.machine_id,
                    )
                )
                continue
            spec = endpoint.device
            wrong = [
                name
                for name, ok in (
                    ("network", endpoint.network == network.id),
                    ("kind", device.kind is spec.kind),
                    ("mID", device.gt_mid == spec.gt_mid),
                    ("cards", device.cards == spec.cards),
                    ("config", device.config == spec.config),
                )
                if not ok
            ]
            if wrong:
                out.append(
                    Violation(
                        ViolationCode.ME_DEVICE_MISMATCH,
                        f"ME endpoint {device.endpoint_id!r} of {device.machine_id!r} is built "
                        f"with the wrong {', '.join(wrong)}: the problem asks for "
                        f"{spec.kind.value} on network {endpoint.network!r}",
                        machine_id=device.machine_id,
                    )
                )
            paired[key] = (device, endpoint, network.id)
            commodities, direction = _SERVES[spec.kind]
            machine = machines.get(device.machine_id)
            for port in _ports(machine, endpoint):
                if port.commodity not in commodities or (
                    direction is not None and port.direction is not direction
                ):
                    out.append(
                        Violation(
                            ViolationCode.ME_DEVICE_MISMATCH,
                            f"{spec.kind.value} cannot serve {port.commodity.value} "
                            f"{port.direction.value} {port.id!r} of {device.machine_id!r}",
                            machine_id=device.machine_id,
                        )
                    )
    for (machine_id, endpoint_id), endpoint in endpoints.items():
        if (machine_id, endpoint_id) not in paired:
            out.append(
                Violation(
                    ViolationCode.ME_ENDPOINT_MISSING,
                    f"ME endpoint {endpoint_id!r} of {machine_id!r} ({endpoint.device.kind.value} "
                    f"on network {endpoint.network!r}) is not built",
                    machine_id=machine_id,
                )
            )
    return paired


def _ports(machine: Machine | None, endpoint: MEEndpoint) -> list[Port]:
    if machine is None:
        return []
    by_id = {p.id: p for p in machine.faces.ports}
    return [by_id[p] for p in endpoint.ports if p in by_id]


# --- 2. where each device stands --------------------------------------------------------------------


def _check_placement(
    problem: InputIR,
    layout: LayoutResult,
    machines: Mapping[str, Machine],
    placements: Mapping[str, Placement],
    devices: Mapping[tuple[str, str], tuple[MEPlacedDevice, MEEndpoint, str]],
    out: list[Violation],
) -> None:
    """Rules 2 and 3: every device is where its job is, and a single block's interfaces share one
    face."""
    cables = {
        c.cell.as_tuple(): (network.id, c.kind)
        for network in layout.me_networks
        for c in network.cables
    }
    hatches = {(h.machine_id, h.cell.as_tuple()): h for h in layout.hatches}
    sides_taken: dict[tuple[Cell, Facing], str] = {}
    receiver_faces: dict[str, set[Facing]] = defaultdict(set)

    for device, endpoint, network_id in devices.values():
        machine = machines.get(device.machine_id)
        placement = placements.get(device.machine_id)
        if machine is None or placement is None:
            continue  # the placement checks report an unplaced machine
        cell = device.cell.as_tuple()
        body = body_cells(placement.cell, machine.footprint, placement.orientation)

        if _is_gt_hatch(device.kind):
            hatch = hatches.get((machine.id, cell))
            if cell not in body:
                _misplaced(device, out, "a GT ME hatch takes a casing cell of its own machine")
            elif hatch is None or hatch.facing is not device.side:
                _misplaced(
                    device, out, f"no hatch of this machine there facing {device.side.value}"
                )
            elif hatch.kind != endpoint.hatch_kind or (
                device.gt_mid in GT_ME_HATCHES
                and GT_ME_HATCHES[device.gt_mid].hatch_kind != hatch.kind
            ):
                _misplaced(
                    device,
                    out,
                    f"the hatch there is a {hatch.kind}, not this GT ME hatch's slot kind",
                )
            elif hatch.port_id not in endpoint.ports:
                _misplaced(
                    device,
                    out,
                    f"the hatch there serves {hatch.port_id!r}, not this endpoint's ports",
                )
            continue

        # A part: on a cable of its network that takes parts, on a side no other part takes.
        under = cables.get(cell)
        if under is None or under[0] != network_id:
            _misplaced(device, out, "a part needs a cable of its own network to sit on")
            continue
        if under[1] not in PART_CABLES:
            out.append(
                Violation(
                    ViolationCode.ME_PART_ON_DENSE,
                    f"{device.kind.value} for {device.endpoint_id!r} of {device.machine_id!r} sits "
                    f"on a {under[1].value} cable at {cell}, which takes no part (spike 3)",
                    machine_id=device.machine_id,
                )
            )
        taken = sides_taken.get((cell, device.side))
        if taken is not None:
            _misplaced(
                device, out, f"the {device.side.value} side of that cable already holds {taken}"
            )
        sides_taken[(cell, device.side)] = device.kind.value
        target = _step(cell, device.side)
        faced = OPPOSITE_FACE[device.side]

        if machine.me_role is MERole.LINK:
            if device.kind not in _STORAGE_BUSES:
                _misplaced(device, out, "a link carries a storage bus")
            if cell not in body or device.side is not placement.orientation:
                _misplaced(
                    device,
                    out,
                    "a link's storage bus sits on its own cable, on its front, facing out",
                )
        elif endpoint.hatch_kind is not None and not machine.hatch_slots:
            # No slot was recorded for this machine, so no hatch is placed on it (as for a pipe's
            # terminal): its part only has to face the machine.
            if target not in body:
                _misplaced(
                    device, out, f"it faces {target}, which is not a cell of {device.machine_id!r}"
                )
        elif endpoint.hatch_kind is not None:
            hatch = hatches.get((machine.id, target))
            if (
                hatch is None
                or hatch.kind != endpoint.hatch_kind
                or hatch.facing is not faced
                or hatch.port_id not in endpoint.ports
            ):
                _misplaced(
                    device,
                    out,
                    f"it must face a {endpoint.hatch_kind} of this machine at {target} whose "
                    f"front faces back at it, serving one of {list(endpoint.ports)}",
                )
        else:
            if target not in body:
                _misplaced(
                    device, out, f"it faces {target}, which is not a cell of {device.machine_id!r}"
                )
                continue
            for port in _ports(machine, endpoint):
                if faced not in usable_faces(port.faces, placement.orientation):
                    _misplaced(
                        device,
                        out,
                        f"it works on the {faced.value} face, which port {port.id!r} may not use "
                        f"(the front carries no I/O)",
                    )
            if device.kind in _RECEIVERS:
                receiver_faces[machine.id].add(faced)

    autos: dict[str, set[Facing]] = defaultdict(set)
    for auto in layout.auto_connections:
        autos[auto.source_machine_id].add(auto.source_face)
    for machine_id, faces in sorted(receiver_faces.items()):
        every = faces | autos.get(machine_id, set())
        if len(every) > 1:
            out.append(
                Violation(
                    ViolationCode.ME_AUTO_OUTPUT_FACES,
                    f"{machine_id!r} would auto-output through {sorted(f.value for f in every)}, "
                    f"but a single block auto-outputs through one face",
                    machine_id=machine_id,
                )
            )


def _misplaced(device: MEPlacedDevice, out: list[Violation], why: str) -> None:
    out.append(
        Violation(
            ViolationCode.ME_DEVICE_PLACEMENT,
            f"{device.kind.value} for {device.endpoint_id!r} of {device.machine_id!r} at "
            f"{device.cell.as_tuple()}: {why}",
            machine_id=device.machine_id,
        )
    )


# --- 3. rates ---------------------------------------------------------------------------------------


def _bus_rate(commodity: Commodity, acceleration: int, super_speed: int) -> float:
    """What one bus moves per tick with these cards, on the validator's own arithmetic."""
    if commodity is Commodity.ITEM:
        per_operation = float(ITEM_ACCELERATION[acceleration] + ITEM_SUPER_SPEED[super_speed])
    else:
        per_operation = float(
            FLUID_BASE_MB * FLUID_ACCELERATION[acceleration] * FLUID_SUPER_SPEED[super_speed]
        )
    return per_operation / BUS_PERIOD_TICKS


def _check_rates(
    problem: InputIR,
    machines: Mapping[str, Machine],
    devices: Mapping[tuple[str, str], tuple[MEPlacedDevice, MEEndpoint, str]],
    out: list[Violation],
) -> None:
    """Rule 4: every device keeps up with its share of its ports' rate; a bus takes four cards."""
    for device, endpoint, _ in devices.values():
        machine = machines.get(device.machine_id)
        if machine is None:
            continue
        cards = device.cards
        if cards.count > UPGRADE_SLOTS:
            out.append(
                Violation(
                    ViolationCode.ME_UPGRADE_SLOTS,
                    f"{device.kind.value} for {device.endpoint_id!r} of {machine.id!r} is fitted "
                    f"{cards.count} cards, but takes {UPGRADE_SLOTS}",
                    machine_id=machine.id,
                )
            )
            continue
        for port in _ports(machine, endpoint):
            if port.rate is None:
                continue  # unrated: nothing to hold it to
            need = port.rate * endpoint.share
            cap = _capacity(device, endpoint, machine, port.commodity)
            if cap is not None and cap + 1e-9 < need:
                unit = "items/t" if port.commodity is Commodity.ITEM else "mB/t"
                out.append(
                    Violation(
                        ViolationCode.ME_DEVICE_RATE_SHORT,
                        f"{device.kind.value} for {device.endpoint_id!r} of {machine.id!r} moves "
                        f"{cap:g} {unit}, short of the {need:g} {unit} port {port.id!r} needs "
                        f"of it",
                        machine_id=machine.id,
                    )
                )


def _capacity(
    device: MEPlacedDevice, endpoint: MEEndpoint, machine: Machine, commodity: Commodity
) -> float | None:
    """What ``device`` moves per tick, or None where nothing of its own bounds it."""
    carded = _CARDED.get(device.kind)
    if carded is not None:
        return _bus_rate(carded, device.cards.acceleration, device.cards.super_speed)
    if device.gt_mid is not None and device.gt_mid in GT_ME_HATCHES:
        return GT_ME_HATCHES[device.gt_mid].per_tick
    if device.kind in _RECEIVERS and endpoint.hatch_kind == "OutputBus":
        # A normal output bus pushes its (tier + 1)^2 slots of 64 into its front every 8 ticks.
        slots = (_tier_index(machine.voltage_tier) + 1) ** 2
        return slots * 64 / OUTPUT_BUS_PUSH_TICKS
    return None  # an interface fed by a hatch or a single block, a stocking hatch, a storage bus


# --- 4. the ground every cable stands on ------------------------------------------------------------


def _check_ground(
    problem: InputIR,
    layout: LayoutResult,
    machines: Mapping[str, Machine],
    placements: Mapping[str, Placement],
    out: list[Violation],
) -> None:
    """Rule 1, and a stub or a link being the cable it is."""
    region = problem.bounding_region
    reserved = {c.as_tuple() for c in problem.reserved_cells}
    owner_of_body: dict[Cell, str] = {}
    for placement in layout.placements:
        machine = machines.get(placement.machine_id)
        if machine is None:
            continue
        for cell in body_cells(placement.cell, machine.footprint, placement.orientation):
            owner_of_body.setdefault(cell, machine.id)
    route_owner: dict[Cell, str] = {}
    for route in layout.routes:
        for cell in route.cells():
            route_owner.setdefault(cell, route.net_id)
    cable_owner: dict[Cell, str] = {}
    for network in layout.me_networks:
        for cable in network.cables:
            cell = cable.cell.as_tuple()
            where = f"ME network {network.id!r}'s cable at {cell}"
            if not in_region(cell, region):
                out.append(Violation(ViolationCode.ROUTE_OUT_OF_BOUNDS, f"{where} is outside"))
            if cell in reserved:
                out.append(Violation(ViolationCode.ROUTE_ON_RESERVED, f"{where} is reserved"))
            body_owner = owner_of_body.get(cell)
            if body_owner is not None:
                own = machines[body_owner]
                if not (
                    own.me_role in (MERole.ATTACH, MERole.LINK) and own.me_network == network.id
                ):
                    out.append(
                        Violation(
                            ViolationCode.ROUTE_THROUGH_MACHINE,
                            f"{where} sits inside {body_owner!r}",
                            machine_id=body_owner,
                        )
                    )
            if cell in route_owner:
                out.append(
                    Violation(
                        ViolationCode.ROUTE_CELL_COLLISION,
                        f"{where} is also net {route_owner[cell]!r}'s route",
                    )
                )
            other = cable_owner.get(cell)
            if other is not None and other != network.id:
                out.append(
                    Violation(
                        ViolationCode.ROUTE_CELL_COLLISION,
                        f"{where} is also ME network {other!r}'s cable",
                    )
                )
            cable_owner.setdefault(cell, network.id)

    kinds = {
        (network.id, c.cell.as_tuple()): c.kind
        for network in layout.me_networks
        for c in network.cables
    }
    for machine in problem.machines:
        if machine.me_role not in (MERole.ATTACH, MERole.LINK):
            continue
        placed = placements.get(machine.id)
        if placed is None or machine.me_network is None:
            continue
        cells = body_cells(placed.cell, machine.footprint, placed.orientation)
        wanted = MECableKind.DENSE if machine.me_role is MERole.ATTACH else None
        for cell in cells:
            kind = kinds.get((machine.me_network, cell))
            if (
                kind is None
                or (wanted is not None and kind is not wanted)
                or (wanted is None and kind not in PART_CABLES)
            ):
                out.append(
                    Violation(
                        ViolationCode.ME_INFRASTRUCTURE,
                        f"ME {machine.me_role.value} {machine.id!r} at {cell} must be a "
                        f"{'dense cable' if wanted else 'cable that takes its storage bus'} of "
                        f"network {machine.me_network!r}",
                        machine_id=machine.id,
                    )
                )


def _exposure(
    problem: InputIR,
    layout: LayoutResult,
    machines: Mapping[str, Machine],
    placements: Mapping[str, Placement],
) -> tuple[Cell, ...]:
    """A subnet's cables on the region's edge, bar its links (which face out on purpose)."""
    region = problem.bounding_region
    subnets = {n.id for n in problem.me.networks if n.mode is MEMode.SUBNET}
    links: set[Cell] = set()
    for machine in machines.values():
        placement = placements.get(machine.id)
        if machine.me_role is MERole.LINK and placement is not None:
            links |= body_cells(placement.cell, machine.footprint, placement.orientation)
    edge: list[Cell] = []
    for network in layout.me_networks:
        if network.id not in subnets:
            continue
        for cable in network.cables:
            x, y, z = cell = cable.cell.as_tuple()
            on_edge = x in (0, region.sx - 1) or y in (0, region.sy - 1) or z in (0, region.sz - 1)
            if on_edge and cell not in links:
                edge.append(cell)
    return tuple(sorted(set(edge)))


# --- 5. the graph AE builds ------------------------------------------------------------------------


def _build_graph(
    problem: InputIR,
    layout: LayoutResult,
    machines: Mapping[str, Machine],
    placements: Mapping[str, Placement],
    built: Mapping[str, MENetworkLayout],
) -> _Graph:
    """Every AE node the layout places, joined the way AE joins them (spike 3)."""
    graph = _Graph()
    cable_at: dict[Cell, int] = {}
    parts_on: dict[Cell, set[Facing]] = defaultdict(set)
    stubs = {
        cell
        for m in machines.values()
        if m.me_role is MERole.ATTACH and (p := placements.get(m.id)) is not None
        for cell in body_cells(p.cell, m.footprint, p.orientation)
    }

    for network in layout.me_networks:
        for cable in network.cables:
            cell = cable.cell.as_tuple()
            if cell in cable_at:
                continue  # two networks on one cell: a collision already reported
            dense = cable.kind in (MECableKind.DENSE, MECableKind.DENSE_COVERED)
            cable_at[cell] = graph.add(
                _Node(
                    key=("cable", cell),
                    network=network.id,
                    colour=network.colour,
                    capacity=CABLE_CAPACITY[cable.kind],
                    cls=_DENSE if dense else _PREFERRED,
                    channel=False,
                    cell=cell,
                    label=f"{cable.kind.value} cable at {cell}",
                    root=cell in stubs,
                )
            )
        for device in network.devices:
            if not _is_gt_hatch(device.kind):
                parts_on[device.cell.as_tuple()].add(device.side)

    def nodes_network(index: int) -> str:
        return graph.nodes[index].network

    hatch_at: dict[Cell, tuple[int, Facing]] = {}
    for network in layout.me_networks:
        for device in network.devices:
            cell = device.cell.as_tuple()
            node = _Node(
                key=("device", device.machine_id, device.endpoint_id),
                network=network.id,
                colour=network.colour,
                capacity=DEVICE_CAPACITY,
                cls=_OTHER,
                channel=True,
                cell=cell,
                label=f"{device.kind.value} for {device.endpoint_id!r} of {device.machine_id!r}",
            )
            index = graph.add(node)
            if _is_gt_hatch(device.kind):
                hatch_at[cell] = (index, device.side)
            elif cell in cable_at and nodes_network(cable_at[cell]) == network.id:
                graph.join(index, cable_at[cell])  # a part joins its own cable, and nothing else

    block_at: dict[Cell, int] = {}
    for machine in machines.values():
        if machine.me_role not in (MERole.CONTROLLER, MERole.ACCEPTOR):
            continue
        placement = placements.get(machine.id)
        if placement is None or machine.me_network is None:
            continue
        controller = machine.me_role is MERole.CONTROLLER
        colour = problem.me.colour(machine.me_network) if controller else AEColor.FLUIX
        index = graph.add(
            _Node(
                key=("block", machine.id),
                network=machine.me_network,
                colour=colour,
                capacity=0 if controller else DEVICE_CAPACITY,
                cls=_OTHER,
                channel=False,
                cell=placement.cell.as_tuple(),
                label=f"{machine.me_role.value} {machine.id!r}",
                controller=controller,
            )
        )
        for cell in body_cells(placement.cell, machine.footprint, placement.orientation):
            block_at[cell] = index

    nodes = graph.nodes

    def joins(a: int, b: int) -> bool:
        return _colours_connect(nodes[a].colour, nodes[b].colour)

    for cell, index in cable_at.items():
        for face in Facing:
            if face in parts_on.get(cell, ()):
                continue  # a part on this side takes it from the cable
            there = _step(cell, face)
            back = OPPOSITE_FACE[face]
            other = cable_at.get(there)
            if other is not None and back not in parts_on.get(there, ()) and joins(index, other):
                graph.join(index, other)
            block = block_at.get(there)
            if block is not None and joins(index, block):
                graph.join(index, block)
            hatch = hatch_at.get(there)
            if hatch is not None and hatch[1] is back and joins(index, hatch[0]):
                graph.join(index, hatch[0])
    for cell, block in block_at.items():
        for face in Facing:
            there = _step(cell, face)
            other = block_at.get(there)
            if other is not None and joins(block, other):
                graph.join(block, other)
            hatch = hatch_at.get(there)
            if hatch is not None and hatch[1] is OPPOSITE_FACE[face] and joins(block, hatch[0]):
                graph.join(block, hatch[0])
    for cell, (index, front) in hatch_at.items():
        facing = hatch_at.get(_step(cell, front))
        if facing is not None and facing[1] is OPPOSITE_FACE[front] and joins(index, facing[0]):
            graph.join(index, facing[0])
    return graph


# --- 6. pieces, sources and pathing ------------------------------------------------------------------


def _components(graph: _Graph) -> list[list[int]]:
    seen: set[int] = set()
    pieces: list[list[int]] = []
    for start in range(len(graph.nodes)):
        if start in seen:
            continue
        piece: list[int] = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            piece.append(node)
            for other in graph.nodes[node].links:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        pieces.append(sorted(piece))
    return pieces


def _check_channels(problem: InputIR, graph: _Graph, out: list[Violation]) -> None:
    """Rules 5 to 8 over the graph AE builds."""
    specs = {n.id: n for n in problem.me.networks}
    pieces = _components(graph)
    pieces_of: dict[str, list[list[int]]] = defaultdict(list)
    for piece in pieces:
        networks = sorted({graph.nodes[i].network for i in piece})
        if len(networks) > 1:
            where = next(graph.nodes[i].label for i in piece)
            out.append(
                Violation(
                    ViolationCode.ME_NETWORK_MERGE,
                    f"ME networks {networks} touch (near {where}), so AE joins them into one "
                    f"network",
                )
            )
            continue
        pieces_of[networks[0]].append(piece)

    for network_id, network_pieces in sorted(pieces_of.items()):
        spec = specs.get(network_id)
        if spec is None:
            continue  # reported as an unknown network
        attached = spec.mode is MEMode.ATTACHED
        if len(network_pieces) > 1 and not (
            attached and all(any(graph.nodes[i].root for i in p) for p in network_pieces)
        ):
            out.append(
                Violation(
                    ViolationCode.ME_NETWORK_SPLIT,
                    f"ME network {network_id!r} is built in {len(network_pieces)} pieces that do "
                    f"not touch, so AE runs them apart",
                )
            )
        devices = sum(graph.nodes[i].channel for p in network_pieces for i in p)
        if attached and devices > spec.me_channel_budget:
            out.append(
                Violation(
                    ViolationCode.ME_ATTACH_BUDGET,
                    f"ME network {network_id!r} needs {devices} channels of the main network, "
                    f"over its budget of {spec.me_channel_budget}",
                )
            )
        for piece in network_pieces:
            _check_piece(graph, piece, network_id, attached, out)


def _check_piece(
    graph: _Graph,
    piece: list[int],
    network_id: str,
    attached: bool,
    out: list[Violation],
) -> None:
    """One connected piece of one network: where its channels come from, and whether they fit."""
    nodes = graph.nodes
    controllers = [i for i in piece if nodes[i].controller]
    devices = [i for i in piece if nodes[i].channel]
    if not attached and not controllers:
        if len(devices) > ADHOC_MAX_DEVICES:
            out.append(
                Violation(
                    ViolationCode.ME_ADHOC_OVERFLOW,
                    f"ME network {network_id!r} has no controller and {len(devices)} channel "
                    f"devices; with more than {ADHOC_MAX_DEVICES} AE gives none of them a channel",
                )
            )
        return  # ad hoc: no topology check at all (spike 2.6)
    if controllers and (attached or not _one_cluster(graph, controllers)):
        why = (
            "is attached to the main network, whose own controller it would conflict with"
            if attached
            else "has controllers that do not form one valid cluster"
        )
        out.append(
            Violation(
                ViolationCode.ME_CONTROLLER_CONFLICT,
                f"ME network {network_id!r} {why}, so AE gives every device 0 channels (spike 2.2)",
            )
        )
        return
    roots = [i for i in piece if nodes[i].root] if attached else []
    labels, parents = _pathing(graph, piece, roots, controllers)
    starved = [i for i in devices if i not in labels]
    for i in starved:
        out.append(
            Violation(
                ViolationCode.ME_CHANNEL_STARVED,
                f"{nodes[i].label} on ME network {network_id!r} has no path to a channel source",
            )
        )
    below = _subtree_devices(graph, labels, parents)
    for i in sorted(labels):
        node = nodes[i]
        load = len(below.get(i, ()))
        if load > node.capacity:
            ambiguous = any(len(parents.get(j, ())) > 1 for j in below.get(i, ()))
            out.append(
                Violation(
                    ViolationCode.ME_CABLE_OVERLOAD,
                    f"{node.label} on ME network {network_id!r} would carry {load} channel "
                    f"devices, over its {node.capacity}: at least {load - node.capacity} of them "
                    f"get no channel"
                    + (
                        " (which ones depends on the order AE visits blocks in)"
                        if ambiguous
                        else ""
                    ),
                )
            )


def _one_cluster(graph: _Graph, controllers: list[int]) -> bool:
    """Whether the controllers are one cluster joined by direct adjacency, within 7 blocks a side,
    none with controller pairs along two axes (spike 2.2)."""
    group = set(controllers)
    seen = {controllers[0]}
    stack = [controllers[0]]
    while stack:
        node = stack.pop()
        for other in graph.nodes[node].links:
            if other in group and other not in seen:
                seen.add(other)
                stack.append(other)
    if seen != group:
        return False
    cells: list[Cell] = [c for i in controllers if (c := graph.nodes[i].cell) is not None]
    if cells and any(max(c[a] for c in cells) - min(c[a] for c in cells) >= 7 for a in range(3)):
        return False
    at = {graph.nodes[i].cell: i for i in controllers}
    for cell in at:
        if cell is None:
            continue
        axes = 0
        for plus, minus in (
            (Facing.EAST, Facing.WEST),
            (Facing.UP, Facing.DOWN),
            (Facing.SOUTH, Facing.NORTH),
        ):
            if _step(cell, plus) in at and _step(cell, minus) in at:
                axes += 1
        if axes >= 2:
            return False
    return True


#: An item of AE's BFS: a node (``("n", index)``) or a connection (``("c", low, high)``).
_Item = tuple[object, ...]
#: A label: the counts of path items in queues 2, 1 and 0, compared in that order (spike 2.5).
_Label = tuple[int, int, int]


def _queue(label: _Label) -> int:
    return 2 if label[0] else 1 if label[1] else 0


def _extend(label: _Label, cls: int) -> _Label:
    """The label of an item of class ``cls`` reached from one labelled ``label``: it enters queue
    ``max(cls, the parent's queue)``, one more item there."""
    queue = max(cls, _queue(label))
    c2, c1, c0 = label
    return (c2 + (queue == 2), c1 + (queue == 1), c0 + (queue == 0))


def _pathing(
    graph: _Graph, piece: list[int], roots: list[int], controllers: list[int]
) -> tuple[dict[int, _Label], dict[int, set[int]]]:
    """AE's BFS order as labels, and every node's possible parents (spike 2.1, 2.5).

    Returns ``node -> its label`` for every node a channel source reaches, and ``node -> the nodes
    that may be its parent`` (a node's parent is the far end of an incident connection with the
    least label; ties are all kept). Controllers are never entered: each connection from one to a
    non-controller is a root connection, as is the main network's connection into an attach stub.
    """
    nodes = graph.nodes
    in_piece = set(piece)
    group = set(controllers)
    best: dict[_Item, _Label] = {}
    heap: list[tuple[_Label, _Item]] = []

    def offer(item: _Item, label: _Label) -> None:
        if item not in best or label < best[item]:
            best[item] = label
            heapq.heappush(heap, (label, item))

    for root in roots:
        offer(("c", -1, root), (0, 0, 1))  # the main network's connection into the stub
    for controller in controllers:
        for other in nodes[controller].links:
            if other in in_piece and other not in group:
                offer(("c", controller, other), (0, 0, 1))
    done: set[_Item] = set()
    while heap:
        label, item = heapq.heappop(heap)
        if item in done or best.get(item) != label:
            continue
        done.add(item)
        if item[0] == "c":
            low, high = item[1], item[2]
            for end in (low, high):
                if isinstance(end, int) and end >= 0 and end not in group:
                    offer(("n", end), _extend(label, nodes[end].cls))
        else:
            node = item[1]
            assert isinstance(node, int)
            for other in nodes[node].links:
                if other in group:
                    continue
                low, high = sorted((node, other))
                offer(("c", low, high), _extend(label, 0))

    labels: dict[int, _Label] = {
        item[1]: label
        for item, label in best.items()
        if item[0] == "n" and isinstance(item[1], int)
    }
    parents: dict[int, set[int]] = {}
    for node, label in labels.items():
        possible: set[int] = set()
        for other in nodes[node].links:
            if other in group or other not in labels:
                continue
            low, high = sorted((node, other))
            connection = best.get(("c", low, high))
            # The connection is this node's parent when it reaches the node at its label, and the
            # other end is its parent when it reached the connection first.
            if (
                connection is not None
                and _extend(connection, nodes[node].cls) == label
                and _extend(labels[other], 0) == connection
            ):
                possible.add(other)
        parents[node] = possible
    return labels, parents


def _subtree_devices(
    graph: _Graph, labels: Mapping[int, _Label], parents: Mapping[int, set[int]]
) -> dict[int, set[int]]:
    """``node -> every channel device at or below it`` through possible-parent links."""
    children: dict[int, set[int]] = defaultdict(set)
    for node, possible in parents.items():
        for parent in possible:
            children[parent].add(node)
    below: dict[int, set[int]] = {}
    for node in sorted(labels, key=lambda n: labels[n], reverse=True):  # leaves first
        mine = {node} if graph.nodes[node].channel else set()
        for child in children.get(node, ()):
            mine |= below.get(child, set())
        below[node] = mine
    return below
