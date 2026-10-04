"""Lower a solved layout to a Schematica ``.schematic`` build ghost (GitHub #96).

The pipeline, and why each step exists::

    (InputIR, LayoutResult)
        |  build_scene            the same flattening the previewer consumes, so a schematic and
        |                         a preview can never disagree about what was solved
        v
    scene ---> machine_cubes()    per-block cubes: a multiblock's whole structure, or one cube for
        |                         a single-block machine, each already yaw-rotated and clamped,
        |                         its tiered parts already the blocks the node chose (#312)
        '---> route cells         one cell per cable/pipe block, with the sides it connects on
        |
        v  lower()
    grid[W*H*L] of Cell(block, data, tile)
        |
        v  to_nbt()
    NBT compound  --gzip-->  .schematic

**The Data nibble is not the machine.** For a GT block it selects the *tile entity class*
(``GTMod`` registers ``BaseMetaTileEntity`` for 0-3 and 12-15, ``BaseMetaPipeEntity`` for 4-11),
and the machine's identity is the ``mID`` inside the tile entity. That is what
``te_base_type`` carries and why a block the manifest cannot type is refused rather than guessed:
a wrong nibble rebuilds a cable as a machine. An ordinary casing is the other case entirely - its
meta IS block metadata and it needs no tile entity.

**A single block points two ways, and GT reads both** (#249, D18). A basic machine's working face
is ``mMainFacing`` and ``mFacing`` is its OUTPUT face, the one it auto-outputs items and fluids
through; a Super Tank auto-outputs out of its front. Which face that is comes from
:func:`gtnh_solver.output_faces.output_faces`, the reading the previewer's arrows use too, so the
file and the preview cannot disagree; every other output face is a cover, which a ``.schematic``
does not carry, so :class:`SchematicWarning` names each (:func:`_single_block_tile`). Before this
the export wrote only ``mFacing``, as the front: a 2.9 paste then worked on its bottom face and
output out of its front.

**A multiblock's tiered parts arrive already swapped** (#312). A Chemical Plant's solid casing,
pipe casing, coils and machine casings are whatever ``Machine.structure_blocks`` names, applied in
``previewer.textures.expand_machine`` before the cubes reach this module, so a swapped casing is
just another plain block here and needs no code of its own; the manifest must name it like any
other, which is why the committed one carries every block the plant's channels accept.

**Block ids are ours to choose.** ``SchematicaMapping`` maps registry name to the id used in this
file, and Schematica remaps onto whatever the loading instance assigned, so the ids here are
allocated compactly from 1 (air stays 0) rather than copied from any particular install. That
also keeps every id under 256, so ``AddBlocks`` is only emitted if a file ever needs it.

**A GT frame box does not fit, and nothing makes it fit** (#212). In GT 2.9 a frame's world
metadata is its *material id* (``BlockFrameBox.MATERIAL_MASK``, 0xFFF; Steel is 305), plus
``MTE_BIT`` (0x1000) once it carries a tile entity, while ``Data`` holds four bits. Measured on the
maintainer's own saves (``tests/golden/schematic/28-sfb`` and ``29-sfb``, one per pack): Schematica
writes the **low nibble** of the material (Steel 1, Black Steel 14) and keeps the material only in a
frame that has a tile entity, as ``BaseMetaPipeEntity`` with ``mID = 4096 + material``. So every
frame is written in that covered-frame shape (:func:`_frame_cell`): the file then says what each
frame is made of. It cannot make a paste right, and :class:`SchematicWarning` says so: GT keeps a
frame's tile entity only when ``MTE_BIT`` is in the metadata, which a nibble cannot carry, so in
game the ghost and a paste show material 1 or 14 (Hydrogen, Fluorine) instead.
"""

from __future__ import annotations

import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gtnh_solver.dataset.pipes import manifest_names
from gtnh_solver.ir import Facing, InputIR, LayoutResult
from gtnh_solver.ir.geometry import OPPOSITE_FACE
from gtnh_solver.output_faces import BlockOutputs, CoverFace, output_faces
from gtnh_solver.previewer.scene import build_scene
from gtnh_solver.previewer.textures import (
    BlockCube,
    TextureManifest,
    auto_output_faces,
    load_multiblock_docs,
    machine_cubes,
)

from . import nbt

#: ``ForgeDirection`` ordinals, which is what GT writes into ``mFacing`` and the bits of
#: ``mConnections``. Verified against Forge's own ``ForgeDirection.java``; its deltas are
#: identical to :data:`gtnh_solver.ir.geometry.FACING_DELTA`, so the two agree by construction.
FORGE_DIRECTION: Final[dict[Facing, int]] = {
    Facing.DOWN: 0,
    Facing.UP: 1,
    Facing.NORTH: 2,
    Facing.SOUTH: 3,
    Facing.WEST: 4,
    Facing.EAST: 5,
}

