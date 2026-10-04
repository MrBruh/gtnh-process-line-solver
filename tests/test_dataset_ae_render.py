"""The committed ME render data (``data/ae2/<AE2 version>/render.json``) and its loader.

The file is derived from AE2's source by ``tools/derive_ae_render.py``; these tests do not trust the
tool. They hold the committed numbers to the figures ``docs/spikes/329-me-ae2.md`` section 8 read by
hand (so a deriver bug that agrees with itself still fails here), check that the file is complete and
geometrically sane without going through the models, and pin the loader's refusals: a stale schema
or an incomplete file must not half-load into a previewer that would then draw nothing quietly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gtnh_solver.dataset.ae_render import (
    AE_RENDER_PATH,
    AE_RENDER_SCHEMA,
    ME_BLOCKS,
    PART_DEVICES,
    AERender,
    PartArm,
    asset_path,
    load_ae_render,
)
from gtnh_solver.dataset.mod_jars import AE2, AE2FC
from gtnh_solver.dataset.schema import DatasetSchemaError
from gtnh_solver.ir.me import AEColor, MECableKind, MEDeviceKind

_RAW: dict[str, Any] = json.loads(AE_RENDER_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def render() -> AERender:
    return load_ae_render()


def _boxes(node: object) -> Iterator[list[int]]:
    """Every box anywhere in the raw document: a ``box`` value or each of a ``boxes`` list."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "box":
                yield value
            elif key == "boxes":
                yield from value
            else:
                yield from _boxes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _boxes(item)


# --- the file as committed -------------------------------------------------------------------------


def test_the_committed_file_is_the_pinned_ae2s_and_says_where_it_came_from() -> None:
    assert AE_RENDER_PATH.parent.name == AE2.version
    assert _RAW["schema"] == AE_RENDER_SCHEMA
    sources = _RAW["provenance"]["sources"]
    assert sources[AE2.modid]["tag"] == AE2.version
    assert sources[AE2.modid]["repo"] == AE2.source_repo
    assert sources[AE2FC.modid]["tag"] == AE2FC.version
    assert _RAW["provenance"]["derived_by"] == "tools/derive_ae_render.py"


def test_every_cable_kind_part_colour_and_block_is_present() -> None:
    assert set(_RAW["cables"]) == {kind.value for kind in MECableKind}
    assert set(_RAW["cable_icons"]) == {kind.value for kind in MECableKind}
    assert set(_RAW["colours"]) == {colour.value for colour in AEColor}
    assert set(_RAW["parts"]) == {device.value for device in PART_DEVICES}
    assert set(_RAW["blocks"]) == set(ME_BLOCKS)
    for icons in _RAW["cable_icons"].values():
        assert set(icons["by_colour"]) == {colour.value for colour in AEColor}


def test_every_box_is_inside_the_block_and_has_volume() -> None:
    boxes = list(_boxes(_RAW))
    assert len(boxes) > 50  # cables, connections and every part's two box lists
    for box in boxes:
        assert len(box) == 6, box
        assert all(isinstance(v, int) and 0 <= v <= 16 for v in box), box
        assert all(box[i] < box[i + 3] for i in range(3)), box


def test_it_holds_numbers_and_icon_names_only() -> None:
    text = AE_RENDER_PATH.read_text(encoding="utf-8")
    # The note says where PNGs come from, so look for embedded image data, not the word.
    assert "base64" not in text
    assert "data:image" not in text
    assert "\x89PNG" not in text
    assert AE_RENDER_PATH.stat().st_size < 64_000


def test_every_icon_is_an_me_jar_icon(render: AERender) -> None:
    names = render.icon_names()
    assert len(names) > 100
    assert {name.split(":")[0] for name in names} == {AE2.modid, AE2FC.modid}
    for name in names:
        assert asset_path(name).startswith((f"assets/{AE2.modid}/", f"assets/{AE2FC.modid}/"))


# --- the spike's figures (section 8), read by hand ----------------------------------------------------


