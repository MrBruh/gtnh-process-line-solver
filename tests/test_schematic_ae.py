"""AE2 in a ``.schematic``: the in-game golden, read back block by block and part by part (#339).

``tests/golden/schematic/ae2-golden-cmd.schematic`` and ``ae2-golden-gui.schematic`` are one build
the maintainer made in a 2.9.0-beta-3 world and saved twice, with ``/schematicaSave`` and from the
GUI; ``ae2-golden-items.json`` is that world's FML item table, cut to the items they name. What the
saves show is written up in ``docs/spikes/329-me-ae2.md`` 7.5, and every fact that section states is
pinned here, so a reader change that misreads a cable bus fails against a file nobody here wrote.
The export (:mod:`gtnh_solver.schematic.ae`) is then held to the same file: a layout of the
golden's AE2 side exports its tile entities tag for tag.

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
import warnings
from collections.abc import Iterable
from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file, load_plan, to_input_ir
from gtnh_solver.dataset import CHEMICAL_PLANT, load_physical_dataset
from gtnh_solver.dataset.me import CABLE_DAMAGE, PART_DAMAGE
from gtnh_solver.ir import (
    AEColor,
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    Machine,
    MECableKind,
    MECards,
    MEConfig,
    MEDeviceKind,
    MEMode,
    MENetworkLayout,
    MENetworkSpec,
    MEPlacedDevice,
    MERole,
    PlacedHatch,
)
from gtnh_solver.ir.geometry import rotated_slot
from gtnh_solver.previewer.scene import build_scene
from gtnh_solver.previewer.textures import TextureManifest, load_multiblock_docs
from gtnh_solver.schematic import (
    SchematicError,
    SchematicWarning,
    build_schematic,
    nbt,
    read_schematic,
    write_schematic,
)
from gtnh_solver.schematic.ae import _orientation, _settings, _WorldItems, gt_colour
from gtnh_solver.schematic.core import FORGE_DIRECTION
from gtnh_solver.schematic.read import AEPart, AETile, ItemRef, Schematic
from gtnh_solver.solver import solve
from tests._helpers import world_save
from tests._me_fixtures import MAIN, at, cable, controller, coord

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


# ------------------------------------------------------------------------------------- exporting
#
# The export is checked against the golden it was written from: a layout that builds the golden's
# cables, parts, controller and acceptor at the golden's own cells must export the very tile
# entities Schematica saved, tag for tag and type for type.

_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"
_COMMITTED_MULTIBLOCKS = Path(__file__).resolve().parents[1] / "data" / "multiblocks"
_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_ORANGE, _GREEN = "orange", "green"


def _typed(value: object) -> object:
    """``value`` with every NBT tag's type kept beside it: Python's ``Short(4) == Int(4)``, so a
    plain ``==`` would pass a tag written at the wrong width."""
    if isinstance(value, dict):
        return {key: _typed(item) for key, item in value.items()}
    if isinstance(value, nbt.List):
        return ("List", value.element_type, [_typed(item) for item in value])
    return (type(value).__name__, value)


def _manifest() -> TextureManifest:
    return TextureManifest.load(_COMMITTED_MANIFEST)


def _export(
    problem: InputIR, layout: LayoutResult, item_ids: dict[str, int] | None = _ITEMS
) -> Schematic:
    root = build_schematic(problem, layout, manifest=_manifest(), item_ids=item_ids)
    return read_schematic(nbt.dumps("Schematic", root))


def _part(
    kind: MEDeviceKind,
    cell: tuple[int, int, int],
    side: Facing,
    *,
    cards: MECards | None = None,
    config: tuple[str, ...] = (),
) -> MEPlacedDevice:
    return MEPlacedDevice(
        machine_id="m",
        endpoint_id=f"{kind.value}@{cell}",
        kind=kind,
        cell=coord(*cell),
        side=side,
        cards=cards or MECards(),
        config=config,
    )


def _golden_layout() -> tuple[InputIR, LayoutResult]:
    """The golden's AE2 side as a layout (module docstring): its cables in three networks by
    colour, its six buses with their cards and filters, its controller and its acceptor."""
    acceptor = Machine(
        id="acc",
        type="ME Energy Acceptor",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        me_role=MERole.ACCEPTOR,
        me_network=MAIN,
    )
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=6, sz=4),
        machines=[controller(network=MAIN), acceptor],
        me=MEConfig(
            networks=[
                MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED),
                MENetworkSpec(id=_ORANGE, mode=MEMode.SUBNET, colour=AEColor.ORANGE),
                MENetworkSpec(id=_GREEN, mode=MEMode.SUBNET, colour=AEColor.GREEN),
            ]
        ),
    )
    glass, smart = MECableKind.GLASS, MECableKind.SMART
    main_cables = [
        cable(0, 0, 0, MECableKind.DENSE),
        cable(1, 0, 0, smart),
        cable(2, 0, 0, MECableKind.COVERED),
        *(cable(x, y, 0, glass) for x, y in ((3, 0), (4, 1), (4, 2), (4, 3), (4, 4))),
    ]
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at("ctrl", 4, 0, 0, Facing.NORTH), at("acc", 5, 0, 0, Facing.NORTH)],
        me_networks=[
            MENetworkLayout(
                id=MAIN,
                colour=AEColor.FLUIX,
                cables=main_cables,
                devices=[
                    _part(MEDeviceKind.FLUID_IMPORT_BUS, (1, 0, 0), Facing.NORTH),
                    _part(
                        MEDeviceKind.FLUID_EXPORT_BUS,
                        (2, 0, 0),
                        Facing.NORTH,
                        cards=MECards(acceleration=1),
                        config=("water",),
                    ),
                ],
            ),
            MENetworkLayout(
                id=_ORANGE,
                colour=AEColor.ORANGE,
                cables=[cable(4, 0, 1), cable(3, 0, 2), cable(4, 0, 2)],
                devices=[
                    _part(
                        MEDeviceKind.EXPORT_BUS,
                        (3, 0, 2),
                        Facing.UP,
                        cards=MECards(acceleration=1, super_speed=1),
                        config=("minecraft:cobblestone",),
                    ),
                    _part(
                        MEDeviceKind.IMPORT_BUS, (4, 0, 2), Facing.UP, cards=MECards(acceleration=1)
                    ),
                ],
            ),
            MENetworkLayout(
                id=_GREEN,
                colour=AEColor.GREEN,
                cables=[cable(2, 0, 1), cable(2, 0, 2), cable(2, 0, 3), cable(2, 1, 3)],
                devices=[
                    _part(
                        MEDeviceKind.STORAGE_BUS, (2, 0, 2), Facing.WEST, config=("minecraft:sand",)
                    ),
                    _part(MEDeviceKind.FLUID_STORAGE_BUS, (2, 0, 2), Facing.UP, config=("water",)),
                ],
            ),
        ],
    )
    return problem, layout


def test_a_layout_of_the_golden_exports_its_tile_entities_tag_for_tag() -> None:
    """Every cable bus, the controller and the acceptor, against the GUI save, whose live state is
    a fresh block's (``hasRedstone`` 2, no stored power). One tag differs, and is the slot's, not
    the export's: the fluid export bus's water was set with 1000 mB, and the export writes 1 for
    every filter, since no bus reads the amount (spike 7.5)."""
    problem, layout = _golden_layout()
    golden = _golden("ae2-golden-gui")
    with pytest.warns(SchematicWarning, match=r"printer applies no tile-entity NBT"):
        exported = _export(problem, layout)

    cells = [*_CABLES, (4, 0, 0), (5, 0, 0)]
    for pos in cells:
        ours, theirs = exported.tile_at(*pos), golden.tile_at(*pos)
        assert ours is not None, pos
        assert theirs is not None, pos
        expected = nbt.Compound(theirs.raw)
        if pos == (2, 0, 0):
            assert expected["extra:2"]["config"]["#0"]["Cnt"] == 1000
            expected["extra:2"]["config"]["#0"]["Cnt"] = nbt.Long(1)
        assert _typed(ours.raw) == _typed(expected), pos
        assert exported.block_at(*pos)[0] == golden.block_at(*pos)[0], pos
    # Block metadata: the golden's controller was online (1); a placed one starts offline.
    assert golden.block_at(4, 0, 0)[1] == 1
    assert all(exported.block_at(*pos)[1] == 0 for pos in cells)
    assert {t.pos for t in exported.tile_entities} == set(cells)


def test_a_subnet_controller_is_painted_its_networks_colour() -> None:
    problem, layout = _golden_layout()
    problem = problem.model_copy(
        update={"machines": [controller(network=_GREEN), *problem.machines[1:]]}
    )
    with pytest.warns(SchematicWarning):
        exported = _export(problem, layout)
    assert _ae(exported, 4, 0, 0).painted == AEColor.GREEN.ordinal
    acceptor = exported.tile_at(5, 0, 0)
    assert acceptor is not None
    assert "paintedColor" not in acceptor.raw  # uncoloured, so it joins any network


def test_a_block_facing_up_takes_south_as_its_up() -> None:
    assert _orientation(Facing.EAST) == ("EAST", "UP")
    assert _orientation(Facing.UP) == ("UP", "SOUTH")
    assert _orientation(Facing.DOWN) == ("DOWN", "SOUTH")


def test_without_a_world_no_cable_bus_is_written_and_each_is_counted() -> None:
    """A cable bus names its items by the world's ids, so no world, no cable bus; the controller
    and the acceptor need none and are written all the same."""
    problem, layout = _golden_layout()
    with pytest.warns(
        SchematicWarning, match=r"15 AE2 cable block\(s\) and the 6 part\(s\) on them are left out"
    ) as caught:
        exported = _export(problem, layout, item_ids=None)
    (message,) = [str(w.message) for w in caught if "ME networks" in str(w.message)]
    assert "--world" in message
    assert "printer" not in message  # nothing of a cable bus was written for it to skip
    assert exported.histogram() == {
        "appliedenergistics2:tile.BlockController": 1,
        "appliedenergistics2:tile.BlockEnergyAcceptor": 1,
    }


def test_a_world_without_ae2_is_refused_rather_than_exported_without_it() -> None:
    """A GT:NH world without AE2 (it has GT's cover item, so the covers' check passes it)."""
    problem, layout = _golden_layout()
    no_ae2 = {
        k: v for k, v in _ITEMS.items() if not k.startswith(("appliedenergistics2:", "ae2fc:"))
    }
    with pytest.raises(SchematicError, match=r"no appliedenergistics2:item.ItemMultiPart"):
        _export(problem, layout, item_ids=no_ae2)


def test_a_filter_item_the_world_lacks_is_refused_by_name() -> None:
    problem, layout = _golden_layout()
    no_sand = {k: v for k, v in _ITEMS.items() if k != "minecraft:sand"}
    with pytest.raises(SchematicError, match=r"no minecraft:sand, so the export cannot name the"):
        _export(problem, layout, item_ids=no_sand)


def test_an_item_filter_keeps_its_meta() -> None:
    """A resource ``registry@meta`` is that item at that damage: ``gt.metaitem.01@2299`` is the
    dust in the golden's drive cell."""
    stack = _WorldItems(_ITEMS).filter_stack("gregtech:gt.metaitem.01@2299", fluid=False)
    assert stack is not None
    assert (stack["id"], stack["Damage"]) == (_ITEMS["gregtech:gt.metaitem.01"], 2299)