#: Unit step -> ForgeDirection ordinal, for turning a route cell's connections into a bitmask.
_STEP_DIRECTION: Final[dict[tuple[int, int, int], int]] = {
    (0, -1, 0): 0,
    (0, 1, 0): 1,
    (0, 0, -1): 2,
    (0, 0, 1): 3,
    (-1, 0, 0): 4,
    (1, 0, 0): 5,
}

#: The real GT block that stands in for a synthesized power source. Kept in step with
#: ``tools/derive_small_manifest.py``, which is what makes sure it ships.
POWER_SOURCE_STAND_IN: Final = "Debug Power Generator"

#: GT's own NBT version stamp, as seen in every tile entity of the golden files.
_NBT_VERSION: Final = 509051476

_AIR: Final = "minecraft:air"
#: The block every GT machine, pipe and cable is a meta of; frame tile entities are named there.
_GT_MACHINES: Final = "gregtech:gt.blockmachines"

#: The most a ``Data`` entry holds: Schematica keeps four bits of a block's metadata (#212).
_DATA_NIBBLE: Final = 0x0F

#: GT's frame box block, whose metadata is a material id rather than a nibble (see the module
#: docstring and :func:`_frame_cell`).
FRAME_BLOCK: Final = "gregtech:gt.blockframes"
#: ``BlockFrameBox.MATERIAL_MASK``: the material-id bits of a frame's metadata.
_FRAME_MATERIAL_MASK: Final = 0xFFF
#: A frame's tile entity is ``mID = 4096 + material``: ``BlockFrameBox.spawnFrameEntity`` ("4096 is
#: found in LoaderMetaTileEntities for frames"), and the covered frame in both frame goldens.
FRAME_MID_BASE: Final = 4096

#: Every ``MTEBasicMachine`` subclass in GT5-Unofficial at the pinned tags (5.09.51.482 for 2.8.4,
#: 5.09.54.20 for 2.9.0-beta-2, which adds ``MTEDrawerFramer``, and 5.09.54.133 for 2.9.0-beta-3,
#: which adds ``MTEIceCreamMachine``), by the fully qualified name the texture
#: manifest records as ``source_class``. The manifest names only a block's LEAF class, so a basic
#: machine is known by being one of these, read off the class hierarchy in the source (every class
#: that extends ``MTEBasicMachine``, transitively; the monorepo's addon packages included). A class
#: missing here exports as it always did, facing its front, which is wrong only for a basic machine,
#: so a pack bump that adds one should add it here.
BASIC_MACHINE_CLASSES: Final = frozenset(
    {
        "bartworks.common.tileentities.debug.MTECreativeScanner",
        "bartworks.common.tileentities.tiered.MTEBioLab",
        "gregtech.api.metatileentity.implementations.MTEBasicMachineBronze",
        "gregtech.api.metatileentity.implementations.MTEBasicMachineSteel",
        "gregtech.api.metatileentity.implementations.MTEBasicMachineWithRecipe",
        "gregtech.common.tileentities.machines.basic.MTEAdvSeismicProspector",
        "gregtech.common.tileentities.machines.basic.MTEBetterJukebox",
        "gregtech.common.tileentities.machines.basic.MTEBoxinator",
        "gregtech.common.tileentities.machines.basic.MTEDrawerFramer",
        "gregtech.common.tileentities.machines.basic.MTEIceCreamMachine",
        "gregtech.common.tileentities.machines.basic.MTEIndustrialApiary",
        "gregtech.common.tileentities.machines.basic.MTEMassfabricator",
        "gregtech.common.tileentities.machines.basic.MTEMiner",
        "gregtech.common.tileentities.machines.basic.MTENameRemover",
        "gregtech.common.tileentities.machines.basic.MTEPotionBrewer",
        "gregtech.common.tileentities.machines.basic.MTEPump",
        "gregtech.common.tileentities.machines.basic.MTEReplicator",
        "gregtech.common.tileentities.machines.basic.MTERockBreaker",
        "gregtech.common.tileentities.machines.basic.MTEScanner",
        "gregtech.common.tileentities.machines.steam.MTESteamAlloySmelterBronze",
        "gregtech.common.tileentities.machines.steam.MTESteamAlloySmelterSteel",
        "gregtech.common.tileentities.machines.steam.MTESteamCompressorBronze",
        "gregtech.common.tileentities.machines.steam.MTESteamCompressorSteel",
        "gregtech.common.tileentities.machines.steam.MTESteamExtractorBronze",
        "gregtech.common.tileentities.machines.steam.MTESteamExtractorSteel",
        "gregtech.common.tileentities.machines.steam.MTESteamForgeHammerBronze",
        "gregtech.common.tileentities.machines.steam.MTESteamForgeHammerSteel",
        "gregtech.common.tileentities.machines.steam.MTESteamFurnaceBronze",
        "gregtech.common.tileentities.machines.steam.MTESteamFurnaceSteel",
        "gregtech.common.tileentities.machines.steam.MTESteamMaceratorBronze",
        "gregtech.common.tileentities.machines.steam.MTESteamMaceratorSteel",
        "gtPlusPlus.xmod.gregtech.common.tileentities.machines.basic.MTEAtmosphericReconditioner",
        "gtPlusPlus.xmod.gregtech.common.tileentities.machines.basic.MTEAutoChisel",
    }
)

