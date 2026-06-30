"""buildguide.core - render a LayoutResult as a human-readable text build guide.

Aimed at being *buildable from alone*: a header (status / region / counts), a bill of materials,
a **placement table** (each machine's exact cell + front face + footprint), a power note (where to
feed external power and at what amperage, since synthetic sources are not self-powered), the
**connections** (per net: the machine faces, the cover each pipe terminal needs, the exact cell
path, and per-segment cable thickness for power), and a per-layer ASCII map with a key. This is
the cheap, visible Phase 1 payoff - something a player can actually read and build from, ahead of
the three.js previewer (docs/ROADMAP.md). Covers follow docs/DOMAIN.md (conveyor for items,
pump/regulator for fluids); auto-output needs no cover (the machine ejects into the neighbour).
"""

from __future__ import annotations

import string
from collections import Counter

from gtnh_solver.dataset import tier_voltage
from gtnh_solver.ir import (
    CellCoord,
    Commodity,
    InputIR,
    IODirection,
    LayoutResult,
    Machine,
    Net,
    Port,
    Route,
)
from gtnh_solver.ir.geometry import Cell, occupied_cells

# Single-char machine markers (upper, lower, digits = 62; covers the ~30-50 machine target).
_MARKERS = string.ascii_uppercase + string.ascii_lowercase + string.digits
_PIPE_CHAR = {"item": "+", "fluid": "~", "power": "="}
_PIPE_LABEL = {"item": "item pipe", "fluid": "fluid pipe", "power": "power cable"}
# The GT cover a physical pipe terminal needs, by commodity (docs/DOMAIN.md). Power cables connect
# bare (no cover); auto-output needs none either.
_COVER = {"item": "conveyor cover", "fluid": "pump cover"}
# Per-commodity throughput unit for the System I/O rates (typed, docs/IR.md).
_RATE_UNIT = {Commodity.ITEM: "items/t", Commodity.FLUID: "mB/t", Commodity.POWER: "EU/t"}


def build_guide(problem: InputIR, layout: LayoutResult) -> str:
    """Render ``layout`` (its problem supplies machine types and net resources) as text."""
    machines = {m.id: m for m in problem.machines}
    nets = {n.id: n for n in problem.nets}
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}
    coord_of = {p.machine_id: p.cell for p in layout.placements}
    lines: list[str] = []
    lines += _header(problem, layout)
    lines += _bom(layout, machines)
    lines += _placement_table(layout, machines)
    lines += _system_io(problem, port_dir, coord_of)
    lines += _power_note(layout, machines)
    lines += _connections(layout, machines, nets, port_dir, coord_of)
    lines += _layer_maps(problem, layout, machines)
    return "\n".join(lines) + "\n"


def _header(problem: InputIR, layout: LayoutResult) -> list[str]:
    region = problem.bounding_region
    return [
        "# Build guide",
        "",
        f"Status: {layout.status.value}    seed: {layout.seed}",
        f"Region: {region.sx} x {region.sy} x {region.sz} cells (x, y, z)",
        f"Machines placed: {len(layout.placements)}    Nets routed: {len(layout.routes)}",
        "",
    ]


def _bom(layout: LayoutResult, machines: dict[str, Machine]) -> list[str]:
    by_type: Counter[str] = Counter()
    for p in layout.placements:
        machine = machines.get(p.machine_id)
        by_type[machine.type if machine else "(unknown)"] += 1

    pipe_cells: dict[str, set[Cell]] = {}
    covers = 0
    for r in layout.routes:
        cells = pipe_cells.setdefault(r.commodity.value, set())
        for seg in r.segments:
            cells.add((seg.start.x, seg.start.y, seg.start.z))
            cells.add((seg.end.x, seg.end.y, seg.end.z))
        if r.commodity is not Commodity.POWER:
            covers += len(r.terminals)  # power cables connect bare; covers are item/fluid only

    lines = ["## Bill of materials", "", "Machines:"]
    lines += [f"  {n:>3}  x  {typ}" for typ, n in sorted(by_type.items())]
    lines += ["", "Routing:"]
    if pipe_cells:
        lines += [f"  {len(c):>3}  x  {_PIPE_LABEL[k]}" for k, c in sorted(pipe_cells.items())]
    else:
        lines.append("  (no pipes)")
    lines.append(f"  {covers:>3}  x  I/O cover (one per pipe terminal)")
    lines.append(
        f"  {len(layout.auto_connections):>3}  x  auto-output connection (adjacent, no pipe)"
    )
    lines.append("")
    return lines


