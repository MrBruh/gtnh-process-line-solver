"""A machine's tiered parts are drawn, and so exported, from what its node chose (#312).

The previewer swaps each chosen channel's cells in the form itself (``textures.structure_swaps``),
before anything reads the form, so the hatches wear a swapped casing and the controller is drawn
over it exactly as GT draws both. These run against the committed Chemical Plant dump and manifest:
the plant's 92 hatch cells are all solid casing, and its controller is that casing plus overlays.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset import CHEMICAL_PLANT, BlockId, to_physical
from gtnh_solver.dataset.schema import SCHEMA_VERSION, MultiblockDoc
from gtnh_solver.previewer.textures import (
    BlockCube,
    TextureManifest,
    _face_icons,
    _swapped,
    expand_machine,
    face_key,
    load_multiblock_docs,
    structure_swaps,
)

_DATA = Path(__file__).resolve().parents[1] / "data"
_DOCS = load_multiblock_docs(_DATA / "multiblocks")
_PLANT_DOC = _DOCS[CHEMICAL_PLANT]
_MANIFEST = TextureManifest.load(_DATA / "textures" / "manifest.json")

_BRONZE_SOLID: BlockId = ("miscutils:gtplusplus.blockspecialcasings.2", 0)
_STABLE_TITANIUM: BlockId = ("gregtech:gt.blockcasings4", 2)
_LV_CASING: BlockId = ("gregtech:gt.blockcasings", 1)
_HV_CASING: BlockId = ("gregtech:gt.blockcasings", 3)
_STEEL_PIPE: BlockId = ("gregtech:gt.blockcasings2", 13)
_TITANIUM_PIPE: BlockId = ("gregtech:gt.blockcasings2", 14)
_CUPRONICKEL: BlockId = ("gregtech:gt.blockcasings5", 0)
_CONTROLLER: BlockId = ("gregtech:gt.blockmachines", 998)

#: What gtnh-nitrobenzene's plant is built from (tests/test_adapter_structure_blocks.py).
_NITROBENZENE = {"casing": _STABLE_TITANIUM, "machine_casing": _HV_CASING, "pipe": _TITANIUM_PIPE}


def _plant(
    chosen: dict[str, BlockId], hatches: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A scene machine for the plant at the origin facing north, built from ``chosen``."""
    return {
        "id": "plant",
        "type": "Chemical Plant",
        "block_key": CHEMICAL_PLANT,
        "cell": [0, 0, 0],
        "size": [7, 7, 7],
        "front": "north",
        "voltage_tier": "HV",
        "structure_blocks": {channel: list(block) for channel, block in chosen.items()},
        "hatches": hatches or [],
    }


def _histogram(cubes: list[BlockCube]) -> Counter[BlockId]:
    return Counter((c.block, c.meta) for c in cubes)


def _controller(cubes: list[BlockCube]) -> BlockCube:
    return next(c for c in cubes if (c.block, c.meta) == _CONTROLLER)


def _north_icons(cube: BlockCube) -> list[str]:
    _, stacks = _face_icons(cube, _MANIFEST)
    (idle,) = [idle for key, (idle, _) in stacks.items() if key.split("|")[2] == "NORTH"]
    return [layer["icon"].rsplit("/", 1)[-1] for layer in idle]


# --------------------------------------------------------------------------- the swap


def test_each_chosen_channel_maps_its_dumped_block_to_the_chosen_one() -> None:
    variant = _PLANT_DOC.variants[0]
    assert structure_swaps(_PLANT_DOC, variant, _NITROBENZENE) == {
        _BRONZE_SOLID: _STABLE_TITANIUM,
        _LV_CASING: _HV_CASING,
        _STEEL_PIPE: _TITANIUM_PIPE,
    }


