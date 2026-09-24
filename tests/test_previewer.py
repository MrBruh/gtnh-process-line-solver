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

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    METoggles,
    Port,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.previewer import build_scene, render_html, write_preview
from gtnh_solver.solver import solve
from tests._helpers import at, consumer, machine, net, producer

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _sand_scene(me_toggles: METoggles | None = None) -> dict[str, Any]:
    # The fast (constructive) solve: deterministic layout coordinates that the exact-cell
    # assertions below can rely on; scene building does not care which placer produced them.
    ir = adapt_file(_SAND, me_toggles=me_toggles)
    return build_scene(ir, solve(ir, optimize=False))


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
    identical blue noodles. Nitrobenzene is the fixture because it is the only shipped line that
    lays actual pipes; sand's item chain all auto-outputs, leaving power alone.
    """
    problem, layout = solved_nitrobenzene
    scene = build_scene(problem, layout)
    resource_of = {n.id: n.fluid_or_item for n in problem.nets}
    rate_of = {n.id: n.throughput for n in problem.nets}

    fluids = [r for r in scene["routes"] if r["commodity"] == "fluid"]
    assert fluids, "the nitrobenzene line pipes fluids; that is what it is the fixture for"
    for route in fluids:
        # Verbatim, exactly the id the plan carries - no display name invented here (#155).
        assert route["resource"] == resource_of[route["netId"]]
        assert route["rate"] == pytest.approx(rate_of[route["netId"]])
        assert route["unit"] == "mB"  # stem only; the viewer appends /t or /s, as it does for io

    # A power net names no fluid or item at all, so its route says so instead of inventing one: the
    # commodity is the whole answer there, and the rate is the EU/t the net moves.
    power = next(r for r in scene["routes"] if r["commodity"] == "power")
    assert power["resource"] is None
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
    assert io["inputs"][0]["rate"] == pytest.approx(0.1)
    assert io["inputs"][0]["unit"] == "items"  # stem only; the viewer appends /t or /s
    assert io["outputs"] == [
        {"resource": "minecraft:sand", "rate": pytest.approx(0.1), "unit": "items", "me": False}
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
    """With items on ME (``--me items``) the sand line routes no item at all: no pipe, no
    auto-output arrow, and no ME block drawn in their place yet (#222). Its two Super Chests would
    then sit unconnected and read as a line that forgot its pipes, so every item flow at the
    boundary, and what each chest holds, is flagged ``me`` for the panel and the hover to say so.
    Power is still cabled, and says nothing of the sort.
    """
    scene = _sand_scene(METoggles(items=True))
    assert [r["commodity"] for r in scene["routes"]] == ["power"]
    assert scene["autoConnections"] == []
    io = scene["io"]
    assert [(f["resource"], f["me"]) for f in io["inputs"]] == [("minecraft:stone", True)]
    assert [(f["resource"], f["me"]) for f in io["outputs"]] == [("minecraft:sand", True)]
    assert io["power"]["me"] is False
    held = [c for m in scene["machines"] for c in m["contents"]]
    assert held
    assert all(c["me"] for c in held)


def test_scene_says_power_left_to_me_arrives_over_me() -> None:
    # The other commodity on its own: no cable is laid, the feed spec is still stated (the ME side
    # has to deliver it), and the item flows, still auto-output, are not flagged.
    scene = _sand_scene(METoggles(power=True))
    assert scene["routes"] == []
    io = scene["io"]
    assert io["power"]["me"] is True
    assert io["power"]["byTier"] == {"LV": {"volts": 32, "amps": 2}}
    assert not any(f["me"] for f in io["inputs"] + io["outputs"])


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
    assert placed["contents"] == [{"resource": "water", "flow": "out", "me": False}]


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
    # marker), not the JS that swaps the materials - the running faces ride scene.texturesActive.
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
    assert html.count(_XSS) == 3  # the three strings seeded above...
    assert payload.count(_XSS) == 3  # ...all of them inside the inlined JSON, none outside it
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