def test_cable_cores_arms_plugs_and_straight_runs(render: AERender) -> None:
    glass = render.cables[MECableKind.GLASS]
    assert glass.core.box == (6, 6, 6, 10, 10, 10)
    assert glass.connections["other"].arm.box == (6, 0, 6, 10, 6, 10)
    plug = glass.connections["other"].plug
    assert plug is not None
    assert plug.box == (5, 0, 5, 11, 4, 11)
    assert plug.icons is MECableKind.COVERED  # glass plugs into a device in the covered skin
    assert glass.straight.box == (6, 0, 6, 10, 16, 10)

    for kind in (MECableKind.COVERED, MECableKind.SMART):
        cable = render.cables[kind]
        assert cable.core.box == (5, 5, 5, 11, 11, 11)
        assert cable.connections["other"].arm.box == (6, 0, 6, 10, 5, 10)  # thinner than the core
        assert cable.straight.box == (5, 0, 5, 11, 16, 11)

    for kind in (MECableKind.DENSE, MECableKind.DENSE_COVERED):
        cable = render.cables[kind]
        assert cable.core.box == (3, 3, 3, 13, 13, 13)
        assert cable.connections["dense"].arm.box == (4, 0, 4, 12, 5, 12)
        assert cable.connections["dense"].plug is None
        assert cable.connections["other"].arm.box == (6, 0, 6, 10, 5, 10)  # the covered arm
        assert cable.straight.box == (3, 0, 3, 13, 16, 13)
        assert cable.part_arm is None  # a dense cable takes no part


def test_which_icons_and_lights_each_cable_box_wears(render: AERender) -> None:
    smart = render.cables[MECableKind.SMART]
    assert smart.core.icons is MECableKind.COVERED
    assert not smart.core.lights
    assert smart.connections["other"].arm.icons is MECableKind.SMART
    assert smart.connections["other"].arm.lights
    assert smart.straight.lights

    dense = render.cables[MECableKind.DENSE]
    assert dense.core.icons is MECableKind.DENSE
    assert dense.connections["dense"].arm.icons is MECableKind.SMART  # dense arms reuse smart
    assert dense.connections["dense"].arm.lights
    assert dense.connections["smart"].arm.icons is MECableKind.SMART
    assert dense.connections["other"].arm.icons is MECableKind.COVERED
    assert not dense.connections["other"].arm.lights

    dense_covered = render.cables[MECableKind.DENSE_COVERED]
    assert dense_covered.connections["dense"].arm.icons is MECableKind.COVERED
    assert not dense_covered.connections["dense"].arm.lights
    assert "smart" not in dense_covered.connections

    for kind in (MECableKind.GLASS, MECableKind.COVERED, MECableKind.DENSE_COVERED):
        assert not render.cables[kind].straight.lights  # only smart and dense show channels


def test_a_part_arm_ends_at_the_parts_length(render: AERender) -> None:
    glass_arm = render.cables[MECableKind.GLASS].part_arm
    covered_arm = render.cables[MECableKind.COVERED].part_arm
    assert glass_arm is not None
    assert covered_arm is not None
    assert glass_arm.box(5) == (6, 5, 6, 10, 6, 10)
    assert covered_arm.box(4) == (6, 4, 6, 10, 5, 10)
    assert glass_arm.drawn_below_length == 8


def test_a_part_arm_is_none_where_nothing_of_it_shows(render: AERender) -> None:
    # A bus's arm length is 5, a covered or smart cable's face toward it is at 5 too: AE2 sets a box
    # with no height there, flat on a face already drawn. A storage bus or interface (4) gets one.
    for kind in (MECableKind.COVERED, MECableKind.SMART):
        arm = render.cables[kind].part_arm
        assert arm is not None
        assert arm.top == 5
        for device in (MEDeviceKind.IMPORT_BUS, MEDeviceKind.EXPORT_BUS):
            assert arm.box(render.parts[device].arm_length) is None
        for device in (MEDeviceKind.STORAGE_BUS, MEDeviceKind.INTERFACE):
            assert arm.box(render.parts[device].arm_length) == (6, 4, 6, 10, 5, 10)
    glass_arm = render.cables[MECableKind.GLASS].part_arm
    assert glass_arm is not None
    assert glass_arm.box(6) is None  # glass's face is at 6

    # AE2's guard (len < 8) skips the arm by itself, whatever the top: shown on a taller arm.
    tall = PartArm.model_validate({**_RAW["cables"]["glass"]["part_arm"], "top": 16})
    assert tall.box(7) == (6, 7, 6, 10, 16, 10)
    assert tall.box(8) is None


