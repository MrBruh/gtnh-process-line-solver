"""What a plan's ME side needs built: each machine's ME devices and each network's infrastructure.

The second half of choosing ME per net (#332 chose which nets ride which network; this, #335, says
what that takes). It runs inside ``core._close``, after the choice is stamped on the nets and before
the power synthesis, and turns the choice into InputIR v9 fields the router builds and the
validator checks (docs/DOMAIN.md, "What a valid ME build is")::

    nets on ME + their networks
        |
        |-- boundary storages   attached, link: dropped (the main network is the storage)
        |                       chests: kept, each read by a storage bus partitioned to it;
        |                       an internal net gets a buffer Super Chest/Tank of its own
        |-- machine ports       a device per port from dataset.me.me_devices_for, cards to its
        |                       rate; a single block's outputs on one network share one interface
        |                       on its auto-output face (a Dual Interface with fluids), unless a
        |                       pipe already holds that face, when import buses pull them
        |-- infrastructure      attached: dense stubs, a channel budget's worth at most;
        |                       subnet: a link per commodity (link storage), a controller above 8;
        v                       acceptor power: an Energy Acceptor, rated for the network's draw
    Machine.me_endpoints, Machine.me_role       (or InfeasiblePlanError / MEPlanError)

**An acceptor network's Energy Acceptor** is a machine drawing EU like any other, so the power
synthesis that runs next gives it a power port and puts it on the shared-amperage tree of the line's
highest tier: an acceptor takes any voltage (spike 6.3), and the highest draws the fewest amps on
the thinnest cable. Only a subnet takes one: an attached network is part of the player's main
network, which their base already powers, and an acceptor there would power the whole base and keep
filling its storage from the line's supply, so that choice is refused (:class:`MEPlanError`).

Its ``eut`` must be known before any cable is laid, so it is an UPPER BOUND on the network's draw
(``dataset.me``, spike 6): its devices' idle draws, a controller's, what they move, and the channel
term of a network whose every device's channel crosses as many cable blocks as the line's region
is wide, high and deep together (its Manhattan diameter, never under ``ESTIMATED_CABLE_HOPS``). The
router lays each device's cable as a shortest path from the cable already laid, so only a detour
the halo forces takes a channel further than that; the validator holds the rating to what the laid
network really draws, so such a layout is caught. Too high a rating only thickens a power cable,
and on the highest tier barely that.

**Ports on ME are never shared with a pipe.** Every net on one machine port must ride the same
network: a port's device takes all of its output, or feeds all of its input, so a port half piped
and half on ME (or on two networks) has no one device to give it, and that choice is refused
(:class:`MEPlanError`) rather than guessed. A machine's *other* ports may be piped freely.

**Which output a single block auto-outputs** (#329, the maintainer's rule): a piped output keeps the
auto-output face, so every output on ME is pulled by an import bus configured to its resource; with
every output on ME, the network carrying the most auto-outputs into an interface on that face (a
Dual Interface when it carries a fluid too, since one takes both, spike 4.6) and the others are
pulled. A fluid faster than GT pushes it (1000 mB a push, spike 4.7) is pulled by a fluid import
bus rather than pushed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence

from gtnh_solver.dataset.me import (
    ADHOC_MAX_DEVICES,
    CONTROLLER_IDLE_AE,
    DEFAULT_IDLE_AE,
    ESTIMATED_CABLE_HOPS,
    MEDeviceChoice,
    MEShortfall,
    ae_to_eu,
    endpoint_moves,
    estimated_channel_load,
    me_devices_for,
    network_ae_per_tick,
    single_block_fluid_push_rate,
)
from gtnh_solver.dataset.voltage import VOLTAGE_BY_TIER
from gtnh_solver.ir import (
    Commodity,
    FaceSpec,
    Infeasibility,
    IODirection,
    Machine,
    MachineFaceRef,
    MECards,
    MEConfig,
    MEDeviceKind,
    MEDeviceSpec,
    MEEndpoint,
    MEHatchPolicy,
    MEMode,
    MENetworkSpec,
    MEPower,
    MERole,
    MEStorage,
    Net,
    Port,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED

from ._errors import InfeasiblePlanError, MEPlanError

#: Channels one attach stub carries: it is a dense cable (spike 1.1).
_STUB_CHANNELS = 32
#: The type each infrastructure block is drawn and exported as, by its role and commodity.
_STUB_TYPE = "ME Dense Smart Cable"
_CONTROLLER_TYPE = "ME Controller"
_LINK_TYPE = {Commodity.ITEM: "ME Storage Bus", Commodity.FLUID: "ME Fluid Storage Bus"}
_LINK_BUS = {
    Commodity.ITEM: MEDeviceKind.STORAGE_BUS,
    Commodity.FLUID: MEDeviceKind.FLUID_STORAGE_BUS,
}
_BUFFER_TYPE = {Commodity.ITEM: "Super Chest", Commodity.FLUID: "Super Tank"}
_ACCEPTOR_TYPE = "ME Energy Acceptor"
#: The tier an infrastructure block is listed at. None of them draws power but an acceptor, which
#: takes the line's highest powered tier and never one below this (:func:`_acceptor_tier`).
_INFRA_TIER = "LV"


def build_me(
    machines: Sequence[Machine],
    nets: Sequence[Net],
    me: MEConfig,
    *,
    storage_ids: Collection[str],
    multiblock_ids: Collection[str],
    line_tier: str,
    recipe_ticks: Mapping[str, float],
    cable_hops: int = 0,
) -> tuple[list[Machine], list[Net]]:
    """``machines`` and ``nets`` with the ME side the choice stamped on ``nets`` needs (module
    docstring). ``storage_ids`` are the boundary storages and output buffers; ``recipe_ticks`` the
    shortest recipe each machine runs, which bounds a single block's fluid push; ``cable_hops``
    the most cable blocks an Energy Acceptor's rating assumes each device's channel crosses, the
    region's Manhattan diameter (never fewer than ``ESTIMATED_CABLE_HOPS``). Raises
    :class:`MEPlanError` for a port shared by a pipe and ME or by two networks, and
    :class:`InfeasiblePlanError` for a port no device keeps up with or a network over its budget."""
    if not any(net.rides_me for net in nets):
        return list(machines), list(nets)
    machines, nets = _storages(list(machines), list(nets), me, storage_ids)
    by_port = _port_networks(nets)
    endpoints: dict[str, list[MEEndpoint]] = defaultdict(list)
    for machine in machines:
        if machine.id in storage_ids or machine.id.startswith(_BUFFER_PREFIX):
            endpoints[machine.id].extend(_storage_endpoints(machine, by_port, me))
            continue
        endpoints[machine.id].extend(
            _machine_endpoints(
                machine,
                by_port,
                me,
                multiblock=machine.id in multiblock_ids,
                line_tier=line_tier,
                recipe_ticks=recipe_ticks.get(machine.id),
            )
        )
    machines = [
        m.model_copy(update={"me_endpoints": tuple(endpoints[m.id])}) if endpoints.get(m.id) else m
        for m in machines
    ]
    hops = max(ESTIMATED_CABLE_HOPS, cable_hops)
    return machines + _infrastructure(machines, nets, me, hops), nets


# --- boundary storages -----------------------------------------------------------------------------

#: The id prefix of a chests subnet's buffer for an internal net.
_BUFFER_PREFIX = "me-buffer:"


def _storages(
    machines: list[Machine], nets: list[Net], me: MEConfig, storage_ids: Collection[str]
) -> tuple[list[Machine], list[Net]]:
    """Drop the boundary storages an attached or link network replaces, and give a chests subnet's
    internal nets a buffer of their own."""
    dropped: set[tuple[str, str]] = set()
    out_nets: list[Net] = []
    buffers: list[Machine] = []
    for net in nets:
        if net.me_network is None:
            out_nets.append(net)
            continue
        spec = me.network(net.me_network)
        storage_ends = [e for e in net.endpoints if e.machine_id in storage_ids]
        if spec.mode is MEMode.ATTACHED or spec.storage is MEStorage.LINK:
            dropped.update((e.machine_id, e.port_id) for e in storage_ends)
            kept = [e for e in net.endpoints if e.machine_id not in storage_ids]
            if kept:
                out_nets.append(net.model_copy(update={"endpoints": kept}))
            continue
        if storage_ends:
            out_nets.append(net)
            continue
        # chests, and nothing on the net holds its resource: a buffer of its own does.
        resource = net.fluid_or_item or net.id
        buffer_id = f"{_BUFFER_PREFIX}{net.id}"
        port = Port(
            id=f"input:{resource}",
            commodity=net.commodity,
            direction=IODirection.INPUT,
            rate=net.throughput,
        )
        buffers.append(
            Machine(
                id=buffer_id,
                type=_BUFFER_TYPE[net.commodity],
                voltage_tier=_INFRA_TIER,
                orientation_options=list(HORIZONTAL_FACINGS_ORDERED),
                faces=FaceSpec(ports=[port]),
            )
        )
        out_nets.append(
            net.model_copy(
                update={
                    "endpoints": [
                        *net.endpoints,
                        MachineFaceRef(machine_id=buffer_id, port_id=port.id),
                    ]
                }
            )
        )
    # A storage port another net still pipes from stays: one tank can feed a machine over ME and
    # another over a pipe, and only the net on ME stops reaching it.
    dropped -= {(e.machine_id, e.port_id) for net in out_nets for e in net.endpoints}
    if dropped:
        machines = [
            m.model_copy(
                update={
                    "faces": FaceSpec(
                        ports=[p for p in m.faces.ports if (m.id, p.id) not in dropped]
                    )
                }
            )
            if any(mid == m.id for mid, _ in dropped)
            else m
            for m in machines
        ]
        machines = [m for m in machines if m.faces.ports or m.id not in storage_ids]
    return machines + buffers, out_nets


def _port_networks(nets: Sequence[Net]) -> dict[tuple[str, str], str]:
    """``(machine, port) -> the network its nets ride``, for every port on ME; refuses a port on two
    networks, or on ME and a pipe at once."""
    seen: dict[tuple[str, str], set[str | None]] = defaultdict(set)
    named: dict[tuple[str, str], list[str]] = defaultdict(list)
    for net in nets:
        if net.commodity is Commodity.POWER:
            continue
        for ref in net.endpoints:
            seen[(ref.machine_id, ref.port_id)].add(net.me_network)
            named[(ref.machine_id, ref.port_id)].append(net.id)
    out: dict[tuple[str, str], str] = {}
    for key, networks in seen.items():
        on = {n for n in networks if n is not None}
        if not on:
            continue
        if len(networks) > 1:
            machine_id, port_id = key
            where = " and ".join(sorted(str(n) if n else "a pipe" for n in networks))
            raise MEPlanError(
                f"port {port_id!r} of {machine_id!r} carries nets {', '.join(named[key])} on "
                f"{where}; one device takes all of a port, so choose the same for every net on it"
            )
        out[key] = next(iter(on))
    return out


def _storage_endpoints(
    machine: Machine, by_port: Mapping[tuple[str, str], str], me: MEConfig
) -> list[MEEndpoint]:
    """A chests subnet's storage bus on a boundary storage or buffer, partitioned to what it holds."""
    out: list[MEEndpoint] = []
    by_network: dict[str, list[Port]] = defaultdict(list)
    for port in machine.faces.ports:
        network = by_port.get((machine.id, port.id))
        if network is not None:
            by_network[network].append(port)
    for network, ports in sorted(by_network.items()):
        commodity = ports[0].commodity
        resources = tuple(sorted({p.id.split(":", 1)[1] for p in ports}))
        out.append(
            MEEndpoint(
                id=f"me:{network}",
                network=network,
                ports=tuple(p.id for p in ports),
                device=MEDeviceSpec(kind=_LINK_BUS[commodity], config=resources),
            )
        )
    return out


