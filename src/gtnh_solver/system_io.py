"""system_io - the boundary of a solved line: what to feed in, what comes out, total power.

The 3D previewer answers "what does this line consume and produce at its edge, and how much power
does it draw". Deriving that inside a renderer is how two surfaces come to disagree, so it lives
here instead: the single source, pure over the ``InputIR`` + the ``LayoutResult``; a renderer only
formats it.

- **inputs**: a boundary storage (Super Chest/Tank) that *only* sources the line - nothing feeds it,
  so the builder fills it - or a Crop Manager (``Machine.outside_front``, #282), which puts out
  what its field outside the build yields. Each carries the resource + its typed rate.
- **outputs**: the product the line makes - normally a boundary storage that only *sinks* (a
  synthesized collection buffer, #16), or, as a fallback, a machine OUTPUT port no net consumes.
- **power**: the summed ``eut`` the placed machines draw, plus the amperage each synthetic source
  must supply - each machine's *fractional* load at its delivered voltage (cable loss over distance
  included), summed over the machines sharing that source and rounded up to whole amps only then
  (docs/DOMAIN.md - a shared-amperage net's aggregate draw is what the source must supply; machines
  buffer packets, so per-machine whole amps would overstate it). Reported per source *and* per
  tier, which differ once a tier needs more than one source.
- **me**: per ME network (#335), what its storage must supply and takes in (a net on ME no machine
  of the line produces is drawn from storage, one no machine consumes is stored there), how many
  channel devices it has, and how many channels of the player's main network it spends: an
  attached network one per device, a link subnet one per link, a chests subnet none. For an
  attached or link network that storage is the player's main network, which is outside the build,
  so its stock is reported, never checked. And what it draws (#336), from the cable the layout
  lays (:func:`laid_me_ae_per_tick`, spike 6): what an attached network adds to the main network,
  what a quartz fiber must carry to an external subnet, or what its Energy Acceptor takes from the
  line's EU supply. The same figures go into the layout itself (``LayoutMetrics.me``, through
  :func:`me_network_metrics`), for a reader that sees only the layout.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from gtnh_solver.dataset import UnknownTierError, UnpowerableError, amp_load, whole_amps
from gtnh_solver.dataset.me import (
    ADHOC_MAX_DEVICES,
    CONTROLLER_IDLE_AE,
    DEFAULT_GRID_BUFFER_AE,
    DEFAULT_IDLE_AE,
    GT_ME_HATCHES,
    adhoc_channel_load,
    ae_to_eu,
    endpoint_moves,
    flush_ae,
    network_ae_per_tick,
    tree_channel_load,
)
from gtnh_solver.ir import (
    AEColor,
    Commodity,
    InputIR,
    IODirection,
    LayoutResult,
    MEFlowMetrics,
    MEMode,
    MENetworkLayout,
    MENetworkMetrics,
    MENetworkSpec,
    MEPower,
    MERole,
    Net,
    Port,
    Route,
    Segment,
)
from gtnh_solver.ir.nets import port_direction_map

#: Per-commodity rate unit stem, no time suffix. The previewer appends ``/t`` or ``/s`` for its
#: tick-vs-second toggle; a rate itself is per tick (typed, docs/IR.md).
RATE_STEM = {Commodity.ITEM: "items", Commodity.FLUID: "mB", Commodity.POWER: "EU"}


@dataclass(frozen=True)
class BoundaryFlow:
    """One resource crossing the line's edge at a specific machine (an input to load or a product
    to collect). ``rate`` is the sourcing net's typed throughput, or ``None`` when no net gives one
    (a dangling output, or an unwired boundary storage).

    ``resource`` is what it carries as one label (:func:`net_resource`: a merged run's several,
    comma separated) and ``resources`` the same ids one by one, which is what a consumer that looks
    each one up must read: a fluid id can contain a comma itself (``1,3dimethylbenzene``)."""

    machine_id: str
    machine_type: str
    cell: tuple[int, int, int]
    resource: str
    resources: tuple[str, ...]
    commodity: Commodity
    rate: float | None


@dataclass(frozen=True)
class MEFlow:
    """One resource an ME network's storage supplies or takes in: what it is, as one label and id
    by id (as :class:`BoundaryFlow` has them), its kind, and the net's typed rate."""

    resource: str
    resources: tuple[str, ...]
    commodity: Commodity
    rate: float


