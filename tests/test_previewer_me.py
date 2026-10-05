"""The preview of an ME network (#338): the scene's ``me`` section, the texture pass that embeds
AE2's art for it, the credit that art's licence asks for, and the viewer that draws it.

``previewer.me_blocks`` has its own tests for the geometry; these hold the seams around it. The
scene must carry what the viewer draws with no lookup of its own (boxes in three.js face order,
the parts' hover text, each network's legend entry from ``system_io``); the texture pass must cost
only the ME icons when an AE2 or FC jar fails, never the GT textures; and a page carries AE2's
credit exactly when it embeds AE2 or FC art.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from PIL import Image

import gtnh_solver.previewer as previewer_package
import gtnh_solver.previewer.jar as jar_module
from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset.ae_render import asset_path, load_ae_render
from gtnh_solver.dataset.mod_jars import AE2, AE2FC
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    InputIR,
    LayoutResult,
    LayoutStatus,
    MECableCell,
    MECableKind,
    MEDeviceKind,
    MEMode,
)
from gtnh_solver.previewer import SCENE_VERSION, build_scene, render_html, write_preview
from gtnh_solver.previewer.me_textures import credit, me_icons, texturize_me
from gtnh_solver.previewer.textures import DEFAULT_MANIFEST_PATH, texturize_scene
from gtnh_solver.solver import solve
from tests._helpers import at, machine
from tests._me_fixtures import attached_line, comb, gt_hatch_line

_REPO = Path(__file__).resolve().parents[1]


def _me(scene: dict[str, Any]) -> dict[str, Any]:
    me = scene["me"]
    assert me is not None
    return me  # type: ignore[no-any-return]


def _cell(scene: dict[str, Any], cell: list[int]) -> dict[str, Any]:
    return next(c for c in _me(scene)["cells"] if c["cell"] == cell)


# --- the scene ----------------------------------------------------------------------------------------


def test_a_layout_with_no_me_network_has_no_me_section() -> None:
    problem = InputIR(bounding_region=CellBox(sx=2, sy=1, sz=2), machines=[machine("a", [])])
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=[at("a", 0, 0, 0)])
    scene = build_scene(problem, layout)
    assert scene["version"] == SCENE_VERSION == 2
    assert scene["me"] is None
    assert scene["machines"][0]["meRole"] is None


def test_the_scene_carries_each_networks_legend_entry() -> None:
    (network,) = _me(build_scene(*attached_line()))["networks"]
    assert {k: network[k] for k in ("id", "mode", "colour", "colourName", "budget")} == {
        "id": "main",
        "mode": "attached",
        "colour": "fluix",
        "colourName": "Fluix",
        "budget": 32,
    }
    # Three channel devices, each a channel of the main network (system_io's ME section).
    assert (network["devices"], network["mainChannels"]) == (3, 3)
    assert (network["cables"], network["stubs"], network["controllers"]) == (6, 1, 0)
    assert network["adhocLimit"] is None  # attached: the main network hands out its channels
    assert network["swatch"].startswith("#")
    # The stone nothing in the line makes comes out of the network's storage.
    assert [(f["resource"], f["network"]) for f in network["supplies"]] == [("stone", "main")]
    assert network["absorbs"] == []


def test_an_ad_hoc_subnet_says_how_many_devices_it_can_give_a_channel() -> None:
    (network,) = _me(build_scene(*comb(3, mode=MEMode.SUBNET)))["networks"]
    assert (network["mode"], network["colour"], network["adhocLimit"]) == ("subnet", "orange", 8)
    (network,) = _me(build_scene(*comb(3, mode=MEMode.SUBNET, with_controller=True)))["networks"]
    assert (network["controllers"], network["adhocLimit"]) == (1, None)


def test_every_cable_block_is_in_the_scene_with_its_load_and_role() -> None:
    scene = build_scene(*attached_line())
    assert len(_me(scene)["cells"]) == 6
    stub = _cell(scene, [0, 0, 2])
    assert (stub["kind"], stub["role"], stub["roleLabel"], stub["machine"]) == (
        "dense",
        "attach",
        "attach stub",
        "stub",
    )
    assert stub["outside"] == ["west"]  # where the main network meets it
    assert stub["capacity"] == 32
    assert stub["label"] == "ME Dense Smart Cable (Fluix)"
    plain = _cell(scene, [3, 0, 2])
    assert (plain["kind"], plain["capacity"], plain["role"], plain["outside"]) == (
        "smart",
        8,
        None,
        [],
    )


def test_a_parts_hover_names_the_device_its_cards_and_what_it_serves() -> None:
    scene = build_scene(*attached_line())
    (feed,) = _cell(scene, [2, 0, 2])["parts"]
    assert feed["label"] == "ME Export Bus (1 x Acceleration Card)"
    assert (feed["side"], feed["machine"], feed["ports"], feed["flow"]) == (
        "north",
        "a",
        ["in"],
        "in",
    )
    (out,) = _cell(scene, [1, 0, 1])["parts"]
    assert (out["label"], out["side"], out["flow"]) == ("ME Interface", "east", "out")


def test_box_faces_are_in_three_js_slot_order() -> None:
    # three.js BoxGeometry: [+x east, -x west, +y up, -y down, +z south, -z north]. The export bus on
    # (2,0,2) faces north into its machine: its front icon is on the -z slot, its back on +z.
    scene = build_scene(*attached_line())
    plate = next(b for b in _cell(scene, [2, 0, 2])["boxes"] if b["part"] == 0)
    assert plate["faces"][5] == "appliedenergistics2:ItemPart.ExportBus"
    assert plate["faces"][4] == "appliedenergistics2:PartMonitorBack"
    assert plate["color"] == "#9aa0a8"  # a part falls back to grey, never to the cable's colour


def test_a_box_is_placed_in_world_blocks() -> None:
    scene = build_scene(*attached_line())
    (core,) = [b for b in _cell(scene, [1, 0, 2])["boxes"] if b["size"] == [0.375] * 3]
    assert core["center"] == [1.5, 0.5, 2.5]  # a smart core, 5..11 sixteenths, in the cell's middle
    for box in _cell(scene, [1, 0, 2])["boxes"]:
        for axis, low in enumerate([1, 0, 2]):
            half = box["size"][axis] / 2
            assert low <= box["center"][axis] - half
            assert box["center"][axis] + half <= low + 1


def test_lights_carry_their_mask_tint_and_faces() -> None:
    scene = build_scene(*attached_line())
    lit = [b for b in _cell(scene, [1, 0, 2])["boxes"] if b["lights"]]
    assert lit
    for box in lit:
        assert [light["icon"].split(":")[1][:12] for light in box["lights"]] == ["MECableSmart"] * 2
        for light in box["lights"]:
            assert light["tint"].startswith("#")
            assert light["faces"] == [face is not None for face in box["faces"]]


def test_a_controller_is_a_block_in_the_scene_and_its_machine_says_so() -> None:
    scene = build_scene(*comb(2, mode=MEMode.SUBNET, with_controller=True))
    (block,) = _me(scene)["blocks"]
    assert (block["role"], block["label"], block["machine"], block["look"]) == (
        "controller",
        "ME Controller",
        "ctrl",
        "powered",
    )
    assert block["boxes"][0]["size"] == [1.0, 1.0, 1.0]
    assert {m["id"]: m["meRole"] for m in scene["machines"]}["ctrl"] == "controller"


def test_the_view_frames_the_me_cables_too() -> None:
    problem, layout = attached_line()
    network = layout.me_networks[0]
    beyond = MECableCell(cell=CellCoord(x=5, y=0, z=2), kind=MECableKind.SMART)
    layout = layout.model_copy(
        update={"me_networks": [network.model_copy(update={"cables": [*network.cables, beyond]})]}
    )
    assert build_scene(problem, layout)["bounds"]["max"][0] == 6  # the cable at x = 5, past b


def test_a_gt_me_hatch_is_a_hatch_in_the_scene_and_no_part() -> None:
    scene = build_scene(*gt_hatch_line())
    assert all(not c["parts"] for c in _me(scene)["cells"])
    hatch = next(m for m in scene["machines"] if m["id"] == "mb")["hatches"][0]
    assert hatch["gtMid"] == 2710


def test_the_texture_pass_leaves_an_me_block_to_the_me_layer() -> None:
    # A stub is an AE2 cable: even with a type a GT block answers to, it is never expanded into a
    # GT cube, or the page would draw a Forge Hammer where the stub is.
    scene = build_scene(*attached_line())
    stub = next(m for m in scene["machines"] if m["id"] == "stub")
    stub.update({"type": "Forge Hammer", "recipe_map": None, "voltage_tier": "LV"})
    twin = {**stub, "id": "twin", "meRole": None}
    scene["machines"].append(twin)
    texturize_scene(
        scene,
        multiblocks_dir=_REPO / "data" / "multiblocks",
        manifest_path=DEFAULT_MANIFEST_PATH,
        png_provider=lambda _: {},
    )
    assert not stub.get("expanded")
    assert twin.get("expanded")  # the same machine with no ME role does resolve


def _sand_on_me() -> tuple[InputIR, LayoutResult]:
    # The optimizing solve: the fast one's touching row leaves a block no face for its devices.
    ir = adapt_file(_REPO / "examples" / "gtnh-sand.json", me_commodities=(Commodity.ITEM,))
    return ir, solve(ir, seed=0, optimize=True)


def test_the_texture_summary_does_not_call_an_me_block_a_placeholder() -> None:
    summary = texturize_scene(
        build_scene(*attached_line()),
        multiblocks_dir=_REPO / "data" / "multiblocks",
        manifest_path=DEFAULT_MANIFEST_PATH,
        png_provider=lambda _: {},
    )
    assert summary.placeholder_types == ("t",)  # the stub is the ME layer's, not a missing block


def test_a_sand_line_on_me_draws_its_network_and_names_it_in_the_io_panel() -> None:
    scene = build_scene(*_sand_on_me())
    me = _me(scene)
    assert me["networks"][0]["id"] == "main"
    assert me["cells"]
    assert any(c["role"] == "attach" for c in me["cells"])
    assert [(f["resource"], f["network"]) for f in scene["io"]["inputs"]] == [
        ("minecraft:stone", "main")
    ]


# --- the texture pass and the credit ------------------------------------------------------------------


def _png(size: int = 16, height: int | None = None) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (size, height or size), (200, 200, 200, 255)).save(out, format="PNG")
    return out.getvalue()


def _every_icon(paths: Mapping[str, str]) -> dict[str, bytes]:
    """A provider holding every AE2 and FC icon: 16x16, the channel masks 64x64 as in the jar."""
    return {icon: _png(64 if "MECableSmart" in icon else 16) for icon in paths}


def _size(uri: str) -> tuple[int, int]:
    image = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    return image.size


def test_the_me_pass_embeds_each_face_icon_and_each_light_mask_at_its_own_size() -> None:
    scene = build_scene(*attached_line())
    faces, lights = me_icons(scene)
    assert faces
    assert lights
    assert texturize_me(scene, _every_icon) == frozenset({"appliedenergistics2"})
    assert faces <= set(scene["textures"])
    assert all(_size(scene["textures"][icon]) == (16, 16) for icon in faces)
    masks = _me(scene)["lights"]
    assert set(masks) == lights
    # A channel mask keeps its 64 pixels: squeezed into a 16 pixel tile its lines would vanish.
    assert _size(masks["appliedenergistics2:MECableSmart00"]) == (64, 64)


def test_an_animated_light_keeps_its_first_frame() -> None:
    scene = build_scene(*comb(1, mode=MEMode.SUBNET, with_controller=True))

    def strips(paths: Mapping[str, str]) -> dict[str, bytes]:
        return {icon: _png(16, 192 if "Lights" in icon else 16) for icon in paths}

    texturize_me(scene, strips)
    assert _size(_me(scene)["lights"]["appliedenergistics2:BlockControllerLights"]) == (16, 16)


def test_an_icon_that_does_not_arrive_costs_only_itself() -> None:
    # FC's jar failed: its fronts stay flat, and AE2's art (and the GT pass) is untouched.
    problem, layout = attached_line()
    network = layout.me_networks[0]
    fluid = [
        d.model_copy(update={"kind": MEDeviceKind.FLUID_EXPORT_BUS})
        if d.endpoint_id == "feed"
        else d
        for d in network.devices
    ]
    layout = layout.model_copy(
        update={"me_networks": [network.model_copy(update={"devices": fluid})]}
    )
    scene = build_scene(problem, layout)
    scene["textures"] = {"gregtech:gt.blockmachines|1|NORTH|inactive": "data:image/png;base64,"}

    def no_fc(paths: Mapping[str, str]) -> dict[str, bytes]:
        return {
            icon: png for icon, png in _every_icon(paths).items() if not icon.startswith("ae2fc")
        }

    assert texturize_me(scene, no_fc) == frozenset({"appliedenergistics2"})
    assert "ae2fc:fluid_export_face" not in scene["textures"]
    assert "appliedenergistics2:PartExportSides" in scene["textures"]
    assert "gregtech:gt.blockmachines|1|NORTH|inactive" in scene["textures"]
    assert texturize_me(build_scene(problem, layout), lambda _: {}) == frozenset()


def test_a_scene_with_no_me_network_asks_no_jar_for_anything() -> None:
    def refuse(paths: Mapping[str, str]) -> dict[str, bytes]:
        raise AssertionError(f"asked for {sorted(paths)}")

    problem = InputIR(bounding_region=CellBox(sx=1, sy=1, sz=1), machines=[machine("a", [])])
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=[at("a", 0, 0, 0)])
    assert texturize_me(build_scene(problem, layout), refuse) == frozenset()


def test_the_credit_names_the_mods_the_licence_and_the_terms() -> None:
    assert credit(frozenset()) is None
    assert credit(frozenset({"gregtech"})) is None  # GT's art is LGPL and asks for no such line
    both = credit(frozenset({"appliedenergistics2", "ae2fc"}))
    assert both is not None
    assert both["licence"] == "CC BY-NC-SA 3.0"
    assert both["url"] == "https://creativecommons.org/licenses/by-nc-sa/3.0/"
    assert "AlgorithmX2" in both["text"]
    assert "AE2FluidCraft" in both["text"]
    assert both["text"].index("Applied Energistics 2") < both["text"].index("AE2FluidCraft")
    assert "non-commercial" in both["text"]
    # The HUD's short form, which no fold hides, keeps the attribution and the terms.
    assert "AlgorithmX2" in both["short"]
    assert "non-commercial" in both["short"]
    only_ae2 = credit(frozenset({"appliedenergistics2"}))
    assert only_ae2 is not None
    assert "AE2FluidCraft" not in only_ae2["text"]


# --- write_preview, end to end, with the jars faked ----------------------------------------------------


class _Nexus:
    """Serves a fake jar per URL holding every icon a preview could ask that jar for; ``failing``
    URLs raise as an outage would."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.calls: list[str] = []

    def __call__(self, url: str, filename: str) -> None:
        self.calls.append(url)
        if url in self.failing:
            raise URLError("nexus unreachable")
        if url == AE2.url:
            entries = {asset_path(i) for i in load_ae_render().icon_names() if i.startswith("app")}
        elif url == AE2FC.url:
            entries = {
                asset_path(i) for i in load_ae_render().icon_names() if i.startswith("ae2fc")
            }
        else:  # GT's jar: every sprite the committed manifest names
            raw = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
            entries = set(raw["icons"].values())
        with zipfile.ZipFile(filename, "w") as archive:
            for entry in sorted(entries):
                archive.writestr(entry, _png(64 if "MECableSmart" in entry else 16))


