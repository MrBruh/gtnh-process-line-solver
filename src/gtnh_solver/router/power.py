"""router.power - the shared-amperage power router.

Power is **not** a disjoint per-pipe flow: a source feeds a cable trunk that machines tap, and
the amperage **sums** along the shared segments toward the source (docs/DOMAIN.md). This router
turns each per-tier power net (one source endpoint + that tier's machine endpoints, synthesized
by the adapter) into a trunk and sizes every segment to the amperage flowing through it.

ASCII (one tier, a path-trunk; thickness sized to the summed downstream amperage)::

    source ===4x=== m0(1A) ==2x== m1(1A) ==1x== m2(1A)
           |<- 3A ->|<-- 2A --->|<-- 1A -->|

Crude on purpose (correctness-first, the handoff sequencing): **one source per tier**, the trunk
is a path through the machines in endpoint order (A* per leg over the shared ``_grid``, each leg
avoiding the cells already laid so the legs never overlap and the trunk stays a tree), and a leg
needing **> 16x** is rejected (Phase 2 adds multi-source / parallel-run / voltage-upgrade
optimization). The validator independently re-derives the per-segment amperage and checks the
thickness, so a sizing bug here is caught, not certified.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.dataset import UnknownTierError, amperage
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Infeasibility,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell

from ._grid import astar, coord, dock, obstacle_cells

#: GT cable thicknesses, smallest first (16x is the hard cap - docs/DOMAIN.md).
_THICKNESSES = (1, 2, 4, 8, 16)
_MAX_THICKNESS = _THICKNESSES[-1]


@dataclass(frozen=True)
class PowerRouteResult:
    """Power router output: all power routes, or a partial set plus why it stalled.

    ``failed_nets`` names the power net that stalled (empty when ``ok``), so the solver's
    place<->route feedback loop can penalize it and re-place (helps a dock/path failure; an
    amperage/tier failure is not placement-fixable, but the loop's cycle detection stops quickly).
    """

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None
    failed_nets: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True iff every (non-ME) power net was routed and amperage-feasible."""
        return self.infeasibility is None


def route_power(
    problem: InputIR,
    placements: Sequence[Placement],
    *,
    extra_obstacles: Collection[Cell] = (),
) -> PowerRouteResult:
    """Route each per-tier power net of ``problem`` as a shared-amperage trunk over ``placements``.

    ``extra_obstacles`` are cells already taken by other routes (the item/fluid pipes the solver
    laid first), so power cables never share a cell with them - the crude single-channel capacity.
    Each tier's trunk is likewise added to the obstacle set before the next tier routes, so two
    power trunks never collide either.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine: dict[str, Placement] = {}
    for placement in placements:
        placement_by_machine.setdefault(placement.machine_id, placement)  # one placement/machine
    region = problem.bounding_region
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}

    if problem.me_toggles.toggled(Commodity.POWER):
        return PowerRouteResult()  # power is on the ME network; nothing to route

    obstacles = obstacle_cells(problem, placements, machines) | set(extra_obstacles)
    docked: set[Cell] = set()
    routes: list[Route] = []
    for net in problem.nets:
        if net.commodity is not Commodity.POWER:
            continue
        sources = [e for e in net.endpoints if port_dir.get(_key(e)) is IODirection.OUTPUT]
        sinks = [e for e in net.endpoints if port_dir.get(_key(e)) is IODirection.INPUT]
        if len(sources) != 1 or not sinks:
            return PowerRouteResult(tuple(routes), _malformed(net.id), (net.id,))

        terminals = _dock_all(
            [sources[0], *sinks], placement_by_machine, machines, obstacles, docked, region
        )
        if terminals is None:
            return PowerRouteResult(tuple(routes), _no_dock(net.id), (net.id,))

        try:
            amps = [
                amperage(machines[e.machine_id].eut, machines[e.machine_id].voltage_tier)
                for e in sinks
            ]
        except UnknownTierError as exc:
            return PowerRouteResult(tuple(routes), _unknown_tier(net.id, str(exc)), (net.id,))

        built = _build_trunk([t.cell for t in terminals], amps, obstacles, region)
        if isinstance(built, Infeasibility):
            return PowerRouteResult(tuple(routes), _tag(built, net.id), (net.id,))
        segments, thickness = built
        routes.append(
            Route(
                net_id=net.id,
                commodity=Commodity.POWER,
                terminals=terminals,
                segments=segments,
                thickness_per_segment=thickness,
            )
        )
        # Capacity: this trunk now owns these cells, so the next tier's trunk routes around it.
        for seg in segments:
            obstacles.add((seg.start.x, seg.start.y, seg.start.z))
            obstacles.add((seg.end.x, seg.end.y, seg.end.z))
    return PowerRouteResult(tuple(routes))


def _key(endpoint: MachineFaceRef) -> tuple[str, str]:
    return (endpoint.machine_id, endpoint.port_id)


def _dock_all(
    endpoints: Sequence[MachineFaceRef],
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> list[Terminal] | None:
    """A terminal per endpoint (source first), or None if any endpoint cannot dock/place."""
    terminals: list[Terminal] = []
    for ep in endpoints:
        placement = placement_by_machine.get(ep.machine_id)
        machine = machines.get(ep.machine_id)
        if placement is None or machine is None:
            return None
        terminal = dock(ep.port_id, placement, machine, obstacles, docked, region)
        if terminal is None:
            return None
        docked.add((terminal.cell.x, terminal.cell.y, terminal.cell.z))
        terminals.append(terminal)
    return terminals


def _build_trunk(
    cells: Sequence[CellCoord], amps: Sequence[int], obstacles: set[Cell], region: CellBox
) -> tuple[list[Segment], list[int]] | Infeasibility:
    """Chain source->m0->m1->... ; size each leg to the amperage of the machines downstream of it.

    ``cells`` is ``[source, m0, m1, ...]``; ``amps[i]`` is the draw of machine ``m_i``. Leg ``i``
    (between ``cells[i]`` and ``cells[i+1]``) carries every machine from ``m_i`` onward, so its
    load is ``sum(amps[i:])`` - the suffix sum that defines a shared-amperage trunk.

    Each laid leg's cells become obstacles for the legs that follow, so two legs never share a
    cell: the trunk is always a simple, non-self-crossing path (a tree the validator can root at
    the source and re-derive). Without this, A*-ing each leg independently could overlap legs into
    a tangle whose per-segment amperage is undefined - which the validator then rejects as
    POWER_ROUTE_NOT_A_TREE rather than certify (the start cell of each leg is the previous leg's
    end, which A* never tests against obstacles, so chaining still connects).
    """
    segments: list[Segment] = []
    thickness: list[int] = []
    blocked = set(obstacles)  # grows with each laid leg to keep the trunk a non-overlapping path
    for i, (a, b) in enumerate(pairwise(cells)):
        path = astar((a.x, a.y, a.z), (b.x, b.y, b.z), blocked, region)
        if path is None:
            return Infeasibility(
                constraint="routing",
                detail="no free cell path between power terminals",
                suggested_relaxation="enlarge the bounding region or reduce obstacles",
            )
        leg_load = sum(amps[i:])  # machines m_i.. are downstream of this leg
        if leg_load > _MAX_THICKNESS:
            return Infeasibility(
                constraint="amperage",
                detail=f"a cable segment must carry {leg_load} amps, over the 16x cable cap",
                suggested_relaxation="split into parallel runs or use a higher voltage tier "
                "(more power per amp) - Phase 2 multi-source optimization",
            )
        thick = _cable_thickness(leg_load)
        for c0, c1 in pairwise(path):
            segments.append(Segment(start=coord(c0), end=coord(c1), channel=0))
            thickness.append(thick)
        blocked.update(path)  # later legs must avoid these cells so the graph stays a tree
    return segments, thickness


def _cable_thickness(load: int) -> int:
    """Smallest cable thickness (1/2/4/8/16) that carries ``load`` amps (>=1 even for 0)."""
    for t in _THICKNESSES:
        if load <= t:
            return t
    return _MAX_THICKNESS  # the caller already rejected load > 16


def _malformed(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="power_net",
        detail=f"power net {net_id!r} is not one source + >=1 sink (cannot form a trunk)",
        suggested_relaxation="check the power synthesis: each tier net needs exactly one source",
    )


def _no_dock(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="face_reachability",
        detail=f"power net {net_id!r} could not dock a terminal (no free non-front face cell)",
        suggested_relaxation="free up adjacent cells, or leave routing gaps around machines",
    )


def _unknown_tier(net_id: str, tier: str) -> Infeasibility:
    return Infeasibility(
        constraint="voltage_tier",
        detail=f"power net {net_id!r} serves an unknown voltage tier {tier}",
        suggested_relaxation="add the tier to dataset.VOLTAGE_BY_TIER, or fix the export tier",
    )


def _tag(infeasibility: Infeasibility, net_id: str) -> Infeasibility:
    """Prefix a trunk-building infeasibility with the net id (which _build_trunk does not know)."""
    return infeasibility.model_copy(
        update={"detail": f"power net {net_id!r}: {infeasibility.detail}"}
    )
