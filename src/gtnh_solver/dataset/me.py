"""AE2 (ME network) rule data: what each cable carries, what each ME device moves and costs, and
which device serves a machine's port.

Sibling of :mod:`gtnh_solver.dataset.pipe_capacity`, and the same trade: shared rule DATA, read
from the pack's source at its pins (AE2 ``rv3-beta-1050-GTNH``, AE2FluidCraft ``1.5.106-gtnh``, GT
``5.09.54.133``, the 2.9.0-beta-3 config). Every figure is cited in ``docs/spikes/329-me-ae2.md``;
"spike 4.2" below means that note's section 4.2. The validator re-checks the rules with its own
logic (docs/ARCHITECTURE.md #4); this is where both it and the solver read the numbers from.

**A port's ME device** (:func:`me_devices_for`) is the first of these that keeps up with the port's
rate, one device and then two::

    port
      |
      +-- multiblock --- GT ME hatch allowed? (policy, and the line's tier) --+
      |                                                                       |
      |        yes: the GT ME hatch, then a normal hatch with a part     no: a normal hatch with a part
      |             (tier_aware tries both at one device first;              in front of it
      |              always tries the GT hatch at one and two first)
      |
      +-- single block, input ............ an export bus on an input face, carded to the rate
      +-- single block, auto-output ...... an interface on the output face, then an import bus
      +-- single block, other output ..... an import bus on any face but the main one

    none keeps up with two  ->  MEShortfall, naming the rate and the most two devices move

**Rates** come from three places, never from the interface itself: an ME interface takes a GT push
straight into the network (spike 4.5), so what an interface moves is what GT pushes into it.

- a **bus** moves its cards' amount once per :data:`BUS_PERIOD_TICKS` (spike 4.1-4.3);
- a **GT pusher** bounds an interface: a normal output bus pushes ``8 x (tier + 1)^2`` items/t
  (:func:`output_bus_push_rate`), a single block's fluid auto-output 1000 mB per push
  (:func:`single_block_fluid_push_rate`); a normal output hatch pushes its whole tank every tick,
  and a single block's item auto-output every output stack on each completion, so neither bounds it;
- a **GT ME hatch** flushes its buffer every 41 ticks (:data:`GT_ME_HATCHES`); its stocking input
  hatches hold nothing and draw from network stock, so only that stock and power bound them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gtnh_solver.ir import Commodity, IODirection
from gtnh_solver.ir.me import AEColor, MECableKind, MECards, MEDeviceKind, MEHatchPolicy

from .voltage import VOLTAGE_BY_TIER

# --- power (spike 6) -------------------------------------------------------------------------------

#: The pack's ``UsageMultiplier`` (``AppliedEnergistics2.cfg``): every idle draw, channel cost and
#: transfer cost is extracted times this, and every AE energy buffer holds this many times its
#: nominal size (spike 6.1).
USAGE_MULTIPLIER = 10.0
#: AE per GT EU: the pack's ``IC2`` ratio, which ``PowerUnits.EU`` uses for GT power (spike 6.1).
AE_PER_EU = 2.0
#: A node's idle draw when it sets none, which no device this solver places overrides (spike 6.1).
DEFAULT_IDLE_AE = 1.0
#: A controller block's idle draw (``TileController``).
CONTROLLER_IDLE_AE = 3.0
#: Nominal AE per item a network inserts or extracts, and per started 1000 mB of fluid (spike 6.1).
#: Times :data:`USAGE_MULTIPLIER`, like everything else.
AE_PER_ITEM = 1.0
AE_PER_FLUID_OPERATION = 1.0
#: The mB one fluid transfer charge covers: a move of ``n`` mB costs ``ceil(n / 1000)`` charges.
MB_PER_FLUID_OPERATION = 1000
#: AE/t a network's channels cost, per channel per hop counted the way AE counts them (spike 6.2):
#: the channels passing through every node and every connection, summed, over this.
CHANNEL_LOAD_PER_AE = 128.0

# --- cables and channels (spike 1, 2) ------------------------------------------------------------

#: How many channels a cable of each kind carries through itself (spike 1.1).
CABLE_CAPACITY: dict[MECableKind, int] = {
    MECableKind.GLASS: 8,
    MECableKind.COVERED: 8,
    MECableKind.SMART: 8,
    MECableKind.DENSE: 32,
    MECableKind.DENSE_COVERED: 32,
}
#: The cables a part (a bus, an interface) can sit on: every part but the cable anchor refuses a
#: dense cable (spike 3).
PART_CABLES: frozenset[MECableKind] = frozenset(
    {MECableKind.GLASS, MECableKind.COVERED, MECableKind.SMART}
)
#: The cable kinds that show their channels in game, which the previewer draws lights on.
LIT_CABLES: frozenset[MECableKind] = frozenset({MECableKind.SMART, MECableKind.DENSE})
#: How many channels any other node carries through itself: a block device, a GT ME hatch, a part.
DEVICE_CAPACITY = 8
#: Most channel devices a network with no controller gives a channel to. One more and every one of
#: them loses its channel, whatever cable they are on (spike 2.6).
ADHOC_MAX_DEVICES = 8
#: Upgrade slots on one AE2 bus (spike 4.2).
UPGRADE_SLOTS = 4


def colours_connect(a: AEColor, b: AEColor) -> bool:
    """Whether a node coloured ``a`` connects to a neighbour coloured ``b``.

    AE2's ``AEColor.matches``: either is Fluix (AE2's ``Transparent``), or both are the same colour
    (spike 3). That is the whole of network isolation: a subnet beside the player's main network is
    kept apart by colour alone.
    """
    return AEColor.FLUIX in (a, b) or a is b


# --- buses and cards (spike 4.1-4.3) -------------------------------------------------------------

#: Ticks between a bus's operations once it has work every call: the bottom of its tick range,
#: which AE's scheduler settles at (spike 4.1).
BUS_PERIOD_TICKS = 5
#: Items an item bus moves per operation, by Acceleration Cards fitted (``PartImportBus`` /
#: ``PartExportBus.calculateAmountToSend``).
ITEM_ACCELERATION = (1, 8, 32, 64, 96)
#: Items Hyper-Acceleration Cards ADD per operation, by cards fitted.
ITEM_SUPER_SPEED = (0, 16, 128, 1024, 8192)
#: mB an AE2FluidCraft fluid bus moves per operation with no cards.
FLUID_BASE_MB = 1000
#: What Acceleration Cards multiply a fluid bus's amount by, by cards fitted (the switch falls
#: through: 8, then 8 x 4, then x 2, then x 1.5).
FLUID_ACCELERATION = (1, 8, 32, 64, 96)
#: What Hyper-Acceleration Cards multiply it by on top, by cards fitted.
FLUID_SUPER_SPEED = (1, 32, 512, 6144, 49152)
#: The tier a line must reach before a bus is fitted Hyper-Acceleration Cards: their recipe needs
#: an Elite World Accelerator, an LuV machine (spike 4.3). A maintainer decision on #329.
SUPER_SPEED_TIER = "LuV"

# --- GT pushers (spike 4.7, 4.8) -----------------------------------------------------------------

#: Ticks between a normal GT output bus's pushes into what faces its front (``MTEHatchOutputBus``).
OUTPUT_BUS_PUSH_TICKS = 8
#: mB a single block's fluid auto-output pushes at most at once (``MTEBasicMachine``, ``drain(1000)``).
SINGLE_BLOCK_FLUID_PUSH_MB = 1000
#: Ticks between a single block's fluid pushes when no recipe completes in between.
SINGLE_BLOCK_FLUID_PUSH_TICKS = 20


@dataclass(frozen=True)
class GTMEHatch:
    """One of GT's own ME hatches (spike 5.1).

    ``hatch_kind`` is the ``HatchElement`` kind of the class it extends, in ``ir.HATCH_KINDS``'
    vocabulary, which is what a multiblock's hatch slot must accept for it. ``per_tick`` is what it
    moves at most, sustained, per tick, or ``None`` when it holds nothing of its own and only the
    network's stock and power bound it.
    """

    mid: int
    name: str
    kind: MEDeviceKind
    hatch_kind: str
    tier: str
    commodity: Commodity
    direction: IODirection
    per_tick: float | None


#: Ticks between an ME output bus's or hatch's flushes under continuous load (``tick >
#: lastOutputTick + 40``, ``MTEHatchOutputMEBase``).
GT_ME_FLUSH_TICKS = 41

#: GT's ME hatches by mID. Two pairs share a class (2711/2718 and 2712/2717), so the mID is the only
#: identity (spike 5.1). The advanced stocking hatches (2711, 2712) only add auto-pull and are never
#: chosen, but are listed so a reader of a build can name them.
GT_ME_HATCHES: dict[int, GTMEHatch] = {
    hatch.mid: hatch
    for hatch in (
        GTMEHatch(
            2710,
            "Output Bus (ME)",
            MEDeviceKind.GT_OUTPUT_BUS_ME,
            "OutputBus",
            "EV",
            Commodity.ITEM,
            IODirection.OUTPUT,
            1600 / GT_ME_FLUSH_TICKS,
        ),
        GTMEHatch(
            2713,
            "Output Hatch (ME)",
            MEDeviceKind.GT_OUTPUT_HATCH_ME,
            "OutputHatch",
            "EV",
            Commodity.FLUID,
            IODirection.OUTPUT,
            128000 / GT_ME_FLUSH_TICKS,
        ),
        GTMEHatch(
            2718,
            "Stocking Input Bus (ME)",
            MEDeviceKind.GT_STOCKING_INPUT_BUS_ME,
            "InputBus",
            "EV",
            Commodity.ITEM,
            IODirection.INPUT,
            None,
        ),
        GTMEHatch(
            2711,
            "Advanced Stocking Input Bus (ME)",
            MEDeviceKind.GT_STOCKING_INPUT_BUS_ME,
            "InputBus",
            "LuV",
            Commodity.ITEM,
            IODirection.INPUT,
            None,
        ),
        GTMEHatch(
            2717,
            "Stocking Input Hatch (ME)",
            MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME,
            "InputHatch",
            "UV",
            Commodity.FLUID,
            IODirection.INPUT,
            None,
        ),
        GTMEHatch(
            2712,
            "Advanced Stocking Input Hatch (ME)",
            MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME,
            "InputHatch",
            "UHV",
            Commodity.FLUID,
            IODirection.INPUT,
            None,
        ),
    )
}

#: The GT ME hatch a multiblock port is given, by what it carries: the basic ones.
_GT_ME_HATCH_FOR: dict[tuple[Commodity, IODirection], int] = {
    (Commodity.ITEM, IODirection.OUTPUT): 2710,
    (Commodity.FLUID, IODirection.OUTPUT): 2713,
    (Commodity.ITEM, IODirection.INPUT): 2718,
    (Commodity.FLUID, IODirection.INPUT): 2717,
}

#: The normal hatch kind a multiblock port is given when no GT ME hatch serves it, and the AE2 part
#: standing in front of it (spike 4.8): an output pushes into an interface, an input is filled by an
#: export bus.
_NORMAL_HATCH_PART: dict[tuple[Commodity, IODirection], tuple[str, MEDeviceKind]] = {
    (Commodity.ITEM, IODirection.OUTPUT): ("OutputBus", MEDeviceKind.INTERFACE),
    (Commodity.FLUID, IODirection.OUTPUT): ("OutputHatch", MEDeviceKind.DUAL_INTERFACE),
    (Commodity.ITEM, IODirection.INPUT): ("InputBus", MEDeviceKind.EXPORT_BUS),
    (Commodity.FLUID, IODirection.INPUT): ("InputHatch", MEDeviceKind.FLUID_EXPORT_BUS),
}

# --- item identities, for the export (spike 7.3) ---------------------------------------------------

#: The item every AE2 cable and AE2 part is a damage of.
PART_ITEM = "appliedenergistics2:item.ItemMultiPart"
#: The item every AE2 upgrade card is a damage of.
MATERIAL_ITEM = "appliedenergistics2:item.ItemMultiMaterial"
#: A cable's item damage base; its colour's :attr:`AEColor.ordinal` is added.
CABLE_DAMAGE: dict[MECableKind, int] = {
    MECableKind.GLASS: 0,
    MECableKind.COVERED: 20,
    MECableKind.SMART: 40,
    MECableKind.DENSE: 60,
    MECableKind.DENSE_COVERED: 520,
}
#: Each AE2 part's item damage; an AE2FluidCraft part is an item of its own (:data:`FC_PART_ITEMS`).
PART_DAMAGE: dict[MEDeviceKind, int] = {
    MEDeviceKind.STORAGE_BUS: 220,
    MEDeviceKind.IMPORT_BUS: 240,
    MEDeviceKind.EXPORT_BUS: 260,
    MEDeviceKind.INTERFACE: 440,
}
#: Each AE2FluidCraft part's item, damage 0.
FC_PART_ITEMS: dict[MEDeviceKind, str] = {
    MEDeviceKind.FLUID_IMPORT_BUS: "ae2fc:part_fluid_import",
    MEDeviceKind.FLUID_EXPORT_BUS: "ae2fc:part_fluid_export",
    MEDeviceKind.FLUID_STORAGE_BUS: "ae2fc:part_fluid_storage_bus",
    MEDeviceKind.DUAL_INTERFACE: "ae2fc:part_fluid_interface",
}
#: Each upgrade card's damage of :data:`MATERIAL_ITEM`, by its :class:`MECards` field.
CARD_DAMAGE: dict[str, int] = {"acceleration": 30, "super_speed": 56, "capacity": 27}

#: How each device is named in game, for a builder reading a layout.
DEVICE_NAMES: dict[MEDeviceKind, str] = {
    MEDeviceKind.IMPORT_BUS: "ME Import Bus",
    MEDeviceKind.EXPORT_BUS: "ME Export Bus",
    MEDeviceKind.STORAGE_BUS: "ME Storage Bus",
    MEDeviceKind.INTERFACE: "ME Interface",
    MEDeviceKind.FLUID_IMPORT_BUS: "ME Fluid Import Bus",
    MEDeviceKind.FLUID_EXPORT_BUS: "ME Fluid Export Bus",
    MEDeviceKind.FLUID_STORAGE_BUS: "ME Fluid Storage Bus",
    MEDeviceKind.DUAL_INTERFACE: "ME Dual Interface",
    MEDeviceKind.GT_OUTPUT_BUS_ME: "Output Bus (ME)",
    MEDeviceKind.GT_OUTPUT_HATCH_ME: "Output Hatch (ME)",
    MEDeviceKind.GT_STOCKING_INPUT_BUS_ME: "Stocking Input Bus (ME)",
    MEDeviceKind.GT_STOCKING_INPUT_HATCH_ME: "Stocking Input Hatch (ME)",
}
_CARD_NAMES = (
    ("acceleration", "Acceleration Card"),
    ("super_speed", "Hyper-Acceleration Card"),
    ("capacity", "Capacity Card"),
)
#: How a normal hatch kind is named in game, for a label.
_HATCH_NAMES = {
    "OutputBus": "Output Bus",
    "OutputHatch": "Output Hatch",
    "InputBus": "Input Bus",
    "InputHatch": "Input Hatch",
}

#: The buses that are carded to a rate, by the commodity they move.
_BUS_COMMODITY: dict[MEDeviceKind, Commodity] = {
    MEDeviceKind.IMPORT_BUS: Commodity.ITEM,
    MEDeviceKind.EXPORT_BUS: Commodity.ITEM,
    MEDeviceKind.FLUID_IMPORT_BUS: Commodity.FLUID,
    MEDeviceKind.FLUID_EXPORT_BUS: Commodity.FLUID,
}


# --- tiers ---------------------------------------------------------------------------------------


def _tier_index(tier: str) -> int | None:
    """GT's tier number for ``tier`` (ULV 0, LV 1, ... EV 4, LuV 6, UV 8), or None off the ladder."""
    ladder = list(VOLTAGE_BY_TIER)
    return ladder.index(tier) if tier in ladder else None


