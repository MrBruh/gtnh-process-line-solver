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

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset.schema import MultiblockDoc
from gtnh_solver.previewer.bake import bake_layers
from gtnh_solver.previewer.scene import build_scene
from gtnh_solver.previewer.textures import (
    _GT_SIDE_TO_THREE_SLOT,
    TextureManifest,
    expand_machine,
    primary_variant,
    texturize_scene,
)
from gtnh_solver.solver import solve

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
#: A fully-transparent overlay: an active layer that differs in the STACK but composites to nothing,
#: so its bake is byte-identical to idle - the case the byte-level active dedup must drop.
CLEAR = "gregtech:iconsets/OVERLAY_CLEAR"
#: Cable sprites. GT ships these GREYSCALE and the material's whole identity is the RGBA multiply
#: (docs/DOMAIN.md), which is why the route bake applies the tint raw - so these are bright here and
#: a correctly baked cable comes out dark.
WIRE = "gregtech:materialicons/DULL/wire"
INSUL_SMALL = "gregtech:iconsets/INSULATION_SMALL"
INSUL_FULL = "gregtech:iconsets/INSULATION_FULL"

#: A png_provider: white base sprites so a tint multiply shows through as the tint colour, and a
#: half-alpha overlay so compositing is observable.
_ICON_PNG = {
    CASING: _png((200, 200, 200, 255)),
    COIL: _png((255, 255, 255, 255)),
    MACH_SIDE: _png((255, 255, 255, 255)),
    OVERLAY: _png((0, 0, 0, 128)),
    CLEAR: _png((0, 0, 0, 0)),
    WIRE: _png((200, 200, 200, 255)),
    INSUL_SMALL: _png((200, 200, 200, 255)),
    INSUL_FULL: _png((200, 200, 200, 255)),
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
            # Two tiers of a generically named single-block machine, keyed by their GT tier-prefixed
            # in-game names (as the real manifest is), to exercise tier-aware name resolution. There
            # is deliberately no "Elite Test Hammer", so a higher tier must fall back to Basic.
            "gregtech:gt.blockmachines|611": {
                "kind": "mte",
                "display_name": "Basic Test Hammer",
                "sides": {
                    "SOUTH": {
                        "inactive": [{"icon": MACH_SIDE, "rgba": [120, 130, 200, 0], "glow": False}]
                    }
                },
            },
            "gregtech:gt.blockmachines|612": {
                "kind": "mte",
                "display_name": "Advanced Test Hammer",
                "sides": {
                    "SOUTH": {
                        "inactive": [{"icon": MACH_SIDE, "rgba": [120, 130, 200, 0], "glow": False}]
                    }
                },
            },
            # A single-block machine that carries a DISTINCT running skin: its NORTH face gains an
            # overlay when active (so its active bake differs from idle), while its SOUTH face stores
            # an identical active stack (so that face dedupes to one texture). Drives the state-toggle
            # scene contract.
            "gregtech:gt.blockmachines|7": {
                "kind": "mte",
                "display_name": "Test Toggle",
                "sides": {
                    "NORTH": {
                        "inactive": [
                            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 255], "glow": False}
                        ],
                        "active": [
                            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 255], "glow": False},
                            {"icon": OVERLAY, "rgba": [255, 255, 255, 0], "glow": False},
                        ],
                    },
                    "SOUTH": {
                        "inactive": [
                            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 255], "glow": False}
                        ],
                        "active": [
                            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 255], "glow": False}
                        ],
                    },
                },
            },
            # A machine whose active NORTH stack DIFFERS in layers (it adds a fully-transparent
            # overlay) but bakes byte-identical to idle. The byte-level dedup must drop it - no
            # active override, even though the stacks are not equal.
            "gregtech:gt.blockmachines|8": {
                "kind": "mte",
                "display_name": "Test Ghost",
                "sides": {
                    "NORTH": {
                        "inactive": [
                            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 255], "glow": False}
                        ],
                        "active": [
                            {"icon": MACH_SIDE, "rgba": [255, 255, 255, 255], "glow": False},
                            {"icon": CLEAR, "rgba": [255, 255, 255, 255], "glow": False},
                        ],
                    }
                },
            },
            # A machine whose idle NORTH face references an UNFETCHABLE icon (absent from the icons
            # map, so it bakes to nothing) while its active stack differs. The active override must be
            # skipped so it never targets a face that fell back to a placeholder.
            "gregtech:gt.blockmachines|9": {
                "kind": "mte",
                "display_name": "Test Phantom",
                "sides": {
                    "NORTH": {
                        "inactive": [
                            {
                                "icon": "gregtech:iconsets/UNFETCHABLE",
                                "rgba": [0, 0, 0, 0],
                                "glow": False,
                            }
                        ],
                        "active": [
                            {
                                "icon": "gregtech:iconsets/UNFETCHABLE",
                                "rgba": [0, 0, 0, 0],
                                "glow": False,
                            },
                            {"icon": OVERLAY, "rgba": [255, 255, 255, 0], "glow": False},
                        ],
                    }
                },
            },
            # An ordinary isotropic cable: one look for an open end, one for a closed face, both
            # under "all" because the shape comes from geometry rather than from the sprite.
            "gregtech:gt.blockmachines|1247": {
                "kind": "pipe",
                "display_name": "cable.tin.02",
                "pipe": {"thickness": 0.375, "insulated": True, "voltage": 32},
                "sides": {
                    "all": {
                        "open": [
                            {"icon": WIRE, "rgba": [220, 220, 220, 0], "glow": False},
                            {"icon": INSUL_SMALL, "rgba": [64, 64, 64, 0], "glow": False},
                        ],
                        "closed": [{"icon": INSUL_FULL, "rgba": [64, 64, 64, 0], "glow": False}],
                    }
                },
            },
            # A pipe the extractor found NOT isotropic, so it wrote per side and filed a gap. The
            # route lookup must refuse it rather than paint one side's sprite on all six.
            "gregtech:gt.blockmachines|1290": {
                "kind": "pipe",
                "display_name": "cable.lopsided.01",
                "sides": {
                    "NORTH": {
                        "open": [{"icon": WIRE, "rgba": [220, 220, 220, 0], "glow": False}],
                        "closed": [{"icon": INSUL_FULL, "rgba": [64, 64, 64, 0], "glow": False}],
                    }
                },
            },
            # Only one of the two looks. Half a pipe reads as a bug where the flat bar reads as
            # missing data, so this must be refused whole.
            "gregtech:gt.blockmachines|1291": {
                "kind": "pipe",
                "display_name": "cable.halfbaked.01",
                "sides": {
                    "all": {"open": [{"icon": WIRE, "rgba": [220, 220, 220, 0], "glow": False}]}
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
            CLEAR: "assets/gregtech/textures/blocks/iconsets/OVERLAY_CLEAR.png",
            WIRE: "assets/gregtech/textures/blocks/materialicons/DULL/wire.png",
            INSUL_SMALL: "assets/gregtech/textures/blocks/iconsets/INSULATION_SMALL.png",
            INSUL_FULL: "assets/gregtech/textures/blocks/iconsets/INSULATION_FULL.png",
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


def _route(*blocks: str | None) -> dict[str, Any]:
    """A power route with one cell per named block - the shape ``build_scene`` emits."""
    return {
        "netId": "power:LV",
        "commodity": "power",
        "color": "#ffd000",
        "segments": [],
        "terminals": [],
        "cells": [
            {
                "cell": [i, 0, 0],
                "dirs": [[1, 0, 0]],
                "thickness": 2,
                "size": 0.375,
                "block": block,
                "label": "2x tin cable",
            }
            for i, block in enumerate(blocks)
        ],
    }


def _machine(
    mid: str,
    mtype: str,
    cell: list[int],
    size: list[int],
    front: str = "north",
    voltage_tier: str = "LV",
    block_key: str | None = None,
) -> dict[str, Any]:
    return {
        "id": mid,
        "type": mtype,
        "block_key": block_key,
        "cell": cell,
        "size": size,
        "front": front,
        "voltage_tier": voltage_tier,
        "role": "machine",
        "color": "#6ca0dc",
    }


# --------------------------------------------------------------------------------------------------
# bake.py - the Pillow compositor
# --------------------------------------------------------------------------------------------------


def test_bake_applies_rgba_tint_not_neutral() -> None:
    """A white base sprite tinted [120,130,200] bakes a hue-shifted colour, never neutral grey.

    Peak-channel normalisation (max 200) scales the tint to (0.6, 0.65, 1.0), so the white sprite
    bakes to (153, 166, 255): still visibly tinted toward blue, just brighter than the old
    ``/ 255`` result (120, 130, 200) because the tint no longer doubles as a brightness cut.
    """
    baked = bake_layers([{"icon": MACH_SIDE, "rgba": [120, 130, 200, 0], "glow": False}], _ICON_PNG)
    assert baked is not None
    r, g, b, a = _pixel(baked)
    assert (r, g, b) == (153, 166, 255), "tint hue must be applied to the base sprite"
    assert r < b, "the tint must stay visibly hue-shifted (bluer), not neutral grey"
    assert a == 255, "a GT alpha of 0 means opaque, so the baked base stays fully opaque"


def test_bake_dark_neutral_tint_does_not_blacken_sprite() -> None:
    """Regression: a dark-neutral casing tint [32,32,32] must not crush a bright sprite to black.

    A raw ``value / 255`` multiply turned [32,32,32] into ~0.125 and baked a bright casing to mean
    RGB ~20 (near-black). Peak-channel normalisation makes a neutral tint identity, so the bright
    sprite shows through at full brightness instead of blacking out.
    """
    baked = bake_layers([{"icon": CASING, "rgba": [32, 32, 32, 0], "glow": False}], _ICON_PNG)
    assert baked is not None
    r, g, b, _ = _pixel(baked)
    assert (r + g + b) / 3 > 120, "a dark-neutral tint must not crush the sprite to near-black"


def test_bake_fully_black_tint_defaults_to_identity() -> None:
    """A degenerate all-zero tint has no brightest channel to normalise by, so it bakes as identity."""
    baked = bake_layers([{"icon": CASING, "rgba": [0, 0, 0, 0], "glow": False}], _ICON_PNG)
    assert baked is not None
    assert _pixel(baked)[:3] == (200, 200, 200), "an all-zero tint must leave the sprite unchanged"


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


def test_mte_block_resolves_generic_name_by_tier() -> None:
    """A generic plan name plus its voltage tier resolves to the tier-prefixed manifest entry."""
    m = TextureManifest(_manifest_dict())
    assert m.mte_block("Test Hammer", "LV") == ("gregtech:gt.blockmachines", 611)  # Basic
    assert m.mte_block("Test Hammer", "MV") == ("gregtech:gt.blockmachines", 612)  # Advanced


def test_mte_block_unknown_tier_falls_back_to_basic() -> None:
    """A tier without a determinable GT prefix (HV+, or absent) resolves the near-identical Basic skin."""
    m = TextureManifest(_manifest_dict())
    assert m.mte_block("Test Hammer", "HV") == (
        "gregtech:gt.blockmachines",
        611,
    )  # no Elite -> Basic
    assert m.mte_block("Test Hammer", None) == ("gregtech:gt.blockmachines", 611)


def test_mte_block_normalizes_case_and_punctuation() -> None:
    """Matching tolerates case, punctuation, and whitespace between the plan and the manifest name."""
    m = TextureManifest(_manifest_dict())
    assert m.mte_block("  basic   test-hammer ") == ("gregtech:gt.blockmachines", 611)
    assert m.mte_block("test hammer", "MV") == ("gregtech:gt.blockmachines", 612)


def test_mte_block_unknown_machine_stays_unresolved() -> None:
    """A genuinely unknown machine resolves to None (kept on the placeholder fallback, never mis-mapped)."""
    m = TextureManifest(_manifest_dict())
    assert m.mte_block("Coke Oven", "LV") is None
    assert m.mte_block("Coke Oven", "MV") is None


def _mte_names(*names: str) -> TextureManifest:
    """A manifest of just MTE display names (each its own block+meta), for name-resolution tests."""
    blocks = {
        f"gregtech:gt.blockmachines|{700 + i}": {"kind": "mte", "display_name": n, "sides": {}}
        for i, n in enumerate(names)
    }
    return TextureManifest({"blocks": blocks, "icons": {}})


def test_mte_block_tiered_storage_resolves_to_lowest_variant() -> None:
    """A generic tiered-storage name (Super Tank) resolves to its lowest numeral variant."""
    m = _mte_names("Super Tank III", "Super Tank I", "Super Tank II")
    assert m.mte_block("Super Tank") == m.mte_block("Super Tank I")  # lowest tier, not III
    assert m.mte_block("Super Chest") is None  # a family that is absent stays unresolved


def test_mte_block_flavor_prefixed_name_resolves() -> None:
    """A generic name that is a whole-word suffix of a flavor-prefixed in-game name resolves to it."""
    m = _mte_names("ExxonMobil Chemical Plant", "Large Chemical Reactor", "Industrial Coke Oven")
    assert m.mte_block("Chemical Plant") == m.mte_block("ExxonMobil Chemical Plant")
    assert m.mte_block("Coke Oven") == m.mte_block("Industrial Coke Oven")
    assert m.mte_block("eactor") is None  # a mid-word suffix must not count (whole word only)


def test_mte_block_flavor_prefix_picks_shortest() -> None:
    """When several flavor-prefixed names share the suffix, the shortest (fewest extra words) wins."""
    m = _mte_names("Industrial Coke Oven", "Super Duper Industrial Coke Oven")
    assert m.mte_block("Coke Oven") == m.mte_block("Industrial Coke Oven")


def test_mte_block_tier_prefix_wins_over_tiered_and_flavor() -> None:
    """A voltage-tier-prefix hit resolves before the tiered/flavor fallbacks are tried."""
    m = _mte_names("Basic Forge Hammer", "Forge Hammer II", "Deluxe Forge Hammer")
    assert m.mte_block("Forge Hammer", "LV") == m.mte_block("Basic Forge Hammer")


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


def _bar_doc(length: int = 3) -> dict[str, Any]:
    """A non-cubic ``length``x1x1 bar of casings, so a yaw would spill it past a reserved footprint."""
    return {
        "schema": 1,
        "controller": {
            "registry_name": "gregtech:gt.blockmachines",
            "meta": 1,
            "display_name": "Bar",
            "source_class": "C",
            "facing_convention": "",
        },
        "variants": [
            {
                "trigger_stack_size": 1,
                "channels": {},
                "blocks": [
                    {"d": [i, 0, 0], "block": "gregtech:gt.blockcasings", "meta": 11}
                    for i in range(length)
                ],
                "hints": [],
                "bbox": [length, 1, 1],
            }
        ],
        "substitutions": {},
        "failures": [],
    }


def test_cubes_never_spill_outside_the_reserved_footprint() -> None:
    """No cube may fall outside [cell, cell+size): a machine's blocks cannot overlap a neighbour."""
    doc = MultiblockDoc.model_validate(_ebf_doc())
    # Declare a footprint smaller than the structure; the hard clamp drops the out-of-bounds cubes.
    cubes = expand_machine(_machine("m", "Test EBF", cell=[5, 0, 5], size=[1, 2, 2]), doc)
    assert cubes
    for c in cubes:
        assert 5 <= c.cell[0] < 6
        assert 0 <= c.cell[1] < 2
        assert 5 <= c.cell[2] < 7


def test_yaw_rotates_the_blocks_inside_the_rotated_reserved_box() -> None:
    """A yawed non-cubic machine renders turned, because the reserved box turns with it.

    The scene now emits ``size`` as the box AS PLACED (``scene._reserved_size``), so an east-facing
    3x1x1 bar reserves 1x1x3 and its blocks run along z. There is no spill and therefore no
    fall-back to the native orientation: the old behaviour drew the bar unturned, which was a
    picture of a build the solver had not reserved space for.
    """
    doc = MultiblockDoc.model_validate(_bar_doc(3))
    cubes = expand_machine(_machine("m", "Bar", cell=[0, 0, 0], size=[1, 1, 3], front="east"), doc)
    assert len(cubes) == 3, "all three blocks survive the clamp"
    for c in cubes:
        assert c.cell[0] == 0
        assert c.cell[1] == 0
        assert 0 <= c.cell[2] < 3


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


def test_generic_single_block_machine_textures_via_tier(dataset: tuple[Path, Path]) -> None:
    """A generically named 1x1x1 machine ("Test Hammer" at LV) textures via tier-prefixed resolution."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Hammer", [4, 0, 4], [1, 1, 1], voltage_tier="LV")])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.block_cubes == 1
    assert scene["blocks"][0]["block"] == "gregtech:gt.blockmachines"
    assert scene["blocks"][0]["meta"] == 611  # Basic Test Hammer, resolved from generic name + LV
    assert "Test Hammer" in summary.textured_types


def test_storage_glyph_faces_auto_output_direction(dataset: tuple[Path, Path]) -> None:
    """A boundary-storage block auto-outputs from its front, so its output glyph rotates to face the
    auto-output direction (EAST here), not the placer's default 'north' - the Super Tank/Chest fix."""
    mb, manifest = dataset
    scene = {
        "version": 1,
        "machines": [{**_machine("s1", "Test Macerator", [0, 0, 0], [1, 1, 1]), "role": "storage"}],
        "autoConnections": [
            {
                "netId": "n",
                "source": "s1",
                "target": "x",
                "sourceFace": "east",
                "targetFace": "west",
            }
        ],
    }
    texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider)
    # the world EAST face now samples the machine's NORTH glyph, so the glyph points where it ejects
    assert (
        scene["blocks"][0]["texture"][_GT_SIDE_TO_THREE_SLOT[5]]
        == "gregtech:gt.blockmachines|5|NORTH|inactive"
    )


def test_non_storage_glyph_keeps_placed_front(dataset: tuple[Path, Path]) -> None:
    """A non-storage machine ignores the auto-output face: its front glyph stays on its placed front,
    so only Super Tank/Chest-style storage blocks are reoriented."""
    mb, manifest = dataset
    scene = {
        "version": 1,
        "machines": [_machine("m1", "Test Macerator", [0, 0, 0], [1, 1, 1])],  # role 'machine'
        "autoConnections": [
            {
                "netId": "n",
                "source": "m1",
                "target": "x",
                "sourceFace": "east",
                "targetFace": "west",
            }
        ],
    }
    texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider)
    block = scene["blocks"][0]
    assert (
        block["texture"][
            _GT_SIDE_TO_THREE_SLOT[2]
        ]  # world NORTH (the placed front) keeps the glyph
        == "gregtech:gt.blockmachines|5|NORTH|inactive"
    )
    assert block["texture"][_GT_SIDE_TO_THREE_SLOT[5]] is None  # nothing rotated onto EAST


