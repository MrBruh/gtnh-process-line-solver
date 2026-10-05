"""AE2 in a ``.schematic``: the in-game golden, read back block by block and part by part (#339).

``tests/golden/schematic/ae2-golden-cmd.schematic`` and ``ae2-golden-gui.schematic`` are one build
the maintainer made in a 2.9.0-beta-3 world and saved twice, with ``/schematicaSave`` and from the
GUI; ``ae2-golden-items.json`` is that world's FML item table, cut to the items they name. What the
saves show is written up in ``docs/spikes/329-me-ae2.md`` 7.5, and every fact that section states is
pinned here, so a reader change that misreads a cable bus fails against a file nobody here wrote.

The golden, by cell (x, y, z), y up::

    y=0  z=0  dense   smart   covered  glass   CONTROLLER  ACCEPTOR  GT cable  power source
              Fluix   Fluix   Fluix    Fluix
                      +FC     +FC
                      import  export
                      (north) (north)
         z=1  .       .       green    .       orange      .         GT cable
         z=2  .       Super   green    orange  orange      .         GT cable
                      Chest   +storage +export +import
                              (west)   (up)    (up)
                              +FC storage (up)
         z=3  .       .       green
    y=1       DRIVE (3,1,0), glass (4,1,0), Super Tank (2,1,2), LV Macerator (3,1,2), green (2,1,3),
              ME INTERFACE block (3,1,3)
    y=2..4    glass (4,2..4,0) up to an LV Output Bus (3,4,0) and Output Hatch (5,4,0) facing it
    y=5       Stocking Input Bus (ME) (4,5,0), facing down onto the glass, painted black
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gtnh_solver.schematic import nbt, read_schematic
from gtnh_solver.schematic.read import AETile, ItemRef, Schematic

_GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden" / "schematic"
_SAVES = ("ae2-golden-cmd", "ae2-golden-gui")
#: The golden world's item table: registry name -> numeric id.
_ITEMS: dict[str, int] = json.loads(
    (_GOLDEN / "ae2-golden-items.json").read_text(encoding="utf-8")
)["items"]

_CABLE_BUS = "appliedenergistics2:tile.BlockCableBus"
_PART = _ITEMS["appliedenergistics2:item.ItemMultiPart"]
_CARD = _ITEMS["appliedenergistics2:item.ItemMultiMaterial"]
#: ForgeDirection ordinals.
_DOWN, _UP, _NORTH, _SOUTH, _WEST, _EAST = range(6)


def _golden(name: str) -> Schematic:
    return read_schematic(_GOLDEN / f"{name}.schematic")


def _ae(schematic: Schematic, x: int, y: int, z: int) -> AETile:
    tile = schematic.tile_at(x, y, z)
    assert tile is not None, f"no tile entity at {(x, y, z)}"
    ae = tile.ae
    assert ae is not None, f"{tile.id} at {(x, y, z)} is not an AE2 tile"
    return ae


def _fluid(stack: nbt.Compound) -> str:
    assert stack["StackType"] == "fluid"
    return str(stack["FluidName"])


def _item(stack: nbt.Compound) -> tuple[int, int]:
    assert stack["StackType"] == "item"
    return int(stack["id"]), int(stack["Damage"])


# --------------------------------------------------------------------------------- reading back


@pytest.mark.parametrize("name", _SAVES)
def test_every_ae2_block_of_the_golden_reads_back_by_name(name: str) -> None:
    """The five AE2 blocks the list asks for, each at its cell, and nothing unmapped."""
    schematic = _golden(name)

    assert schematic.size == (8, 6, 4)
    assert not [n for n in schematic.histogram(air=True) if n.startswith("<unmapped:")]
    assert schematic.histogram() == {
        _CABLE_BUS: 15,
        "gregtech:gt.blockmachines": 15,
        "appliedenergistics2:tile.BlockController": 1,
        "appliedenergistics2:tile.BlockDrive": 1,
        "appliedenergistics2:tile.BlockEnergyAcceptor": 1,
        "appliedenergistics2:tile.BlockInterface": 1,
    }
    # A controller's Data is AE's live state (0 offline, 1 online): this one was powered.
    assert schematic.block_at(4, 0, 0) == ("appliedenergistics2:tile.BlockController", 1)
    assert schematic.block_at(5, 0, 0) == ("appliedenergistics2:tile.BlockEnergyAcceptor", 0)
    assert schematic.block_at(3, 1, 0) == ("appliedenergistics2:tile.BlockDrive", 0)
    assert schematic.block_at(3, 1, 3) == ("appliedenergistics2:tile.BlockInterface", 0)
    assert all(data == 0 for _, block, data in schematic.iter_cells() if block == _CABLE_BUS)


@pytest.mark.parametrize("name", _SAVES)
def test_a_block_device_says_which_way_it_was_placed(name: str) -> None:
    """Controller, acceptor and drive store AE's orientation as ForgeDirection names; the interface
    block is omnidirectional until wrenched (``UNKNOWN`` both ways, ``pointAt`` 6, spike 7.4)."""
    schematic = _golden(name)

    for cell in ((4, 0, 0), (5, 0, 0), (3, 1, 0)):
        ae = _ae(schematic, *cell)
        assert (ae.forward, ae.up) == ("NORTH", "UP")
        assert ae.cable is None
        assert not ae.parts
    assert _ae(schematic, 4, 0, 0).painted == 16  # a controller unpainted is Fluix
    assert _ae(schematic, 3, 1, 0).painted == 16
    assert _ae(schematic, 5, 0, 0).painted is None  # an acceptor has no colour of its own
    interface = _ae(schematic, 3, 1, 3)
    assert (interface.forward, interface.up) == ("UNKNOWN", "UNKNOWN")
    tile = schematic.tile_at(3, 1, 3)
    assert tile is not None
    assert tile.raw["pointAt"] == 6


@pytest.mark.parametrize("name", _SAVES)
def test_the_drive_holds_its_1k_cell(name: str) -> None:
    tile = _golden(name).tile_at(3, 1, 0)
    assert tile is not None
    cell = tile.raw["inv"]["item0"]
    assert ItemRef.from_nbt(cell) == ItemRef(
        _ITEMS["appliedenergistics2:item.ItemBasicStorageCell.1k"], 0, 1
    )
    assert cell["tag"]["it"] == 1  # one item type stored
    assert all(not tile.raw["inv"][f"item{n}"] for n in range(1, 10))


#: Every cable cell's ``def:6`` damage: ``CABLE_DAMAGE[kind] + AEColor ordinal`` (spike 7.3).
_CABLES = {
    (0, 0, 0): 60 + 16,  # dense smart, Fluix
    (1, 0, 0): 40 + 16,  # smart, Fluix
    (2, 0, 0): 20 + 16,  # covered, Fluix: it carries a part, so covered cable takes parts
    (3, 0, 0): 0 + 16,  # glass, Fluix, ending at the controller
    (4, 1, 0): 16,  # the straight glass run up to the hatches
    (4, 2, 0): 16,
    (4, 3, 0): 16,
    (4, 4, 0): 16,
    (4, 0, 1): 40 + 1,  # smart, Orange
    (3, 0, 2): 41,
    (4, 0, 2): 41,
    (2, 0, 1): 40 + 13,  # smart, Green
    (2, 0, 2): 53,
    (2, 0, 3): 53,
    (2, 1, 3): 53,
}


@pytest.mark.parametrize("name", _SAVES)
def test_every_cable_reads_back_as_its_kind_and_colour(name: str) -> None:
    schematic = _golden(name)

    buses = {
        pos: _ae(schematic, *pos) for pos, block, _ in schematic.iter_cells() if block == _CABLE_BUS
    }
    assert set(buses) == set(_CABLES)
    for pos, ae in buses.items():
        assert ae.cable == ItemRef(_PART, _CABLES[pos], 1), pos
        assert (ae.forward, ae.up, ae.painted) == (None, None, None)  # a cable bus never rotates
        tile = schematic.tile_at(*pos)
        assert tile is not None
        # The centre cable writes nothing of its own, and no part keeps its grid node: Schematica
        # saves a tile entity by loading a fresh copy of it, which has none yet.
        assert tile.raw["extra:6"] == {}
        assert not [k for k in tile.raw if k in ("part", "proxy")]
        assert not [k for part in ae.parts.values() for k in part.extra if k in ("part", "proxy")]


@pytest.mark.parametrize("name", _SAVES)
def test_every_part_reads_back_on_its_side_with_its_cards_and_config(name: str) -> None:
    """The buses the list asks for, each by item, side, cards and filter or partition."""
    schematic = _golden(name)
    acceleration, super_speed = ItemRef(_CARD, 30, 1), ItemRef(_CARD, 56, 1)

    fluid_import = _ae(schematic, 1, 0, 0).parts
    assert set(fluid_import) == {_NORTH}
    assert fluid_import[_NORTH].item == ItemRef(_ITEMS["ae2fc:part_fluid_import"], 0, 1)
    assert fluid_import[_NORTH].upgrades == ()
    assert fluid_import[_NORTH].config == ()

    (fluid_export,) = _ae(schematic, 2, 0, 0).parts.values()
    assert (fluid_export.side, fluid_export.item) == (
        _NORTH,
        ItemRef(_ITEMS["ae2fc:part_fluid_export"], 0, 1),
    )
    assert fluid_export.upgrades == (acceleration,)
    assert [_fluid(s) for s in fluid_export.config] == ["water"]

    # One cable bus, two parts on different sides: a storage bus west onto the Super Chest,
    # partitioned to sand, and a fluid storage bus up onto the Super Tank, partitioned to water.
    two = _ae(schematic, 2, 0, 2).parts
    assert set(two) == {_WEST, _UP}
    assert two[_WEST].item == ItemRef(_PART, 220, 1)
    assert [_item(s) for s in two[_WEST].config] == [(_ITEMS["minecraft:sand"], 0)]
    assert two[_UP].item == ItemRef(_ITEMS["ae2fc:part_fluid_storage_bus"], 0, 1)
    assert [_fluid(s) for s in two[_UP].config] == ["water"]
    assert two[_WEST].upgrades == two[_UP].upgrades == ()

    (export,) = _ae(schematic, 3, 0, 2).parts.values()
    assert (export.side, export.item) == (_UP, ItemRef(_PART, 260, 1))
    assert export.upgrades == (acceleration, super_speed)  # one card a slot, in slot order
    assert [_item(s) for s in export.config] == [(_ITEMS["minecraft:cobblestone"], 0)]

    (imported,) = _ae(schematic, 4, 0, 2).parts.values()
    assert (imported.side, imported.item) == (_UP, ItemRef(_PART, 240, 1))
    assert imported.upgrades == (acceleration,)
    assert imported.config == ()  # unconfigured: no config tag at all, not an empty one
    assert "config" not in imported.extra


@pytest.mark.parametrize("name", _SAVES)
def test_each_part_faces_the_block_it_works_on(name: str) -> None:
    """The storage buses face the Super Chest and Super Tank, the export bus the macerator's bottom,
    and the macerator auto-outputs south into the ME Interface block."""
    schematic = _golden(name)

    def mid(x: int, y: int, z: int) -> int | None:
        tile = schematic.tile_at(x, y, z)
        return tile.mid if tile is not None else None

    assert mid(1, 0, 2) == 135  # Super Chest I, west of the two-part bus
    assert mid(2, 1, 2) == 130  # Super Tank I, above it
    macerator = schematic.tile_at(3, 1, 2)
    assert macerator is not None
    assert macerator.mid == 301  # Basic Macerator, above the export bus
    assert (macerator.placed_facing, macerator.facing) == (_NORTH, _SOUTH)
    assert macerator.raw["mItemTransfer"] == 1
    assert schematic.block_at(3, 1, 3)[0] == "appliedenergistics2:tile.BlockInterface"


@pytest.mark.parametrize("name", _SAVES)
def test_the_gt_hatches_face_the_cable_and_the_me_one_is_painted(name: str) -> None:
    """Three GT hatches face the glass cable at (4, 4, 0). Only the stocking bus is an ME hatch
    (2718); the other two are a normal LV Output Bus (81) and Output Hatch (61), so the golden pins
    no Output Bus or Output Hatch (ME). GT stores paint as its dye plus one: mColor 1 is dye 0,
    black, which AE reads as ``AEColor`` 15 (spike 5.2)."""
    schematic = _golden(name)

    hatches = {pos: schematic.tile_at(*pos) for pos in ((3, 4, 0), (5, 4, 0), (4, 5, 0))}
    found = {pos: (t.mid, t.facing, int(t.raw["mColor"])) for pos, t in hatches.items() if t}
    assert found == {
        (3, 4, 0): (81, _EAST, 0),
        (5, 4, 0): (61, _WEST, 0),
        (4, 5, 0): (2718, _DOWN, 1),
    }
    assert all(t is not None and t.id == "BaseMetaTileEntity" for t in hatches.values())


@pytest.mark.parametrize("name", _SAVES)
def test_the_acceptor_is_fed_by_a_gt_cable(name: str) -> None:
    schematic = _golden(name)
    cable = schematic.tile_at(6, 0, 0)
    assert cable is not None
    assert cable.mid == 1250  # 12x Tin Cable
    assert cable.connections is not None
    assert cable.connections & (1 << _WEST)  # into the acceptor
    source = schematic.tile_at(7, 0, 0)
    assert source is not None
    assert source.mid == 15498  # Debug Power Generator


def test_the_two_saves_differ_only_in_live_state() -> None:
    """``/schematicaSave`` reads the server world and the GUI the client's; for AE2 they agree but
    for ``hasRedstone`` (the server had settled three buses to NO, 1; the GUI save keeps 2,
    undecided) and the controller's stored power."""
    cmd, gui = (_golden(n) for n in _SAVES)
    live = {"hasRedstone", "internalCurrentPower"}

    def ae_tiles(schematic: Schematic) -> dict[tuple[int, int, int], dict[str, object]]:
        return {
            t.pos: {k: v for k, v in t.raw.items() if k not in live}
            for t in schematic.tile_entities
            if t.ae is not None
        }

    assert ae_tiles(cmd) == ae_tiles(gui)
    assert {t.raw["hasRedstone"] for t in gui.tile_entities if t.id == "BlockCableBus"} == {2}
    assert {
        t.pos for t in cmd.tile_entities if t.id == "BlockCableBus" and t.raw["hasRedstone"] != 2
    } == {(2, 0, 0), (3, 0, 2), (4, 0, 2)}


