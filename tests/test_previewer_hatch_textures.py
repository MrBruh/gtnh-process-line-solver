"""Hatches render as real GT hatch blocks, at their own facing and in their multiblock's casing.

Three things here are the correctness of the whole lane, and all three fail silently rather than
loudly:

1. **The splice.** The extractor pins ``aFacing`` to NORTH for every MTE, so the front overlays are
   only ever recorded on that one side, and ``_rotate_side`` could not express a vertical facing at
   all. A face is therefore composed from a background plus NORTH's overlays.
2. **The background.** GT re-skins a hatch to the controller's casing when the multiblock forms
   (``MTEHatch.getTexture`` reads ``casingTexturePages``), and the dump captures hatches standing
   alone, so the recorded background is the *unattached* look (GitHub #109). Left alone it draws an
   input bus on an Industrial Coke Oven in HV machine casing. Where the casing is unknown the face
   falls back to the target side's OWN layer 0, which is wrong on every hatch in the pack if read
   off a fixed side instead, since UP and DOWN carry ``_TOP``/``_BOTTOM`` against ``_SIDE``.
3. **The pool key.** Two hatches of one type facing different ways, or sitting on different
   multiblocks, must not collide in the texture pool, or one silently gets the other's bake.
"""

from __future__ import annotations

from typing import Any

from gtnh_solver.previewer.textures import (
    BlockCube,
    TextureManifest,
    _face_icons,
    expand_machine,
)

_SIDE = "gregtech:iconsets/MACHINE_HV_SIDE"
_TOP = "gregtech:iconsets/MACHINE_HV_TOP"
_BOTTOM = "gregtech:iconsets/MACHINE_HV_BOTTOM"
_PIPE_IN = "gregtech:iconsets/OVERLAY_PIPE_IN"
_ITEM_IN = "gregtech:iconsets/ITEM_IN_SIGN"
_MAINT = "gregtech:iconsets/OVERLAY_MAINTENANCE"
_TAPE = "gregtech:iconsets/OVERLAY_DUCTTAPE"

_CASING = "gregtech:iconsets/MACHINE_CASING_CHEMICALLY_INERT"
_OTHER_CASING = "gregtech:iconsets/MACHINE_HEATPROOFCASING"
_COIL = "gregtech:iconsets/BLOCK_COIL_CUPRONICKEL"

_INPUT_BUS = "gregtech.api.metatileentity.implementations.MTEHatchInputBus"
_ENERGY = "gregtech.api.metatileentity.implementations.MTEHatchEnergy"
_MAINTENANCE = "gregtech.api.metatileentity.implementations.MTEHatchMaintenance"
_MUFFLER = "gregtech.api.metatileentity.implementations.MTEHatchMuffler"


def _layer(icon: str) -> dict[str, Any]:
    return {"icon": icon, "rgba": [255, 255, 255, 255], "glow": False}


def _hatch_entry(name: str, source: str, *, front: list[str]) -> dict[str, Any]:
    """A hatch as the real dump records one: one background layer per side, overlays on NORTH."""
    plain = {"inactive": [_layer(_SIDE)], "active": [_layer(_SIDE)]}
    return {
        "kind": "mte",
        "display_name": name,
        "source_class": source,
        "sides": {
            "NORTH": {
                "inactive": [_layer(_SIDE), *(_layer(i) for i in front)],
                "active": [_layer(_SIDE), *(_layer(i) for i in front)],
            },
            "SOUTH": plain,
            "EAST": plain,
            "WEST": plain,
            "UP": {"inactive": [_layer(_TOP)], "active": [_layer(_TOP)]},
            "DOWN": {"inactive": [_layer(_BOTTOM)], "active": [_layer(_BOTTOM)]},
        },
    }


def _casing_entry(icon: str) -> dict[str, Any]:
    """A multiblock casing as the dump records one: a single isotropic layer under ``sides.all``."""
    return {"kind": "block", "sides": {"all": {"inactive": [_layer(icon)]}}}


