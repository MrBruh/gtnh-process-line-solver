"""Tests for the previewer's testable seam: the (problem, layout) -> scene mapping and the HTML
assembly. The WebGL rendering itself is validated by eye, not in CI - so everything that *can*
be asserted (scene shape, colours, thickness, the inlined-and-self-contained HTML) is.
"""

from __future__ import annotations

import json
from pathlib import Path

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Port,
)
from gtnh_solver.previewer import build_scene, render_html, write_preview
from gtnh_solver.solver import solve

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _sand_scene() -> dict:
    ir = adapt_file(_SAND)
    return build_scene(ir, solve(ir))


def test_scene_has_machines_region_and_legend() -> None:
    scene = _sand_scene()
    assert scene["status"] == "valid"
    assert scene["region"]["sy"] == 4  # the adapter's bounding region is 4 tall
    assert len(scene["machines"]) == 5  # 3 hammers + chest + LV source
    assert any(m["role"] == "source" for m in scene["machines"])  # the synthesized power source
    assert any(m["role"] == "storage" for m in scene["machines"])  # the Super Chest
    assert scene["legend"]  # one entry per machine type
    # every machine carries the geometry the viewer needs, with no further lookups
    m = scene["machines"][0]
    assert set(m) >= {"id", "type", "cell", "size", "front", "role", "color"}


def test_scene_power_route_carries_thickness() -> None:
    scene = _sand_scene()
    power = [r for r in scene["routes"] if r["commodity"] == "power"]
    assert len(power) == 1
    thicknesses = [seg["thickness"] for r in power for seg in r["segments"]]
    assert thicknesses  # the trunk has segments
    assert all(isinstance(t, int) for t in thicknesses)
    assert set(thicknesses) <= {1, 2, 4, 8, 16}


def test_scene_items_auto_feed_so_no_item_pipes() -> None:
    scene = _sand_scene()
    assert [r["commodity"] for r in scene["routes"]] == ["power"]  # only the power cable is routed
    assert len(scene["autoConnections"]) == 3  # the three item nets auto-feed


def test_scene_item_pipe_segments_have_null_thickness() -> None:
    # a fan-out: one item net auto-outputs, the other is piped (thickness is power-only)
    def producer(mid: str) -> Machine:
        return Machine(
            id=mid,
            type="P",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(
                ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
            ),
        )

    def consumer(mid: str) -> Machine:
        return Machine(
            id=mid,
            type="C",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(
                ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
            ),
        )

    def net(nid: str, src: str, dst: str) -> Net:
        return Net(
            id=nid,
            commodity=Commodity.ITEM,
            fluid_or_item="x",
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id=src, port_id="out"),
                MachineFaceRef(machine_id=dst, port_id="in"),
            ],
        )

    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m1"), consumer("m2"), consumer("m3")],
        nets=[net("n1", "m1", "m2"), net("n2", "m1", "m3")],
    )
    scene = build_scene(problem, solve(problem))
    item_routes = [r for r in scene["routes"] if r["commodity"] == "item"]
    assert item_routes  # the piped fan-out leg
    assert all(seg["thickness"] is None for r in item_routes for seg in r["segments"])


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


def test_render_html_inlines_the_exact_scene() -> None:
    scene = _sand_scene()
    assert json.dumps(scene) in render_html(scene)  # embedded verbatim - no file:// fetch needed


def test_write_preview_writes_an_html_file(tmp_path: Path) -> None:
    ir = adapt_file(_SAND)
    out = write_preview(ir, solve(ir), tmp_path / "view.html")
    assert out.exists()
    assert "gtnh-solve preview" in out.read_text(encoding="utf-8")
