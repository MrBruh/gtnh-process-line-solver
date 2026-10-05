"""Tests for the previewer's testable seam: the (problem, layout) -> scene mapping and the HTML
assembly. The WebGL rendering itself is validated by eye, not in CI - so everything that *can*
be asserted (scene shape, colours, thickness, the inlined-and-self-contained HTML) is.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import gtnh_solver.previewer as previewer_package
from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    AutoConnection,
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
    PlacedHatch,
    Port,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.previewer import build_scene, render_html, write_preview
from gtnh_solver.previewer.scene import (
    FACE_CAP,
    FACE_COVERED,
    FACE_EXPOSED,
    _hatch_label,
    block_face_cover,
)
from gtnh_solver.solver import solve
from tests._helpers import at, consumer, layered_tower, machine, net, producer
from tests._me_fixtures import gt_hatch_line

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _sand_scene(
    *me_commodities: Commodity, me_power: bool = False, optimize: bool = False
) -> dict[str, Any]:
    # The fast (constructive) solve by default: deterministic layout coordinates that the
    # exact-cell assertions below can rely on; scene building does not care which placer produced
    # them. A line on an ME network needs the optimizing solve, since the fast path's touching row
    # leaves a block no face for its ME devices (#335).
    ir = adapt_file(_SAND, me_commodities=me_commodities, me_power=me_power)
    return build_scene(ir, solve(ir, seed=0, optimize=optimize))


def test_scene_has_machines_region_and_legend() -> None:
    scene = _sand_scene()
    assert scene["status"] == "valid"
    assert scene["region"]["sy"] == 4  # the adapter's bounding region is 4 tall
    assert len(scene["machines"]) == 6  # 3 hammers + 2 chests (input + output buffer) + LV source
    assert any(m["role"] == "source" for m in scene["machines"])  # the synthesized power source
    assert any(m["role"] == "storage" for m in scene["machines"])  # the Super Chest
    assert scene["legend"]  # one entry per machine type
    # every machine carries the geometry the viewer needs, with no further lookups
    m = scene["machines"][0]
    assert set(m) >= {"id", "type", "cell", "size", "front", "role", "color", "voltage_tier"}
    assert all(mm["voltage_tier"] for mm in scene["machines"])  # tier drives single-block texturing
    # ...and so does the recipe map a machine runs, which names it where its type does not (#232)
    hammers = [mm for mm in scene["machines"] if mm["type"] == "Forge Hammer"]
    assert hammers
    assert all(mm["recipe_map"] == "gt.recipe.hammer" for mm in hammers)


def test_scene_power_route_carries_thickness() -> None:
    scene = _sand_scene()
    power = [r for r in scene["routes"] if r["commodity"] == "power"]
    assert len(power) == 1
    thicknesses = [seg["thickness"] for r in power for seg in r["segments"]]
    assert thicknesses  # the trunk has segments
    assert all(isinstance(t, int) for t in thicknesses)
    assert set(thicknesses) <= {1, 2, 4, 8, 12, 16}


def test_scene_items_auto_feed_so_no_item_pipes() -> None:
    scene = _sand_scene()
    assert [r["commodity"] for r in scene["routes"]] == ["power"]  # only the power cable is routed
    assert len(scene["autoConnections"]) == 4  # the 3 item chain links + the sand->output buffer


def test_scene_item_pipe_segments_have_null_thickness() -> None:
    # a fan-out: one item net auto-outputs, the other is piped (thickness is power-only)
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m1"), consumer("m2"), consumer("m3")],
        nets=[net("n1", "m1", "m2"), net("n2", "m1", "m3")],
    )
    scene = build_scene(problem, solve(problem))
    item_routes = [r for r in scene["routes"] if r["commodity"] == "item"]
    assert item_routes  # the piped fan-out leg
    assert all(seg["thickness"] is None for r in item_routes for seg in r["segments"])
    # ...and every cell of one is a normal item pipe: gauge 1, GT's own 0.5-block cross-section.
    cells = [c for r in item_routes for c in r["cells"]]
    assert cells
    assert all(c["thickness"] == 1 and c["size"] == pytest.approx(0.5) for c in cells)
    assert all(c["block"] == "gt_pipe_tin" for c in cells)


def test_scene_bounds_are_tight_not_the_search_region() -> None:
    scene = _sand_scene()
    region = scene["region"]
    bounds = scene["bounds"]
    span = [bounds["max"][i] - bounds["min"][i] for i in range(3)]
    assert {m["cell"][1] for m in scene["machines"]} == {0}  # every machine sits on the floor...
    assert span[1] <= 2  # ...the power cable may rise a single layer to reach around the row
    assert max(span) <= 6  # still tight around the built structure...
    assert max(span) < max(region["sx"], region["sy"], region["sz"])  # ...not the 10x4x10 region


def test_scene_bounds_fall_back_to_region_when_empty() -> None:
    problem = InputIR(bounding_region=CellBox(sx=3, sy=2, sz=4))
    scene = build_scene(problem, LayoutResult(status=LayoutStatus.VALID, seed=0))
    assert scene["bounds"] == {"min": [0, 0, 0], "max": [3, 2, 4]}


def test_scene_bounds_count_a_one_block_pipe() -> None:
    # A route with no segment (LayoutResult v3) is still a block that gets built, so it frames the
    # scene like any other. Read off the segments, it was nothing, and the scene fell back.
    problem = InputIR(bounding_region=CellBox(sx=5, sy=5, sz=5))
    block = CellCoord(x=2, y=3, z=4)
    route = Route(
        net_id="n",
        commodity=Commodity.ITEM,
        terminals=[
            Terminal(machine_id="a", port_id="out", face=Facing.UP, cell=block),
            Terminal(machine_id="b", port_id="in", face=Facing.DOWN, cell=block),
        ],
    )
    scene = build_scene(problem, LayoutResult(status=LayoutStatus.VALID, seed=0, routes=[route]))
    assert scene["bounds"] == {"min": [2, 3, 4], "max": [3, 4, 5]}


# ------------------------------------------------------ which block faces the viewer can skip


def test_a_face_against_a_block_on_its_own_layer_is_covered() -> None:
    # Two blocks side by side on x: the faces they press together are never seen in any view.
    cover = block_face_cover([(0, 0, 0), (1, 0, 0)])
    assert cover[(0, 0, 0)] == (FACE_COVERED, 0, 0, 0, 0, 0)  # +x, -x, +y, -y, +z, -z
    assert cover[(1, 0, 0)] == (0, FACE_COVERED, 0, 0, 0, 0)


def test_a_face_against_the_next_layer_is_a_cap() -> None:
    # Stacked blocks hide each other's touching faces only while both layers show. Isolate either
    # layer and that face is its lid, so it is a CAP rather than covered.
    cover = block_face_cover([(0, 0, 0), (0, 1, 0)])
    assert cover[(0, 0, 0)] == (0, 0, FACE_CAP, 0, 0, 0)
    assert cover[(0, 1, 0)] == (0, 0, 0, FACE_CAP, 0, 0)


def test_a_block_with_no_neighbour_shows_every_face() -> None:
    assert block_face_cover([(3, 4, 5)]) == {(3, 4, 5): (FACE_EXPOSED,) * 6}


_cells = st.sets(
    st.tuples(st.integers(0, 3), st.integers(0, 3), st.integers(0, 3)), min_size=1, max_size=24
)


@given(cells=_cells)
def test_cover_is_exactly_what_the_neighbours_are(cells: set[tuple[int, int, int]]) -> None:
    """Restated independently of the implementation: a face is exposed iff no block is beside it,
    and a hidden face is a cap iff it is a top or bottom. Nothing else hides a face, because the
    layer slider hides whole layers and nothing else hides a block."""
    normals = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    cover = block_face_cover(cells)
    assert set(cover) == cells
    for (x, y, z), slots in cover.items():
        for (dx, dy, dz), slot in zip(normals, slots, strict=True):
            beside = (x + dx, y + dy, z + dz) in cells
            if not beside:
                assert slot == FACE_EXPOSED
            else:
                assert slot == (FACE_CAP if dy else FACE_COVERED)


def test_scene_routes_carry_terminals() -> None:
    power = next(r for r in _sand_scene()["routes"] if r["commodity"] == "power")
    assert power["terminals"]  # so the viewer can draw a lead to each machine face
    term = power["terminals"][0]
    assert set(term) == {"machine", "port", "face", "cell"}
    assert term["port"]  # which port it serves, so a viewer can tie it to its hatch
    assert len(term["cell"]) == 3
    # A terminal no longer carries its own thickness: the cell it docks at does, and the lead is an
    # arm of that block (GitHub #6, now one derivation instead of two - see the fork test below).
    by_cell = {tuple(c["cell"]): c for c in power["cells"]}
    assert all(
        by_cell[tuple(t["cell"])]["thickness"] in {1, 2, 4, 8, 12, 16} for t in power["terminals"]
    )


def test_scene_route_cells_carry_the_block_and_its_real_size() -> None:
    """What the viewer draws with. ``size`` is GT's own cross-section for the gauge rather than a
    bar scaled to look right, and ``block`` is the manifest join key the texture pass resolves."""
    power = next(r for r in _sand_scene()["routes"] if r["commodity"] == "power")
    assert {k for c in power["cells"] for k in c} == {
        "cell",
        "dirs",
        "thickness",
        "size",
        "block",
        "label",
        "boxes",
    }
    sizes = {c["thickness"]: c["size"] for c in power["cells"]}
    assert sizes == {1: pytest.approx(0.25), 2: pytest.approx(0.375)}  # GT's insulated ladder
    assert {c["block"] for c in power["cells"]} == {"cable.tin.01", "cable.tin.02"}
    assert all(sum(abs(d) for d in dirs) == 1 for c in power["cells"] for dirs in c["dirs"])


def test_scene_route_cells_carry_the_gt_shape_as_boxes() -> None:
    """The viewer draws these and decides nothing about the shape itself. A straight run is one box
    through the block; a cell that turns or branches is a core plus an arm per connection."""
    power = next(r for r in _sand_scene()["routes"] if r["commodity"] == "power")
    by_cell = {tuple(c["cell"]): c for c in power["cells"]}

    straight = by_cell[(4, 0, 1)]  # the trunk runs east-west through it, nothing else attached
    assert [tuple(d) for d in straight["dirs"]] == [(-1, 0, 0), (1, 0, 0)]
    (box,) = straight["boxes"]
    assert box["size"] == [1.0, pytest.approx(0.375), pytest.approx(0.375)]
    assert box["open"] == [True, True, False, False, False, False]  # +x, -x: the two block faces

    split = by_cell[(2, 0, 1)]  # the branch cell: three connections
    assert len(split["boxes"]) == 4  # a core plus one arm each
    ends = [i for b in split["boxes"] for i, is_end in enumerate(b["open"]) if is_end]
    assert sorted(ends) == [0, 1, 5]  # +x, -x, -z - one open end per connection, no more


def test_scene_route_carries_its_material_and_says_it_stands_in() -> None:
    """The legend footnotes this. A preview that draws Tin without saying the material was chosen
    for recognisability reads as a specification (docs/DOMAIN.md), which is the one failure
    nothing downstream can detect."""
    power = next(r for r in _sand_scene()["routes"] if r["commodity"] == "power")
    assert power["material"] == {
        "family": "cable",
        "material": "tin",
        "tier": "LV",
        "standIn": True,
    }


def test_scene_route_carries_the_resource_it_moves_and_at_what_rate(
    solved_nitrobenzene: tuple[InputIR, LayoutResult],
) -> None:
    """What a hovered pipe has to answer (GitHub #155). The scene carried the commodity, the net id
    and the colour, and nothing that said *what* - so a bundle of crossing fluid pipes was eight
    identical blue noodles. Nitrobenzene is the fixture because its nets carry real fluids at real
    rates; sand's item chain all auto-outputs, leaving power alone.

    The routes are laid by hand, one per fluid net and one power trunk, because what is under test
    is what the scene says about a route, not which pipes a solve lays. Without the dataset the
    fixture's solve is partial (a 1x1x1 machine has too few faces for its ports), and which of its
    pipes the router's collision-free fallback keeps changes whenever placement does (#254).
    """
    problem, _ = solved_nitrobenzene
    hop = [Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=0)]
    routes = [
        Route(net_id=n.id, commodity=Commodity.FLUID, segments=hop)
        for n in problem.nets
        if n.commodity is Commodity.FLUID
    ]
    trunk = next(n for n in problem.nets if n.commodity is Commodity.POWER)
    routes.append(
        Route(net_id=trunk.id, commodity=Commodity.POWER, segments=hop, thickness_per_segment=[1])
    )
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, routes=routes)
    scene = build_scene(problem, layout)
    resource_of = {n.id: n.fluid_or_item for n in problem.nets}
    rate_of = {n.id: n.throughput for n in problem.nets}

    fluids = [r for r in scene["routes"] if r["commodity"] == "fluid"]
    assert fluids, "the nitrobenzene line carries fluids; that is what it is the fixture for"
    names = problem.resource_names
    for route in fluids:
        # Verbatim, exactly the id the plan carries - no display name invented here (#155).
        assert route["resource"] == resource_of[route["netId"]]
        # ...and the label puts the plan's own name in front of it (#296). Every fluid this
        # line moves is named in its plan, so every label carries both.
        resource = route["resource"]
        assert route["label"] == f"{names[resource]} ({resource})"
        assert route["rate"] == pytest.approx(rate_of[route["netId"]])
        assert route["unit"] == "mB"  # stem only; the viewer appends /t or /s, as it does for io

    # A power net names no fluid or item at all, so its route says so instead of inventing one: the
    # commodity is the whole answer there, and the rate is the EU/t the net moves.
    power = next(r for r in scene["routes"] if r["commodity"] == "power")
    assert power["resource"] is None
    assert power["label"] is None
    assert power["rate"] == pytest.approx(rate_of[power["netId"]])
    assert power["unit"] == "EU"


def test_scene_route_whose_net_is_gone_says_nothing_about_what_it_carries() -> None:
    """The previewer draws what it is handed - the validator is the gate, not this - so a route
    referencing a net the problem does not have degrades to "unknown" rather than raising and
    taking the whole preview down with it."""
    route = Route(
        net_id="no-such-net",
        commodity=Commodity.ITEM,
        segments=[Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=0)],
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4))
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, routes=[route])
    (scene_route,) = build_scene(problem, layout)["routes"]
    assert scene_route["resource"] is None
    assert scene_route["label"] is None
    assert scene_route["rate"] is None
    assert scene_route["unit"] == "items"  # the commodity is the route's own, so this still stands


def test_scene_route_cell_takes_the_fattest_cable_that_meets_it() -> None:
    # A hand-built trunk with known per-segment thicknesses (GitHub #6, docs/DOMAIN.md):
    #
    #   src ==4x== [tap] ==2x== m1
    #               |
    #               1x
    #               |
    #               m3
    #
    # Each cell is built at the thickest cable incident to it; the fork is touched by the 4x, 2x
    # and 1x segments at once, so it is a 4x block - that is the cable that physically meets it,
    # and under-sizing is what burns. A terminal docking there gets its lead from the same cell,
    # so the #6 guarantee holds with one derivation rather than two. build_scene's route mapping
    # never looks placements up, so a routes-only layout keeps the fixture minimal.
    def cell(x: int, y: int, z: int) -> CellCoord:
        return CellCoord(x=x, y=y, z=z)

    def seg(a: CellCoord, b: CellCoord) -> Segment:
        return Segment(start=a, end=b, channel=0)

    def term(mid: str, c: CellCoord) -> Terminal:
        return Terminal(machine_id=mid, port_id="power", face=Facing.NORTH, cell=c)

    src, fork, m1, m3 = cell(0, 0, 0), cell(1, 0, 0), cell(2, 0, 0), cell(1, 0, 1)
    route = Route(
        net_id="power:LV",
        commodity=Commodity.POWER,
        terminals=[term("src", src), term("m1", m1), term("m2", fork), term("m3", m3)],
        segments=[seg(src, fork), seg(fork, m1), seg(fork, m3)],
        thickness_per_segment=[4, 2, 1],
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4))
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, routes=[route])
    (scene_route,) = build_scene(problem, layout)["routes"]
    by_cell = {tuple(c["cell"]): c for c in scene_route["cells"]}
    assert [by_cell[(c.x, c.y, c.z)]["thickness"] for c in (src, m1, fork, m3)] == [4, 2, 4, 1]
    # ...and the fork is BUILT as the 4x cable, which is the half that is a build instruction
    # rather than a rendering detail: it is one block and the fattest incident cable meets it.
    # This fixture publishes no material, so the label degrades to the generic wording a route had
    # before any of this - the gauge is real either way, and that is the half being pinned here.
    assert by_cell[(1, 0, 0)]["label"] == "4x power cable"
    assert by_cell[(1, 0, 0)]["block"] is None
    assert scene_route["material"] is None


def test_scene_reports_system_io() -> None:
    # the boundary summary the HUD renders (GitHub #5): what to feed, what comes out, total power
    io = _sand_scene()["io"]
    assert len(io["inputs"]) == 1
    assert io["inputs"][0]["resource"] == "minecraft:stone"
    assert io["inputs"][0]["label"] == "Stone (minecraft:stone)"  # the plan's name, #296
    assert io["inputs"][0]["rate"] == pytest.approx(0.1)
    assert io["inputs"][0]["unit"] == "items"  # stem only; the viewer appends /t or /s
    assert io["outputs"] == [
        {
            "resource": "minecraft:sand",
            "label": "Sand (minecraft:sand)",
            "resources": [{"id": "minecraft:sand", "label": "Sand (minecraft:sand)"}],
            "rate": pytest.approx(0.1),
            "unit": "items",
            "me": False,
            "network": None,  # a chest of the line's own, on no ME network
        }
    ]
    # the power feed per tier: the FULL LV tier voltage (32, not the hammers' 16 EU/t draw) x the
    # amps to supply - what a GT source is fed. The hammers' fractional loads (~0.53 A each at
    # their delivered voltages) sum to 1.66 and round up once to 2 A, so ``total`` is that feed
    # (32 V x 2 A = 64 EU/t) and matches the breakdown, not the machines' lower actual draw.
    assert io["power"] == {
        "total": pytest.approx(64),
        "byTier": {"LV": {"volts": 32, "amps": 2}},
        "me": False,
    }


def test_scene_says_a_flow_left_to_me_arrives_over_me() -> None:
    """With items on ME (``--me items``) the sand line routes no item pipe and auto-outputs nothing
    between machines; its items ride an ME network the solve lays (#335) and the preview draws
    (#338). Its stone comes from the network's storage and its sand goes back there, so no Super
    Chest stands at either end, and the panel lists both, flagged ``me`` with the network whose
    storage it is. Power is still cabled, and says nothing of the sort.
    """
    scene = _sand_scene(Commodity.ITEM, optimize=True)
    assert scene["status"] == "valid"
    assert [r["commodity"] for r in scene["routes"]] == ["power"]
    assert scene["autoConnections"] == []
    io = scene["io"]
    assert [(f["resource"], f["me"], f["network"]) for f in io["inputs"]] == [
        ("minecraft:stone", True, "main")
    ]
    assert [(f["resource"], f["me"], f["network"]) for f in io["outputs"]] == [
        ("minecraft:sand", True, "main")
    ]
    assert io["power"]["me"] is False
    assert not any(m["type"].startswith("Super ") for m in scene["machines"])


def test_scene_says_power_left_to_me_arrives_over_me() -> None:
    # The other commodity on its own: no cable is laid and no source block stands unconnected
    # (#225), the feed spec is still stated from the machines' energy ports (the ME side has to
    # deliver it), and the item flows, still auto-output, are not flagged.
    scene = _sand_scene(me_power=True)
    assert scene["routes"] == []
    assert not any(m["role"] == "source" for m in scene["machines"])
    io = scene["io"]
    assert io["power"]["me"] is True
    assert io["power"]["byTier"] == {"LV": {"volts": 32, "amps": 2}}
    assert not any(f["me"] for f in io["inputs"] + io["outputs"])


def test_scene_names_a_gt_me_hatch_by_its_mid() -> None:
    """GT's ME hatch is listed among the hatches by the slot kind it fills; its device says which
    ME hatch it is, and the scene carries that mID for the texture pass and its name for the hover
    (#338). A normal hatch with an interface in front of it stays a normal hatch."""
    problem, layout = gt_hatch_line()
    (hatch,) = next(m for m in build_scene(problem, layout)["machines"] if m["id"] == "mb")[
        "hatches"
    ]
    assert (hatch["kind"], hatch["gtMid"], hatch["label"]) == ("OutputBus", 2710, "Output Bus (ME)")
    problem, layout = gt_hatch_line(normal=True)
    (hatch,) = next(m for m in build_scene(problem, layout)["machines"] if m["id"] == "mb")[
        "hatches"
    ]
    assert (hatch["gtMid"], hatch["label"]) == (None, "Output Bus")


def _hatched(
    recipe_map: str | None,
    *hatches: tuple[str, str | None],
    names: dict[str, str] | None = None,
    keys: tuple[str, ...] = ("label", "flow", "resource", "lock", "lockSlot", "layer", "spare"),
) -> list[dict[str, Any]]:
    """The scene's hatch entries for one multiblock with ``(kind, port id)`` hatches along x,
    each cut down to ``keys``.

    Its ports are the ones the hatches name (``{direction}:{resource}``, or ``power:in``), and
    ``names`` is the problem's ``resource_names`` (none by default)."""
    ports = [
        Port(
            id=port_id,
            commodity=Commodity.POWER if port_id.startswith("power") else Commodity.FLUID,
            direction=IODirection.OUTPUT if port_id.startswith("output") else IODirection.INPUT,
        )
        for _, port_id in hatches
        if port_id is not None
    ]
    reactor = Machine(
        id="r",
        type="Large Chemical Reactor",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=3, sy=3, sz=3),
        faces=FaceSpec(ports=ports),
        recipe_map=recipe_map,
    )
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[reactor],
        resource_names=names or {},
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("r", 0, 0, 0)],
        hatches=[
            PlacedHatch(
                machine_id="r",
                kind=kind,
                cell=CellCoord(x=x, y=0, z=0),
                facing=Facing.NORTH,
                port_id=port_id,
            )
            for x, (kind, port_id) in enumerate(hatches)
        ],
    )
    (scene_machine,) = build_scene(problem, layout)["machines"]
    return [{k: h[k] for k in keys} for h in scene_machine["hatches"]]


def test_scene_hatch_says_what_it_moves_and_what_it_must_be_locked_to() -> None:
    """Hovering a hatch names it and says what it moves (#120). A reactor making two fluids puts
    each in whichever output hatch takes it first, so those two also say the product each must be
    locked to and the slot GT sets it in; an input, an energy hatch and the maintenance hatch need
    nothing of the kind."""
    entries = _hatched(
        "gt.recipe.largechemicalreactor",
        ("InputHatch", "input:water"),
        ("OutputHatch", "output:nitricacid"),
        ("OutputHatch", "output:hydrogen"),
        ("Energy", "power:in"),
        ("Maintenance", None),
    )
    locked = {"lockSlot": "Locked Fluid slot", "layer": None, "spare": False}
    unlocked = {"lock": None, "lockSlot": None, "layer": None, "spare": False}
    assert entries == [
        {"label": "Input Hatch", "flow": "in", "resource": "water", **unlocked},
        {"label": "Output Hatch", "flow": "out", "resource": "nitricacid", "lock": "nitricacid"}
        | locked,
        {"label": "Output Hatch", "flow": "out", "resource": "hydrogen", "lock": "hydrogen"}
        | locked,
        {"label": "Energy Hatch", "flow": None, "resource": None, **unlocked},
        {"label": "Maintenance Hatch", "flow": None, "resource": None, **unlocked},
    ]


def test_scene_hatch_labels_what_it_moves_and_its_lock_like_every_other_surface() -> None:
    """A hatch's hover prints the resource it moves and the product it is locked to the way the
    rest of the preview does (#296): the plan's name, then the id that goes in the Locked Fluid
    slot. A resource the plan names nothing reads as its id, and an energy hatch moves none."""
    entries = _hatched(
        "gt.recipe.largechemicalreactor",
        ("OutputHatch", "output:nitricacid"),
        ("OutputHatch", "output:hydrogen"),
        ("Energy", "power:in"),
        names={"nitricacid": "Nitric Acid"},
        keys=("resourceLabel", "lockLabel"),
    )
    assert entries == [
        {"resourceLabel": "Nitric Acid (nitricacid)", "lockLabel": "Nitric Acid (nitricacid)"},
        {"resourceLabel": "hydrogen", "lockLabel": "hydrogen"},
        {"resourceLabel": None, "lockLabel": None},
    ]


def test_scene_tower_outputs_name_their_layer_and_a_spare_says_what_it_is() -> None:
    """A tower fills output i from layer i (#299): each output hatch carries the layer it stands
    on, so its hover can say to leave it unlocked, and a spare on a layer no product uses says it
    is one. Layers are GT's index here, from 0; the hover numbers them from 1."""
    tower = layered_tower(outputs=["benzene"], layers=2, inputs=["creosote"])
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[tower])

    def hatch(kind: str, cell: tuple[int, int, int], port: str | None) -> PlacedHatch:
        return PlacedHatch(
            machine_id=tower.id,
            kind=kind,
            cell=CellCoord(x=cell[0], y=cell[1], z=cell[2]),
            facing=Facing.WEST,
            port_id=port,
        )

    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at(tower.id, 2, 0, 2)],
        hatches=[
            hatch("InputHatch", (2, 0, 3), "input:creosote"),
            hatch("OutputHatch", (2, 1, 3), "output:benzene"),
            hatch("OutputHatch", (2, 2, 3), None),
            hatch("Maintenance", (2, 0, 4), None),
        ],
    )
    (scene_machine,) = build_scene(problem, layout)["machines"]
    assert [
        (h["resource"], h["lock"], h["layer"], h["spare"]) for h in scene_machine["hatches"]
    ] == [
        ("creosote", None, None, False),
        ("benzene", None, 0, False),
        (None, None, 1, True),
        (None, None, None, False),
    ]