def _manifest() -> TextureManifest:
    return TextureManifest(
        {
            "schema": 2,
            "blocks": {
                "gregtech:gt.blockcasings|0": _casing_entry(_CASING),
                "gregtech:gt.blockcasings|11": _casing_entry(_OTHER_CASING),
                "gregtech:gt.blockcasings5|0": _casing_entry(_COIL),
                "gregtech:gt.blockmachines|71": _hatch_entry(
                    "Input Bus (LV)", _INPUT_BUS, front=[_PIPE_IN, _ITEM_IN]
                ),
                "gregtech:gt.blockmachines|73": _hatch_entry(
                    "Input Bus (HV)", _INPUT_BUS, front=[_PIPE_IN, _ITEM_IN]
                ),
                "gregtech:gt.blockmachines|41": _hatch_entry(
                    "LV Energy Hatch", _ENERGY, front=[_PIPE_IN]
                ),
                "gregtech:gt.blockmachines|91": _hatch_entry(
                    "Muffler Hatch (LV)", _MUFFLER, front=[_PIPE_IN]
                ),
                # The maintenance hatch's two states are INVERTED in the dump, exactly as GT
                # records them: inactive carries the duct tape, meaning broken.
                "gregtech:gt.blockmachines|90": {
                    "kind": "mte",
                    "display_name": "Maintenance Hatch",
                    "source_class": _MAINTENANCE,
                    "sides": {
                        "NORTH": {
                            "inactive": [_layer(_SIDE), _layer(_MAINT), _layer(_TAPE)],
                            "active": [_layer(_SIDE), _layer(_MAINT)],
                        },
                        "WEST": {"inactive": [_layer(_SIDE)], "active": [_layer(_SIDE)]},
                        "UP": {"inactive": [_layer(_TOP)], "active": [_layer(_TOP)]},
                    },
                },
                # Shares MTEHatchMaintenance and has a longer name, so the plain hatch must win.
                "gregtech:gt.blockmachines|111": _hatch_entry(
                    "Auto Maintenance Hatch", _MAINTENANCE, front=[_MAINT]
                ),
            },
            "icons": {},
        }
    )


def _icons(stack: list[dict[str, Any]]) -> list[str]:
    return [layer["icon"].rsplit("/", 1)[-1] for layer in stack]


# ----------------------------------------------------------------------- resolving the block


def test_a_hatch_kind_and_tier_resolve_to_the_gt_block() -> None:
    m = _manifest()
    assert m.hatch_block("InputBus", "HV") == ("gregtech:gt.blockmachines", 73)
    assert m.hatch_block("InputBus", "LV") == ("gregtech:gt.blockmachines", 71)
    assert m.hatch_block("Energy", "LV") == ("gregtech:gt.blockmachines", 41)


def test_an_untiered_family_resolves_at_any_tier_and_prefers_the_plain_block() -> None:
    # Both maintenance hatches share MTEHatchMaintenance and neither name carries a tier. The
    # shortest name wins, which is the plain hatch rather than the LuV-gated automatic one.
    m = _manifest()
    assert m.hatch_block("Maintenance", "HV") == ("gregtech:gt.blockmachines", 90)
    assert m.hatch_block("Maintenance", None) == ("gregtech:gt.blockmachines", 90)


def test_a_tier_above_a_familys_ceiling_falls_back_to_the_highest_below_it() -> None:
    # GT does not tier every family the whole way up - the muffler stops at UHV - and a machine
    # above the ceiling must still get a skin rather than lose its cube.
    m = _manifest()
    assert m.hatch_block("Muffler", "UV") == ("gregtech:gt.blockmachines", 91)
    assert m.hatch_block("InputBus", "UV") == (
        "gregtech:gt.blockmachines",
        73,
    )  # the highest there is
    assert m.hatch_block("Dynamo", "LV") is None  # nothing of that kind at all


# ------------------------------------------------------------------------------- the splice


def test_a_hatch_wears_its_overlays_only_on_the_face_it_actually_faces() -> None:
    cube = BlockCube((0, 0, 0), "gregtech:gt.blockmachines", 73, steps=0, facing="WEST")
    _, stacks = _face_icons(cube, _manifest())
    by_side = {key.split("|")[2]: idle for key, (idle, _) in stacks.items()}

    assert _icons(by_side["WEST"]) == ["MACHINE_HV_SIDE", "OVERLAY_PIPE_IN", "ITEM_IN_SIGN"]
    assert _icons(by_side["EAST"]) == ["MACHINE_HV_SIDE"]  # background only
    assert _icons(by_side["NORTH"]) == ["MACHINE_HV_SIDE"]  # the DUMP's front, but not this one's


