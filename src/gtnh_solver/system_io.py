"""system_io - the boundary of a solved line: what to feed in, what comes out, total power.

Both the text build guide and the 3D previewer answer the same question - "what does this line
consume and produce at its edge, and how much power does it draw" - so deriving it twice would let
the two surfaces drift. This module is the single source, pure over the ``InputIR`` + the
``LayoutResult``; the renderers only format it.

- **inputs**: a boundary storage (Super Chest/Tank) that *only* sources the line - nothing feeds it,
  so the builder fills it. Each carries the resource + its typed rate.
- **outputs**: the product the line makes - normally a boundary storage that only *sinks* (a
  synthesized collection buffer, #16), or, as a fallback, a machine OUTPUT port no net consumes.
- **power**: the summed ``eut`` the placed machines draw, plus the amperage per voltage tier the
  source must supply - each machine's *fractional* load at its delivered voltage (cable loss over
  distance included), summed per tier and rounded up to whole amps only then (docs/DOMAIN.md - a
  shared-amperage net's aggregate draw is what the source must supply; machines buffer packets,
  so per-machine whole amps would overstate it).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from gtnh_solver.dataset import UnknownTierError, UnpowerableError, amp_load, whole_amps
from gtnh_solver.ir import Commodity, InputIR, IODirection, LayoutResult, Net, Port, Route, Segment

#: Per-commodity rate unit stem, no time suffix. The previewer appends ``/t`` or ``/s`` for its
#: tick-vs-second toggle; the text guide uses ``RATE_UNIT`` below.
RATE_STEM = {Commodity.ITEM: "items", Commodity.FLUID: "mB", Commodity.POWER: "EU"}
#: Per-tick throughput unit (typed, docs/IR.md). The canonical map the text guide renders with.
RATE_UNIT = {commodity: f"{stem}/t" for commodity, stem in RATE_STEM.items()}


@dataclass(frozen=True)
class BoundaryFlow:
    """One resource crossing the line's edge at a specific machine (an input to load or a product
    to collect). ``rate`` is the sourcing net's typed throughput, or ``None`` when no net gives one
    (a dangling output, or an unwired boundary storage)."""

    machine_id: str
    machine_type: str
    cell: tuple[int, int, int]
    resource: str
    commodity: Commodity
    rate: float | None


@dataclass(frozen=True)
class SystemIO:
    """The whole boundary: inputs to load, outputs to collect, the total EU/t draw, and the summed
    **amperage** per voltage tier (what an external source must supply, docs/DOMAIN.md - the tier
    already implies the voltage, so amps is the useful per-tier number). Each machine's fractional
    load (``eut`` over its delivered voltage, so cable loss over distance is included) is summed
    per tier and only the total rounds up to whole amps - machines buffer packets, so rounding per
    machine would overstate what the builder must feed (confirmed in game)."""

    inputs: list[BoundaryFlow]
    outputs: list[BoundaryFlow]
    power_total: float
    power_amps_by_tier: dict[str, int]


def is_boundary_storage(machine_type: str) -> bool:
    """A Super Chest / Super Tank boundary buffer (the previewer's storage role, docs/DOMAIN.md)."""
    return machine_type.startswith("Super ")


def port_resource(port: Port) -> str:
    """The resource a non-power port carries, recovered from its ``{direction}:{resource}`` id."""
    prefix = f"{port.direction.value}:"
    return port.id[len(prefix) :] if port.id.startswith(prefix) else port.id


def system_io(problem: InputIR, layout: LayoutResult) -> SystemIO:
    """Derive the boundary I/O + summed power of ``layout`` (only machines it actually placed)."""
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}
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
        cell_t = (cell.x, cell.y, cell.z)
        out_ports = [p for p in machine.faces.ports if p.direction is IODirection.OUTPUT]
        dirs = {p.direction for p in machine.faces.ports}
        only_sources = IODirection.INPUT not in dirs and IODirection.OUTPUT in dirs

        if is_boundary_storage(machine.type) and only_sources:
            for port in out_ports:
                src = net_by_source.get((machine.id, port.id))
                resource = src.fluid_or_item if src and src.fluid_or_item else port_resource(port)
                rate = src.throughput if src else None
                inputs.append(
                    BoundaryFlow(machine.id, machine.type, cell_t, resource, port.commodity, rate)
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
                resource = (
                    sink.fluid_or_item if sink and sink.fluid_or_item else port_resource(port)
                )
                rate = sink.throughput if sink else port.rate
                outputs.append(
                    BoundaryFlow(machine.id, machine.type, cell_t, resource, port.commodity, rate)
                )
            continue

        for port in out_ports:
            if port.commodity is Commodity.POWER:
                continue  # a power output is a source, not a product to collect
            if (machine.id, port.id) in net_by_source:
                continue  # consumed by a net or auto-output (e.g. wired to a collection buffer)
            outputs.append(
                BoundaryFlow(
                    machine.id, machine.type, cell_t, port_resource(port), port.commodity, port.rate
                )
            )

    # Cable-block distance from the source to each powered machine (its depth in the routed power
    # tree), so each load is sized at the loss-reduced *delivered* voltage the builder must
    # actually feed - not the lossless ideal. Loads stay fractional per machine and round up to
    # whole amps only per tier (dataset.amp_load / whole_amps). Machines with no cable (ME power,
    # or an unrouted net) fall back to distance 0. The validator re-derives this distance
    # independently for its amperage check.
    power_distance = _power_distances(layout.routes, port_dir)
    power_total = 0.0
    load_by_tier: dict[str, float] = {}
    for machine in problem.machines:
        if machine.eut <= 0 or machine.id not in coord_of:
            continue  # unpowered blocks / sources draw nothing; describe only placed machines
        tier = machine.voltage_tier
        power_total += machine.eut
        try:
            load = amp_load(machine.eut, tier, distance=power_distance.get(machine.id, 0))
        except (UnknownTierError, UnpowerableError):
            continue  # an off-ladder tier or a run loss has killed: nothing sizeable to report
        load_by_tier[tier] = load_by_tier.get(tier, 0.0) + load
    power_amps_by_tier = {tier: whole_amps(load) for tier, load in load_by_tier.items()}

    return SystemIO(
        inputs=inputs,
        outputs=outputs,
        power_total=power_total,
        power_amps_by_tier=power_amps_by_tier,
    )


def _power_distances(
    routes: list[Route], port_dir: dict[tuple[str, str], IODirection]
) -> dict[str, int]:
    """Cable-block distance from the source to each powered machine, per power route: the machine
    terminal's hop-depth in the routed cable tree (BFS from the single source terminal). Machines
    not on a cable (power on ME, or an unrouted net) are absent, so the caller treats them as
    distance 0. Kept deliberately separate from the validator's own rooting (which re-derives the
    same distance independently, the gate's job) - this is only for the boundary summary."""
    distances: dict[str, int] = {}
    for r in routes:
        if r.commodity is not Commodity.POWER:
            continue
        sources = [
            (t.cell.x, t.cell.y, t.cell.z)
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
                d = depth.get((t.cell.x, t.cell.y, t.cell.z))
                if d is not None:
                    distances[t.machine_id] = d
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
        a = (seg.start.x, seg.start.y, seg.start.z)
        b = (seg.end.x, seg.end.y, seg.end.z)
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
