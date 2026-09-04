"""The route -> blocks derivation the build guide counts and the previewer draws.

This code was JavaScript in the viewer template until now, which is why it is worth being precise
about what is being tested. Two of the three things it does are just bookkeeping; one is a build
instruction:

1. **The mask** - which sides of each cell connect. Bookkeeping, but the previewer's whole shape
   comes out of it, so the properties are pinned with hypothesis rather than an example or two.
2. **The gauge** - a cell incident to two thicknesses is built at the thicker one (docs/DOMAIN.md).
   This is the build instruction: as a coloured bar it was harmless smoothing, as a real block it
   tells a player which cable to place, and under-sizing is what burns. The sand line's cell
   ``(2,0,1)`` really does carry a 1x and a 2x segment, so it pins the rule on a real artifact.
3. **The block** - the name and label. The stand-in flag has to survive to the guide, and the
   dataset name has to actually *join* to a manifest entry, so a real solve is checked against the
   committed manifest rather than the names being asserted against the table that produced them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import load_physical_dataset
from gtnh_solver.ir import (
    CellCoord,
    Commodity,
    Facing,
    Infeasibility,
    LayoutResult,
    LayoutStatus,
    PipeFamily,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import FACE_DELTAS
from gtnh_solver.route_blocks import route_block, route_block_counts, route_cells
from gtnh_solver.solver import solve

_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"

_COORDS = st.integers(min_value=-4, max_value=4)
_CELLS = st.builds(CellCoord, x=_COORDS, y=_COORDS, z=_COORDS)
_GAUGES = st.sampled_from((1, 2, 4, 8, 12, 16))


@st.composite
def _power_routes(draw: st.DrawFn) -> Route:
    """A power route of unit hops from random cells, with random terminals - deliberately not a
    connected tree, because none of the properties below need one and a generator that insisted on
    it would test the generator."""
    steps = list(FACE_DELTAS.values())
    hops = draw(st.lists(st.tuples(_CELLS, st.sampled_from(steps)), min_size=1, max_size=8))
    segments = [
        Segment(
            start=start,
            end=CellCoord(x=start.x + dx, y=start.y + dy, z=start.z + dz),
            channel=0,
        )
        for start, (dx, dy, dz) in hops
    ]
    terminals = draw(
        st.lists(
            st.builds(
                Terminal,
                machine_id=st.just("m"),
                port_id=st.just("p"),
                face=st.sampled_from(list(Facing)),
                cell=_CELLS,
            ),
            max_size=3,
        )
    )
    return Route(
        net_id="power:LV",
        commodity=Commodity.POWER,
        segments=segments,
        terminals=terminals,
        thickness_per_segment=[draw(_GAUGES) for _ in segments],
    )


# ------------------------------------------------------------------------------------------- 1


@given(_power_routes())
def test_every_cell_the_route_touches_becomes_exactly_one_block(route: Route) -> None:
    touched = {c for seg in route.segments for c in (seg.start.as_tuple(), seg.end.as_tuple())}
    touched |= {t.cell.as_tuple() for t in route.terminals}
    cells = [rc.cell for rc in route_cells(route)]
    assert sorted(cells) == sorted(touched)
    assert len(cells) == len(set(cells))  # one cell is one block, however many hops cross it


@given(_power_routes())
def test_every_connection_is_a_unit_step(route: Route) -> None:
    for rc in route_cells(route):
        for step in rc.dirs:
            assert sum(abs(d) for d in step) == 1


@given(_power_routes())
def test_a_hop_connects_both_of_its_cells_to_each_other(route: Route) -> None:
    """The arms have to meet: an arm out of A toward B with no arm back out of B is a cable that
    stops mid-block."""
    by_cell = {rc.cell: rc for rc in route_cells(route)}
    for seg in route.segments:
        a, b = seg.start.as_tuple(), seg.end.as_tuple()
        step = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        assert step in by_cell[a].dirs
        assert (-step[0], -step[1], -step[2]) in by_cell[b].dirs


@given(_power_routes())
def test_a_terminal_connects_inward_toward_the_machine_it_docks_on(route: Route) -> None:
    """``face`` is the machine's face, so the lead runs the other way - out of the cell and into
    the block. Getting the sign wrong points every machine lead into open air."""
    by_cell = {rc.cell: rc for rc in route_cells(route)}
    for t in route.terminals:
        dx, dy, dz = FACE_DELTAS[t.face]
        assert (-dx, -dy, -dz) in by_cell[t.cell.as_tuple()].dirs


def test_a_non_unit_segment_is_a_block_at_each_end_and_no_arm_between() -> None:
    """The contract does not forbid a non-unit hop - only the validator does, and previews get
    drawn of invalid layouts. Growing an arm for one would run a cable through whatever sits in
    between; two disconnected nodes is what actually happened.
    """
    route = Route(
        net_id="power:LV",
        commodity=Commodity.POWER,
        segments=[Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=3, y=0, z=0), channel=0)],
        thickness_per_segment=[1],
    )
    cells = route_cells(route)
    assert [rc.cell for rc in cells] == [(0, 0, 0), (3, 0, 0)]
    assert all(rc.dirs == frozenset() for rc in cells)


# ------------------------------------------------------------------------------------------- 2


@given(_power_routes())
def test_a_cell_is_built_at_the_thickest_cable_that_meets_it(route: Route) -> None:
    """Rule 1, restated independently: recomputed here by scanning the segments rather than by
    re-running the accumulator under test."""
    gauges = route.thickness_per_segment or []
    for rc in route_cells(route):
        incident = [
            gauge
            for seg, gauge in zip(route.segments, gauges, strict=True)
            if rc.cell in (seg.start.as_tuple(), seg.end.as_tuple())
        ]
        assert rc.thickness == max(incident, default=1)


def test_the_sand_lines_split_cell_takes_the_thicker_cable() -> None:
    """The rule on a real artifact. ``(2,0,1)`` is where the LV trunk splits: a 2x run continues
    east and a 1x leg drops west to the last machine, so the cell is incident to both. It is one
    block and the 2x cable is the one that physically meets it.
    """
    layout = _sand()
    power = next(r for r in layout.routes if r.commodity is Commodity.POWER)
    by_cell = {rc.cell: rc for rc in route_cells(power)}
    split = by_cell[(2, 0, 1)]
    incident = {
        gauge
        for seg, gauge in zip(power.segments, power.thickness_per_segment or [], strict=True)
        if (2, 0, 1) in (seg.start.as_tuple(), seg.end.as_tuple())
    }
    assert incident == {1, 2}, "the fixture no longer pins the rule; find the new split cell"
    assert split.thickness == 2
    assert split.block.label == "2x tin cable"
    assert split.thickness_blocks == pytest.approx(0.375)


# ------------------------------------------------------------------------------------------- 3


def test_a_cable_names_its_gauge_and_its_manifest_entry() -> None:
    block = route_block(
        Commodity.POWER, 2, RouteMaterial(family=PipeFamily.CABLE, material="tin", tier="LV")
    )
    assert (block.label, block.dataset_name, block.stand_in) == (
        "2x tin cable",
        "cable.tin.02",
        True,
    )


def test_a_pipe_names_its_family_and_carries_no_gauge() -> None:
    for commodity, family, material in (
        (Commodity.FLUID, PipeFamily.FLUID_PIPE, "bronze"),
        (Commodity.ITEM, PipeFamily.ITEM_PIPE, "tin"),
    ):
        block = route_block(commodity, 1, RouteMaterial(family=family, material=material))
        assert block.label == f"{material} {commodity.value} pipe"
        assert block.dataset_name == f"gt_pipe_{material}"
        assert block.stand_in


def test_an_unspecified_route_keeps_its_gauge_and_the_old_wording() -> None:
    """``None`` is what every route said before ``material`` existed and what a mixed-tier trunk
    still says, so it degrades to the guide's previous label rather than to a blank."""
    power = route_block(Commodity.POWER, 4, None)
    assert (power.label, power.dataset_name, power.stand_in) == ("4x power cable", None, False)
    fluid = route_block(Commodity.FLUID, 1, None)
    assert (fluid.label, fluid.dataset_name, fluid.stand_in) == ("fluid pipe", None, False)


