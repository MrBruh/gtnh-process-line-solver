"""Tests for the negotiation in ``router.core``: docks and paths together, power included.

The router re-routes every net as a tree each round (``router.steiner``), prices the cells and
casing keys two nets hold, and stops once nothing is shared, or once it has proven the overlap is
geometry. These pin what that buys and what it must never do: power and pipes are negotiated
together, so a pipe cannot take the cell an energy hatch needs; only nets in a conflict are ripped
up; history makes a cell that stays contested dearer; two nets never share a casing cell; and no
stopping rule ever turns into an invalid layout.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
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
from gtnh_solver.router import core, route, steiner
from gtnh_solver.router._grid import claim_key, obstacle_cells
from gtnh_solver.solver.core import _assemble
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import at, consumer, machine, net, power_source, producer, property_examples

_MALFORMED = {
    ViolationCode.ROUTE_CELL_COLLISION,
    ViolationCode.ROUTE_OUT_OF_BOUNDS,
    ViolationCode.ROUTE_DISCONTINUOUS,
    ViolationCode.ROUTE_THROUGH_MACHINE,
    ViolationCode.ROUTE_ON_RESERVED,
    ViolationCode.MISSING_TERMINAL,
    ViolationCode.DUPLICATE_TERMINAL,
    ViolationCode.TERMINAL_ON_FRONT_FACE,
    ViolationCode.TERMINAL_NOT_ADJACENT,
    ViolationCode.TERMINAL_NOT_ON_ROUTE,
    ViolationCode.TERMINAL_FACE_CONTENTION,
}


# ------------------------------------------------------------------------- power and pipes


def _hatch_and_pipe() -> tuple[InputIR, list[Placement]]:
    """A machine with two free faces, wanted by a pipe and by a cable.

    ::

        x:    0  1  2  3
        z=0   .  Y  M  X        M's front is north (off the region); (2,0,1) is walled
        z=1   S  .  #  .        S is the power source (front west, off the region)
        z=2   .  P  .  .        P feeds items into M

    P's body splits the free cells in two: the west half reaches M only through Y, the east half
    only through X. Laid alone, P's pipe docks M on Y (two cells) and so does the source's cable.
    Only one of them can have it: the pipe has a way round to X, and the cable, whose source is on
    the west side, does not. The old router held one cell per energy port up front and hoped; this
    one negotiates it.
    """
    m = machine(
        "m",
        [
            Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT),
        ],
    ).model_copy(update={"eut": 8.0})
    p = machine(
        "p",
        [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
        orientation=Facing.SOUTH,
    )
    power = Net(
        id="power:LV",
        commodity=Commodity.POWER,
        throughput=8.0,
        endpoints=[
            MachineFaceRef(machine_id="src", port_id="power:out"),
            MachineFaceRef(machine_id="m", port_id="power:in"),
        ],
    )
    items = Net(
        id="items",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="p", port_id="out"),
            MachineFaceRef(machine_id="m", port_id="in"),
        ],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=1, sz=3),
        machines=[power_source(orientations=[Facing.WEST]), m, p],
        nets=[items, power],
        reserved_cells=[CellCoord(x=2, y=0, z=1)],
    )
    placements = [
        at("src", 0, 0, 1, orientation=Facing.WEST),
        at("m", 2, 0, 0),
        at("p", 1, 0, 2, orientation=Facing.SOUTH),
    ]
    return problem, placements


def test_a_pipe_leaves_the_cell_an_energy_hatch_needs() -> None:
    problem, placements = _hatch_and_pipe()
    routed = route(problem, placements)

    assert routed.ok, routed.infeasibility
    (pipe,) = routed.routes
    m_dock = next(t.cell.as_tuple() for t in pipe.terminals if t.machine_id == "m")
    assert m_dock == (3, 0, 0)  # X: the long way round, leaving Y to the cable

    layout, failed = _assemble(problem, tuple(placements), 0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    cable = next(r for r in layout.routes if r.commodity is Commodity.POWER)
    assert (1, 0, 0) in cable.cells()


def test_with_power_on_me_the_pipe_takes_the_short_way() -> None:
    # The control: nothing else wants Y, so the pipe keeps its two-cell route.
    problem, placements = _hatch_and_pipe()
    items_only = problem.model_copy(
        update={"me_toggles": problem.me_toggles.model_copy(update={"power": True})}
    )
    (pipe,) = route(items_only, placements).routes
    assert pipe.cells() == {(1, 0, 1), (1, 0, 0)}


# ------------------------------------------------------------------------------ the rounds


def test_a_net_in_no_conflict_is_routed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rip-up is per conflict: after round 1 only the nets on an over-used cell re-route, and the
    # rest keep their trees (and keep them priced for everyone else).
    problem, placements = _hatch_and_pipe()
    far = producer("q")
    sink = consumer("r")
    problem = problem.model_copy(
        update={
            "bounding_region": CellBox(sx=8, sy=1, sz=3),
            "machines": [*problem.machines, far, sink],
            "nets": [*problem.nets, net("bystander", "q", "r")],
        }
    )
    placements = [*placements, at("q", 6, 0, 0), at("r", 6, 0, 2)]
    calls: dict[str, int] = {}
    real = steiner.route_tree

    def counting(eps: list[steiner.Endpoint], *args: object, **kwargs: object) -> object:
        machines = {e.machine_id for e in eps}
        name = "bystander" if "q" in machines else "items" if "p" in machines else "power"
        calls[name] = calls.get(name, 0) + 1
        return real(eps, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(core, "route_tree", counting)
    routed = route(problem, placements)

    assert routed.ok, routed.infeasibility
    assert calls["bystander"] == 1
    assert calls["items"] > 1
    assert calls["power"] > 1


def test_prices_multiply_history_by_present_sharing() -> None:
    history = {1: 2.0, 2: 1.0}
    usage = {2: 1, 3: 2}
    prices = core._prices(history, usage, 0.5)
    # A cell with history but no other user pays its history; one in use pays history-scaled
    # sharing on top; a fresh contested cell pays sharing alone.
    assert prices == {1: 2.0, 2: (1 + 1.0) * (1 + 0.5 * 1) - 1, 3: (1 + 0.0) * (1 + 0.5 * 2) - 1}
    assert core._prices(history, usage, 0.0) == {1: 2.0, 2: 1.0, 3: 0.0}  # round 1: history only


def test_counting_a_resource_down_to_zero_forgets_it() -> None:
    counter = {7: 1}
    core._count(counter, [7, 8], 1)
    assert counter == {7: 2, 8: 1}
    core._count(counter, [7, 8], -1)
    assert counter == {7: 1}


def test_parallel_openings_exhaust_the_rounds_and_salvage(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three nets and two gaps in a wall: any one net can take either gap, so no gap is ever forced
    # on two nets and the congestion proof never fires, yet three cannot fit through two. The
    # rounds run out, a collision-free subset is kept in problem order, and the rest fail
    # explicitly as congestion rather than as an overlapping layout.
    monkeypatch.setattr(core, "_MAX_ROUNDS", 12)
    wall = [CellCoord(x=x, y=0, z=2) for x in range(7) if x not in (2, 4)]
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=5),
        machines=[
            producer("a"),
            consumer("b"),
            producer("c"),
            consumer("d"),
            producer("e"),
            consumer("f"),
        ],
        nets=[net("n1", "a", "b"), net("n2", "c", "d"), net("n3", "e", "f")],
        reserved_cells=wall,
    )
    placements = [
        at("a", 1, 0, 0),
        at("b", 1, 0, 4),
        at("c", 3, 0, 0),
        at("d", 3, 0, 4),
        at("e", 5, 0, 0),
        at("f", 5, 0, 4),
    ]
    result = route(problem, placements)

    assert not result.ok
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "congestion"
    assert len(result.failed_nets) == 1
    assert len(result.routes) == 2
    first, second = (r.cells() for r in result.routes)
    assert first.isdisjoint(second)


def test_a_walled_off_net_fails_without_holding_anything() -> None:
    # A net with no tree at all fails as routing in round 1 and leaves the others its space.
    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=1, sz=3),
        machines=[producer("a"), consumer("b"), producer("c"), consumer("d")],
        nets=[net("walled", "a", "b"), net("open", "c", "d")],
        reserved_cells=[CellCoord(x=1, y=0, z=z) for z in range(3)],
    )
    placements = [at("a", 0, 0, 1), at("b", 2, 0, 1), at("c", 3, 0, 0), at("d", 3, 0, 2)]
    result = route(problem, placements)

    assert result.failed_nets == ("walled",)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "routing"
    assert [r.net_id for r in result.routes] == ["open"]


# ---------------------------------------------------------------------------- casing keys


def _two_ended(mid: str, ports: list[Port]) -> Machine:
    """A 3x1x1 multiblock whose two end cells each take any hatch."""
    slots = tuple(
        HatchSlot(offset=CellCoord(x=x, y=0, z=0), kinds=("InputBus", "OutputBus", "Energy"))
        for x in (0, 2)
    )
    return Machine(
        id=mid,
        type="t",
        footprint=CellBox(sx=3, sy=1, sz=1),
        faces=FaceSpec(ports=ports),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        hatch_cells=2,
        hatch_slots=slots,
    )


def test_two_nets_never_share_a_casing_cell() -> None:
    # A multiblock with two hatch cells and two item connections, both nearest the west end. Two
    # hatches cannot be one block even facing two ways, so the negotiation prices the casing cell
    # itself: one connection moves to the east end.
    #
    #   x:   0  1  2  3  4  5  6
    #   z=1  a  .  [ m  m  m ]  .      m's front is north; its hatch cells are its two ends
    #   z=2  .  .  .  .  .  .  .
    #   z=3  c  .  .  .  .  .  .       a feeds m's first input, c its second
    m = _two_ended(
        "m",
        [
            Port(id="in1", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="in2", commodity=Commodity.ITEM, direction=IODirection.INPUT),
        ],
    )
    one = Net(
        id="one",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="out"),
            MachineFaceRef(machine_id="m", port_id="in1"),
        ],
    )
    two = Net(
        id="two",
        commodity=Commodity.ITEM,
        fluid_or_item="y",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="c", port_id="out"),
            MachineFaceRef(machine_id="m", port_id="in2"),
        ],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=4),
        machines=[producer("a"), producer("c"), m],
        nets=[one, two],
    )
    placements = [at("a", 0, 0, 1), at("c", 0, 0, 3), at("m", 2, 0, 1)]
    result = route(problem, placements)

    assert result.ok, result.infeasibility
    machines = {mm.id: mm for mm in problem.machines}
    keys = [claim_key(t, m) for r in result.routes for t in r.terminals if t.machine_id == "m"]
    assert sorted(keys) == [(2, 0, 1), (4, 0, 1)]
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(result.routes)
    )
    report = validate(problem, layout)
    assert _MALFORMED.isdisjoint(report.codes()), str(report)
    assert machines["m"].hatch_slots  # the premise: a multiblock, which contends over casing cells


# ------------------------------------------------------------------ never silently invalid


@st.composite
def _grid_problems(draw: st.DrawFn) -> tuple[InputIR, list[Placement]]:
    """Two to four point-to-point item nets on a small single-layer grid with random walls."""
    sx = draw(st.integers(min_value=4, max_value=7))
    sz = draw(st.integers(min_value=4, max_value=7))
    cells = [(x, z) for x in range(sx) for z in range(sz)]
    count = draw(st.integers(min_value=2, max_value=4))
    spots = draw(
        st.lists(st.sampled_from(cells), min_size=2 * count, max_size=2 * count, unique=True)
    )
    rest = [c for c in cells if c not in spots]
    walls = (
        draw(st.lists(st.sampled_from(rest), max_size=len(rest) // 3, unique=True)) if rest else []
    )
    fronts = draw(
        st.lists(
            st.sampled_from([Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST]),
            min_size=2 * count,
            max_size=2 * count,
        )
    )
    machines: list[Machine] = []
    nets: list[Net] = []
    placements: list[Placement] = []
    for i in range(count):
        src, dst = f"s{i}", f"d{i}"
        for mid, direction, port, (x, z), front in (
            (src, IODirection.OUTPUT, "out", spots[2 * i], fronts[2 * i]),
            (dst, IODirection.INPUT, "in", spots[2 * i + 1], fronts[2 * i + 1]),
        ):
            machines.append(
                machine(
                    mid,
                    [Port(id=port, commodity=Commodity.ITEM, direction=direction)],
                    orientation=front,
                )
            )
            placements.append(at(mid, x, 0, z, orientation=front))
        nets.append(net(f"n{i}", src, dst, fluid=f"f{i}"))
    problem = InputIR(
        bounding_region=CellBox(sx=sx, sy=1, sz=sz),
        machines=machines,
        nets=nets,
        reserved_cells=[CellCoord(x=x, y=0, z=z) for x, z in walls],
    )
    return problem, placements


def _check_never_invalid(problem: InputIR, placements: list[Placement]) -> None:
    result = route(problem, placements)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        routes=list(result.routes),
        auto_connections=list(result.auto_connections),
    )
    report = validate(problem, layout)
    assert _MALFORMED.isdisjoint(report.codes()), str(report)
    if result.ok:
        assert {r.net_id for r in result.routes} | {a.net_id for a in result.auto_connections} == {
            n.id for n in problem.nets
        }
    else:
        assert result.infeasibility is not None
        assert result.failed_nets
        assert not {r.net_id for r in result.routes} & set(result.failed_nets)


@settings(max_examples=property_examples(120), deadline=None)
@given(_grid_problems())
def test_the_negotiation_never_emits_an_invalid_route(
    case: tuple[InputIR, list[Placement]],
) -> None:
    # Whatever the geometry, every route it emits is well formed and collision-free, and a net it
    # could not route is reported, never half-laid.
    _check_never_invalid(*case)


@settings(max_examples=property_examples(60), deadline=None)
@given(_grid_problems())
def test_the_early_stop_never_emits_an_invalid_route(case: tuple[InputIR, list[Placement]]) -> None:
    # The same, with the congestion proof asked after every round instead of after a stall: the
    # early stop may only ever end in the salvage, which keeps a collision-free subset.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(core, "_STALL_ROUNDS", 0)
        _check_never_invalid(*case)


# ------------------------------------------------------------------------- the congestion proof


def _key_contest(
    *, escapable: bool
) -> tuple[list[Net], dict[str, steiner.Tree], dict[str, list[steiner.Endpoint]], steiner.Grid]:
    """Two nets whose trees hold one casing key of ``m``, plus a bystander net elsewhere.

    With ``escapable`` the multiblock has a second hatch cell either net could move to; without,
    its one hatch cell is the only place either can dock, so both are forced onto it.
    """
    ends = (0, 2) if escapable else (0,)
    slots = tuple(
        HatchSlot(offset=CellCoord(x=x, y=0, z=0), kinds=("InputBus", "OutputBus", "Energy"))
        for x in ends
    )
    m = Machine(
        id="m",
        type="t",
        footprint=CellBox(sx=3, sy=1, sz=1),
        faces=FaceSpec(
            ports=[
                Port(id="in1", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                Port(id="in2", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            ]
        ),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        hatch_cells=len(ends),
        hatch_slots=slots,
    )

    def feed(nid: str, src: str, port: str) -> Net:
        return Net(
            id=nid,
            commodity=Commodity.ITEM,
            fluid_or_item=nid,
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id=src, port_id="out"),
                MachineFaceRef(machine_id="m", port_id=port),
            ],
        )

    nets = [feed("one", "a", "in1"), feed("two", "c", "in2"), net("bystander", "q", "r")]
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=1, sz=5),
        machines=[producer("a"), producer("c"), m, producer("q"), consumer("r")],
        nets=nets,
    )
    placements = [
        at("a", 0, 0, 1),
        at("c", 0, 0, 3),
        at("m", 2, 0, 1),
        at("q", 7, 0, 0),
        at("r", 7, 0, 4),
    ]
    machines = {mm.id: mm for mm in problem.machines}
    hard = obstacle_cells(problem, placements, machines)
    grid = steiner.Grid(problem.bounding_region, hard)
    index = {p.machine_id: p for p in placements}
    eps_by_net: dict[str, list[steiner.Endpoint]] = {}
    trees: dict[str, steiner.Tree] = {}
    west = ("m", (2, 0, 1))
    for n in nets:
        eps = core._endpoints(n, index, machines, hard, {}, problem.bounding_region, grid)
        assert isinstance(eps, list)
        eps_by_net[n.id] = eps
        # The contesting nets are routed with every other casing cell priced out, so both
        # trees hold the west end's key; the bystander is routed as it likes.
        dear = {("m", (4, 0, 1)): 1e6}
        tree = steiner.route_tree(eps, grid, {}, dear)
        assert tree is not None
        trees[n.id] = tree
    assert west in trees["one"].keys(eps_by_net["one"])
    assert west in trees["two"].keys(eps_by_net["two"])
    assert not trees["bystander"].keys(eps_by_net["bystander"])
    return nets, trees, eps_by_net, grid


def test_the_proof_certifies_a_casing_cell_both_nets_need() -> None:
    nets, trees, eps_by_net, grid = _key_contest(escapable=False)
    assert core._congestion_is_irreducible([], [("m", (2, 0, 1))], nets, trees, eps_by_net, grid)


def test_the_proof_declines_a_casing_cell_a_net_could_leave() -> None:
    # Either net could take the east end instead, so another round could still price one off:
    # the proof must decline, whatever the bystander (which holds no key) does.
    nets, trees, eps_by_net, grid = _key_contest(escapable=True)
    assert not core._congestion_is_irreducible(
        [], [("m", (2, 0, 1))], nets, trees, eps_by_net, grid
    )
