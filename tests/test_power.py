"""Tests for the shared-amperage power router (router.power).

Headline: a power net is sized to the *summed* amperage flowing through each cable segment -
the trunk tapers from the source. Plus the never-silently-invalid promise: an over-16x load,
an unknown tier, or a malformed net is an explicit infeasibility, and the routes it does emit
validate.
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
    METoggles,
    Net,
    Placement,
    Port,
)
from gtnh_solver.router import route_power
from gtnh_solver.validator import validate

_POWER = Commodity.POWER


def _src(mid: str = "src") -> Machine:
    return Machine(
        id=mid,
        type="Power Source (LV)",
        voltage_tier="LV",
        eut=0.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="power:out", commodity=_POWER, direction=IODirection.OUTPUT)]
        ),
    )


def _load(mid: str, eut: float, *, tier: str = "LV") -> Machine:
    return Machine(
        id=mid,
        type="M",
        voltage_tier=tier,
        eut=eut,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[Port(id="power:in", commodity=_POWER, direction=IODirection.INPUT)]),
    )


def _pnet(*machine_ids: str, source: str = "src") -> Net:
    return Net(
        id="power:LV",
        commodity=_POWER,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=source, port_id="power:out"),
            *(MachineFaceRef(machine_id=m, port_id="power:in") for m in machine_ids),
        ],
    )


def _at(mid: str, x: int, y: int, z: int) -> Placement:
    return Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=Facing.NORTH)


def test_power_trunk_sizes_thickness_by_summed_amperage() -> None:
    # source + two 1-amp machines (LV, eut=32). Leg source->m0 carries both (2 amps -> 2x);
    # leg m0->m1 carries one (1 amp -> 1x). The trunk tapers from the source.
    problem = InputIR(
        bounding_region=CellBox(sx=10, sy=4, sz=10),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
    )
    placements = [_at("src", 0, 0, 0), _at("m0", 2, 0, 0), _at("m1", 4, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok
    route = result.routes[0]
    assert route.commodity is _POWER
    assert route.thickness_per_segment is not None
    assert len(route.thickness_per_segment) == len(route.segments)
    assert set(route.thickness_per_segment) == {1, 2}  # the 2-amp leg and the 1-amp leg
    assert max(route.thickness_per_segment) == 2
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_trunk_stays_a_tree_when_legs_would_otherwise_overlap() -> None:
    # Source in the MIDDLE of its two sinks: the m0->m1 leg's shortest path retraces the
    # src->m0 leg, so routing each leg independently would lay overlapping segments - a tangle
    # whose per-segment amperage is undefined (the validator rejects it as POWER_ROUTE_NOT_A_TREE).
    # The router avoids the cells it already laid, so the trunk is a single non-overlapping tree.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
    )
    placements = [_at("src", 2, 0, 0), _at("m0", 0, 0, 0), _at("m1", 4, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    edges = [
        ((s.start.x, s.start.y, s.start.z), (s.end.x, s.end.y, s.end.z)) for s in route.segments
    ]
    nodes = {c for e in edges for c in e}
    assert len(edges) == len(set(edges))  # no segment laid twice
    assert len(edges) == len(nodes) - 1  # a tree on N nodes has exactly N-1 edges
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_cable_routes_around_extra_obstacles() -> None:
    # The solver passes the item/fluid pipe cells as extra_obstacles so a cable never shares a
    # cell with a pipe (crude single-channel capacity). Block the straight path's middle cell and
    # the trunk must detour around it - still feasible and validator-clean.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    placements = [_at("src", 0, 0, 0), _at("m0", 2, 0, 0)]
    blocked = {(1, 0, 1)}  # the cell the straight src->m0 cable would otherwise use
    result = route_power(problem, placements, extra_obstacles=blocked)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    cells = {(s.start.x, s.start.y, s.start.z) for s in route.segments} | {
        (s.end.x, s.end.y, s.end.z) for s in route.segments
    }
    assert blocked.isdisjoint(cells)  # the cable routed around the obstacle
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_single_machine_thickness_matches_its_amperage() -> None:
    # one machine drawing 3 amps at LV (eut just over 2 amps: 65 > 64) -> a 4x cable.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 65)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [_at("src", 0, 0, 0), _at("m0", 2, 0, 0)])
    assert result.ok
    assert set(result.routes[0].thickness_per_segment or []) == {4}  # ceil(65/32)=3 -> 4x


def test_power_rejects_over_16x_amperage() -> None:
    # 17 amps at LV (eut > 16 * 32 = 512) needs more than a 16x cable -> rejected, not certified.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 544)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [_at("src", 0, 0, 0), _at("m0", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "amperage"


def test_power_unknown_tier_is_infeasible() -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32, tier="NOPE")],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [_at("src", 0, 0, 0), _at("m0", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "voltage_tier"


def test_power_net_without_a_source_is_infeasible() -> None:
    # two INPUT machines, no OUTPUT source -> cannot form a trunk
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_load("m0", 32), _load("m1", 32)],
        nets=[
            Net(
                id="power:LV",
                commodity=_POWER,
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="m0", port_id="power:in"),
                    MachineFaceRef(machine_id="m1", port_id="power:in"),
                ],
            )
        ],
    )
    result = route_power(problem, [_at("m0", 0, 0, 0), _at("m1", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "power_net"
    assert result.failed_nets == ("power:LV",)  # the stalled net, for the solver's feedback loop


def test_power_me_toggled_is_skipped() -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
        me_toggles=METoggles(power=True),
    )
    result = route_power(problem, [_at("src", 0, 0, 0), _at("m0", 2, 0, 0)])
    assert result.ok
    assert result.routes == ()  # power is on the ME network; nothing is cabled


def test_power_infeasible_when_a_terminal_cannot_dock() -> None:
    # a 2x1x1 region fully filled leaves no free non-front face to dock a power terminal
    problem = InputIR(
        bounding_region=CellBox(sx=2, sy=1, sz=1),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [_at("src", 0, 0, 0), _at("m0", 1, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"


def test_power_infeasible_when_no_path_between_terminals() -> None:
    # a reserved wall at x=1 splits the single-layer region; the cable cannot cross
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
        reserved_cells=[CellCoord(x=1, y=0, z=z) for z in range(3)],
    )
    result = route_power(problem, [_at("src", 0, 0, 0), _at("m0", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "routing"