def reaches(tier: str, floor: str) -> bool:
    """Whether ``tier`` is ``floor`` or above. A tier off the ladder reaches nothing, so a line whose
    tier is unknown is never assumed to have what a higher tier unlocks."""
    have, need = _tier_index(tier), _tier_index(floor)
    return have is not None and need is not None and have >= need


def super_speed_allowed(line_tier: str, *, enabled: bool = True) -> bool:
    """Whether a bus on a line whose highest tier is ``line_tier`` may be fitted Hyper-Acceleration
    Cards: the network allows them (``enabled``) and the line reaches :data:`SUPER_SPEED_TIER`."""
    return enabled and reaches(line_tier, SUPER_SPEED_TIER)


# --- rates -----------------------------------------------------------------------------------------


def bus_per_operation(commodity: Commodity, cards: MECards) -> int:
    """What one operation of an item bus (items) or fluid bus (mB) moves, with ``cards`` fitted.

    Raises :class:`ValueError` for power, which no bus moves, and :class:`IndexError` for more than
    four cards of a speed kind (AE refuses the fifth).
    """
    if commodity is Commodity.ITEM:
        return ITEM_ACCELERATION[cards.acceleration] + ITEM_SUPER_SPEED[cards.super_speed]
    if commodity is Commodity.FLUID:
        return (
            FLUID_BASE_MB
            * FLUID_ACCELERATION[cards.acceleration]
            * FLUID_SUPER_SPEED[cards.super_speed]
        )
    raise ValueError(f"no ME bus moves {commodity.value}")