#: The Super and Quantum Tanks (``MTEDigitalTankBase``), which auto-output fluid out of their front
#: face (``mFacing``) and nowhere else, once ``mOutputFluid`` is set.
DIGITAL_TANK_CLASSES: Final = frozenset(
    {
        "gregtech.common.tileentities.storage.MTESuperTank",
        "gregtech.common.tileentities.storage.MTEQuantumTank",
    }
)


class SchematicWarning(UserWarning):
    """The file was written, but part of it will not rebuild faithfully in game.

    GT frame boxes, whose material a ``.schematic`` cannot carry into a paste (#212); output faces
    that need a cover, which a ``.schematic`` does not carry; and Item Filters, whose slots it does
    not carry (#249). Each warning names what to build by hand and where.
    """


class SchematicError(RuntimeError):
    """The layout cannot be lowered to a schematic, with the reason named.

    Raised rather than papered over: a ``.schematic`` that silently drops or mistypes a block is
    worse than none, because it looks buildable.
    """


@dataclass(frozen=True)
class Cell:
    """One cell of the grid: what block stands there, and its tile entity if it needs one."""

    block: str  # registry name, e.g. "gregtech:gt.blockmachines"
    data: int  # the Data nibble: a TE base type for a GT block, real block meta for a casing
    tile: nbt.Compound | None = None


def _gt_tile(
    kind: str, mid: int, x: int, y: int, z: int, *, facing: int | None, connections: int | None
) -> nbt.Compound:
    """The minimal tile entity GT needs to reconstruct this block.

    Only the fields that decide *what the block is and which way it points*. A single block's
    auto-output is added on top by :func:`_single_block_tile`, since which face it pushes through is
    part of which way it points. The rest of what GT writes (covers, colour, recipe locks, stored
    energy) is machine configuration, which is the paste-fidelity half of #96 and deliberately
    absent: a build ghost wants the right block in the right orientation, and inventing a
    half-configured machine would be a worse lie than an unconfigured one.

    **``eRotation`` / ``eFlip`` are deliberately not written.** A multiblock controller
    (``MTEEnhancedMultiBlockBase``) stores its StructureLib alignment in them, but they default to
    exactly what we would write: ``Rotation.byIndex(0)`` is ``NORMAL`` and ``Flip.byIndex(0)`` is
    ``NONE`` (both enums declare those first, ``getIndex()`` is ``ordinal()``), and ``getByte`` on
    an absent tag returns 0. Every controller in ``tests/golden/schematic/`` carries 0/0 whatever
    way it faces, so there is nothing else to copy.

    **Known Schematica quirk, not a defect here (measured 2026-09-18):** a NORTH-facing controller
    draws upside down in Schematica's *ghost overlay*. It is the renderer, not the file: the
    golden - which Schematica itself wrote from a working in-world build, and which records the
    same ``mFacing=2, eRotation=0, eFlip=0`` we emit - renders the same way, while placing the
    block for real at that spot is correct, and sweeping ``eRotation`` through all four values
    changes nothing. EAST, WEST and SOUTH are unaffected. Do not "fix" it by perturbing the
    facing: that would corrupt a file that is already right.
    """
    tile = nbt.Compound(
        {
            "id": nbt.String("BaseMetaPipeEntity" if kind == "pipe" else "BaseMetaTileEntity"),
            "x": nbt.Int(x),
            "y": nbt.Int(y),
            "z": nbt.Int(z),
            "mID": nbt.Int(mid),
            "nbtVersion": nbt.Int(_NBT_VERSION),
        }
    )
    if facing is not None:
        tile["mFacing"] = nbt.Short(facing)
    if connections is not None:
        tile["mConnections"] = nbt.Byte(connections)
    return tile