def test_an_interface_part_is_left_off_its_cable_and_listed() -> None:
    """No golden has an Interface or a Dual Interface part, so neither is written; the cable still
    is, and the warning says where each goes."""
    problem, layout = _golden_layout()
    main = layout.me_networks[0]
    extra = [
        _part(MEDeviceKind.INTERFACE, (3, 0, 0), Facing.SOUTH),
        _part(MEDeviceKind.DUAL_INTERFACE, (4, 1, 0), Facing.WEST),
    ]
    layout = layout.model_copy(
        update={
            "me_networks": [
                main.model_copy(update={"devices": [*main.devices, *extra]}),
                *layout.me_networks[1:],
            ]
        }
    )
    with pytest.warns(SchematicWarning) as caught:
        exported = _export(problem, layout)
    (message,) = [str(w.message) for w in caught if "ME networks" in str(w.message)]
    assert "2 part(s) are not written" in message
    assert "ME Interface x1: south side of the cable at (3, 0, 0)" in message
    assert "ME Dual Interface x1: west side of the cable at (4, 1, 0)" in message
    assert "15 AE2 cable block(s) with 6 part(s) are written" in message
    assert not _ae(exported, 3, 0, 0).parts
    assert not _ae(exported, 4, 1, 0).parts


def test_an_unverified_part_is_refused_if_it_ever_reaches_the_writer() -> None:
    with pytest.raises(SchematicError, match=r"no golden shows what an AE2 interface writes"):
        _settings(MEDeviceKind.INTERFACE)


