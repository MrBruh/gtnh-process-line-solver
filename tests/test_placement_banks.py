"""placement.banks - a chain of banks of parallel single blocks, laid out as columns.

The line this exists for is ``examples/gtnh-parallel-sand.json``, and the layout it has to reproduce
is the maintainer's own build (``tests/golden/schematic/sand-parallel-exported.schematic``), built
and run in game: a 3x3x4 box, 12 pipe blocks, 3 cable blocks. The annealer never found it: the best
of 16 seeds on ``main`` was a 128-cell box with 32 pipe blocks. Everything else here pins how narrow
the constructor is. A line that is not exactly one chain of single-block banks gets ``None``, and
the solver then runs exactly as it did before.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.adapter import adapt_file
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
    PinnedIO,
    Placement,
    Port,
)
from gtnh_solver.ir.geometry import front_on_boundary
from gtnh_solver.placement import bank_columns, crowded_machines
from gtnh_solver.solver import core as solver_core
from gtnh_solver.solver import solve
from gtnh_solver.solver._structure import structure_quality
from gtnh_solver.solver.core import _assemble
from gtnh_solver.validator import validate
from tests._helpers import PLACEMENT_CODES, power_source
from tests.test_golden_sand_parallel import _proven_build

_ROOT = Path(__file__).resolve().parents[1]
_HORIZONTAL = [Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST]


@pytest.fixture(scope="module")
def psand() -> InputIR:
    return adapt_file(_ROOT / "examples" / "gtnh-parallel-sand.json")


def _poses(placements: Sequence[Placement]) -> set[tuple[str, tuple[int, int, int], Facing]]:
    return {(p.machine_id, (p.cell.x, p.cell.y, p.cell.z), p.orientation) for p in placements}


def test_parallel_sand_is_laid_out_as_the_maintainers_build(psand: InputIR) -> None:
    # The same machines, facings and relative cells as the build decoded from the game, moved
    # against the plan region's far z wall so that the power source feeds out of it.
    _, build = _proven_build()
    shift = psand.bounding_region.sz - 4  # the build is 4 deep, its cap row on the wall
    moved = [
        p.model_copy(update={"cell": CellCoord(x=p.cell.x, y=p.cell.y, z=p.cell.z + shift)})
        for p in build.placements
    ]
    columns = bank_columns(psand)
    assert columns is not None
    assert _poses(columns) == _poses(moved)


def test_the_columns_route_to_the_build_that_ran(psand: InputIR) -> None:
    columns = bank_columns(psand)
    assert columns is not None
    assert crowded_machines(psand, columns) == ()
    layout, failed = _assemble(psand, columns, 0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert failed == ()
    pipes = {c for r in layout.routes if r.commodity is not Commodity.POWER for c in r.cells()}
    cable = {c for r in layout.routes if r.commodity is Commodity.POWER for c in r.cells()}
    assert (len(pipes), len(cable)) == (12, 3)


def test_solve_returns_the_column_build_on_parallel_sand(psand: InputIR) -> None:
    # The size gap itself. The candidate is routed first and wins the ranking outright: a 3x4
    # floor, three layers, and 15 route cells (12 pipe, 3 cable).
    layout = solve(psand, seed=0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert (layout.metrics.footprint, layout.metrics.layers) == (12, 3)
    assert structure_quality(psand, layout.placements, layout.routes, "footprint") == (12, 15, 36)


def test_a_column_layout_that_does_not_come_out_valid_leaves_the_line_to_the_grid(
    psand: InputIR, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The candidate is only ever kept VALID. Here its assembly is made to fail, and the solver has
    # to fall back to its annealed attempts, whose failures it does not blame on the columns.
    columns = bank_columns(psand)
    assert columns is not None
    real = solver_core._assemble

    def assemble(
        problem: InputIR, placements: tuple[Placement, ...], *args: Any, **kwargs: Any
    ) -> Any:
        layout, failed = real(problem, placements, *args, **kwargs)
        if placements == columns:
            return layout.model_copy(update={"status": LayoutStatus.PARTIAL_INVALID}), failed
        return layout, failed

    monkeypatch.setattr(solver_core, "_assemble", assemble)
    layout = solve(psand, seed=0)
    assert layout.status is LayoutStatus.VALID
    assert _poses(layout.placements) != _poses(columns)


# --- synthetic chains ---------------------------------------------------------------------------


def _stage(mid: str, orientations: list[Facing] | None = None) -> Machine:
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=orientations or _HORIZONTAL,
        faces=FaceSpec(
            ports=[
                Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
                Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT),
            ]
        ),
    )


def _end(mid: str, direction: IODirection) -> Machine:
    port = "out" if direction is IODirection.OUTPUT else "in"
    return Machine(
        id=mid,
        type="chest",
        voltage_tier="LV",
        orientation_options=_HORIZONTAL,
        faces=FaceSpec(ports=[Port(id=port, commodity=Commodity.ITEM, direction=direction)]),
    )


def _flow(nid: str, sources: Sequence[Machine], sinks: Sequence[Machine]) -> Net:
    return Net(
        id=nid,
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            *(MachineFaceRef(machine_id=m.id, port_id="out") for m in sources),
            *(MachineFaceRef(machine_id=m.id, port_id="in") for m in sinks),
        ],
    )


def _chain(
    sizes: Sequence[int],
    *,
    feed: bool = True,
    drain: bool = True,
    region: CellBox | None = None,
    orientations: list[Facing] | None = None,
) -> InputIR:
    """A feed, banks of ``sizes`` machines in a row, a drain, and one power source for them all."""
    banks = [[_stage(f"s{k}#{j}", orientations) for j in range(n)] for k, n in enumerate(sizes)]
    levels = [
        *([[_end("feed", IODirection.OUTPUT)]] if feed else []),
        *banks,
        *([[_end("drain", IODirection.INPUT)]] if drain else []),
    ]
    nets = [_flow(f"n{i}", a, b) for i, (a, b) in enumerate(pairwise(levels))]
    stages = [m for bank in banks for m in bank]
    nets.append(
        Net(
            id="power:LV",
            commodity=Commodity.POWER,
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id="src", port_id="power:out"),
                *(MachineFaceRef(machine_id=m.id, port_id="power:in") for m in stages),
            ],
        )
    )
    return InputIR(
        bounding_region=region or CellBox(sx=8, sy=6, sz=8),
        machines=[*(m for level in levels for m in level), power_source(orientations=_HORIZONTAL)],
        nets=nets,
    )


def _placements_are_clean(problem: InputIR, placements: Sequence[Placement]) -> None:
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=list(placements))
    assert PLACEMENT_CODES.isdisjoint(validate(problem, layout).codes())


@pytest.mark.parametrize("sizes", [[3], [2, 2], [3, 1, 3], [2, 3, 2, 3]])
def test_a_chain_of_banks_gets_clean_columns(sizes: list[int]) -> None:
    problem = _chain(sizes)
    columns = bank_columns(problem)
    assert columns is not None
    assert [p.machine_id for p in columns] == [m.id for m in problem.machines]
    _placements_are_clean(problem, columns)
    # Each bank is one straight column: one (x, y), consecutive z.
    for k, n in enumerate(sizes):
        members = [p for p in columns if p.machine_id.startswith(f"s{k}#")]
        assert len({(p.cell.x, p.cell.y) for p in members}) == 1
        assert sorted(p.cell.z for p in members) == list(
            range(members[0].cell.z, members[0].cell.z + n)
        )


def test_the_feed_and_the_drain_cap_their_stages_and_the_source_feeds_out_of_the_wall() -> None:
    problem = _chain([3, 3])
    columns = bank_columns(problem)
    assert columns is not None
    at = {p.machine_id: p for p in columns}
    cap = problem.bounding_region.sz - 1
    assert (at["feed"].cell.x, at["feed"].cell.y, at["feed"].cell.z) == (0, 0, cap)
    assert at["drain"].cell.z == cap
    assert at["src"].cell.z == cap
    source = next(m for m in problem.machines if m.is_power_source)
    assert front_on_boundary(
        at["src"].cell, source.footprint, at["src"].orientation, problem.bounding_region
    )


def test_a_bank_line_without_its_feed_or_drain_still_gets_columns() -> None:
    columns = bank_columns(_chain([3], feed=False, drain=False))
    assert columns is not None
    assert {p.machine_id for p in columns} == {"s0#0", "s0#1", "s0#2", "src"}


def test_a_machine_that_cannot_face_north_takes_its_first_legal_facing() -> None:
    problem = _chain([2, 2], orientations=[Facing.EAST, Facing.WEST])
    columns = bank_columns(problem)
    assert columns is not None
    assert all(p.orientation is Facing.EAST for p in columns if "#" in p.machine_id)
    _placements_are_clean(problem, columns)


# --- where it steps aside -------------------------------------------------------------------------


def test_a_line_of_single_machines_is_left_to_the_search() -> None:
    assert bank_columns(adapt_file(_ROOT / "examples" / "gtnh-sand.json")) is None
    assert bank_columns(_chain([1, 1, 1])) is None


def test_a_multiblock_is_left_to_the_search(psand: InputIR) -> None:
    big = psand.machines[0].model_copy(update={"footprint": CellBox(sx=3, sy=3, sz=3)})
    assert bank_columns(psand.model_copy(update={"machines": [big, *psand.machines[1:]]})) is None


def test_pinned_io_is_left_to_the_search(psand: InputIR) -> None:
    pin = PinnedIO(net_id=psand.nets[0].id, cell=CellCoord(x=0, y=0, z=0), kind=IODirection.INPUT)
    assert bank_columns(psand.model_copy(update={"pinned": [pin]})) is None


def test_a_branch_is_not_a_chain() -> None:
    # One bank feeding two drains on two nets.
    problem = _chain([2])
    extra = _end("drain2", IODirection.INPUT)
    bank = [m for m in problem.machines if m.id.startswith("s0#")]
    branched = problem.model_copy(
        update={
            "machines": [*problem.machines, extra],
            "nets": [*problem.nets, _flow("n9", bank, [extra])],
        }
    )
    assert bank_columns(branched) is None


def test_two_feeds_into_one_bank_are_not_a_chain() -> None:
    problem = _chain([2])
    extra = _end("feed2", IODirection.OUTPUT)
    bank = [m for m in problem.machines if m.id.startswith("s0#")]
    joined = problem.model_copy(
        update={
            "machines": [*problem.machines, extra],
            "nets": [*problem.nets, _flow("n9", [extra], bank)],
        }
    )
    assert bank_columns(joined) is None


def test_a_net_into_two_banks_is_not_a_chain() -> None:
    problem = _chain([2, 2], feed=False, drain=False)
    feed = _end("feed", IODirection.OUTPUT)
    stages = [m for m in problem.machines if m.id.startswith("s")]
    fanned = problem.model_copy(
        update={
            "machines": [*problem.machines, feed],
            "nets": [*problem.nets, _flow("n9", [feed], stages)],
        }
    )
    assert bank_columns(fanned) is None


def test_a_bank_feeding_itself_is_not_a_chain() -> None:
    problem = _chain([2], feed=False, drain=False)
    bank = [m for m in problem.machines if m.id.startswith("s0#")]
    looped = problem.model_copy(update={"nets": [*problem.nets, _flow("n9", bank, bank)]})
    assert bank_columns(looped) is None


def test_a_cycle_of_banks_is_not_a_chain() -> None:
    problem = _chain([2, 2], feed=False, drain=False)
    a = [m for m in problem.machines if m.id.startswith("s0#")]
    b = [m for m in problem.machines if m.id.startswith("s1#")]
    cycled = problem.model_copy(update={"nets": [*problem.nets, _flow("n9", b, a)]})
    assert bank_columns(cycled) is None


def test_a_second_line_or_a_stray_machine_is_not_one_chain() -> None:
    problem = _chain([2, 2])
    stray = _end("stray", IODirection.OUTPUT)
    assert bank_columns(problem.model_copy(update={"machines": [*problem.machines, stray]})) is None


def test_a_power_source_on_a_material_net_is_not_a_chain() -> None:
    problem = _chain([2], feed=False)
    source = next(m for m in problem.machines if m.is_power_source)
    hybrid = source.model_copy(
        update={
            "faces": FaceSpec(
                ports=[
                    *source.faces.ports,
                    Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
                ]
            )
        }
    )
    bank = [m for m in problem.machines if m.id.startswith("s0#")]
    feeding = problem.model_copy(
        update={
            "machines": [m for m in problem.machines if m is not source] + [hybrid],
            "nets": [*problem.nets, _flow("n9", [hybrid], bank)],
        }
    )
    assert bank_columns(feeding) is None


@pytest.mark.parametrize(
    "region",
    [
        CellBox(sx=24, sy=4, sz=3),  # too shallow: three deep, and a cap row behind
        CellBox(sx=24, sy=2, sz=24),  # too low: three stages need three layers
        CellBox(sx=2, sy=4, sz=24),  # too narrow for the diagonal
    ],
)
def test_a_region_too_small_for_the_columns_gets_none(psand: InputIR, region: CellBox) -> None:
    assert bank_columns(psand.model_copy(update={"bounding_region": region})) is None


def test_a_reserved_cell_on_a_column_gets_none(psand: InputIR) -> None:
    blocked = CellCoord(x=1, y=0, z=psand.bounding_region.sz - 4)
    assert bank_columns(psand.model_copy(update={"reserved_cells": [blocked]})) is None


def test_a_source_that_cannot_face_the_wall_gets_none(psand: InputIR) -> None:
    machines = [
        m.model_copy(update={"orientation_options": [Facing.NORTH]}) if m.is_power_source else m
        for m in psand.machines
    ]
    assert bank_columns(psand.model_copy(update={"machines": machines})) is None


def test_no_free_column_for_the_source_gets_none() -> None:
    # One layer three wide: the stage, its feed and its drain take every column there is.
    assert bank_columns(_chain([3], region=CellBox(sx=3, sy=1, sz=8))) is None


def test_no_free_column_for_the_drain_gets_none() -> None:
    # Four stages zig-zag up a 3-wide, 4-high region; the last stage's pipe run takes the only
    # column beside it the drain could have had.
    assert bank_columns(_chain([2, 2, 2, 2], region=CellBox(sx=3, sy=4, sz=8))) is None