def _tower_doc() -> MultiblockDoc:
    """A two-form parametric tower: 3x3x3 and 3x5x3, so a size choice is observable."""
    variants = []
    for h in (3, 5):
        variants.append(
            {
                "trigger_stack_size": h,
                "channels": {},
                "blocks": [
                    {"d": [x, y, z], "block": "gregtech:gt.blockcasings", "meta": 11}
                    for y in range(h)
                    for x in range(3)
                    for z in range(3)
                ],
                "hints": [],
                "bbox": [3, h, 3],
            }
        )
    return MultiblockDoc.model_validate(
        {
            "schema": 2,
            "controller": {
                "registry_name": "gregtech:gt.blockmachines",
                "meta": 1126,
                "display_name": "Test Tower",
                "source_class": "test.MTETestTower",
            },
            "variants": variants,
        }
    )


def test_expansion_renders_the_variant_the_adapter_reserved() -> None:
    """The rendered form must match the reserved footprint, not simply the largest form.

    ``expand_machine`` clamps to the reserved box, so picking the taller form for a machine reserved
    at 3x3x3 would silently draw a truncated tower instead of failing.
    """
    doc = _tower_doc()
    short = expand_machine(_machine("m1", "Test Tower", [0, 0, 0], [3, 3, 3]), doc)
    tall = expand_machine(_machine("m2", "Test Tower", [0, 0, 0], [3, 5, 3]), doc)
    assert len(short) == 27  # 3x3x3, the small form rendered whole
    assert len(tall) == 45  # 3x5x3
    assert max(c.cell[1] for c in short) == 2  # and the short one is genuinely 3 tall


