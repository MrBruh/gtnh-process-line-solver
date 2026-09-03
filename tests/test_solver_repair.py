"""Tests for the post-placement power-source repair pass (solver.repair).

Headline: the annealer has no gradient on a power source (#123), so after the pipes are laid each
source is offered the free cells around the sinks it feeds and **every candidate is really
routed** - the pass takes the shortest cable it can actually build. Plus the invariant that makes
it safe to run on every attempt: it is ranked by the feedback loop's own quality key, so it either
improves the structure or leaves it exactly alone, and it never proposes a pose the validator
would reject (off-wall feed face, undeclared orientation, a body on a pipe or a reserved cell).
"""

from __future__ import annotations

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    METoggles,
    Net,
    PinnedIO,
    Placement,
    Port,
    Route,
    Segment,
)
from gtnh_solver.ir.geometry import front_on_boundary
from gtnh_solver.placement import Objective
from gtnh_solver.router import route_power
from gtnh_solver.solver._structure import structure_quality
from gtnh_solver.solver.repair import repair_power_sources
from tests._helpers import at, power_source

_POWER = Commodity.POWER
_OBJECTIVES: tuple[Objective, ...] = ("footprint", "volume", "balanced")


def _load(mid: str, *, eut: float = 32.0) -> Machine:
    """An LV machine drawing ``eut`` through a single power input."""
    return Machine(
        id=mid,
        type="M",
        voltage_tier="LV",
        eut=eut,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[Port(id="power:in", commodity=_POWER, direction=IODirection.INPUT)]),
    )


def _pnet(source: str, sink: str, *, net_id: str = "power:LV") -> Net:
    return Net(
        id=net_id,
        commodity=_POWER,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=source, port_id="power:out"),
            MachineFaceRef(machine_id=sink, port_id="power:in"),
        ],
    )


def _stranded(
    *,
    reserved: list[CellCoord] | None = None,
    pinned: list[PinnedIO] | None = None,
    me_toggles: METoggles | None = None,
) -> InputIR:
    """A source parked at the far wall from the one machine it feeds, in an 8x1x2 corridor.

    Every cell has ``y == 0``, so the only wall a NORTH-facing feed face can sit on is ``z == 0``:
    the source's candidate ground comes down to the single free cell beside the load. The
    un-repaired layout has to drag cable the length of the corridor to reach it.
    """
    return InputIR(
        bounding_region=CellBox(sx=8, sy=1, sz=2),
        machines=[power_source("src", orientations=[Facing.NORTH]), _load("k")],
        nets=[_pnet("src", "k")],
        reserved_cells=reserved or [],
        pinned=pinned or [],
        me_toggles=me_toggles or METoggles(),
    )


#: The stranded corridor's starting placement: source at one wall, its load at the other.
_STRANDED_START = [at("src", 0, 0, 0), at("k", 7, 0, 0)]


def _cable(problem: InputIR, placements: list[Placement]) -> int:
    """Power cable cells these placements cost as they stand - the un-repaired baseline."""
    power = route_power(problem, placements)
    return len({cell for r in power.routes for cell in r.cells()})


def _repair(
    problem: InputIR,
    placements: list[Placement],
    *,
    item_routes: tuple[Route, ...] = (),
    objective: Objective = "footprint",
) -> tuple[list[Placement], int]:
    """Run the pass and return ``(placements, power cable cells)`` - the two things it decides."""
    repaired, power = repair_power_sources(
        problem,
        placements,
        item_routes=item_routes,
        claimed_cells={},
        objective=objective,
    )
    return repaired, len({cell for r in power.routes for cell in r.cells()})


def _source(placements: list[Placement]) -> Placement:
    return next(p for p in placements if p.machine_id == "src")


def test_repair_pulls_a_stranded_source_onto_its_load() -> None:
    # The bug #123 names: nothing in the placement cost pays for the trunk a source drags behind
    # it, so the annealer can leave it at the far wall. The repair routes each candidate for real
    # and takes the one cell that docks straight onto the load.
    problem = _stranded()
    baseline = _cable(problem, _STRANDED_START)
    after, cable = _repair(problem, _STRANDED_START)

    assert _source(after).cell == CellCoord(x=6, y=0, z=0), "source did not move next to its load"
    assert baseline == 6, "fixture is not actually stranded"
    # Docked side by side, the whole net is the source's own dock cell plus the one the load taps.
    assert cable == 2, f"a docked source needs two cable cells, got {cable}"


def test_repair_leaves_an_already_tight_layout_alone() -> None:
    # The invariant that lets this run on every attempt: with the source already docked there is
    # no strictly-better candidate, so the pass returns the placements untouched rather than
    # shuffling to an equal-cost pose (which would churn the layout for nothing).
    problem = _stranded()
    tight = [at("src", 6, 0, 0), at("k", 7, 0, 0)]
    after, cable = _repair(problem, tight)

    assert after == tight
    assert cable == 2


def test_repair_is_deterministic() -> None:
    # Sources are swept in problem order and poses in sorted order precisely so a given placement
    # always yields the same repair - the solver's whole determinism promise runs through here.
    problem = _stranded()
    first, first_cable = _repair(problem, _STRANDED_START)
    second, second_cable = _repair(problem, _STRANDED_START)

    assert first == second
    assert first_cable == second_cable


