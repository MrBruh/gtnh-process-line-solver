"""Read a ``.schematic`` back into blocks and tile entities: the inverse of :mod:`.core`.

``core`` lowers a solved layout *to* a file; this reads one *in*, whoever wrote it - our own
exporter, or Schematica saving a real in-world build (which is what ``tests/golden/schematic/``
holds). It exists so that verifying an export means reading a file rather than re-deriving the
format::

    .schematic --gunzip--> NBT compound          nbt.loads
        |
        |  read_schematic()
        v
    Schematic(width, height, length, block_ids[], block_data[], names{}, tile_entities[])
        |
        '--> block_at(x, y, z) -> ("gregtech:gt.blockcasings5", 0)
        '--> tile_at(x, y, z)  -> TileEntity(mid=998, id="BaseMetaTileEntity", ...)

Three details are easy to get wrong, and getting any of them wrong decodes to plausible nonsense
rather than to an error:

**Cell order is MCEdit's** ``(y * Length + z) * Width + x``. That is what lines each
``TileEntities`` entry up with its cell, and :func:`gtnh_solver.schematic.core.to_nbt` writes in
the same order.

**``AddBlocks`` packs the high nibble of a 12-bit block id two cells to a byte, and an even cell
index takes the HIGH nibble.** This is measured, not recalled: decoded that way the nitrobenzene
golden leaves 0 of its 1386 cells unmapped and all 116 tile entities standing on a real block,
while the other order leaves 224 cells unmapped and 47 tile entities floating in air. That file
needs the array at all only because ``gt.blockmachines`` is id 2417 in it; our own exporter numbers
ids compactly from 1 and omits ``AddBlocks`` entirely, so both shapes are handled.

**An id with no name is reported, never guessed.** ``SchematicaMapping`` is Schematica's own
extension, so a plain MCEdit file has none and every id in it is nameless. Rather than inventing a
registry name, an unmapped id reads back as ``"<unmapped:2417>"``. Same doctrine as the texture
dump: a visible gap is recoverable, a confident wrong answer is not.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from . import nbt
from .core import SchematicError

#: The registry name of air, which is id 0 by convention in every ``.schematic`` dialect.
_AIR: Final = "minecraft:air"


@dataclass(frozen=True)
class TileEntity:
    """One ``TileEntities`` entry, with the fields that identify a GT block pulled out.

    ``mid`` is GT's ``mID``: the machine's real identity, and the reason a block-only export cannot
    work (it runs to five digits while ``Data`` is four bits). It is ``None`` for a tile entity that
    is not a GT machine at all - an EnderIO reservoir, a StorageDrawers drawer - which is a fact
    about the block rather than a parse failure. ``raw`` keeps the whole compound, so covers, stored
    fluids and recipe locks stay reachable without this dataclass modelling GT's ~90 tags.
    """

    id: str
    pos: tuple[int, int, int]
    mid: int | None
    facing: int | None
    connections: int | None
    raw: nbt.Compound


@dataclass(frozen=True)
class Schematic:
    """A decoded ``.schematic``: a block grid plus the tile entities standing in it."""

    width: int
    height: int
    length: int
    materials: str
    block_ids: tuple[int, ...]
    block_data: bytes
    names: Mapping[int, str]
    tile_entities: tuple[TileEntity, ...]
    root: nbt.Compound

    @property
    def size(self) -> tuple[int, int, int]:
        """``(width, height, length)``, the order the NBT tags are named in."""
        return (self.width, self.height, self.length)

    @property
    def volume(self) -> int:
        """Total cells, air included."""
        return self.width * self.height * self.length

    def index(self, x: int, y: int, z: int) -> int:
        """The flat cell index of ``(x, y, z)`` in MCEdit order."""
        if not (0 <= x < self.width and 0 <= y < self.height and 0 <= z < self.length):
            raise SchematicError(f"({x}, {y}, {z}) is outside a {self.size} schematic")
        return (y * self.length + z) * self.width + x

    def name_of(self, block_id: int) -> str:
        """The registry name for ``block_id``, or an explicit ``"<unmapped:N>"`` marker."""
        if block_id == 0:
            return self.names.get(0, _AIR)
        return self.names.get(block_id, f"<unmapped:{block_id}>")

    def block_at(self, x: int, y: int, z: int) -> tuple[str, int]:
        """``(registry name, Data nibble)`` at ``(x, y, z)``.

        The nibble is *not* the machine: for a GT block it selects the tile entity class and the
        identity lives in :attr:`TileEntity.mid`, while for an ordinary casing it is genuine block
        metadata. :mod:`.core` documents which is which.
        """
        i = self.index(x, y, z)
        return (self.name_of(self.block_ids[i]), self.block_data[i])

    def tile_at(self, x: int, y: int, z: int) -> TileEntity | None:
        """The tile entity at ``(x, y, z)``, or ``None`` if that cell has none."""
        for tile in self.tile_entities:
            if tile.pos == (x, y, z):
                return tile
        return None

    def iter_cells(self, *, air: bool = False) -> Iterator[tuple[tuple[int, int, int], str, int]]:
        """Yield ``((x, y, z), registry name, Data nibble)``, skipping air unless asked."""
        for y in range(self.height):
            for z in range(self.length):
                for x in range(self.width):
                    i = (y * self.length + z) * self.width + x
                    name = self.name_of(self.block_ids[i])
                    if not air and name == _AIR:
                        continue
                    yield ((x, y, z), name, self.block_data[i])

    def histogram(self, *, air: bool = False) -> dict[str, int]:
        """Cell counts per registry name, most common first, air excluded unless asked."""
        counts: dict[str, int] = {}
        for block_id in self.block_ids:
            name = self.name_of(block_id)
            if not air and name == _AIR:
                continue
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _int_or_none(tile: nbt.Compound, key: str) -> int | None:
    """``tile[key]`` as an int, or ``None`` when the tag is absent (or not a number)."""
    value: Any = tile.get(key)
    return int(value) if isinstance(value, int) else None


def _tile_entity(tile: nbt.Compound) -> TileEntity:
    return TileEntity(
        id=str(tile.get("id", "")),
        pos=(int(tile.get("x", 0)), int(tile.get("y", 0)), int(tile.get("z", 0))),
        mid=_int_or_none(tile, "mID"),
        facing=_int_or_none(tile, "mFacing"),
        connections=_int_or_none(tile, "mConnections"),
        raw=tile,
    )


def read_schematic(source: str | Path | bytes) -> Schematic:
    """Decode ``source`` (a path, or the raw file bytes) into a :class:`Schematic`.

    Raises :class:`~gtnh_solver.schematic.core.SchematicError` on a file whose arrays disagree with
    its own stated dimensions, rather than decoding the part that fits: a truncated grid would
    otherwise read back as a build with air where the missing cells were.
    """
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        _, root = nbt.loads(raw)
    except (nbt.NBTError, gzip.BadGzipFile, EOFError, ValueError) as exc:
        raise SchematicError(f"not a readable .schematic: {exc}") from exc

    for tag in ("Width", "Height", "Length", "Blocks", "Data"):
        if tag not in root:
            raise SchematicError(f"missing the {tag!r} tag; is this a .schematic?")
    width, height, length = int(root["Width"]), int(root["Height"]), int(root["Length"])
    count = width * height * length

    blocks = bytes(root["Blocks"])
    data = bytes(root["Data"])
    for tag, array in (("Blocks", blocks), ("Data", data)):
        if len(array) != count:
            raise SchematicError(
                f"{tag} holds {len(array)} bytes but {width}x{height}x{length} needs {count}"
            )

    add = bytes(root.get("AddBlocks", b""))
    if add and len(add) < (count + 1) // 2:
        raise SchematicError(f"AddBlocks holds {len(add)} bytes, too few for {count} cells")

    ids: list[int] = []
    for i in range(count):
        high = 0
        if add:
            packed = add[i >> 1]
            high = (packed >> 4) & 0xF if i % 2 == 0 else packed & 0xF
        ids.append((high << 8) | blocks[i])

    mapping = root.get("SchematicaMapping") or nbt.Compound()
    names = {int(value): str(key) for key, value in mapping.items()}

    return Schematic(
        width=width,
        height=height,
        length=length,
        materials=str(root.get("Materials", "")),
        block_ids=tuple(ids),
        block_data=data,
        names=names,
        tile_entities=tuple(_tile_entity(t) for t in root.get("TileEntities", [])),
        root=root,
    )