@pytest.mark.parametrize(
    ("device", "boxes", "arm"),
    [
        (
            MEDeviceKind.IMPORT_BUS,
            [(6, 6, 11, 10, 10, 13), (5, 5, 13, 11, 11, 14), (4, 4, 14, 12, 12, 16)],
            5,
        ),
        (
            MEDeviceKind.EXPORT_BUS,
            [
                (4, 4, 12, 12, 12, 14),
                (5, 5, 14, 11, 11, 15),
                (6, 6, 15, 10, 10, 16),
                (6, 6, 11, 10, 10, 12),
            ],
            5,
        ),
        (
            MEDeviceKind.STORAGE_BUS,
            [(3, 3, 15, 13, 13, 16), (2, 2, 14, 14, 14, 15), (5, 5, 12, 11, 11, 14)],
            4,
        ),
        (MEDeviceKind.INTERFACE, [(2, 2, 14, 14, 14, 16), (5, 5, 12, 11, 11, 14)], 4),
    ],
)
def test_part_boxes_and_arm_lengths(
    render: AERender, device: MEDeviceKind, boxes: list[tuple[int, ...]], arm: int
) -> None:
    part = render.parts[device]
    assert list(part.boxes) == boxes
    assert part.arm_length == arm
    assert part.mod == AE2.modid
    assert part.render[-1].status_lights  # the last box drawn is the status box
    assert not any(box.status_lights for box in part.render[:-1])
    assert part.render[-1].sides == "appliedenergistics2:PartMonitorSidesStatus"
    assert part.status_lights == "appliedenergistics2:PartMonitorSidesStatusLights"


def test_fluidcraft_parts_are_ae2s_with_their_own_fronts(render: AERender) -> None:
    pairs = {
        MEDeviceKind.FLUID_IMPORT_BUS: (MEDeviceKind.IMPORT_BUS, "ae2fc:fluid_import_face"),
        MEDeviceKind.FLUID_EXPORT_BUS: (MEDeviceKind.EXPORT_BUS, "ae2fc:fluid_export_face"),
        MEDeviceKind.FLUID_STORAGE_BUS: (MEDeviceKind.STORAGE_BUS, "ae2fc:fluid_storage_bus"),
        MEDeviceKind.DUAL_INTERFACE: (MEDeviceKind.INTERFACE, "ae2fc:part_fluid_interface"),
    }
    for fluid, (item, front) in pairs.items():
        fc, ae = render.parts[fluid], render.parts[item]
        assert fc.mod == AE2FC.modid
        assert (fc.boxes, fc.arm_length) == (ae.boxes, ae.arm_length)
        assert [b.box for b in fc.render] == [b.box for b in ae.render]
        assert {b.front for b in fc.render} == {front}
        assert [(b.sides, b.back) for b in fc.render] == [(b.sides, b.back) for b in ae.render]
    assert render.parts[MEDeviceKind.IMPORT_BUS].render[0].front == (
        "appliedenergistics2:ItemPart.ImportBus"
    )
    assert render.parts[MEDeviceKind.IMPORT_BUS].render[0].sides == (
        "appliedenergistics2:PartImportSides"
    )


def test_cable_icons_keep_ae2s_grey_spellings(render: AERender) -> None:
    assert render.cable_icon(MECableKind.GLASS, AEColor.GRAY) == "appliedenergistics2:MECable_Grey"
    assert render.cable_icon(MECableKind.GLASS, AEColor.LIGHT_GRAY) == (
        "appliedenergistics2:MECable_LightGrey"
    )
    for kind, stem in (
        (MECableKind.COVERED, "MECovered"),
        (MECableKind.SMART, "MESmart"),
        (MECableKind.DENSE, "MEDense"),
        (MECableKind.DENSE_COVERED, "MEDenseCovered"),
    ):
        assert render.cable_icon(kind, AEColor.GRAY) == f"appliedenergistics2:{stem}_Gray"
        assert render.cable_icon(kind, AEColor.LIGHT_GRAY) == (
            f"appliedenergistics2:{stem}_LightGrey"
        )
        assert render.cable_icon(kind, AEColor.LIGHT_BLUE) == (
            f"appliedenergistics2:{stem}_LightBlue"
        )
    fluix = {kind: render.cable_icon(kind, AEColor.FLUIX) for kind in MECableKind}
    assert fluix == {
        MECableKind.GLASS: "appliedenergistics2:ItemPart.CableGlass",
        MECableKind.COVERED: "appliedenergistics2:ItemPart.CableCovered",
        MECableKind.SMART: "appliedenergistics2:ItemPart.CableSmart",
        MECableKind.DENSE: "appliedenergistics2:ItemPart.CableDense",
        MECableKind.DENSE_COVERED: "appliedenergistics2:ItemPart.CableDenseCovered",
    }


def test_channel_lights_by_count(render: AERender) -> None:
    lights = render.channel_lights
    for count in range(5):  # 0c/10 up to four
        assert lights.icons(count) == (
            f"appliedenergistics2:MECableSmart0{count}",
            "appliedenergistics2:MECableSmart10",
        )
    for count in range(5, 9):  # then 04/1(c-4)
        assert lights.icons(count) == (
            "appliedenergistics2:MECableSmart04",
            f"appliedenergistics2:MECableSmart1{count - 4}",
        )
    assert lights.icons(99) == lights.icons(8)
    assert lights.pass_tints == ("black_variant", "white_variant")