def test_repair_keeps_the_feed_face_on_the_boundary_and_a_declared_orientation() -> None:
    # A relocated source is still a source: its front face is the reserved external-feed face and
    # must stay flush on a region wall, and its facing must be one it actually declares. Both are
    # validator-enforced, so a pass that ignored either would hand back invalid layouts.
    problem = _stranded()
    machine = next(m for m in problem.machines if m.id == "src")
    after, _ = _repair(problem, _STRANDED_START)
    moved = _source(after)

    assert moved.orientation in machine.orientation_options
    assert front_on_boundary(
        moved.cell, machine.footprint, moved.orientation, problem.bounding_region
    )


def test_repair_will_not_stand_a_source_on_a_laid_pipe() -> None:
    # The pipes are routed before power, and a machine body over a route cell is a validator
    # violation - so the one cell that would otherwise win is off the table and the source stays.
    problem = _stranded()
    pipe = Route(
        net_id="n",
        commodity=Commodity.ITEM,
        segments=[Segment(start=CellCoord(x=6, y=0, z=0), end=CellCoord(x=6, y=0, z=0), channel=0)],
    )
    after, _ = _repair(problem, _STRANDED_START, item_routes=(pipe,))

    assert _source(after).cell == CellCoord(x=0, y=0, z=0), "source moved onto a pipe cell"


def test_repair_will_not_stand_a_source_on_a_reserved_cell() -> None:
    # The same rule for ground the builder declared off-limits, which the pass sees through the
    # problem rather than through the routes.
    problem = _stranded(reserved=[CellCoord(x=6, y=0, z=0)])
    after, _ = _repair(problem, _STRANDED_START)

    assert _source(after).cell == CellCoord(x=0, y=0, z=0), "source moved onto a reserved cell"


def test_repair_leaves_a_pinned_power_net_alone() -> None:
    # A pinned I/O constrains the net's route to a cell the power router does not itself model,
    # so moving its source could invalidate a layout this pass cannot re-check. It declines.
    problem = _stranded(
        pinned=[PinnedIO(net_id="power:LV", cell=CellCoord(x=1, y=0, z=0), kind=IODirection.OUTPUT)]
    )
    after, _ = _repair(problem, _STRANDED_START)

    assert after == _STRANDED_START


def test_repair_is_a_no_op_when_power_rides_the_me_network() -> None:
    # ME-toggled power is not physically routed at all, so there is no cable to shorten and
    # nothing to move.
    problem = _stranded(me_toggles=METoggles(power=True))
    after, cable = _repair(problem, _STRANDED_START)

    assert after == _STRANDED_START
    assert cable == 0


def test_repair_never_worsens_the_loops_quality_key() -> None:
    # The safety property behind running this on every attempt: candidates are ranked by exactly
    # the key the feedback loop ranks whole attempts on, so a repaired layout is never one the
    # loop would have liked less - the compactness terms included, not just the cable.
    problem = _stranded()
    for objective in _OBJECTIVES:
        before = structure_quality(
            problem,
            _STRANDED_START,
            list(route_power(problem, _STRANDED_START).routes),
            objective,
        )
        repaired, power = repair_power_sources(
            problem,
            _STRANDED_START,
            item_routes=(),
            claimed_cells={},
            objective=objective,
        )
        after = structure_quality(problem, repaired, list(power.routes), objective)
        assert after <= before, f"{objective}: repair worsened the quality key {before} -> {after}"


def test_repair_moves_every_source_of_a_multi_tier_build() -> None:
    # Two tiers, two sources, both parked at the far wall from the loads they feed: the sweep is
    # per-source and each one is re-scored against the state the previous one left, so both get
    # pulled in - one stranded source must not mask the other.
    problem = InputIR(
        bounding_region=CellBox(sx=12, sy=2, sz=3),
        machines=[
            power_source("src", orientations=[Facing.NORTH, Facing.SOUTH]),
            power_source("src2", orientations=[Facing.NORTH, Facing.SOUTH]),
            _load("k"),
            _load("k2"),
        ],
        nets=[_pnet("src", "k"), _pnet("src2", "k2", net_id="power:MV")],
    )
    before = [at("src", 0, 0, 0), at("src2", 2, 0, 0), at("k", 8, 0, 0), at("k2", 10, 0, 0)]
    baseline = _cable(problem, before)
    after, cable = _repair(problem, before)
    moved = {p.machine_id: p.cell for p in after}

    assert moved["src"] != CellCoord(x=0, y=0, z=0), "the LV source was left stranded"
    assert moved["src2"] != CellCoord(x=2, y=0, z=0), "the MV source was left stranded"
    assert cable < baseline
    assert (baseline, cable) == (18, 4)


def test_repair_leaves_a_placement_whose_power_will_not_route_untouched() -> None:
    # A placement that cannot carry its power is on its way to the feedback loop as a *diagnosis*
    # ("these nets failed, penalize them and re-place"). Relocating sources first would change
    # which nets fail and tell the loop a different story each attempt - and since it gives up
    # early when a failed-net set repeats, that churn cost nitrobenzene/balanced its valid layout
    # outright. So a broken layout is handed back exactly as it came in.
    problem = InputIR(
        bounding_region=CellBox(sx=9, sy=1, sz=2),
        machines=[
            power_source("src", orientations=[Facing.NORTH]),
            power_source("src2", orientations=[Facing.NORTH]),
            _load("k"),
            _load("k2"),
        ],
        nets=[_pnet("src", "k"), _pnet("src2", "k2", net_id="power:MV")],
    )
    before = [at("src", 0, 0, 0), at("src2", 1, 0, 0), at("k", 5, 0, 0), at("k2", 8, 0, 0)]
    repaired, power = repair_power_sources(
        problem, before, item_routes=(), claimed_cells={}, objective="footprint"
    )

    assert power.failed_nets, "fixture no longer exercises an unroutable power net"
    assert repaired == before