def bus_rate(commodity: Commodity, cards: MECards) -> float:
    """Items/t or mB/t a bus with ``cards`` moves while it has work every operation."""
    return bus_per_operation(commodity, cards) / BUS_PERIOD_TICKS


def _speed_cards(*, super_speed: bool) -> list[MECards]:
    """Every speed-card fit one bus can take, fewest cards first and, among equally many, fewest
    Hyper-Acceleration Cards first; none of those at all when ``super_speed`` is off."""
    return [
        MECards(acceleration=total - supers, super_speed=supers)
        for total in range(UPGRADE_SLOTS + 1)
        for supers in (range(total + 1) if super_speed else (0,))
    ]


def bus_cards_for(commodity: Commodity, rate: float, *, super_speed: bool = True) -> MECards | None:
    """The fewest speed cards with which one bus moves ``rate`` per tick, or ``None`` if none do.

    Fewest cards first, since each costs a slot and a craft; among equally many, the fewest
    Hyper-Acceleration Cards, which are LuV-gated; ``super_speed=False`` allows none. The cards
    always fit in :data:`UPGRADE_SLOTS`.
    """
    return next(
        (c for c in _speed_cards(super_speed=super_speed) if bus_rate(commodity, c) >= rate), None
    )


def max_bus_rate(commodity: Commodity, *, super_speed: bool = True) -> float:
    """The most one bus moves per tick with the best speed cards it may take."""
    return max(bus_rate(commodity, c) for c in _speed_cards(super_speed=super_speed))


