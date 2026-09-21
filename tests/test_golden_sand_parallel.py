"""The maintainer's parallel-sand build, decoded from the file our exporter wrote and he built.

``tests/golden/schematic/sand-parallel-exported.schematic`` is the one golden whose wiring and
facings are real (the Schematica copy's are not; see that directory's README). It was built in game
and runs on 3 cable blocks and 12 pipe blocks, where the solver lays 23 and 40. Its pipes do what
the router could not before #164: several terminals of ONE net on one pipe block, 20 item
terminals on 12 cells. This module turns the file back into a ``LayoutResult`` and pins three
things about it::

    .schematic --read_schematic--> machines (mID, mFacing)   pipes/cables (mID, mConnections)
                                        |                              |
                                        v                              v
                                   Placements           a run per connected component; a
                                                        Terminal per wired side facing a machine
                                        \\                             /
                                         '---> LayoutResult <--------'
                                                   |
                         validate() accepts it  <--'--> every run belongs to exactly one net

**What is chosen rather than read.** An exported GT machine records only its ``mID`` and facing,
so which recipe each Forge Hammer runs is not in the file. The build's author put stage 1 on the
bottom layer and stage 3 on the top (the Schematica copy shows stone in the bottom chest and sand
in the top hammers' output slots), so a hammer's stage is its ``y``. Which net each run carries is
then **not** assumed: it is looked up as the one net whose endpoints are exactly the machines the
run is wired to, and the decode fails if there is none.

The pipe gauge is deliberately left off (``material=None``). The build used plain tin pipe and runs
at a third of its designed rate for it; judging that is #165 and #190, not this module, which is
about geometry and wiring.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    METoggles,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell
from gtnh_solver.placement import crowded_machines
from gtnh_solver.router import route
from gtnh_solver.schematic import read_schematic
from gtnh_solver.schematic.core import FORGE_DIRECTION
from gtnh_solver.schematic.read import TileEntity
from gtnh_solver.validator import validate

_ROOT = Path(__file__).resolve().parents[1]
_PLAN = _ROOT / "examples" / "gtnh-parallel-sand.json"
_BUILD = _ROOT / "tests" / "golden" / "schematic" / "sand-parallel-exported.schematic"

#: The GT ``mID`` of every block in the build (the texture manifest names them; see the README).
_HAMMER, _CHEST, _SOURCE, _PIPE = 611, 135, 15498, 5591
#: Tin cable ids and their gauge in amps: ``cable.tin.02`` and ``cable.tin.04``.
_CABLE_GAUGE = {1247: 2, 1248: 4}

#: Stage by layer, as the build's author assigned them (module docstring): plan node id per ``y``.
_HAMMER_NODE = {
    0: "node-520d2e94-3cf4-491e-9b7e-5d45111be789",  # stone -> cobblestone
    1: "node-e53ac8ed-affa-4267-aff9-d92443c747bf",  # cobblestone -> gravel
    2: "node-1979ee2a-883a-408b-8903-a4316ed9f84b",  # gravel -> sand
}
_CHEST_ID = {
    0: "storage-e7a7cfc2-8d7d-467e-9a76-7a3c9556d4ab",  # the stone the line is fed
    2: "storage-1be6e593-bcf4-4018-b5f3-2952b23c3bd7",  # the sand it collects
}

_FACING_OF = {ordinal: face for face, ordinal in FORGE_DIRECTION.items()}


def _machine_id(tile: TileEntity) -> str | None:
    _, y, z = tile.pos
    if tile.mid == _HAMMER:
        return f"{_HAMMER_NODE[y]}#{z + 1}"  # the three hammers of a stage are interchangeable
    if tile.mid == _CHEST:
        return _CHEST_ID[y]
    if tile.mid == _SOURCE:
        return "power-source:LV"
    return None


def _runs(
    tiles: dict[Cell, TileEntity], mids: Collection[int], machine_at: dict[Cell, str]
) -> list[tuple[list[tuple[Cell, Cell]], list[tuple[Cell, str, Facing]]]]:
    """Each connected run of ``mids`` blocks as ``(links, docks)``, read off ``mConnections``.

    A link joins two wired blocks of the run; a dock is ``(cell, machine, face)`` for a side wired
    into a machine, ``face`` being the machine's own face that the block sits against.
    """
    cells = {pos for pos, tile in tiles.items() if tile.mid in mids}
    links: set[tuple[Cell, Cell]] = set()
    docks: list[tuple[Cell, str, Facing]] = []
    for cell in sorted(cells):
        mask = tiles[cell].connections or 0
        for face, ordinal in FORGE_DIRECTION.items():
            if not mask & (1 << ordinal):
                continue
            dx, dy, dz = FACE_DELTAS[face]
            there = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
            if there in cells:
                links.add((min(cell, there), max(cell, there)))
            elif there in machine_at:
                docks.append((cell, machine_at[there], OPPOSITE_FACE[face]))
    runs = []
    unseen = set(cells)
    while unseen:
        run: set[Cell] = set()
        frontier = [min(unseen)]
        while frontier:
            cell = frontier.pop()
            if cell in run:
                continue
            run.add(cell)
            frontier += [b if a == cell else a for a, b in links if cell in (a, b)]
        unseen -= run
        runs.append((sorted(k for k in links if k[0] in run), [d for d in docks if d[0] in run]))
    return runs


def _coord(cell: Cell) -> CellCoord:
    return CellCoord(x=cell[0], y=cell[1], z=cell[2])


def _proven_build() -> tuple[InputIR, LayoutResult]:
    """The exported build as ``(problem, layout)``, the region shrunk to the build's own box."""
    build = read_schematic(_BUILD)
    problem = adapt_file(_PLAN)
    # The build is its own region: that is what puts the power source's front (its feed face)
    # flush on the boundary, as the validator requires.
    problem = problem.model_copy(
        update={"bounding_region": CellBox(sx=build.width, sy=build.height, sz=build.length)}
    )
    tiles = {tile.pos: tile for tile in build.tile_entities}
    gauge = {pos: _CABLE_GAUGE[t.mid] for pos, t in tiles.items() if t.mid in _CABLE_GAUGE}
    machine_at: dict[Cell, str] = {}
    placements = []
    for pos, tile in sorted(tiles.items()):
        mid = _machine_id(tile)
        if mid is None or tile.facing is None:
            continue
        machine_at[pos] = mid
        placements.append(
            Placement(machine_id=mid, cell=_coord(pos), orientation=_FACING_OF[tile.facing])
        )

    net_by_machines = {frozenset(e.machine_id for e in n.endpoints): n for n in problem.nets}
    routes = []
    for mids in ({_PIPE}, set(_CABLE_GAUGE)):
        for links, docks in _runs(tiles, mids, machine_at):
            net = net_by_machines[frozenset(machine for _, machine, _ in docks)]
            port = {e.machine_id: e.port_id for e in net.endpoints}
            routes.append(
                Route(
                    net_id=net.id,
                    commodity=net.commodity,
                    terminals=[
                        Terminal(machine_id=m, port_id=port[m], face=face, cell=_coord(cell))
                        for cell, m, face in docks
                    ],
                    segments=[Segment(start=_coord(a), end=_coord(b), channel=0) for a, b in links],
                    # A link is as thick as the thinner of the two blocks it joins.
                    thickness_per_segment=(
                        [min(gauge[a], gauge[b]) for a, b in links]
                        if net.commodity is Commodity.POWER
                        else None
                    ),
                )
            )
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=routes)
    return problem, layout