def test_every_item_id_in_the_golden_is_in_the_extract_of_its_world() -> None:
    """The extract is the world the golden was saved in: every numeric id its AE2 tiles name, in a
    stack or a filter, resolves there (another world of the same instance numbers them apart)."""
    by_id = {v: k for k, v in _ITEMS.items()}
    named: set[int] = set()
    for tile in _golden("ae2-golden-cmd").tile_entities:
        ae = tile.ae
        if ae is None or ae.cable is None:
            continue
        named.add(ae.cable.id)
        for part in ae.parts.values():
            named |= {part.item.id, *(card.id for card in part.upgrades)}
            named |= {int(s["id"]) for s in part.config if s["StackType"] == "item"}
    assert named <= set(by_id)
    assert {by_id[i] for i in named} == {
        "appliedenergistics2:item.ItemMultiPart",
        "appliedenergistics2:item.ItemMultiMaterial",
        "ae2fc:part_fluid_import",
        "ae2fc:part_fluid_export",
        "ae2fc:part_fluid_storage_bus",
        "minecraft:cobblestone",
        "minecraft:sand",
    }


def test_a_tile_ae2_did_not_write_has_no_ae_view() -> None:
    schematic = _golden("ae2-golden-cmd")
    tile = schematic.tile_at(3, 1, 2)
    assert tile is not None
    assert tile.ae is None  # the macerator


def test_a_cable_bus_with_a_malformed_part_reads_what_is_there() -> None:
    """A part with no ``extra`` reads as one with nothing written, and an inventory with a stray
    key keeps only its numbered slots: a reader that tolerates what AE tolerates."""
    raw = nbt.Compound(
        {
            "id": nbt.String("BlockCableBus"),
            "def:3": nbt.Compound(
                {"id": nbt.Short(4631), "Count": nbt.Byte(1), "Damage": nbt.Short(240)}
            ),
            "extra:2": nbt.Compound(
                {
                    "upgrades": nbt.Compound(
                        {"#1": nbt.Compound({"id": nbt.Short(4630)}), "x": nbt.Int(0)}
                    )
                }
            ),
            "def:2": nbt.Compound({"id": nbt.Short(4631), "Damage": nbt.Short(260)}),
        }
    )
    from gtnh_solver.schematic.read import _tile_entity

    ae = _tile_entity(raw).ae
    assert ae is not None
    assert ae.cable is None
    assert ae.parts[3].extra == {}
    assert ae.parts[2].upgrades == (ItemRef(4630, 0, 0),)