def _plant_with_an_me_hatch(colour: AEColor) -> tuple[InputIR, LayoutResult, tuple[int, int, int]]:
    """gtnh-nitrobenzene's Chemical Plant alone, with a Stocking Input Bus (ME) in one of its input
    bus slots, on a network of ``colour``; the hatch's cell is returned too."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        adapted = adapt_file(
            str(_EXAMPLES / "gtnh-nitrobenzene.json"),
            physical=load_physical_dataset(_COMMITTED_MULTIBLOCKS),
        )
    plant = next(m for m in adapted.machines if m.block_key == CHEMICAL_PLANT)
    plant = plant.model_copy(update={"faces": plant.faces.model_copy(update={"ports": []})})
    slot = next(s for s in plant.hatch_slots if "InputBus" in s.kinds)
    cell = rotated_slot(slot.offset.as_tuple(), plant.footprint, Facing.NORTH)
    attached = colour is AEColor.FLUIX
    spec = (
        MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED)
        if attached
        else MENetworkSpec(id=MAIN, mode=MEMode.SUBNET, colour=colour)
    )
    problem = InputIR(
        bounding_region=plant.footprint, machines=[plant], me=MEConfig(networks=[spec])
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[at(plant.id, 0, 0, 0, Facing.NORTH)],
        hatches=[
            PlacedHatch(machine_id=plant.id, kind="InputBus", cell=coord(*cell), facing=Facing.UP)
        ],
        me_networks=[
            MENetworkLayout(
                id=MAIN,
                colour=colour,
                devices=[
                    MEPlacedDevice(
                        machine_id=plant.id,
                        endpoint_id="in",
                        kind=MEDeviceKind.GT_STOCKING_INPUT_BUS_ME,
                        cell=coord(*cell),
                        side=Facing.UP,
                        gt_mid=2718,
                    )
                ],
            )
        ],
    )
    return problem, layout, cell


def _export_plant(problem: InputIR, layout: LayoutResult) -> Schematic:
    docs = load_multiblock_docs(_COMMITTED_MULTIBLOCKS)
    root = build_schematic(problem, layout, manifest=_manifest(), docs=docs)
    return read_schematic(nbt.dumps("Schematic", root))


def test_a_gt_me_hatch_is_written_by_its_mid_and_painted_as_the_golden_one_is() -> None:
    """The golden's Stocking Input Bus (ME), painted black, carries ``mID`` 2718 and ``mColor`` 1
    with ``mFacing`` onto its cable; a black subnet's hatch exports those same tags."""
    problem, layout, cell = _plant_with_an_me_hatch(AEColor.BLACK)
    ours = _export_plant(problem, layout).tile_at(*cell)
    theirs = _golden("ae2-golden-cmd").tile_at(4, 5, 0)
    assert ours is not None
    assert theirs is not None
    shared = ("id", "mID", "mColor")
    assert _typed({k: ours.raw[k] for k in shared}) == _typed({k: theirs.raw[k] for k in shared})
    assert _typed(ours.raw["mFacing"]) == _typed(nbt.Short(FORGE_DIRECTION[Facing.UP]))
    assert type(theirs.raw["mFacing"]) is nbt.Short