def output_bus_push_rate(tier: str) -> float:
    """Items/t a normal GT output bus of ``tier`` pushes into what faces its front.

    Every :data:`OUTPUT_BUS_PUSH_TICKS` it pushes up to its ``(tier + 1)^2`` slots of 64 (spike 4.8).
    A tier off the ladder is read as ULV, the smallest bus, so the figure never overstates.
    """
    index = _tier_index(tier) or 0
    return (index + 1) ** 2 * 64 / OUTPUT_BUS_PUSH_TICKS


def single_block_fluid_push_rate(recipe_ticks: float) -> float:
    """mB/t a single block's fluid auto-output pushes at most, for a recipe of ``recipe_ticks``.

    It pushes up to :data:`SINGLE_BLOCK_FLUID_PUSH_MB` on each completion and every
    :data:`SINGLE_BLOCK_FLUID_PUSH_TICKS` (spike 4.7). A faster output stays in the machine, which
    stops starting recipes once the next would not fit, whatever takes it from the face.
    """
    return SINGLE_BLOCK_FLUID_PUSH_MB * (1 / recipe_ticks + 1 / SINGLE_BLOCK_FLUID_PUSH_TICKS)


def network_ae_per_tick(
    *,
    idle: float,
    channel_load: float = 0.0,
    items_per_tick: float = 0.0,
    fluid_operations_per_tick: float = 0.0,
) -> float:
    """AE/t a network draws, as the pack's AE2 extracts it.

    ``idle`` is its devices' nominal idle draws summed (:data:`DEFAULT_IDLE_AE` each, a controller
    :data:`CONTROLLER_IDLE_AE`); ``channel_load`` the channels through every node and every
    connection, summed, the way AE counts them (spike 6.2); ``items_per_tick`` the items it inserts
    or extracts per tick, each move counted; ``fluid_operations_per_tick`` the 1000 mB charges its
    fluid moves start per tick. Everything is extracted times :data:`USAGE_MULTIPLIER`.
    """
    nominal = (
        idle
        + channel_load / CHANNEL_LOAD_PER_AE
        + items_per_tick * AE_PER_ITEM
        + fluid_operations_per_tick * AE_PER_FLUID_OPERATION
    )
    return nominal * USAGE_MULTIPLIER


