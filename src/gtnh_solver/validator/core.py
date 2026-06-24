"""Independent geometric + structural validation of a layout against its problem.

This is the only *automated* correctness gate (there is no headless GT simulator). Its
logic is written independently of the router/placement so it can catch their bugs
(docs/ARCHITECTURE.md #4). ``validate`` never raises and never short-circuits: it returns
*every* violation it can prove, computed from geometry/structure alone - independent of the
``status`` the layout claims, so a layout that calls itself ``valid`` while overlapping
machines is caught (``report.ok is False``).

What is checked now (needs only the IR):
  completeness/referential - every machine placed the right number of times with a legal
  orientation; every physically-routed net routed exactly once; ME-toggled commodities not
  routed; route commodity matches its net.
  geometry - machines in-bounds, non-overlapping, off reserved cells; routes in-bounds,
  contiguous, every segment a unit (+/-1) hop, and never running through a machine body or a
  reserved cell; pinned I/O actually sits on its net's route.
  terminals - every net endpoint has a terminal on a usable (non-front) face adjacent to its
  machine, and that terminal cell lies on the route (the geometric half of required-I/O-face
  reachability).
  auto-output - every auto-connection joins its net's real OUTPUT->INPUT endpoint machines
  (resolved by port direction) on adjacent usable faces; power/ME commodities cannot
  auto-output, and a machine has at most one auto-output face.
  power - per-segment cable thickness is present and well-formed (1/2/4/8/16, aligned).

What is deferred to the dataset lane (rule data not available yet) - TODO:
  throughput/tier caps, one-fluid-per-line, *summed* amperage <= cable rating, and the
  dataset-specific half of face rules (which faces a given machine type may use, covers).
  These need the physical-rules dataset; the checks above are the floor they build on.
"""

from __future__ import annotations

from collections import defaultdict

from gtnh_solver.ir import (
    AutoConnection,
    Commodity,
    InputIR,
    IODirection,
    LayoutResult,
    Net,
    Placement,
)

from ._geometry import (
    FACE_DELTAS,
    OPPOSITE_FACE,
    Cell,
    in_region,
    is_connected,
    is_unit_step,
    occupied_cells,
)
from .report import ValidationReport, Violation, ViolationCode


def validate(problem: InputIR, layout: LayoutResult) -> ValidationReport:
    """Validate ``layout`` against ``problem`` and return all proven violations."""
    out: list[Violation] = []
    _check_placements(problem, layout, out)
    _check_routes(problem, layout, out)
    _check_terminals(problem, layout, out)
    _check_auto_connections(problem, layout, out)
    _check_pinned(problem, layout, out)
    return ValidationReport(tuple(out))