@dataclass(frozen=True)
class MENetworkIO:
    """What one ME network asks of the player (module docstring).

    ``supplies`` must be in the network's storage for the line to run, and ``absorbs`` lands there;
    for an attached or link network that storage is the player's main network. ``devices`` are its
    channel devices, ``main_channels`` the main network's channels it spends, and
    ``channel_budget`` what its devices may spend (``ir.MENetworkMetrics``).

    ``ae_per_tick`` and ``eu_per_tick`` are what it draws, from the cable laid
    (:func:`laid_me_ae_per_tick`). ``external_store_ae`` is what the network powering it from
    outside must keep stored for one flush of its GT ME output buses and hatches (spike 5.3, 6.3):
    the largest flush, when that is more than AE's 1,000 AE default buffer and nothing of its own
    stores energy (an external subnet with no controller), else 0; an attached network draws on the
    main network's controller, and an acceptor network on its acceptor, which the validator holds.
    On an acceptor network, ``acceptor_eu_per_tick`` is what its Energy Acceptor is rated for, an
    upper bound set before the cable was laid that the validator holds to be enough,
    ``acceptor_source`` the power source whose cable reaches it, and ``acceptor_source_amps`` that
    source's whole output, which the acceptor's cable is sized for: the most the builder may feed
    it, since the acceptor takes every amp offered.
    """

    network: str
    mode: MEMode
    supplies: tuple[MEFlow, ...]
    absorbs: tuple[MEFlow, ...]
    devices: int
    main_channels: int
    colour: AEColor = AEColor.FLUIX
    power: MEPower = MEPower.EXTERNAL
    channel_budget: int | None = None
    ae_per_tick: float = 0.0
    eu_per_tick: float = 0.0
    external_store_ae: float = 0.0
    acceptor_eu_per_tick: float | None = None
    acceptor_source: str | None = None
    acceptor_source_amps: int | None = None


@dataclass(frozen=True)
class SystemIO:
    """The whole boundary: inputs to load, outputs to collect, the total EU/t draw, and the summed
    **amperage** to feed (what an external source must supply, docs/DOMAIN.md - a tier already
    implies its voltage, so amps is the useful number). Each machine's fractional load (``eut`` over
    its delivered voltage, so cable loss over distance is included) is summed and only the total
    rounds up to whole amps - machines buffer packets, so rounding per machine would overstate what
    the builder must feed (confirmed in game).

    Amps come two ways, because a tier can hold several sources once its load outgrows one cable
    run (adapter.power): ``power_amps_by_source`` is what to feed **each source block** and is the
    one to render per source, while ``power_amps_by_tier`` is the tier-wide total across all of its
    sources, for a summary. They agree exactly when a tier has one source; when it has several the
    tier figure is the lower of the two, since it rounds once rather than once per source.

    The per-source figure was the text build guide's wiring spec and has had no renderer since that
    guide was retired (#203): the previewer's power panel shows the tier-wide one. It stays because
    it is the number a builder needs at each source of a split tier, and it is not derivable from
    the tier figure."""

    inputs: list[BoundaryFlow]
    outputs: list[BoundaryFlow]
    power_total: float
    power_amps_by_tier: dict[str, int]
    power_amps_by_source: dict[str, int]
    #: One entry per ME network the problem uses, in problem order (module docstring).
    me: tuple[MENetworkIO, ...] = ()


def is_boundary_storage(machine_type: str) -> bool:
    """A Super Chest / Super Tank boundary buffer (the previewer's storage role, docs/DOMAIN.md)."""
    return machine_type.startswith("Super ")


def port_resource(port: Port) -> str:
    """The resource a non-power port carries, recovered from its ``{direction}:{resource}`` id."""
    prefix = f"{port.direction.value}:"
    return port.id[len(prefix) :] if port.id.startswith(prefix) else port.id


def net_resource(net: Net) -> str | None:
    """What ``net`` carries as one label: its fluid or item, or ``None`` for power.

    A merged item run (``Net.items``, #249) carries several, which it names comma separated in the
    order the net lists them ("a, b, c"), so a hover or a panel row shows everything in the pipe.
    Every other net names its one resource exactly as before, verbatim as the plan spells it.
    """
    return ", ".join(net.resources) or None


def resource_label(resource: str, names: Mapping[str, str]) -> str:
    """How a person reads ``resource``: its display name with the raw id beside it,
    ``"Toluene (liquid_toluene)"``, or the id alone when the plan names it nothing (#296).

    The name is ``InputIR.resource_names``, the exporter's, never one authored here. The id stays in
    the label because it is what a builder searches NEI for, and a name alone can be ambiguous.
    """
    name = names.get(resource, "")
    return f"{name} ({resource})" if name and name != resource else resource