def ae_to_eu(ae: float) -> float:
    """GT EU that buys ``ae`` AE: an energy acceptor turns 1 EU into :data:`AE_PER_EU` AE."""
    return ae / AE_PER_EU


# --- choosing a port's device ----------------------------------------------------------------------


@dataclass(frozen=True)
class MEDeviceChoice:
    """One device serving a machine port's ME connection.

    ``hatch_kind`` is set on a multiblock: the kind of hatch slot the connection takes, either the
    GT ME hatch itself (``gt_mid`` set) or the normal hatch the AE2 part stands in front of.
    ``per_tick`` is the most it moves per tick, or ``None`` when nothing of the device's own bounds
    it (a stocking hatch, an interface fed by a pusher that bounds nothing).
    """

    kind: MEDeviceKind
    cards: MECards = field(default_factory=MECards)
    gt_mid: int | None = None
    hatch_kind: str | None = None
    per_tick: float | None = None

    @property
    def label(self) -> str:
        """How a builder names it: ``"ME Export Bus (2 x Acceleration Card)"``, ``"Output Bus
        (ME)"``, ``"Output Bus + ME Interface"``."""
        name = DEVICE_NAMES[self.kind]
        if self.hatch_kind is not None and self.gt_mid is None:
            name = f"{_HATCH_NAMES.get(self.hatch_kind, self.hatch_kind)} + {name}"
        fitted = [
            f"{count} x {card}"
            for field, card in _CARD_NAMES
            if (count := getattr(self.cards, field)) > 0
        ]
        return f"{name} ({', '.join(fitted)})" if fitted else name