@pytest.fixture(scope="module")
def sand_on_me() -> tuple[InputIR, LayoutResult]:
    return _sand_on_me()


def _preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    built: tuple[InputIR, LayoutResult],
    nexus: _Nexus,
    *,
    textures: bool = True,
) -> dict[str, Any]:
    real = jar_module.multi_jar_png_provider

    def faked(primary: Any, extras: Any) -> Any:
        return real(primary, extras, cache_dir=tmp_path / "cache", download=nexus)

    monkeypatch.setattr(previewer_package, "multi_jar_png_provider", faked)
    page = write_preview(*built, tmp_path / "view.html", textures=textures).read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in page.splitlines() if ln.startswith("const SCENE = "))
    scene: dict[str, Any] = json.loads(line[len("const SCENE = ") :].rstrip().removesuffix(";"))
    return scene


def test_a_preview_embedding_ae2_art_carries_its_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sand_on_me: tuple[InputIR, LayoutResult]
) -> None:
    scene = _preview(tmp_path, monkeypatch, sand_on_me, _Nexus())
    tiles = set(scene["atlas"]["tiles"])
    assert any(key.startswith("appliedenergistics2:") for key in tiles)
    assert any(key.startswith("gregtech:") for key in tiles)
    me = _me(scene)
    assert me["lights"]
    assert me["credit"]["licence"] == "CC BY-NC-SA 3.0"
    assert "non-commercial" in me["credit"]["text"]


