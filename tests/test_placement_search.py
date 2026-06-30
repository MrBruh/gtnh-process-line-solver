"""Tests for the Phase 2 simulated-annealing placer (`placement.search`).

It must (a) only ever emit validator-clean placements, (b) be deterministic per seed, and
(c) actually improve the routing-aware cost over the crude first-fit seed - shown on a star,
where first-fit strings the spokes out in a row but the optimizer clusters them around the hub.
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
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    Placement,
    Port,
)
from gtnh_solver.placement import optimize_placement, place
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_PLACEMENT_CODES = {
    ViolationCode.MACHINE_OVERLAP,
    ViolationCode.MACHINE_OUT_OF_BOUNDS,
    ViolationCode.MACHINE_ON_RESERVED,
    ViolationCode.BAD_ORIENTATION,
    ViolationCode.PLACEMENT_COUNT_MISMATCH,
    ViolationCode.UNKNOWN_MACHINE,
}


def _hub(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="hub",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH],  # >1 so reorient moves are exercised
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )


def _spoke(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="spoke",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
        ),
    )


def _star(n_spokes: int = 4, *, region: CellBox | None = None) -> InputIR:
    """A hub feeding `n_spokes` consumers - first-fit rows them, the optimizer should cluster."""
    spokes = [_spoke(f"s{i}") for i in range(n_spokes)]
    nets = [
        Net(
            id=f"n{i}",
            commodity=Commodity.ITEM,
            fluid_or_item="x",
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id="hub", port_id="out"),
                MachineFaceRef(machine_id=f"s{i}", port_id="in"),
            ],
        )
        for i in range(n_spokes)
    ]
    return InputIR(
        bounding_region=region if region is not None else CellBox(sx=8, sy=2, sz=8),
        machines=[_hub("hub"), *spokes],
        nets=nets,
    )


def _total_hpwl(problem: InputIR, placements: tuple[Placement, ...]) -> float:
    pos = {p.machine_id: p for p in placements}
    sizes = {m.id: m.footprint for m in problem.machines}
    total = 0.0
    for net in problem.nets:
        centers = [
            (
                pos[e.machine_id].cell.x + sizes[e.machine_id].sx / 2,
                pos[e.machine_id].cell.y + sizes[e.machine_id].sy / 2,
                pos[e.machine_id].cell.z + sizes[e.machine_id].sz / 2,
            )
            for e in net.endpoints
            if e.machine_id in pos
        ]
        if len(centers) < 2:
            continue
        for axis in range(3):
            coords = [c[axis] for c in centers]
            total += max(coords) - min(coords)
    return total


def _net_hpwl(problem: InputIR, placements: tuple[Placement, ...], net_id: str) -> float:
    pos = {p.machine_id: p for p in placements}
    sizes = {m.id: m.footprint for m in problem.machines}
    net = next(n for n in problem.nets if n.id == net_id)
    centers = [
        (
            pos[e.machine_id].cell.x + sizes[e.machine_id].sx / 2,
            pos[e.machine_id].cell.y + sizes[e.machine_id].sy / 2,
            pos[e.machine_id].cell.z + sizes[e.machine_id].sz / 2,
        )
        for e in net.endpoints
        if e.machine_id in pos
    ]
    if len(centers) < 2:
        return 0.0
    return sum(max(c[a] for c in centers) - min(c[a] for c in centers) for a in range(3))


def _validates(problem: InputIR, placements: tuple[Placement, ...]) -> bool:
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=list(placements))
    return _PLACEMENT_CODES.isdisjoint(validate(problem, layout).codes())


def test_optimize_improves_wirelength_over_first_fit() -> None:
    problem = _star(4)
    crude = place(problem)
    optimized = optimize_placement(problem, seed=0)
    assert optimized.ok
    assert _total_hpwl(problem, optimized.placements) < _total_hpwl(problem, crude.placements)


def test_optimize_output_is_validator_clean() -> None:
    problem = _star(5)
    result = optimize_placement(problem, seed=3)
    assert result.ok
    assert _validates(problem, result.placements)


def test_net_penalty_pulls_the_penalized_net_tighter() -> None:
    # A 5-spoke star: the hub can't sit adjacent to every spoke, so by default some spoke net is
    # non-minimal. Penalizing one net (the place<->route feedback signal for an unrouted net) makes
    # the optimizer pull that spoke adjacent to the hub - its wirelength strictly shrinks.
    problem = _star(5, region=CellBox(sx=5, sy=1, sz=5))
    base = optimize_placement(problem, seed=0)
    penalized = optimize_placement(problem, seed=0, net_penalties={"n0": 50.0})
    assert base.ok
    assert penalized.ok
    assert _net_hpwl(problem, penalized.placements, "n0") < _net_hpwl(
        problem, base.placements, "n0"
    )


def test_optimize_is_deterministic_per_seed() -> None:
    problem = _star(4)
    assert (
        optimize_placement(problem, seed=7).placements
        == optimize_placement(problem, seed=7).placements
    )


def test_optimize_respects_reserved_and_bounds() -> None:
    problem = _star(3, region=CellBox(sx=4, sy=1, sz=3)).model_copy(
        update={"reserved_cells": [CellCoord(x=0, y=0, z=0)]}
    )
    result = optimize_placement(problem, seed=1)
    assert result.ok
    assert _validates(problem, result.placements)


def test_optimize_single_machine_returns_constructive_seed() -> None:
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4), machines=[_hub("only")], nets=[])
    assert optimize_placement(problem, seed=0).placements == place(problem).placements


def test_optimize_passes_through_infeasibility() -> None:
    # two 1x1x1 machines into a 1x1x1 region - the seed already can't fit; optimizer surfaces it
    problem = InputIR(
        bounding_region=CellBox(sx=1, sy=1, sz=1), machines=[_hub("a"), _spoke("b")], nets=[]
    )
    result = optimize_placement(problem, seed=0)
    assert not result.ok
    assert result.infeasibility is not None