def test_a_vertical_facing_keeps_its_own_top_background_under_the_overlays() -> None:
    # The whole reason the splice takes layer 0 from the target side. UP and DOWN carry
    # _TOP/_BOTTOM against the horizontals' _SIDE in every hatch in the pack, so composing an
    # up-facing hatch from a horizontal background would be wrong on all of them. 75% of the sand
    # line's terminals are vertical, so this is the common case, not an edge one.
    cube = BlockCube((0, 0, 0), "gregtech:gt.blockmachines", 73, steps=0, facing="UP")
    _, stacks = _face_icons(cube, _manifest())
    by_side = {key.split("|")[2]: idle for key, (idle, _) in stacks.items()}

    assert _icons(by_side["UP"]) == ["MACHINE_HV_TOP", "OVERLAY_PIPE_IN", "ITEM_IN_SIGN"]
    assert _icons(by_side["DOWN"]) == ["MACHINE_HV_BOTTOM"]
    assert _icons(by_side["NORTH"]) == ["MACHINE_HV_SIDE"]


def test_a_hatch_ignores_the_machines_yaw() -> None:
    # An ordinary structure block reads its texture through the machine's rotation; a hatch has a
    # facing of its own and must not be turned twice.
    manifest = _manifest()
    turned = BlockCube((0, 0, 0), "gregtech:gt.blockmachines", 73, steps=3, facing="WEST")
    straight = BlockCube((0, 0, 0), "gregtech:gt.blockmachines", 73, steps=0, facing="WEST")
    assert _face_icons(turned, manifest) == _face_icons(straight, manifest)


def test_two_hatches_of_one_type_facing_differently_get_different_pool_keys() -> None:
    # The one place this change can go quietly wrong: without the facing in the key, the pool
    # dedupe would hand a WEST-facing bus the UP-facing bus's bake.
    manifest = _manifest()
    west, _ = _face_icons(
        BlockCube((0, 0, 0), "gregtech:gt.blockmachines", 73, steps=0, facing="WEST"), manifest
    )
    up, _ = _face_icons(
        BlockCube((0, 0, 0), "gregtech:gt.blockmachines", 73, steps=0, facing="UP"), manifest
    )
    assert {k for k in west if k} & {k for k in up if k} == set()
    assert all(k is None or k.endswith("|WEST") for k in west)
    assert all(k is None or k.endswith("|UP") for k in up)


def test_a_maintenance_hatch_is_not_drawn_duct_taped() -> None:
    # GT flips it to active the moment it joins a formed multiblock, so the dumped "inactive"
    # stack - MAINTENANCE + DUCTTAPE - is the BROKEN look. Reading the states straight would draw
    # every machine in the line as needing repair.
    cube = BlockCube(
        (0, 0, 0),
        "gregtech:gt.blockmachines",
        90,
        steps=0,
        facing="WEST",
        idle_state="active",
        active_state="active",
    )
    _, stacks = _face_icons(cube, _manifest())
    (idle, running) = next(v for k, v in stacks.items() if k.split("|")[2] == "WEST")
    assert _icons(idle) == ["MACHINE_HV_SIDE", "OVERLAY_MAINTENANCE"]
    assert "OVERLAY_DUCTTAPE" not in _icons(idle)
    assert running == idle  # and no running override that would reintroduce the tape


# --------------------------------------------------------------- substituting into the structure


def _casing_doc(meta: int = 0) -> Any:
    from gtnh_solver.dataset.schema import MultiblockDoc

    return MultiblockDoc.model_validate(
        {
            "schema": 2,
            "controller": {
                "registry_name": "gregtech:gt.blockmachines",
                "meta": 1000,
                "display_name": "Test Box",
                "source_class": "x",
                "facing_convention": "controller front = NORTH",
            },
            "variants": [
                {
                    "trigger_stack_size": 1,
                    "bbox": [2, 1, 1],
                    "blocks": [
                        {"d": [0, 0, 0], "block": "gregtech:gt.blockcasings", "meta": meta},
                        {"d": [1, 0, 0], "block": "gregtech:gt.blockcasings", "meta": meta},
                    ],
                }
            ],
        }
    )


def _scene_machine(hatches: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "m",
        "type": "Test Box",
        "block_key": None,
        "cell": [0, 0, 0],
        "size": [2, 1, 1],
        "front": "north",
        "voltage_tier": "HV",
        "role": "machine",
        "color": "#fff",
        "hatches": hatches,
    }