@dataclass(frozen=True)
class MEShortfall:
    """No device, one or two, keeps up with a port: the reason, in numbers.

    ``capacity`` is the most the best of them moves, two at once; ``rate`` what the port needs.
    """

    commodity: Commodity
    direction: IODirection
    rate: float
    capacity: float

    @property
    def detail(self) -> str:
        """One sentence a user can act on."""
        unit = "items/t" if self.commodity is Commodity.ITEM else "mB/t"
        return (
            f"an ME {self.commodity.value} {self.direction.value} of {self.rate:g} {unit} is more "
            f"than two ME devices move ({self.capacity:g} {unit})"
        )


#: Most devices one port is split across: a second bus or hatch, then a shortfall (#329's decision).
MAX_DEVICES_PER_PORT = 2


@dataclass(frozen=True)
class _Option:
    """One way to serve a port: a device for a given rate share, and how many it may be split into."""

    commodity: Commodity
    template: MEDeviceChoice
    carded: bool
    max_count: int

    def device(self, share: float, *, super_speed: bool) -> MEDeviceChoice | None:
        """This option at a ``share`` of the port's rate, or None if it cannot keep up."""
        if self.carded:
            cards = bus_cards_for(self.commodity, share, super_speed=super_speed)
            if cards is None:
                return None
            return MEDeviceChoice(
                kind=self.template.kind,
                cards=cards,
                hatch_kind=self.template.hatch_kind,
                per_tick=bus_rate(self.commodity, cards),
            )
        cap = self.template.per_tick
        return self.template if cap is None or cap >= share else None

    def ceiling(self, *, super_speed: bool) -> float:
        """The most this option moves at its largest split: infinite when nothing bounds it."""
        if self.carded:
            return max_bus_rate(self.commodity, super_speed=super_speed) * self.max_count
        cap = self.template.per_tick
        return float("inf") if cap is None else cap * self.max_count


