"""Tests for the Phase 2 simulated-annealing placer (`placement.search`).

It must (a) only ever emit validator-clean placements, (b) be deterministic per seed, and
(c) actually improve the routing-aware cost over the crude first-fit seed - shown on a star,
where first-fit strings the spokes out in a row but the optimizer clusters them around the hub.
"""

from __future__ import annotations

import math
import random

import pytest

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
from gtnh_solver.ir.geometry import (
    FACE_DELTAS,
    Cell,
    Pose,
    box_in_region,
    front_on_boundary,
    in_region,
    occupied_cells,
    pose_of,
    rotated_footprint,
)
from gtnh_solver.placement import optimize_placement, place
from gtnh_solver.placement.search import (
    _apply_occupied_delta,
    _AutoPair,
    _body,
    _box_offsets,
    _dockable_cells,
    _Extent,
    _marginal_insertion_cost,
    _nudge,
    _occupancy_grid,
    _placed_extent,
    _placed_invariants,
    _placement,
    _relocate,
    _SearchContext,
    _turn_fits,
)
from gtnh_solver.validator import validate
from tests._helpers import PLACEMENT_CODES, at, power_source


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
    return PLACEMENT_CODES.isdisjoint(validate(problem, layout).codes())


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


def test_optimize_objective_selects_the_compactness_weights() -> None:
    # The objective (footprint | volume | balanced) picks the compactness weights: each mode must
    # yield a complete, validator-clean placement, deterministically per seed. The layout-shape
    # semantics (stack tall vs stay flat) are asserted end to end on sand in test_solver.
    problem = _star(5)
    for objective in ("footprint", "volume", "balanced"):
        result = optimize_placement(problem, seed=2, objective=objective)
        again = optimize_placement(problem, seed=2, objective=objective)
        assert result.ok
        assert result.placements == again.placements
        assert _validates(problem, result.placements)


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
    source = power_source(
        "psrc",
        orientations=[Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST],
        port_id="po",  # the power net below wires this exact port id
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


def test_apply_occupied_delta_survives_lns_reordering_with_multiblock_footprints() -> None:
    # Regression: an accepted LNS move returns the placement list REORDERED (kept machines
    # first, reinserted ones appended), so the delta must diff by machine id. An index-paired
    # diff vacates a machine that never moved and expands the wrong machine's footprint at the
    # new cell - invisible while every footprint is 1x1x1, silently wrong once multiblocks land.
    wide = Machine(
        id="wide",
        type="wide",
        footprint=CellBox(sx=2, sy=1, sz=1),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
    )
    machines = {m.id: m for m in (_hub("a"), wide, _spoke("c"))}

    def on_row(mid: str, x: int) -> Pose:
        return pose_of(at(mid, x, 0, 0))

    def rebuild(placements: list[Pose]) -> set[tuple[int, int, int]]:
        return {
            c
            for p in placements
            for c in occupied_cells(
                CellCoord(x=p.cell[0], y=p.cell[1], z=p.cell[2]),
                machines[p.machine_id].footprint,
                p.orientation,
            )
        }

    before = [on_row("a", 0), on_row("wide", 2), on_row("c", 5)]
    # LNS ruined + reinserted the wide machine: kept a and c keep their cells but shift to the
    # front of the list, and the wide machine lands at the tail with a new origin.
    after = [before[0], before[2], on_row("wide", 7)]
    occupied = rebuild(before)
    _apply_occupied_delta(occupied, before, after, {k: _body(m) for k, m in machines.items()})
    assert occupied == rebuild(after)


def test_apply_occupied_delta_follows_a_reorient_that_never_moves_the_cell() -> None:
    # The other half of the same hazard, and the one rotation introduced. A reorient leaves the
    # origin alone, so a diff keyed on the CELL sees no change - while a quarter turn moves every
    # cell a non-cubic machine covers. The delta would then drift out of sync with the layout for
    # the rest of the anneal, and every later overlap test would be run against a stale set.
    wide = Machine(
        id="wide",
        type="wide",
        footprint=CellBox(sx=3, sy=1, sz=1),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.EAST],
    )
    machines = {m.id: m for m in (_hub("a"), wide)}

    def rebuild(placements: list[Pose]) -> set[tuple[int, int, int]]:
        return {
            c
            for p in placements
            for c in occupied_cells(
                CellCoord(x=p.cell[0], y=p.cell[1], z=p.cell[2]),
                machines[p.machine_id].footprint,
                p.orientation,
            )
        }

    before = [pose_of(at("a", 0, 0, 0)), pose_of(at("wide", 4, 0, 0))]
    turned = Pose("wide", before[1].cell, Facing.EAST)
    after = [before[0], turned]
    assert before[1].cell == turned.cell, "the premise: a reorient does not move the origin"

    occupied = rebuild(before)
    _apply_occupied_delta(occupied, before, after, {k: _body(m) for k, m in machines.items()})
    assert occupied == rebuild(after)
    assert rebuild(before) != rebuild(after), "a turned 3x1x1 must cover different cells"


