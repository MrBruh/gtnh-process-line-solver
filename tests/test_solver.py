"""Tests for the Phase 1 solver (place + auto-output + item/fluid + power route).

Headline: solving the real sand line yields a fully valid layout whose item chain **auto-feeds
with zero pipes** and whose synthesized power net is cabled as a shared-amperage trunk. Plus the
invariants: the result is always either VALID-and-validator-clean or
non-VALID-with-an-explicit-infeasibility.

Every solve here is ``minimal`` unless it says otherwise (``tests/conftest.py``). The tests that
hold the search to a quality bar (the hand-built sand targets, the parallel line) are marked
``full_solve`` and run only with ``--full-solve``; the ones that test the multi-start itself (the
ranking, the pool) pass ``effort="full"``, since one attempt has nothing to rank.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from gtnh_solver.adapter import (
    Edge,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    Resource,
    Storage,
    adapt_file,
    to_input_ir,
)
from gtnh_solver.adapter.power import synthesize_power
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    Infeasibility,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    PipeSize,
    Placement,
    Port,
    RelativeFace,
    Route,
    Segment,
)
from gtnh_solver.placement import (
    Objective,
    PlacementResult,
    optimize_placement,
    place,
    single_block_shortfalls,
)
from gtnh_solver.router import RouteResult, assign_auto_outputs, route
from gtnh_solver.solver import Effort, solve
from gtnh_solver.solver import core as solver_core
from gtnh_solver.solver._structure import structure_quality
from gtnh_solver.validator import ValidationReport, Violation, ViolationCode, validate
from tests._helpers import at, consumer, hub_line, net, power_source, producer

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"
_PARALLEL_SAND = _EXAMPLES / "gtnh-parallel-sand.json"


@pytest.mark.full_solve
def test_solve_sand_items_auto_feed_and_power_is_cabled() -> None:
    ir = adapt_file(_SAND)
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID
    assert validate(ir, layout).ok
    item_nets = [n for n in ir.nets if n.commodity is Commodity.ITEM]
    assert len(layout.auto_connections) == len(item_nets)  # every item net auto-feeds: zero pipes
    assert [r.commodity for r in layout.routes] == [
        Commodity.POWER
    ]  # only the power trunk is cabled


def _structure_metrics(layout: LayoutResult) -> tuple[int, int, int]:
    """(footprint, volume, power cable cells) of the whole built structure (machines + routes)."""
    cells = {(p.cell.x, p.cell.y, p.cell.z) for p in layout.placements}
    power_cells: set[tuple[int, int, int]] = set()
    for r in layout.routes:
        for seg in r.segments:
            ends = {(seg.start.x, seg.start.y, seg.start.z), (seg.end.x, seg.end.y, seg.end.z)}
            cells.update(ends)
            if r.commodity is Commodity.POWER:
                power_cells.update(ends)
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    zs = [c[2] for c in cells]
    footprint = (max(xs) - min(xs) + 1) * (max(zs) - min(zs) + 1)
    volume = footprint * (max(ys) - min(ys) + 1)
    return footprint, volume, len(power_cells)


def test_the_ranking_trades_floor_area_against_route_cells() -> None:
    # Stacked, the pair takes one floor cell but needs a two-block pipe up to the consumer; side by
    # side, it takes two floor cells and auto-feeds with no pipe at all. Ranked on floor area
    # first the stack won whatever its pipe cost; the blend (floor plus route cells) picks the
    # pair that builds fewer blocks in all.
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    pipe = Route(
        net_id="n",
        commodity=Commodity.ITEM,
        segments=[Segment(start=CellCoord(x=0, y=1, z=0), end=CellCoord(x=0, y=2, z=0), channel=0)],
    )
    stacked = structure_quality(problem, [at("a", 0, 0, 0), at("b", 0, 3, 0)], [pipe], "footprint")
    side_by_side = structure_quality(problem, [at("a", 0, 0, 0), at("b", 1, 0, 0)], [], "footprint")
    assert stacked == (1 + 2, 1, 4)
    assert side_by_side == (2 + 0, 2, 2)
    assert side_by_side < stacked


@pytest.mark.full_solve
def test_solve_sand_optimized_matches_or_beats_the_hand_built_target() -> None:
    # The acceptance target (docs/ROADMAP.md lane C): the maintainer hand-builds the sand line in
    # a 3x2x2 volume with 3 power cables, so the optimizer must find that or better - VALID, the
    # whole built structure (machines + routes) on a floor area <= 3x2 = 6 cells, and <= 3 power
    # cable cells. The quality-driven feedback loop is what finds it: it routes every attempt and
    # keeps the best by floor area plus route cells instead of returning the first valid.
    layout = solve(adapt_file(_SAND))
    assert layout.status is LayoutStatus.VALID
    footprint, _, cables = _structure_metrics(layout)
    assert footprint <= 6, f"structure footprint {footprint} exceeds the hand-built 3x2"
    assert cables <= 3, f"{cables} power cable cells exceed the hand-built 3"


@pytest.mark.full_solve
def test_solve_sand_volume_objective_stays_within_the_hand_built_box() -> None:
    # objective="volume" minimizes the enclosing box instead of the floor area: a flatter,
    # larger-floor layout is acceptable, but the structure must fit the hand-built 3x2x2 = 12
    # volume or better, and the <= 3-cable wire goal applies to every objective.
    ir = adapt_file(_SAND)
    layout = solve(ir, objective="volume")
    assert layout.status is LayoutStatus.VALID
    _, volume, cables = _structure_metrics(layout)
    assert volume <= 12, f"structure volume {volume} exceeds the hand-built 3x2x2"
    assert cables <= 3, f"{cables} power cable cells exceed the hand-built 3"


@pytest.mark.full_solve
def test_solve_sand_balanced_objective_is_valid_and_low_wire() -> None:
    # objective="balanced" weighs floor area and enclosing box together; it must still produce a
    # fully valid sand layout within the hand-built compactness and wire budget on both metrics.
    ir = adapt_file(_SAND)
    layout = solve(ir, objective="balanced")
    assert layout.status is LayoutStatus.VALID
    footprint, volume, cables = _structure_metrics(layout)
    assert footprint <= 6
    assert volume <= 12
    assert cables <= 3


@pytest.mark.full_solve
def test_solve_the_parallel_line_reaches_a_valid_layout() -> None:
    """The acceptance case for #76: three nodes at three instances each, nine machines, VALID.

    It took two things, because the line was short of room in two different ways at once. The
    placer packed the nine into a solid row where each had two free cells for three connections,
    which no routing order can rescue - that is the face-shortfall term plus the crowding gate.
    And the router docked greedily net by net, stranding a net on a machine that did have room,
    which is the re-seat rescue. Fixing either alone still left the line partial_invalid, so this
    test is the one that holds both down.
    """
    ir = adapt_file(_PARALLEL_SAND)
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(ir, layout).ok

    # And every item run is laid big enough to reach all three machines of its stage (#165). The
    # maintainer built this line with plain tin pipes and only one stone hammer in three was fed.
    item_routes = [r for r in layout.routes if r.commodity is Commodity.ITEM]
    assert len(item_routes) == 4
    assert all(r.material is not None for r in item_routes)
    assert {r.material.size for r in item_routes if r.material} == {PipeSize.HUGE}


# The optimized path's determinism is proven over generated problems by
# test_solver_properties.py::test_solve_is_deterministic_for_a_given_problem_and_seed, which
# subsumes the sand-only example that used to sit here and costs 16s less (GitHub #94). The fast
# path keeps its own example below: it is a different placer, and near-instant.


def test_fast_mode_uses_constructive_placement() -> None:
    # optimize=False skips SA/LNS and the feedback loop: it places with the constructive first-fit
    # placer and still validates. Sand is simple enough that the fast layout is fully valid.
    ir = adapt_file(_SAND)
    layout = solve(ir, optimize=False)
    assert layout.status is LayoutStatus.VALID
    assert validate(ir, layout).ok
    assert layout.placements == list(place(ir).placements)  # exactly the constructive placement


def test_fast_mode_is_deterministic() -> None:
    ir = adapt_file(_SAND)
    assert solve(ir, optimize=False) == solve(ir, optimize=False)


@pytest.mark.parametrize(
    "me_commodities", [(), (Commodity.FLUID,)], ids=["no-me-network", "a-network-nothing-rides"]
)
def test_a_fast_solve_with_no_me_block_to_lay_is_the_constructive_layout(
    me_commodities: tuple[Commodity, ...],
) -> None:
    # Only a line with an ME block to lay gets a minimal attempt in place of the fast path (#352,
    # tests/test_solver_me.py); every other fast solve is the constructive placement assembled as
    # it stands, byte for byte. Sand has no fluid net, so its fluids on ME declare a network that
    # nothing rides, which lays no block.
    problem = adapt_file(_SAND, me_commodities=me_commodities)
    assert [n.id for n in problem.me.networks] == (["main"] if me_commodities else [])
    assert not solver_core.fast_falls_back(problem)
    constructive, _ = solver_core._assemble(problem, place(problem).placements, 0, repair=False)
    assert solve(problem, optimize=False).model_dump_json() == constructive.model_dump_json()


def test_fast_mode_passes_through_infeasibility() -> None:
    # two 1x1x1 machines into a 1x1x1 region: constructive placement cannot fit them, and fast mode
    # surfaces that as an explicit infeasibility rather than a silent failure.
    problem = InputIR(
        bounding_region=CellBox(sx=1, sy=1, sz=1),
        machines=[producer("m0"), consumer("m1")],
        nets=[],
    )
    layout = solve(problem, optimize=False)
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.infeasibility is not None


@pytest.mark.full_solve
def test_optimize_recovers_a_congested_line_fast_mode_leaves_partial() -> None:
    # A tight single-layer fan-out that the constructive placement cannot route in one shot. The
    # optimizer's SA/LNS + place<->route feedback loop recovers a VALID layout; fast mode, with no
    # re-placement, leaves it non-VALID. This is the tradeoff the "optimize or not" control exposes.
    edges = [("m0", "m2"), ("m0", "m3"), ("m1", "m3"), ("m1", "m4"), ("m2", "m5"), ("m4", "m5")]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=7),
        machines=[_io_machine(f"m{i}") for i in range(6)],
        nets=[_edge(f"e{k}", a, b) for k, (a, b) in enumerate(edges)],
    )
    assert solve(problem, optimize=True).status is LayoutStatus.VALID
    assert solve(problem, optimize=False).status is not LayoutStatus.VALID


def _stacked_line() -> InputIR:
    """Issue #132's case B: two unpowered blocks and two powered ones at two tiers in a 3x2x4 box.

    The floor term rewards stacking them, and a stacked machine can be left with only its front
    free, which no cable docks on; the constructive placement lays them flat and is VALID.
    """

    def block(mid: str, tier: str, eut: float, sx: int, sz: int) -> Machine:
        return Machine(
            id=mid,
            type="t",
            voltage_tier=tier,
            eut=eut,
            footprint=CellBox(sx=sx, sy=1, sz=sz),
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(ports=[]),
        )

    machines, nets = synthesize_power(
        [
            block("m0", "LV", 0.0, 2, 1),
            block("m1", "LV", 0.0, 2, 1),
            block("m2", "LV", 8.0, 1, 2),
            block("m3", "MV", 8.0, 1, 1),
        ],
        [],
    )
    return InputIR(bounding_region=CellBox(sx=3, sy=2, sz=4), machines=machines, nets=nets)


@pytest.mark.parametrize("seed", range(5))
def test_optimizing_is_never_worse_than_the_fast_path(seed: int) -> None:
    # The suite's one short attempt stacks this line on seeds 3 and 4, and m2's cable then has no
    # face to dock on; the fast path's flat layout is VALID, so the optimizer returns it (#132).
    problem = _stacked_line()
    assert solve(problem, optimize=False).status is LayoutStatus.VALID
    assert solve(problem, seed=seed).status is LayoutStatus.VALID


def test_with_no_valid_attempt_the_optimizer_returns_the_fast_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every attempt is made to come out partial, so the only VALID layout left is the fast path's,
    # and it is the one returned: exactly that layout, not a re-annealed or repaired one.
    real = solver_core._attempt

    def partial(*args: Any, **kwargs: Any) -> Any:
        attempt = real(*args, **kwargs)
        if attempt.layout is None:
            return attempt
        failed = attempt.layout.model_copy(
            update={
                "status": LayoutStatus.PARTIAL_INVALID,
                "infeasibility": Infeasibility(constraint="routing", detail="forced by the test"),
            }
        )
        return dataclasses.replace(attempt, layout=failed, failed_nets=("forced",))

    monkeypatch.setattr(solver_core, "_attempt", partial)
    problem = _stacked_line()
    assert solve(problem, seed=3) == solve(problem, seed=3, optimize=False)


def test_solve_returns_valid_or_explicit_infeasibility(
    solved_nitrobenzene: tuple[InputIR, LayoutResult],
) -> None:
    ir, layout = solved_nitrobenzene
    if layout.status is LayoutStatus.VALID:
        assert validate(ir, layout).ok
    else:
        assert layout.infeasibility is not None  # incompleteness is never silent


def test_solve_infeasible_when_machines_do_not_fit() -> None:
    a = Machine(id="a", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    b = Machine(id="b", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    problem = InputIR(bounding_region=CellBox(sx=1, sy=1, sz=1), machines=[a, b], nets=[])
    layout = solve(problem)
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.infeasibility is not None
    assert layout.metrics.footprint is None  # nothing placed -> no measurable build to report


@pytest.mark.parametrize("optimize", [True, False], ids=["optimized", "fast"])
def test_a_partial_layout_names_a_single_block_with_more_connections_than_faces(
    optimize: bool,
) -> None:
    # Left to the routers, the reason is whichever net lost the last free face, as congestion or an
    # undockable terminal, with advice about room that no amount of room satisfies. The cause is
    # the hub's sixth connection, so the reason names it and keeps the routers' own words after it.
    layout = solve(hub_line(6), optimize=optimize)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert layout.placements, "still the laid partial layout: only its reason is restated"
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "single_block_faces"
    assert "'hub' (t, 6)" in layout.infeasibility.detail
    assert "The routers stopped at: " in layout.infeasibility.detail
    assert layout.infeasibility.suggested_relaxation is not None
    assert "structure" in layout.infeasibility.suggested_relaxation
    # A single block is merged through Item Filters only once proven one (#249), so the advice
    # names the census that would prove it.
    assert "census dataset for the plan's pack" in layout.infeasibility.suggested_relaxation


def test_machines_that_do_not_fit_keep_their_own_reason() -> None:
    # The hub is one face short as well, but nothing was placed: the region is the first problem.
    layout = solve(hub_line(6, region=CellBox(sx=2, sy=1, sz=2)))
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint != "single_block_faces"


def test_solve_populates_footprint_and_layer_metrics(
    solved_sand: tuple[InputIR, LayoutResult],
) -> None:
    # LayoutMetrics is produced, not just declared (GitHub #13): the previewer reads footprint and
    # layers off the returned layout, and the seed-compare workflow ranks on them.
    ir, layout = solved_sand
    assert layout.status is LayoutStatus.VALID
    assert layout.metrics.footprint is not None
    assert layout.metrics.footprint > 0
    assert layout.metrics.layers is not None
    assert layout.metrics.layers >= 1
    # the fast path assembles through the same code, so it reports metrics too
    assert solve(ir, optimize=False).metrics.footprint is not None
    # buildability/congestion have no scoring model yet, so they stay deferred (None), not faked
    assert layout.metrics.buildability is None
    assert layout.metrics.congestion is None


def test_layout_metrics_empty_layout_reports_no_metrics() -> None:
    # No placements (e.g. the infeasible path) -> all-None metrics, not a fake zero, so a viewer
    # can tell "nothing built" from "a real 0-footprint build".
    m = Machine(id="a", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    ir = InputIR(bounding_region=CellBox(sx=2, sy=1, sz=2), machines=[m], nets=[])
    metrics = solver_core._layout_metrics(ir, [], [])
    assert metrics.footprint is None
    assert metrics.layers is None


# EAST-first orientation: the constructive seed faces every machine's front down the +x chain
# axis, blocking the east/west auto-output - so only reorientation can recover the free connection.
_EAST_FIRST = [Facing.EAST, Facing.NORTH, Facing.SOUTH, Facing.WEST]


def _relay(mid: str) -> Machine:
    # both an input and an output port; EAST-first orientation so reorient moves are exercised
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=_EAST_FIRST,
        faces=FaceSpec(
            ports=[
                Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            ]
        ),
    )


def _east_first(machine: Machine) -> Machine:
    return machine.model_copy(update={"orientation_options": _EAST_FIRST})


def test_optimizer_reorients_to_enable_auto_output_the_seed_blocks() -> None:
    # A straight chain m0->m1->m2->m3 packed along +x, every machine free to reorient. With the
    # EAST-first default the constructive seed points each front down the chain axis, so NOTHING
    # auto-feeds (seed = 0). SA's reorient move must therefore carry a cost signal that pulls fronts
    # off the connecting faces and recovers the free connections - the FIX 3 guard. The old
    # orientation-blind cost made reorient a free random walk (delta 0, never strictly better), so
    # `best` stayed frozen on the seed orientation and auto-output never recovered (stuck at 0).
    machines = [
        _east_first(producer("m0")),
        _relay("m1"),
        _relay("m2"),
        _east_first(consumer("m3")),
    ]
    problem = InputIR(
        bounding_region=CellBox(sx=12, sy=4, sz=12),
        machines=machines,
        nets=[net("n0", "m0", "m1"), net("n1", "m1", "m2"), net("n2", "m2", "m3")],
    )
    seed_autos = assign_auto_outputs(problem, place(problem).placements).connections
    assert len(seed_autos) == 0  # the seed orientation blocks every link; reorientation must fix it
    for s in range(8):
        layout = solve(problem, seed=s)
        assert layout.status is LayoutStatus.VALID, f"seed {s}: {layout.infeasibility}"
        # the optimizer must recover auto-output the seed could not - strictly more than zero
        assert len(layout.auto_connections) > len(seed_autos), f"seed {s} recovered no auto-output"


def _io_machine(mid: str) -> Machine:
    # both an output and an input port, free to reorient - usable anywhere in a small graph
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST],
        faces=FaceSpec(
            ports=[
                Port(id="o", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
                Port(id="i", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            ]
        ),
    )


def _edge(nid: str, src: str, dst: str) -> Net:
    return Net(
        id=nid,
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=src, port_id="o"),
            MachineFaceRef(machine_id=dst, port_id="i"),
        ],
    )


def test_the_multi_start_recovers_a_layout_a_single_attempt_leaves_partial() -> None:
    # A tight single-layer fan-out graph where the seed-3 placement strands a net - the router
    # cannot lay its pipe in the congested layout, so one assembly attempt is partial_invalid.
    # Another seed of the multi-start places it differently and routes cleanly: solve() returns
    # VALID where a single attempt did not. (The seed is whichever one the annealer happens to
    # strand: it was seed 1 while the LNS recreate priced compactness without the one-cell nudge,
    # #254, seed 0 with both, seed 1 again since an anneal returns the cheapest placement the
    # crowding gate passes, and seed 3 since a single-block line starts from the spaced lattice,
    # which routes seeds 0 to 2.)
    edges = [("m0", "m2"), ("m0", "m3"), ("m1", "m3"), ("m1", "m4"), ("m2", "m5"), ("m4", "m5")]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=7),
        machines=[_io_machine(f"m{i}") for i in range(6)],
        nets=[_edge(f"e{k}", a, b) for k, (a, b) in enumerate(edges)],
    )
    first = optimize_placement(problem, seed=3)
    single_attempt, failed = solver_core._assemble(problem, first.placements, 3)
    assert single_attempt.status is LayoutStatus.PARTIAL_INVALID  # one attempt cannot route it...
    assert failed  # ...and it names the net it could not lay

    layout = solve(problem, seed=3, effort="full")
    assert layout.status is LayoutStatus.VALID, layout.infeasibility  # ...another attempt does
    assert validate(problem, layout).ok
    assert layout.seed != 3  # it took a later attempt, not attempt 3
    assert solve(problem, seed=3, effort="full") == layout  # still deterministic


def test_solve_fork_auto_outputs_one_and_pipes_the_other() -> None:
    # m1 feeds both m2 and m3; its single auto-output covers one, the other is piped.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m1"), consumer("m2"), consumer("m3")],
        nets=[net("n1", "m1", "m2"), net("n2", "m1", "m3")],
    )
    layout = solve(problem)
    assert layout.status is LayoutStatus.VALID
    assert validate(problem, layout).ok
    assert len(layout.auto_connections) == 1  # one auto-output...
    assert len(layout.routes) == 1  # ...and the rest piped


def _output_machine(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="o", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )


def test_solve_downgrades_when_assembled_layout_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # place.ok && route.ok alone never proved the layout sound. With a buggy router that emits
    # a geometrically invalid route, solve() must run its own output through the independent
    # validator and downgrade VALID -> partial_invalid instead of passing it off as valid. (The
    # two output ports keep auto-output out of it, so the injected route is what gets validated.)
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_output_machine("a"), _output_machine("b")],
        nets=[
            Net(
                id="n",
                commodity=Commodity.ITEM,
                fluid_or_item="x",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="a", port_id="o"),
                    MachineFaceRef(machine_id="b", port_id="o"),
                ],
            )
        ],
    )
    teleport = Route(  # a single segment that jumps two cells - the validator must reject it
        net_id="n",
        commodity=Commodity.ITEM,
        segments=[Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=0, y=0, z=2), channel=0)],
    )
    monkeypatch.setattr(solver_core, "route", lambda *a, **k: RouteResult(routes=(teleport,)))

    layout = solve(problem)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "validation"
    assert validate(problem, layout).ok is False  # the bad route is preserved, not silently dropped


def test_solve_returns_an_explicit_partial_when_every_attempt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The give-up path of the never-silently-invalid promise. The machines place fine, but the
    # router is rigged to fail the same net on every attempt. Every attempt of the grid runs (they
    # are independent, so there is no failed-net history to stop on), and the result is an
    # explicit non-VALID layout that carries the infeasibility - never a silently-invalid one.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m0"), consumer("m1")],
        nets=[net("n", "m0", "m1")],
    )
    stuck = Infeasibility(constraint="routing", detail="rigged: net n never routes")

    def always_fails_the_same_net(
        prob: InputIR, placements: object, *, max_rounds: int | None = None
    ) -> RouteResult:
        return RouteResult(infeasibility=stuck, failed_nets=("n",))

    monkeypatch.setattr(solver_core, "route", always_fails_the_same_net)

    attempts = 0

    # Spelled out rather than forwarded through *args: this is the signature solve() actually
    # calls, so a parameter it gains or renames fails here instead of sliding through untyped.
    def counting_optimize(
        problem: InputIR,
        *,
        seed: int = 0,
        net_penalties: dict[str, float] | None = None,
        face_penalties: dict[str, float] | None = None,
        objective: Objective = "footprint",
        max_iterations: int | None = None,
    ) -> PlacementResult:
        nonlocal attempts
        attempts += 1
        assert not net_penalties  # every attempt anneals on its own...
        assert not face_penalties  # ...with nothing carried over from another
        return optimize_placement(
            problem, seed=seed, objective=objective, max_iterations=max_iterations
        )

    monkeypatch.setattr(solver_core, "optimize_placement", counting_optimize)

    layout = solve(problem, effort="full")
    assert layout.status is LayoutStatus.PARTIAL_INVALID  # not VALID
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "routing"  # the router's reason is surfaced...
    assert validate(problem, layout).ok is False  # ...and the stalled net is never certified valid
    assert attempts == solver_core._BUDGETS["full"].attempts


# ------------------------------------------------- a starved machine is a PLACEMENT defect (#106)


def _starving_power_line() -> tuple[InputIR, tuple[Placement, ...], tuple[Placement, ...]]:
    """One LV machine on a long region, plus the placements that starve it and that do not.

    The machine draws 32 EU/t through a single 2 A energy hatch, so its intake is
    ``2 A * delivered_volts`` and cable loss costs 1 V a block. The far placement routes 22 cable
    blocks, leaving 10 V and so 20 EU/t of the 32 it needs, while every segment of the trunk is
    correctly 1x - the shortfall is distance, nothing else. The hatch allowance is designed against
    a 16-block run (``dataset.DESIGN_RUN_BLOCKS``), so anything past that starves; past ~31 the
    voltage-drop rejection fires first and masks it, which is why the far placement sits at 24.
    The source faces WEST from x=0 so its feed face is on the region boundary, and nothing else is
    wrong with either layout.
    """
    sink = Machine(
        id="m",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        eut=32.0,
        faces=FaceSpec(
            ports=[
                Port(
                    id="power:in",
                    commodity=Commodity.POWER,
                    direction=IODirection.INPUT,
                    rate=32.0,
                    # Load-bearing: a port whose ceiling is unknown contributes nothing to the
                    # supply sum and marks its machine unmeasured, so a fixture that omits it
                    # would pass for the wrong reason, with the check never running. 2 A is what
                    # an energy hatch takes - the ceiling the adapter states for a machine whose
                    # structural record proves it has hatches.
                    max_amps=2.0,
                )
            ]
        ),
    )
    problem = InputIR(
        bounding_region=CellBox(sx=28, sy=3, sz=3),
        machines=[power_source("src", orientations=[Facing.WEST]), sink],
        nets=[
            Net(
                id="power:LV",
                commodity=Commodity.POWER,
                throughput=32.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="power:out"),
                    MachineFaceRef(machine_id="m", port_id="power:in"),
                ],
            )
        ],
    )
    source = at("src", 0, 0, 1, orientation=Facing.WEST)
    return problem, (source, at("m", 24, 0, 1)), (source, at("m", 4, 0, 1))


def test_a_starved_machine_names_its_power_net_as_the_failed_net() -> None:
    # The bug: every cable is thick enough, the router reports ok, and the validator kills the
    # layout for a shortfall that is purely distance-driven - so returning it with NO failed net
    # (the "a validation failure is a solver bug" rule) denied the loop the one signal that fixes
    # it. It must name the machine's power net, and say power_supply rather than blaming a bug.
    problem, far, near = _starving_power_line()
    layout, failed = solver_core._assemble(problem, far, 0)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert failed == ("power:LV",)
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "power_supply"
    assert "'m'" in layout.infeasibility.detail  # names the machine, and its shortfall
    assert "20 EU/t" in layout.infeasibility.detail
    # ...and the same machine 20 blocks nearer its source is simply valid: only distance differs.
    close, no_failures = solver_core._assemble(problem, near, 0)
    assert close.status is LayoutStatus.VALID, close.infeasibility
    assert no_failures == ()


def test_a_starved_power_net_is_named_failed_as_power_supply() -> None:
    # A starve names its power net as failed, but the cable WAS laid: the shortfall is distance,
    # which re-placing fixes and re-routing cannot, and the infeasibility says so.
    problem, far, _near = _starving_power_line()
    layout, failed = solver_core._assemble(problem, far, 0)
    assert failed == ("power:LV",)
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "power_supply"


def test_an_attempt_that_starves_a_machine_loses_to_one_that_places_it_nearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Attempt 0 strands the machine 24 blocks out and starves it; the next attempt places it near.
    # The starved layout ranks as one that left its power net unrouted, so the near one wins. The
    # placer is stubbed rather than annealed so the test pins the ranking, not whatever SA happens
    # to do spatially. No attempt is told about another's failure: they are independent.
    problem, far, near = _starving_power_line()
    penalties_seen: list[object] = []

    def stub_placer(prob: InputIR, **kwargs: object) -> PlacementResult:
        penalties_seen.append(kwargs.get("net_penalties"))
        return PlacementResult(placements=far if len(penalties_seen) == 1 else near)

    monkeypatch.setattr(solver_core, "optimize_placement", stub_placer)

    layout = solve(problem, effort="full")
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(problem, layout).ok
    assert {p.machine_id: p.cell for p in layout.placements}["m"] == near[1].cell
    assert not any(penalties_seen)


def test_a_starve_alongside_a_real_bug_still_steers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The short-circuit stays the backstop it was written for. A report that proves anything else
    # as well is a genuine placer/router bug, where re-placing cannot help, so the layout comes
    # back with no failed net and the generic validation infeasibility - not a power_supply one.
    problem, _far, near = _starving_power_line()
    mixed = ValidationReport(
        violations=(
            Violation(ViolationCode.POWER_SUPPLY_INSUFFICIENT, "starved", machine_id="m"),
            Violation(ViolationCode.MACHINE_OVERLAP, "rigged: a real geometric bug"),
        )
    )
    monkeypatch.setattr(solver_core, "validate", lambda *a, **k: mixed)

    layout, failed = solver_core._assemble(problem, near, 0)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert failed == ()
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "validation"


# ------------------------------------------------ the attempts may run in a pool of processes


class _RecordingPool:
    """Stands in for ``ProcessPoolExecutor``: runs each submitted call at once, in this process,
    and records which attempt seeds it was handed."""

    created: ClassVar[list[_RecordingPool]] = []

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.seeds: list[int] = []
        _RecordingPool.created.append(self)

    def __enter__(self) -> _RecordingPool:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def submit(self, fn: object, *args: object) -> object:
        assert callable(fn)
        self.seeds.append(args[2])  # type: ignore[arg-type]
        result = fn(*args)

        class _Done:
            def result(self) -> object:
                return result

        return _Done()


def test_the_pool_runs_every_attempt_after_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)  # any attempt is slow enough
    ir = adapt_file(_SAND)
    pooled = solve(ir, seed=3, jobs=4, effort="full")
    assert len(_RecordingPool.created) == 1
    pool = _RecordingPool.created[0]
    assert pool.max_workers == 4
    attempts = solver_core._BUDGETS["full"].attempts
    assert pool.seeds == list(range(4, 3 + attempts))  # attempt 0 ran here
    # The same layout as every attempt in one process.
    assert pooled == solve(ir, seed=3, effort="full")


def test_one_job_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)
    solve(adapt_file(_SAND), jobs=1, effort="full")
    assert _RecordingPool.created == []


def test_a_single_attempt_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # A minimal solve is one attempt, and attempt 0 always runs here: nothing is left for a pool.
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)
    solve(adapt_file(_SAND), jobs=4, effort="minimal")
    assert _RecordingPool.created == []


def test_a_quick_first_attempt_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Starting processes costs more than a quick line's remaining attempts, so it stays here.
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", float("inf"))
    solve(adapt_file(_SAND), jobs=4, effort="full")
    assert _RecordingPool.created == []


def test_a_real_pool_returns_the_same_layout_as_one_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attempts really cross a process boundary here (pickled out and back), and the layout is
    # still the one a single process finds: the number of jobs never changes the answer.
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)
    ir = adapt_file(_SAND)
    assert solve(ir, seed=1, jobs=2, effort="full") == solve(ir, seed=1, jobs=1, effort="full")


# ------------------------------------------------ effort: how hard the optimized path works


def _record_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[Objective, int, int | None]], list[int | None]]:
    """Record each attempt's ``(weighting, seed, anneal cap)`` and each routing's round cap."""
    anneals: list[tuple[Objective, int, int | None]] = []
    rounds: list[int | None] = []

    def recording_optimize(
        problem: InputIR,
        *,
        seed: int = 0,
        net_penalties: dict[str, float] | None = None,
        face_penalties: dict[str, float] | None = None,
        objective: Objective = "footprint",
        max_iterations: int | None = None,
    ) -> PlacementResult:
        anneals.append((objective, seed, max_iterations))
        return optimize_placement(
            problem, seed=seed, objective=objective, max_iterations=max_iterations
        )

    def recording_route(
        problem: InputIR, placements: Sequence[Placement], *, max_rounds: int | None = None
    ) -> RouteResult:
        rounds.append(max_rounds)
        return route(problem, placements, max_rounds=max_rounds)

    monkeypatch.setattr(solver_core, "optimize_placement", recording_optimize)
    monkeypatch.setattr(solver_core, "route", recording_route)
    return anneals, rounds


def test_minimal_effort_runs_one_short_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    anneals, rounds = _record_budgets(monkeypatch)
    solve(adapt_file(_SAND), seed=5, effort="minimal")
    assert anneals == [("footprint", 5, 250)]
    assert rounds == [8]


def test_full_effort_runs_the_whole_grid_on_the_stages_own_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anneals, rounds = _record_budgets(monkeypatch)
    solve(adapt_file(_SAND), seed=5, effort="full")
    assert anneals == [("footprint", s, None) for s in range(5, 13)]
    assert rounds
    assert set(rounds) == {None}


@pytest.mark.parametrize(
    ("effort", "grid"),
    [
        ("minimal", [("volume", 0)]),
        ("full", [(mode, s) for s in range(4) for mode in ("volume", "footprint")]),
    ],
)
def test_a_budget_below_one_seed_per_weighting_keeps_the_objectives_own(
    monkeypatch: pytest.MonkeyPatch, effort: Effort, grid: list[tuple[Objective, int]]
) -> None:
    # A volume solve alternates its own weighting with the footprint explorer, own first. The one
    # attempt a minimal solve has is the objective's own, not the explorer's.
    anneals, _ = _record_budgets(monkeypatch)
    solve(adapt_file(_SAND), objective="volume", effort=effort)
    assert [(mode, seed) for mode, seed, _ in anneals] == grid


def test_no_effort_named_takes_the_default_when_solve_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Read at call time, which is what lets tests/conftest.py make the whole suite minimal.
    anneals, _ = _record_budgets(monkeypatch)
    ir = adapt_file(_SAND)
    solve(ir)
    assert len(anneals) == 1  # the suite's default
    monkeypatch.setattr(solver_core, "DEFAULT_EFFORT", "full")
    solve(ir)
    assert len(anneals) == 1 + 8


# ------------------------------------------------ more rounds: a time budget or a round count
#
# Without either a solve is round 0 alone, and every test above is the proof that nothing about
# that changed: the same grid, the same pool decision, the same ranking.


def _without_rounds(layout: LayoutResult) -> LayoutResult:
    """``layout`` as a solve without a budget would have returned it: no ``rounds`` metric."""
    return layout.model_copy(update={"metrics": layout.metrics.model_copy(update={"rounds": None})})


def _ticking_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock that moves only when an attempt runs, one second each, so a full round takes 8."""
    now = [0.0]
    real = solver_core._attempt

    def ticking(*args: object) -> solver_core._Attempt:
        now[0] += 1.0
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(solver_core, "_now", lambda: now[0])
    monkeypatch.setattr(solver_core, "_attempt", ticking)