def test_the_count_a_side_shows(render: AERender) -> None:
    lights = render.channel_lights
    assert lights.shown_count(5) == 5
    assert lights.shown_count(12) == 8  # a normal side caps at eight
    assert lights.shown_count(32, dense=True) == 8  # a dense-to-dense side counts in fours
    assert lights.shown_count(13, dense=True) == 3
    assert lights.shown_count(40, dense=True) == 8
    assert lights.shown_count(7, powered=False) == 0


def test_colour_tints_are_ae2s(render: AERender) -> None:
    white = render.colours[AEColor.WHITE]
    assert (white.ordinal, white.ae_name) == (0, "White")
    assert white.black_variant == (0xBE, 0xBE, 0xBE)
    assert white.white_variant == (0xFA, 0xFA, 0xFA)
    fluix = render.colours[AEColor.FLUIX]
    assert (fluix.ordinal, fluix.ae_name) == (16, "Transparent")
    assert [render.colours[c].ordinal for c in AEColor] == list(range(17))


def test_blocks_faces_and_the_cable_type_they_report(render: AERender) -> None:
    cable_types = {key: block.cable_type for key, block in render.blocks.items()}
    assert cable_types == {
        "controller": MECableKind.DENSE,
        "interface": MECableKind.SMART,
        "drive": MECableKind.SMART,
        "energy_acceptor": MECableKind.COVERED,
    }
    drive = render.blocks["drive"].faces
    assert drive["south"] == "appliedenergistics2:BlockDriveFront"
    assert drive["down"] == "appliedenergistics2:BlockDriveBottom"
    assert drive["up"] == "appliedenergistics2:BlockDrive"
    assert {drive["north"], drive["east"], drive["west"]} == {"appliedenergistics2:BlockDriveSide"}
    assert set(render.blocks["interface"].faces.values()) == {"appliedenergistics2:BlockInterface"}
    assert set(render.blocks["energy_acceptor"].faces.values()) == {
        "appliedenergistics2:BlockEnergyAcceptor"
    }
    states = render.blocks["controller"].states
    assert states is not None
    assert states["off"].icon == "appliedenergistics2:BlockController"
    assert states["powered"].icon == "appliedenergistics2:BlockControllerPowered"
    assert states["powered"].lights == "appliedenergistics2:BlockControllerLights"
    assert states["conflict"].lights == "appliedenergistics2:BlockControllerConflict"
    assert states["column_powered"].lights == "appliedenergistics2:BlockControllerColumnLights"
    assert states["column_off"].lights is None
    assert render.blocks["drive"].states is None


def test_the_part_frame_puts_z_at_the_parts_own_side(render: AERender) -> None:
    axes = render.part_frame.axes
    assert axes["down"] == {"x": "east", "y": "north", "z": "down"}
    assert axes["north"] == {"x": "west", "y": "up", "z": "north"}
    assert all(frame["z"] == side for side, frame in axes.items())


# --- routing helpers ------------------------------------------------------------------------------


def test_a_cable_picks_its_connection_by_what_the_neighbour_reports(render: AERender) -> None:
    dense = render.cables[MECableKind.DENSE]
    assert dense.connection(MECableKind.DENSE_COVERED) == dense.connections["dense"]
    assert dense.connection(MECableKind.DENSE) == dense.connections["dense"]
    assert dense.connection(MECableKind.SMART) == dense.connections["smart"]
    assert dense.connection(MECableKind.COVERED) == dense.connections["other"]
    assert dense.connection(None) == dense.connections["other"]  # a GT ME hatch reports none
    dense_covered = render.cables[MECableKind.DENSE_COVERED]
    assert dense_covered.connection(MECableKind.SMART) == dense_covered.connections["other"]
    glass = render.cables[MECableKind.GLASS]
    assert glass.connection(MECableKind.DENSE) == glass.connections["other"]


def test_asset_path() -> None:
    assert asset_path("appliedenergistics2:MECable_Grey") == (
        "assets/appliedenergistics2/textures/blocks/MECable_Grey.png"
    )
    assert asset_path("ae2fc:fluid_import_face") == (
        "assets/ae2fc/textures/blocks/fluid_import_face.png"
    )
    with pytest.raises(ValueError, match="modid:Name"):
        asset_path("MECable_Grey")


# --- the loader's refusals --------------------------------------------------------------------------


