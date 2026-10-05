"""Net-topology helpers shared across the solver lanes.

Small, pure lookups over the input IR that were hand-rolled identically in the placement, router,
solver, and system_io lanes: the port-direction map, the resource a port's id names, a net's
source/sink split, and the one-placement-per-machine index. Kept here (not re-exported from ``ir``) so the lanes
deep-import them the way they deep-import ``ir.geometry`` - helpers stay off the contract's public
surface (see ``ir/__init__``).

Reading input port directions is *data plumbing*, not rule computation, so the validator may share
these too without giving up its independence (docs/ARCHITECTURE.md #4): it re-derives the RULES on
its own arithmetic, it does not re-invent how to read the same input data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from .enums import Commodity, Facing, IODirection
from .input_ir import InputIR, Machine, MachineFaceRef, Net, Port
from .output import Placement

#: The faces of a single block that can carry a connection: every face but the front, which carries
#: no I/O (docs/DOMAIN.md). Kept beside :func:`connection_counts` because the two are the one measure
#: the adapter's Item Filter merge (a single block using all of them) and
#: ``placement.feasibility.single_block_shortfalls`` (one needing more) both read.
SINGLE_BLOCK_IO_FACES = len(Facing) - 1


def connection_counts(
    nets: Iterable[Net], rides_me: Callable[[Net], bool] | None = None
) -> dict[str, int]:
    """``machine_id -> how many connections it carries``: one per net endpoint on it.

    A net left to ME docks nothing, so it counts nothing: ``rides_me`` says which, a problem's
    ``InputIR.rides_me`` where there is one, else each net's own ``Net.rides_me``. Each endpoint is
    one connection, and on a single block one face of its own, whether a pipe or cable docks on it or
    an auto-output spends it touching its sink. It takes nets rather than a whole ``InputIR`` so the
    adapter can ask it of the nets it is still building, and decide which machines to merge with the
    same count ``placement.feasibility.single_block_shortfalls`` later reports from.
    """
    counts: dict[str, int] = {}
    skip = rides_me if rides_me is not None else _net_rides_me
    for net in nets:
        if skip(net):
            continue
        for endpoint in net.endpoints:
            counts[endpoint.machine_id] = counts.get(endpoint.machine_id, 0) + 1
    return counts


def _net_rides_me(net: Net) -> bool:
    return net.rides_me


def machines_with_me_outputs(problem: InputIR, commodity: Commodity) -> frozenset[str]:
    """The machines with an OUTPUT port of ``commodity`` on a net that rides ME.

    Asked of a tower filling its fluid outputs by layer, whose output hatches the layout does not
    draw while an output rides ME: a stage that would place or check one per layer abstains there.
    """
    port_dir = port_direction_map(problem)
    return frozenset(
        ep.machine_id
        for net in problem.nets
        if net.commodity is commodity and problem.rides_me(net)
        for ep in net.endpoints
        if port_dir.get((ep.machine_id, ep.port_id)) is IODirection.OUTPUT
    )


def me_device_ports(machine: Machine) -> list[tuple[str, str]]:
    """``(port, network)`` per ME device of ``machine`` that docks on the line: the first port each
    endpoint serves, the one its face is chosen by (a Dual Interface takes its second port along on
    the same face). Each takes a face and a cell of its own, like a pipe's terminal. A link's
    storage bus serves no port and faces out of the build, so it docks nothing here (#335)."""
    return [(e.ports[0], e.network) for e in machine.me_endpoints if e.ports]


def me_network_machines(problem: InputIR) -> dict[str, list[str]]:
    """``ME network -> the machines on it``, in problem order: every machine with an ME endpoint on
    it, and its infrastructure (stubs, links, controller, acceptor). What the placement pulls
    together, the way a net's endpoints are pulled together (#335)."""
    members: dict[str, list[str]] = {}
    for machine in problem.machines:
        networks = [e.network for e in machine.me_endpoints]
        if machine.me_network is not None:
            networks.append(machine.me_network)
        for network in dict.fromkeys(networks):
            members.setdefault(network, []).append(machine.id)
    return members


def port_resource(port: Port) -> str:
    """The resource a non-power port carries, recovered from its ``{direction}:{resource}`` id."""
    prefix = f"{port.direction.value}:"
    return port.id[len(prefix) :] if port.id.startswith(prefix) else port.id


def port_direction_map(problem: InputIR) -> dict[tuple[str, str], IODirection]:
    """``(machine_id, port_id) -> IODirection`` for every port in ``problem``."""
    return {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}


def net_sources_sinks(
    net: Net, port_dir: dict[tuple[str, str], IODirection]
) -> tuple[list[MachineFaceRef], list[MachineFaceRef]]:
    """Split ``net``'s endpoints into ``(sources, sinks)`` by port direction (OUTPUT, INPUT).

    ``port_dir`` is a :func:`port_direction_map`, passed in so a caller in a loop builds it once.
    Endpoint order within each list is preserved, and an endpoint whose direction is unknown
    (absent from ``port_dir``) falls into neither - exactly as the hand-rolled comprehensions did.
    Returns the endpoints themselves; a caller wanting machine ids maps ``e.machine_id`` over them.
    """
    sources: list[MachineFaceRef] = []
    sinks: list[MachineFaceRef] = []
    for endpoint in net.endpoints:
        direction = port_dir.get((endpoint.machine_id, endpoint.port_id))
        if direction is IODirection.OUTPUT:
            sources.append(endpoint)
        elif direction is IODirection.INPUT:
            sinks.append(endpoint)
    return sources, sinks


def placement_index(placements: Sequence[Placement]) -> dict[str, Placement]:
    """``machine_id -> its placement``, keeping the first when a machine id repeats (the
    one-placement-per-machine convention the routers and validator share)."""
    by_machine: dict[str, Placement] = {}
    for placement in placements:
        by_machine.setdefault(placement.machine_id, placement)
    return by_machine