def test_the_hatch_hover_numbers_a_towers_layers_and_names_a_spare() -> None:
    html = render_html(_sand_scene())
    assert "'spare for output layer ' + (h.layer + 1)" in html
    assert "'output layer ' + (h.layer + 1)" in html
    assert "filled by layer, leave unlocked" in html


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("OutputHatch", "Output Hatch"),
        ("InputBus", "Input Bus"),
        ("Energy", "Energy Hatch"),  # GT's kind drops the word the block's name has
        ("Maintenance", "Maintenance Hatch"),
        ("MultiAmpEnergy", "Multi Amp Energy Hatch"),
    ],
)
def test_a_hatch_kind_reads_as_the_block_is_called(kind: str, label: str) -> None:
    assert _hatch_label(kind) == label


def test_render_html_gives_every_hatch_block_a_hover_of_its_own() -> None:
    # The page side of #120: a hatch's cube owns its own hover (keyed by machine and cell, since a
    # hatch replaces one casing cell), and the tag says what the hatch is locked to.
    html = render_html(_sand_scene())
    assert "function hatchHover(what)" in html
    assert "if (what.hatch) return hatchHover(what);" in html
    assert "'locked to: '" in html


def test_scene_storage_says_what_it_holds_and_which_way_that_flows() -> None:
    """A Super Chest read "Super Chest" and nothing else (GitHub #155). What it buffers is on its
    ports - where ``adapter.core`` encoded it - and which way that flows is what tells two buffers
    of the SAME resource apart: one the builder keeps stocked, one a product collects in.
    """
    scene = _sand_scene()
    held = {
        (c["flow"], c["resource"])
        for m in scene["machines"]
        if m["role"] == "storage"
        for c in m["contents"]
    }
    assert held == {("in", "minecraft:stone"), ("out", "minecraft:sand")}
    # Only a boundary buffer holds anything: a machine's ports state its recipe, not a stock.
    assert all(m["contents"] == [] for m in scene["machines"] if m["role"] != "storage")
    # ...and ``flow`` means what the io panel means by the same two words, which is the INVERSE of
    # the port's own direction (a storage that OUTPUTS into the line is one the builder fills).
    # Asserted against the panel rather than against a literal so the two cannot drift apart.
    io = scene["io"]
    assert {f["resource"] for f in io["inputs"]} == {r for flow, r in held if flow == "in"}
    assert {f["resource"] for f in io["outputs"]} == {r for flow, r in held if flow == "out"}


