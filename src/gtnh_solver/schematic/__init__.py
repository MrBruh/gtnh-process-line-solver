"""schematic - export a solved layout as a Schematica ``.schematic`` build ghost (GitHub #96).

Minecraft 1.7.10 has no Litematica; the in-game consumer is Schematica, which loads a classic
MCEdit-style ``.schematic`` as a build overlay. See :mod:`.core` for the lowering, :mod:`.read` for
decoding a file back, :mod:`.world` for the target world's item ids a cover needs, and :mod:`.nbt`
for the binary format.
"""

from __future__ import annotations

from .core import SchematicError, SchematicWarning, build_schematic, write_schematic
from .read import Schematic, TileEntity, read_schematic
from .world import item_ids

__all__ = [
    "Schematic",
    "SchematicError",
    "SchematicWarning",
    "TileEntity",
    "build_schematic",
    "item_ids",
    "read_schematic",
    "write_schematic",
]