def net_label(net: Net, names: Mapping[str, str]) -> str | None:
    """:func:`net_resource` with each resource labelled by :func:`resource_label`, or ``None`` for
    power. A merged item run's several read in the net's own order, comma separated."""
    return ", ".join(resource_label(resource, names) for resource in net.resources) or None


def system_io(problem: InputIR, layout: LayoutResult) -> SystemIO:
    """Derive the boundary I/O + summed power of ``layout`` (only machines it actually placed)."""
    port_dir = port_direction_map(problem)
    coord_of = {pl.machine_id: pl.cell for pl in layout.placements}

    # The net each output port sources / each input port sinks (keyed by machine+port). A source's
    # presence means the port is consumed (not a dangling output); a sink's carries the rate feeding
    # a collection buffer.
    net_by_source: dict[tuple[str, str], Net] = {}
    net_by_sink: dict[tuple[str, str], Net] = {}
    for net in problem.nets:
        for ep in net.endpoints:
            direction = port_dir.get((ep.machine_id, ep.port_id))
            if direction is IODirection.OUTPUT:
                net_by_source[(ep.machine_id, ep.port_id)] = net
            elif direction is IODirection.INPUT:
                net_by_sink[(ep.machine_id, ep.port_id)] = net

    inputs: list[BoundaryFlow] = []
    outputs: list[BoundaryFlow] = []
    for machine in problem.machines:
        cell = coord_of.get(machine.id)
        if cell is None:  # only describe machines the layout actually placed
            continue
        cell_t = cell.as_tuple()
        out_ports = [p for p in machine.faces.ports if p.direction is IODirection.OUTPUT]
        dirs = {p.direction for p in machine.faces.ports}
        only_sources = IODirection.INPUT not in dirs and IODirection.OUTPUT in dirs

        # A Crop Manager is an input too: what its field outside the build yields (#282).
        if (is_boundary_storage(machine.type) or machine.outside_front) and only_sources:
            for port in out_ports:
                src = net_by_source.get((machine.id, port.id))
                carried = _carried(src, port)
                rate = src.throughput if src else None
                inputs.append(
                    BoundaryFlow(
                        machine.id,
                        machine.type,
                        cell_t,
                        ", ".join(carried),
                        carried,
                        port.commodity,
                        rate,
                    )
                )
            continue

        in_ports = [p for p in machine.faces.ports if p.direction is IODirection.INPUT]
        only_sinks = IODirection.OUTPUT not in dirs and IODirection.INPUT in dirs
        if is_boundary_storage(machine.type) and only_sinks:
            # a collection buffer (#16): the product it gathers is a system output, its rate the net
            for port in in_ports:
                if port.commodity is Commodity.POWER:
                    continue
                sink = net_by_sink.get((machine.id, port.id))
                carried = _carried(sink, port)
                rate = sink.throughput if sink else port.rate
                outputs.append(
                    BoundaryFlow(
                        machine.id,
                        machine.type,
                        cell_t,
                        ", ".join(carried),
                        carried,
                        port.commodity,
                        rate,
                    )
                )
            continue

        for port in out_ports:
            if port.commodity is Commodity.POWER:
                continue  # a power output is a source, not a product to collect
            if (machine.id, port.id) in net_by_source:
                continue  # consumed by a net or auto-output (e.g. wired to a collection buffer)
            resource = port_resource(port)
            outputs.append(
                BoundaryFlow(
                    machine.id,
                    machine.type,
                    cell_t,
                    resource,
                    (resource,),
                    port.commodity,
                    port.rate,
                )
            )

    # Cable-block distance from the source to each powered machine (its depth in the routed power
    # tree), so each load is sized at the loss-reduced *delivered* voltage the builder must
    # actually feed - not the lossless ideal. Loads stay fractional per machine and round up to
    # whole amps only per tier (dataset.amp_load / whole_amps). Machines with no cable (ME power,
    # or an unrouted net) fall back to distance 0. The validator re-derives this distance
    # independently for its amperage check.
    power_distance = _power_distances(layout.routes, port_dir)
    source_of = _power_source_of(problem, port_dir)
    power_total = 0.0
    load_by_tier: dict[str, float] = {}
    load_by_source: dict[str, float] = {}
    for machine in problem.machines:
        if machine.eut <= 0 or machine.id not in coord_of:
            continue  # unpowered blocks / sources draw nothing; describe only placed machines
        tier = machine.voltage_tier
        power_total += machine.eut
        # Per *connection*, not per machine: a machine's energy hatches can sit on different nets
        # and so be fed by different sources (adapter.power), each at its own cable distance. A
        # machine with no power port at all (a hand-built problem) keeps the whole-draw reading.
        feeds: list[tuple[str | None, float]] = [
            (p.id, machine.port_eut(p.id)) for p in machine.power_input_ports
        ] or [(None, machine.eut)]
        for port_id, eut in feeds:
            key = (machine.id, port_id)
            try:
                load = amp_load(eut, tier, distance=power_distance.get(key, 0))
            except UnknownTierError, UnpowerableError:
                continue  # an off-ladder tier or a run loss has killed: nothing sizeable to report
            load_by_tier[tier] = load_by_tier.get(tier, 0.0) + load
            source = source_of.get(key)
            if source is not None:
                load_by_source[source] = load_by_source.get(source, 0.0) + load
    power_amps_by_tier = {tier: whole_amps(load) for tier, load in load_by_tier.items()}
    power_amps_by_source = {sid: whole_amps(load) for sid, load in load_by_source.items()}

    return SystemIO(
        inputs=inputs,
        outputs=outputs,
        power_total=power_total,
        power_amps_by_tier=power_amps_by_tier,
        power_amps_by_source=power_amps_by_source,
        me=me_network_io(problem, layout.me_networks, power_amps_by_source),
    )