def test_scene_storage_contents_skip_its_power_connection() -> None:
    """A storage's power hatch is not something it holds. No shipped line gives a Super Tank one (a
    buffer draws no EU), so the hand-built case is what keeps that filter honest."""
    tank = machine(
        "t",
        [
            Port(id="input:water", commodity=Commodity.FLUID, direction=IODirection.INPUT),
            Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT),
        ],
        type_="Super Tank",
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4), machines=[tank])
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=[at("t", 0, 0, 0)])
    (placed,) = build_scene(problem, layout)["machines"]
    # No resource names in a hand-built problem, so the label is the bare id.
    assert placed["contents"] == [
        {"resource": "water", "label": "water", "flow": "out", "me": False}
    ]


def test_scene_names_an_item_filter_and_what_it_lets_through() -> None:
    """An Item Filter the adapter placed to sort a merged run (#249) renders as a filter and its
    hover lists the items its slots must let through, which is what a builder sets in game. It is
    known by ``filter_items``, not by its type string, and every other machine lists nothing."""
    item_filter = Machine.model_validate(
        {
            **machine(
                "item-filter:w:gt.dust.stone",
                [
                    Port(id="input:x", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                    Port(id="output:x", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
                ],
                type_="Ultra Low Voltage Item Filter",
            ).model_dump(),
            "filter_items": ("gt.dust.stone",),
        }
    )
    washer = machine("w", [], type_="Ore Washer")
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[item_filter, washer],
        resource_names={"gt.dust.stone": "Stone Dust"},
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at(item_filter.id, 0, 0, 0), at("w", 2, 0, 0)],
    )
    scene = build_scene(problem, layout)
    by_id = {m["id"]: m for m in scene["machines"]}
    assert by_id[item_filter.id]["role"] == "filter"
    assert by_id[item_filter.id]["filter_items"] == ["gt.dust.stone"]
    # The hover lists them by label: the plan's name, with the id that goes in the slot (#296).
    assert by_id[item_filter.id]["filter_labels"] == ["Stone Dust (gt.dust.stone)"]
    assert by_id["w"]["role"] == "machine"
    assert by_id["w"]["filter_items"] == []
    assert by_id["w"]["filter_labels"] == []
    assert "'lets through: '" in render_html(scene)