def _placement_table(layout: LayoutResult, machines: dict[str, Machine]) -> list[str]:
    """Exact build coordinates: where each machine goes and which way its front faces."""
    if not layout.placements:
        return []
    lines = [
        "## Placement",
        "",
        "Place each machine at its (x, y, z) cell, facing the listed front (the front face carries",
        "no I/O - covers and auto-output go on the other five faces):",
        "",
    ]
    for p in sorted(layout.placements, key=lambda pl: (pl.cell.y, pl.cell.z, pl.cell.x)):
        machine = machines.get(p.machine_id)
        typ = machine.type if machine else p.machine_id
        fp = machine.footprint if machine else None
        size = f"{fp.sx}x{fp.sy}x{fp.sz}" if fp else "?"
        lines.append(
            f"  {typ:<22} at ({p.cell.x}, {p.cell.y}, {p.cell.z})"
            f"   front {p.orientation.value:<5}   {size}"
        )
    lines.append("")
    return lines


def _is_boundary_storage(machine: Machine) -> bool:
    """A Super Chest / Super Tank boundary buffer (matches the previewer's storage role)."""
    return machine.type.startswith("Super ")


def _port_resource(port: Port) -> str:
    """The resource a non-power port carries, recovered from its ``{direction}:{resource}`` id."""
    prefix = f"{port.direction.value}:"
    return port.id[len(prefix) :] if port.id.startswith(prefix) else port.id


def _rate_note(net: Net | None) -> str:
    """`` (~<rate> <unit>)`` for a net's typed throughput, or `` `` when the net is unknown."""
    if net is None:
        return ""
    return f" (~{net.throughput:g} {_RATE_UNIT[net.commodity]})"


def _system_io(
    problem: InputIR,
    port_dir: dict[tuple[str, str], IODirection],
    coord_of: dict[str, CellCoord],
) -> list[str]:
    """The line's boundary: raw inputs to load, finished products to collect (GitHub #15).

    Inputs are boundary storages (Super Chest/Tank) that only *source* the line - nothing feeds
    them, so you fill them by hand. Outputs are machine OUTPUT ports no net consumes - the product
    exits there with nothing collecting it, so the builder must place a Super Chest/Tank. Both come
    straight from the IR (storage roles; output ports minus the ones a net wires), so the guide is
    buildable without reading the source plan.
    """
    # The net each output port sources, keyed by (machine, port) - gives its resource + rate, and
    # its presence means the port is consumed (not a dangling system output).
    net_by_source: dict[tuple[str, str], Net] = {}
    for net in problem.nets:
        for ep in net.endpoints:
            if port_dir.get((ep.machine_id, ep.port_id)) is IODirection.OUTPUT:
                net_by_source[(ep.machine_id, ep.port_id)] = net

    inputs: list[str] = []
    outputs: list[str] = []
    for machine in problem.machines:
        cell = coord_of.get(machine.id)
        if cell is None:  # only describe machines the layout actually placed
            continue
        where = f"{machine.type} at ({cell.x}, {cell.y}, {cell.z})"
        out_ports = [p for p in machine.faces.ports if p.direction is IODirection.OUTPUT]
        dirs = {p.direction for p in machine.faces.ports}
        only_sources = IODirection.INPUT not in dirs and IODirection.OUTPUT in dirs

        if _is_boundary_storage(machine) and only_sources:
            for port in out_ports:
                src_net = net_by_source.get((machine.id, port.id))
                resource = (
                    src_net.fluid_or_item
                    if src_net and src_net.fluid_or_item
                    else _port_resource(port)
                )
                inputs.append(f"  load {where} with {resource}{_rate_note(src_net)}")
            continue

        for port in out_ports:
            if port.commodity is Commodity.POWER:
                continue  # a power output is a source, not a product to collect
            if (machine.id, port.id) in net_by_source:
                continue  # consumed by a net or auto-output
            outputs.append(
                f"  {_port_resource(port)} exits {where} - place a Super Chest/Tank to collect it"
            )

    if not inputs and not outputs:
        return []
    lines = ["## System inputs / outputs", ""]
    if inputs:
        lines += ["Inputs (load these yourself):", *inputs, ""]
    if outputs:
        lines += ["Outputs (place a buffer to collect each):", *outputs, ""]
    return lines


