"""Tests for the lane 7 v2 texture pipeline: the Pillow bake and per-block previewer expansion.

The golden tests here are the regression guards the reconciled plan section 7 asks for: a machine's
baked base face is *tinted* (not neutral grey, so a dropped RGBA multiply is caught); a multiblock
expands to many distinct textured cubes rather than one stretched box (principle 6); and at least one
interior block (a coil) carries a texture distinct from the casing. Everything runs on synthetic
sprites + a synthetic manifest, so no jar fetch and no network.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset.schema import MultiblockDoc
from gtnh_solver.previewer.bake import bake_layers
from gtnh_solver.previewer.textures import (
    TextureManifest,
    expand_machine,
    primary_variant,
    texturize_scene,
)

pytest.importorskip("PIL")
from PIL import Image

# --------------------------------------------------------------------------------------------------
# Synthetic sprites + manifest + dataset
# --------------------------------------------------------------------------------------------------


def _png(color: tuple[int, int, int, int], size: int = 16) -> bytes:
    """A solid-colour ``size``x``size`` RGBA PNG - a stand-in for a real GT iconset sprite."""
    out = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(out, format="PNG")
    return out.getvalue()


def _strip(frames: list[tuple[int, int, int, int]]) -> bytes:
    """A vertical animation strip (16 wide, 16*len tall); the bake must take frame 0 (the top)."""
    img = Image.new("RGBA", (16, 16 * len(frames)))
    for i, c in enumerate(frames):
        img.paste(Image.new("RGBA", (16, 16), c), (0, i * 16))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _pixel(png: bytes, xy: tuple[int, int] = (0, 0)) -> tuple[int, int, int, int]:
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel(xy)


#: Icon names used across the synthetic manifest.
CASING = "gregtech:iconsets/MACHINE_HEATPROOFCASING"
COIL = "gregtech:iconsets/BLOCK_COIL_CUPRONICKEL"
MACH_SIDE = "gregtech:iconsets/MACHINE_LV_SIDE"
OVERLAY = "gregtech:iconsets/OVERLAY_FRONT_MACERATOR"

#: A png_provider: white base sprites so a tint multiply shows through as the tint colour, and a
#: half-alpha overlay so compositing is observable.
_ICON_PNG = {
    CASING: _png((200, 200, 200, 255)),
    COIL: _png((255, 255, 255, 255)),
    MACH_SIDE: _png((255, 255, 255, 255)),
    OVERLAY: _png((0, 0, 0, 128)),
}


def _provider(paths: Any) -> dict[str, bytes]:
    return {icon: _ICON_PNG[icon] for icon in paths if icon in _ICON_PNG}


def _manifest_dict() -> dict[str, Any]:
    """A schema-2 layered manifest: an MTE machine (tinted base + overlay), a casing, and a coil."""
    return {
        "schema": 2,
        "blocks": {
            "gregtech:gt.blockmachines|5": {
                "kind": "mte",
                "display_name": "Test Macerator",
                "sides": {
                    "NORTH": {
                        "inactive": [
                            {"icon": MACH_SIDE, "rgba": [120, 130, 200, 0], "glow": False},
                            {"icon": OVERLAY, "rgba": [255, 255, 255, 0], "glow": False},
                        ]
                    },
                    "SOUTH": {
                        "inactive": [{"icon": MACH_SIDE, "rgba": [120, 130, 200, 0], "glow": False}]
                    },
                },
            },
            "gregtech:gt.blockmachines|1000": {
                "kind": "mte",
                "display_name": "Test EBF",
                "sides": {
                    "NORTH": {
                        "inactive": [
                            {"icon": CASING, "rgba": [255, 255, 255, 255], "glow": False},
                            {"icon": OVERLAY, "rgba": [255, 255, 255, 0], "glow": False},
                        ]
                    },
                    "all": {
                        "inactive": [{"icon": CASING, "rgba": [255, 255, 255, 255], "glow": False}]
                    },
                },
            },
            "gregtech:gt.blockcasings|11": {
                "kind": "block",
                "sides": {
                    "all": {
                        "inactive": [{"icon": CASING, "rgba": [255, 255, 255, 255], "glow": False}]
                    }
                },
            },
            "gregtech:gt.blockcasings5|0": {
                "kind": "block",
                "sides": {
                    "all": {
                        "inactive": [{"icon": COIL, "rgba": [255, 200, 120, 255], "glow": False}]
                    }
                },
            },
        },
        "icons": {
            CASING: "assets/gregtech/textures/blocks/iconsets/MACHINE_HEATPROOFCASING.png",
            COIL: "assets/gregtech/textures/blocks/iconsets/BLOCK_COIL_CUPRONICKEL.png",
            MACH_SIDE: "assets/gregtech/textures/blocks/iconsets/MACHINE_LV_SIDE.png",
            OVERLAY: "assets/gregtech/textures/blocks/iconsets/OVERLAY_FRONT_MACERATOR.png",
        },
    }


def _ebf_doc() -> dict[str, Any]:
    """A mini EBF-like doc: a controller, three heat-proof casings, and one coil (5 blocks)."""
    blocks = [
        {"d": [0, 0, 0], "block": "gregtech:gt.blockmachines", "meta": 1000},
        {"d": [1, 0, 0], "block": "gregtech:gt.blockcasings", "meta": 11},
        {"d": [0, 0, 1], "block": "gregtech:gt.blockcasings", "meta": 11},
        {"d": [1, 0, 1], "block": "gregtech:gt.blockcasings", "meta": 11},
        {"d": [0, 1, 0], "block": "gregtech:gt.blockcasings5", "meta": 0},  # the coil layer
    ]
    return {
        "schema": 1,
        "controller": {
            "registry_name": "gregtech:gt.blockmachines",
            "meta": 1000,
            "display_name": "Test EBF",
            "source_class": "test.MTETestEBF",
            "facing_convention": "front NORTH",
        },
        "variants": [
            {
                "trigger_stack_size": 1,
                "channels": {},
                "blocks": blocks,
                "hints": [],
                "bbox": [2, 2, 2],
            }
        ],
        "substitutions": {},
        "failures": [],
    }


@pytest.fixture
def dataset(tmp_path: Path) -> tuple[Path, Path]:
    """A committed-dataset layout on disk: ``multiblocks/`` with the EBF doc + the layered manifest."""
    mb = tmp_path / "multiblocks"
    mb.mkdir()
    (mb / "test_ebf.json").write_text(json.dumps(_ebf_doc()), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest_dict()), encoding="utf-8")
    return mb, manifest


def _scene(machines: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "machines": machines}


def _machine(
    mid: str, mtype: str, cell: list[int], size: list[int], front: str = "north"
) -> dict[str, Any]:
    return {
        "id": mid,
        "type": mtype,
        "cell": cell,
        "size": size,
        "front": front,
        "role": "machine",
        "color": "#6ca0dc",
    }


# --------------------------------------------------------------------------------------------------
# bake.py - the Pillow compositor
# --------------------------------------------------------------------------------------------------


def test_bake_applies_rgba_tint_not_neutral() -> None:
    """A white base sprite tinted [120,130,200] bakes to that colour, never left neutral grey."""
    baked = bake_layers([{"icon": MACH_SIDE, "rgba": [120, 130, 200, 0], "glow": False}], _ICON_PNG)
    assert baked is not None
    r, g, b, a = _pixel(baked)
    assert (r, g, b) == (120, 130, 200), "tint multiply must be applied to the base sprite"
    assert a == 255, "a GT alpha of 0 means opaque, so the baked base stays fully opaque"


def test_bake_composites_overlay_over_base() -> None:
    """A half-alpha overlay darkens the base where it sits: the composite differs from the base alone."""
    base_only = bake_layers(
        [{"icon": MACH_SIDE, "rgba": [255, 255, 255, 0], "glow": False}], _ICON_PNG
    )
    composited = bake_layers(
        [
            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 0], "glow": False},
            {"icon": OVERLAY, "rgba": [255, 255, 255, 0], "glow": False},
        ],
        _ICON_PNG,
    )
    assert base_only is not None
    assert composited is not None
    assert _pixel(base_only) != _pixel(composited), "the overlay must change the baked result"


def test_bake_identity_rgba_leaves_sprite_unchanged() -> None:
    """An identity tint ([255,255,255,255]) bakes the sprite as-is (fast path, no per-pixel loop)."""
    baked = bake_layers([{"icon": CASING, "rgba": [255, 255, 255, 255], "glow": False}], _ICON_PNG)
    assert baked is not None
    assert _pixel(baked)[:3] == (200, 200, 200)


def test_bake_takes_frame0_of_an_animated_strip() -> None:
    """An animated vertical strip bakes its first (top) frame, per the v1 animation non-goal."""
    strip = {"anim": _strip([(10, 20, 30, 255), (200, 200, 200, 255)])}
    baked = bake_layers([{"icon": "anim", "rgba": [255, 255, 255, 255], "glow": False}], strip)
    assert baked is not None
    assert _pixel(baked)[:3] == (10, 20, 30)


def test_bake_skips_missing_icons_and_returns_none_when_all_missing() -> None:
    """A layer whose PNG was not fetched is skipped; a stack with none available bakes to None."""
    assert (
        bake_layers([{"icon": "absent", "rgba": [255, 255, 255, 255], "glow": False}], _ICON_PNG)
        is None
    )
    baked = bake_layers(
        [
            {"icon": "absent", "rgba": [255, 255, 255, 255], "glow": False},
            {"icon": CASING, "rgba": [255, 255, 255, 255], "glow": False},
        ],
        _ICON_PNG,
    )
    assert baked is not None  # the available casing layer still bakes


# --------------------------------------------------------------------------------------------------
# Manifest v2 loader
# --------------------------------------------------------------------------------------------------


def test_manifest_layers_side_and_state_fallback() -> None:
    m = TextureManifest(_manifest_dict())
    assert m.layers("gregtech:gt.blockmachines", 5, "NORTH")[0]["icon"] == MACH_SIDE
    assert (
        m.layers("gregtech:gt.blockcasings", 11, "EAST")[0]["icon"] == CASING
    )  # missing side -> "all"
    assert m.layers("gregtech:nope", 0, "NORTH") == []


def test_manifest_mte_block_reverse_index() -> None:
    m = TextureManifest(_manifest_dict())
    assert m.mte_block("Test Macerator") == ("gregtech:gt.blockmachines", 5)
    assert m.mte_block("Nonexistent") is None


# --------------------------------------------------------------------------------------------------
# Per-block expansion
# --------------------------------------------------------------------------------------------------


def test_expand_machine_yields_one_cube_per_block_at_offsets() -> None:
    doc = MultiblockDoc.model_validate(_ebf_doc())
    machine = _machine("m1", "Test EBF", cell=[10, 0, 20], size=[2, 2, 2])
    cubes = expand_machine(machine, doc)
    assert len(cubes) == len(primary_variant(doc).blocks) == 5
    cells = {c.cell for c in cubes}
    assert (10, 0, 20) in cells  # controller min corner lands on the placement cell
    assert (10, 1, 20) in cells  # the coil, one layer up


def test_expand_machine_yaw_rotates_positions_for_east_facing() -> None:
    doc = MultiblockDoc.model_validate(_ebf_doc())
    north = expand_machine(_machine("m", "Test EBF", [0, 0, 0], [2, 2, 2], "north"), doc)
    east = expand_machine(_machine("m", "Test EBF", [0, 0, 0], [2, 2, 2], "east"), doc)
    assert {c.cell for c in north} != {c.cell for c in east}, (
        "an east-facing machine rotates its blocks"
    )


# --------------------------------------------------------------------------------------------------
# texturize_scene - the integration + the principle-6 golden guards
# --------------------------------------------------------------------------------------------------


def test_multiblock_expands_to_many_cubes_not_one_box(dataset: tuple[Path, Path]) -> None:
    """Principle 6: an EBF renders as its constituent blocks, never a single stretched box."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test EBF", [0, 0, 0], [2, 2, 2])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.block_cubes == 5, "one textured cube per constituent block"
    assert len(scene["blocks"]) == 5
    assert scene["machines"][0].get("expanded") is True, "the box is replaced by the cubes"
    assert "Test EBF" in summary.textured_types