def _two_machines_on_one_output_pipe(pack: str) -> dict[str, Any]:
    """The scene of two Macerators whose outputs share one pipe block into a Super Chest."""
    out = Port(id="output:x", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)
    machines = [machine("m1", [out], type_="Macerator"), machine("m2", [out], type_="Macerator")]
    chest = machine(
        "c",
        [Port(id="input:x", commodity=Commodity.ITEM, direction=IODirection.INPUT)],
        type_="Super Chest",
    )
    shared = Net(
        id="x",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="m1", port_id="output:x"),
            MachineFaceRef(machine_id="m2", port_id="output:x"),
            MachineFaceRef(machine_id="c", port_id="input:x"),
        ],
    )
    cell = CellCoord(x=1, y=0, z=0)
    pipe = Route(
        net_id="x",
        commodity=Commodity.ITEM,
        terminals=[
            Terminal(machine_id="m1", port_id="output:x", face=Facing.EAST, cell=cell),
            Terminal(machine_id="m2", port_id="output:x", face=Facing.WEST, cell=cell),
            Terminal(machine_id="c", port_id="input:x", face=Facing.NORTH, cell=cell),
        ],
        segments=[],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[*machines, chest],
        nets=[shared],
        pack_version=pack,
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("m1", 0, 0, 0), at("m2", 2, 0, 0), at("c", 1, 0, 1)],
        routes=[pipe],
    )
    return build_scene(problem, layout)


def test_the_scene_marks_a_machine_that_must_refuse_input_through_its_output_face() -> None:
    """Two machines' outputs on one pipe: on 2.9 each takes the other's output through its output
    face unless set not to, so the scene flags both for the red arrow and the hover (#278); a 2.8.4
    plan, whose machines already refuse it, flags neither."""
    flagged = {
        m["id"]: m["outputs"]["forbidInput"]
        for m in _two_machines_on_one_output_pipe("2.9.0-beta-2")["machines"]
        if m["outputs"]
    }
    assert flagged == {"m1": True, "m2": True}
    on_28 = _two_machines_on_one_output_pipe("2.8.4")["machines"]
    assert not any(m["outputs"]["forbidInput"] for m in on_28 if m["outputs"])
    page = render_html(_two_machines_on_one_output_pipe("2.9.0-beta-2"))
    assert "forbidById[id] ? FORBID_LINES : []" in page  # the machine's hover, and its red arrow's
    assert "'output face: set Input from Output Side forbidden'" in page
    assert "'(screwdriver it, not sneaking, until chat says so)'" in page