def test_a_gt_me_hatch_on_a_fluix_network_is_left_unpainted() -> None:
    problem, layout, cell = _plant_with_an_me_hatch(AEColor.FLUIX)
    ours = _export_plant(problem, layout).tile_at(*cell)
    assert ours is not None
    assert ours.mid == 2718
    assert "mColor" not in ours.raw


def test_gt_colour_is_the_dye_plus_one() -> None:
    assert gt_colour(AEColor.FLUIX) == 0
    assert gt_colour(AEColor.BLACK) == 1  # the golden's painted hatch
    assert gt_colour(AEColor.WHITE) == 16


def test_a_gt_me_hatch_the_manifest_cannot_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The texture pass keeps the casing where the manifest lacks the hatch; exporting that would
    give a multiblock that forms without its ME hatch, so it is refused by name."""
    problem, layout, _ = _plant_with_an_me_hatch(AEColor.BLACK)
    monkeypatch.setattr(TextureManifest, "me_hatch", lambda self, mid: None)
    with pytest.raises(SchematicError, match=r"Stocking Input Bus \(ME\) \(mID 2718\)"):
        _export_plant(problem, layout)


def test_sand_on_me_exports_every_cable_and_part_and_reads_them_back() -> None:
    """``gtnh-solve examples/gtnh-sand.json --me items`` for a named world: every cable cell is a
    cable bus of its kind and colour, every part on its side with its cards and filter, read back
    through ``read_schematic``; the interfaces are the parts left off and listed."""
    plan = load_plan(str(_EXAMPLES / "gtnh-sand.json"))
    problem = to_input_ir(plan, me_commodities={Commodity.ITEM})
    layout = solve(problem)
    assert layout.status is LayoutStatus.VALID
    (network,) = layout.me_networks
    low = build_scene(problem, layout)["bounds"]["min"]

    with pytest.warns(SchematicWarning) as caught:
        exported = _export(problem, layout)

    def rel(cell: CellCoord) -> tuple[int, int, int]:
        return (cell.x - int(low[0]), cell.y - int(low[1]), cell.z - int(low[2]))

    for built in network.cables:
        ae = _ae(exported, *rel(built.cell))
        assert ae.cable == ItemRef(_PART, CABLE_DAMAGE[built.kind] + network.colour.ordinal, 1)
    interfaces = [d for d in network.devices if d.kind is MEDeviceKind.INTERFACE]
    assert interfaces  # sand's machines push their products into interfaces
    for device in network.devices:
        parts = _ae(exported, *rel(device.cell)).parts
        side = FORGE_DIRECTION[device.side]
        if device.kind is MEDeviceKind.INTERFACE:
            assert side not in parts
            continue
        part = parts[side]
        assert part.item == ItemRef(_PART, PART_DAMAGE[device.kind], 1)
        assert len(part.upgrades) == device.cards.count
        assert [_item(s) for s in part.config] == [(_ITEMS[r], 0) for r in device.config]
    (message,) = [str(w.message) for w in caught if "ME networks" in str(w.message)]
    assert f"{len(interfaces)} part(s) are not written" in message
    assert f"{len(network.cables)} AE2 cable block(s)" in message


def test_a_line_with_no_me_gets_no_ae2_block_and_no_word_of_it(
    solved_sand: tuple[InputIR, LayoutResult],
) -> None:
    """With a world or without, a line without ME exports no AE2 block and says nothing of ME (a
    world adds its covers, which are #328's to check)."""
    problem, layout = solved_sand
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for item_ids in (None, _ITEMS):
            root = build_schematic(problem, layout, manifest=_manifest(), item_ids=item_ids)
            names = read_schematic(nbt.dumps("Schematic", root)).histogram()
            assert not [n for n in names if n.startswith("appliedenergistics2:")]
    assert not [w for w in caught if "ME networks" in str(w.message)]


def test_write_schematic_passes_the_world_through(tmp_path: Path) -> None:
    problem, layout = _golden_layout()
    with pytest.warns(SchematicWarning):
        path = write_schematic(problem, layout, tmp_path / "ae.schematic", item_ids=_ITEMS)
    assert read_schematic(path).histogram()[_CABLE_BUS] == len(_CABLES)


def test_two_networks_on_one_cell_write_the_first() -> None:
    """A clash the validator reports; the export writes the cell once, as the first network has it."""
    problem, layout = _golden_layout()
    orange = layout.me_networks[1]
    clash = orange.model_copy(update={"cables": [*orange.cables, cable(3, 0, 0)]})
    layout = layout.model_copy(
        update={"me_networks": [layout.me_networks[0], clash, layout.me_networks[2]]}
    )
    with pytest.warns(SchematicWarning, match=r"15 AE2 cable block"):
        exported = _export(problem, layout)
    assert _ae(exported, 3, 0, 0).cable == ItemRef(_PART, _CABLES[(3, 0, 0)], 1)


# ---------------------------------------------------------------------- --inspect-schematic (#339)

_NAMES = {number: name for name, number in _ITEMS.items()}


def test_inspect_lists_every_ae2_tile_and_names_the_cables_without_a_world(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without the world's table only ItemMultiPart can be named, read off a cable: the cables and
    the AE2 buses resolve, and FC's buses and the cards print as the world's raw ids."""
    from gtnh_solver.cli import main

    assert main(["--inspect-schematic", str(_GOLDEN / "ae2-golden-cmd.schematic")]) == 0
    out = capsys.readouterr().out
    assert f"AE2 (item ids are the saving world's; its cables say ItemMultiPart is {_PART})" in out
    lines = [line.strip() for line in out.splitlines()]
    assert "BlockController      at (4, 0, 0): forward north, up up, painted fluix" in lines
    assert "BlockEnergyAcceptor  at (5, 0, 0): forward north, up up" in lines
    assert "BlockInterface       at (3, 1, 3): forward unknown, up unknown" in lines
    assert "BlockCableBus        at (0, 0, 0): ME Dense Smart Cable (Fluix)" in lines
    assert (
        "BlockCableBus        at (2, 0, 2): ME Smart Cable (Green); up: item 4655:0, set to water; "
        "west: ME Storage Bus, set to item 12:0"
    ) in lines
    assert (
        "BlockCableBus        at (3, 0, 2): ME Smart Cable (Orange); up: ME Export Bus, cards item "
        "4630:30 + item 4630:56, set to item 4:0"
    ) in lines


def test_with_the_worlds_table_every_item_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    from gtnh_solver.cli import _print_ae

    _print_ae(_golden("ae2-golden-gui"), _NAMES)
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert lines[1] == "AE2 (items named by --world's item table)"
    assert (
        "BlockCableBus        at (1, 0, 0): ME Smart Cable (Fluix); north: ME Fluid Import Bus"
        in (lines)
    )
    assert (
        "BlockCableBus        at (2, 0, 2): ME Smart Cable (Green); up: ME Fluid Storage Bus, set "
        "to water; west: ME Storage Bus, set to minecraft:sand"
    ) in lines
    assert (
        "BlockCableBus        at (3, 0, 2): ME Smart Cable (Orange); up: ME Export Bus, cards "
        "Acceleration Card + Hyper-Acceleration Card, set to minecraft:cobblestone"
    ) in lines


def test_a_file_without_ae2_prints_no_ae2_section(capsys: pytest.CaptureFixture[str]) -> None:
    from gtnh_solver.cli import _print_ae

    _print_ae(read_schematic(_GOLDEN / "sand.schematic"))
    assert capsys.readouterr().out == ""


def test_an_item_is_named_as_far_as_what_is_known_allows() -> None:
    from gtnh_solver.schematic.ae import describe_item, describe_tile, table_part_item_id

    assert describe_item(ItemRef(7639, 2299, 1), part_item=_PART, names=_NAMES) == (
        "gregtech:gt.metaitem.01@2299"
    )
    assert describe_item(ItemRef(9999, 0, 1), part_item=_PART, names=_NAMES) == "item 9999:0"
    assert describe_item(ItemRef(_CARD, 30, 1), part_item=_PART) == f"item {_CARD}:30"
    assert describe_item(ItemRef(_PART, 999, 1), part_item=_PART) == f"item {_PART}:999"
    # A world's table names ItemMultiPart with no cable to read it off; a table without AE2 cannot.
    assert table_part_item_id(_NAMES) == _PART
    assert table_part_item_id({}) is None
    bare = AETile(cable=None, parts={}, forward=None, up=None, painted=None, has_redstone=None)
    assert describe_tile(bare, part_item=None) == "no AE2 tags"
    cableless = AETile(
        cable=None,
        parts={1: AEPart(1, ItemRef(_PART, 240, 1), nbt.Compound())},
        forward=None,
        up=None,
        painted=None,
        has_redstone=2,
    )
    assert describe_tile(cableless, part_item=_PART) == "no cable; up: ME Import Bus"


# ------------------------------------------------------------------------ --world on the CLI (#339)


def test_cli_inspect_with_the_saving_world_names_every_ae2_item(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from gtnh_solver.cli import main

    world = world_save(tmp_path, _ITEMS)
    assert (
        main(
            [
                "--inspect-schematic",
                str(_GOLDEN / "ae2-golden-gui.schematic"),
                "--world",
                str(world),
            ]
        )
        == 0
    )
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert "AE2 (items named by --world's item table)" in lines
    assert (
        "BlockCableBus        at (2, 0, 0): ME Covered Cable (Fluix); north: ME Fluid Export Bus, "
        "cards Acceleration Card, set to water"
    ) in lines


def test_cli_inspect_with_an_unreadable_world_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from gtnh_solver.cli import main

    golden = str(_GOLDEN / "ae2-golden-gui.schematic")
    assert main(["--inspect-schematic", golden, "--world", str(tmp_path / "nope")]) == 2
    assert "cannot read --world: no level.dat" in capsys.readouterr().err


# ------------------------------------------------------------------- review follow-ups (#339)


def _with_devices(
    layout: LayoutResult, network: int, devices: list[MEPlacedDevice]
) -> LayoutResult:
    """``layout`` with network ``network``'s devices replaced by ``devices``."""
    networks = list(layout.me_networks)
    networks[network] = networks[network].model_copy(update={"devices": devices})
    return layout.model_copy(update={"me_networks": networks})


def _me_message(caught: Iterable[warnings.WarningMessage]) -> str:
    (message,) = [str(w.message) for w in caught if "ME networks" in str(w.message)]
    return message


def test_a_wildcard_filter_slot_is_left_unset_and_listed() -> None:
    """``minecraft:log@32767`` means any log, which AE2 matches only through a Fuzzy Card, so the
    slot is left empty (the slots after it keep their numbers) and the warning names it."""
    problem, layout = _golden_layout()
    export, imported = layout.me_networks[1].devices
    wild = export.model_copy(update={"config": ("minecraft:log@32767", "minecraft:cobblestone")})
    layout = _with_devices(layout, 1, [wild, imported])
    with pytest.warns(SchematicWarning) as caught:
        exported = _export(problem, layout)
    part = _ae(exported, 3, 0, 2).parts[_UP]
    assert set(part.extra["config"]) == {"#1"}
    assert [_item(s) for s in part.config] == [(_ITEMS["minecraft:cobblestone"], 0)]
    message = _me_message(caught)
    assert "1 filter slot(s) are left unset" in message
    assert "Fuzzy Card" in message
    assert "ME Export Bus on the up side of the cable at (3, 0, 2): minecraft:log@32767" in message


def test_a_bus_set_only_to_a_wildcard_writes_no_config() -> None:
    problem, layout = _golden_layout()
    export, imported = layout.me_networks[1].devices
    wild = export.model_copy(update={"config": ("minecraft:log@32767",)})
    with pytest.warns(SchematicWarning, match=r"1 filter slot\(s\) are left unset"):
        exported = _export(problem, _with_devices(layout, 1, [wild, imported]))
    assert "config" not in _ae(exported, 3, 0, 2).parts[_UP].extra


def test_a_part_where_no_cable_is_laid_is_counted_rather_than_dropped() -> None:
    """The validator refuses such a layout; the export still says so rather than lose the part."""
    problem, layout = _golden_layout()
    stray = _part(MEDeviceKind.IMPORT_BUS, (6, 0, 3), Facing.EAST)
    layout = _with_devices(layout, 0, [*layout.me_networks[0].devices, stray])
    for item_ids in (_ITEMS, None):
        with pytest.warns(SchematicWarning) as caught:
            exported = _export(problem, layout, item_ids=item_ids)
        message = _me_message(caught)
        assert "1 part(s) stand where the layout lays no cable" in message
        assert "ME Import Bus x1: east side of the cell at (6, 0, 3)" in message
        assert exported.tile_at(6, 0, 3) is None


def test_capacity_cards_take_the_slots_after_the_speed_cards() -> None:
    """One card a slot, in ``CARD_DAMAGE`` order: Acceleration, Hyper-Acceleration, Capacity."""
    problem, layout = _golden_layout()
    export, imported = layout.me_networks[1].devices
    carded = export.model_copy(update={"cards": MECards(acceleration=1, super_speed=1, capacity=2)})
    with pytest.warns(SchematicWarning):
        exported = _export(problem, _with_devices(layout, 1, [carded, imported]))
    part = _ae(exported, 3, 0, 2).parts[_UP]
    assert list(part.extra["upgrades"]) == ["#0", "#1", "#2", "#3"]
    assert part.upgrades == (
        ItemRef(_CARD, 30, 1),
        ItemRef(_CARD, 56, 1),
        ItemRef(_CARD, 27, 1),
        ItemRef(_CARD, 27, 1),
    )


def test_a_controller_facing_up_exports_up_with_south_as_its_up() -> None:
    problem, layout = _golden_layout()
    placements = [
        at("ctrl", 4, 0, 0, Facing.UP) if p.machine_id == "ctrl" else p for p in layout.placements
    ]
    layout = layout.model_copy(update={"placements": placements})
    with pytest.warns(SchematicWarning):
        exported = _export(problem, layout)
    ae = _ae(exported, 4, 0, 0)
    assert (ae.forward, ae.up) == ("UP", "SOUTH")


def test_inspect_with_another_worlds_table_says_so_and_keeps_the_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The golden's sibling world puts ItemMultiPart at 4356 and a Tinkers' item at 4631; read
    through it, every cable would print as that item. One line says the file was saved elsewhere,
    and the section falls back to what the file itself names."""
    from gtnh_solver.cli import main

    sibling = {"appliedenergistics2:item.ItemMultiPart": 4356, "TConstruct:potionLauncher": _PART}
    world = world_save(tmp_path, sibling)
    golden = str(_GOLDEN / "ae2-golden-gui.schematic")
    assert main(["--inspect-schematic", golden, "--world", str(world)]) == 0
    captured = capsys.readouterr()
    assert (
        "warning: --world's item table puts ItemMultiPart at 4356, but this file's cables use "
        f"{_PART}: it was saved in another world, so its items stay numbers"
    ) in captured.err
    assert f"AE2 (item ids are the saving world's; its cables say ItemMultiPart is {_PART})" in (
        captured.out
    )
    assert "BlockCableBus        at (0, 0, 0): ME Dense Smart Cable (Fluix)" in captured.out
    assert "TConstruct" not in captured.out


def test_inspect_with_a_table_without_ae2_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    from gtnh_solver.cli import _print_ae

    _print_ae(_golden("ae2-golden-gui"), {1: "minecraft:stone"})
    captured = capsys.readouterr()
    assert "puts ItemMultiPart nowhere" in captured.err
    assert "ME Dense Smart Cable (Fluix)" in captured.out


def test_a_table_names_the_parts_of_a_file_with_no_cable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no cable to read ItemMultiPart's id off, the world's table supplies it."""
    from gtnh_solver.cli import _print_ae

    root = nbt.Compound(_golden("ae2-golden-gui").root)
    kept = [t for t in root["TileEntities"] if t["id"] != "BlockCableBus"]
    lone = nbt.Compound(
        {
            "id": nbt.String("BlockCableBus"),
            "x": nbt.Int(1),
            "y": nbt.Int(0),
            "z": nbt.Int(0),
            "def:1": nbt.Compound(
                {"id": nbt.Short(_PART), "Count": nbt.Byte(1), "Damage": nbt.Short(240)}
            ),
            "extra:1": nbt.Compound(),
        }
    )
    root["TileEntities"] = nbt.List(nbt.TAG_COMPOUND, [*kept, lone])
    _print_ae(read_schematic(nbt.dumps("Schematic", root)), _NAMES)
    out = capsys.readouterr().out
    assert "AE2 (items named by --world's item table)" in out
    assert "BlockCableBus        at (1, 0, 0): no cable; up: ME Import Bus" in out


def test_an_odd_tile_reads_as_what_it_says() -> None:
    """Colours out of AE2's range print as their number; a missing ``up`` is not printed."""
    from gtnh_solver.schematic.ae import describe_tile

    def block(painted: int | None, forward: str | None = None, up: str | None = None) -> AETile:
        return AETile(
            cable=None, parts={}, forward=forward, up=up, painted=painted, has_redstone=None
        )

    assert describe_tile(block(16), part_item=None) == "painted fluix"
    assert describe_tile(block(0), part_item=None) == "painted white"
    assert describe_tile(block(17), part_item=None) == "painted 17"
    assert describe_tile(block(-1), part_item=None) == "painted -1"
    assert describe_tile(block(None, "NORTH"), part_item=None) == "forward north"


def test_a_mistyped_item_number_reads_as_0_as_minecraft_reads_it() -> None:
    """``NBTTagCompound.getShort`` on a missing or mistyped tag gives 0; so do the reader and the
    inspect line, rather than throwing on a String ``id``."""
    from gtnh_solver.schematic.ae import describe_tile
    from gtnh_solver.schematic.read import _tile_entity

    as_string = nbt.Compound({"id": nbt.String("appliedenergistics2:item.ItemMultiPart")})
    assert ItemRef.from_nbt(as_string) == ItemRef(0, 0, 0)
    raw = nbt.Compound(
        {
            "id": nbt.String("BlockCableBus"),
            "def:6": nbt.Compound({"id": nbt.Short(_PART), "Damage": nbt.Short(16)}),
            "def:1": nbt.Compound({"id": nbt.Short(_PART), "Damage": nbt.Short(260)}),
            "extra:1": nbt.Compound(
                {"config": nbt.Compound({"#0": nbt.Compound({"id": nbt.String("x")})})}
            ),
        }
    )
    ae = _tile_entity(raw).ae
    assert ae is not None
    assert describe_tile(ae, part_item=_PART) == (
        "ME Glass Cable (Fluix); up: ME Export Bus, set to item 0:0"
    )


def test_only_ascii_numbered_keys_are_inventory_slots() -> None:
    """``str.isdigit`` passes a superscript two, which ``int`` then refuses; a key not led by ``#``
    is no slot either."""
    from gtnh_solver.schematic.read import AEPart, _slot

    assert _slot("#0") == 0
    assert _slot("#12") == 12
    assert _slot("#²") is None
    assert _slot("x1") is None
    assert _slot("#") is None
    card = nbt.Compound({"id": nbt.Short(_CARD), "Damage": nbt.Short(30)})
    extra = nbt.Compound(
        {"upgrades": nbt.Compound({"#²": card, "x1": card, "#1": card, "#0": nbt.Int(5)})}
    )
    assert AEPart(1, ItemRef(_PART, 260, 1), extra).upgrades == (ItemRef(_CARD, 30, 0),)
