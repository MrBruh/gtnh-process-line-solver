"""buildguide.core - render a LayoutResult as a human-readable text build guide.

Four sections: a header (status / region / counts), a bill of materials (machines by type,
pipe/cable cells per commodity, I/O cover count), the connections (per net: resource and the
machine faces it links), and a per-layer ASCII map (one character per cell) with a key. This
is the cheap, visible Phase 1 payoff - something a player can actually read and build from,
ahead of the three.js previewer (docs/ROADMAP.md).
"""

from __future__ import annotations

import string
from collections import Counter

from gtnh_solver.ir import InputIR, LayoutResult, Machine, Net
from gtnh_solver.ir.geometry import Cell, occupied_cells

# Single-char machine markers (upper, lower, digits = 62; covers the ~30-50 machine target).
_MARKERS = string.ascii_uppercase + string.ascii_lowercase + string.digits
_PIPE_CHAR = {"item": "+", "fluid": "~", "power": "="}
_PIPE_LABEL = {"item": "item pipe", "fluid": "fluid pipe", "power": "power cable"}


def build_guide(problem: InputIR, layout: LayoutResult) -> str:
    """Render ``layout`` (its problem supplies machine types and net resources) as text."""
    machines = {m.id: m for m in problem.machines}
    nets = {n.id: n for n in problem.nets}
    lines: list[str] = []
    lines += _header(problem, layout)
    lines += _bom(layout, machines)
    lines += _connections(layout, machines, nets)
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
        covers += len(r.terminals)

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


def _machine_label(machine_id: str, machines: dict[str, Machine]) -> str:
    return machines[machine_id].type if machine_id in machines else machine_id


def _connections(
    layout: LayoutResult, machines: dict[str, Machine], nets: dict[str, Net]
) -> list[str]:
    if not layout.routes and not layout.auto_connections:
        return []
    lines = ["## Connections", ""]
    for ac in layout.auto_connections:
        net = nets.get(ac.net_id)
        resource = net.fluid_or_item if net and net.fluid_or_item else ac.net_id
        src = _machine_label(ac.source_machine_id, machines)
        tgt = _machine_label(ac.target_machine_id, machines)
        lines.append(
            f"  {resource:<22} {src} [{ac.source_face.value}] => {tgt} [{ac.target_face.value}]   (auto-output)"
        )
    for r in layout.routes:
        net = nets.get(r.net_id)
        resource = net.fluid_or_item if net and net.fluid_or_item else r.commodity.value
        ends = [f"{_machine_label(t.machine_id, machines)} [{t.face.value}]" for t in r.terminals]
        lines.append(f"  {resource:<22} {' -> '.join(ends)}   (pipe)")
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
