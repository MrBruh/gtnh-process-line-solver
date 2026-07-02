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
from gtnh_solver.ir.geometry import front_on_boundary
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
    ViolationCode.POWER_FEED_NOT_ON_BOUNDARY,
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
    # A 6-spoke star: a hub can seat only 4 spokes at distance 1, so some spoke nets must stay
    # non-minimal. Heavily penalizing one net (the place<->route feedback signal for an unrouted
    # net) spends the scarce adjacency on it: the penalized net ends up at least as tight as every
    # other, and strictly tighter than the loosest - the unavoidable slack fell on an un-penalized
    # net, not the penalized one.
    problem = _star(6, region=CellBox(sx=5, sy=1, sz=5))
    penalized = optimize_placement(problem, seed=0, net_penalties={"n0": 50.0})
    assert penalized.ok
    n0 = _net_hpwl(problem, penalized.placements, "n0")
    others = [_net_hpwl(problem, penalized.placements, f"n{i}") for i in range(1, 6)]
    assert n0 <= min(others)  # the penalized net is at least as tight as every un-penalized one
    assert n0 < max(others)  # and strictly tighter than the loosest: the slack fell elsewhere


def test_optimize_is_deterministic_per_seed() -> None:
    problem = _star(4)
    assert (
        optimize_placement(problem, seed=7).placements
        == optimize_placement(problem, seed=7).placements
    )


def test_lns_scales_and_stays_valid_on_a_larger_star() -> None:
    # A 9-spoke star exercises the LNS ruin-and-recreate move (a hub + its net-neighbours are a
    # natural related cluster): the optimizer must still emit a complete, validator-clean placement
    # and beat the first-fit row.
    problem = _star(9, region=CellBox(sx=7, sy=2, sz=7))
    crude = place(problem)
    optimized = optimize_placement(problem, seed=0)
    assert optimized.ok
    assert len(optimized.placements) == len(problem.machines)
    assert _validates(problem, optimized.placements)
    assert _total_hpwl(problem, optimized.placements) < _total_hpwl(problem, crude.placements)


def test_lns_handles_netless_machines_via_random_ruin() -> None:
    # With no nets the adjacency is empty, so LNS ruin cannot grow a related cluster and pads it
    # with a random selection; recreate (no net-neighbours to bias toward) still yields a complete,
    # validator-clean placement rather than crashing or dropping a machine.
    machines = [_hub(f"m{i}") for i in range(4)]
    problem = InputIR(bounding_region=CellBox(sx=6, sy=1, sz=6), machines=machines, nets=[])
    result = optimize_placement(problem, seed=2)
    assert result.ok
    assert len(result.placements) == 4
    assert _validates(problem, result.placements)


def test_lns_fills_a_tight_region_using_the_first_fit_fallback() -> None:
    # A region with exactly enough cells for the machines: after a ruin, a machine's freed origin
    # can be retaken by an earlier re-insert and no neighbour cell is free, so recreate must fall
    # back to a first-fit scan for any open slot. The result stays complete and validator-clean.
    problem = _star(3, region=CellBox(sx=2, sy=1, sz=2))  # 4 machines, 4 cells: fully packed
    result = optimize_placement(problem, seed=0)
    assert result.ok
    assert len(result.placements) == 4
    assert _validates(problem, result.placements)


def _powered_star(n_spokes: int = 3) -> InputIR:
    """A hub feeding powered spokes plus a power source + per-tier net (the adapter's shape)."""
    spokes = [
        Machine(
            id=f"s{i}",
            type="spoke",
            voltage_tier="LV",
            eut=32.0,
            orientation_options=[Facing.NORTH, Facing.SOUTH],
            faces=FaceSpec(
                ports=[
                    Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                    Port(id="pin", commodity=Commodity.POWER, direction=IODirection.INPUT),
                ]
            ),
        )
        for i in range(n_spokes)
    ]
    source = Machine(
        id="psrc",
        type="Power Source (LV)",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST],
        faces=FaceSpec(
            ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
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
    nets.append(
        Net(
            id="power:LV",
            commodity=Commodity.POWER,
            throughput=32.0 * n_spokes,
            endpoints=[
                MachineFaceRef(machine_id="psrc", port_id="po"),
                *(MachineFaceRef(machine_id=f"s{i}", port_id="pin") for i in range(n_spokes)),
            ],
        )
    )
    return InputIR(
        bounding_region=CellBox(sx=8, sy=2, sz=8),
        machines=[_hub("hub"), *spokes, source],
        nets=nets,
    )


def test_optimize_keeps_the_power_source_feed_on_the_boundary() -> None:
    # Every move must keep the source's front (feed) face flush on a region wall - the hard
    # constraint the validator enforces. Several seeds so relocate/swap/reorient and the LNS
    # recreate all get exercised against it.
    problem = _powered_star()
    machine = next(m for m in problem.machines if m.id == "psrc")
    for seed in range(4):
        result = optimize_placement(problem, seed=seed)
        assert result.ok
        src = next(p for p in result.placements if p.machine_id == "psrc")
        assert front_on_boundary(
            src.cell, machine.footprint, src.orientation, problem.bounding_region
        ), f"seed {seed}: source feed face left the boundary"
        assert _validates(problem, result.placements)


def test_power_net_penalty_switches_on_the_mst_pull() -> None:
    # A power net has no base cost term (real cable cost is judged by the solver on routed
    # layouts), so only a feedback penalty - the router failed the net - activates its MST pull.
    # Under a heavy penalty the source must end up hugging its sinks: the trunk the router could
    # not lay gets the shortest possible tree to try again with.
    problem = _powered_star()
    pulled = optimize_placement(problem, seed=1, net_penalties={"power:LV": 50.0})
    assert pulled.ok
    pos = {p.machine_id: p.cell for p in pulled.placements}
    src = pos["psrc"]
    nearest = min(
        abs(src.x - pos[f"s{i}"].x) + abs(src.y - pos[f"s{i}"].y) + abs(src.z - pos[f"s{i}"].z)
        for i in range(3)
    )
    assert nearest <= 2, f"penalized power net left the source {nearest} cells from its sinks"
    assert _validates(problem, pulled.placements)


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