def _frame_cell(meta: int, x: int, y: int, z: int) -> Cell:
    """A GT frame box, in the shape Schematica itself writes a frame that has a tile entity.

    ``Data`` is the low nibble of the material id, which is all Schematica keeps (``28-sfb`` and
    ``29-sfb``: Steel 305 reads back 1, Black Steel 334 reads back 14). The material itself goes in
    a ``BaseMetaPipeEntity`` with ``mID = 4096 + material``, exactly as the covered frame in both
    saves carries it, so the file says what every frame is made of. Written for every frame, not
    only covered ones, because a plain frame's nibble names the wrong material (Steel's 1 is
    Hydrogen) and the file would otherwise have nothing better to say.
    """
    material = meta & _FRAME_MATERIAL_MASK
    return Cell(
        FRAME_BLOCK,
        material & _DATA_NIBBLE,
        _gt_tile("pipe", FRAME_MID_BASE + material, x, y, z, facing=None, connections=0),
    )


def _untypeable(what: str, manifest: TextureManifest) -> SchematicError:
    """The refusal for a block whose ``te_base_type`` the consulted manifest does not carry.

    **Which manifest answered is half the message** (#166). Resolution prefers the newest local
    ``data/<version>/`` dump over the committed ``data/textures/manifest.json``, so a dump generated
    before #158 added the field shadows a committed manifest that has it. The old wording said only
    "the manifest" and pointed at #158, which reads as "the shipped data is stale" when the truth is
    the reverse; that misreading is what produced a wrong bug report. So the message names the file
    it read, dates it, and distinguishes the two ways the field can be absent: a manifest carrying
    it for **no** block predates the field, while one carrying it for others is merely short of this
    block, and those want different fixes.
    """
    if manifest.carries_te_base_type:
        why = (
            "that manifest types other blocks but not this one, so it is short of this block "
            "rather than old; regenerate the dataset "
            "(docs/dataset-extraction/implementation.md)"
        )
    else:
        why = (
            "that manifest carries te_base_type for no block at all, so it predates the field "
            "(GitHub #158) - if it is a local data/<version>/ dump it is shadowing the committed "
            "data/textures/manifest.json, which does carry it (GitHub #166); re-run the extractor "
            "for this pack, or pass --dataset-version to pin a newer dump"
        )
    return SchematicError(
        f"{what} has no te_base_type in {manifest.origin()}, so its Data nibble is unknown and it "
        f"would rebuild as the wrong kind of tile entity; {why}"
    )


def _cube_cell(
    cube: BlockCube, manifest: TextureManifest, front: Facing, origin: tuple[int, int, int]
) -> Cell:
    """One expanded machine block as a grid cell."""
    kind = manifest.kind(cube.block, cube.meta)
    if kind is None:
        raise SchematicError(
            f"{cube.block}|{cube.meta} is not in {manifest.origin()}, so it cannot be typed; "
            "regenerate the dataset (docs/dataset-extraction/implementation.md)"
        )
    x, y, z = (cube.cell[i] - origin[i] for i in range(3))
    if cube.block == FRAME_BLOCK:
        return _frame_cell(cube.meta, x, y, z)
    if kind == "block":
        if cube.meta > _DATA_NIBBLE:
            raise SchematicError(
                f"{cube.block}|{cube.meta} has block metadata above 15, which a .schematic's Data "
                "cannot hold, and only GT frame boxes have a known encoding for it (GitHub #212)"
            )
        return Cell(cube.block, cube.meta)  # a casing: meta IS block metadata, no tile entity

    base = manifest.te_base_type(cube.block, cube.meta)
    if base is None:
        raise _untypeable(f"{cube.block}|{cube.meta} ({kind})", manifest)
    # A hatch points where the router put it; anything else rides the machine's placed front.
    side = Facing(cube.facing.lower()) if cube.facing is not None else front
    return Cell(
        cube.block,
        base,
        _gt_tile(kind, cube.meta, x, y, z, facing=FORGE_DIRECTION[side], connections=None),
    )


