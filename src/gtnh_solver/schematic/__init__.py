"""schematic - export a solved layout as a Schematica ``.schematic`` build ghost (GitHub #96).

Minecraft 1.7.10 has no Litematica; the in-game consumer is Schematica, which loads a classic
MCEdit-style ``.schematic`` as a build overlay. See :mod:`.core` for the lowering, :mod:`.read` for
decoding a file back, and :mod:`.nbt` for the binary format.
"""

from __future__ import annotations

from .core import SchematicError, build_schematic, write_schematic
from .read import Schematic, TileEntity, read_schematic

__all__ = [
    "Schematic",
    "SchematicError",
    "TileEntity",
    "build_schematic",
    "read_schematic",
    "write_schematic",
]