# --- a machine's own ports ----------------------------------------------------------------------------


def _machine_endpoints(
    machine: Machine,
    by_port: Mapping[tuple[str, str], str],
    me: MEConfig,
    *,
    multiblock: bool,
    line_tier: str,
    recipe_ticks: float | None,
) -> list[MEEndpoint]:
    """The ME devices one machine's ports on ME need."""
    on_me = [
        (port, by_port[(machine.id, port.id)])
        for port in machine.faces.ports
        if (machine.id, port.id) in by_port
    ]
    if not on_me:
        return []
    out: list[MEEndpoint] = []
    pushed: dict[str, str] = {}  # port id -> the network whose interface it auto-outputs into
    if not multiblock:
        pushed = _auto_output(machine, on_me, recipe_ticks)
        receivers: dict[str, list[Port]] = defaultdict(list)
        for port, network in on_me:
            if port.id in pushed:
                receivers[network].append(port)
        for network, ports in sorted(receivers.items()):
            fluids = any(p.commodity is Commodity.FLUID for p in ports)
            out.append(
                MEEndpoint(
                    id="me:auto-output",
                    network=network,
                    ports=tuple(p.id for p in ports),
                    device=MEDeviceSpec(
                        kind=MEDeviceKind.DUAL_INTERFACE if fluids else MEDeviceKind.INTERFACE
                    ),
                )
            )
    for port, network in on_me:
        if port.id in pushed:
            continue
        spec = me.network(network)
        chosen = me_devices_for(
            port.commodity,
            port.direction,
            port.rate,
            multiblock=multiblock,
            machine_tier=machine.voltage_tier,
            line_tier=line_tier,
            # A GT ME hatch stands in a casing slot, and a multiblock whose structure the dataset
            # lacks has none recorded (no hatch is placed there, as for a pipe), so its devices are
            # AE2 parts facing the machine.
            hatches=spec.hatches if machine.hatch_slots or not multiblock else MEHatchPolicy.NEVER,
            super_speed=spec.super_speed,
        )
        if isinstance(chosen, MEShortfall):
            raise InfeasiblePlanError(
                Infeasibility(
                    constraint="me_device_rate",
                    detail=f"port {port.id!r} of {machine.id!r}: {chosen.detail}",
                    suggested_relaxation=(
                        "keep this net piped, or spread the work over more machines"
                    ),
                )
            )
        out.extend(_endpoints(port, network, chosen))
    return out


