"""Enumerations shared by the input IR and the output layout schema.

String-valued enums (not ``StrEnum``, which is 3.11+) so they serialize to the same
literal strings the docs use (``"item"``, ``"output"``, ...) and stay 3.10-compatible.
"""

from __future__ import annotations

from enum import Enum


class Commodity(str, Enum):
    """What flows along a net. Power is a shared-amperage net, not a per-pipe flow."""

    ITEM = "item"
    FLUID = "fluid"
    POWER = "power"


class IODirection(str, Enum):
    """Direction of a machine port or a pinned external I/O point."""

    INPUT = "input"
    OUTPUT = "output"


class Facing(str, Enum):
    """A block face / cardinal direction. A machine's front face carries no I/O; its
    ``orientation`` is the direction the front face points (see docs/DOMAIN.md)."""

    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"
    UP = "up"
    DOWN = "down"


class LayoutStatus(str, Enum):
    """Terminal status of a solve."""

    VALID = "valid"
    INFEASIBLE = "infeasible"
    PARTIAL_INVALID = "partial_invalid"


#: The facings a machine front can take. GT machines are placed facing a horizontal direction;
#: they never face up/down (top/bottom faces can still carry I/O - see docs/DOMAIN.md).
HORIZONTAL_FACINGS = frozenset({Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST})