def test_interior_coil_texture_distinct_from_casing(dataset: tuple[Path, Path]) -> None:
    """At least one interior block (the coil) bakes a texture distinct from the casing shell."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test EBF", [0, 0, 0], [2, 2, 2])])
    texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider)
    pool = scene["textures"]
    coil_keys = [k for k in pool if "blockcasings5" in k]
    casing_keys = [k for k in pool if "gt.blockcasings|11" in k]
    assert coil_keys
    assert casing_keys
    assert pool[coil_keys[0]] != pool[casing_keys[0]], "coil and casing must bake to different PNGs"


def test_single_block_machine_renders_one_textured_cube(dataset: tuple[Path, Path]) -> None:
    """A machine with no multiblock doc but a manifest display name renders as one textured cube."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Macerator", [3, 0, 3], [1, 1, 1])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.block_cubes == 1
    assert scene["blocks"][0]["cell"] == [3, 0, 3]
    assert "Test Macerator" in summary.textured_types


def test_single_block_machine_base_face_is_tinted(dataset: tuple[Path, Path]) -> None:
    """Golden tint guard: the single-block machine's baked base face is the tint, not neutral grey."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Macerator", [0, 0, 0], [1, 1, 1])])
    texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider)
    key = next(k for k in scene["textures"] if k.startswith("gregtech:gt.blockmachines|5|SOUTH"))
    png = base64.b64decode(scene["textures"][key].split(",", 1)[1])
    r, g, b, _ = _pixel(png)
    assert (r, g, b) == (120, 130, 200), "a dropped RGBA multiply would leave this neutral grey"


def test_icon_name_stability_ebf_casing(dataset: tuple[Path, Path]) -> None:
    """The EBF heat-proof casing resolves the exact expected iconset name (deobf-rename guard)."""
    m = TextureManifest(json.loads(Path(dataset[1]).read_text(encoding="utf-8")))
    layers = m.layers("gregtech:gt.blockcasings", 11, "all")
    assert layers[0]["icon"] == "gregtech:iconsets/MACHINE_HEATPROOFCASING"


def test_undocumented_machine_stays_placeholder_and_fetches_nothing(
    dataset: tuple[Path, Path],
) -> None:
    """A machine with neither a doc nor a manifest name keeps its box and triggers no jar fetch."""
    mb, manifest = dataset
    calls: list[Any] = []

    def spy(paths: Any) -> dict[str, bytes]:
        calls.append(paths)
        return _provider(paths)

    scene = _scene([_machine("m1", "Coke Oven", [0, 0, 0], [3, 3, 3])])
    summary = texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=spy)
    assert summary.block_cubes == 0
    assert scene["machines"][0].get("expanded") is None
    assert calls == [], "no icons needed -> the provider is never called"


def test_missing_dataset_degrades_to_all_placeholder(tmp_path: Path) -> None:
    """No committed dump -> every machine stays a placeholder, nothing raises."""
    scene = _scene([_machine("m1", "Test EBF", [0, 0, 0], [2, 2, 2])])
    summary = texturize_scene(
        scene, multiblocks_dir=tmp_path / "absent", manifest_path=tmp_path / "absent.json"
    )
    assert summary.textured_types == ()
    assert summary.placeholder_types == ("Test EBF",)
    assert scene["blocks"] == []
