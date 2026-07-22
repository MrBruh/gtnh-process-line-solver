"""Tests for the previewer's testable seam: the (problem, layout) -> scene mapping and the HTML
assembly. The WebGL rendering itself is validated by eye, not in CI - so everything that *can*
be asserted (scene shape, colours, thickness, the inlined-and-self-contained HTML) is.
"""

from __future__ import annotations

import json
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
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.previewer import build_scene, render_html, write_preview
from gtnh_solver.solver import solve
from tests._helpers import consumer, net, producer

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _sand_scene() -> dict:
    # The fast (constructive) solve: deterministic layout coordinates that the exact-cell
    # assertions below can rely on; scene building does not care which placer produced them.
    ir = adapt_file(_SAND)
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
    # item/fluid terminals carry no thickness either, so their leads keep the fixed pipe size
    assert all(t["thickness"] is None for r in item_routes for t in r["terminals"])


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
    assert set(term) == {"machine", "face", "cell", "thickness"}
    assert len(term["cell"]) == 3
    # every power terminal sizes its lead from a real cable thickness (GitHub #6)
    assert all(t["thickness"] in {1, 2, 4, 8, 12, 16} for t in power["terminals"])


def test_scene_power_terminal_thickness_is_the_fattest_incident_segment() -> None:
    # A hand-built trunk with known per-segment thicknesses (GitHub #6):
    #
    #   src ==4x== [tap] ==2x== m1
    #               |
    #               1x
    #               |
    #               m3
    #
    # Each terminal must report the thickness of the segment incident to its cell; m2 taps the
    # branch cell, which 4x/2x/1x segments all touch, so the THICKEST (4) wins - that is the
    # cable that visually meets the block. build_scene's route mapping never looks placements
    # up, so a routes-only layout keeps the fixture minimal.
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
    assert [t["thickness"] for t in scene_route["terminals"]] == [4, 2, 4, 1]


def test_scene_reports_system_io() -> None:
    # the boundary summary the HUD renders (GitHub #5): what to feed, what comes out, total power
    io = _sand_scene()["io"]
    assert len(io["inputs"]) == 1
    assert io["inputs"][0]["resource"] == "minecraft:stone"
    assert io["inputs"][0]["rate"] == pytest.approx(0.1)
    assert io["inputs"][0]["unit"] == "items"  # stem only; the viewer appends /t or /s
    assert io["outputs"] == [
        {"resource": "minecraft:sand", "rate": pytest.approx(0.1), "unit": "items"}
    ]
    # the power feed per tier: the FULL LV tier voltage (32, not the hammers' 16 EU/t draw) x the
    # amps to supply - what a GT source is fed. The hammers' fractional loads (~0.53 A each at
    # their delivered voltages) sum to 1.66 and round up once to 2 A, so ``total`` is that feed
    # (32 V x 2 A = 64 EU/t) and matches the breakdown, not the machines' lower actual draw.
    assert io["power"] == {
        "total": pytest.approx(64),
        "byTier": {"LV": {"volts": 32, "amps": 2}},
    }


def test_scene_is_deterministic() -> None:
    assert _sand_scene() == _sand_scene()


def test_render_html_is_self_contained_with_camera_and_layer_controls() -> None:
    html = render_html(_sand_scene())
    assert html.startswith("<!doctype html>")
    assert "three.module.js" in html  # three.js pulled from the CDN
    assert "OrbitControls" in html  # move the camera around
    assert 'id="layer"' in html  # the layer-by-layer slider...
    assert 'type="range"' in html  # ...is a range input
    assert "Forge Hammer" in html  # the scene is inlined, not fetched


def test_render_html_wires_the_requested_viewer_features() -> None:
    html = render_html(_sand_scene())
    assert "screenSpacePanning" in html  # camera can translate, not just orbit (#1)
    assert "listenToKeyEvents" in html  # ...incl. arrow-key panning
    assert "BoxGeometry" in html  # cables/pipes are rectangular bars, not cylinders (#2)
    assert "PlaneGeometry" in html  # machine names live on the front face (#3)
    assert "faceArrow" in html  # per-face auto-output direction arrows (#4)
    assert "t.thickness" in html  # leads sized from the scene's terminal thickness (#6)
    assert "Raycaster" in html  # hover a block -> its machine name tag
    assert 'id="nametag"' in html  # ...shown in the floating name-tag element


def test_render_html_auto_output_arrow_draws_on_top_of_the_machine() -> None:
    # GitHub #30: the front-face auto-output arrow (#20) was buried - a placeholder machine's opaque
    # name plate drew over it, and a machine that bakes textures renders as full-size (1.0) block cubes
    # that swallowed an arrow tucked against the 0.92-scaled box. The arrow is now lifted just outside
    # the machine's actual rendered surface (expansion-aware), so it draws on top of both the casing
    # texture and the label, which keeps its opaque high-contrast backing. The WebGL result is eye-
    # validated, so assert the two mechanisms on their own code: the arrow offset is expansion-aware,
    # and the name decal still fills an opaque backing.
    html = render_html(_sand_scene())
    arrows = html[
        html.index("for (const ac of SCENE.autoConnections)") : html.index("const layer =")
    ]
    assert "expandedById" in arrows  # arrow clears full-size textured cubes, not just the flat box
    front = html[html.index("function frontFace(") : html.index("const TEXTURES")]
    assert "fillRect(0, 0, W, H)" in front  # ...and the name keeps its opaque, readable backing


def test_render_html_marks_an_unresolved_block_face_as_missing_not_grey() -> None:
    # GitHub #98 asks for texture gaps to be loud, not silent. A face with no baked texture used to
    # fall back to a neutral casing grey, which was actively misleading: plenty of GT casings ARE
    # plain grey, so an unresolved sprite looked exactly like a correctly rendered one and the gap
    # stayed invisible in the very view meant to reveal it. It now draws Minecraft's own
    # missing-texture checkerboard. The WebGL result is eye-validated, so assert the mechanism: the
    # fallback material is built from a magenta/black canvas and is what an unbaked face resolves to.
    html = render_html(_sand_scene())
    missing = html[html.index("const _MISSING = ") : html.index("function blockMaterials(")]
    assert "#f800f8" in missing  # MC's missing-texture magenta...
    assert "#000000" in missing  # ...checkered against black
    assert "NearestFilter" in missing  # crisp pixel art, consistent with every other sprite
    assert "return _MISSING;" in html  # ...and an unbaked face actually falls back to it


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


def test_render_html_draws_a_floor_grid() -> None:
    # The floor grid frames the build (GitHub #19). Its snap-to-cell-boundary math is bounds-derived,
    # JS-only, and eye-validated, so assert only that the grid is wired into the page (one coarse
    # marker) instead of grepping the exact alignment expression a refactor is free to move.
    assert "GridHelper" in render_html(_sand_scene())


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


def test_write_preview_writes_an_html_file(tmp_path: Path) -> None:
    ir = adapt_file(_SAND)
    out = write_preview(ir, solve(ir), tmp_path / "view.html")
    assert out.exists()
    assert "gtnh-solve preview" in out.read_text(encoding="utf-8")
