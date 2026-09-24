"""Tests for ``router.steiner``: one net as a group Steiner tree whose docks are part of the search.

Each endpoint is a group of dock cells and the tree only has to touch one of each; which one is
decided by the search that lays the path. These pin the rules the negotiation leans on: the packed
grid orders like tuples, a cell several machines touch is preferred because it serves them all,
two connections of one machine never share a claim key, the cheapest start wins, and ``None`` means
geometry, never price.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Placement,
    Port,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import placement_index
from gtnh_solver.router import core, steiner
from gtnh_solver.router._grid import obstacle_cells
from gtnh_solver.router.steiner import Endpoint, Grid, route_tree
from tests._helpers import at, consumer, machine, net, producer


def _endpoints(
    problem: InputIR, placements: Sequence[Placement], net_id: str
) -> tuple[list[Endpoint], Grid]:
    """The endpoints of ``net_id`` exactly as the negotiation builds them, and the grid they use."""
    machines = {m.id: m for m in problem.machines}
    hard = obstacle_cells(problem, placements, machines)
    grid = Grid(problem.bounding_region, hard)
    (the_net,) = [n for n in problem.nets if n.id == net_id]
    eps = core._endpoints(
        the_net, placement_index(placements), machines, hard, {}, problem.bounding_region, grid
    )
    assert isinstance(eps, list), eps
    return eps, grid


def _cells(tree: steiner.Tree, grid: Grid) -> set[Cell]:
    return {grid.dec(c) for c in tree.cells}


def _docks(tree: steiner.Tree, grid: Grid) -> dict[int, Cell]:
    return {i: grid.dec(c) for i, (c, _) in tree.terminals.items()}


# ------------------------------------------------------------------------------------ the grid


def test_the_grid_packs_cells_in_tuple_order() -> None:
    # Every tie in the search breaks on the packed cell, so packing must order exactly as the
    # (x, y, z) tuples do, or the router would stop being deterministic in the same way the
    # validator and the old router are.
    grid = Grid(CellBox(sx=3, sy=2, sz=4), set())
    cells = [(x, y, z) for x in range(3) for y in range(2) for z in range(4)]
    assert sorted(cells, key=grid.enc) == sorted(cells)
    assert all(grid.dec(grid.enc(c)) == c for c in cells)


def test_the_grid_neighbours_stop_at_walls_and_the_region_edge() -> None:
    grid = Grid(CellBox(sx=3, sy=1, sz=3), {(1, 0, 0), (5, 5, 5)})  # a wall, and one outside
    assert grid.hard == {grid.enc((1, 0, 0))}  # a cell outside the region is dropped
    corner = {grid.dec(n) for n in grid.adj(grid.enc((0, 0, 0)))}
    assert corner == {(0, 0, 1)}  # (1,0,0) is a wall and the rest are off the region
    centre = {grid.dec(n) for n in grid.adj(grid.enc((1, 0, 1)))}
    assert centre == {(0, 0, 1), (2, 0, 1), (1, 0, 2)}
    assert grid.adj(grid.enc((1, 0, 1))) is grid.adj(grid.enc((1, 0, 1)))  # cached


# ---------------------------------------------------------------------------- tree shape


def test_a_cell_several_machines_touch_serves_them_all() -> None:
    # Three consumers around one free cell H, fed from a producer across the region. Each consumer
    # also has cells of its own nearer the producer, so a search ranking steps by plain cost would
    # take those one by one. Ranking by cost per endpoint served docks all three on H: one pipe
    # block wired to three neighbours, the manifold of the maintainer's build.
    #
    #   z=0   .  b  .
    #   z=1   c  H  d        H = (1,0,1)
    #   z=2   .  .  .
    #   z=3   .  a  .        a produces (its front faces south, off the region); b c d consume
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=4),
        machines=[
            machine(
                "a",
                [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
                orientation=Facing.SOUTH,
            ),
            consumer("b"),
            consumer("c"),
            consumer("d"),
        ],
        nets=[net("n", "a", "b", "c", "d")],
    )
    placements = [
        at("a", 1, 0, 3, orientation=Facing.SOUTH),
        at("b", 1, 0, 0),
        at("c", 0, 0, 1),
        at("d", 2, 0, 1),
    ]
    eps, grid = _endpoints(problem, placements, "n")
    tree = route_tree(eps, grid, {}, {})

    assert tree is not None
    docks = _docks(tree, grid)
    assert docks[1] == docks[2] == docks[3] == (1, 0, 1)
    assert _cells(tree, grid) == {(1, 0, 1), (1, 0, 2)}  # H and the one cell down to a


def test_every_endpoint_on_one_cell_is_one_block() -> None:
    # Two machines whose best dock is the one cell between them: that block is the whole pipe, a
    # tree with no legs. It used to get a stub beside it, a second block that led nowhere, only so
    # the route would have a segment.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=2, sz=1),
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    eps, grid = _endpoints(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)], "n")
    tree = route_tree(eps, grid, {}, {})

    assert tree is not None
    assert set(_docks(tree, grid).values()) == {(1, 0, 0)}
    assert _cells(tree, grid) == {(1, 0, 0)}
    assert tree.legs == []


def test_a_price_moves_the_dock_not_just_the_path() -> None:
    # The same pair with a second way round: priced high enough, the shared cell is left and both
    # machines dock elsewhere. Docks are chosen by the search, so a price reaches them.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=2, sz=1),
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    eps, grid = _endpoints(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)], "n")
    tree = route_tree(eps, grid, {grid.enc((1, 0, 0)): 50.0}, {})

    assert tree is not None
    assert (1, 0, 0) not in _cells(tree, grid)
    assert set(_docks(tree, grid).values()) == {(0, 1, 0), (2, 1, 0)}


def test_two_connections_of_one_machine_take_two_cells() -> None:
    # A machine on one net twice (two ports) must dock each on a cell of its own: one face of a
    # single block does one thing. Asked both as the net's first endpoint (where the first leg runs
    # from every dock cell at once) and as a later one.
    both = machine(
        "m",
        [
            Port(id="in1", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            Port(id="in2", commodity=Commodity.ITEM, direction=IODirection.INPUT),
        ],
    )
    for order in (["m", "m", "a"], ["a", "m", "m"]):
        ports = {"m": iter(["in1", "in2"]), "a": iter(["out"])}
        the_net = Net(
            id="n",
            commodity=Commodity.ITEM,
            fluid_or_item="x",
            throughput=1.0,
            endpoints=[MachineFaceRef(machine_id=mid, port_id=next(ports[mid])) for mid in order],
        )
        problem = InputIR(
            bounding_region=CellBox(sx=5, sy=1, sz=3),
            machines=[both, producer("a")],
            nets=[the_net],
        )
        eps, grid = _endpoints(problem, [at("m", 2, 0, 1), at("a", 0, 0, 1)], "n")
        tree = route_tree(eps, grid, {}, {})

        assert tree is not None, order
        on_m = [grid.dec(c) for i, (c, _) in tree.terminals.items() if eps[i].machine_id == "m"]
        assert len(on_m) == 2, (order, on_m)
        assert len(set(on_m)) == 2, (order, on_m)


def _hub_problem() -> tuple[InputIR, list[Placement]]:
    """A net of three, big enough that the tree tries single starts as well as the joint one."""
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=3, sz=7),
        machines=[producer("a"), consumer("b"), consumer("c")],
        nets=[net("n", "a", "b", "c")],
    )
    # a mid-region, so five of its faces are free: more starts than a later round tries.
    return problem, [at("a", 3, 1, 3), at("b", 6, 1, 6), at("c", 0, 1, 6)]


def test_a_later_round_tries_fewer_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no earlier tree, a net of three tries the joint start plus up to STARTS single ones;
    # given the start its last tree grew from, it tries that, LATE_STARTS cheapest, and the joint.
    problem, placements = _hub_problem()
    eps, grid = _endpoints(problem, placements, "n")
    grown: list[int] = []
    real = steiner._grow

    def spy(*args: object, **kwargs: object) -> steiner.Tree | None:
        grown.append(len(args[1]))  # type: ignore[arg-type]  # the seeds mapping
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(steiner, "_grow", spy)
    monkeypatch.setattr(steiner, "STARTS", 99)  # no bound-skipping below can hide the count
    first = route_tree(eps, grid, {}, {})
    assert first is not None
    fresh = len(grown)
    grown.clear()
    again = route_tree(eps, grid, {}, {}, prefer=first.terminals[0][0])
    assert again is not None
    assert len(grown) < fresh
    assert len(grown) <= 1 + steiner.LATE_STARTS + 1


def test_a_start_that_cannot_win_is_not_grown(monkeypatch: pytest.MonkeyPatch) -> None:
    # A single start whose distance to the farthest endpoint already costs more than the best tree
    # found cannot win, so it is skipped rather than grown. Here b and c sit together far east of
    # a, so a's west and south dock cells are at least two steps worse than its east one.
    problem = InputIR(
        bounding_region=CellBox(sx=10, sy=1, sz=3),
        machines=[producer("a"), consumer("b"), consumer("c")],
        nets=[net("n", "a", "b", "c")],
    )
    placements = [at("a", 2, 0, 1), at("b", 7, 0, 1), at("c", 8, 0, 1)]
    eps, grid = _endpoints(problem, placements, "n")
    assert len(eps[0].cands) == 3
    grown: list[int] = []
    real = steiner._grow

    def spy(*args: object, **kwargs: object) -> steiner.Tree | None:
        grown.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(steiner, "_grow", spy)
    tree = route_tree(eps, grid, {}, {})
    assert tree is not None
    assert len(grown) < 1 + len(eps[0].cands)  # the joint start, and not every single one


# ------------------------------------------------------------------------ no tree at all


def test_no_tree_when_an_endpoint_is_walled_off() -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3),
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
        reserved_cells=[CellCoord(x=1, y=0, z=z) for z in range(3)],
    )
    eps, grid = _endpoints(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)], "n")
    assert route_tree(eps, grid, {}, {}) is None
    # Prices never block: however dear a cell is, a net with a way through still gets a tree.
    open_problem = problem.model_copy(update={"reserved_cells": []})
    eps, grid = _endpoints(open_problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)], "n")
    priced = {grid.enc((x, 0, z)): 1e6 for x in range(3) for z in range(3)}
    assert route_tree(eps, grid, priced, {}) is not None


def test_no_tree_when_every_dock_of_the_first_endpoint_is_blocked() -> None:
    # The negotiation's congestion proof walls one contested cell and asks for a tree. When that
    # cell is the first endpoint's only dock, there is nothing to start from: None, not a crash.
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=2),
        machines=[
            machine(
                "a",
                [Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)],
                orientation=Facing.SOUTH,
            ),
            consumer("b"),
            consumer("c"),
        ],
        nets=[net("n", "a", "b", "c")],
        reserved_cells=[CellCoord(x=1, y=0, z=0)],
    )
    placements = [at("a", 0, 0, 0, orientation=Facing.SOUTH), at("b", 2, 0, 1), at("c", 2, 0, 0)]
    eps, grid = _endpoints(problem.model_copy(update={"reserved_cells": []}), placements, "n")
    assert [grid.dec(c) for c, _ in eps[0].cands] == [(1, 0, 0)]
    blocked = frozenset({grid.enc((1, 0, 0))})
    assert route_tree(eps, grid, {}, {}, blocked=blocked) is None


def test_no_tree_for_a_net_with_one_endpoint() -> None:
    lone = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[MachineFaceRef(machine_id="a", port_id="out")],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=3, sy=1, sz=3), machines=[producer("a")], nets=[lone]
    )
    eps, grid = _endpoints(problem, [at("a", 1, 0, 1)], "n")
    assert route_tree(eps, grid, {}, {}) is None


# ------------------------------------------------------------------------- multiblock casing


def _two_slot_block(mid: str) -> Machine:
    """A 3x1x1 multiblock whose two end cells each take any hatch; the middle takes none."""
    slots = tuple(
        HatchSlot(offset=CellCoord(x=x, y=0, z=0), kinds=("InputBus", "OutputBus", "Energy"))
        for x in (0, 2)
    )
    return Machine(
        id=mid,
        type="t",
        footprint=CellBox(sx=3, sy=1, sz=1),
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
        ),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        hatch_cells=2,
        hatch_slots=slots,
    )


def test_a_casing_price_moves_a_multiblock_dock() -> None:
    # A multiblock contends over casing cells, so the price of a casing key (another net's hatch
    # wants it) is paid by any terminal on it, from whichever face. Priced up, the west end's key
    # sends the dock to the east end even though the west end is nearer.
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=1, sz=3),
        machines=[producer("a"), _two_slot_block("m")],
        nets=[net("n", "a", "m")],
    )
    placements = [at("a", 0, 0, 1), at("m", 2, 0, 1)]
    eps, grid = _endpoints(problem, placements, "n")
    assert eps[1].multiblock

    cheap = route_tree(eps, grid, {}, {})
    assert cheap is not None
    assert eps[1].key_of[cheap.terminals[1][0]] == (2, 0, 1)  # the west end, nearest a

    dear = route_tree(eps, grid, {}, {("m", (2, 0, 1)): 100.0})
    assert dear is not None
    assert eps[1].key_of[dear.terminals[1][0]] == (4, 0, 1)  # the east end instead
    assert dear.keys(eps) == {("m", (4, 0, 1))}


# --------------------------------------------------------------------------------- power trunks


def _pair_sharing_one_cell(region: CellBox) -> tuple[list[Endpoint], Grid]:
    """A source and a sink whose cheapest docks are the one cell between them."""
    problem = InputIR(
        bounding_region=region,
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    return _endpoints(problem, [at("a", 0, 0, 0), at("b", 2, 0, 0)], "n")


def test_a_trunk_lays_its_last_endpoint_a_leg_where_a_pipe_is_one_block() -> None:
    # ``router.power`` never lets its last sink tap a trunk with no segment: it lays that sink a
    # leg to a dock cell of its own, since a cable's gauge lives on its segments. A power net's tree
    # reserves that shape, not a pipe's single block.
    eps, grid = _pair_sharing_one_cell(CellBox(sx=3, sy=2, sz=1))
    pipe = route_tree(eps, grid, {}, {})
    trunk = route_tree(eps, grid, {}, {}, trunk=True)

    assert pipe is not None
    assert trunk is not None
    assert set(_docks(pipe, grid).values()) == {(1, 0, 0)}  # both on the shared cell, and only it
    assert _cells(pipe, grid) == {(1, 0, 0)}
    docks = _docks(trunk, grid)
    assert docks[0] != docks[1]  # the sink docks on a cell of its own, at the end of a real leg
    assert grid.dec(trunk.legs[-1][-1]) == docks[1]


def test_no_trunk_when_the_last_endpoint_has_no_other_cell() -> None:
    # The same pair in a one-cell-high corridor: the sink's only other dock is off the region, so
    # there is no trunk router.power could lay, and the tree says so. A pipe needs nothing more
    # than the shared cell, so the same corridor is a one-block pipe.
    eps, grid = _pair_sharing_one_cell(CellBox(sx=3, sy=1, sz=1))
    assert route_tree(eps, grid, {}, {}, trunk=True) is None
    pipe = route_tree(eps, grid, {}, {})
    assert pipe is not None
    assert _cells(pipe, grid) == {(1, 0, 0)}


# ------------------------------------------------------------------------------------- the search


def test_a_dear_seed_reached_cheaply_from_another_is_not_expanded_twice() -> None:
    # Seeds carry their own entry costs (a priced dock cell of the first endpoint). A dear seed next
    # to a cheap one is reached more cheaply through it; its own heap entry is then stale, and the
    # search must skip it rather than expand the cell again at the worse cost.
    grid = Grid(CellBox(sx=9, sy=1, sz=1), set())
    cheap, dear, goal = grid.enc((0, 0, 0)), grid.enc((1, 0, 0)), grid.enc((8, 0, 0))
    found = steiner._search(
        {cheap: 0.0, dear: 5.0}, {goal: [(0.0, 0)]}, grid, {}, ["m"], frozenset()
    )

    assert found is not None
    path, served = found
    assert served == [0]
    assert path[0] == cheap  # the path runs through the dear seed, entered from the cheap one
    assert [grid.dec(c) for c in path] == [(x, 0, 0) for x in range(9)]


def test_a_farther_cell_wins_when_it_serves_enough_machines() -> None:
    # The search runs past a goal it has already beaten on cost per endpoint, because a cell that
    # serves more machines can still win from farther out: one machine at 2 cells is the first
    # candidate, a pair at 5 cells is popped and loses, and four machines at 8 cells take it.
    grid = Grid(CellBox(sx=9, sy=1, sz=1), set())
    seed = grid.enc((0, 0, 0))
    goals = {
        grid.enc((2, 0, 0)): [(0.0, 0)],
        grid.enc((5, 0, 0)): [(0.0, 1), (0.0, 2)],
        grid.enc((8, 0, 0)): [(0.0, 3), (0.0, 4), (0.0, 5), (0.0, 6)],
    }
    machine_of = [f"m{i}" for i in range(7)]
    found = steiner._search({seed: 0.0}, goals, grid, {}, machine_of, frozenset())

    assert found is not None
    path, served = found
    assert served == [3, 4, 5, 6]
    assert grid.dec(path[-1]) == (8, 0, 0)
