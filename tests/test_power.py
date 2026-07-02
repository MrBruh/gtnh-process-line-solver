"""Tests for the shared-amperage power router (router.power).

Headline: a power net is a cable TREE sized to the *summed* amperage flowing through each
segment - the trunk tapers from the source, branches carry only their own sinks, and a sink
whose dock candidate is already a trunk cell taps it instead of laying new cable. Plus the
never-silently-invalid promise: an over-16x load, an unknown tier, or a malformed net is an
explicit infeasibility, and the routes it does emit validate.
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
    Port,
)
from gtnh_solver.router import route_power
from gtnh_solver.router.power import _route_pass
from gtnh_solver.validator import validate
from tests._helpers import at, power_source

_POWER = Commodity.POWER


def _src(mid: str = "src") -> Machine:
    # The shared LV power source; kept as a one-liner alias so the many call sites stay readable.
    return power_source(mid)


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


def _tier_net(net_id: str, source: str, sink: str) -> Net:
    # A distinct per-tier power net (one source + one sink) - unlike ``_pnet`` this takes an
    # explicit id and source, so several tiers can coexist in one problem.
    return Net(
        id=net_id,
        commodity=_POWER,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=source, port_id="power:out"),
            MachineFaceRef(machine_id=sink, port_id="power:in"),
        ],
    )


def test_power_trunk_sizes_thickness_by_summed_amperage() -> None:
    # source + two full-tier machines (LV, eut=32) in a row: src(0,0,0), m0(3,0,0), m1(6,0,0).
    # (At a 2-block spacing m0 would simply tap the source's dock cell - the shared-tap test
    # below covers that; this spacing keeps a genuinely shared segment.) The source docks east
    # at (1,0,0), the tree root; m0's leg is the 1-hop (1,0,0)->(2,0,0), so m0 sits 1 block out
    # -> 31 V -> ceil(32/31)=2 amps. m1's leg extends the tree from (2,0,0) around m0's body,
    # 5 hops -> depth 6 -> 26 V -> 2 amps. The shared segment (1,0,0)->(2,0,0) carries both
    # sinks (4 amps -> 4x); the five m1-only segments carry 2 amps -> 2x. The trunk tapers from
    # the source, thickened for loss.
    problem = InputIR(
        bounding_region=CellBox(sx=10, sy=4, sz=10),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
    )
    placements = [at("src", 0, 0, 0), at("m0", 3, 0, 0), at("m1", 6, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok
    route = result.routes[0]
    assert route.commodity is _POWER
    assert route.thickness_per_segment is not None
    assert len(route.thickness_per_segment) == len(route.segments) == 6
    assert set(route.thickness_per_segment) == {2, 4}  # the 4-amp shared segment + the 2-amp rest
    assert max(route.thickness_per_segment) == 4
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_trunk_stays_a_tree_when_legs_would_otherwise_overlap() -> None:
    # Source in the MIDDLE of its two sinks: m0 taps the source's dock cell (it is adjacent to
    # it), and m1's leg lays fresh cable around the source body without ever re-using a laid
    # cell - overlapping segments would be a tangle whose per-segment amperage is undefined (the
    # validator rejects it as POWER_ROUTE_NOT_A_TREE). The trunk must stay a single tree.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
    )
    placements = [at("src", 2, 0, 0), at("m0", 0, 0, 0), at("m1", 4, 0, 0)]
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
    placements = [at("src", 0, 0, 0), at("m0", 2, 0, 0)]
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


def test_power_docks_on_the_face_toward_the_trunk_not_a_fixed_side() -> None:
    # The source sits five blocks east of its lone sink, the +z (south) face free on both. Fixed
    # south-first docking put both terminals on the z=1 row and ran the cable the long way (5); the
    # router now docks each machine on the face pointing at the other, so the cable runs straight
    # through the gap between them (3). Routing - not a fixed face order - chose the faces.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 1)],
        nets=[_pnet("m0")],
    )
    placements = [at("src", 5, 0, 0), at("m0", 0, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    faces = {t.machine_id: t.face for t in route.terminals}
    assert faces["src"] is Facing.WEST  # toward the sink, not the default south (+z)
    assert faces["m0"] is Facing.EAST  # toward the source
    assert len(route.segments) == 3  # straight run x=4..1, not the 5-long south detour
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_single_machine_thickness_matches_its_amperage() -> None:
    # one machine at LV, eut=65, docked 2 blocks out (30 V after loss) -> ceil(65/30)=3 amps -> 4x.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 65)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 2, 0, 0)])
    assert result.ok
    assert set(result.routes[0].thickness_per_segment or []) == {4}  # ceil(65/30)=3 -> 4x


def test_power_loss_thickens_a_far_cable_past_its_lossless_size() -> None:
    # 16 EU/t at LV is 1 amp at the source (16/32) and would stay 1 amp lossless, but docked toward
    # the source the machine is 18 blocks out -> 14 V delivered -> ceil(16/14)=2 amps -> a 2x cable.
    # Loss, not the raw draw, is what thickened it - the far machine would starve on a 1x run.
    problem = InputIR(
        bounding_region=CellBox(sx=30, sy=4, sz=10),
        machines=[_src(), _load("m0", 16)],
        nets=[_pnet("m0")],
    )
    placements = [at("src", 0, 0, 0), at("m0", 20, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    assert set(route.thickness_per_segment or []) == {2}  # 1 amp lossless, 2 with 20 blocks of loss
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_rejects_a_run_too_long_to_keep_voltage() -> None:
    # LV is 32 V and the cable loses 1 V/block, so a machine >= 32 blocks out receives <= 0 V -
    # unpowerable at this tier no matter how thick the cable. Rejected, not silently certified.
    problem = InputIR(
        bounding_region=CellBox(sx=40, sy=4, sz=10),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 35, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "voltage_drop"
    assert result.failed_nets == ("power:LV",)  # the stalled net, for the solver's feedback loop


def test_power_rejects_over_16x_amperage() -> None:
    # 17 amps at LV (eut > 16 * 32 = 512) needs more than a 16x cable -> rejected, not certified.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 544)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "amperage"


def test_power_unknown_tier_is_infeasible() -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32, tier="NOPE")],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 2, 0, 0)])
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
    result = route_power(problem, [at("m0", 0, 0, 0), at("m1", 2, 0, 0)])
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
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 2, 0, 0)])
    assert result.ok
    assert result.routes == ()  # power is on the ME network; nothing is cabled


def test_power_infeasible_when_a_terminal_cannot_dock() -> None:
    # a 2x1x1 region fully filled leaves no free non-front face to dock a power terminal
    problem = InputIR(
        bounding_region=CellBox(sx=2, sy=1, sz=1),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 1, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"


def test_power_infeasible_when_a_net_machine_is_unplaced() -> None:
    # a power net endpoint whose machine never got a placement has no faces to dock on
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0)])  # m0 is missing a placement
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
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "routing"


def test_power_branched_trunk_sizes_each_branch_to_its_own_sink() -> None:
    # Source between two sinks on opposite sides: src(3,0,0), m0(0,0,0), m1(6,0,0). The source
    # docks west at (2,0,0) (toward its first sink); m0's leg is the 1-hop (2,0,0)->(1,0,0), and
    # m1's leg leaves the SAME root cell the other way (5 hops around the source body), so the
    # tree BRANCHES at the root. Each branch carries only its own sink: m0 at depth 1 -> 31 V ->
    # 2 amps, m1 at depth 5 -> 27 V -> 2 amps -> every segment is 2x. A path-trunk suffix sum
    # would have overcharged the m0 branch with m1's amps too (4 amps -> 4x).
    problem = InputIR(
        bounding_region=CellBox(sx=10, sy=4, sz=10),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
    )
    placements = [at("src", 3, 0, 0), at("m0", 0, 0, 0), at("m1", 6, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    assert route.thickness_per_segment is not None
    # Select the m0 branch by its cells - the root (2,0,0) -> (1,0,0) leg - not by lay order: the
    # contract is that each branch is sized to its own sink, not which one is emitted first.
    thickness_by_edge = {
        frozenset(
            {(s.start.x, s.start.y, s.start.z), (s.end.x, s.end.y, s.end.z)}
        ): route.thickness_per_segment[i]
        for i, s in enumerate(route.segments)
    }
    m0_branch = frozenset({(2, 0, 0), (1, 0, 0)})
    assert m0_branch in thickness_by_edge  # the m0 branch exists as its own single-hop leg
    assert thickness_by_edge[m0_branch] == 2  # not 4x: m1's amps do not ride this branch
    assert set(route.thickness_per_segment) == {2}
    branches = sum(1 for s in route.segments if (s.start.x, s.start.y, s.start.z) == (2, 0, 0))
    assert branches == 2  # both legs leave the root cell - a real branch, not a path
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_sink_taps_the_sources_dock_cell() -> None:
    # src(0,0,0), m0(2,0,0), m1(4,0,0). The source docks east at (1,0,0); that same cell is m0's
    # west dock candidate, so m0 TAPS it - terminal on the trunk cell, distance 0, no new cable
    # (GT: one cable block feeds every adjacent wired face). m1 then extends the trunk with a
    # 4-hop leg around m0's body (depth 4 -> 28 V -> 2 amps). Trunk total: 5 cells / 4 segments,
    # each carrying only m1's 2 amps (m0 draws straight from the root and loads no segment). The
    # old distinct-dock path-trunk needed 2 extra cells just to give m0 its own dock.
    problem = InputIR(
        bounding_region=CellBox(sx=10, sy=4, sz=10),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
    )
    placements = [at("src", 0, 0, 0), at("m0", 2, 0, 0), at("m1", 4, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    cell_of = {t.machine_id: (t.cell.x, t.cell.y, t.cell.z) for t in route.terminals}
    assert cell_of["m0"] == cell_of["src"] == (1, 0, 0)  # the tap: two terminals, one cable cell
    cells = {
        c
        for s in route.segments
        for c in ((s.start.x, s.start.y, s.start.z), (s.end.x, s.end.y, s.end.z))
    }
    assert len(cells) == 5  # the root + m1's 4-hop leg; no cable spent on m0
    assert len(route.segments) == 4
    assert set(route.thickness_per_segment or []) == {2}  # only m1's 2 amps ride the cable
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_lone_sink_adjacent_to_the_root_still_gets_a_cable() -> None:
    # The lone sink's west dock candidate IS the source's dock cell (1,0,0), but tapping it
    # would leave a route with zero segments - invalid (ROUTE_DISCONTINUOUS). The last sink of a
    # still-segment-less trunk therefore lays a real leg: 2 hops to another of its faces
    # (depth 2 -> 30 V -> ceil(32/30)=2 amps).
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    placements = [at("src", 0, 0, 0), at("m0", 2, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    assert len(route.segments) >= 1  # never a zero-segment power route
    assert len(route.segments) == 2
    assert set(route.thickness_per_segment or []) == {2}
    cell_of = {t.machine_id: (t.cell.x, t.cell.y, t.cell.z) for t in route.terminals}
    assert cell_of["m0"] != cell_of["src"]  # no tap: the sink docked on a fresh cell
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_cluster_of_three_sinks_needs_at_most_three_cable_cells() -> None:
    # The sand-layout acceptance shape: a source + 3 sinks packed around a short trunk must not
    # need more cable than the maintainer's hand-built 3 cells (the path-trunk's distinct docks
    # forced >= 4). Here the trunk is 2 cells: the root (1,0,0) - which m0 taps - plus one riser
    # (1,1,0) laid for m1, which m2 then taps. m0 draws at distance 0 (1 amp, loads no segment);
    # m1 and m2 draw at depth 1 -> 31 V -> 2 amps each, so the lone segment carries 4 -> 4x.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32), _load("m1", 32), _load("m2", 32)],
        nets=[_pnet("m0", "m1", "m2")],
    )
    placements = [
        at("src", 0, 0, 0),
        at("m0", 2, 0, 0),
        at("m1", 0, 1, 0),
        at("m2", 2, 1, 0),
    ]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    cells = {
        c
        for s in route.segments
        for c in ((s.start.x, s.start.y, s.start.z), (s.end.x, s.end.y, s.end.z))
    }
    assert len(cells) <= 3  # the acceptance bar; this cluster actually trunks with 2
    assert cells == {(1, 0, 0), (1, 1, 0)}
    assert route.thickness_per_segment == [4]  # m1 + m2 (2 amps each at depth 1) share the riser
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_tap_picks_the_trunk_cell_nearest_the_source() -> None:
    # m1 is adjacent to two trunk cells: the root (1,0,0) on its west face, and (2,0,1) - laid
    # for m0 at depth 2 - on its south face, which comes FIRST in dock-candidate order. The tap
    # must pick the cell with the smallest tree depth (the root, distance 0), not the first
    # candidate, or m1 would be sized as if it sat 2 blocks of cable loss out. (0,0,1) is
    # reserved so the source deterministically docks east at (1,0,0).
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[_src(), _load("m0", 32), _load("m1", 32)],
        nets=[_pnet("m0", "m1")],
        reserved_cells=[CellCoord(x=0, y=0, z=1)],
    )
    placements = [at("src", 0, 0, 0), at("m0", 3, 0, 1), at("m1", 2, 0, 0)]
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    route = result.routes[0]
    cell_of = {t.machine_id: (t.cell.x, t.cell.y, t.cell.z) for t in route.terminals}
    assert cell_of["m1"] == (1, 0, 0)  # the root (depth 0), not the deeper (2,0,1) candidate
    # m0's 2-hop leg (1,0,0)->(1,0,1)->(2,0,1) puts m0 at depth 2 -> 30 V -> 2 amps; m1 taps the
    # root and rides no segment, so both segments carry m0 alone.
    assert route.thickness_per_segment == [2, 2]
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=[route])
    assert validate(problem, layout).ok, str(validate(problem, layout))


def test_power_infeasible_when_the_only_dock_cell_is_the_segmentless_trunk() -> None:
    # In a 3x1x1 corridor the lone free cell (1,0,0) is both the source's dock and the sink's
    # only candidate. The sink cannot tap it (a zero-segment route is invalid) and has no other
    # goal to route to -> an explicit face_reachability infeasibility, never a bogus route.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=1),
        machines=[_src(), _load("m0", 32)],
        nets=[_pnet("m0")],
    )
    result = route_power(problem, [at("src", 0, 0, 0), at("m0", 2, 0, 0)])
    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"
    assert result.failed_nets == ("power:LV",)


def test_power_routing_is_deterministic() -> None:
    # Same input twice -> identical routes (terminals, segments, thicknesses), even where
    # several equally-short legs exist: taps, A* seeding, and tie-breaks are all order-stable.
    problem = InputIR(
        bounding_region=CellBox(sx=10, sy=4, sz=10),
        machines=[_src(), _load("m0", 32), _load("m1", 32), _load("m2", 32)],
        nets=[_pnet("m0", "m1", "m2")],
    )
    placements = [
        at("src", 0, 0, 0),
        at("m0", 2, 0, 0),
        at("m1", 4, 0, 0),
        at("m2", 4, 0, 2),
    ]
    first = route_power(problem, placements)
    second = route_power(problem, placements)
    assert first.ok
    assert second.ok
    assert first.routes == second.routes


def test_power_rip_up_reroute_fixes_a_tier_ordering_wedge() -> None:
    # The power analogue of the item router's rip-up/reroute test (test_router.py): a wall at z=3
    # with two gaps (x=1 and x=5), plus x=2 walled for z<3, makes a top-left pocket (x=0..1,
    # z=0..2) whose only exit down is gap x=1. Tier T2 (source s2 in the pocket -> sink k2 below)
    # can ONLY leave through gap x=1; tier T1 (source s1 top-right -> sink k1 below) prefers gap
    # x=1 but can detour to gap x=5. In problem order T1's trunk routes first, grabs gap x=1 and
    # wedges T2 out - a false infeasibility from tier order alone. Failed-first rip-up/reroute
    # reorders T2 first and T1 detours, so cross-tier power routing is not hostage to net order.
    reserved = [CellCoord(x=x, y=0, z=3) for x in range(7) if x not in (1, 5)] + [
        CellCoord(x=2, y=0, z=z) for z in range(3)
    ]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=6),
        machines=[_src("s1"), _load("k1", 1), _src("s2"), _load("k2", 1)],
        nets=[_tier_net("power:T1", "s1", "k1"), _tier_net("power:T2", "s2", "k2")],
        reserved_cells=reserved,
    )
    placements = [at("s1", 3, 0, 0), at("k1", 0, 0, 5), at("s2", 0, 0, 0), at("k2", 0, 0, 4)]

    # One greedy pass in problem order wedges T2 out (T1's trunk took the pocket's only exit)...
    _, failures = _route_pass(problem, placements, list(problem.nets))
    assert failures, "expected the problem-order pass to fail a tier"
    assert "power:T2" in failures
    # ...but failed-first rip-up/reroute reorders (failed tier first) and routes both, cleanly.
    result = route_power(problem, placements)
    assert result.ok, result.infeasibility
    assert len(result.routes) == 2
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    assert validate(problem, layout).ok, str(validate(problem, layout))
    # Same seed -> same result: the retry reorder is deterministic, no unseeded randomness.
    assert route_power(problem, placements).routes == result.routes


def test_power_reports_every_still_failing_net_not_just_the_first() -> None:
    # Two independent tiers that both genuinely fail (each sink draws 17 amps at LV, eut > 16*32 =
    # 512, over the 16x cap) - no reorder can rescue them. The router must report BOTH still-
    # failing nets (parity with the item router), so the solver's feedback loop can penalize them
    # all, with the first one's specific reason carried on ``infeasibility``.
    problem = InputIR(
        bounding_region=CellBox(sx=12, sy=1, sz=4),
        machines=[_src("sa"), _load("ka", 544), _src("sb"), _load("kb", 544)],
        nets=[_tier_net("power:A", "sa", "ka"), _tier_net("power:B", "sb", "kb")],
    )
    placements = [at("sa", 0, 0, 0), at("ka", 2, 0, 0), at("sb", 0, 0, 2), at("kb", 2, 0, 2)]
    result = route_power(problem, placements)
    assert not result.ok
    assert result.failed_nets == ("power:A", "power:B")  # ALL failing nets, in problem order
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "amperage"  # the first one's specific reason