def me_network_io(
    problem: InputIR,
    networks: Sequence[MENetworkLayout],
    amps_by_source: Mapping[str, int] = MappingProxyType({}),
) -> tuple[MENetworkIO, ...]:
    """Each ME network's ask of the player (module docstring), its power from ``networks``, the
    networks a layout lays, and ``amps_by_source`` what each power source is fed
    (``SystemIO.power_amps_by_source``). Several nets moving one resource the same way (water fed
    to three machines) are one flow, their rates summed."""
    port_dir = port_direction_map(problem)
    source_of = _power_source_of(problem, port_dir)
    laid = {n.id: n for n in networks}
    out: list[MENetworkIO] = []
    for spec in problem.me.networks:
        supplies: dict[tuple[tuple[str, ...], Commodity], MEFlow] = {}
        absorbs: dict[tuple[tuple[str, ...], Commodity], MEFlow] = {}
        for net in problem.nets:
            if net.me_network != spec.id:
                continue
            directions = {port_dir.get((e.machine_id, e.port_id)) for e in net.endpoints}
            if IODirection.OUTPUT not in directions:
                flows = supplies  # nothing in the line makes it: the network's storage does
            elif IODirection.INPUT not in directions:
                flows = absorbs  # nothing in the line takes it: the network's storage does
            else:
                continue
            resource = net_resource(net) or net.id
            key = (net.resources or (resource,), net.commodity)
            rate = net.throughput + (flows[key].rate if key in flows else 0.0)
            flows[key] = MEFlow(resource, key[0], net.commodity, rate)
        devices = sum(1 for m in problem.machines for e in m.me_endpoints if e.network == spec.id)
        links = sum(
            1 for m in problem.machines if m.me_role is MERole.LINK and m.me_network == spec.id
        )
        acceptors = [
            m for m in problem.machines if m.me_role is MERole.ACCEPTOR and m.me_network == spec.id
        ]
        built = laid.get(spec.id)
        ae = laid_me_ae_per_tick(problem, spec, built)
        source = next(
            (
                source_of[(m.id, p.id)]
                for m in acceptors
                for p in m.power_input_ports
                if (m.id, p.id) in source_of
            ),
            None,
        )
        out.append(
            MENetworkIO(
                network=spec.id,
                mode=spec.mode,
                supplies=tuple(supplies.values()),
                absorbs=tuple(absorbs.values()),
                devices=devices,
                main_channels=devices if spec.mode is MEMode.ATTACHED else links,
                colour=problem.me.colour(spec.id),
                power=spec.power,
                channel_budget=_channel_budget(problem, spec),
                ae_per_tick=ae,
                eu_per_tick=ae_to_eu(ae),
                external_store_ae=_external_store(problem, spec, built),
                acceptor_eu_per_tick=(
                    sum(m.eut for m in acceptors) if spec.power is MEPower.ACCEPTOR else None
                ),
                acceptor_source=source,
                acceptor_source_amps=amps_by_source.get(source) if source is not None else None,
            )
        )
    return tuple(out)