def test_the_viewer_prints_each_resource_by_its_label() -> None:
    """Every surface that names a resource prints the scene's ``label`` (#296), not the bare id: a
    route's tag and the nets panel, a storage's contents, an Item Filter's slots, a cover's tag,
    a hatch's hover (what it moves and what it is locked to) and the system i/o panel. The template is the untested last mile, so this pins that it reads the
    field the scene tests check rather than the raw ``resource`` beside it."""
    scene = _sand_scene()
    held = {c["label"] for m in scene["machines"] for c in m["contents"]}
    assert held == {"Stone (minecraft:stone)", "Sand (minecraft:sand)"}
    page = render_html(scene)
    for reads in (
        "resourceParts(r.resources)",  # a route's tag and its nets-panel row...
        "r.label || r.commodity",  # ...and a power route's, which names no resource
        "c.flow + ': ', { icon: c.resource }, c.label",  # a storage's contents
        "m.filter_labels",  # an Item Filter's slots
        "[r.netId, r.resources || []]",  # what a cover lets out
        "resourceParts(f.resources)",  # the system i/o panel
        "flowRow('in', i)",
        "flowRow('out', o)",
        "h.flow + ': ', { icon: h.resource }, h.resourceLabel",  # a hatch's hover
        "'locked to: ', { icon: h.lock }, h.lockLabel",
    ):
        assert reads in page, reads


def test_the_nitrobenzene_super_tanks_are_individually_identifiable(
    solved_nitrobenzene: tuple[InputIR, LayoutResult],
) -> None:
    """The case GitHub #155 was filed on: which Super Tank holds the toluene was not answerable
    from the preview at all, since every one of them rendered as "Super Tank".

    Two of this line's tanks hold water - one the builder fills, one the line fills - so the
    resource alone would still leave that pair identical; the flow is what separates them.
    """
    problem, layout = solved_nitrobenzene
    scene = build_scene(problem, layout)
    tanks = [m for m in scene["machines"] if m["type"] == "Super Tank"]
    assert len(tanks) >= 4
    tags = [tuple((c["flow"], c["resource"]) for c in m["contents"]) for m in tanks]
    assert len(set(tags)) == len(tags), f"two tanks hover identically: {tags}"
    assert (("out", "liquid_toluene"),) in tags
    assert sorted(t for t in tags if t[0][1] == "water") == [
        (("in", "water"),),
        (("out", "water"),),
    ]

    # Resource ids as the IR carries them, metas and all: a builder can paste one into NEI, where a
    # display name invented here would be a guess (#155, and route_blocks on GT material names).
    chests = {
        c["resource"]
        for m in scene["machines"]
        if m["type"] == "Super Chest"
        for c in m["contents"]
    }
    assert {"gregtech:gt.metaitem.01@2022", "minecraft:log@32767"} <= chests


def test_scene_routes_carry_the_distinct_net_id_the_solo_filter_keys_on(
    solved_nitrobenzene: tuple[InputIR, LayoutResult],
) -> None:
    # The legend's per-net solo (GitHub #240) keys on netId, and it has to: a power route names no
    # resource at all (`resource` is None), and nothing in a plan says two nets cannot carry the
    # same fluid. Distinctness is the half that matters for the filter - if two routes shared an
    # id, the row for one of them would light up the other's pipes too, which is exactly the
    # confusion the feature exists to end. This line has six fluid nets and three power nets.
    problem, layout = solved_nitrobenzene
    ids = [r["netId"] for r in build_scene(problem, layout)["routes"]]
    assert len(ids) > 1
    assert all(ids), "a route with no net id cannot be soloed"
    assert len(set(ids)) == len(ids), f"two routes share a net id: {ids}"


def test_render_html_wires_the_per_net_solo_rows() -> None:
    # The legend's net rows (GitHub #240): each hides every other run so one reads end to end. The
    # rows are built at runtime out of scene.routes, so what the page itself can be asserted on is
    # that the section, the row that clears a solo, and the lit state all ship - one coarse marker
    # each, not the JS that filters the meshes, which is eye-validated (GitHub #94).
    html = render_html(_sand_scene())
    assert "'nets'" in html  # the legend section heading
    assert "show all nets" in html  # the row that clears an active solo
    assert ".net.on" in html  # the soloed row's lit style


def test_render_html_makes_every_legend_section_a_fold() -> None:
    # Each legend section folds under its heading. It is a native <details>, so the page needs no
    # handler of its own to open or close one, and a screen reader announces it as expandable. The
    # sections are built at runtime, so what ships is the element, its heading and the style they
    # share: one coarse marker each (GitHub #94). Remembering a folded section across the legend's
    # rebuilds is JS no test executes, and is eye-validated.
    html = render_html(_sand_scene())
    assert "el('details')" in html  # a section is a disclosure...
    assert "el('summary'" in html  # ...whose heading is what folds it
    assert ".sec > summary" in html  # the heading's style
    for heading in ("'machines'", "'routes'", "'nets'", "'materials'", "'system i/o'"):
        assert heading in html, f"legend section {heading} is gone"


def test_render_html_draws_merged_meshes_and_only_on_change() -> None:
    # What keeps a large preview smooth: blocks and pipes merged into a few meshes instead of a mesh
    # (and a draw call per face) each, the faces the scene says another block hides left out, and a
    # frame drawn only when something changed. ev-nitrobenzene went from 19,193 draw calls a frame to
    # 680. The merging runs in the browser, so one coarse marker each (GitHub #94); what `cover`
    # says is tested in Python, and the rest was compared against the old page in a browser.
    html = render_html(_sand_scene())
    assert "b.cover" in html  # the viewer reads which faces to skip...
    assert "new Batch()" in html  # ...merges what is left...
    assert "requestRender" in html  # ...and draws a frame only when asked to


def test_scene_is_deterministic() -> None:
    assert _sand_scene() == _sand_scene()


def test_render_html_ships_the_page_shell_and_its_stable_controls() -> None:
    # The page's addressable surface: the doctype, and the element ids anything driving the viewer
    # (a future headless test, a user script) has to target. Renaming one is a real break, which is
    # why these are asserted and the JS behind them is not - that part is eye-validated, and grepping
    # its identifiers only pins the current spelling of code no test executes (GitHub #94).
    html = render_html(_sand_scene())
    assert html.startswith("<!doctype html>")
    assert 'id="layer"' in html  # the layer-by-layer slider...
    assert 'type="range"' in html  # ...is a range input
    assert 'id="nametag"' in html  # the floating name tag a hovered block writes into
    assert 'id="status"' in html  # the HUD headline the viewer writes the solve summary into
    # The headline is markup in one place and a write target in another, and the markup ships the
    # placeholder: rename the span alone and the page loads saying 'loading...' forever, with
    # nothing else to notice. So assert the writer actually addresses the element that exists.
    assert "getElementById('status')" in html


def test_render_html_folds_three_panels_each_wired_to_one_that_exists() -> None:
    # What makes the page fit a phone (GitHub #237): the legend drawer, the gesture hint, and the
    # four view toggles above the layer slider all fold away. The buttons are the addressable
    # surface; the invariant worth pinning beside them is that each one's aria-controls names a
    # panel that actually exists - point it at the wrong id and it still LOOKS right on screen, it
    # just announces nothing and no test would notice.
    html = render_html(_sand_scene())
    folds = {
        button: re.search(r'aria-controls="(\w+)"', attrs)
        for button, attrs in re.findall(r'<button id="(\w+)"([^>]*)>', html)
        if "aria-controls" in attrs
    }
    assert set(folds) == {"legendToggle", "hintToggle", "moreToggle"}
    for button, panel in folds.items():
        assert panel is not None, f"#{button} controls nothing"
        assert f'id="{panel.group(1)}"' in html


def test_render_html_adapts_to_narrow_viewports_and_coarse_pointers() -> None:
    # The responsive contract, as two coarse markers rather than pinned rule text: a width query
    # (the controls bar spans the screen instead of running off it) and a pointer query (44px
    # targets for a thumb). Drop either and the page silently reverts to desktop-only, which no
    # other test would catch - the panels all still render, just off the edge of a phone.
    html = render_html(_sand_scene())
    assert "viewport-fit=cover" in html  # ...which is what makes the safe-area insets apply
    assert "@media (max-width:" in html
    assert "@media (pointer: coarse)" in html