def test_a_time_budget_runs_the_rounds_that_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    # After round 0, 8 s spent plus 8 for the next fits in 20; after round 1, 16 + 8 does not. Round
    # 1 anneals the seeds after round 0's, so the two rounds cover seeds 5 to 20 once each.
    _ticking_clock(monkeypatch)
    anneals, _ = _record_budgets(monkeypatch)
    layout = solve(adapt_file(_SAND), seed=5, effort="full", time_budget=20.0)
    assert anneals == [("footprint", s, None) for s in range(5, 21)]
    assert layout.metrics.rounds == 2


def test_rounds_replays_a_timed_solve_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    _ticking_clock(monkeypatch)
    ir = adapt_file(_SAND)
    timed = solve(ir, seed=5, effort="full", time_budget=20.0)
    assert timed.metrics.rounds == 2
    assert solve(ir, seed=5, effort="full", rounds=2).model_dump() == timed.model_dump()


def test_a_budget_of_nothing_runs_round_0_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Round 0 always runs, and is exactly the solve without a budget.
    ir = adapt_file(_SAND)
    anneals, _ = _record_budgets(monkeypatch)
    layout = solve(ir, seed=5, effort="full", time_budget=0.0)
    assert anneals == [("footprint", s, None) for s in range(5, 13)]
    assert layout.metrics.rounds == 1
    assert _without_rounds(layout) == solve(ir, seed=5, effort="full")