def _endpoints(port: Port, network: str, chosen: tuple[MEDeviceChoice, ...]) -> list[MEEndpoint]:
    """The endpoint(s) ``chosen`` devices make for ``port``: one, or two each carrying half."""
    resource = port.id.split(":", 1)[1] if ":" in port.id else port.id
    buses = {
        MEDeviceKind.IMPORT_BUS,
        MEDeviceKind.EXPORT_BUS,
        MEDeviceKind.FLUID_IMPORT_BUS,
        MEDeviceKind.FLUID_EXPORT_BUS,
    }
    out: list[MEEndpoint] = []
    for i, device in enumerate(chosen):
        out.append(
            MEEndpoint(
                id=f"me:{port.id}" + (f"#{i + 1}" if len(chosen) > 1 else ""),
                network=network,
                ports=(port.id,),
                device=MEDeviceSpec(
                    kind=device.kind,
                    gt_mid=device.gt_mid,
                    cards=device.cards,
                    # A bus moves only what it is set to: an unconfigured export bus moves nothing,
                    # and an import bus left open would pull every output, not this one (spike 4.2).
                    config=(resource,) if device.kind in buses else (),
                ),
                hatch_kind=device.hatch_kind,
                share=1.0 / len(chosen),
            )
        )
    return out


def _auto_output(
    machine: Machine, on_me: Sequence[tuple[Port, str]], recipe_ticks: float | None
) -> dict[str, str]:
    """``port id -> network`` for the single block's outputs that auto-output into an interface.

    None while any output of the machine is piped (the pipe keeps the face). Otherwise the network
    carrying the most, by summed rate, takes the face; a fluid output faster than GT pushes it
    (spike 4.7) is left to a fluid import bus.
    """
    outputs = [
        p
        for p in machine.faces.ports
        if p.direction is IODirection.OUTPUT and p.commodity is not Commodity.POWER
    ]
    on_me_outputs = {p.id: n for p, n in on_me if p.direction is IODirection.OUTPUT}
    if not on_me_outputs or any(p.id not in on_me_outputs for p in outputs):
        return {}
    push = single_block_fluid_push_rate(recipe_ticks) if recipe_ticks else None
    rates: dict[str, float] = defaultdict(float)
    for port in outputs:
        rates[on_me_outputs[port.id]] += port.rate or 0.0
    winner = min(rates, key=lambda n: (-rates[n], n))
    return {
        port.id: winner
        for port in outputs
        if on_me_outputs[port.id] == winner
        and not (
            port.commodity is Commodity.FLUID
            and push is not None
            and port.rate is not None
            and port.rate > push
        )
    }