def _single_block_tile(
    cell: Cell, source_class: str, front: Facing, outputs: BlockOutputs | None
) -> Cell:
    """``cell`` with the facing and auto-output its GT class needs, read from ``outputs``.

    A **basic machine** (:data:`BASIC_MACHINE_CLASSES`) has two facings, and the old export wrote
    only one. ``mMainFacing`` is its working face, the solver's front, and ``mFacing`` its OUTPUT
    face, which it auto-outputs items and fluids through (``MTEBasicMachine`` 2.8.4 lines 481-515,
    2.9 lines 514-547, and the Basic Forge Hammer in ``tests/golden/schematic/sand.schematic`` writes
    exactly these tags, Int main facing and Short facing). What each tag must hold:

    ====================== ====== =============================================================
    tag                    value  why
    ====================== ====== =============================================================
    mMainFacing (Int)      front  2.9 loads it as written, and an absent tag reads DOWN (0)
    mFacing (Short)        auto   the one face ``output_faces`` says it auto-outputs through,
                                  else the face opposite its front (nothing leaves it)
    mItemTransfer          0/1    what leaves through that face: GT pushes only when set
    mFluidTransfer         0/1    (both load with ``getBoolean``, so absent is off)
    mHasBeenUpdated        1      2.8.4's ``doDisplayThings`` flips ``mFacing`` to its back on
                                  the first tick unless set; 2.9 has no such field and ignores it
    mAllowInputFromOutputSide 0   the solver never docks an input on the output face
    mDisableFilter         1      the field defaults on, but loads with ``getBoolean``, so an
    mDisableMultiStack     1      absent tag would switch both off in a paste
    ====================== ====== =============================================================

    A **Super or Quantum Tank** auto-outputs fluid out of its front (``mFacing``) and only once
    ``mOutputFluid`` is set (``MTEDigitalTankBase.onPostTick``), so a tank with an auto face faces
    it. Everything else, an Item Filter included, keeps the facing :func:`_cube_cell` wrote: a
    filter pushes out of the face opposite ``mFacing`` (``MTEBuffer.moveItems``), which is the
    solver's back when ``mFacing`` is its front, with no toggle to set.
    """
    if cell.tile is None:
        return cell
    tile = nbt.Compound(cell.tile)
    auto = outputs.auto_face if outputs is not None else None
    if source_class in BASIC_MACHINE_CLASSES:
        output = auto if auto is not None else OPPOSITE_FACE[front]
        tile["mMainFacing"] = nbt.Int(FORGE_DIRECTION[front])
        tile["mFacing"] = nbt.Short(FORGE_DIRECTION[output])
        tile["mItemTransfer"] = nbt.Byte(int(outputs is not None and outputs.auto_items))
        tile["mFluidTransfer"] = nbt.Byte(int(outputs is not None and outputs.auto_fluids))
        tile["mHasBeenUpdated"] = nbt.Byte(1)
        tile["mAllowInputFromOutputSide"] = nbt.Byte(0)
        tile["mDisableFilter"] = nbt.Byte(1)
        tile["mDisableMultiStack"] = nbt.Byte(1)
    elif source_class in DIGITAL_TANK_CLASSES and auto is not None:
        tile["mFacing"] = nbt.Short(FORGE_DIRECTION[auto])
        tile["mOutputFluid"] = nbt.Byte(1)
    else:
        return cell
    return Cell(cell.block, cell.data, tile)


def _route_cell(
    raw: dict[str, Any], manifest: TextureManifest, origin: tuple[int, int, int]
) -> Cell:
    """One cable or pipe cell as a grid cell, wired to the sides its route connects on."""
    name = raw.get("block")
    if not name:
        raise SchematicError(
            "a route cell names no dataset block, so it cannot be exported; this is a route with "
            "no material (see dataset/pipes.py)"
        )
    found = manifest.pipe_block(str(name))
    if found is None:
        tried = " or ".join(manifest_names(str(name)))
        raise SchematicError(
            f"{name} is not in {manifest.origin()} (looked for {tried}); regenerate the dataset"
        )
    block, meta = found
    base = manifest.te_base_type(block, meta)
    if base is None:
        raise _untypeable(f"{name} ({block}|{meta})", manifest)
    # mConnections is a ForgeDirection bitmask, one bit per side the route connects. The bit order
    # is confirmed against real GT wiring: in a 2.9 save (golden sand-parallel-29-gui) every bit,
    # read this way, lands on a block and every pipe-to-pipe link is set at both ends (#96).
    mask = 0
    for step in raw.get("dirs", []):
        ordinal = _STEP_DIRECTION.get((int(step[0]), int(step[1]), int(step[2])))
        if ordinal is not None:
            mask |= 1 << ordinal
    cell = raw["cell"]
    x, y, z = (int(cell[i]) - origin[i] for i in range(3))
    return Cell(block, base, _gt_tile("pipe", meta, x, y, z, facing=None, connections=mask))


