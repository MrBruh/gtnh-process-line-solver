"""Tests for the Phase 1 solver (place + auto-output + item/fluid + power route).

Headline: solving the real sand line yields a fully valid layout whose item chain **auto-feeds
with zero pipes** and whose synthesized power net is cabled as a shared-amperage trunk. Plus the
invariants: the result is always either VALID-and-validator-clean or
non-VALID-with-an-explicit-infeasibility.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

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
    PipeSize,
    Placement,
    Port,
    Route,
    Segment,
)
from gtnh_solver.placement import Objective, PlacementResult, optimize_placement, place
from gtnh_solver.router import RouteResult, assign_auto_outputs
from gtnh_solver.solver import core as solver_core
from gtnh_solver.solver import solve
from gtnh_solver.validator import ValidationReport, Violation, ViolationCode, validate
from tests._helpers import at, consumer, net, power_source, producer

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"
_PARALLEL_SAND = _EXAMPLES / "gtnh-parallel-sand.json"


def test_solve_sand_items_auto_feed_and_power_is_cabled(
    solved_sand: tuple[InputIR, LayoutResult],
) -> None:
    ir, layout = solved_sand
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


def test_solve_sand_optimized_matches_or_beats_the_hand_built_target(
    solved_sand: tuple[InputIR, LayoutResult],
) -> None:
    # The acceptance target (docs/ROADMAP.md lane C): the maintainer hand-builds the sand line in
    # a 3x2x2 volume with 3 power cables, so the optimizer must find that or better - VALID, the
    # whole built structure (machines + routes) on a floor area <= 3x2 = 6 cells, and <= 3 power
    # cable cells. The quality-driven feedback loop is what finds it: it routes every attempt and
    # keeps the best by (footprint, cable cells, volume) instead of returning the first valid.
    _, layout = solved_sand
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
    # A tight single-layer fan-out graph where the seed-0 placement strands a net - the router
    # cannot lay its pipe in the congested layout, so one assembly attempt is partial_invalid.
    # Another seed of the multi-start places it differently and routes cleanly: solve() returns
    # VALID where a single attempt did not. (The seed is whichever one the annealer happens to
    # strand: it was seed 1 while the LNS recreate priced compactness without the one-cell nudge,
    # #254, and is seed 0 again with both.)
    edges = [("m0", "m2"), ("m0", "m3"), ("m1", "m3"), ("m1", "m4"), ("m2", "m5"), ("m4", "m5")]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=7),
        machines=[_io_machine(f"m{i}") for i in range(6)],
        nets=[_edge(f"e{k}", a, b) for k, (a, b) in enumerate(edges)],
    )
    first = optimize_placement(problem, seed=0)
    single_attempt, failed = solver_core._assemble(problem, first.placements, 0)
    assert single_attempt.status is LayoutStatus.PARTIAL_INVALID  # one attempt cannot route it...
    assert failed  # ...and it names the net it could not lay

    layout = solve(problem)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility  # ...another attempt does
    assert validate(problem, layout).ok
    assert layout.seed != 0  # it took a later attempt, not attempt 0
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
        prob: InputIR, placements: object, *, reserved: object = ()
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
    ) -> PlacementResult:
        nonlocal attempts
        attempts += 1
        assert not net_penalties  # every attempt anneals on its own...
        assert not face_penalties  # ...with nothing carried over from another
        return optimize_placement(problem, seed=seed, objective=objective)

    monkeypatch.setattr(solver_core, "optimize_placement", counting_optimize)

    layout = solve(problem)
    assert layout.status is LayoutStatus.PARTIAL_INVALID  # not VALID
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "routing"  # the router's reason is surfaced...
    assert validate(problem, layout).ok is False  # ...and the stalled net is never certified valid
    assert attempts == solver_core._ATTEMPTS


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

    layout = solve(problem)
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
    pooled = solve(ir, seed=3, jobs=4)
    assert len(_RecordingPool.created) == 1
    pool = _RecordingPool.created[0]
    assert pool.max_workers == 4
    assert pool.seeds == list(range(4, 3 + solver_core._ATTEMPTS))  # attempt 0 ran here
    assert pooled == solve(ir, seed=3)  # the same layout as every attempt in one process


def test_one_job_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)
    solve(adapt_file(_SAND), jobs=1)
    assert _RecordingPool.created == []


def test_a_quick_first_attempt_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Starting processes costs more than a quick line's remaining attempts, so it stays here.
    _RecordingPool.created = []
    monkeypatch.setattr(solver_core, "ProcessPoolExecutor", _RecordingPool)
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", float("inf"))
    solve(adapt_file(_SAND), jobs=4)
    assert _RecordingPool.created == []


def test_a_real_pool_returns_the_same_layout_as_one_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attempts really cross a process boundary here (pickled out and back), and the layout is
    # still the one a single process finds: the number of jobs never changes the answer.
    monkeypatch.setattr(solver_core, "_POOL_AFTER_S", 0.0)
    ir = adapt_file(_SAND)
    assert solve(ir, seed=1, jobs=2) == solve(ir, seed=1, jobs=1)