def _check_placements(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> None:
    machines = {m.id: m for m in problem.machines}
    region = problem.bounding_region
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    owner: dict[Cell, str] = {}
    counts: dict[str, int] = defaultdict(int)

    for pl in layout.placements:
        m = machines.get(pl.machine_id)
        if m is None:
            out.append(
                Violation(
                    ViolationCode.UNKNOWN_MACHINE,
                    f"placement references unknown machine {pl.machine_id!r}",
                )
            )
            continue
        counts[pl.machine_id] += 1
        if pl.orientation not in m.orientation_options:
            out.append(
                Violation(
                    ViolationCode.BAD_ORIENTATION,
                    f"machine {pl.machine_id!r} placed facing {pl.orientation.value}, "
                    f"not one of its orientation_options",
                )
            )
        for cell in occupied_cells(pl.cell, m.footprint):
            if not in_region(cell, region):
                out.append(
                    Violation(
                        ViolationCode.MACHINE_OUT_OF_BOUNDS,
                        f"machine {pl.machine_id!r} occupies {cell}, outside the bounding region",
                    )
                )
            if cell in reserved:
                out.append(
                    Violation(
                        ViolationCode.MACHINE_ON_RESERVED,
                        f"machine {pl.machine_id!r} occupies reserved cell {cell}",
                    )
                )
            prev = owner.get(cell)
            if prev is not None:
                out.append(
                    Violation(
                        ViolationCode.MACHINE_OVERLAP,
                        f"machines {prev!r} and {pl.machine_id!r} overlap at {cell}",
                    )
                )
            else:
                owner[cell] = pl.machine_id

    for m in problem.machines:
        if counts[m.id] != 1:
            out.append(
                Violation(
                    ViolationCode.PLACEMENT_COUNT_MISMATCH,
                    f"machine {m.id!r} expects exactly one placement, found {counts[m.id]}",
                )
            )


def _check_routes(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> None:
    nets = {n.id: n for n in problem.nets}
    region = problem.bounding_region
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    machines = {m.id: m for m in problem.machines}
    body_cells: set[Cell] = set()
    for pl in layout.placements:
        m = machines.get(pl.machine_id)
        if m is not None:
            body_cells.update(occupied_cells(pl.cell, m.footprint))
    routed: set[str] = set()

    for r in layout.routes:
        net = nets.get(r.net_id)
        if net is None:
            out.append(
                Violation(ViolationCode.UNKNOWN_NET, f"route references unknown net {r.net_id!r}")
            )
            continue
        if r.net_id in routed:
            out.append(
                Violation(
                    ViolationCode.DUPLICATE_ROUTE, f"net {r.net_id!r} is routed more than once"
                )
            )
        routed.add(r.net_id)

        if r.commodity is not net.commodity:
            out.append(
                Violation(
                    ViolationCode.ROUTE_COMMODITY_MISMATCH,
                    f"route for net {r.net_id!r} is {r.commodity.value}, "
                    f"but the net is {net.commodity.value}",
                )
            )
        if problem.me_toggles.toggled(net.commodity):
            out.append(
                Violation(
                    ViolationCode.UNEXPECTED_ME_ROUTE,
                    f"net {r.net_id!r} ({net.commodity.value}) is ME-toggled and must not be "
                    f"physically routed",
                )
            )

        edges: list[tuple[Cell, Cell]] = []
        route_cells: set[Cell] = set()
        for seg in r.segments:
            start = (seg.start.x, seg.start.y, seg.start.z)
            end = (seg.end.x, seg.end.y, seg.end.z)
            edges.append((start, end))
            route_cells.update((start, end))
            for cell in (start, end):
                if not in_region(cell, region):
                    out.append(
                        Violation(
                            ViolationCode.ROUTE_OUT_OF_BOUNDS,
                            f"route for net {r.net_id!r} passes through {cell}, out of bounds",
                        )
                    )
            if not is_unit_step(start, end):
                out.append(
                    Violation(
                        ViolationCode.ROUTE_SEGMENT_NOT_UNIT,
                        f"route for net {r.net_id!r} has a non-unit segment {start}->{end} "
                        f"(a route hop must move exactly one cell)",
                    )
                )
        if not is_connected(edges):
            out.append(
                Violation(
                    ViolationCode.ROUTE_DISCONTINUOUS,
                    f"route for net {r.net_id!r} is empty or not a single connected path",
                )
            )
        # A coarse cell that the placer/router treats as solid must not also carry a route -
        # the abstraction would otherwise certify a pipe running through a machine body or a
        # reserved cell (docs/ARCHITECTURE.md: cell->block realizability).
        for cell in sorted(route_cells):
            if cell in body_cells:
                out.append(
                    Violation(
                        ViolationCode.ROUTE_THROUGH_MACHINE,
                        f"route for net {r.net_id!r} passes through a machine body at {cell}",
                    )
                )
            if cell in reserved:
                out.append(
                    Violation(
                        ViolationCode.ROUTE_ON_RESERVED,
                        f"route for net {r.net_id!r} passes through reserved cell {cell}",
                    )
                )

        if r.commodity is Commodity.POWER:
            tps = r.thickness_per_segment
            if (
                tps is None
                or len(tps) != len(r.segments)
                or any(t not in (1, 2, 4, 8, 16) for t in tps)
            ):
                out.append(
                    Violation(
                        ViolationCode.POWER_THICKNESS_INVALID,
                        f"power route for net {r.net_id!r} has missing/misaligned/invalid "
                        f"thickness_per_segment",
                    )
                )

    auto_ids = {ac.net_id for ac in layout.auto_connections}
    for net in problem.nets:
        if problem.me_toggles.toggled(net.commodity):
            continue
        routed_here, auto_here = net.id in routed, net.id in auto_ids
        if routed_here and auto_here:
            out.append(
                Violation(
                    ViolationCode.NET_DOUBLE_CONNECTED,
                    f"net {net.id!r} is both routed and auto-connected",
                )
            )
        elif not routed_here and not auto_here:
            out.append(
                Violation(
                    ViolationCode.MISSING_CONNECTION,
                    f"net {net.id!r} is neither routed nor auto-connected",
                )
            )


def _check_terminals(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> None:
    machines = {m.id: m for m in problem.machines}
    placement_by_machine: dict[str, Placement] = {}
    for pl in layout.placements:
        placement_by_machine.setdefault(pl.machine_id, pl)
    nets = {n.id: n for n in problem.nets}

    for r in layout.routes:
        net = nets.get(r.net_id)
        if net is None:
            continue  # UNKNOWN_NET already reported by _check_routes
        route_cells = {
            cell
            for seg in r.segments
            for cell in (
                (seg.start.x, seg.start.y, seg.start.z),
                (seg.end.x, seg.end.y, seg.end.z),
            )
        }
        have = {(t.machine_id, t.port_id) for t in r.terminals}
        for ep in net.endpoints:
            if (ep.machine_id, ep.port_id) not in have:
                out.append(
                    Violation(
                        ViolationCode.MISSING_TERMINAL,
                        f"net {r.net_id!r} endpoint {ep.port_id!r} on {ep.machine_id!r} "
                        f"has no terminal",
                    )
                )
        for t in r.terminals:
            placement = placement_by_machine.get(t.machine_id)
            machine = machines.get(t.machine_id)
            if placement is None or machine is None:
                continue  # placement/machine problems reported elsewhere
            cell = (t.cell.x, t.cell.y, t.cell.z)
            if t.face is placement.orientation:
                out.append(
                    Violation(
                        ViolationCode.TERMINAL_ON_FRONT_FACE,
                        f"terminal for net {r.net_id!r} on {t.machine_id!r} uses the front "
                        f"face {t.face.value}",
                    )
                )
            dx, dy, dz = FACE_DELTAS[t.face]
            body = set(occupied_cells(placement.cell, machine.footprint))
            if (cell[0] - dx, cell[1] - dy, cell[2] - dz) not in body or cell in body:
                out.append(
                    Violation(
                        ViolationCode.TERMINAL_NOT_ADJACENT,
                        f"terminal {cell} for net {r.net_id!r} is not just outside "
                        f"{t.machine_id!r} on face {t.face.value}",
                    )
                )
            if cell not in route_cells:
                out.append(
                    Violation(
                        ViolationCode.TERMINAL_NOT_ON_ROUTE,
                        f"terminal {cell} for net {r.net_id!r} is not on the route",
                    )
                )


def _check_auto_connections(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> None:
    machines = {m.id: m for m in problem.machines}
    nets = {n.id: n for n in problem.nets}
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}
    placement_of: dict[str, Placement] = {}
    for pl in layout.placements:
        placement_of.setdefault(pl.machine_id, pl)
    source_uses: dict[str, int] = defaultdict(int)

    for ac in layout.auto_connections:
        source_uses[ac.source_machine_id] += 1
        net = nets.get(ac.net_id)
        if net is None:
            out.append(
                Violation(
                    ViolationCode.UNKNOWN_NET,
                    f"auto-connection references unknown net {ac.net_id!r}",
                )
            )
        else:
            _check_auto_net(problem, net, ac, port_dir, out)
        sp = placement_of.get(ac.source_machine_id)
        sm = machines.get(ac.source_machine_id)
        tp = placement_of.get(ac.target_machine_id)
        tm = machines.get(ac.target_machine_id)
        if sp is None or sm is None or tp is None or tm is None:
            continue  # unknown / unplaced machine reported by _check_placements
        if ac.source_face is sp.orientation or ac.target_face is tp.orientation:
            out.append(
                Violation(
                    ViolationCode.AUTO_OUTPUT_ON_FRONT_FACE,
                    f"auto-output for net {ac.net_id!r} uses a front face",
                )
            )
        dx, dy, dz = FACE_DELTAS[ac.source_face]
        source_cells = set(occupied_cells(sp.cell, sm.footprint))
        target_cells = set(occupied_cells(tp.cell, tm.footprint))
        adjacent = any((x + dx, y + dy, z + dz) in target_cells for x, y, z in source_cells)
        if not adjacent or ac.target_face is not OPPOSITE_FACE[ac.source_face]:
            out.append(
                Violation(
                    ViolationCode.AUTO_OUTPUT_NOT_ADJACENT,
                    f"auto-output for net {ac.net_id!r}: {ac.source_machine_id!r} does not meet "
                    f"{ac.target_machine_id!r} on {ac.source_face.value}/{ac.target_face.value}",
                )
            )

    for machine_id, uses in source_uses.items():
        if uses > 1:
            out.append(
                Violation(
                    ViolationCode.DUPLICATE_AUTO_OUTPUT,
                    f"machine {machine_id!r} auto-outputs to {uses} nets (only one auto-output face)",
                )
            )


def _check_auto_net(
    problem: InputIR,
    net: Net,
    ac: AutoConnection,
    port_dir: dict[tuple[str, str], IODirection],
    out: list[Violation],
) -> None:
    """Check an auto-connection actually satisfies its claimed net (not just any two machines).

    Geometry alone is not enough: a layout could claim net ``n`` is auto-connected by two
    adjacent machines that are not even ``n``'s endpoints. So the net must be a routable
    item/fluid commodity, and ``source``/``target`` must be the net's real OUTPUT and INPUT
    endpoint machines (resolved by port direction).
    """
    if net.commodity is Commodity.POWER or problem.me_toggles.toggled(net.commodity):
        reason = (
            "power is a shared-amperage net, not a face auto-output"
            if net.commodity is Commodity.POWER
            else "the commodity is ME-routed, not physically connected"
        )
        out.append(
            Violation(
                ViolationCode.AUTO_OUTPUT_ILLEGAL_COMMODITY,
                f"net {ac.net_id!r} ({net.commodity.value}) cannot be satisfied by "
                f"auto-output - {reason}",
            )
        )
        return
    out_machines = {
        e.machine_id
        for e in net.endpoints
        if port_dir.get((e.machine_id, e.port_id)) is IODirection.OUTPUT
    }
    in_machines = {
        e.machine_id
        for e in net.endpoints
        if port_dir.get((e.machine_id, e.port_id)) is IODirection.INPUT
    }
    if ac.source_machine_id not in out_machines or ac.target_machine_id not in in_machines:
        out.append(
            Violation(
                ViolationCode.AUTO_OUTPUT_WRONG_ENDPOINTS,
                f"auto-output for net {ac.net_id!r}: "
                f"{ac.source_machine_id!r}->{ac.target_machine_id!r} are not the net's "
                f"output->input endpoint machines",
            )
        )


def _check_pinned(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> None:
    cells_by_net: dict[str, set[Cell]] = defaultdict(set)
    for r in layout.routes:
        for seg in r.segments:
            cells_by_net[r.net_id].add((seg.start.x, seg.start.y, seg.start.z))
            cells_by_net[r.net_id].add((seg.end.x, seg.end.y, seg.end.z))

    for pin in problem.pinned:
        cell = (pin.cell.x, pin.cell.y, pin.cell.z)
        if cell not in cells_by_net.get(pin.net_id, set()):
            out.append(
                Violation(
                    ViolationCode.PINNED_IO_NOT_ON_ROUTE,
                    f"pinned {pin.kind.value} for net {pin.net_id!r} at {cell} is not on the "
                    f"net's route",
                )
            )