def lower(
    problem: InputIR,
    layout: LayoutResult,
    *,
    manifest: TextureManifest,
    docs: dict[str, Any] | None = None,
) -> tuple[tuple[int, int, int], dict[tuple[int, int, int], Cell]]:
    """Flatten ``layout`` into ``((width, height, length), {(x, y, z): Cell})``.

    Coordinates are relative to the layout's tight content bounds, so the emitted file is the
    built structure rather than the solver's oversized search region.
    """
    scene = build_scene(problem, layout)
    docs = docs if docs is not None else {}
    bounds = scene["bounds"]
    origin = (int(bounds["min"][0]), int(bounds["min"][1]), int(bounds["min"][2]))
    size = tuple(int(bounds["max"][i]) - origin[i] for i in range(3))
    grid: dict[tuple[int, int, int], Cell] = {}

    auto_out = auto_output_faces(scene)
    outputs = output_faces(problem, layout)
    covers: list[tuple[str, tuple[int, int, int], CoverFace]] = []
    filters: list[tuple[str, tuple[int, int, int], list[str]]] = []
    forbids: list[tuple[str, tuple[int, int, int], Facing]] = []
    for machine in scene["machines"]:
        cubes = machine_cubes(machine, docs, manifest, auto_out)
        if not cubes:
            cubes = _stand_in_cubes(machine, manifest)
        front = Facing(str(machine.get("front", "north")))
        single = len(cubes) == 1 and tuple(machine.get("size", (1, 1, 1))) == (1, 1, 1)
        for cube in cubes:
            cell = _cube_cell(cube, manifest, front, origin)
            key = tuple(cube.cell[i] - origin[i] for i in range(3))
            if single:
                machine_outputs = outputs.get(str(machine["id"]))
                cell = _single_block_tile(
                    cell, manifest.source_class(cube.block, cube.meta), front, machine_outputs
                )
                name = manifest.display_name(cube.block, cube.meta) or str(machine["type"])
                if machine_outputs is not None:
                    covers.extend((name, key, c) for c in machine_outputs.covers)  # type: ignore[misc]
                    if machine_outputs.forbid_input_from_output and machine_outputs.auto_face:
                        forbids.append((name, key, machine_outputs.auto_face))  # type: ignore[arg-type]
                if machine.get("filter_items"):
                    filters.append((name, key, list(machine["filter_items"])))  # type: ignore[arg-type]
            grid[key] = cell  # type: ignore[index]

    for route in scene["routes"]:
        for raw in route["cells"]:
            cell = _route_cell(raw, manifest, origin)
            key = tuple(int(raw["cell"][i]) - origin[i] for i in range(3))
            grid[key] = cell  # type: ignore[index]

    _warn_about_frames(grid, manifest)
    _warn_about_covers(covers)
    _warn_about_filters(filters)
    _warn_about_output_side(forbids)
    return size, grid  # type: ignore[return-value]


def _warn_about_frames(grid: dict[tuple[int, int, int], Cell], manifest: TextureManifest) -> None:
    """Say how many frame boxes a paste will get wrong, and of which materials (#212)."""
    mids: Counter[int] = Counter(
        int(cell.tile["mID"]) for cell in grid.values() if cell.block == FRAME_BLOCK and cell.tile
    )
    if not mids:
        return
    named = ", ".join(
        f"{manifest.display_name(_GT_MACHINES, mid) or f'material {mid - FRAME_MID_BASE}'} x{n}"
        for mid, n in sorted(mids.items())
    )
    warnings.warn(
        f"{sum(mids.values())} GT frame box(es) ({named}): a .schematic keeps only the low 4 bits "
        "of a frame's material, and GT drops a pasted frame's tile entity, so in game the ghost "
        "and a paste show these frames as the wrong material. The file records each frame's real "
        "material (--inspect-schematic names it); build them from that (GitHub #212).",
        SchematicWarning,
        stacklevel=3,
    )


def _warn_about_covers(covers: list[tuple[str, tuple[int, int, int], CoverFace]]) -> None:
    """Name every output face that takes a cover, since a ``.schematic`` carries no covers.

    GT auto-outputs a single block through one face only, and a Super Chest through none
    (``output_faces``), so each other output face needs the cover named: a conveyor for items, a
    pump for fluids. Grouped by cover, each with the block and where it stands in the file, so a
    builder can walk the ghost and fit them.
    """
    if not covers:
        return
    by_cover: dict[str, list[str]] = {}
    for name, (x, y, z), cover in sorted(covers, key=lambda c: (c[2].cover, c[1], c[2].face.value)):
        by_cover.setdefault(cover.cover, []).append(
            f"{cover.face.value} face of {name} at ({x}, {y}, {z})"
        )
    listed = "; ".join(
        f"{kind} x{len(faces)}: {', '.join(faces)}" for kind, faces in by_cover.items()
    )
    warnings.warn(
        f"{len(covers)} output face(s) need a cover, which a .schematic does not carry, so fit them "
        "by hand: GT auto-outputs a single block through one face only, and a Super Chest through "
        f"none. {listed}",
        SchematicWarning,
        stacklevel=3,
    )