def test_a_time_budget_stops_at_the_backstop() -> None:
    assert solver_core._another_round(63, None, 1e9, elapsed=0.0, last=0.0)
    assert not solver_core._another_round(
        solver_core._MAX_BUDGET_ROUNDS, None, 1e9, elapsed=0.0, last=0.0
    )
    # ...which binds a budget only: asked for a round count, a solve runs exactly that many.
    assert solver_core._another_round(100, 101, None, elapsed=0.0, last=0.0)
    assert not solver_core._another_round(1, None, None, elapsed=0.0, last=0.0)


@pytest.mark.parametrize(
    ("objective", "effort", "grid"),
    [
        ("footprint", "minimal", [("footprint", 5), ("footprint", 6)]),
        (
            "volume",
            "full",
            [(mode, s) for s in range(5, 13) for mode in ("volume", "footprint")],
        ),
    ],
)
def test_each_round_moves_every_seed_on_by_one_rounds_worth(
    monkeypatch: pytest.MonkeyPatch,
    objective: Objective,
    effort: Effort,
    grid: list[tuple[Objective, int]],
) -> None:
    # A full volume round is seeds s..s+3 of each weighting, so round 1 starts at s+4.
    anneals, _ = _record_budgets(monkeypatch)
    solve(adapt_file(_SAND), seed=5, objective=objective, effort=effort, rounds=2)
    assert [(mode, seed) for mode, seed, _ in anneals] == grid