# --- infrastructure ------------------------------------------------------------------------------------


def _infrastructure(
    machines: Sequence[Machine], nets: Sequence[Net], me: MEConfig, hops: int
) -> list[Machine]:
    """Each network's stubs, links, controller and acceptor (module docstring); ``hops`` is the
    cable an acceptor's rating assumes between each device and its channel source."""
    devices: dict[str, int] = defaultdict(int)
    for machine in machines:
        for endpoint in machine.me_endpoints:
            devices[endpoint.network] += 1
    carried: dict[str, set[Commodity]] = defaultdict(set)
    for net in nets:
        if net.me_network is not None:
            carried[net.me_network].add(net.commodity)
    out: list[Machine] = []
    for spec in me.networks:
        count = devices.get(spec.id, 0)
        if count == 0 and spec.id not in carried:
            continue
        blocks = _network_blocks(spec, count, carried.get(spec.id, set()))
        if spec.power is MEPower.ACCEPTOR:
            if spec.mode is MEMode.ATTACHED:
                raise MEPlanError(
                    f"ME network {spec.id!r} is attached to your main network, which your base "
                    f"already powers: an Energy Acceptor there would power your whole base and "
                    f"keep filling its storage from this line's supply. Leave its power external, "
                    f"or make it a subnet to give it an acceptor of its own"
                )
            blocks.append(_acceptor(spec, [*machines, *blocks], hops))
        out.extend(blocks)
    return out