def _write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "render.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_file_of_another_schema_is_refused_before_it_is_read(tmp_path: Path) -> None:
    with pytest.raises(DatasetSchemaError, match="render schema 99"):
        load_ae_render(_write(tmp_path, {**_RAW, "schema": 99}))
    with pytest.raises(DatasetSchemaError, match="None"):
        load_ae_render(_write(tmp_path, {"cables": {}}))


def test_an_incomplete_file_does_not_load(tmp_path: Path) -> None:
    parts = {k: v for k, v in _RAW["parts"].items() if k != MEDeviceKind.DUAL_INTERFACE.value}
    with pytest.raises(ValidationError, match="dual_interface"):
        load_ae_render(_write(tmp_path, {**_RAW, "parts": parts}))
    icons = json.loads(json.dumps(_RAW["cable_icons"]))
    del icons["glass"]["by_colour"]["fluix"]
    with pytest.raises(ValidationError, match="fluix"):
        load_ae_render(_write(tmp_path, {**_RAW, "cable_icons": icons}))


@pytest.mark.parametrize(
    "box",
    [[6, 6, 6, 10, 10, 17], [-1, 6, 6, 10, 10, 10], [10, 6, 6, 6, 10, 10], [6, 6, 6, 10, 6, 10]],
)
def test_a_box_outside_the_block_or_without_volume_does_not_load(
    tmp_path: Path, box: list[int]
) -> None:
    cables = json.loads(json.dumps(_RAW["cables"]))
    cables["glass"]["core"]["box"] = box
    with pytest.raises(ValidationError, match="core"):
        load_ae_render(_write(tmp_path, {**_RAW, "cables": cables}))


def test_lights_must_cover_every_count(tmp_path: Path) -> None:
    lights = json.loads(json.dumps(_RAW["channel_lights"]))
    del lights["by_count"]["3"]
    with pytest.raises(ValidationError, match="every count"):
        load_ae_render(_write(tmp_path, {**_RAW, "channel_lights": lights}))


def _edited(edit: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """A deep copy of the committed document with ``edit`` applied to it."""
    doc: dict[str, Any] = json.loads(json.dumps(_RAW))
    edit(doc)
    return doc


def _east_frame(doc: dict[str, Any]) -> dict[str, str]:
    frame: dict[str, str] = doc["part_frame"]["axes"]["east"]  # x south, y up, z east
    return frame


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            lambda d: d["cables"]["glass"]["part_arm"].update(x=[10, 6]),
            "increasing span",
            id="span-reversed",
        ),
        pytest.param(
            lambda d: d["cables"]["glass"]["part_arm"].update(z=[6, 17]),
            "increasing span",
            id="span-outside",
        ),
        pytest.param(
            lambda d: d["colours"]["white"].update(black_variant=[256, 0, 0]),
            "RGB triple",
            id="rgb",
        ),
        pytest.param(
            lambda d: d["cables"]["glass"]["connections"].pop("other"),
            "'other' connection",
            id="no-other-connection",
        ),
        pytest.param(
            lambda d: d["blocks"]["drive"]["faces"].pop("up"),
            "every face",
            id="block-face",
        ),
        pytest.param(
            lambda d: d["part_frame"]["axes"].pop("east"),
            "a part frame per side",
            id="frame-side",
        ),
        pytest.param(lambda d: _east_frame(d).pop("z"), "names axes", id="frame-no-z"),
        pytest.param(lambda d: _east_frame(d).pop("x"), "names axes", id="frame-no-x"),
        pytest.param(
            lambda d: _east_frame(d).update(z="west"), "z axis points west", id="frame-z-elsewhere"
        ),
        pytest.param(
            lambda d: _east_frame(d).update(x="up"), "perpendicular", id="frame-axis-repeated"
        ),
        pytest.param(
            lambda d: _east_frame(d).update(x="down"), "perpendicular", id="frame-axis-opposite"
        ),
        pytest.param(
            lambda d: d["channel_lights"].update(dense_per_count=0),
            "greater than 0",
            id="dense-per-count",
        ),
        pytest.param(
            lambda d: d["channel_lights"].update(dense_cap=0), "greater than 0", id="dense-cap"
        ),
        pytest.param(
            lambda d: d["channel_lights"].update(max_count=0), "greater than 0", id="max-count"
        ),
    ],
)
def test_a_malformed_file_is_a_validation_error_not_a_crash(
    tmp_path: Path, edit: Callable[[dict[str, Any]], object], message: str
) -> None:
    # Each is refused at load, as a ValidationError naming it: a KeyError out of a validator, or a
    # zero that loads and then divides, would surface far from the file that caused it.
    with pytest.raises(ValidationError, match=message):
        load_ae_render(_write(tmp_path, _edited(edit)))
