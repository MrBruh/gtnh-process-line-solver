"""Lower a solved layout to a Schematica ``.schematic`` build ghost (GitHub #96).

The pipeline, and why each step exists::

    (InputIR, LayoutResult)
        |  build_scene            the same flattening the previewer consumes, so a schematic and
        |                         a preview can never disagree about what was solved
        v
    scene ---> machine_cubes()    per-block cubes: a multiblock's whole structure, or one cube for
        |                         a single-block machine, each already yaw-rotated and clamped
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
from gtnh_solver.previewer.scene import build_scene
from gtnh_solver.previewer.textures import (
    BlockCube,
    TextureManifest,
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


class SchematicWarning(UserWarning):
    """The file was written, but part of it will not rebuild faithfully in game.

    Today that is only GT frame boxes, whose material a ``.schematic`` cannot carry into a paste
    (#212); the file still records it, so the warning says where to read what to build.
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

    Only the fields that decide *what the block is and which way it points*. The rest of what GT
    writes (covers, colour, I/O disables, recipe locks, stored energy) is machine configuration,
    which is the paste-fidelity half of #96 and deliberately absent: a build ghost wants the right
    block in the right orientation, and inventing a half-configured machine would be a worse lie
    than an unconfigured one.

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
    # mConnections is a ForgeDirection bitmask. NOTE: both golden files carry 0 throughout, so the
    # bit order is taken from ForgeDirection rather than confirmed against a real wired pipe - it
    # is the one thing here still wanting an in-game check (#96).
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

    auto_out = {str(ac["source"]): str(ac["sourceFace"]) for ac in scene.get("autoConnections", [])}
    for machine in scene["machines"]:
        cubes = machine_cubes(machine, docs, manifest, auto_out)
        if not cubes:
            cubes = _stand_in_cubes(machine, manifest)
        front = Facing(str(machine.get("front", "north")))
        for cube in cubes:
            cell = _cube_cell(cube, manifest, front, origin)
            grid[tuple(cube.cell[i] - origin[i] for i in range(3))] = cell  # type: ignore[index]

    for route in scene["routes"]:
        for raw in route["cells"]:
            cell = _route_cell(raw, manifest, origin)
            key = tuple(int(raw["cell"][i]) - origin[i] for i in range(3))
            grid[key] = cell  # type: ignore[index]

    _warn_about_frames(grid, manifest)
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


def _stand_in_cubes(machine: dict[str, Any], manifest: TextureManifest) -> list[BlockCube]:
    """The substitute block for a machine that resolves to none of its own.

    Only the adapter's synthesized power source qualifies. It is our invention rather than a GT
    block, so nothing in the pack is named after it, and leaving its cell empty would put a hole
    in the export exactly where the builder has to feed the line. Anything else that fails to
    resolve is a real gap and is refused.
    """
    if not machine.get("role") == "source":
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