def test_only_the_chosen_channels_cells_change_and_without_a_manifest_too() -> None:
    """The ``.schematic`` export builds these same cubes, so the swap cannot wait for a manifest."""
    before = _histogram(expand_machine(_plant({}), _PLANT_DOC))
    after = _histogram(expand_machine(_plant(_NITROBENZENE), _PLANT_DOC))

    assert before == {
        _BRONZE_SOLID: 92,
        _LV_CASING: 57,
        _CUPRONICKEL: 27,
        _STEEL_PIPE: 18,
        _CONTROLLER: 1,
    }
    assert after == {
        _STABLE_TITANIUM: 92,
        _HV_CASING: 57,
        _CUPRONICKEL: 27,  # no coil chosen: drawn as dumped
        _TITANIUM_PIPE: 18,
        _CONTROLLER: 1,
    }


def test_a_tier_the_dump_never_places_is_drawn() -> None:
    """Bronze pipe casing is valid in game and absent from the dump (``dataset.channel_blocks``)."""
    bronze_pipe = ("gregtech:gt.blockcasings2", 12)
    cubes = expand_machine(_plant({"pipe": bronze_pipe}), _PLANT_DOC)
    assert _histogram(cubes)[bronze_pipe] == 18


def test_swaps_apply_all_at_once_and_never_chain() -> None:
    """A cell takes the target of the block it was dumped as: with a -> b and b -> c, the cells
    dumped as a end up b, not c."""
    a, b, c = ("x:a", 0), ("x:b", 0), ("x:c", 0)
    dumped = _PLANT_DOC.variants[0]
    variant = dumped.model_copy(
        update={
            "blocks": [
                block.model_copy(update={"block": (a if i % 2 else b)[0], "meta": 0})
                for i, block in enumerate(dumped.blocks)
            ]
        }
    )
    swapped = _swapped(variant, {a: b, b: c})
    assert [(block.block, block.meta) for block in swapped.blocks] == [
        b if i % 2 else c for i in range(len(dumped.blocks))
    ]


def test_a_block_the_channel_does_not_accept_is_ignored_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PTFE pipe casing is a real block, but not one the plant's ``check()`` accepts; only an IR
    adapted against another dump could ask for it."""
    with caplog.at_level(logging.WARNING, logger="gtnh_solver.previewer.textures"):
        swaps = structure_swaps(
            _PLANT_DOC, _PLANT_DOC.variants[0], {"pipe": ("gregtech:gt.blockcasings8", 1)}
        )
    assert swaps == {}
    assert "'pipe' channel does not accept gregtech:gt.blockcasings8@1" in caplog.text


def test_a_channel_the_dump_does_not_record_is_ignored() -> None:
    assert structure_swaps(_PLANT_DOC, _PLANT_DOC.variants[0], {"glass": ("x:glass", 0)}) == {}


def _two_form_oven() -> MultiblockDoc:
    """A stack-built oven whose form N carries coil N, as each Industrial Coke Oven form does: a
    channel's first entry names the coil of one form only."""

    def form(stack: int) -> dict[str, Any]:
        length = 2 + stack
        return {
            "trigger_stack_size": stack,
            "blocks": [
                {"d": [0, 0, 0], "block": "gregtech:gt.blockmachines", "meta": 15543},
                *(
                    {"d": [x, 0, 0], "block": "gregtech:gt.blockcasings5", "meta": stack - 1}
                    for x in range(1, length)
                ),
            ],
            "bbox": [length, 1, 1],
        }

    return MultiblockDoc.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "controller": {
                "registry_name": "gregtech:gt.blockmachines",
                "meta": 15543,
                "display_name": "Industrial Coke Oven",
                "source_class": "MTEIndustrialCokeOven",
            },
            "variants": [form(1), form(2)],
            "substitutions": {
                "coil": [
                    {"channel_value": n + 1, "block": "gregtech:gt.blockcasings5", "meta": n}
                    for n in range(4)
                ]
            },
        }
    )


