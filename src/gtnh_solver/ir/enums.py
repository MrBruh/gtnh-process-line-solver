"""Enumerations shared by the input IR and the output layout schema.

String-valued enums, so they serialize to the same literal strings the docs use (``"item"``,
``"output"``, ...). Mixed-in ``(str, Enum)`` rather than ``StrEnum``: the two serialize alike,
but ``str()`` and f-string formatting of a member differ between them, so switching would
change any message that prints one.
"""

from __future__ import annotations

from enum import Enum


class Commodity(str, Enum):
    """What flows along a net. Power is a shared-amperage net, not a per-pipe flow."""

    ITEM = "item"
    FLUID = "fluid"
    POWER = "power"


class PipeFamily(str, Enum):
    """Which GT transport family a route is built from.

    Not the same axis as :class:`Commodity`, even though v1 maps one to one: a commodity is what
    flows, a family is what it flows through. GT has separate blocks for fluid and item pipes with
    their own size ladders, and cables are a third family whose gauge is amperage rather than
    throughput.
    """

    CABLE = "cable"
    FLUID_PIPE = "fluid_pipe"
    ITEM_PIPE = "item_pipe"


class PipeSize(str, Enum):
    """How big a fluid or item pipe is: GT's size ladder, smallest first.

    Both pipe families share it (GT builds each material in all five sizes, ``OrePrefixes.pipeTiny``
    through ``pipeHuge``), and a bigger size moves more per tick. A cable has no size: its gauge is
    amperage, carried per segment by ``Route.thickness_per_segment``. GT's quadruple and nonuple
    fluid pipes are deliberately absent - they carry four or nine fluids at once, which makes them a
    different transport (docs/DOMAIN.md, "Fluids and items"), not a bigger pipe.

    Declaration order IS the ladder: iterate the enum to walk it from tiny to huge.
    """

    TINY = "tiny"
    SMALL = "small"
    NORMAL = "normal"
    LARGE = "large"
    HUGE = "huge"


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


#: The facings a machine front can take, in the canonical order machines default to (front
#: defaults to the first, NORTH). The adapter seeds every machine's ``orientation_options`` with
#: these; one source for both adapters, which each hard-coded the same list.
HORIZONTAL_FACINGS_ORDERED: tuple[Facing, ...] = (
    Facing.NORTH,
    Facing.SOUTH,
    Facing.EAST,
    Facing.WEST,
)

#: The facings a machine front can take. GT machines are placed facing a horizontal direction;
#: they never face up/down (top/bottom faces can still carry I/O - see docs/DOMAIN.md). The
#: membership form the ``Machine`` validator checks orientation options against.
HORIZONTAL_FACINGS = frozenset(HORIZONTAL_FACINGS_ORDERED)
