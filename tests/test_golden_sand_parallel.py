"""The maintainer's parallel-sand build, decoded from the file our exporter wrote and he built.

``tests/golden/schematic/sand-parallel-exported.schematic`` is the one golden whose wiring and
facings are real (the Schematica copy's are not; see that directory's README). It was built in game
and runs on 3 cable blocks and 12 pipe blocks. Its pipes do what the router could not before #164:
several terminals of ONE net on one pipe block, 20 item terminals on 12 cells. This module turns the
file back into a ``LayoutResult``, pins what the validator makes of it, and pins that the solver,
handed the build's placement, lays those same 12 pipe blocks and 3 cable blocks::

    .schematic --read_schematic--> machines (mID, mFacing)   pipes/cables (mID, mConnections)
                                        |                              |
                                        v                              v
                                   Placements           a run per connected component; a
                                                        Terminal per wired side facing a machine
                                        \\                             /
                                         '---> LayoutResult <--------'
                                                   |
            validate() refuses its plain pipes  <--'--> every run belongs to exactly one net
            and accepts the gauges that worked

**What is chosen rather than read.** An exported GT machine records only its ``mID`` and facing,
so which recipe each Forge Hammer runs is not in the file. The build's author put stage 1 on the
bottom layer and stage 3 on the top (the Schematica copy shows stone in the bottom chest and sand
in the top hammers' output slots), so a hammer's stage is its ``y``. Which net each run carries is
then **not** assumed: it is looked up as the one net whose endpoints are exactly the machines the
run is wired to, and the decode fails if there is none.

**The pipe gauge is read, not assumed.** Every pipe in the export is ``mID`` 5591, the normal tin
item pipe, so each run is published at that size: it is what was built, and what ran at a third of
its designed rate. The validator refuses it (#190). The gauges that then worked are read from
``sand-parallel-reference.schematic``, the Schematica copy of the working build: its block
identities are trustworthy (only its wiring and facings are not; see the README), and its twelve
pipes sit on exactly the export's twelve cells. It has huge on the two runs to and from a chest and
large on the two between hammer stages, and the validator must accept that.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import route_material
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    METoggles,
    PipeSize,
    Placement,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell
from gtnh_solver.router import route
from gtnh_solver.schematic import read_schematic
from gtnh_solver.schematic.core import FORGE_DIRECTION
from gtnh_solver.schematic.read import TileEntity
from gtnh_solver.solver.core import _assemble
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_ROOT = Path(__file__).resolve().parents[1]
_PLAN = _ROOT / "examples" / "gtnh-parallel-sand.json"
_BUILD = _ROOT / "tests" / "golden" / "schematic" / "sand-parallel-exported.schematic"
_WORKING = _ROOT / "tests" / "golden" / "schematic" / "sand-parallel-reference.schematic"

#: The GT ``mID`` of every block in the build (the texture manifest names them; see the README).
_HAMMER, _CHEST, _SOURCE = 611, 135, 15498
#: Tin item pipe ids by size, 5589 up (``dataset/pipe_capacity.py``). The export has only 5591; the
#: working build has 5592 and 5593.
_PIPE_SIZE = {
    5589: PipeSize.TINY,
    5590: PipeSize.SMALL,
    5591: PipeSize.NORMAL,
    5592: PipeSize.LARGE,
    5593: PipeSize.HUGE,
}
#: Tin cable ids and their gauge in amps: ``cable.tin.02`` and ``cable.tin.04``.
_CABLE_GAUGE = {1247: 2, 1248: 4}

#: The item each run carries, by net: the two runs that meet a chest and the two between stages.
_STONE = "edge-3ba1a1b2-bb5c-496e-bbf7-159d937312eb"
_SAND = "edge-989406ac-e022-4e21-908b-be736ca604e9"
_COBBLESTONE = "edge-433ca11c-5481-4170-ba04-5d4c7c65a005"
_GRAVEL = "edge-5d4de04a-f32e-48bc-840e-da80b61d55e2"

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
    for mids in (set(_PIPE_SIZE), set(_CABLE_GAUGE)):
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
                    # A pipe run is published at the size it was built at. The cables keep no
                    # material: their gauge is the thickness above, and it is all power needs.
                    material=(
                        _run_material(tiles, {c for link in links for c in link})
                        if net.commodity is Commodity.ITEM
                        else None
                    ),
                )
            )
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=placements, routes=routes)
    return problem, layout


def _run_material(tiles: dict[Cell, TileEntity], cells: Iterable[Cell]) -> RouteMaterial:
    """The stand-in tin pipe at the one size every block of the run was built at."""
    (size,) = {_PIPE_SIZE[tiles[cell].mid or 0] for cell in cells}  # a pipe block has an mID
    material = route_material(Commodity.ITEM, size=size)
    assert material is not None  # only a cable with no single tier comes back without one
    return material


def _working_build() -> tuple[InputIR, LayoutResult]:
    """The same build at the gauges that made it work: huge to and from the chests, large between.

    Geometry and wiring are the export's; only the pipe sizes change, each run taking the size the
    Schematica copy of the working build records on its cells (module docstring).
    """
    problem, layout = _proven_build()
    tiles = {tile.pos: tile for tile in read_schematic(_WORKING).tile_entities}
    routes = [
        r.model_copy(update={"material": _run_material(tiles, r.cells())})
        if r.commodity is Commodity.ITEM
        else r
        for r in layout.routes
    ]
    return problem, layout.model_copy(update={"routes": routes})


def _item_sizes(routes: Iterable[Route]) -> dict[str, PipeSize | None]:
    return {
        r.net_id: r.material.size if r.material else None
        for r in routes
        if r.commodity is Commodity.ITEM
    }


def _refused_block(message: str) -> Cell:
    """The pipe block an ``item_pipe_size_insufficient`` message names."""
    found = re.search(r" block \((\d+), (\d+), (\d+)\) carries ", message)
    assert found, message
    x, y, z = (int(v) for v in found.groups())
    return (x, y, z)


@pytest.fixture
def proven_build() -> tuple[InputIR, LayoutResult]:
    return _proven_build()


@pytest.fixture
def working_build() -> tuple[InputIR, LayoutResult]:
    return _working_build()


def test_the_export_built_in_game_is_refused_for_its_plain_pipes(
    proven_build: tuple[InputIR, LayoutResult],
) -> None:
    """The case #190 was filed for: this layout ran at a third of its rate and ``validate()`` passed
    it. The stone chest docks on the end block of its run, so stone for the two far hammers crosses
    that block: three streams through a pipe that makes one insertion per 40 ticks. The sand run is
    the mirror image. The cobblestone and gravel runs pair each producer with the consumer above
    it, one stream a block, and a normal pipe carries that.

    Everything else about the build was proven in game (geometry, wiring, facings, power), so the
    pipe size must be the ONLY thing refused: that part of the old guard stands.
    """
    problem, layout = proven_build
    assert set(_item_sizes(layout.routes).values()) == {PipeSize.NORMAL}  # 5591, as built

    report = validate(problem, layout)
    assert not report.ok
    assert set(report.codes()) == {ViolationCode.ITEM_PIPE_SIZE_INSUFFICIENT}, str(report)
    refused = {_refused_block(v.message) for v in report.violations}
    # The block at the stone chest carries 3 streams and the next one 2; the far end carries 1,
    # which a normal pipe does carry. On the sand run the far end also pays for the middle hammer's
    # deliveries, being as near that sender as the chest's block is (GT's scan, see the validator).
    # Nothing on the cobblestone (x = 2, y = 0) or gravel (x = 2, y = 2) runs.
    assert refused == {(0, 0, 1), (0, 0, 2), (0, 2, 0), (0, 2, 1), (0, 2, 2)}

    at_the_chest = next(v.message for v in report.violations if "block (0, 0, 2)" in v.message)
    assert at_the_chest == (
        f"item route for net {_STONE!r} block (0, 0, 2) carries 3 streams of minecraft:stone "
        "(0.3 items/t) needing 3 insertions per 40 ticks, but its normal tin pipe makes only 1"
    )


def test_the_working_build_passes_the_validator(
    working_build: tuple[InputIR, LayoutResult],
) -> None:
    """The strongest guard on the validator this repo has: a layout known to build and run must
    never be rejected. 8 of its 12 pipe blocks and all 3 of its cables carry terminals of several
    machines, so a rule that forbade that would fail here, and so would a pipe rule that sized the
    stage-to-stage runs for the worst case rather than for the one stream each block carries."""
    problem, layout = working_build
    assert _item_sizes(layout.routes) == {
        _STONE: PipeSize.HUGE,
        _SAND: PipeSize.HUGE,
        _COBBLESTONE: PipeSize.LARGE,
        _GRAVEL: PipeSize.LARGE,
    }
    report = validate(problem, layout)
    assert report.ok, str(report)


def test_huge_is_needed_exactly_where_a_chest_docks(
    working_build: tuple[InputIR, LayoutResult],
) -> None:
    # The rule is tight as well as lenient: one size down on every run, large everywhere, fails at
    # the two chest blocks (3 streams against large's 2 insertions per 40 ticks) and nowhere else.
    problem, layout = working_build
    large = route_material(Commodity.ITEM, size=PipeSize.LARGE)
    regauged = layout.model_copy(
        update={
            "routes": [
                r.model_copy(update={"material": large}) if r.commodity is Commodity.ITEM else r
                for r in layout.routes
            ]
        }
    )
    report = validate(problem, regauged)
    assert set(report.codes()) == {ViolationCode.ITEM_PIPE_SIZE_INSUFFICIENT}
    assert sorted(_refused_block(v.message) for v in report.violations) == [(0, 0, 2), (0, 2, 2)]


def test_the_gate_does_not_demand_the_size_the_router_would_lay(
    working_build: tuple[InputIR, LayoutResult],
) -> None:
    """The trap #190 had to avoid. The router sizes a whole run for the point where all its streams
    could meet, so on this placement it lays huge on the cobblestone run. Used as the refusal
    threshold, that rule would reject the working build's large there. The solver may over-size;
    the gate must not reject what works."""
    problem, layout = working_build
    items_only = problem.model_copy(update={"me_toggles": METoggles(power=True)})
    routed = route(items_only, layout.placements)
    assert routed.ok, routed.infeasibility
    assert _item_sizes(routed.routes) == {
        _STONE: PipeSize.HUGE,
        _SAND: PipeSize.HUGE,
        _COBBLESTONE: PipeSize.HUGE,
        _GRAVEL: PipeSize.HUGE,
    }
    assert _item_sizes(layout.routes)[_COBBLESTONE] is PipeSize.LARGE
    assert validate(problem, layout).ok


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
    # net, and the result passes the same gate. This pins the item router alone, power on ME: with
    # nothing else in the way each item net takes one 3-block run, the build's own 12 blocks.
    problem, layout = proven_build
    items_only = problem.model_copy(update={"me_toggles": METoggles(power=True)})
    result = route(items_only, layout.placements)

    assert result.ok, result.infeasibility
    assert any(
        len({t.cell.as_tuple() for t in r.terminals}) < len(r.terminals) for r in result.routes
    )
    assert len(set().union(*(r.cells() for r in result.routes))) == 12
    routed = layout.model_copy(update={"routes": list(result.routes)})
    report = validate(items_only, routed)
    assert report.ok, str(report)


def test_the_solver_lays_the_proven_placement_as_it_was_built(
    proven_build: tuple[InputIR, LayoutResult],
) -> None:
    # The regression pin for #164. Given the maintainer's placement, the solver's own assembly
    # (router, power router, hatches, validator) lays it exactly as he built it in game: the same
    # 12 pipe blocks and the same 3 cable blocks. Until the router negotiated docks and power
    # together it could not route this placement at all, because the power hold took 3 of the 4
    # dock cells of a middle hammer and gravel had nowhere left to dock.
    problem, layout = proven_build
    routed, failed = _assemble(problem, tuple(layout.placements), 0)

    assert routed.status is LayoutStatus.VALID, routed.infeasibility
    assert failed == ()

    def cells(routes: Iterable[Route], power: bool) -> set[Cell]:
        return {c for r in routes if (r.commodity is Commodity.POWER) is power for c in r.cells()}

    assert len(cells(routed.routes, power=False)) == 12
    assert len(cells(routed.routes, power=True)) == 3
    assert cells(routed.routes, power=False) == cells(layout.routes, power=False)
    assert cells(routed.routes, power=True) == cells(layout.routes, power=True)