def _warn_about_filters(filters: list[tuple[str, tuple[int, int, int], list[str]]]) -> None:
    """Say what each Item Filter must let through, since a ``.schematic`` carries no inventory.

    A filter's nine slots are what it sorts by (``MTEFilter.allowPutStack``); a pasted one is empty
    and passes nothing, so the builder sets each from this list (#249).
    """
    if not filters:
        return
    listed = "; ".join(
        f"{name} at ({x}, {y}, {z}): {', '.join(items)}"
        for name, (x, y, z), items in sorted(filters, key=lambda f: f[1])
    )
    warnings.warn(
        f"{len(filters)} Item Filter(s): a .schematic carries no inventory, so set each filter's "
        f"slots to the item it lets through. {listed}",
        SchematicWarning,
        stacklevel=3,
    )


def _warn_about_output_side(forbids: list[tuple[str, tuple[int, int, int], Facing]]) -> None:
    """Name every machine that must refuse input through its output face, and how (#278).

    Its output face shares a pipe with another machine's outputs (``output_faces``), and on 2.9 a
    basic machine takes items and fluids in through that face by default, so one run dry takes a
    sibling's output and jams. The file writes each one's ``mAllowInputFromOutputSide`` off
    (:func:`_single_block_tile`), but a machine placed by hand from the ghost is new and starts
    allowed. The step names the state to reach, not a click count: a 2.8.4 machine starts
    forbidden, and the same click would allow it.
    """
    if not forbids:
        return
    listed = "; ".join(
        f"{name} at ({x}, {y}, {z}), {face.value} face"
        for name, (x, y, z), face in sorted(forbids, key=lambda f: f[1])
    )
    warnings.warn(
        f"{len(forbids)} machine(s) share an output pipe with another machine's outputs. On 2.9 a "
        "new basic machine takes input through its output face, so one run dry would take a "
        "sibling's output and jam. This file records each as refusing it, but a machine placed by "
        "hand from the ghost does not get that: screwdriver its output face, not sneaking, until "
        f'chat says "Input from Output Side forbidden". {listed}',
        SchematicWarning,
        stacklevel=3,
    )


def _stand_in_cubes(machine: dict[str, Any], manifest: TextureManifest) -> list[BlockCube]:
    """The substitute block for a machine that resolves to none of its own.

    Only the adapter's synthesized power source qualifies. It is our invention rather than a GT
    block, so nothing in the pack is named after it, and leaving its cell empty would put a hole
    in the export exactly where the builder has to feed the line. Anything else that fails to
    resolve is a real gap and is refused, and the refusal says which gap: a machine reserved as a
    single block is one the texture manifest cannot name, while a bigger one is a multiblock whose
    structure was never dumped. Blaming the dump for the first sent #232's reader to the wrong fix.
    """
    if machine.get("role") == "filter":
        raise SchematicError(
            f"{machine.get('type')!r} is not in {manifest.origin()}, so the Item Filter cannot be "
            "exported; a texture dump from the extractor's server pass lacks the item filters "
            "(GT builds their textures client-side), so run its client texture pass for this pack "
            "(GitHub #249)"
        )
    if machine.get("role") != "source":
        if tuple(machine.get("size", (1, 1, 1))) == (1, 1, 1):
            recipe_map = machine.get("recipe_map")
            runs = f"recipe map {recipe_map!r}" if recipe_map else "no stated recipe map"
            raise SchematicError(
                f"{machine.get('type')!r} ({runs}, {machine.get('voltage_tier')}) matches no "
                f"single-block machine in {manifest.origin()}, so it cannot be exported; a manifest "
                "dumped before #232 records no recipe maps, so re-run the extractor's texture pass "
                "for this pack (GitHub #232)"
            )
        raise SchematicError(
            f"{machine.get('type')!r} resolves to no GT block, so it cannot be exported; it is "
            "most likely a multiblock whose structure was never dumped (GitHub #98)"
        )
    found = manifest.mte_block(POWER_SOURCE_STAND_IN)
    if found is None:
        raise SchematicError(
            f"{POWER_SOURCE_STAND_IN!r} is missing from the texture manifest, so the synthesized "
            "power source has no stand-in; regenerate the committed manifest with "
            "tools/derive_small_manifest.py (GitHub #158)"
        )
    cell = machine["cell"]
    return [BlockCube((int(cell[0]), int(cell[1]), int(cell[2])), found[0], found[1], 0)]