@pytest.fixture
def proven_build() -> tuple[InputIR, LayoutResult]:
    return _proven_build()


def test_the_build_proven_in_game_passes_the_validator(
    proven_build: tuple[InputIR, LayoutResult],
) -> None:
    # The strongest guard on the validator this repo has: a layout known to build and run (for
    # geometry, wiring and power) must never be rejected. 8 of its 12 pipe blocks and all 3 of its
    # cables carry terminals of several machines, so a rule that forbade that would fail here.
    problem, layout = proven_build
    report = validate(problem, layout)
    assert report.ok, str(report)


def test_the_build_shares_pipe_blocks_within_a_net_and_never_across_nets(
    proven_build: tuple[InputIR, LayoutResult],
) -> None:
    # The evidence behind keeping cross-net sharing forbidden (#164): the 12 pipes are four
    # disjoint runs, one per item net, and a block with several terminals on it serves several
    # machines of that one net. Nothing in the build needs two nets on one block.
    problem, layout = proven_build
    items = [r for r in layout.routes if r.commodity is Commodity.ITEM]
    assert len(items) == len([n for n in problem.nets if n.commodity is Commodity.ITEM]) == 4
    owners: dict[Cell, set[str]] = {}
    for r in layout.routes:
        for cell in r.cells():
            owners.setdefault(cell, set()).add(r.net_id)
    assert all(len(nets) == 1 for nets in owners.values())

    item_cells = set().union(*(r.cells() for r in items))
    assert len(item_cells) == 12
    assert sum(len(r.terminals) for r in items) == 20
    for r in layout.routes:
        on_cell: dict[Cell, list[str]] = {}
        for t in r.terminals:
            on_cell.setdefault(t.cell.as_tuple(), []).append(t.machine_id)
        # Shared, but never by two connections of one machine: one face does one thing.
        assert all(len(ms) == len(set(ms)) for ms in on_cell.values()), on_cell
    shared = [r for r in items if len({t.cell.as_tuple() for t in r.terminals}) < len(r.terminals)]
    assert len(shared) == 4  # every item net puts two of its terminals on one block somewhere