def _fit_ctx(
    region: CellBox, machines: list[Machine], reserved: set[Cell] | None = None
) -> _SearchContext:
    """A context carrying only what a fit test reads: the region, the machines, the reserved cells.

    The net views stay empty - the two moves exercised below decide on geometry alone, and giving
    them nets would only obscure which rejection the test is actually pinning.
    """
    return _SearchContext(
        bodies={m.id: _body(m) for m in machines},
        region=region,
        bounds=(region.sx, region.sy, region.sz),
        reserved=reserved or set(),
        adjacency={},
        machine_nets={},
        machine_power={},
        machine_auto={},
        weights=(1.0, 0.02),
    )


def _wide(mid: str = "wide") -> Machine:
    return Machine(
        id=mid,
        type="wide",
        footprint=CellBox(sx=3, sy=1, sz=1),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.EAST],
    )


def test_turn_fits_rejects_a_turn_that_swings_the_body_out_of_the_region() -> None:
    """A quarter turn swaps a non-cubic machine's horizontal extents, so a turn that sits happily
    along x can need room along z the region does not have. The test is on the rotated box rather
    than on the turned cells (issue #110) and has to give the cell walk's answer at the wall.
    """
    wide = _body(_wide())
    p = pose_of(at("wide", 0, 0, 0))  # facing NORTH: 3 of x, 1 of z
    slot = _fit_ctx(CellBox(sx=3, sy=1, sz=1), [wide.machine])
    assert _turn_fits(wide, p, Facing.NORTH, slot, set())  # same extents: the short-circuit
    assert not _turn_fits(wide, p, Facing.EAST, slot, set())  # turned it needs 3 of z, there is 1
    room = _fit_ctx(CellBox(sx=3, sy=1, sz=3), [wide.machine])
    assert _turn_fits(wide, p, Facing.EAST, room, set())
    # In-region is necessary, not sufficient: the swept cells must also be free of everyone else.
    # `occupied` is the whole layout's set, so the machine's own cells are excluded by the move.
    assert not _turn_fits(wide, p, Facing.EAST, room, {(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 0, 2)})