def test_a_hatch_replaces_the_casing_cell_it_occupies_rather_than_adding_a_block() -> None:
    # A hatch IS one of the structure's own cells built as something else - which is exactly why it
    # spends the casing budget. Adding a cube instead would double-occupy the cell.
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    cubes = expand_machine(machine, _casing_doc(), _manifest())

    assert len(cubes) == 2  # same cell count as the bare structure
    by_cell = {c.cell: c for c in cubes}
    assert by_cell[(0, 0, 0)].block == "gregtech:gt.blockcasings"  # untouched
    swapped = by_cell[(1, 0, 0)]
    assert (swapped.block, swapped.meta) == ("gregtech:gt.blockmachines", 73)  # the HV bus
    assert swapped.facing == "EAST"
    assert swapped.steps == 0


def test_an_unresolvable_hatch_leaves_the_casing_rather_than_a_hole() -> None:
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "Dynamo", "facing": "east", "port": "out"}]
    )
    cubes = expand_machine(machine, _casing_doc(), _manifest())
    assert len(cubes) == 2
    assert all(c.block == "gregtech:gt.blockcasings" for c in cubes)


def test_expanding_without_a_manifest_leaves_the_structure_bare() -> None:
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    cubes = expand_machine(machine, _casing_doc())
    assert [c.block for c in cubes] == ["gregtech:gt.blockcasings"] * 2
    assert all(c.facing is None for c in cubes)
    assert all(c.casing is None for c in cubes)


# --------------------------------------------------------- the casing a formed hatch is re-skinned to


def _hatch_cube(machine: dict[str, Any], doc: Any = None) -> Any:
    """The one hatch cube ``machine`` expands to."""
    cubes = expand_machine(machine, doc or _casing_doc(), _manifest())
    return next(c for c in cubes if c.facing is not None)


def test_a_formed_hatch_wears_the_multiblocks_casing_not_its_standalone_skin() -> None:
    # GitHub #109 part 2, the whole point. GT calls updateTexture(casingIndex) when the hatch joins
    # a formed multiblock, so its background becomes casingTexturePages[...] - the controller's
    # casing - and the MACHINE_<TIER>_SIDE the dump recorded is only the unattached fallback. The
    # structure cell the hatch replaces IS that casing, because a GT hatch element is built
    # `.casingIndex(CASING_INDEX).buildAndChain(ofBlock(CASING, META))` over the same casing.
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    cube = _hatch_cube(machine)
    assert cube.casing == ("gregtech:gt.blockcasings", 0)

    _, stacks = _face_icons(cube, _manifest())
    by_side = {key.split("|")[2]: idle for key, (idle, _) in stacks.items()}
    # The facing side keeps the overlays; every side takes the casing under them.
    assert _icons(by_side["EAST"]) == [
        "MACHINE_CASING_CHEMICALLY_INERT",
        "OVERLAY_PIPE_IN",
        "ITEM_IN_SIGN",
    ]
    assert _icons(by_side["WEST"]) == ["MACHINE_CASING_CHEMICALLY_INERT"]
    # An isotropic casing covers the vertical faces too: GT's copied block texture is one ITexture
    # for the whole block, so the hatch stops showing MACHINE_HV_TOP / _BOTTOM as well.
    assert _icons(by_side["UP"]) == ["MACHINE_CASING_CHEMICALLY_INERT"]
    assert _icons(by_side["DOWN"]) == ["MACHINE_CASING_CHEMICALLY_INERT"]


def test_a_hatch_whose_casing_the_manifest_cannot_skin_keeps_the_standalone_look() -> None:
    # Graceful degradation, and the reason the casing is gated on has_block: an unknown casing must
    # leave a complete hatch behind, not a face with no texture. It falls back per side, so the
    # vertical faces keep _TOP/_BOTTOM rather than a horizontal's _SIDE.
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    cube = _hatch_cube(machine, _casing_doc(meta=7))  # meta 7 is in no manifest entry
    assert cube.casing is None

    _, stacks = _face_icons(cube, _manifest())
    by_side = {key.split("|")[2]: idle for key, (idle, _) in stacks.items()}
    assert _icons(by_side["EAST"]) == ["MACHINE_HV_SIDE", "OVERLAY_PIPE_IN", "ITEM_IN_SIGN"]
    assert _icons(by_side["UP"]) == ["MACHINE_HV_TOP"]
    assert _icons(by_side["DOWN"]) == ["MACHINE_HV_BOTTOM"]