def test_the_router_can_route_the_proven_placement(
    proven_build: tuple[InputIR, LayoutResult],
) -> None:
    # Before #164 the router could not route this placement at all: it reported "no free cell
    # path" for the cobblestone net. Letting terminals of one net share a cell routes every item
    # net, and the result passes the same gate. This pins the item router alone, so power is put on
    # ME: the solver would first hold back a dock per energy port, which on this placement takes a
    # cell the cobblestone net needs (a separate defect, R6 in docs/spikes/164-channel-capacity.md),
    # and the dock chain is greedy, so its pipes need not leave the build's cable column free.
    problem, layout = proven_build
    items_only = problem.model_copy(update={"me_toggles": METoggles(power=True)})
    result = route(items_only, layout.placements)

    assert result.ok, result.infeasibility
    assert any(
        len({t.cell.as_tuple() for t in r.terminals}) < len(r.terminals) for r in result.routes
    )
    routed = layout.model_copy(update={"routes": list(result.routes)})
    report = validate(items_only, routed)
    assert report.ok, str(report)


def test_the_crowding_gate_does_not_turn_the_proven_placement_away(
    proven_build: tuple[InputIR, LayoutResult],
) -> None:
    # ``crowded_machines`` is what the solver asks before it routes a placement, and a placement it
    # names crowded is never routed at all. It used to demand a cell of its own for every pipe
    # connection, which this build's shared pipe blocks break everywhere: 8 machines named in the
    # build's own box, and 5 hammers plus the power source in the plan's region in the order the
    # solver hands placements over (#164). A layout built and run in game must pass it in either
    # region, and in any order - the verdict is about geometry, not about who was asked first.
    problem, layout = proven_build
    by_plan = {m.id: i for i, m in enumerate(problem.machines)}
    orders = {
        "as read": list(layout.placements),
        "as planned": sorted(layout.placements, key=lambda p: by_plan[p.machine_id]),
        "reversed": list(reversed(layout.placements)),
    }
    plan_region = adapt_file(_PLAN).bounding_region
    for region in (problem.bounding_region, plan_region):
        boxed = problem.model_copy(update={"bounding_region": region})
        for name, placements in orders.items():
            assert crowded_machines(boxed, placements) == (), (region, name)
