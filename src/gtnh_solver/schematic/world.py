"""Read a Minecraft 1.7.10 world's numeric item ids, which is what lets the export write a cover.

A GT cover is stored in its machine's tile entity as ``gt.covers``, and each entry names its cover
item by number: ``Item.getIdFromItem(item) | meta << 16`` (``GTUtility.stackToInt``). That number is
the world's, not the pack's. FML hands out item ids per world and records them in the world's
``level.dat``, and two worlds of one 2.9 instance were measured to disagree (``gt.metaitem.01`` is
7639 in one and 7436 in the other). Schematica remaps a file's *block* ids through
``SchematicaMapping`` but never touches tile-entity NBT, so a cover written with another world's
number lands as no cover, as a different cover, or as one the client crashes drawing.

So the export writes covers only when it is told which world the build goes in, and reads that
world's own table::

    <world>/level.dat --gunzip--> FML.ItemData: [{K: "\\x02gregtech:gt.metaitem.01", V: 7639}, ...]
                                                  |
                                     item_ids()   v
                                  {"gregtech:gt.metaitem.01": 7639, ...}

FML prefixes each name with one control character, ``\\x01`` for a block and ``\\x02`` for an item;
only the items are returned.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Final

from . import nbt
from .core import SchematicError

#: FML's marker for an item entry in ``ItemData``; a block entry starts with ``\x01``.
_ITEM_PREFIX: Final = "\x02"


def item_ids(world: str | Path) -> dict[str, int]:
    """``registry name -> numeric item id`` for the world at ``world``.

    ``world`` is the world's save folder or its ``level.dat``. Raises
    :class:`~gtnh_solver.schematic.core.SchematicError` naming the file when it is missing,
    unreadable, or has no FML item table (a world never opened with Forge has none).
    """
    path = Path(world)
    level = path / "level.dat" if path.is_dir() else path
    if not level.is_file():
        raise SchematicError(
            f"no level.dat at {level}; give the world's save folder or its level.dat"
        )
    try:
        _, root = nbt.loads(level.read_bytes())
    except (nbt.NBTError, gzip.BadGzipFile, EOFError, ValueError) as exc:
        raise SchematicError(f"{level} is not a readable level.dat: {exc}") from exc
    fml = root.get("FML")
    entries = fml.get("ItemData") if isinstance(fml, nbt.Compound) else None
    if not entries:
        raise SchematicError(
            f"{level} has no FML item table, so its item ids are unknown; open the world once "
            "with the pack's Forge before exporting covers for it"
        )
    return {
        str(entry["K"])[len(_ITEM_PREFIX) :]: int(entry["V"])
        for entry in entries
        if str(entry.get("K", "")).startswith(_ITEM_PREFIX)
    }
