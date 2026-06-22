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
  geometry - machines in-bounds, non-overlapping, off reserved cells; routes in-bounds and
  contiguous; pinned I/O actually sits on its net's route.
  terminals - every net endpoint has a terminal on a usable (non-front) face adjacent to its
  machine, and that terminal cell lies on the route (the geometric half of required-I/O-face
  reachability).
  power - per-segment cable thickness is present and well-formed (1/2/4/8/16, aligned).

What is deferred to the dataset lane (rule data not available yet) - TODO:
  throughput/tier caps, one-fluid-per-line, *summed* amperage <= cable rating, and the
  dataset-specific half of face rules (which faces a given machine type may use, covers).
  These need the physical-rules dataset; the checks above are the floor they build on.
"""

from __future__ import annotations

from collections import defaultdict

from gtnh_solver.ir import Commodity, InputIR, LayoutResult, Placement

from ._geometry import FACE_DELTAS, Cell, in_region, is_connected, occupied_cells
from .report import ValidationReport, Violation, ViolationCode


def validate(problem: InputIR, layout: LayoutResult) -> ValidationReport:
    """Validate ``layout`` against ``problem`` and return all proven violations."""
    out: list[Violation] = []
    _check_placements(problem, layout, out)
    _check_routes(problem, layout, out)
    _check_terminals(problem, layout, out)
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
        if counts[m.id] != m.count:
            out.append(
                Violation(
                    ViolationCode.PLACEMENT_COUNT_MISMATCH,
                    f"machine {m.id!r} expects {m.count} placement(s), found {counts[m.id]}",
                )
            )


def _check_routes(problem: InputIR, layout: LayoutResult, out: list[Violation]) -> None:
    nets = {n.id: n for n in problem.nets}
    region = problem.bounding_region
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

        for seg in r.segments:
            for cell in (
                (seg.start.x, seg.start.y, seg.start.z),
                (seg.end.x, seg.end.y, seg.end.z),
            ):
                if not in_region(cell, region):
                    out.append(
                        Violation(
                            ViolationCode.ROUTE_OUT_OF_BOUNDS,
                            f"route for net {r.net_id!r} passes through {cell}, out of bounds",
                        )
                    )
        edges = [
            (
                (seg.start.x, seg.start.y, seg.start.z),
                (seg.end.x, seg.end.y, seg.end.z),
            )
            for seg in r.segments
        ]
        if not is_connected(edges):
            out.append(
                Violation(
                    ViolationCode.ROUTE_DISCONTINUOUS,
                    f"route for net {r.net_id!r} is empty or not a single connected path",
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

    for net in problem.nets:
        if problem.me_toggles.toggled(net.commodity):
            continue
        if net.id not in routed:
            out.append(Violation(ViolationCode.MISSING_ROUTE, f"net {net.id!r} has no route"))


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