def to_nbt(size: tuple[int, int, int], grid: dict[tuple[int, int, int], Cell]) -> nbt.Compound:
    """Build the ``Schematic`` root compound for a lowered grid."""
    width, height, length = size
    count = width * height * length
    if count <= 0:
        raise SchematicError(f"empty build volume {size}; nothing was placed or routed")

    # Compact, deterministic ids: air is 0, every other registry name numbered in sorted order.
    names = sorted({cell.block for cell in grid.values()})
    ids = {name: i + 1 for i, name in enumerate(names)}
    if len(ids) > 254:  # pragma: no cover - a line with 255 distinct blocks is not a thing today
        raise SchematicError(f"{len(ids)} distinct blocks exceeds what a single byte id can hold")

    blocks = bytearray(count)
    data = bytearray(count)
    tiles = nbt.List(nbt.TAG_COMPOUND)
    for y in range(height):
        for z in range(length):
            for x in range(width):
                cell = grid.get((x, y, z))
                if cell is None:
                    continue
                index = (y * length + z) * width + x  # MCEdit order, as the goldens are written
                blocks[index] = ids[cell.block]
                if not 0 <= cell.data <= _DATA_NIBBLE:
                    raise SchematicError(
                        f"{cell.block} at {(x, y, z)} has Data {cell.data}, which a .schematic "
                        "cannot hold; nothing may reach the file unencoded (GitHub #212)"
                    )
                data[index] = cell.data
                if cell.tile is not None:
                    tiles.append(cell.tile)

    mapping = nbt.Compound({_AIR: nbt.Short(0)})
    for name in names:
        mapping[name] = nbt.Short(ids[name])

    return nbt.Compound(
        {
            "Width": nbt.Short(width),
            "Height": nbt.Short(height),
            "Length": nbt.Short(length),
            "Materials": nbt.String("Alpha"),
            "Blocks": nbt.ByteArray(bytes(blocks)),
            "Data": nbt.ByteArray(bytes(data)),
            "Entities": nbt.List(nbt.TAG_COMPOUND),
            "TileEntities": tiles,
            "SchematicaMapping": mapping,
        }
    )


def build_schematic(
    problem: InputIR,
    layout: LayoutResult,
    *,
    manifest: TextureManifest,
    docs: dict[str, Any] | None = None,
) -> nbt.Compound:
    """The ``Schematic`` root compound for ``layout``."""
    size, grid = lower(problem, layout, manifest=manifest, docs=docs)
    return to_nbt(size, grid)


def write_schematic(
    problem: InputIR,
    layout: LayoutResult,
    path: str | Path,
    *,
    version: str | None = None,
) -> Path:
    """Write ``layout`` to ``path`` as a Schematica-loadable ``.schematic``.

    Resolves the dataset the same way the previewer does, so a preview and an export of one solve
    describe the same blocks - provided the caller passes both the same ``version``, which is why
    the CLI hands each of them the version it derived from the plan (#206).

    **A missing half of the dataset is refused by name** rather than surfacing as a bare
    ``FileNotFoundError``, which the CLI can only report as "could not write" the output:

    - no texture manifest: nothing can be typed at all, pinned or not;
    - no ``multiblocks/`` under a **pinned** ``version``: without structures a multiblock cannot be
      told from a single block, so it would export as a lone controller that never forms - a file
      that looks buildable and is not. Unpinned resolution always lands on a real folder (a local
      dump, else the committed fixtures), so there the check has nothing to catch.
    """
    from gtnh_solver.dataset.roots import extractor_hint, resolve_dataset_path

    manifest_path = resolve_dataset_path("textures/manifest.json", version=version)
    if not manifest_path.is_file():
        raise SchematicError(
            f"no texture manifest at {manifest_path}, so no block can be typed; "
            + (
                extractor_hint("textures/manifest.json", version)
                if version is not None
                else "that is the committed fallback, so this checkout is missing it"
            )
        )
    multiblocks = resolve_dataset_path("multiblocks", version=version)
    if version is not None and not multiblocks.is_dir():
        raise SchematicError(
            f"no multiblock structures at {multiblocks}, so a multiblock cannot be told from a "
            f"single block and would export as a lone controller that never forms; "
            f"{extractor_hint('multiblocks', version)}"
        )
    manifest = TextureManifest.load(manifest_path)
    docs = load_multiblock_docs(multiblocks)
    root = build_schematic(problem, layout, manifest=manifest, docs=docs)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(nbt.dumps("Schematic", root))
    return out
