"""system_io - the boundary of a solved line: what to feed in, what comes out, total power.

Both the text build guide and the 3D previewer answer the same question - "what does this line
consume and produce at its edge, and how much power does it draw" - so deriving it twice would let
the two surfaces drift. This module is the single source, pure over the ``InputIR`` + the
``LayoutResult``; the renderers only format it.

- **inputs**: a boundary storage (Super Chest/Tank) that *only* sources the line - nothing feeds it,
  so the builder fills it. Each carries the resource + its typed rate.
- **outputs**: a machine OUTPUT port no net consumes - the finished product exits there with
  nothing collecting it, so the builder must place a Super Chest/Tank.
- **power**: the summed ``eut`` the placed machines draw, broken down by voltage tier
  (docs/DOMAIN.md - a shared-amperage net's draw is what the source must supply).
"""

from __future__ import annotations

from dataclasses import dataclass

from gtnh_solver.dataset import amperage
from gtnh_solver.ir import Commodity, InputIR, IODirection, LayoutResult, Net, Port

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
    already implies the voltage, so amps is the useful per-tier number)."""

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

    # The net each output port sources (keyed by machine+port): its presence means the port is
    # consumed (not a dangling system output), and it carries the resource + rate.
    net_by_source: dict[tuple[str, str], Net] = {}
    for net in problem.nets:
        for ep in net.endpoints:
            if port_dir.get((ep.machine_id, ep.port_id)) is IODirection.OUTPUT:
                net_by_source[(ep.machine_id, ep.port_id)] = net

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

        for port in out_ports:
            if port.commodity is Commodity.POWER:
                continue  # a power output is a source, not a product to collect
            if (machine.id, port.id) in net_by_source:
                continue  # consumed by a net or auto-output
            outputs.append(
                BoundaryFlow(
                    machine.id, machine.type, cell_t, port_resource(port), port.commodity, port.rate
                )
            )

    power_total = 0.0
    power_amps_by_tier: dict[str, int] = {}
    for machine in problem.machines:
        if machine.eut <= 0 or machine.id not in coord_of:
            continue  # unpowered blocks / sources draw nothing; describe only placed machines
        tier = machine.voltage_tier
        power_total += machine.eut
        power_amps_by_tier[tier] = power_amps_by_tier.get(tier, 0) + amperage(machine.eut, tier)

    return SystemIO(
        inputs=inputs,
        outputs=outputs,
        power_total=power_total,
        power_amps_by_tier=power_amps_by_tier,
    )