def test_render_html_states_the_touch_gestures_without_promising_a_mouse() -> None:
    # The hint is written twice: the markup carries the mouse wording, and the viewer replaces it on
    # a coarse pointer. The invariant is that the touch wording does not advertise gestures a phone
    # does not have - 'right-drag to pan' is unreachable advice, and 'hover' is the one interaction
    # touch lacks entirely, which is why the tap picker exists at all (#237).
    html = render_html(_sand_scene())
    mouse = re.search(r'<div id="hint">(.*?)</div>', html)
    touch = re.search(r"getElementById\('hint'\)\.textContent =\s*'(.*?)';", html)
    assert mouse is not None, "the page ships no gesture hint"
    assert touch is not None, "no touch wording replaces the mouse hint"
    assert "hover" in mouse.group(1)
    assert "drag" in mouse.group(1)
    assert "tap" in touch.group(1)
    assert "hover" not in touch.group(1)
    assert "right-drag" not in touch.group(1)


def test_render_html_labels_the_auto_output_toggle_identically_before_and_after_a_click() -> None:
    # The label is written twice, the same way the sibling rate/state toggles do it: once in the
    # markup for the initial render, once in the click handler that rewrites it. Change one and the
    # button silently renames itself the first time it is pressed, which no other test would catch.
    # The invariant is that the two AGREE, so assert them against each other rather than against a
    # pinned literal - renaming the button then stays a one-line change (GitHub #94).
    html = render_html(_sand_scene())
    markup = re.search(r'<button id="arrowToggle"[^>]*>(.*?)</button>', html)
    handler = re.search(r"arrowToggle\.textContent = '(.*?)'", html)
    assert markup is not None, "the page ships no #arrowToggle button"
    assert handler is not None, "no click handler restates the #arrowToggle label"
    # The handler appends the state word; the markup carries the label with the on-load state baked
    # in, so the page must load saying exactly what a click would restate for that same state.
    assert markup.group(1) == f"{handler.group(1)}on"


def test_scene_still_carries_a_multiblock_auto_connection_it_draws_no_arrow_for() -> None:
    # The other half of #153: the arrow goes, the CONNECTION stays. It is a real connection - the
    # layout records it and the validator re-checks it - so the fix belongs in the renderer, not
    # in the scene contract. Filtering these out of `autoConnections` would silently drop them from
    # every other consumer (and from the boundary-storage glyph orientation in `textures`).
    source = producer("mb").model_copy(update={"footprint": CellBox(sx=3, sy=3, sz=3)})
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=4, sz=6),
        machines=[source, consumer("c")],
        nets=[net("n0", "mb", "c")],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("mb", 0, 0, 0), at("c", 3, 0, 0)],
        auto_connections=[
            AutoConnection(
                net_id="n0",
                source_machine_id="mb",
                source_face=Facing.EAST,
                target_machine_id="c",
                target_face=Facing.WEST,
            )
        ],
    )
    scene = build_scene(problem, layout)
    (auto,) = scene["autoConnections"]
    assert (auto["source"], auto["sourceFace"]) == ("mb", "east")
    size = next(m["size"] for m in scene["machines"] if m["id"] == "mb")
    assert size[0] * size[1] * size[2] > 1  # ...and it is the multi-cell source the viewer skips
    # A multiblock ejects from a hatch, so it carries no output reading to draw from (#249).
    assert all(m["outputs"] is None for m in scene["machines"])


def test_scene_gives_every_sand_block_its_auto_face_or_its_covers() -> None:
    # The maintainer's rule (#249): every single block shows how its outputs leave it. A hammer
    # auto-outputs through one face; the source Super Chest cannot auto-output at all, so its one
    # output face is a conveyor cover; the sink chest and the power source have no output face.
    scene = _sand_scene()
    for m in scene["machines"]:
        out = m["outputs"]
        if m["type"] == "Forge Hammer":
            assert out["autoFace"] is not None
            assert out["covers"] == []
        elif m["role"] == "storage" and out is not None:
            assert out["autoFace"] is None
            assert [c["cover"] for c in out["covers"]] == ["conveyor"]
        else:
            assert out is None, m["id"]


def test_scene_marks_a_piped_single_block_output_as_its_auto_face() -> None:
    # No AutoConnection: the hammer pipes its output. It still auto-outputs into that pipe, so the
    # viewer draws the same arrow, which is what tells a builder no conveyor cover is meant.
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=2, sz=4),
        machines=[producer("p"), consumer("c")],
        nets=[net("n0", "p", "c")],
    )
    cell = CellCoord(x=1, y=0, z=0)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("p", 0, 0, 0), at("c", 2, 0, 0)],
        routes=[
            Route(
                net_id="n0",
                commodity=Commodity.ITEM,
                terminals=[
                    Terminal(machine_id="p", port_id="out", face=Facing.EAST, cell=cell),
                    Terminal(machine_id="c", port_id="in", face=Facing.WEST, cell=cell),
                ],
                segments=[],
            )
        ],
    )
    by_id = {m["id"]: m for m in build_scene(problem, layout)["machines"]}
    assert by_id["p"]["outputs"] == {
        "autoFace": "east",
        "autoItems": True,
        "autoFluids": False,
        "covers": [],
        "forbidInput": False,  # alone on its pipe, so nothing else's output can come back in (#278)
    }
    assert by_id["c"]["outputs"] is None


def test_render_html_draws_arrows_from_each_blocks_auto_face_and_cover_markers() -> None:
    page = render_html(_sand_scene())
    assert "for (const m of SCENE.machines)" in page
    assert "out.autoFace" in page
    assert "function coverMark(kind)" in page
    assert "'cover (conveyor / pump)'" in page
    assert "coverHover(" in page
    # The arrows no longer come from machine-to-machine links alone: a piped output shows one too.
    assert "for (const ac of SCENE.autoConnections)" not in page


def test_scene_route_segments_and_terminals_drive_node_and_arm_drawing() -> None:
    # Routes are drawn GT-style: a cube at each cell centre with a uniform arm out per connection -
    # an adjacent route cell or a docked machine face (GitHub #31). That rendering is a JS detail,
    # eye-validated; the versioned contract is the scene data it consumes, so assert on that. Every
    # segment carries its two endpoint cells one step apart (the unit an arm spans), and every
    # terminal its docked face + cell (the machine lead each arm points at).
    power = next(r for r in _sand_scene()["routes"] if r["commodity"] == "power")
    assert power["segments"]
    for seg in power["segments"]:
        assert len(seg["from"]) == 3
        assert len(seg["to"]) == 3
        assert sum(abs(seg["from"][i] - seg["to"][i]) for i in range(3)) == 1  # adjacent cells
    assert power["terminals"]
    for term in power["terminals"]:
        assert term["face"]
        assert len(term["cell"]) == 3


def test_scene_auto_connections_carry_source_and_ejecting_face() -> None:
    # The per-face auto-output arrows (GitHub #20) are driven by the scene's autoConnections: each
    # names the ejecting source machine + face, which the viewer turns into an arrow on every
    # perpendicular face. Assert the contract the arrows read, not the JS that positions them.
    faces = {"north", "south", "east", "west", "up", "down"}
    autos = _sand_scene()["autoConnections"]
    assert autos
    for ac in autos:
        assert ac["source"]
        assert ac["sourceFace"] in faces
        assert ac["target"]
        assert ac["targetFace"] in faces


def test_render_html_shows_system_io_panel_with_rate_toggle() -> None:
    # The boundary summary the HUD renders (GitHub #5). Its data (inputs/outputs/power) is the scene
    # contract asserted in test_scene_reports_system_io; here just confirm the panel and its per-tick
    # / per-second toggle are wired into the page - one coarse marker each, not the JS internals.
    html = render_html(_sand_scene())
    assert "system i/o" in html  # the boundary panel label
    assert 'id="rateUnit"' in html  # the per-tick / per-second toggle button


def test_render_html_wires_the_active_idle_state_toggle() -> None:
    # The idle/running skin toggle: builders can switch every machine between its at-rest and running
    # texture where the two differ. Assert the stable control id is wired into the page (one coarse
    # marker), not the JS that swaps the atlas - the running tiles ride scene.atlas.active.
    assert 'id="stateToggle"' in render_html(_sand_scene())


def test_render_html_inlines_the_exact_scene() -> None:
    scene = _sand_scene()
    assert json.dumps(scene) in render_html(scene)  # embedded verbatim - no file:// fetch needed