def _is_power_source(machine: Machine) -> bool:
    return any(
        p.commodity is Commodity.POWER and p.direction is IODirection.OUTPUT
        for p in machine.faces.ports
    )


def _power_note(layout: LayoutResult, machines: dict[str, Machine]) -> list[str]:
    """Tell the builder where to feed external power - synthetic sources are not self-powered.

    Each source is stated as a wiring spec: its tier voltage, the amperage to feed (the cable
    thickness at the trunk root, i.e. the summed amps of its tier), and the EU/t that buys. The
    per-segment thickness along the trunk is listed under Connections.
    """
    sources = [
        (p, machines[p.machine_id])
        for p in layout.placements
        if p.machine_id in machines and _is_power_source(machines[p.machine_id])
    ]
    if not sources:
        return []
    lines = [
        "## Power",
        "",
        "Source-powering is left to you (docs/DOMAIN.md): place an external power source feeding",
        "each synthetic source block below at the listed tier voltage and amperage. Per-segment",
        "cable thickness is listed under Connections.",
        "",
    ]
    for p, m in sources:
        root = _root_thickness(layout, p.machine_id)
        cell = f"({p.cell.x}, {p.cell.y}, {p.cell.z})"
        if root is None:  # source with no cable (nothing to size against)
            lines.append(f"  {m.type} at {cell}")
            continue
        volts = tier_voltage(m.voltage_tier)
        lines.append(
            f"  {m.type} at {cell}: feed {m.voltage_tier} ({volts} V), "
            f">={root} A -> up to {volts * root} EU/t"
        )
    lines.append("")
    return lines


def _root_thickness(layout: LayoutResult, source_machine_id: str) -> int | None:
    """The thickest cable segment on the trunk this source feeds (its root carries the whole tier)."""
    best: int | None = None
    for r in layout.routes:
        if r.commodity is not Commodity.POWER or not r.thickness_per_segment:
            continue
        if any(t.machine_id == source_machine_id for t in r.terminals):
            best = max(best or 0, max(r.thickness_per_segment))
    return best


def _machine_at(
    machine_id: str, machines: dict[str, Machine], coord_of: dict[str, CellCoord]
) -> str:
    """``Type (x,y,z)`` - the machine's type and where it sits (so a net names the right instance)."""
    label = machines[machine_id].type if machine_id in machines else machine_id
    cell = coord_of.get(machine_id)
    return f"{label} ({cell.x},{cell.y},{cell.z})" if cell is not None else label


def _cover_label(commodity: Commodity, direction: IODirection | None) -> str:
    """The GT cover a pipe terminal needs: conveyor (items) / pump (fluids), in input/output mode."""
    cover = _COVER.get(commodity.value, "cover")
    mode = direction.value if direction is not None else "i/o"
    return f"{cover} ({mode})"


def _route_path(route: Route) -> str:
    """The route's cells in order, joined by ``->`` (items/fluids) or ``=Nx=`` per power segment.

    Assumes the segments form a single ordered chain (each segment starts where the previous ended)
    - which the crude A* pipes and path-trunk cables do; a branching tree would render approximately.
    """
    if not route.segments:
        return "(no cells)"
    first = route.segments[0].start
    parts = [f"({first.x},{first.y},{first.z})"]
    for i, seg in enumerate(route.segments):
        link = f" ={route.thickness_per_segment[i]}x= " if route.thickness_per_segment else " -> "
        parts.append(f"{link}({seg.end.x},{seg.end.y},{seg.end.z})")
    return "".join(parts)