def _network_blocks(
    spec: MENetworkSpec, count: int, carried: Collection[Commodity]
) -> list[Machine]:
    """One network's stubs, links and controller, for the ``count`` devices its machines need."""
    if spec.mode is MEMode.ATTACHED:
        if count > spec.me_channel_budget:
            raise InfeasiblePlanError(
                Infeasibility(
                    constraint="me_channel_budget",
                    detail=(
                        f"ME network {spec.id!r} needs {count} channels of your main network, "
                        f"over the {spec.me_channel_budget} you said it has free"
                    ),
                    suggested_relaxation=(
                        "make it a subnet (its own controller, one channel of your main "
                        "network through a link), raise the budget, or keep some nets piped"
                    ),
                )
            )
        stubs = max(1, math.ceil(count / _STUB_CHANNELS))
        return [_stub(spec, i) for i in range(stubs)]
    out: list[Machine] = []
    if spec.storage is MEStorage.LINK:
        for commodity in (Commodity.ITEM, Commodity.FLUID):
            if commodity in carried:
                out.append(_link(spec, commodity))
                count += 1
    if count > ADHOC_MAX_DEVICES:
        out.append(_controller(spec))
    return out


def _acceptor(spec: MENetworkSpec, machines: Sequence[Machine], hops: int) -> Machine:
    """The Energy Acceptor powering ``spec`` from the line's EU supply, rated for an estimate of the
    network's draw (module docstring), each device's channel crossing ``hops`` cable blocks.
    ``machines`` are every machine of the line, its other infrastructure blocks included: their
    endpoints on ``spec`` are its devices."""
    devices = 0
    items = fluid = 0.0
    for machine in machines:
        ports = {p.id: p for p in machine.faces.ports}
        for endpoint in machine.me_endpoints:
            if endpoint.network != spec.id:
                continue
            devices += 1
            moved, charges = endpoint_moves(endpoint, ports)
            items += moved
            fluid += charges
    controllers = sum(
        1 for m in machines if m.me_role is MERole.CONTROLLER and m.me_network == spec.id
    )
    adhoc = spec.mode is MEMode.SUBNET and not controllers
    ae = network_ae_per_tick(
        idle=devices * DEFAULT_IDLE_AE + controllers * CONTROLLER_IDLE_AE,
        # The acceptor is a node of the grid too, which an ad-hoc channel term counts.
        channel_load=estimated_channel_load(devices, adhoc=adhoc, blocks=1, hops=hops),
        items_per_tick=items,
        fluid_operations_per_tick=fluid,
    )
    return Machine(
        id=f"me-acceptor:{spec.id}",
        type=_ACCEPTOR_TYPE,
        voltage_tier=_acceptor_tier(machines),
        eut=ae_to_eu(ae),
        orientation_options=list(HORIZONTAL_FACINGS_ORDERED),
        me_role=MERole.ACCEPTOR,
        me_network=spec.id,
    )


