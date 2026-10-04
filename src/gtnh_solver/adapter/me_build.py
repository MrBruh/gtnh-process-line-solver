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
        v                       subnet: a link per commodity (link storage), a controller above 8
    Machine.me_endpoints, Machine.me_role       (or InfeasiblePlanError / MEPlanError)

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
    MEDeviceChoice,
    MEShortfall,
    me_devices_for,
    single_block_fluid_push_rate,
)
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
    MEMode,
    MENetworkSpec,
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
#: The tier an infrastructure block is listed at: none draws power (an acceptor's arrives in #336).
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
) -> tuple[list[Machine], list[Net]]:
    """``machines`` and ``nets`` with the ME side the choice stamped on ``nets`` needs (module
    docstring). ``storage_ids`` are the boundary storages and output buffers; ``recipe_ticks`` the
    shortest recipe each machine runs, which bounds a single block's fluid push. Raises
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
    return machines + _infrastructure(machines, nets, me), nets


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
            hatches=spec.hatches,
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
    machines: Sequence[Machine], nets: Sequence[Net], me: MEConfig
) -> list[Machine]:
    """Each network's stubs, links and controller (module docstring)."""
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
            out.extend(_stub(spec, i) for i in range(stubs))
            continue
        if spec.storage is MEStorage.LINK:
            for commodity in (Commodity.ITEM, Commodity.FLUID):
                if commodity in carried.get(spec.id, set()):
                    out.append(_link(spec, commodity))
                    count += 1
        if count > ADHOC_MAX_DEVICES:
            out.append(_controller(spec))
    return out


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