def _connections(
    layout: LayoutResult,
    machines: dict[str, Machine],
    nets: dict[str, Net],
    port_dir: dict[tuple[str, str], IODirection],
    coord_of: dict[str, CellCoord],
) -> list[str]:
    if not layout.routes and not layout.auto_connections:
        return []
    lines = ["## Connections", ""]
    for ac in layout.auto_connections:
        net = nets.get(ac.net_id)
        resource = net.fluid_or_item if net and net.fluid_or_item else ac.net_id
        src = _machine_at(ac.source_machine_id, machines, coord_of)
        tgt = _machine_at(ac.target_machine_id, machines, coord_of)
        lines.append(
            f"  {resource:<22} {src} [{ac.source_face.value}] => "
            f"{tgt} [{ac.target_face.value}]   (auto-output)"
        )
    for r in layout.routes:
        net = nets.get(r.net_id)
        resource = net.fluid_or_item if net and net.fluid_or_item else r.commodity.value
        kind = "power" if r.commodity is Commodity.POWER else "pipe"
        ends = []
        for t in r.terminals:
            label = _machine_at(t.machine_id, machines, coord_of)
            if r.commodity is Commodity.POWER:
                ends.append(f"{label} [{t.face.value}]")  # cables connect bare, no cover
            else:
                cover = _cover_label(r.commodity, port_dir.get((t.machine_id, t.port_id)))
                ends.append(f"{label} [{t.face.value}, {cover}]")
        lines.append(f"  {resource:<22} {' -> '.join(ends)}   ({kind})")
        lines.append(f"      lay along: {_route_path(r)}")
    lines.append("")
    return lines


def _layer_maps(problem: InputIR, layout: LayoutResult, machines: dict[str, Machine]) -> list[str]:
    marker_of: dict[str, str] = {}
    machine_cells: dict[Cell, str] = {}
    for p in layout.placements:
        if p.machine_id not in marker_of:
            marker_of[p.machine_id] = _MARKERS[len(marker_of) % len(_MARKERS)]
        machine = machines.get(p.machine_id)
        if machine is None:
            continue
        for cell in occupied_cells(p.cell, machine.footprint):
            machine_cells[cell] = marker_of[p.machine_id]

    route_cells: dict[Cell, str] = {}
    for r in layout.routes:
        char = _PIPE_CHAR[r.commodity.value]
        for seg in r.segments:
            route_cells.setdefault((seg.start.x, seg.start.y, seg.start.z), char)
            route_cells.setdefault((seg.end.x, seg.end.y, seg.end.z), char)

    occupied = set(machine_cells) | set(route_cells)
    if not occupied:
        return ["## Layout", "", "  (empty)", ""]

    min_x, max_x = min(c[0] for c in occupied), max(c[0] for c in occupied)
    min_z, max_z = min(c[2] for c in occupied), max(c[2] for c in occupied)
    lines = ["## Layout (one char per cell; x increases right, z increases down)", ""]
    for y in range(problem.bounding_region.sy):
        if not any(c[1] == y for c in occupied):
            continue
        lines.append(f"### Layer y = {y}")
        for z in range(min_z, max_z + 1):
            row = "".join(
                machine_cells.get((x, y, z)) or route_cells.get((x, y, z)) or "."
                for x in range(min_x, max_x + 1)
            )
            lines.append(f"  {row}")
        lines += [f"  (rows z={min_z}..{max_z}, cols x={min_x}..{max_x})", ""]

    lines.append("## Key")
    for machine_id, marker in marker_of.items():
        machine = machines.get(machine_id)
        lines.append(f"  {marker} = {machine.type if machine else machine_id}")
    lines.append("  + = item pipe    ~ = fluid pipe    = = power cable    . = empty")
    return lines