def test_relocate_rejects_an_origin_whose_feed_turn_leaves_the_region() -> None:
    """The one path where a legally drawn origin still cannot take the body: a *source* that has to
    be re-oriented to keep its feed face on the boundary is placed with extents the draw never
    bounded (``_rand_origin`` bounds with the CURRENT orientation), and the turn can overrun.

    A 1x1x3 source in a 1x1x8 corridor: only ``z == 0`` puts a NORTH front on the boundary and a
    reserved cell blocks it, so every other draw turns WEST - which needs 3 of x where there is 1.
    """
    tall = Machine(
        id="src",
        type="Power Source (LV)",
        footprint=CellBox(sx=1, sy=1, sz=3),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.WEST],
        faces=FaceSpec(
            ports=[Port(id="power:out", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
    assert tall.is_power_source, "the premise: only a source is ever re-oriented for its feed face"
    ctx = _fit_ctx(CellBox(sx=1, sy=1, sz=8), [tall], reserved={(0, 0, 0)})
    placed = [pose_of(at("src", 0, 0, 5))]
    occupied = {(0, 0, 5), (0, 0, 6), (0, 0, 7)}
    assert _relocate(placed, ctx, occupied, random.Random(0)) is None
    assert occupied == {(0, 0, 5), (0, 0, 6), (0, 0, 7)}, "the move must restore what it borrowed"


# ------------------------------------------------------- the marginal-cost pruning bound


def _cost_ctx(machines: list[Machine], region: CellBox) -> _SearchContext:
    """A context whose nets make every term of the marginal cost non-trivial.

    The bound below is only interesting when wire, cable and auto all contribute: a machine wired
    to two others, power-penalized against them, and auto-output-paired with both.
    """
    ids = [m.id for m in machines]
    return _SearchContext(
        bodies={m.id: _body(m) for m in machines},
        region=region,
        bounds=(region.sx, region.sy, region.sz),
        reserved=set(),
        adjacency={ids[0]: set(ids[1:])},
        machine_nets={ids[0]: [(ids, 1.0)], ids[1]: [(ids, 1.0)], ids[2]: [(ids, 1.0)]},
        machine_power={ids[0]: [(ids, 2.0)], ids[1]: [], ids[2]: []},
        machine_auto={
            # Both directions, so the cost exercises the is_source branch either way. The ports
            # are what decide whether the connection is possible at all (#107), so they are the
            # real ones off _hub rather than placeholders.
            ids[0]: [
                _AutoPair(ids[0], "out", ids[1], "out"),
                _AutoPair(ids[2], "out", ids[0], "out"),
            ],
            ids[1]: [],
            ids[2]: [],
        },
        weights=(1.0, 0.02),
    )


#: No extent, and the one ``b`` at (4,0,0) and ``c`` at (0,0,4) span, so candidates on the far row and
#: column of the 6x6 region grow it and the growth term is inside what the bound has to admit.
_EXTENTS = [None, _Extent(0, 4, 0, 0, 0, 4)]


@pytest.mark.parametrize("extent", _EXTENTS)
def test_the_pruning_bound_never_changes_which_cost_is_reported(extent: _Extent | None) -> None:
    """``bound`` is an early-out, not an approximation: below it the exact cost still comes back.

    The auto reward is *subtracted*, so the running ``wire + cable`` total is an upper bound on the
    result. Comparing that against ``bound`` directly would discard candidates the reward would
    have pulled under it - the bug this test exists to catch. Every origin is scored unbounded,
    then re-scored at a bound on either side of its own cost; the answer must be the exact cost
    whenever it can still win, and never a *wrong* finite number when it cannot.
    """
    machines = [_hub("a"), _hub("b"), _hub("c")]
    region = CellBox(sx=6, sy=2, sz=6)
    ctx = _cost_ctx(machines, region)
    placed_pos = {"b": pose_of(at("b", 4, 0, 0)), "c": pose_of(at("c", 0, 0, 4))}
    net_boxes, power_attach = _placed_invariants("a", placed_pos, ctx)
    body = ctx.bodies["a"]

    checked = 0
    for x in range(region.sx):
        for z in range(region.sz):
            args = ((x, 0, z), Facing.NORTH, body, placed_pos, net_boxes, power_attach, ctx)
            exact = _marginal_insertion_cost("a", *args, extent=extent)
            assert math.isfinite(exact)
            # A bound above the true cost must not perturb it...
            assert _marginal_insertion_cost("a", *args, bound=exact + 1e-9, extent=extent) == exact
            # ...and one at or below it may only ever abstain, never report a different number.
            pruned = _marginal_insertion_cost("a", *args, bound=exact, extent=extent)
            assert pruned == exact or pruned == math.inf
            checked += 1
    assert checked == region.sx * region.sz


@pytest.mark.parametrize("extent", _EXTENTS)
def test_the_pruning_bound_matches_an_unbounded_scan(extent: _Extent | None) -> None:
    """End to end on the loop's own contract: pruning picks the same winner as scoring everything.

    ``_best_insertion`` keeps a candidate only on a strict ``<``, so an admissible bound cannot
    change the argmin - this pins that the bound really is admissible rather than merely plausible.
    """
    machines = [_hub("a"), _hub("b"), _hub("c")]
    region = CellBox(sx=6, sy=2, sz=6)
    ctx = _cost_ctx(machines, region)
    placed_pos = {"b": pose_of(at("b", 4, 0, 0)), "c": pose_of(at("c", 0, 0, 4))}
    net_boxes, power_attach = _placed_invariants("a", placed_pos, ctx)

    def scan(use_bound: bool) -> tuple[float, tuple[int, int, int] | None]:
        best_cost, best = math.inf, None
        for x in range(region.sx):
            for z in range(region.sz):
                cost = _marginal_insertion_cost(
                    "a",
                    (x, 0, z),
                    Facing.NORTH,
                    ctx.bodies["a"],
                    placed_pos,
                    net_boxes,
                    power_attach,
                    ctx,
                    bound=best_cost if use_bound else math.inf,
                    extent=extent,
                )
                if cost < best_cost:
                    best_cost, best = cost, (x, 0, z)
        return best_cost, best

    assert scan(use_bound=True) == scan(use_bound=False)


def _netless_ctx(
    machines: list[Machine], region: CellBox, weights: tuple[float, float]
) -> _SearchContext:
    """No nets, no power, no auto pairs: an insertion's cost is its growth of the build alone."""
    return _SearchContext(
        bodies={m.id: _body(m) for m in machines},
        region=region,
        bounds=(region.sx, region.sy, region.sz),
        reserved=set(),
        adjacency={},
        machine_nets={m.id: [] for m in machines},
        machine_power={m.id: [] for m in machines},
        machine_auto={m.id: [] for m in machines},
        weights=weights,
    )


@pytest.mark.parametrize(
    ("origin", "weights", "growth"),
    [
        ((2, 0, 0), (1.0, 0.02), 0.0),  # between the two: the build does not grow
        ((6, 0, 0), (1.0, 0.02), 2 + 0.02 * 2),  # past the end: floor 5 -> 7, volume 5 -> 7
        ((2, 0, 1), (1.0, 0.02), 5 + 0.02 * 5),  # beside the row: floor 5 -> 10, volume 5 -> 10
        ((2, 1, 0), (1.0, 0.02), 0.02 * 5),  # on top: the floor is free, only volume grows
        ((2, 1, 0), (0.0, 1.0), 5.0),  # ...which the volume objective prices in full
    ],
)
def test_an_insertion_pays_the_objectives_weights_on_what_it_adds_to_the_build(
    origin: Cell, weights: tuple[float, float], growth: float
) -> None:
    """The LNS recreate prices compactness (#254). It used to rank spots on nets and auto-output
    alone, so a machine landing past the end of the build cost the same as one tucked inside it,
    and the move could never fold the strip the first-fit start lays a big line out in."""
    machines = [_hub("a"), _hub("b"), _hub("c")]
    ctx = _netless_ctx(machines, CellBox(sx=8, sy=2, sz=8), weights)
    placed = [pose_of(at("b", 0, 0, 0)), pose_of(at("c", 4, 0, 0))]
    extent = _placed_extent(placed, ctx)
    assert extent == _Extent(0, 4, 0, 0, 0, 0)

    placed_pos = {q.machine_id: q for q in placed}

    def cost(extent: _Extent | None) -> float:
        body = ctx.bodies["a"]
        return _marginal_insertion_cost(
            "a", origin, Facing.NORTH, body, placed_pos, [], [], ctx, extent=extent
        )

    assert cost(extent) == pytest.approx(growth)
    assert cost(None) == 0.0  # nothing placed yet: there is no box to grow


def test_placed_extent_is_none_with_nothing_placed() -> None:
    ctx = _netless_ctx([_hub("a")], CellBox(sx=4, sy=1, sz=4), (1.0, 0.02))
    assert _placed_extent([], ctx) is None


def test_placed_invariants_summarise_only_the_placed_members() -> None:
    """A net with nothing else placed yet contributes no box, so it costs nothing until one is.

    Dropping it is what makes the precomputed form equal the old per-candidate ``len(xs) > 1``
    guard rather than adding a spurious zero-width span.
    """
    machines = [_hub("a"), _hub("b"), _hub("c")]
    ctx = _cost_ctx(machines, CellBox(sx=6, sy=2, sz=6))
    assert _placed_invariants("a", {}, ctx)[0] == []
    boxes, power = _placed_invariants("a", {"b": pose_of(at("b", 4, 0, 0))}, ctx)
    assert len(boxes) == 1
    assert boxes[0].x0 == boxes[0].x1, "one placed member is a degenerate box, not an empty one"
    assert [len(pa.centroids) for pa in power] == [1]


# ------------------------------------------------------- the flat occupancy grid


def _flat(cell: Cell, region: CellBox) -> int:
    x, y, z = cell
    return x + y * region.sx + z * region.sx * region.sy


def test_occupancy_grid_marks_exactly_occupied_and_reserved() -> None:
    """The grid is a restatement of ``occupied | reserved``, so it must mark those and nothing more.

    A byte set where no body sits would reject legal placements; one missed would admit an overlap
    the validator then has to catch. Both directions are asserted over every cell of the region.
    """
    region = CellBox(sx=4, sy=2, sz=3)
    occupied = {(0, 0, 0), (3, 1, 2)}
    reserved = {(2, 0, 1)}
    grid = _occupancy_grid(region, occupied, reserved)

    assert len(grid) == region.sx * region.sy * region.sz
    blocked = occupied | reserved
    for z in range(region.sz):
        for y in range(region.sy):
            for x in range(region.sx):
                want = 1 if (x, y, z) in blocked else 0
                assert grid[_flat((x, y, z), region)] == want, (x, y, z)


def test_occupancy_grid_ignores_a_reserved_cell_outside_the_region() -> None:
    """``reserved`` comes from the plan, so it is the one input that can name an out-of-bounds cell.

    Marking it would corrupt an unrelated index (the flat layout wraps), so it is dropped. Every
    ``occupied`` cell cleared ``box_in_region`` to get into the set and is trusted unguarded.
    """
    region = CellBox(sx=2, sy=1, sz=2)
    grid = _occupancy_grid(region, set(), {(9, 9, 9), (-1, 0, 0), (1, 0, 1)})
    assert list(grid) == [0, 0, 0, 1]


def test_box_offsets_address_exactly_the_body_cells() -> None:
    """Offsets are the body's cells expressed once, relative to the origin, instead of per candidate.

    Pinned against ``occupied_cells`` (the shared primitive the router and validator also read) at
    a non-cubic machine turned both ways, because a turn is what makes the two disagree if the
    offsets were ever built from the declared rather than the rotated box.
    """
    region = CellBox(sx=6, sy=2, sz=6)
    machine = _wide()
    for orientation in (Facing.NORTH, Facing.EAST):
        box = rotated_footprint(machine.footprint, orientation)
        offsets = _box_offsets((box.sx, box.sy, box.sz), (region.sx, region.sy, region.sz))
        for origin in (CellCoord(x=0, y=0, z=0), CellCoord(x=2, y=1, z=3)):
            base = _flat((origin.x, origin.y, origin.z), region)
            got = sorted(base + off for off in offsets)
            want = sorted(
                _flat(c, region) for c in occupied_cells(origin, machine.footprint, orientation)
            )
            assert got == want, (orientation, origin)


def test_the_grid_fit_test_agrees_with_the_set_test_everywhere() -> None:
    """The equivalence the speedup rests on, asserted over every origin and orientation.

    The grid walk replaced ``reserved.isdisjoint(cells) and occupied.isdisjoint(cells)``. It is a
    faster spelling of that predicate or it is a bug, so this compares the two answers directly
    rather than trusting that a solve still produces the same layout.
    """
    region = CellBox(sx=5, sy=2, sz=5)
    machine = _wide()
    occupied: set[Cell] = {(1, 0, 1), (4, 1, 4)}
    reserved: set[Cell] = {(3, 0, 2)}
    grid = _occupancy_grid(region, occupied, reserved)

    checked = 0
    for z in range(region.sz):
        for y in range(region.sy):
            for x in range(region.sx):
                origin = CellCoord(x=x, y=y, z=z)
                for orientation in (Facing.NORTH, Facing.EAST):
                    if not box_in_region(origin, machine.footprint, orientation, region):
                        continue  # the grid walk is only defined behind this gate
                    cells = list(occupied_cells(origin, machine.footprint, orientation))
                    want = reserved.isdisjoint(cells) and occupied.isdisjoint(cells)
                    box = rotated_footprint(machine.footprint, orientation)
                    base = _flat((x, y, z), region)
                    size, bounds = (box.sx, box.sy, box.sz), (region.sx, region.sy, region.sz)
                    got = not any(grid[base + off] for off in _box_offsets(size, bounds))
                    assert got == want, (origin, orientation)
                    checked += 1
    assert checked > 50, "the sweep must actually reach in-region placements"


def test_the_grid_never_drifts_from_occupied_during_a_recreate() -> None:
    """The invariant the whole optimisation depends on: grid == occupied | reserved, every call.

    ``_ruin_and_recreate`` now builds the grid once and sets bits as each machine lands, instead of
    rebuilding it per insertion. If those two ever drift, ``_best_insertion`` starts answering the
    fit test against a stale world - silently admitting an overlap or refusing a free cell - and
    the failure surfaces far downstream in the validator, if at all. So this re-derives the grid
    from scratch on every single call and asserts it matches the one being carried.
    """
    import gtnh_solver.placement.search as search_module

    real = search_module._best_insertion
    seen = 0

    def checking_best_insertion(
        p: Pose,
        placed: list[Pose],
        occupied: set[Cell],
        grid: bytearray,
        ctx: _SearchContext,
        rng: random.Random,
    ) -> tuple[Cell, Facing] | None:
        nonlocal seen
        seen += 1
        assert grid == _occupancy_grid(ctx.region, occupied, ctx.reserved), (
            f"grid drifted from occupied on call {seen}"
        )
        return real(p, placed, occupied, grid, ctx, rng)

    search_module._best_insertion = checking_best_insertion
    try:
        result = optimize_placement(_star(5), seed=3)
    finally:
        search_module._best_insertion = real

    assert result.ok
    assert seen > 0, "the recreate move never ran; the invariant went unchecked"


# ------------------------------------------------------- plain values in the hot loops (#256)


def _dockable_by_scan(
    pose: Pose, footprint: CellBox, occupied: set[Cell], region: CellBox, reserved: set[Cell]
) -> set[Cell]:
    """The dockable set built the way it was before #256: every body cell stepped through every
    non-front face, in ``FACE_DELTAS`` order, and filtered as it goes."""
    origin = CellCoord(x=pose.cell[0], y=pose.cell[1], z=pose.cell[2])
    body = tuple(occupied_cells(origin, footprint, pose.orientation))
    cells: set[Cell] = set()
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is pose.orientation:
            continue
        for bx, by, bz in body:
            cand = (bx + dx, by + dy, bz + dz)
            if cand in occupied or cand in reserved:
                continue
            if in_region(cand, region):
                cells.add(cand)
    return cells


def _pose_cells(pose: Pose, machine: Machine) -> list[Cell]:
    origin = CellCoord(x=pose.cell[0], y=pose.cell[1], z=pose.cell[2])
    return list(occupied_cells(origin, machine.footprint, pose.orientation))


@pytest.mark.parametrize("front", list(Facing))
def test_dockable_cells_match_the_face_by_body_scan_in_iteration_order(front: Facing) -> None:
    """The shell of precomputed offsets must give the set the full scan gave, in the same order.

    Equal as sets is not enough. ``_face_shortfall`` sums floats over the set, so a set that held
    the same cells but iterated them differently could round differently, move the cost in its
    last bit and flip an annealing decision - a layout change with no bug anywhere to point at. The
    order survives only if the shell inserts the surviving cells in the scan's order, so the lists
    are compared, not the sets. Swept over box shapes (so a rotation swaps unequal extents), every
    facing, origins against the region walls, and neighbours and reserved cells scattered around.
    """
    rng = random.Random(0)
    region = CellBox(sx=9, sy=6, sz=9)
    bounds = (region.sx, region.sy, region.sz)
    checked = 0
    for sx in range(1, 5):
        for sy in range(1, 4):
            for sz in range(1, 5):
                machine = Machine(
                    id="m",
                    type="m",
                    footprint=CellBox(sx=sx, sy=sy, sz=sz),
                    voltage_tier="LV",
                    orientation_options=[Facing.NORTH],
                )
                body = _body(machine)
                size = body.sizes[front]
                for _ in range(3):
                    x, y, z = (rng.randrange(bounds[a] - size[a] + 1) for a in range(3))
                    pose = Pose("m", (x, y, z), front)
                    # The machine's own body is always in `occupied`: that is the precondition the
                    # shell leans on to drop the offsets that land back inside it.
                    occupied = set(_pose_cells(pose, machine))
                    for _ in range(40):
                        occupied.add((rng.randrange(9), rng.randrange(6), rng.randrange(9)))
                    reserved = {(rng.randrange(9), rng.randrange(6), rng.randrange(9))}
                    got = _dockable_cells(pose, body, occupied, bounds, reserved)
                    want = _dockable_by_scan(pose, machine.footprint, occupied, region, reserved)
                    assert list(got) == list(want), (size, front, pose.cell)
                    checked += 1
    assert checked == 4 * 3 * 4 * 3


def test_a_pose_round_trips_to_the_same_placement() -> None:
    """The anneal holds poses and converts back once; the layout it returns must be unchanged."""
    placement = at("m", 3, 1, 4, orientation=Facing.EAST)
    pose = pose_of(placement)
    assert pose == Pose("m", (3, 1, 4), Facing.EAST)
    assert _placement(pose) == placement


def test_a_body_reads_the_machine_as_placement_sees_it() -> None:
    """``_Body`` restates a machine in plain values, so it must say what the model says."""
    source = power_source("src", orientations=[Facing.NORTH, Facing.WEST])
    wide = _wide()
    for machine in (source, wide, _hub("h")):
        body = _body(machine)
        assert body.machine is machine
        assert body.orientations == tuple(machine.orientation_options)
        assert body.is_power_source == machine.is_power_source
        assert body.port_ids == tuple(port.id for port in machine.faces.ports)
        for facing in Facing:
            box = rotated_footprint(machine.footprint, facing)
            assert body.sizes[facing] == (box.sx, box.sy, box.sz)
    assert _body(source).is_power_source
    assert _body(wide).sizes[Facing.EAST] == (1, 1, 3)


# ------------------------------------------------------------------ the one-cell nudge


def test_a_nudge_moves_one_machine_by_one_cell_and_stays_valid() -> None:
    # Every nudge the search could make on a star with a power source: exactly one machine moves,
    # by exactly one cell, the layout stays validator-clean (the source's feed face included), and
    # the occupied set the move borrows comes back as it was.
    problem = _star(4)
    problem = problem.model_copy(
        update={
            "machines": [
                *problem.machines,
                power_source(
                    "src", orientations=[Facing.NORTH, Facing.EAST, Facing.SOUTH, Facing.WEST]
                ),
            ]
        }
    )
    ctx = _fit_ctx(problem.bounding_region, list(problem.machines))
    placed = [pose_of(p) for p in place(problem).placements]
    occupied = {c for p in placed for c in _pose_cells(p, ctx.bodies[p.machine_id].machine)}
    before = set(occupied)
    moved = 0
    for seed in range(200):
        cand = _nudge(placed, ctx, occupied, random.Random(seed))
        assert occupied == before, "the move must restore what it borrowed"
        if cand is None:
            continue
        changed = [(a, b) for a, b in zip(placed, cand, strict=True) if a != b]
        assert len(changed) == 1
        [(a, b)] = changed
        assert sum(abs(i - j) for i, j in zip(a.cell, b.cell, strict=True)) == 1
        assert _validates(problem, tuple(_placement(p) for p in cand))
        moved += 1
    assert moved > 0


def test_a_machine_boxed_in_on_every_side_cannot_be_nudged() -> None:
    # A 3x1x1 region filled by three blocks: every step runs into a neighbour or off the region.
    machines = [_spoke(f"s{i}") for i in range(3)]
    ctx = _fit_ctx(CellBox(sx=3, sy=1, sz=1), machines)
    placed = [Pose(f"s{i}", (i, 0, 0), Facing.NORTH) for i in range(3)]
    occupied = {(0, 0, 0), (1, 0, 0), (2, 0, 0)}
    for seed in range(20):
        assert _nudge(placed, ctx, occupied, random.Random(seed)) is None
    assert occupied == {(0, 0, 0), (1, 0, 0), (2, 0, 0)}


def test_only_a_line_of_single_blocks_is_nudged(monkeypatch: pytest.MonkeyPatch) -> None:
    # The nudge measured as noise at best on multiblock lines, so any multiblock turns it off and
    # the search there is exactly what it was; a line of single blocks gets it.
    import gtnh_solver.placement.search as search_module

    calls = 0
    real = search_module._nudge

    def counting(*args: object, **kwargs: object) -> list[Pose] | None:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_module, "_nudge", counting)
    optimize_placement(_star(4), seed=0)
    assert calls > 0

    calls = 0
    with_multiblock = _star(4)
    with_multiblock = with_multiblock.model_copy(
        update={"machines": [*with_multiblock.machines, _wide()]}
    )
    optimize_placement(with_multiblock, seed=0)
    assert calls == 0