def test_a_failing_ae2_jar_costs_only_the_me_icons_and_the_credit_with_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sand_on_me: tuple[InputIR, LayoutResult]
) -> None:
    nexus = _Nexus(failing=frozenset({AE2.url}))
    scene = _preview(tmp_path, monkeypatch, sand_on_me, nexus)
    tiles = set(scene["atlas"]["tiles"])
    assert any(key.startswith("gregtech:") for key in tiles)  # the GT textures all arrived
    assert not any(key.startswith("appliedenergistics2:") for key in tiles)
    assert any(m.get("expanded") for m in scene["machines"])
    me = _me(scene)
    assert me["lights"] == {}
    assert me["credit"] is None  # no AE2 art on the page, so nothing to credit
    assert AE2.url in nexus.calls


def test_a_preview_without_textures_embeds_no_art_and_no_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sand_on_me: tuple[InputIR, LayoutResult]
) -> None:
    nexus = _Nexus()
    scene = _preview(tmp_path, monkeypatch, sand_on_me, nexus, textures=False)
    assert _me(scene)["credit"] is None
    assert nexus.calls == []


def test_a_preview_with_no_me_network_never_fetches_the_me_jars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, solved_sand: tuple[InputIR, LayoutResult]
) -> None:
    nexus = _Nexus()
    scene = _preview(tmp_path, monkeypatch, solved_sand, nexus)
    assert scene["me"] is None
    assert AE2.url not in nexus.calls
    assert AE2FC.url not in nexus.calls