def test_a_hatch_appended_outside_the_structure_carries_no_casing() -> None:
    # _substitute_hatches appends a hatch whose cell the structure does not place, rather than
    # dropping it. There is no casing to inherit there, so it keeps the standalone look.
    machine = _scene_machine(
        [{"cell": [5, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    cube = _hatch_cube(machine)
    assert cube.cell == (5, 0, 0)
    assert cube.casing is None


def test_one_hatch_kind_on_two_multiblocks_gets_two_pool_keys() -> None:
    # The same failure the facing key guards, one level up: a line has one bus kind serving several
    # machines, and each wears its own casing. Without the casing in the key the pool dedupe would
    # paint every input bus in whichever machine baked first.
    manifest = _manifest()
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    inert, _ = _face_icons(_hatch_cube(machine, _casing_doc(meta=0)), manifest)
    heatproof, _ = _face_icons(_hatch_cube(machine, _casing_doc(meta=11)), manifest)

    assert {k for k in inert if k} & {k for k in heatproof if k} == set()
    assert all(k is None or k.endswith("|gregtech:gt.blockcasings|0") for k in inert)
    assert all(k is None or k.endswith("|gregtech:gt.blockcasings|11") for k in heatproof)


def _coil_chain_doc() -> Any:
    """The Large Chemical Reactor's shape: hatch cells mostly casing, one of them a coil.

    Its ``x`` element chains ``activeCoils(..)`` ahead of the casing, so the dump records a coil at
    that cell even though the element's ``casingIndex`` names the casing. Read per cell, a hatch
    landing there would be drawn as coil.
    """
    from gtnh_solver.dataset.schema import MultiblockDoc

    return MultiblockDoc.model_validate(
        {
            "schema": 2,
            "controller": {
                "registry_name": "gregtech:gt.blockmachines",
                "meta": 1169,
                "display_name": "Test LCR",
                "source_class": "x",
                "facing_convention": "controller front = NORTH",
            },
            "variants": [
                {
                    "trigger_stack_size": 1,
                    "bbox": [3, 1, 1],
                    "blocks": [
                        {"d": [0, 0, 0], "block": "gregtech:gt.blockcasings", "meta": 0},
                        {"d": [1, 0, 0], "block": "gregtech:gt.blockcasings", "meta": 0},
                        {"d": [2, 0, 0], "block": "gregtech:gt.blockcasings5", "meta": 0},
                    ],
                    "hatch_slots": [
                        {"d": [0, 0, 0], "kinds": ["InputBus"]},
                        {"d": [1, 0, 0], "kinds": ["InputBus"]},
                        {"d": [2, 0, 0], "kinds": ["InputBus"]},
                    ],
                }
            ],
        }
    )


def test_the_casing_is_the_machines_modal_hatch_cell_not_the_cell_under_the_hatch() -> None:
    # GT gives every hatch on a controller ONE casing index, so this is a per-machine property and
    # the mode over the machine's hatch cells is the estimator for it. The per-cell reading is not
    # merely less robust, it is wrong on a shipped example: nitrobenzene's Large Chemical Reactors
    # have a hatch-capable cell holding a cupronickel coil, and a hatch there came out coil-skinned.
    machine = _scene_machine(
        [{"cell": [2, 0, 0], "kind": "InputBus", "facing": "east", "port": "in"}]
    )
    machine["size"] = [3, 1, 1]
    cube = _hatch_cube(machine, _coil_chain_doc())
    assert cube.cell == (2, 0, 0), "the hatch really is on the coil cell"
    assert cube.casing == ("gregtech:gt.blockcasings", 0)

    _, stacks = _face_icons(cube, _manifest())
    by_side = {key.split("|")[2]: idle for key, (idle, _) in stacks.items()}
    assert _icons(by_side["WEST"]) == ["MACHINE_CASING_CHEMICALLY_INERT"]
    assert "BLOCK_COIL_CUPRONICKEL" not in _icons(by_side["WEST"])


def test_a_maintenance_hatch_takes_the_casing_and_still_is_not_duct_taped() -> None:
    # The two fixes have to compose: the maintenance hatch reads its INVERTED states (so it is not
    # drawn broken) AND takes the multiblock casing, whose own entry stores only "inactive".
    machine = _scene_machine(
        [{"cell": [1, 0, 0], "kind": "Maintenance", "facing": "west", "port": "maint"}]
    )
    cube = _hatch_cube(machine)
    assert (cube.idle_state, cube.active_state) == ("active", "active")

    _, stacks = _face_icons(cube, _manifest())
    (idle, running) = next(v for k, v in stacks.items() if k.split("|")[2] == "WEST")
    assert _icons(idle) == ["MACHINE_CASING_CHEMICALLY_INERT", "OVERLAY_MAINTENANCE"]
    assert running == idle