def _inlined_scene_json(html: str) -> str:
    # Pull the inlined scene JSON payload back out of the rendered page, located by the stable
    # ``const SCENE = <json>;`` assignment. ``json.dumps`` emits no raw newline, so the payload is a
    # single line that ends at the statement's semicolon - independent of whatever JS follows it (so
    # the viewer is free to derive its legend from the scene rather than a pinned ``const`` line).
    line = next(ln for ln in html.splitlines() if ln.startswith("const SCENE = "))
    return line[len("const SCENE = ") :].rstrip().removesuffix(";")


def test_render_html_escapes_closing_script_in_inline_json() -> None:
    # Plan JSON is external input (GitHub #39): a machine type or resource id containing "</script>"
    # must not be able to close the inline <script> and break (or inject into) the page.
    scene = _sand_scene()
    scene["machines"][0]["type"] = "</script><script>alert(1)</script>"
    payload = _inlined_scene_json(render_html(scene))
    assert "</script>" not in payload  # the raw closing tag never reaches the page as data...
    assert "<\\/script>" in payload  # ...it is escaped to <\/script>
    assert json.loads(payload) == scene  # ...and json still round-trips (\/ is a valid escape)


#: The classic XSS probe, the one the issue that asked for this reproduction used (GitHub #111).
#: No quotes in it, so ``json.dumps`` embeds it verbatim and the "only as data" assertions below
#: can match the exact string.
_XSS = "<img src=x onerror=alert(1)>"

#: Sinks that turn a string into live markup. None of them may appear in the emitted page.
_HTML_SINKS = (
    r"innerHTML\s*=",
    r"outerHTML\s*=",
    r"insertAdjacentHTML",
    r"document\.write\(",
    r"srcdoc",
)


def _xss_scene() -> dict[str, Any]:
    """The sand scene with a script payload in every plan-derived string the panel prints."""
    scene = _sand_scene()
    scene["legend"][0]["label"] = _XSS  # machine type -> the legend rows
    scene["io"]["inputs"][0]["resource"] = _XSS  # resource id -> the system-i/o rows
    scene["io"]["outputs"][0]["resource"] = _XSS
    # ...and one by one, with the picture maps keyed by it (#297): the row's text, its icon's
    # lookup and its colour's lookup. The icon URI and the colour are write_preview's and the IR's.
    scene["io"]["inputs"][0]["resources"] = [{"id": _XSS, "label": _XSS}]
    scene["icons"] = {_XSS: "data:image/png;base64,iVBORw0KGgo="}
    scene["resourceColors"] = {_XSS: "#123456"}
    return scene


def _inline_blocks(html: str) -> dict[str, str]:
    """The page's three inline blocks, exactly as the browser reads them (for the CSP hashes).

    Non-greedy to the first closing tag is safe precisely because of the ``</`` escape (GitHub
    #39): no plan string can put a literal ``</script>`` inside the payload.
    """
    return {
        name: re.search(pattern, html, re.S).group(1)  # type: ignore[union-attr]
        for name, pattern in (
            ("style", r"<style>(.*?)</style>"),
            ("importmap", r'<script type="importmap">(.*?)</script>'),
            ("module", r'<script type="module">(.*?)</script>'),
        )
    }


def _csp_of(html: str) -> str:
    match = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', html)
    assert match is not None, "the page ships no Content-Security-Policy"
    return match.group(1)


def _sha256_source(text: str) -> str:
    return f"'sha256-{base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()}'"


def test_render_html_builds_the_legend_without_an_html_sink() -> None:
    # GitHub #111: the legend and system-i/o rows used to be a concatenated HTML string assigned to
    # innerHTML, so a machine type of "<img src=x onerror=...>" ran on open. The panel is DOM nodes
    # now - assert the page carries no markup sink at all, which is the property that keeps it so.
    html = render_html(_xss_scene())
    for sink in _HTML_SINKS:
        assert re.search(sink, html) is None, f"plan text can reach {sink}"


def test_render_html_keeps_a_plan_payload_out_of_the_pages_markup() -> None:
    # The payload may appear in the page exactly once: as a JSON string inside the scene the viewer
    # reads at runtime. Anywhere else it would be markup (or JS) the browser executes.
    html = render_html(_xss_scene())
    payload = _inlined_scene_json(html)
    assert html.count(_XSS) == 7  # the seven strings seeded above...
    assert payload.count(_XSS) == 7  # ...all of them inside the inlined JSON, none outside it
    assert json.loads(payload)["legend"][0]["label"] == _XSS  # and it survives as the data it is


def test_render_html_csp_admits_its_own_blocks_and_the_three_js_origin() -> None:
    # Defence in depth, and the one part of it that can silently break the previewer instead of
    # hardening it: a policy whose hashes do not match the emitted blocks blocks the viewer, and one
    # that omits the CDN origin blocks three.js. Recompute both from the page the browser gets.
    html = render_html(_xss_scene())
    csp, blocks = _csp_of(html), _inline_blocks(html)
    script_src = next(d for d in csp.split("; ") if d.startswith("script-src "))
    style_src = next(d for d in csp.split("; ") if d.startswith("style-src "))
    assert _sha256_source(blocks["importmap"]) in script_src
    assert _sha256_source(blocks["module"]) in script_src  # per-scene: the payload is inside it
    assert _sha256_source(blocks["style"]) in style_src
    assert "https://unpkg.com" in script_src  # ...or three.js never loads and the page is blank
    assert "default-src 'none'" in csp
    assert "img-src data:" in csp  # the baked GT textures
    assert "'unsafe-inline'" not in csp  # would hand an injected handler back the run of the page
    assert "'unsafe-eval'" not in csp


def test_write_preview_writes_an_html_file(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult]
) -> None:
    ir, layout = solved_sand
    out = write_preview(ir, layout, tmp_path / "view.html")
    assert out.exists()
    assert "gtnh-solve preview" in out.read_text(encoding="utf-8")


def test_write_preview_makes_its_parent(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult]
) -> None:
    # As write_schematic already did. The write is the preview's last step, after the scene is
    # built and every texture baked, so a missing out/ used to discard all of that with a raw
    # FileNotFoundError (#150). Two levels, so a single mkdir without parents=True would fail.
    ir, layout = solved_sand
    out = write_preview(ir, layout, tmp_path / "out" / "nested" / "view.html", textures=False)
    assert out.is_file()
    assert "gtnh-solve preview" in out.read_text(encoding="utf-8")


def _written_scene(path: Path) -> dict[str, Any]:
    scene: dict[str, Any] = json.loads(_inlined_scene_json(path.read_text(encoding="utf-8")))
    return scene