def me_network_metrics(problem: InputIR, layout: LayoutResult) -> list[MENetworkMetrics]:
    """Each ME network of ``layout`` as the layout carries it (``LayoutMetrics.me``): the
    :class:`MENetworkIO` :func:`system_io` reports for it."""
    return [
        MENetworkMetrics(
            id=io.network,
            mode=io.mode,
            colour=io.colour,
            power=io.power,
            devices=io.devices,
            channel_budget=io.channel_budget,
            main_channels=io.main_channels,
            ae_per_tick=io.ae_per_tick,
            eu_per_tick=io.eu_per_tick,
            external_store_ae=io.external_store_ae,
            acceptor_eu_per_tick=io.acceptor_eu_per_tick,
            acceptor_source=io.acceptor_source,
            acceptor_source_amps=io.acceptor_source_amps,
            supplies=[_flow_metrics(f) for f in io.supplies],
            absorbs=[_flow_metrics(f) for f in io.absorbs],
        )
        for io in system_io(problem, layout).me
    ]


def _flow_metrics(flow: MEFlow) -> MEFlowMetrics:
    return MEFlowMetrics(resources=flow.resources, commodity=flow.commodity, rate=flow.rate)


def laid_me_ae_per_tick(
    problem: InputIR, spec: MENetworkSpec, laid: MENetworkLayout | None
) -> float:
    """AE/t the ME network ``spec`` draws as ``laid`` builds it (spike 6, ``dataset.me``).

    Its devices' and controllers' idle draws, what each laid device moves (one charge per item, one
    per started 1000 mB), and the channel term over the cable laid: with a controller (a subnet's
    own, or the main network's for an attached network), twice the channels through every node, a
    cable carrying its ``me_channels`` and each device its one; ad hoc, every node (cable, device,
    acceptor) times the channels in use, none once there are more than 8. For an attached network
    this is what it adds to the main network, short of the main network's own cable between its
    controller and the stub, which the layout cannot see. ``laid`` is None for a network the layout
    does not build, which counts no device and no cable.
    """
    cables = laid.cables if laid is not None else []
    devices = laid.devices if laid is not None else []
    machines = {m.id: m for m in problem.machines}
    items = fluid = 0.0
    for device in devices:
        machine = machines.get(device.machine_id)
        if machine is None:
            continue
        for endpoint in machine.me_endpoints:
            if endpoint.id == device.endpoint_id:
                moved, charges = endpoint_moves(endpoint, {p.id: p for p in machine.faces.ports})
                items += moved
                fluid += charges
    roles = [m.me_role for m in problem.machines if m.me_network == spec.id]
    controllers = roles.count(MERole.CONTROLLER)
    if spec.mode is MEMode.SUBNET and not controllers:
        channels = len(devices) if len(devices) <= ADHOC_MAX_DEVICES else 0
        nodes = len(cables) + len(devices) + roles.count(MERole.ACCEPTOR)
        load = adhoc_channel_load(nodes, channels)
    else:
        load = tree_channel_load([*(c.me_channels for c in cables), *(1 for _ in devices)])
    return network_ae_per_tick(
        idle=len(devices) * DEFAULT_IDLE_AE + controllers * CONTROLLER_IDLE_AE,
        channel_load=load,
        items_per_tick=items,
        fluid_operations_per_tick=fluid,
    )


def _external_store(problem: InputIR, spec: MENetworkSpec, laid: MENetworkLayout | None) -> float:
    """What the network powering ``spec`` from outside must keep stored for one flush of its GT
    ME output buses and hatches (``MENetworkIO.external_store_ae``): only an external subnet with
    no controller of its own leans on that store for a flush over AE's default buffer."""
    controlled = any(
        m.me_role is MERole.CONTROLLER and m.me_network == spec.id for m in problem.machines
    )
    if spec.power is not MEPower.EXTERNAL or spec.mode is not MEMode.SUBNET or controlled:
        return 0.0
    devices = laid.devices if laid is not None else []
    largest = max(
        (flush_ae(GT_ME_HATCHES[d.gt_mid]) for d in devices if d.gt_mid in GT_ME_HATCHES),
        default=0.0,
    )
    return largest if largest > DEFAULT_GRID_BUFFER_AE else 0.0