def me_devices_for(
    commodity: Commodity,
    direction: IODirection,
    rate: float | None,
    *,
    multiblock: bool,
    machine_tier: str,
    line_tier: str,
    hatches: MEHatchPolicy = MEHatchPolicy.TIER_AWARE,
    auto_output: bool = False,
    super_speed: bool = True,
    push_rate: float | None = None,
) -> tuple[MEDeviceChoice, ...] | MEShortfall:
    """The ME device(s) serving one machine port, or the shortfall when none keep up.

    ``direction`` is the port's: an OUTPUT gives to the network, an INPUT takes from it. ``rate`` is
    what the port moves per tick (items/t, mB/t), ``None`` when the plan does not say, which is
    served as no rate at all. ``machine_tier`` sizes a normal output bus's push. ``line_tier``, the
    line's highest, decides what a ``tier_aware`` policy allows and whether Hyper-Acceleration
    Cards may be fitted (:func:`super_speed_allowed`), which ``super_speed=False`` forbids
    outright.

    On a single block, ``auto_output`` says this output leaves through the machine's auto-output
    face, so an interface on that face receives it, and ``push_rate`` is the most the machine pushes
    through that face per tick (:func:`single_block_fluid_push_rate`), ``None`` when that bounds
    nothing. The caller decides which of a machine's outputs auto-outputs, and gives one ME Dual
    Interface a single block's item and fluid outputs when both ride one network.

    Devices are tried in the order the module docstring draws, one device and then
    :data:`MAX_DEVICES_PER_PORT`; the first that keeps up is returned, as that many equal devices
    each carrying an equal share. Raises :class:`ValueError` for a power port, which ME never serves.
    """
    if commodity is Commodity.POWER:
        raise ValueError("an ME device moves items or fluids, never power")
    need = rate or 0.0
    allowed = super_speed_allowed(line_tier, enabled=super_speed)
    groups = _option_groups(
        commodity,
        direction,
        multiblock=multiblock,
        machine_tier=machine_tier,
        line_tier=line_tier,
        hatches=hatches,
        auto_output=auto_output,
        push_rate=push_rate,
    )
    for group in groups:
        for count in range(1, MAX_DEVICES_PER_PORT + 1):
            for option in group:
                if count > option.max_count:
                    continue
                device = option.device(need / count, super_speed=allowed)
                if device is not None:
                    return (device,) * count
    ceiling = max(o.ceiling(super_speed=allowed) for group in groups for o in group)
    return MEShortfall(commodity=commodity, direction=direction, rate=need, capacity=ceiling)


