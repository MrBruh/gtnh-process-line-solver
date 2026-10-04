"""The ME (AE2) vocabulary the contracts and the rule data share.

Applied Energistics 2 builds a line's ME side from a handful of things this module names: the
cable kinds a network is laid in (:class:`MECableKind`), the seventeen colours that keep two
networks apart (:class:`AEColor`), the devices that move items and fluids between a machine and the
network (:class:`MEDeviceKind`), the upgrade cards a bus is fitted with (:class:`MECards`), and the
policy that decides when a multiblock uses GT's own ME hatches (:class:`MEHatchPolicy`).

Value types only, and additive: nothing in ``InputIR`` or ``LayoutResult`` carries them yet, so they
bump neither contract (``ir/__init__.py``). The RULES about them, which colours connect, how many
channels a cable carries, how fast a bus moves, are DATA in :mod:`gtnh_solver.dataset.me`, cited
against the pack's source in ``docs/spikes/329-me-ae2.md``; this module only says what the words
are.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from ._base import FrozenModel


class AEColor(str, Enum):
    """An AE2 cable or device colour, in AE2's own order (``appeng.api.util.AEColor``).

    **Declaration order IS AE2's ordinal**, which is what a cable's item damage adds to its kind's
    base (spike 7.3): use :attr:`ordinal` for the number. ``FLUIX`` is AE2's ``Transparent``, the
    uncoloured cable; which colours connect is rule data (``dataset.me.colours_connect``).
    """

    WHITE = "white"
    ORANGE = "orange"
    MAGENTA = "magenta"
    LIGHT_BLUE = "light_blue"
    YELLOW = "yellow"
    LIME = "lime"
    PINK = "pink"
    GRAY = "gray"
    LIGHT_GRAY = "light_gray"
    CYAN = "cyan"
    PURPLE = "purple"
    BLUE = "blue"
    BROWN = "brown"
    GREEN = "green"
    RED = "red"
    BLACK = "black"
    FLUIX = "fluix"

    @property
    def ordinal(self) -> int:
        """AE2's ordinal for this colour: White 0 through Black 15, Fluix 16."""
        return list(AEColor).index(self)


class MECableKind(str, Enum):
    """An AE2 cable, by what it is built from. Only the smart and dense kinds show their channels.

    How many channels each carries, and which take parts, is rule data
    (``dataset.me.CABLE_CAPACITY``, ``dataset.me.PART_CABLES``).
    """

    GLASS = "glass"
    COVERED = "covered"
    SMART = "smart"
    DENSE = "dense"
    DENSE_COVERED = "dense_covered"


class MEDeviceKind(str, Enum):
    """A device that moves a machine's items or fluids between it and an ME network.

    Two families, by what the device is in the world:

    - **AE2 parts**, which sit on one side of a cable block and work on the block that side faces:
      AE2's item buses and interface, and AE2FluidCraft's fluid buses and Dual Interface. The Dual
      Interface takes items as well as fluids (spike 4.6), so it serves a single block whose items
      and fluids leave through one face;
    - **GT's own ME hatches**, which take a multiblock hatch slot like any other hatch and connect to
      AE through their front face alone (spike 5.2).

    What each moves per tick, and what it costs, is rule data (:mod:`gtnh_solver.dataset.me`).
    """

    IMPORT_BUS = "import_bus"
    EXPORT_BUS = "export_bus"
    STORAGE_BUS = "storage_bus"
    INTERFACE = "interface"
    FLUID_IMPORT_BUS = "fluid_import_bus"
    FLUID_EXPORT_BUS = "fluid_export_bus"
    FLUID_STORAGE_BUS = "fluid_storage_bus"
    DUAL_INTERFACE = "dual_interface"
    GT_OUTPUT_BUS_ME = "gt_output_bus_me"
    GT_OUTPUT_HATCH_ME = "gt_output_hatch_me"
    GT_STOCKING_INPUT_BUS_ME = "gt_stocking_input_bus_me"
    GT_STOCKING_INPUT_HATCH_ME = "gt_stocking_input_hatch_me"


class MEHatchPolicy(str, Enum):
    """When a multiblock's ME connection is one of GT's own ME hatches.

    - ``tier_aware`` (the default): when the line has reached the hatch's tier, so the player can
      make it;
    - ``always``: whatever the line's tier;
    - ``never``: never; the multiblock keeps a normal hatch and an AE2 part in front of it moves its
      contents.
    """

    TIER_AWARE = "tier_aware"
    ALWAYS = "always"
    NEVER = "never"


class MECards(FrozenModel):
    """The upgrade cards fitted to one AE2 bus.

    ``acceleration`` is AE2's Acceleration Card (its ``CardSpeed``), ``super_speed`` GT:NH's
    Hyper-Acceleration Card (``CardSuperSpeed``), ``capacity`` the Capacity Card that opens more
    config slots. AE2 takes at most four of each, which is all this states; that they share a bus's
    four upgrade slots is a rule the validator checks (``dataset.me.UPGRADE_SLOTS``), not a shape.
    """

    acceleration: int = Field(default=0, ge=0, le=4)
    super_speed: int = Field(default=0, ge=0, le=4)
    capacity: int = Field(default=0, ge=0, le=4)

    @property
    def count(self) -> int:
        """How many upgrade slots these cards take."""
        return self.acceleration + self.super_speed + self.capacity