def _channel_budget(problem: InputIR, spec: MENetworkSpec) -> int | None:
    """What a network's devices may spend: an attached one's budget on the main network, an
    ad-hoc subnet's 8, and None for a subnet with a controller, whose cables each carry 32 from
    it."""
    if spec.mode is MEMode.ATTACHED:
        return spec.me_channel_budget
    controlled = any(
        m.me_role is MERole.CONTROLLER and m.me_network == spec.id for m in problem.machines
    )
    return None if controlled else ADHOC_MAX_DEVICES


def _carried(net: Net | None, port: Port) -> tuple[str, ...]:
    """What a boundary storage's ``port`` moves: everything its ``net`` carries, else the one
    resource the port names (an unwired storage, or a net that names nothing)."""
    return (net.resources if net is not None else ()) or (port_resource(port),)


def _power_source_of(
    problem: InputIR, port_dir: dict[tuple[str, str], IODirection]
) -> dict[tuple[str, str | None], str]:
    """Map each powered ``(machine, port)`` to the id of the source feeding it, via its power net.

    Keyed by port, not by machine: a machine's energy hatches can be split across nets, so two
    hatches of one machine may draw from two different sources and neither owns the whole load.
    A tier can hold several nets once its load outgrows one cable run (adapter.power), so the
    source a connection draws from is a property of its **net**, not of its tier. A net without
    exactly one source is skipped: that is a malformed net the validator reports, and guessing an
    attribution here would put a number on a layout that is not certifiable anyway.
    """
    source_of: dict[tuple[str, str | None], str] = {}
    for net in problem.nets:
        if net.commodity is not Commodity.POWER:
            continue
        sources = [
            e.machine_id
            for e in net.endpoints
            if port_dir.get((e.machine_id, e.port_id)) is IODirection.OUTPUT
        ]
        if len(sources) != 1:
            continue
        for endpoint in net.endpoints:
            if port_dir.get((endpoint.machine_id, endpoint.port_id)) is IODirection.INPUT:
                source_of[(endpoint.machine_id, endpoint.port_id)] = sources[0]
    return source_of


def _power_distances(
    routes: list[Route], port_dir: dict[tuple[str, str], IODirection]
) -> dict[tuple[str, str | None], int]:
    """Cable-block distance from the source to each powered ``(machine, port)``, per power route:
    the terminal's hop-depth in the routed cable tree (BFS from the single source terminal). Keyed
    by port because a machine's hatches sit on different cells - and possibly different nets - so
    they are not all the same distance from a source. Connections not on a cable (power on ME, or
    an unrouted net) are absent, so the caller treats them as distance 0. Kept deliberately
    separate from the validator's own rooting (which re-derives the same distance independently,
    the gate's job) - this is only for the boundary summary."""
    distances: dict[tuple[str, str | None], int] = {}
    for r in routes:
        if r.commodity is not Commodity.POWER:
            continue
        sources = [
            t.cell.as_tuple()
            for t in r.terminals
            if port_dir.get((t.machine_id, t.port_id)) is IODirection.OUTPUT
        ]
        if len(sources) != 1:
            continue  # no single source to measure distance from (the validator flags this)
        depth = _cable_depth(r.segments, sources[0])
        if depth is None:
            continue  # not a single tree; the validator flags it, we just skip the summary
        for t in r.terminals:
            if port_dir.get((t.machine_id, t.port_id)) is IODirection.INPUT:
                d = depth.get(t.cell.as_tuple())
                if d is not None:
                    distances[(t.machine_id, t.port_id)] = d
    return distances


def _cable_depth(
    segments: list[Segment], root: tuple[int, int, int]
) -> dict[tuple[int, int, int], int] | None:
    """Hop-depth of each cell from ``root`` over the cable ``segments``, or ``None`` if they are not
    a single tree rooted there (no clean distance to measure)."""
    adj: dict[tuple[int, int, int], set[tuple[int, int, int]]] = defaultdict(set)
    nodes: set[tuple[int, int, int]] = set()
    edges = 0
    for seg in segments:
        a = seg.start.as_tuple()
        b = seg.end.as_tuple()
        adj[a].add(b)
        adj[b].add(a)
        nodes.add(a)
        nodes.add(b)
        edges += 1
    if root not in nodes or edges != len(nodes) - 1:
        return None  # a tree on N nodes has exactly N-1 edges; otherwise a cycle/disconnect
    depth = {root: 0}
    order = [root]
    i = 0
    while i < len(order):
        cur = order[i]
        i += 1
        for nb in adj[cur]:
            if nb not in depth:
                depth[nb] = depth[cur] + 1
                order.append(nb)
    return depth if len(order) == len(nodes) else None