def test_cells_are_matched_by_membership_on_every_form() -> None:
    doc = _two_form_oven()
    second = next(v for v in doc.variants if v.trigger_stack_size == 2)
    nichrome = ("gregtech:gt.blockcasings5", 2)

    assert structure_swaps(doc, second, {"coil": nichrome}) == {
        ("gregtech:gt.blockcasings5", 1): nichrome
    }
    oven = {
        "cell": [0, 0, 0],
        "size": [4, 1, 1],
        "front": "north",
        "structure_blocks": {"coil": list(nichrome)},
    }
    assert _histogram(expand_machine(oven, doc))[nichrome] == 3
    assert to_physical(doc).coil_layer_count == 1


# --------------------------------------------------------------------------- what wears it


def _first_slot_cell() -> list[int]:
    offset = to_physical(_PLANT_DOC).variants[0].slots[0].offset
    return [offset.x, offset.y, offset.z]


def test_a_hatch_wears_the_swapped_casing() -> None:
    hatch = {"cell": _first_slot_cell(), "kind": "InputBus", "facing": "down", "port": "in"}
    cubes = expand_machine(_plant(_NITROBENZENE, [hatch]), _PLANT_DOC, _MANIFEST)
    (bus,) = [c for c in cubes if c.facing is not None]
    assert bus.casing == _STABLE_TITANIUM


def test_the_controller_is_drawn_over_the_swapped_casing_with_its_overlays() -> None:
    """``GTPPMultiBlockBase.getTexture``: the casing on every side, the overlays on the front. The
    dump drew it over Bronze plated bricks, the default casing."""
    plain = _controller(expand_machine(_plant({}), _PLANT_DOC, _MANIFEST))
    recased = _controller(expand_machine(_plant(_NITROBENZENE), _PLANT_DOC, _MANIFEST))

    assert _north_icons(plain) == [
        "MACHINE_BRONZEPLATEDBRICKS",
        "chemicalPlant",
        "chemicalPlantGlow",
    ]
    assert recased.rebase == (_BRONZE_SOLID, _STABLE_TITANIUM)
    assert _north_icons(recased) == [
        "MACHINE_CASING_STABLE_TITANIUM",
        "chemicalPlant",
        "chemicalPlantGlow",
    ]
    faces, _ = _face_icons(recased, _MANIFEST)
    assert all(key is not None and key.endswith("|gregtech:gt.blockcasings4|2") for key in faces)


def test_a_coil_only_swap_leaves_the_controller_alone() -> None:
    cubes = expand_machine(
        _plant({"coil": ("gregtech:gt.blockcasings5", 4)}), _PLANT_DOC, _MANIFEST
    )
    controller = _controller(cubes)
    assert controller.rebase is None
    assert _north_icons(controller)[0] == "MACHINE_BRONZEPLATEDBRICKS"


def test_a_face_not_drawn_over_the_old_casing_is_left_alone() -> None:
    cube = BlockCube(
        cell=(0, 0, 0),
        block=_CONTROLLER[0],
        meta=_CONTROLLER[1],
        steps=0,
        rebase=(_CUPRONICKEL, _STABLE_TITANIUM),
    )
    assert _north_icons(cube)[0] == "MACHINE_BRONZEPLATEDBRICKS"


def test_two_plants_on_different_casings_bake_distinct_faces() -> None:
    def controller(target: BlockId) -> BlockCube:
        return BlockCube(
            cell=(0, 0, 0),
            block=_CONTROLLER[0],
            meta=_CONTROLLER[1],
            steps=0,
            rebase=(_BRONZE_SOLID, target),
        )

    titanium = controller(_STABLE_TITANIUM)
    tungstensteel = controller(("gregtech:gt.blockcasings4", 0))
    plain = BlockCube(cell=(0, 0, 0), block=_CONTROLLER[0], meta=_CONTROLLER[1], steps=0)
    keys = {face_key(cube, "NORTH") for cube in (titanium, tungstensteel, plain)}
    assert len(keys) == 3
