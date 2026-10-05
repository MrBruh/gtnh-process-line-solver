"""Lower a layout's ME networks to AE2 blocks and tile entities (#339), and name them back.

Every tag written here is one the maintainer's in-game golden shows AE2 writing
(``tests/golden/schematic/ae2-golden-*.schematic``, ``docs/spikes/329-me-ae2.md`` 7.5), for a
fresh block or part set up the way the layout says::

    layout.me_networks + problem.machines (me_role) + placements
        |
        |-- controller, acceptor   their AE2 block, oriented as placed, a controller painted its
        |                          network's colour                    (no item id needed)
        |-- GT ME hatch            written by core like any hatch, by its mID (the texture pass
        |                          resolves it, TextureManifest.me_hatch); painted here when its
        |                          network is coloured                 (no item id needed)
        '-- cable cell             BlockCableBus: def:6 the cable, def:N / extra:N each part on
                                   side N with its cards (upgrades) and filter or partition
                                   (config)                            (the world's item ids)

**A cable bus is written only for a named world.** Every item in one, the cable, each part, each
card and an item filter, is an ItemStack numbered by the world's FML item table, which differs
per world, and Schematica never remaps tile NBT; the golden's ids name Tinkers' items in another
world of the same instance. So, as for a GT cover, the export writes cable buses only when the
caller passes that world's table (``item_ids``), and otherwise leaves each cable cell out and
counts it (:func:`warn_about_me`). A fluid filter names its fluid by name and needs no id, but it
rides a part that does.

**Schematica's printer builds none of a cable bus.** It places a block by a simulated click with
its pick-block item and applies no tile NBT (``docs/DOMAIN.md``, Platform), and a cable bus is all
tile NBT: a printed one is empty. The ghost shows every cable, part, card and filter, and the
builder places them from it by hand. A controller and an acceptor print as blocks, facing the
player, like any block.

**Two parts are not written yet.** The golden has no ME Interface part and no AE2FluidCraft Dual
Interface part (it outputs into an Interface block instead), so their NBT is unverified and the
export leaves them off their cable, listing each for the builder (:data:`UNVERIFIED_PARTS`).

**A bus set to an item at any damage** (``@32767``) carries a Fuzzy Card (#353), written in the
upgrade slot after its speed cards, with its filter written as that stack and the bus's fuzzy mode
left at the ``IGNORE_ALL`` every bus writes (:func:`_settings`). The card makes AE2 match the
wildcard through the ore dictionary, every item of the stack's ore names registered at 32767 (the
vanilla logs, as ``logWood``), or a damageable item at any durability; any other item it still
matches only at 32767 (spike 4.2). No golden holds a Fuzzy Card or a wildcard filter, so both are
AE2's source alone (spike 7.5). A wildcard slot on a bus with no card, which the validator refuses,
is left unset and listed rather than written to move nothing (:meth:`_WorldItems.filter_stack`).

What is left out, and what is only in the ghost, is said in one :class:`SchematicWarning`.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from gtnh_solver.dataset.me import (
    CABLE_DAMAGE,
    CABLE_NAMES,
    CARD_DAMAGE,
    DEVICE_NAMES,
    FC_PART_ITEMS,
    MATERIAL_ITEM,
    PART_DAMAGE,
    PART_ITEM,
    wildcard_item,
)
from gtnh_solver.ir import (
    AEColor,
    Facing,
    InputIR,
    LayoutResult,
    MECableKind,
    MEDeviceKind,
    MEPlacedDevice,
    MERole,
)
from gtnh_solver.ir.geometry import occupied_cells
from gtnh_solver.previewer.textures import TextureManifest

from . import nbt
from .core import FORGE_DIRECTION, Cell, SchematicError, SchematicWarning
from .read import CABLE_SIDE, AETile, ItemRef, number

Key = tuple[int, int, int]

CABLE_BUS: Final = "appliedenergistics2:tile.BlockCableBus"
CONTROLLER: Final = "appliedenergistics2:tile.BlockController"
ACCEPTOR: Final = "appliedenergistics2:tile.BlockEnergyAcceptor"

#: The block and tile entity id each infrastructure role is written as; a stub or a link is a
#: cable cell, written with the network's cables.
_BLOCK_OF_ROLE: Final[dict[MERole, tuple[str, str]]] = {
    MERole.CONTROLLER: (CONTROLLER, "BlockController"),
    MERole.ACCEPTOR: (ACCEPTOR, "BlockEnergyAcceptor"),
}

# TODO(#339): the golden has neither part (spike 7.5, "What it lacks"); write them once a save of
# an Interface part and a Dual Interface part pins their NBT, and drop them from this set.
#: The parts the export leaves off their cable, because no golden shows what they write.
UNVERIFIED_PARTS: Final = frozenset({MEDeviceKind.INTERFACE, MEDeviceKind.DUAL_INTERFACE})

#: The parts whose filter or partition is fluids, named by ``FluidName``; the rest take items.
_FLUID_PARTS: Final = frozenset(
    {
        MEDeviceKind.FLUID_IMPORT_BUS,
        MEDeviceKind.FLUID_EXPORT_BUS,
        MEDeviceKind.FLUID_STORAGE_BUS,
    }
)

#: A cable bus's ``hasRedstone``: ``YesNo.UNDECIDED``, a fresh bus's, and every cable bus's in the
#: GUI golden (the server save's 1s are buses it had already checked).
_REDSTONE_UNDECIDED: Final = 2
#: Every ME block's metadata. For a controller 0 is offline: 1 online and 2 conflicted are set by
#: the tile once the network runs (``TileController.updateMeta``), as the golden's 1 is.
_DATA: Final = 0


#: A bus's ``FUZZY_MODE``: ``IGNORE_ALL``, the mode that ignores damage, which every item bus
#: registers as its default (``PartSharedItemBus.java:64``, ``PartStorageBus.java:143``). It is what
#: a fitted Fuzzy Card needs, so a bus with one writes it exactly as a bus without one does.
_FUZZY_MODE: Final = "IGNORE_ALL"


def _settings(kind: MEDeviceKind) -> nbt.Compound:
    """What a fresh bus of ``kind`` writes besides its inventories: the defaults its class
    registers (``PartSharedItemBus.java:63-64``, ``PartBaseExportBus.java:53-54``,
    ``PartStorageBus.java:141-145``), exactly as the golden's buses carry them (spike 7.5). An FC
    bus writes what its AE2 parent does."""
    bus = {"filter": "", "FUZZY_MODE": _FUZZY_MODE, "REDSTONE_CONTROLLED": "IGNORE"}
    if kind in (MEDeviceKind.IMPORT_BUS, MEDeviceKind.FLUID_IMPORT_BUS):
        return nbt.Compound({k: nbt.String(v) for k, v in bus.items()})
    if kind in (MEDeviceKind.EXPORT_BUS, MEDeviceKind.FLUID_EXPORT_BUS):
        export = {**bus, "SCHEDULING_MODE": "DEFAULT", "CRAFT_ONLY": "NO"}
        return nbt.Compound(
            {**{k: nbt.String(v) for k, v in export.items()}, "nextSlot": nbt.Int(0)}
        )
    if kind in (MEDeviceKind.STORAGE_BUS, MEDeviceKind.FLUID_STORAGE_BUS):
        storage = {
            "filter": "",
            "FUZZY_MODE": _FUZZY_MODE,
            "ACCESS": "READ_WRITE",
            "EXTRACTION_MODE": "LOOSE",
            "STORAGE_FILTER": "EXTRACTABLE_ONLY",
            "STICKY_MODE": "NO",
        }
        return nbt.Compound(
            {
                **{k: nbt.String(v) for k, v in storage.items()},
                "priority": nbt.Int(0),
                "filterCache": nbt.Compound(),
            }
        )
    raise SchematicError(f"no golden shows what an AE2 {kind.value} writes, so it is not exported")


@dataclass
class MELowering:
    """What :func:`lower_me` wrote into the grid, and what it left out or only shows in the ghost."""

    cables: int = 0  # cable cells written
    parts: int = 0  # parts written on them
    blocks: int = 0  # controllers and acceptors written
    hatches: int = 0  # GT ME hatches (written by core, painted here)
    left_out_cables: int = 0  # cable cells not written: no world named
    left_out_parts: int = 0  # the parts on them
    #: Parts not written on a cable that was (:data:`UNVERIFIED_PARTS`): kind, cell, side.
    unverified: list[tuple[MEDeviceKind, Key, Facing]] = field(default_factory=list)
    #: Parts on a cell the layout lays no cable on, which a valid layout never has: kind, cell, side.
    cableless: list[tuple[MEDeviceKind, Key, Facing]] = field(default_factory=list)
    #: Config slots left unset for an item at the wildcard meta, on a bus with no Fuzzy Card (which
    #: the validator refuses): kind, cell, side, resource.
    wildcards: list[tuple[MEDeviceKind, Key, Facing, str]] = field(default_factory=list)


class _WorldItems:
    """The target world's item table, as the stacks a cable bus is made of.

    A name the table lacks is refused rather than written as some other item: the world is not a
    GT:NH one, or not the one the build goes in (the covers precedent).
    """

    def __init__(self, table: Mapping[str, int]) -> None:
        self._table = table

    def id(self, name: str, what: str) -> int:
        found = self._table.get(name)
        if found is None:
            raise SchematicError(
                f"the world's item table has no {name}, so the export cannot name {what} in it; "
                "pass the save folder of the world the build goes in"
            )
        return found

    def stack(self, name: str, damage: int, what: str) -> nbt.Compound:
        """A vanilla ItemStack of one ``name`` at ``damage``."""
        return nbt.Compound(
            {
                "id": nbt.Short(self.id(name, what)),
                "Count": nbt.Byte(1),
                "Damage": nbt.Short(damage),
            }
        )

    def filter_stack(
        self, resource: str, *, fluid: bool, fuzzy: bool = False
    ) -> nbt.Compound | None:
        """One ``config`` slot: the AE stack a bus is set to, ``resource`` a fluid's name or an item's
        ``registry[@meta]`` (the adapter's resource ids). ``Cnt`` 1 for either: no bus reads it.

        An item at the wildcard meta (``@32767``, :func:`~gtnh_solver.dataset.me.wildcard_item`)
        is written as that stack on a bus with a Fuzzy Card (``fuzzy``), as a player's filter is
        saved whatever its damage. The card makes AE2 match it through the ore dictionary or, for a
        damageable item, at any durability (``ItemList.findFuzzy``, spike 4.2); an item with
        neither still matches only at 32767. ``None`` on a bus without a card: AE2 then matches
        32767 exactly and the bus moves nothing, so the slot is left unset and the warning names it
        (:func:`warn_about_me`).
        """
        counts = {
            "Count": nbt.Byte(0),
            "Cnt": nbt.Long(1),
            "Req": nbt.Long(0),
            "Craft": nbt.Byte(0),
        }
        if fluid:
            return nbt.Compound(
                {"StackType": nbt.String("fluid"), "FluidName": nbt.String(resource), **counts}
            )
        if wildcard_item(resource) and not fuzzy:
            return None
        name, _, meta = resource.partition("@")
        damage = int(meta) if meta else 0
        return nbt.Compound(
            {
                "StackType": nbt.String("item"),
                "id": nbt.Short(self.id(name, f"the filter item {resource}")),
                "Damage": nbt.Short(damage),
                **counts,
            }
        )


def _inventory(stacks: Sequence[nbt.Compound | None]) -> nbt.Compound:
    """An AE2 inventory, one stack a slot from ``#0``; a ``None`` slot is left empty, as AE2 writes
    an empty slot (no ``#N`` tag), and the slots after it keep their numbers."""
    return nbt.Compound(
        {f"#{slot}": stack for slot, stack in enumerate(stacks) if stack is not None}
    )


def _part(
    device: MEPlacedDevice, items: _WorldItems
) -> tuple[nbt.Compound, nbt.Compound, list[str]]:
    """``(def, extra, unset)`` for one part: its item, its settings, cards and filter or partition,
    and the resources of the config slots left unset (:meth:`_WorldItems.filter_stack`).

    An empty inventory is left out, as AE2 drops its tag (``AppEngInternalInventory.writeToNBT``).
    The cards go one a slot in :data:`CARD_DAMAGE` order, Acceleration first, as the golden's
    export bus holds them, and a Fuzzy Card last.
    """
    name = DEVICE_NAMES[device.kind]
    if device.kind in PART_DAMAGE:
        stack = items.stack(PART_ITEM, PART_DAMAGE[device.kind], f"an {name}")
    else:
        stack = items.stack(FC_PART_ITEMS[device.kind], 0, f"an {name}")
    extra = _settings(device.kind)
    cards = [
        items.stack(MATERIAL_ITEM, damage, "an upgrade card")
        for card, damage in CARD_DAMAGE.items()
        for _ in range(getattr(device.cards, card))
    ]
    if cards:
        extra["upgrades"] = _inventory(cards)
    fluid = device.kind in _FLUID_PARTS
    fuzzy = device.cards.fuzzy > 0
    config = [items.filter_stack(r, fluid=fluid, fuzzy=fuzzy) for r in device.config]
    if any(slot is not None for slot in config):
        extra["config"] = _inventory(config)
    unset = [r for r, slot in zip(device.config, config, strict=True) if slot is None]
    return stack, extra, unset


def _cable_bus(
    key: Key,
    kind: MECableKind,
    colour: AEColor,
    parts: Mapping[Facing, MEPlacedDevice],
    items: _WorldItems,
    report: MELowering,
) -> nbt.Compound:
    """One cable cell's ``BlockCableBus`` tile entity, in the order AE2 visits its sides; a config
    slot left unset goes on ``report``."""
    tile = nbt.Compound(
        {
            "id": nbt.String("BlockCableBus"),
            "x": nbt.Int(key[0]),
            "y": nbt.Int(key[1]),
            "z": nbt.Int(key[2]),
            "hasRedstone": nbt.Int(_REDSTONE_UNDECIDED),
        }
    )
    for side, device in sorted(parts.items(), key=lambda kv: FORGE_DIRECTION[kv[0]]):
        stack, extra, unset = _part(device, items)
        report.wildcards.extend((device.kind, key, side, resource) for resource in unset)
        tile[f"def:{FORGE_DIRECTION[side]}"] = stack
        tile[f"extra:{FORGE_DIRECTION[side]}"] = extra
    damage = CABLE_DAMAGE[kind] + colour.ordinal
    tile[f"def:{CABLE_SIDE}"] = items.stack(PART_ITEM, damage, f"an {CABLE_NAMES[kind]}")
    tile[f"extra:{CABLE_SIDE}"] = nbt.Compound()
    return tile


def _orientation(front: Facing) -> tuple[str, str]:
    """``(orientation_forward, orientation_up)`` for a block whose front faces ``front``.

    AE2's placement rule for a block placed by hand (``AEBaseItemBlock.java:104-113``): ``up`` is
    ``UP`` and ``forward`` the horizontal front, as the golden's controller, acceptor and drive
    carry it. A front up or down comes from a player looking steeply down or up, and AE2 then sets
    ``up`` to the opposite of the way the player faced (``:115-121``), so any horizontal ``up`` is
    one a placement gives; ``SOUTH`` is the one written, as a player facing north would.
    """
    vertical = front in (Facing.UP, Facing.DOWN)
    return front.value.upper(), "SOUTH" if vertical else "UP"


def _block_tile(role: MERole, key: Key, front: Facing, colour: AEColor) -> nbt.Compound:
    """A controller's or an acceptor's tile entity, as a fresh one writes it.

    Both write ``inv`` (no slots) and ``internalCurrentPower`` (empty); a controller also its
    ``paintedColor``, the network's colour, which is what joins it to that network's cable and
    only that (``TileController.java:226``). An acceptor has no colour and joins any.
    """
    forward, up = _orientation(front)
    tile = nbt.Compound(
        {
            "id": nbt.String(_BLOCK_OF_ROLE[role][1]),
            "x": nbt.Int(key[0]),
            "y": nbt.Int(key[1]),
            "z": nbt.Int(key[2]),
            "orientation_forward": nbt.String(forward),
            "orientation_up": nbt.String(up),
        }
    )
    if role is MERole.CONTROLLER:
        tile["paintedColor"] = nbt.Byte(colour.ordinal)
    tile["inv"] = nbt.Compound()
    tile["internalCurrentPower"] = nbt.Double(0.0)
    return tile


def gt_colour(colour: AEColor) -> int:
    """GT's ``mColor`` for a GT ME hatch AE reads as ``colour``: 0 unpainted (Fluix), else the dye
    plus one, and AE reads dye ``d`` as ``AEColor`` ``15 - d`` (spike 5.2 and 7.5: ``mColor`` 1 is
    black in the golden)."""
    return 0 if colour is AEColor.FLUIX else 16 - colour.ordinal


def lower_me(
    problem: InputIR,
    layout: LayoutResult,
    grid: dict[Key, Cell],
    *,
    origin: Key,
    manifest: TextureManifest,
    item_ids: Mapping[str, int] | None,
) -> MELowering:
    """Write every ME block of ``layout`` into ``grid`` (module docstring), keyed like it.

    ``grid`` already holds the machines, a multiblock's GT ME hatch among them. A hatch whose cell
    holds anything but that mID is refused: the texture pass keeps the casing where the manifest
    cannot name the hatch, and the multiblock would then form without it.
    """
    report = MELowering()
    if not layout.me_networks:
        return report

    def rel(cell: tuple[int, int, int]) -> Key:
        return (cell[0] - origin[0], cell[1] - origin[1], cell[2] - origin[2])

    parts: dict[Key, dict[Facing, MEPlacedDevice]] = {}
    for network in layout.me_networks:
        for device in network.devices:
            key = rel(device.cell.as_tuple())
            if device.gt_mid is None:
                parts.setdefault(key, {}).setdefault(device.side, device)
                continue
            _paint_hatch(grid, key, device, network.colour, manifest)
            report.hatches += 1

    items = _WorldItems(item_ids) if item_ids is not None else None
    written: set[Key] = set()
    for network in layout.me_networks:
        for cable in network.cables:
            key = rel(cable.cell.as_tuple())
            if key in written:
                continue  # two networks on one cell is the validator's to report; the first wins
            written.add(key)
            on = parts.get(key, {})
            if items is None:
                report.left_out_cables += 1
                report.left_out_parts += len(on)
                continue
            kept = {side: d for side, d in on.items() if d.kind not in UNVERIFIED_PARTS}
            report.unverified.extend(
                (d.kind, key, side) for side, d in on.items() if d.kind in UNVERIFIED_PARTS
            )
            tile = _cable_bus(key, cable.kind, network.colour, kept, items, report)
            grid[key] = Cell(CABLE_BUS, _DATA, tile)
            report.cables += 1
            report.parts += len(kept)
    # A part sits on a cable; one whose cell the layout lays none on has nothing to be written on.
    # The validator refuses such a layout, but a file must never drop a block without saying so.
    report.cableless.extend(
        (device.kind, key, side)
        for key, on in sorted(parts.items())
        if key not in written
        for side, device in on.items()
    )

    placements = {p.machine_id: p for p in layout.placements}
    for machine in problem.machines:
        placement = placements.get(machine.id)
        role, network_id = machine.me_role, machine.me_network
        if role is None or role not in _BLOCK_OF_ROLE or network_id is None or placement is None:
            continue
        colour = problem.me.colour(network_id)
        block = _BLOCK_OF_ROLE[role][0]
        for cell in occupied_cells(placement.cell, machine.footprint, placement.orientation):
            key = rel(cell)
            tile = _block_tile(role, key, placement.orientation, colour)
            grid[key] = Cell(block, _DATA, tile)
            report.blocks += 1
    return report


def _paint_hatch(
    grid: dict[Key, Cell],
    key: Key,
    device: MEPlacedDevice,
    colour: AEColor,
    manifest: TextureManifest,
) -> None:
    """Check the GT ME hatch core wrote at ``key`` is ``device``'s, and paint it ``colour``."""
    cell = grid.get(key)
    mid = int(cell.tile["mID"]) if cell is not None and cell.tile is not None else None
    if mid != device.gt_mid:
        raise SchematicError(
            f"the GT ME hatch {DEVICE_NAMES[device.kind]} (mID {device.gt_mid}) at {key} is not in "
            f"{manifest.origin()}, so its casing cell would be written instead and the multiblock "
            "would form without it; regenerate the dataset (docs/dataset-extraction/"
            "implementation.md)"
        )
    painted = gt_colour(colour)
    if painted and cell is not None and cell.tile is not None:
        tile = nbt.Compound(cell.tile)
        tile["mColor"] = nbt.Byte(painted)
        grid[key] = Cell(cell.block, cell.data, tile)


def warn_about_me(report: MELowering) -> None:
    """Say in one warning what of the ME networks is left out, and what the ghost alone carries.

    Counted, and each unwritten part listed by cell and side, since the builder places them by
    hand: a cable bus left out for want of a world (``--world``), a part no golden pins yet, and,
    for everything written, that Schematica's printer places no cable or part (module docstring).
    """
    said: list[str] = []
    if report.left_out_cables:
        said.append(
            f"{report.left_out_cables} AE2 cable block(s) and the {report.left_out_parts} part(s) "
            "on them are left out: a cable bus names its cable, parts, cards and filters by the "
            "world's numeric item ids, which the export writes only for a named world (--world); "
            "place them by hand from the layout's me_networks"
        )
    if report.cables:
        said.append(
            f"{report.cables} AE2 cable block(s) with {report.parts} part(s) are written for the "
            "named world, but Schematica's printer applies no tile-entity NBT and a cable bus is "
            "nothing else, so it places none of their cables, parts, cards or filters: the ghost "
            "shows each, and you build them by hand from it"
        )
    if report.unverified:
        said.append(
            f"{len(report.unverified)} part(s) are not written, since no in-game save pins what "
            f"they write yet (GitHub #339), so fit them by hand: {_by_kind(report.unverified)}"
        )
    if report.cableless:
        said.append(
            f"{len(report.cableless)} part(s) stand where the layout lays no cable, so there is "
            f"nothing to write them on; the layout is broken, check it with the validator: "
            f"{_by_kind(report.cableless, on='the cell')}"
        )
    if report.wildcards:
        listed = "; ".join(
            f"{DEVICE_NAMES[kind]} on the {side.value} side of the cable at ({x}, {y}, {z}): "
            f"{resource}"
            for kind, (x, y, z), side, resource in sorted(
                report.wildcards, key=lambda w: (w[1], w[2].value, w[3])
            )
        )
        said.append(
            f"{len(report.wildcards)} filter slot(s) are left unset, since each names an item at "
            "any damage (@32767), which AE2 matches only through a Fuzzy Card, and the bus has "
            "none (the validator refuses such a layout): fit one, or set the slot to the item by "
            f"hand: {listed}"
        )
    if not said:
        return
    warnings.warn("ME networks: " + ". ".join(said) + ".", SchematicWarning, stacklevel=3)


def _by_kind(parts: Sequence[tuple[MEDeviceKind, Key, Facing]], *, on: str = "the cable") -> str:
    """``parts`` grouped by device, each by the side and cell it goes on."""
    by_kind: dict[MEDeviceKind, list[str]] = {}
    for kind, (x, y, z), side in sorted(parts, key=lambda u: (u[0].value, u[1], u[2].value)):
        by_kind.setdefault(kind, []).append(f"{side.value} side of {on} at ({x}, {y}, {z})")
    return "; ".join(
        f"{DEVICE_NAMES[kind]} x{len(where)}: {', '.join(where)}" for kind, where in by_kind.items()
    )


# --- naming what a file holds (--inspect-schematic) -----------------------------------------------
#
# A file's AE2 items are the saving world's numbers. ``names`` (that world's table turned round,
# id -> registry name) names every one; without it, one item can still be named, because a cable
# bus's centre is always a cable: its id is ItemMultiPart's in that world, and every AE2 cable and
# AE2 part is a damage of it (spike 7.3).

_CARD_NAMES: Final = {
    CARD_DAMAGE["acceleration"]: "Acceleration Card",
    CARD_DAMAGE["super_speed"]: "Hyper-Acceleration Card",
    CARD_DAMAGE["capacity"]: "Capacity Card",
    CARD_DAMAGE["fuzzy"]: "Fuzzy Card",
}


def part_item_id(tiles: Sequence[AETile]) -> int | None:
    """``ItemMultiPart``'s id in the world that saved ``tiles``, read off a cable; ``None`` with no
    cable to read it from."""
    return next((t.cable.id for t in tiles if t.cable is not None), None)


def table_part_item_id(names: Mapping[int, str]) -> int | None:
    """``ItemMultiPart``'s id in a world's item table (id to registry name), ``None`` without AE2."""
    return next((item for item, name in names.items() if name == PART_ITEM), None)


def describe_item(
    item: ItemRef, *, part_item: int | None, names: Mapping[int, str] | None = None
) -> str:
    """An AE2 cable or part by its in-game name when ``item`` is ``ItemMultiPart``, a card by its
    name, any other item by its registry name from ``names``, else its raw ``id:damage``."""
    named = names.get(item.id) if names is not None else None
    if item.id == part_item:
        for kind, base in CABLE_DAMAGE.items():
            if base <= item.damage <= base + AEColor.FLUIX.ordinal:
                colour = list(AEColor)[item.damage - base]
                return f"{CABLE_NAMES[kind]} ({colour.value.replace('_', ' ').title()})"
        for device, damage in PART_DAMAGE.items():
            if item.damage == damage:
                return DEVICE_NAMES[device]
    if named == MATERIAL_ITEM and item.damage in _CARD_NAMES:
        return _CARD_NAMES[item.damage]
    for device, fc_item in FC_PART_ITEMS.items():
        if named == fc_item:
            return DEVICE_NAMES[device]
    if named is not None:
        return named if item.damage == 0 else f"{named}@{item.damage}"
    return f"item {item.id}:{item.damage}"


def describe_tile(
    tile: AETile, *, part_item: int | None, names: Mapping[int, str] | None = None
) -> str:
    """One line for an AE2 tile: a block's orientation and colour, or a cable bus's cable and each
    part by side with its cards and filter or partition."""
    if tile.cable is None and not tile.parts:
        said = [
            f"{label} {value.lower()}"
            for label, value in (("forward", tile.forward), ("up", tile.up))
            if value is not None
        ]
        if tile.painted is not None:
            colours = list(AEColor)
            known = 0 <= tile.painted < len(colours)
            said.append(f"painted {colours[tile.painted].value if known else tile.painted}")
        return ", ".join(said) or "no AE2 tags"

    def name(item: ItemRef) -> str:
        return describe_item(item, part_item=part_item, names=names)

    sides = {ordinal: face.value for face, ordinal in FORGE_DIRECTION.items()}
    said = [name(tile.cable) if tile.cable is not None else "no cable"]
    for side, part in sorted(tile.parts.items()):
        bits = [f"{sides[side]}: {name(part.item)}"]
        if part.upgrades:
            bits.append("cards " + " + ".join(name(card) for card in part.upgrades))
        if part.config:
            bits.append(
                "set to "
                + " + ".join(
                    str(s["FluidName"])
                    if "FluidName" in s
                    else name(ItemRef(number(s, "id"), number(s, "Damage"), 1))
                    for s in part.config
                )
            )
        said.append(", ".join(bits))
    return "; ".join(said)
