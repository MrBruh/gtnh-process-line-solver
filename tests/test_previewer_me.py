"""The preview of an ME network (#338): the scene's ``me`` section, the texture pass that embeds
AE2's art for it, the credit that art's licence asks for, and the viewer that draws it.

``previewer.me_blocks`` has its own tests for the geometry; these hold the seams around it. The
scene must carry what the viewer draws with no lookup of its own (boxes in three.js face order,
the parts' hover text, each network's legend entry from ``system_io``); the texture pass must cost
only the ME icons when an AE2 or FC jar fails, never the GT textures; and a page carries AE2's
credit exactly when it embeds AE2 or FC art.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gtnh_solver.adapter import adapt_file
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    InputIR,
    LayoutResult,
    LayoutStatus,
    MECableCell,
    MECableKind,
    MEMode,
)
from gtnh_solver.previewer import SCENE_VERSION, build_scene
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


def test_a_sand_line_on_me_draws_its_network_and_names_it_in_the_io_panel() -> None:
    scene = build_scene(*_sand_on_me())
    me = _me(scene)
    assert me["networks"][0]["id"] == "main"
    assert me["cells"]
    assert any(c["role"] == "attach" for c in me["cells"])
    assert [(f["resource"], f["network"]) for f in scene["io"]["inputs"]] == [
        ("minecraft:stone", "main")
    ]