@pytest.mark.parametrize("seed", [0, 1])
def test_more_rounds_never_rank_worse(seed: int) -> None:
    # Ranked across rounds in grid order, ties to the earliest: a strictly better later attempt is
    # the only thing that can replace round 0's layout.
    ir = adapt_file(_SAND)
    once = solve(ir, seed=seed, effort="full")
    more = solve(ir, seed=seed, effort="full", rounds=3)
    assert once.status is LayoutStatus.VALID
    assert more.status is LayoutStatus.VALID
    key_once = structure_quality(ir, once.placements, once.routes, "footprint")
    key_more = structure_quality(ir, more.placements, more.routes, "footprint")
    assert key_more <= key_once
    if key_more == key_once:
        assert _without_rounds(more) == once


def test_rounds_share_the_pool_round_0_started(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)
    ir = adapt_file(_SAND)
    pooled = solve(ir, seed=3, jobs=4, effort="full", rounds=3)
    assert len(_RecordingPool.created) == 1
    # Attempt 0 ran here, as ever; every later attempt of every round went to the one pool.
    assert _RecordingPool.created[0].seeds == list(range(4, 3 + 3 * 8))
    assert pooled == solve(ir, seed=3, effort="full", rounds=3)


def test_rounds_after_a_quick_round_0_stay_in_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", float("inf"))
    solve(adapt_file(_SAND), jobs=4, effort="full", rounds=2)
    assert _RecordingPool.created == []