def test_write_preview_ships_the_atlas_not_the_pool(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The page draws every face from one packed image (previewer.atlas), so the per-face pool the
    # texture pass writes must not ship beside it: that would put every texture on the page twice.
    # The pass itself is stood in for, since what is under test is what write_preview does after it.
    def texturized(scene: dict[str, Any], **_: Any) -> None:
        machine = scene["machines"][0]
        machine["expanded"] = True
        scene["blocks"] = [
            {"cell": machine["cell"], "machine": machine["id"], "texture": [None] * 6}
        ]
        scene["textures"] = {}
        scene["texturesActive"] = {}

    monkeypatch.setattr(previewer_package, "texturize_scene", texturized)
    ir, layout = solved_sand
    scene = _written_scene(write_preview(ir, layout, tmp_path / "view.html"))
    assert "textures" not in scene
    assert "texturesActive" not in scene
    assert scene["atlas"]["missing"], "a block with no baked face still draws the checkerboard"


def test_a_texture_pass_that_fails_part_way_leaves_plain_boxes(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pass flags a machine expanded before it stores that machine's blocks, so a failure in
    # between used to leave a machine that skipped its box and had no blocks to draw instead: an
    # invisible machine. The fallback takes every trace of the pass back out.
    def half_done(scene: dict[str, Any], **_: Any) -> None:
        scene["machines"][0]["expanded"] = True
        scene["legend"][0]["tile"] = "gregtech:gt.blockmachines|1|NORTH|inactive"
        raise RuntimeError("the jar went away")

    monkeypatch.setattr(previewer_package, "texturize_scene", half_done)
    ir, layout = solved_sand
    scene = _written_scene(write_preview(ir, layout, tmp_path / "view.html"))
    assert not any(m.get("expanded") for m in scene["machines"])
    assert scene["blocks"] == []
    assert scene["atlas"] is None
    assert all(cell.get("tex") is None for r in scene["routes"] for cell in r["cells"])
    # ...and the legend goes back to colour swatches, naming no tile of an atlas that is not there.
    assert not any("tile" in entry for entry in scene["legend"])


def test_the_legend_marks_a_machine_type_by_its_front_face() -> None:
    """The template half of a legend tile, which no test can run: a type with a tile the atlas has
    is drawn as that tile cropped out of the atlas image at 16 px (a CSSOM background, so no new
    markup or image source), and any other type keeps its colour swatch."""
    page = render_html(_sand_scene())
    for reads in (
        "for (const e of SCENE.legend) row(machines, machineMark(e), e.label)",
        "const tile = entry.tile && ATLAS ? ATLAS.tiles[entry.tile] : null",
        "plain.classList.add('mach')",  # a placeholder type keeps its colour, at the same size
        "face.style.backgroundImage = 'url(\"' + ATLAS.image + '\")'",
        ".sw.face, .sw.mach { width: 16px; height: 16px; }",
    ):
        assert reads in page, reads


def test_scene_route_of_a_merged_run_names_every_item_it_carries() -> None:
    """A merged item run (#249) carries several items down one pipe to the filters that sort them,
    so hovering it lists them all rather than naming nothing (its ``fluid_or_item`` is empty)."""
    ports = [
        Port(id="output:items", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
        Port(id="input:items", commodity=Commodity.ITEM, direction=IODirection.INPUT),
    ]
    washer = Machine(
        id="w",
        type="Ore Washer",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=ports),
    )
    trunk = Net(
        id="item-trunk:w",
        commodity=Commodity.ITEM,
        items=("gt.crushed.iron", "gt.dust.stone"),
        throughput=0.2,
        endpoints=[
            MachineFaceRef(machine_id="w", port_id="output:items"),
            MachineFaceRef(machine_id="w", port_id="input:items"),
        ],
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4), machines=[washer], nets=[trunk])
    route = Route(
        net_id=trunk.id,
        commodity=Commodity.ITEM,
        segments=[Segment(start=CellCoord(x=0, y=0, z=0), end=CellCoord(x=1, y=0, z=0), channel=0)],
    )
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, routes=[route])
    (scene_route,) = build_scene(problem, layout)["routes"]
    assert scene_route["resource"] == "gt.crushed.iron, gt.dust.stone"
    assert scene_route["rate"] == pytest.approx(0.2)
    # One by one as well, for the picture beside each (#297).
    assert [r["id"] for r in scene_route["resources"]] == ["gt.crushed.iron", "gt.dust.stone"]


def _xylene_line(names: dict[str, str] | None = None) -> tuple[InputIR, LayoutResult]:
    """A Super Tank feeding a fluid whose id holds a comma, ``1,3dimethylbenzene``, to a mixer."""
    tank = Machine(
        id="tank",
        type="Super Tank",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(
                    id="output:1,3dimethylbenzene",
                    commodity=Commodity.FLUID,
                    direction=IODirection.OUTPUT,
                )
            ]
        ),
    )
    mixer = Machine(
        id="mixer",
        type="Mixer",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.FLUID, direction=IODirection.INPUT)]
        ),
    )
    xylene = Net(
        id="xylene",
        commodity=Commodity.FLUID,
        fluid_or_item="1,3dimethylbenzene",
        throughput=5.0,
        endpoints=[
            MachineFaceRef(machine_id="tank", port_id="output:1,3dimethylbenzene"),
            MachineFaceRef(machine_id="mixer", port_id="in"),
        ],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=2, sz=4),
        machines=[tank, mixer],
        nets=[xylene],
        resource_names=names or {},
        resource_colors={"1,3dimethylbenzene": "#5B773C"},
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("tank", 0, 0, 0), at("mixer", 3, 0, 0)],
        routes=[
            Route(
                net_id="xylene",
                commodity=Commodity.FLUID,
                segments=[
                    Segment(start=CellCoord(x=1, y=0, z=0), end=CellCoord(x=2, y=0, z=0), channel=0)
                ],
            )
        ],
    )
    return problem, layout


def test_a_fluid_id_with_a_comma_stays_one_resource_on_every_surface() -> None:
    """The scene names resources one by one (#297) because a joined label cannot be split back:
    ``1,3dimethylbenzene`` is one fluid, not ``1`` and ``3dimethylbenzene``."""
    scene = build_scene(*_xylene_line({"1,3dimethylbenzene": "1,3-Dimethylbenzene"}))
    expected = [{"id": "1,3dimethylbenzene", "label": "1,3-Dimethylbenzene (1,3dimethylbenzene)"}]
    (route,) = scene["routes"]
    assert route["resources"] == expected
    (feed,) = scene["io"]["inputs"]
    assert feed["resources"] == expected
    assert feed["label"] == expected[0]["label"]


def test_an_io_label_is_built_from_each_resources_own_label() -> None:
    # The io label used to be resource_label() of the joined "a, b" string, which no name table
    # keys, so a merged run's panel row showed bare ids even where the plan named every item.
    scene = _sand_scene()
    for flow in [*scene["io"]["inputs"], *scene["io"]["outputs"]]:
        assert flow["label"] == ", ".join(r["label"] for r in flow["resources"])
    assert {r["label"] for f in scene["io"]["inputs"] for r in f["resources"]} == {
        "Stone (minecraft:stone)"
    }


def test_the_plans_name_beats_an_extra_name_which_beats_the_bare_id() -> None:
    """``extra_names`` (an icon index's, ``previewer.icons``) fills in only what the plan leaves
    unnamed: the plan's own name wins, then the export's, then the id."""
    named = build_scene(
        *_xylene_line({"1,3dimethylbenzene": "From Plan"}),
        extra_names={"1,3dimethylbenzene": "From Export"},
    )
    assert named["routes"][0]["label"] == "From Plan (1,3dimethylbenzene)"
    unnamed = build_scene(*_xylene_line(), extra_names={"1,3dimethylbenzene": "From Export"})
    assert unnamed["routes"][0]["label"] == "From Export (1,3dimethylbenzene)"
    assert unnamed["io"]["inputs"][0]["label"] == "From Export (1,3dimethylbenzene)"
    bare = build_scene(*_xylene_line())
    assert bare["routes"][0]["label"] == "1,3dimethylbenzene"


def test_the_scene_carries_the_plans_colours_and_leaves_icons_to_write_preview() -> None:
    scene = build_scene(*_xylene_line())
    assert scene["resourceColors"] == {"1,3dimethylbenzene": "#5b773c"}  # lowercased by the IR
    assert scene["icons"] == {}  # build_scene stays pure; write_preview fills it
    sand = _sand_scene()
    assert set(sand["resourceColors"]) == {
        "minecraft:cobblestone",
        "minecraft:gravel",
        "minecraft:sand",
        "minecraft:stone",
    }


def test_the_viewer_draws_a_picture_beside_each_resource() -> None:
    """The template half of #297, which no test can run: each resource's icon or colour dot is
    looked up in a Map (a plain object answers ``constructor``), drawn as an ``<img>`` the CSP's
    ``img-src data:`` admits, and a hover tag's lines may carry pictures, so its change check
    compares the lines themselves rather than their joined text."""
    page = render_html(_sand_scene())
    for reads in (
        "new Map(Object.entries(SCENE.icons || {}))",
        "new Map(Object.entries(SCENE.resourceColors || {}))",
        "img.className = 'ico'",
        "img.alt = ''",
        "dot.classList.add('dot')",
        "JSON.stringify(lines)",
        ".ico { width: 16px; height: 16px;",
        "image-rendering: pixelated",
        ".sw.dot",
    ):
        assert reads in page, reads
    assert "img-src data:" in _csp_of(page)


def test_a_hover_tag_shows_each_icon_above_its_name_at_full_size() -> None:
    """On a hover tag the picture is what a builder looks for, so every icon a line names is lifted
    out of the text into a row above it, drawn at the 64 px the export rendered (1:1, so nothing is
    resampled); a line without an icon keeps its inline colour dot. The panels keep 16 px icons."""
    page = render_html(_sand_scene())
    for reads in (
        "function tagLine(line)",
        "nametag.replaceChildren(...lines.map(tagLine))",
        "row.className = 'icons'",
        "#nametag .icons .ico { width: 64px; height: 64px;",
        ".ico { width: 16px; height: 16px;",  # the panels' size is unchanged
    ):
        assert reads in page, reads