def test_the_tally_charges_one_block_per_cell() -> None:
    layout = _sand()
    counts = route_block_counts(layout)
    cells = {c for r in layout.routes for c in r.cells()}
    cells |= {t.cell.as_tuple() for r in layout.routes for t in r.terminals}
    assert sum(n for _, n in counts) == len(cells)
    assert [b.label for b, _ in counts] == sorted(b.label for b, _ in counts)


def test_a_shared_cell_is_charged_once_at_the_thicker_gauge() -> None:
    """Two routes cannot legally share a cell (``ROUTE_CELL_COLLISION``), but an invalid layout
    still gets a bill of materials, and billing the same block twice would be worse than the
    violation itself."""
    hop = [Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=0)]
    layout = LayoutResult(
        status=LayoutStatus.PARTIAL_INVALID,
        seed=0,
        infeasibility=Infeasibility(
            constraint="route_cell_collision", detail="two routes share (0,0,0)-(1,0,0)"
        ),
        routes=[
            Route(
                net_id=f"power:LV#{i}",
                commodity=Commodity.POWER,
                segments=hop,
                thickness_per_segment=[gauge],
            )
            for i, gauge in enumerate((1, 4))
        ],
    )
    assert route_block_counts(layout) == [(route_block(Commodity.POWER, 4, None), 2)]


def test_a_real_solves_blocks_all_resolve_in_the_committed_manifest() -> None:
    """The join key actually joins. A name that resolves to nothing renders as a flat bar with no
    error anywhere, so the table in ``dataset/pipes.py`` is checked against extracted data through
    the whole chain a preview walks, not against itself.
    """
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    names = {e.get("display_name") for e in raw["blocks"].values() if e.get("kind") == "pipe"}
    if not names:
        pytest.skip("no pipes in the committed manifest (fixture-only checkout)")

    layout = _sand()
    blocks = [b for b, _ in route_block_counts(layout)]
    assert blocks, "the sand line routes power; its cables must appear"
    for block in blocks:
        assert block.stand_in, (
            "every v1 material is representative; the guide must be able to say so"
        )
        assert block.dataset_name in names, f"{block.label} resolves to nothing"


def _sand() -> LayoutResult:
    """The solved sand line - the smallest real artifact that routes power."""
    return solve(
        adapt_file("examples/gtnh-sand.json", physical=load_physical_dataset()), optimize=False
    )