def _option_groups(
    commodity: Commodity,
    direction: IODirection,
    *,
    multiblock: bool,
    machine_tier: str,
    line_tier: str,
    hatches: MEHatchPolicy,
    auto_output: bool,
    push_rate: float | None,
) -> list[list[_Option]]:
    """The ways to serve a port, in groups tried in turn; within a group, one device each is tried
    before two of any."""
    if multiblock:
        return _multiblock_groups(commodity, direction, machine_tier, line_tier, hatches)
    if direction is IODirection.INPUT:
        bus = (
            MEDeviceKind.EXPORT_BUS
            if commodity is Commodity.ITEM
            else MEDeviceKind.FLUID_EXPORT_BUS
        )
        return [[_bus(commodity, bus)]]
    pull = MEDeviceKind.IMPORT_BUS if commodity is Commodity.ITEM else MEDeviceKind.FLUID_IMPORT_BUS
    if not auto_output:
        return [[_bus(commodity, pull)]]
    receiver = (
        MEDeviceKind.INTERFACE if commodity is Commodity.ITEM else MEDeviceKind.DUAL_INTERFACE
    )
    # A single block has one auto-output face, so its interface is never doubled.
    interface = _Option(
        commodity, MEDeviceChoice(kind=receiver, per_tick=push_rate), carded=False, max_count=1
    )
    return [[interface, _bus(commodity, pull)]]


def _multiblock_groups(
    commodity: Commodity,
    direction: IODirection,
    machine_tier: str,
    line_tier: str,
    hatches: MEHatchPolicy,
) -> list[list[_Option]]:
    """A multiblock port's ways: its GT ME hatch where the policy allows it, and a normal hatch with
    the AE2 part in front of it (spike 4.8)."""
    hatch_kind, part = _NORMAL_HATCH_PART[(commodity, direction)]
    if part in _BUS_COMMODITY:
        normal = _bus(commodity, part, hatch_kind=hatch_kind)
    else:
        # An output: the normal bus pushes into the interface, the normal hatch its whole tank.
        cap = output_bus_push_rate(machine_tier) if hatch_kind == "OutputBus" else None
        normal = _Option(
            commodity,
            MEDeviceChoice(kind=part, hatch_kind=hatch_kind, per_tick=cap),
            carded=False,
            max_count=MAX_DEVICES_PER_PORT,
        )
    gt = GT_ME_HATCHES[_GT_ME_HATCH_FOR[(commodity, direction)]]
    gt_option = _Option(
        commodity,
        MEDeviceChoice(kind=gt.kind, gt_mid=gt.mid, hatch_kind=gt.hatch_kind, per_tick=gt.per_tick),
        carded=False,
        max_count=MAX_DEVICES_PER_PORT,
    )
    if hatches is MEHatchPolicy.ALWAYS:
        return [[gt_option], [normal]]
    if hatches is MEHatchPolicy.TIER_AWARE and reaches(line_tier, gt.tier):
        return [[gt_option, normal]]
    return [[normal]]


def _bus(commodity: Commodity, kind: MEDeviceKind, *, hatch_kind: str | None = None) -> _Option:
    return _Option(
        commodity,
        MEDeviceChoice(kind=kind, hatch_kind=hatch_kind),
        carded=True,
        max_count=MAX_DEVICES_PER_PORT,
    )