def _acceptor_tier(machines: Sequence[Machine]) -> str:
    """The tier an Energy Acceptor is supplied at: the highest any powered machine of the line runs
    at. An acceptor takes any voltage (spike 6.3), and the highest tier carries its draw in the
    fewest amps on the thinnest cable the line already lays (a maintainer decision on #336);
    :data:`_INFRA_TIER` for a line with no powered machine above it on the ladder."""
    ladder = list(VOLTAGE_BY_TIER)
    floor = ladder.index(_INFRA_TIER)
    tiers = [
        ladder.index(m.voltage_tier)
        for m in machines
        if m.eut > 0 and m.me_role is None and m.voltage_tier in VOLTAGE_BY_TIER
    ]
    return ladder[max([floor, *tiers])]


def _stub(spec: MENetworkSpec, index: int) -> Machine:
    return Machine(
        id=f"me-attach:{spec.id}:{index + 1}",
        type=_STUB_TYPE,
        voltage_tier=_INFRA_TIER,
        orientation_options=list(HORIZONTAL_FACINGS_ORDERED),
        outside_front=True,
        me_role=MERole.ATTACH,
        me_network=spec.id,
    )


def _link(spec: MENetworkSpec, commodity: Commodity) -> Machine:
    return Machine(
        id=f"me-link:{spec.id}:{commodity.value}",
        type=_LINK_TYPE[commodity],
        voltage_tier=_INFRA_TIER,
        orientation_options=list(HORIZONTAL_FACINGS_ORDERED),
        outside_front=True,
        me_role=MERole.LINK,
        me_network=spec.id,
        me_endpoints=(
            MEEndpoint(
                id="me:storage",
                network=spec.id,
                device=MEDeviceSpec(kind=_LINK_BUS[commodity], cards=MECards()),
            ),
        ),
    )


def _controller(spec: MENetworkSpec) -> Machine:
    return Machine(
        id=f"me-controller:{spec.id}",
        type=_CONTROLLER_TYPE,
        voltage_tier=_INFRA_TIER,
        orientation_options=list(HORIZONTAL_FACINGS_ORDERED),
        me_role=MERole.CONTROLLER,
        me_network=spec.id,
    )