# --- the viewer ----------------------------------------------------------------------------------------


def test_the_machine_legend_leaves_the_me_blocks_to_the_me_section() -> None:
    scene = build_scene(*attached_line())
    assert [e["label"] for e in scene["legend"]] == ["t"]  # not the stub's "ME Dense Smart Cable"


def test_the_viewer_draws_the_me_layer_from_the_scene() -> None:
    page = render_html(build_scene(*attached_line()))
    for reads in (
        "const ME = SCENE.me || null",
        # An ME block gets no placeholder box: the ME layer draws it.
        "if (m.expanded || ME_DRAWN.has(m.id)) continue;",
        # The AE2 faces share the atlas through a cutout copy of its material (glass cable).
        "aeMaterial.alphaTest = 0.1",
        "if (ATLAS && icon in ATLAS.tiles) layerBatch.main.face(geo, f, b.center, aeMaterial",
        "else layerBatch.main.face(geo, f, b.center, routeFlat(b.color), owner)",
        # Channel lights: an unlit (fullbright) tinted mask, drawn in front of the face it lights.
        "new THREE.MeshBasicMaterial({",
        "polygonOffset: true",
        "const uri = ME_LIGHTS[icon];",
        # Hovering an ME block, and the legend's section per network.
        "if (what.me) return meHover(what);",
        "section(panel, 'ME networks')",
        "n.mainChannels + ' of ' + n.budget",
        # AE2's credit, on the HUD and in the legend.
        '<div id="credit"></div>',
        "document.getElementById('credit').append(...creditNodes(ME.credit, ME.credit.short))",
    ):
        assert reads in page, reads


def test_the_io_panel_no_longer_says_no_me_block_is_drawn() -> None:
    page = render_html(build_scene(*attached_line()))
    assert "no ME interface is drawn" not in page
    assert "menote" not in page
    # A flow on ME names its network, and power left to the builder says so.
    assert "' via ME' + (flow.network ? ' (' + flow.network + ')' : '')" in page
    assert "supplied by you (--me power)" in page


def test_the_credit_is_built_from_text_nodes_and_its_own_link() -> None:
    page = render_html(build_scene(*attached_line()))
    assert "const link = el('a', credit.licence);" in page
    assert "link.rel = 'noopener noreferrer';" in page
    assert "innerHTML" not in page.split("function creditNodes", 1)[1].split("}", 1)[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_viewer_module_parses(tmp_path: Path) -> None:
    """The one check on the template's JavaScript CI can run: it parses. A syntax error in it
    blanks every preview, and no Python test would notice."""
    page = render_html(build_scene(*comb(3, mode=MEMode.SUBNET, with_controller=True)))
    module = re.search(r'<script type="module">(.*?)</script>', page, re.S)
    assert module is not None
    script = tmp_path / "viewer.mjs"
    script.write_text(module.group(1), encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    checked = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
