"""Tests for the Phase 1 solver (place + auto-output + item/fluid + power route).

Headline: solving the real sand line yields a fully valid layout whose item chain **auto-feeds
with zero pipes** and whose synthesized power net is cabled as a shared-amperage trunk. Plus the
invariants: the result is always either VALID-and-validator-clean or
non-VALID-with-an-explicit-infeasibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
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
    Port,
    Route,
    Segment,
)
from gtnh_solver.placement import optimize_placement, place
from gtnh_solver.router import RouteResult, assign_auto_outputs
from gtnh_solver.solver import core as solver_core
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate
from tests._helpers import consumer, net, producer

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"


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


def test_solve_sand_optimized_matches_or_beats_the_hand_built_target() -> None:
    # The acceptance target (docs/ROADMAP.md lane C): the maintainer hand-builds the sand line in
    # a 3x2x2 volume with 3 power cables, so the optimizer must find that or better - VALID, the
    # whole built structure (machines + routes) on a floor area <= 3x2 = 6 cells, and <= 3 power
    # cable cells. The quality-driven feedback loop is what finds it: it routes every attempt and
    # keeps the best by (footprint, cable cells, volume) instead of returning the first valid.
    ir = adapt_file(_SAND)
    layout = solve(ir)
    assert layout.status is LayoutStatus.VALID
    footprint, _, cables = _structure_metrics(layout)
    assert footprint <= 6, f"structure footprint {footprint} exceeds the hand-built 3x2"
    assert cables <= 3, f"{cables} power cable cells exceed the hand-built 3"


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


def test_solve_is_deterministic() -> None:
    ir = adapt_file(_SAND)
    assert solve(ir) == solve(ir)


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


def test_solve_returns_valid_or_explicit_infeasibility() -> None:
    ir = adapt_file(_NITROBENZENE)
    layout = solve(ir)
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


def test_solve_populates_footprint_and_layer_metrics() -> None:
    # LayoutMetrics is produced, not just declared (GitHub #13): the previewer reads footprint and
    # layers off the returned layout, and the seed-compare workflow ranks on them.
    ir = adapt_file(_SAND)
    layout = solve(ir)
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
    seed_autos, _ = assign_auto_outputs(problem, place(problem).placements)
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


def test_feedback_loop_recovers_a_layout_a_single_attempt_leaves_partial() -> None:
    # A tight single-layer fan-out graph where the seed-0 placement strands a net - the router
    # cannot lay its pipe in the congested layout, so one assembly attempt is partial_invalid.
    # The place<->route feedback loop penalizes the failed net and re-places (next seed), and that
    # placement routes cleanly: solve() returns VALID where a single attempt did not.
    edges = [("m0", "m2"), ("m0", "m3"), ("m1", "m3"), ("m1", "m4"), ("m2", "m5"), ("m4", "m5")]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=7),
        machines=[_io_machine(f"m{i}") for i in range(6)],
        nets=[_edge(f"e{k}", a, b) for k, (a, b) in enumerate(edges)],
    )
    seed0 = optimize_placement(problem, seed=0)
    single_attempt, failed = solver_core._assemble(problem, seed0.placements, 0)
    assert single_attempt.status is LayoutStatus.PARTIAL_INVALID  # one attempt cannot route it...
    assert failed  # ...and it names the net it could not lay (the feedback signal)

    layout = solve(problem)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility  # ...but the loop recovers it
    assert validate(problem, layout).ok
    assert layout.seed != 0  # it took a later attempt (different seed + penalty), not attempt 0
    assert solve(problem) == solve(problem)  # still deterministic


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


def test_solve_gives_up_when_the_same_net_fails_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The give-up path of the never-silently-invalid promise. The machines place fine, but the
    # router is rigged to fail the SAME net on every attempt. The feedback loop must notice the
    # failed-net set repeating (re-placing is making no progress), STOP instead of spinning the
    # whole multi-start grid, and return an explicit non-VALID layout that carries the
    # infeasibility - never a silently-invalid result.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m0"), consumer("m1")],
        nets=[net("n", "m0", "m1")],
    )
    stuck = Infeasibility(constraint="routing", detail="rigged: net n never routes")

    def always_fails_the_same_net(prob: InputIR, placements: object) -> RouteResult:
        return RouteResult(infeasibility=stuck, failed_nets=("n",))

    monkeypatch.setattr(solver_core, "route", always_fails_the_same_net)

    attempts = 0
    real_optimize = solver_core.optimize_placement

    def counting_optimize(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        return real_optimize(*args, **kwargs)

    monkeypatch.setattr(solver_core, "optimize_placement", counting_optimize)

    layout = solve(problem)
    assert layout.status is LayoutStatus.PARTIAL_INVALID  # not VALID, and not a spin
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "routing"  # the router's reason is surfaced...
    assert validate(problem, layout).ok is False  # ...and the stalled net is never certified valid
    # It broke on the second attempt's repeated failed-net set, not after exhausting the full grid.
    assert 1 < attempts < solver_core._MAX_FEEDBACK_PASSES