def test_expansion_falls_back_to_the_largest_form_for_an_unmatched_size() -> None:
    # A size no variant reports (a machine reserved before selection existed) keeps the old choice.
    cubes = expand_machine(_machine("m1", "Test Tower", [0, 0, 0], [3, 9, 3]), _tower_doc())
    assert len(cubes) == 45  # the 3x5x3 form, clamped into the larger reservation


def test_block_key_expands_a_machine_whose_type_does_not_match(dataset: tuple[Path, Path]) -> None:
    """The GT++ case (GitHub #98): the plan's type is the recipe-map name, so only the key matches.

    Mirrors ``PhysicalDataset.get``'s precedence. If these two lookups ever disagreed, the adapter
    would reserve one machine's footprint while the previewer drew another's structure.
    """
    mb, manifest = dataset
    scene = _scene(
        [
            _machine(
                "m1",
                "A Recipe Map Name No Doc Uses",
                [0, 0, 0],
                [2, 2, 2],
                block_key="gregtech:gt.blockmachines@1000",  # the committed "Test EBF" doc
            )
        ]
    )
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.block_cubes == 5  # the full structure, not a placeholder
    assert scene["machines"][0]["expanded"] is True


def test_a_block_with_no_sprite_is_reported_not_swallowed(dataset: tuple[Path, Path]) -> None:
    """A constituent block with no manifest entry renders grey; the summary must say which.

    Nothing else surfaces it: the machine is still flagged ``expanded``, so it keeps no placeholder
    label, and a grey cube inside a multiblock is indistinguishable from a deliberately plain casing.
    That is how the tiered machine casings stayed invisibly broken (GitHub #98).
    """
    mb, manifest = dataset
    # A doc placing a casing the manifest has never heard of - the real shape of the GT casing
    # families whose icons are unreachable server-side.
    doc = _ebf_doc()
    doc["controller"]["display_name"] = "Test Unskinned"
    doc["variants"][0]["blocks"].append(
        {"d": [1, 1, 0], "block": "gregtech:gt.blockcasings8", "meta": 0}
    )
    doc["variants"][0]["bbox"] = [2, 2, 2]
    (mb / "test_unskinned.json").write_text(json.dumps(doc), encoding="utf-8")

    scene = _scene([_machine("m1", "Test Unskinned", [0, 0, 0], [2, 2, 2])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert "gregtech:gt.blockcasings8|0" in summary.unskinned_blocks
    assert summary.block_cubes  # the structure still renders; only that one sprite is missing


def test_a_fully_skinned_machine_reports_nothing_unskinned(dataset: tuple[Path, Path]) -> None:
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Macerator", [0, 0, 0], [1, 1, 1])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.unskinned_blocks == ()


def test_block_key_miss_falls_back_to_the_type(dataset: tuple[Path, Path]) -> None:
    # A block key the dump lacks must not shadow a type that resolves - the dump is partial.
    mb, manifest = dataset
    scene = _scene(
        [
            _machine(
                "m1", "Test EBF", [0, 0, 0], [2, 2, 2], block_key="gregtech:gt.blockmachines@404"
            )
        ]
    )
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.block_cubes == 5


def test_docless_multiblock_keeps_placeholder_not_a_lone_cube(dataset: tuple[Path, Path]) -> None:
    """A doc-less MULTIblock must not collapse to one controller cube (the Distillation Tower case)."""
    mb, manifest = dataset
    # "Test Macerator" is an MTE in the manifest, but here the machine is 3x3x3: a multiblock whose
    # structure was not dumped. It must stay a placeholder box, not masquerade as a one-block machine.
    scene = _scene([_machine("m1", "Test Macerator", [0, 0, 0], [3, 3, 3])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert summary.block_cubes == 0
    assert scene["machines"][0].get("expanded") is None
    assert "Test Macerator" in summary.placeholder_types


def test_single_block_machine_base_face_is_tinted(dataset: tuple[Path, Path]) -> None:
    """Golden tint guard: the single-block machine's baked base face is hue-tinted, not neutral grey."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Macerator", [0, 0, 0], [1, 1, 1])])
    texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider)
    key = next(k for k in scene["textures"] if k.startswith("gregtech:gt.blockmachines|5|SOUTH"))
    png = base64.b64decode(scene["textures"][key].split(",", 1)[1])
    r, g, b, _ = _pixel(png)
    assert (r, g, b) == (153, 166, 255), "a dropped RGBA multiply would leave this neutral grey"
    assert r < b, "the tint stays visibly hue-shifted, not neutral grey"


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


# --------------------------------------------------------------------------------------------------
# Active / idle state toggle - the scene contract the viewer's control swaps between
# --------------------------------------------------------------------------------------------------


def test_active_state_texture_emitted_only_for_a_differing_face(dataset: tuple[Path, Path]) -> None:
    """A face whose running skin differs carries BOTH an idle and an active bake (and they differ);
    an idle-identical face carries just the one texture (deduped). Idle stays the default pool."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Toggle", [0, 0, 0], [1, 1, 1])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    idle = scene["textures"]
    active = scene["texturesActive"]
    north = next(k for k in idle if k.startswith("gregtech:gt.blockmachines|7|NORTH"))
    south = next(k for k in idle if k.startswith("gregtech:gt.blockmachines|7|SOUTH"))
    # the NORTH face gains an _ACTIVE-style overlay when running: both bakes emitted, and distinct
    assert north in active
    assert active[north] != idle[north]
    # the SOUTH face looks the same at rest and running: only the idle bake survives (deduped away)
    assert south in idle
    assert south not in active
    assert summary.embedded_active_icons == 1


def test_no_active_override_when_every_face_is_idle_identical(dataset: tuple[Path, Path]) -> None:
    """A layout whose machines have no distinct running skin bakes no second texture: the active
    pool is empty (the viewer disables the toggle rather than swapping to identical images)."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test EBF", [0, 0, 0], [2, 2, 2])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert scene["textures"]  # it still textured its faces...
    assert scene["texturesActive"] == {}  # ...but not a single face needs a second running texture
    assert summary.embedded_active_icons == 0


def test_active_dedup_is_by_baked_bytes_not_layer_equality(dataset: tuple[Path, Path]) -> None:
    """An active stack that differs in layers but bakes byte-identical to idle emits no override:
    the dedup compares the baked PNG bytes, not just the layer lists."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Ghost", [0, 0, 0], [1, 1, 1])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    assert any(k.startswith("gregtech:gt.blockmachines|8|NORTH") for k in scene["textures"])
    assert scene["texturesActive"] == {}  # the transparent overlay baked to the same bytes
    assert summary.embedded_active_icons == 0


def test_active_override_skipped_when_idle_face_did_not_bake(dataset: tuple[Path, Path]) -> None:
    """If a face's idle bake failed (its icon was unfetchable) the active override is dropped, so it
    never points at a face that fell back to a neutral placeholder."""
    mb, manifest = dataset
    scene = _scene([_machine("m1", "Test Phantom", [0, 0, 0], [1, 1, 1])])
    summary = texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )
    # the idle NORTH face never baked, so it carries no idle texture...
    assert not any(k.startswith("gregtech:gt.blockmachines|9|NORTH") for k in scene["textures"])
    assert scene["texturesActive"] == {}  # ...and no orphan active override is emitted for it
    assert summary.embedded_active_icons == 0


# --------------------------------------------------------------------------------------------------
# Committed-manifest golden guard - basic single-block machine front overlays (issue #3)
# --------------------------------------------------------------------------------------------------

_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"


def test_committed_basic_machine_front_carries_overlay() -> None:
    """Golden guard (issue #3): a basic single-block machine's front face in the SHIPPED manifest
    carries its per-machine ``OVERLAY_FRONT`` glyph on top of the casing.

    Basic machines' textured stack is built ``@SideOnly(CLIENT)`` and is null on the dedicated
    server the extractor runs, so the overlay is reconstructed from its deterministic
    ``basicmachines/<folder>/`` asset path. Before the fix these fronts were casing-only (a plain
    steel box); this pins the Basic Forge Hammer (meta 611) so a future extractor change cannot
    silently drop basic-machine overlays again.
    """
    manifest = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    m = TextureManifest(manifest)
    icons = [layer["icon"] for layer in m.layers("gregtech:gt.blockmachines", 611, "NORTH")]

    assert icons, "the Basic Forge Hammer must be present in the committed manifest"
    assert icons[0] == "gregtech:iconsets/MACHINE_LV_SIDE", (
        "the LV steel casing stays the base layer"
    )
    assert "gregtech:basicmachines/hammer/OVERLAY_FRONT" in icons, (
        "the Forge Hammer's front glyph overlay must sit above the casing, not be dropped"
    )
    # the overlay name resolves to its real jar asset path (a deobf-rename / dropped-icon guard).
    assert (
        manifest["icons"]["gregtech:basicmachines/hammer/OVERLAY_FRONT"]
        == "assets/gregtech/textures/blocks/basicmachines/hammer/OVERLAY_FRONT.png"
    )


# --------------------------------------------------------------------------------------------------
# Routes: a cable is a block too (#4)
# --------------------------------------------------------------------------------------------------


def test_bake_applies_a_dark_tint_raw_when_the_sprite_is_greyscale() -> None:
    """A cable's insulation is a [64,64,64] multiply on a GREYSCALE sprite, so the multiply IS its
    colour (docs/DOMAIN.md). The casing normalisation - which turns any neutral tint into identity -
    is right for a tier casing and wrong here twice over: it washes the cable out, and it collapses
    the dark insulated face into the bright open end until the two roles bake to nearly one image.
    """
    layer = [{"icon": INSUL_FULL, "rgba": [64, 64, 64, 0], "glow": False}]
    raw = bake_layers(layer, _ICON_PNG, normalize_tint=False)
    normalized = bake_layers(layer, _ICON_PNG)
    assert raw is not None
    assert normalized is not None
    assert _pixel(raw)[:3] == (50, 50, 50), "GT's own arithmetic: 200 * 64/255"
    assert _pixel(normalized)[:3] == (200, 200, 200), "the casing default is unchanged"


def _texturized(scene: dict[str, Any], dataset: tuple[Path, Path]) -> Any:
    mb, manifest = dataset
    return texturize_scene(
        scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=_provider
    )


def test_route_cells_are_skinned_with_their_cable_sprites(dataset: tuple[Path, Path]) -> None:
    scene = _scene([])
    scene["routes"] = [_route("cable.tin.02", "cable.tin.02")]
    summary = _texturized(scene, dataset)

    assert summary.route_cells_textured == 2
    assert summary.route_cells_flat == 0
    assert summary.unresolved_route_blocks == ()
    tex = scene["routes"][0]["cells"][0]["tex"]
    assert set(tex) == {"open", "closed"}
    assert tex["open"] != tex["closed"], "the two looks must be distinct pool keys"
    assert all(key in scene["textures"] for key in tex.values())
    # Both cells name one block, so they share one pair of bakes rather than baking per cell.
    assert scene["routes"][0]["cells"][1]["tex"] == tex


def test_a_baked_cable_is_dark_insulation_with_a_bright_core(dataset: tuple[Path, Path]) -> None:
    """The end-to-end proof that the raw tint survives the pass: the closed face is uniformly dark
    and the open end carries the wire showing through the insulation's centre. If this ever inverts
    or flattens, cables render as pale grey noodles - visible, plausible and wrong."""
    scene = _scene([])
    scene["routes"] = [_route("cable.tin.02")]
    _texturized(scene, dataset)
    tex = scene["routes"][0]["cells"][0]["tex"]
    closed = _pixel(base64.b64decode(scene["textures"][tex["closed"]].split(",", 1)[1]))
    assert sum(closed[:3]) / 3 < 80, "insulation must stay dark, not normalise to the raw sprite"


def test_an_unknown_route_block_keeps_its_flat_bar_and_is_reported(
    dataset: tuple[Path, Path],
) -> None:
    """The degradation contract, and it differs from a machine's on purpose: a checkerboarded casing
    reads as "no sprite" beside the casings that have one, but a checkerboarded noodle threaded
    through a layout reads as damage. The flat coloured bar is a correct render, which is exactly
    why the gap has to be *reported* - nothing else would say the manifest is short (cf. #98).
    """
    scene = _scene([])
    scene["routes"] = [_route("cable.unobtainium.04")]
    summary = _texturized(scene, dataset)

    assert scene["routes"][0]["cells"][0]["tex"] is None
    assert summary.route_cells_textured == 0
    assert summary.route_cells_flat == 1
    assert summary.unresolved_route_blocks == ("cable.unobtainium.04",)


def test_a_non_isotropic_pipe_is_refused_not_painted_on_six_faces(
    dataset: tuple[Path, Path],
) -> None:
    """The extractor writes a pipe per side when its own isotropy check fails, and files a gap. The
    previewer must not then take one side's sprite for all six - a plausible wrong sprite is the
    failure nothing downstream can detect, where a flat bar is merely plain."""
    scene = _scene([])
    scene["routes"] = [_route("cable.lopsided.01")]
    summary = _texturized(scene, dataset)

    assert scene["routes"][0]["cells"][0]["tex"] is None
    assert summary.unresolved_route_blocks == ("cable.lopsided.01",)


def test_a_pipe_with_only_one_look_is_refused_whole(dataset: tuple[Path, Path]) -> None:
    """Both roles or neither. An open end with no barrel is the render that looks broken, and
    answering "closed" with the open stack would draw a cable open on every face."""
    scene = _scene([])
    scene["routes"] = [_route("cable.halfbaked.01")]
    summary = _texturized(scene, dataset)

    assert scene["routes"][0]["cells"][0]["tex"] is None
    assert summary.unresolved_route_blocks == ("cable.halfbaked.01",)


def test_a_route_with_no_material_is_flat_but_not_a_gap(dataset: tuple[Path, Path]) -> None:
    """``block: None`` means the route published no material at all - a trunk with no single tier,
    or anything above UV. That is a real answer, not a missing manifest entry, so it keeps the flat
    bar without being reported as something to re-dump."""
    scene = _scene([])
    scene["routes"] = [_route(None)]
    summary = _texturized(scene, dataset)

    assert scene["routes"][0]["cells"][0]["tex"] is None
    assert summary.route_cells_flat == 1
    assert summary.unresolved_route_blocks == ()


def test_routes_and_machines_share_one_icon_fetch(dataset: tuple[Path, Path]) -> None:
    """A page with cables in it still makes exactly one jar call - the expensive part of the pass -
    with the cable sprites in the same request as the casings."""
    calls: list[set[str]] = []

    def spy(paths: Any) -> dict[str, bytes]:
        calls.append(set(paths))
        return _provider(paths)

    scene = _scene([_machine("m1", "Test Macerator", [0, 0, 0], [1, 1, 1])])
    scene["routes"] = [_route("cable.tin.02")]
    mb, manifest = dataset
    texturize_scene(scene, multiblocks_dir=mb, manifest_path=manifest, png_provider=spy)

    assert len(calls) == 1
    assert {WIRE, INSUL_SMALL, INSUL_FULL, MACH_SIDE} <= calls[0]


def test_routes_skin_even_with_no_multiblock_dump(tmp_path: Path) -> None:
    """A checkout with a manifest but no structure docs used to texturize nothing at all. Routes
    need no doc - and neither do single-block machines - so only a missing MANIFEST is fatal now."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest_dict()), encoding="utf-8")
    scene = _scene([])
    scene["routes"] = [_route("cable.tin.02")]
    summary = texturize_scene(
        scene,
        multiblocks_dir=tmp_path / "absent",
        manifest_path=manifest,
        png_provider=_provider,
    )
    assert summary.route_cells_textured == 1


def test_the_shipped_lines_texture_every_route_cell_they_draw() -> None:
    """The whole chain on a real artifact, against the committed manifest: solve, build the scene,
    and resolve every route cell's block the way the texture pass does.

    Nitrobenzene is the fixture because it is the only shipped line that lays actual **pipes** -
    sand's item chain all auto-outputs, so it routes power alone. It solves ``PARTIAL_INVALID``
    against the example-scoped dataset, which is deliberately not asserted away: the routes are real
    and drawn either way, and pinning a status here would make this test fail for a reason that has
    nothing to do with textures.
    """
    manifest = TextureManifest.load(_COMMITTED_MANIFEST)
    problem = adapt_file("examples/gtnh-nitrobenzene.json")
    scene = build_scene(problem, solve(problem))

    blocks = {c["block"] for r in scene["routes"] for c in r["cells"]}
    assert blocks, "nitrobenzene routes power and fluids; both must name a block"
    assert any(b.startswith("gt_pipe_") for b in blocks), "the fluid pipes are the point of it"
    assert any(b.startswith("cable.") for b in blocks)
    for name in sorted(blocks):
        found = manifest.pipe_block(name)
        assert found is not None, f"{name} is missing from the committed manifest"
        for role in ("open", "closed"):
            assert manifest.pipe_layers(*found, role), f"{name} has no {role} face"