def test_a_line_that_does_not_fit_stops_at_round_0() -> None:
    a = Machine(id="a", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    b = Machine(id="b", type="t", voltage_tier="LV", orientation_options=[Facing.NORTH])
    problem = InputIR(bounding_region=CellBox(sx=1, sy=1, sz=1), machines=[a, b], nets=[])
    layout = solve(problem, effort="full", rounds=5)
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.metrics.rounds == 1


def test_the_fast_path_ignores_rounds() -> None:
    layout = solve(adapt_file(_SAND), optimize=False, rounds=3)
    assert layout.metrics.rounds is None


@pytest.mark.parametrize(
    "budget",
    [
        {"time_budget": 10.0, "rounds": 2},
        {"rounds": 0},
        {"time_budget": -1.0},
        {"time_budget": float("inf")},
        {"time_budget": float("nan")},
    ],
)
def test_solve_refuses_a_budget_that_is_not_one(budget: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"time budget|rounds|time_budget"):
        solve(adapt_file(_SAND), **budget)  # type: ignore[arg-type]


# ----------------------------------------------------------------- merged item outputs (#249)


def _iron_shaped_plan() -> Plan:
    """iron.json's Ore Washer node in miniature: three single-block washers, each with an item in,
    a fluid in, three item outputs and power, which is six connections on five usable faces."""
    washer = Recipe(
        id="washer",
        machine_type="Ore Washer",
        eut=16.0,
        duration_ticks=400.0,
        inputs=[
            Resource(kind="item", id="gregtech:crushed.iron", amount=1.0),
            Resource(kind="fluid", id="water", amount=1000.0),
        ],
        outputs=[
            Resource(kind="item", id="gregtech:purified.iron", amount=1.0),
            Resource(kind="item", id="gregtech:dust.tiny.nickel", amount=1.0),
            Resource(kind="item", id="gregtech:dust.stone", amount=1.0),
        ],
        machine_handlers=[MachineHandler(id="h", kind="single", label="Basic Ore Washing Plant")],
    )
    return Plan(
        schema_version=1,
        recipes=[washer],
        nodes=[Node(id="washer", recipe_id="washer", overclock_tier="LV", machine_count=3)],
        storages=[Storage(id="ore", kind="item"), Storage(id="tank", kind="fluid")],
        edges=[
            Edge(
                id="feed",
                source="ore",
                target="washer",
                resource_kind="item",
                resource_id="gregtech:crushed.iron",
            ),
            Edge(
                id="water",
                source="tank",
                target="washer",
                resource_kind="fluid",
                resource_id="water",
            ),
        ],
    )


def test_a_single_block_short_of_faces_is_merged_and_solves_or_says_why() -> None:
    # [E2E] The adapter sends the three washers' items out of one face each, onto one shared trunk
    # to three Item Filters, and the solver lays that line. Whether one minimal attempt lays it validly is a question of layout
    # quality, which this suite does not judge; what it holds is that the answer is VALID and
    # validator-clean, or explicitly infeasible, and never a silently invalid layout.
    ir = to_input_ir(_iron_shaped_plan())

    washers = [m for m in ir.machines if m.type == "Ore Washer"]
    filters = [m for m in ir.machines if m.filter_items]
    trunks = [n for n in ir.nets if n.items]
    assert len(washers) == 3
    assert len(filters) == 3
    assert len(trunks) == 1
    assert all(m.type == "Ultra Low Voltage Item Filter" for m in filters)
    for f in filters:
        (resource,) = f.filter_items
        out = next(p for p in f.faces.ports if p.direction is IODirection.OUTPUT)
        assert out.id == f"output:{resource}"
        assert out.faces == (RelativeFace.BACK,)
        assert f.allowed_faces(out.id, Facing.NORTH) == {Facing.SOUTH}
    assert not single_block_shortfalls(ir), "no washer is short of faces once merged"

    layout = solve(ir)
    if layout.status is LayoutStatus.VALID:
        assert validate(ir, layout).ok
        assert sum(p.machine_id.startswith("item-filter:") for p in layout.placements) == 3
    else:
        assert layout.infeasibility is not None  # incompleteness is never silent
        assert layout.infeasibility.constraint != "single_block_faces"
