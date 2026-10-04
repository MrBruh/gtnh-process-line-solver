"""Tests for how the ``.schematic`` export points a single block and names its covers (#249, D18).

A GT basic machine records two facings: ``mMainFacing``, its working face, and ``mFacing``, the
OUTPUT face it auto-outputs items and fluids through. The export used to write only ``mFacing``, as
the solver's front, so the 2.9 ghost drew it working on its bottom face and outputting out of its
front. Which face
is the output face comes from ``output_faces``, the reading the previewer's arrows use too; every
other output face needs a cover, which the export does not write, so it is warned about. A
Super Tank auto-outputs out of its front, and an Item Filter pushes out of its back.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver import cli
from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import cover_for
from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    PipeFamily,
    PipeSize,
    Placement,
    Port,
    RelativeFace,
    Route,
    RouteMaterial,
    Terminal,
)
from gtnh_solver.ir.geometry import OPPOSITE_FACE
from gtnh_solver.output_faces import CoverFace, output_faces
from gtnh_solver.previewer import build_scene
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import SchematicError, build_schematic, nbt, read_schematic
from gtnh_solver.schematic import core as schematic_core
from gtnh_solver.solver import solve

_ROOT = Path(__file__).resolve().parents[1]
_COMMITTED_MANIFEST = _ROOT / "data" / "textures" / "manifest.json"
_SAND = _ROOT / "examples" / "gtnh-sand.json"
_FORGE = schematic_core.FORGE_DIRECTION

#: The ULV Item Filter as a 2.9 client texture pass records it; the committed manifest lacks it.
_FILTER_ENTRY = {
    "kind": "mte",
    "display_name": "Ultra Low Voltage Item Filter",
    "te_base_type": 0,
    "source_class": "gregtech.common.tileentities.automation.MTEFilter",
    "tier": 0,
    "electric": True,
}


def _manifest(*, with_filter: bool = False) -> TextureManifest:
    raw: dict[str, Any] = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    if with_filter:
        raw["blocks"]["gregtech:gt.blockmachines|9240"] = _FILTER_ENTRY
    return TextureManifest(raw, source=_COMMITTED_MANIFEST)


def _hammer(mid: str, ports: list[Port]) -> Machine:
    """A single-block Forge Hammer, which the committed manifest resolves to mID 611."""
    return Machine(
        id=mid,
        type="Forge Hammer",
        recipe_map="gt.recipe.hammer",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=ports),
    )


def _storage(mid: str, type_: str, ports: list[Port]) -> Machine:
    return Machine(
        id=mid,
        type=type_,
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=ports),
    )


def _port(
    pid: str,
    direction: IODirection,
    commodity: Commodity = Commodity.ITEM,
    rate: float | None = None,
) -> Port:
    return Port(id=pid, commodity=commodity, direction=direction, rate=rate)


def _net(nid: str, src: tuple[str, str], dst: tuple[str, str], commodity: Commodity) -> Net:
    return Net(
        id=nid,
        commodity=commodity,
        fluid_or_item=nid,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=src[0], port_id=src[1]),
            MachineFaceRef(machine_id=dst[0], port_id=dst[1]),
        ],
    )


def _auto(nid: str, src: str, face: Facing, dst: str) -> AutoConnection:
    return AutoConnection(
        net_id=nid,
        source_machine_id=src,
        source_face=face,
        target_machine_id=dst,
        target_face=OPPOSITE_FACE[face],
    )


def _at(mid: str, x: int, y: int, z: int, facing: Facing = Facing.NORTH) -> Placement:
    return Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=facing)


def _tiles(
    problem: InputIR,
    layout: LayoutResult,
    manifest: TextureManifest,
    item_ids: dict[str, int] | None = None,
) -> dict[str, nbt.Compound]:
    """``machine_id -> its tile entity`` in the export of ``layout``."""
    root = build_schematic(problem, layout, manifest=manifest, item_ids=item_ids)
    lo = build_scene(problem, layout)["bounds"]["min"]
    at = {
        (int(t["x"]) + int(lo[0]), int(t["y"]) + int(lo[1]), int(t["z"]) + int(lo[2])): t
        for t in root["TileEntities"]
    }
    return {
        p.machine_id: at[p.cell.as_tuple()] for p in layout.placements if p.cell.as_tuple() in at
    }


def _two_face_hammer(rate_b: float | None = None) -> tuple[InputIR, LayoutResult]:
    """A hammer at (1,0,1) facing north that auto-outputs one item east into a chest and pipes the
    other out of its south face into a second chest, one pipe block between them. ``rate_b`` is
    what leaves through the south face, which takes the cover."""
    hammer = _hammer(
        "h",
        [
            _port("output:a", IODirection.OUTPUT),
            _port("output:b", IODirection.OUTPUT, rate=rate_b),
        ],
    )
    chest_a = _storage("a", "Super Chest", [_port("in", IODirection.INPUT)])
    chest_b = _storage("b", "Super Chest", [_port("in", IODirection.INPUT)])
    nets = [
        _net("na", ("h", "output:a"), ("a", "in"), Commodity.ITEM),
        _net("nb", ("h", "output:b"), ("b", "in"), Commodity.ITEM),
    ]
    pipe_at = CellCoord(x=1, y=0, z=2)
    pipe = Route(
        net_id="nb",
        commodity=Commodity.ITEM,
        terminals=[
            Terminal(machine_id="h", port_id="output:b", face=Facing.SOUTH, cell=pipe_at),
            Terminal(machine_id="b", port_id="in", face=Facing.NORTH, cell=pipe_at),
        ],
        segments=[],
        material=RouteMaterial(family=PipeFamily.ITEM_PIPE, material="tin", size=PipeSize.NORMAL),
    )
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4), machines=[hammer, chest_a, chest_b], nets=nets
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[_at("h", 1, 0, 1), _at("a", 2, 0, 1), _at("b", 1, 0, 3)],
        routes=[pipe],
        auto_connections=[_auto("na", "h", Facing.EAST, "a")],
    )
    return problem, layout


# ----------------------------------------------------------------------------- basic machines


@pytest.fixture(scope="module")
def sand() -> tuple[InputIR, LayoutResult, nbt.Compound]:
    problem = adapt_file(str(_SAND))
    layout = solve(problem, optimize=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", schematic_core.SchematicWarning)
        root = build_schematic(problem, layout, manifest=_manifest())
    return problem, layout, root


def test_every_sand_hammer_works_on_its_front_and_outputs_through_its_auto_face(
    sand: tuple[InputIR, LayoutResult, nbt.Compound],
) -> None:
    problem, layout, _ = sand
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", schematic_core.SchematicWarning)
        tiles = _tiles(problem, layout, _manifest())
    outputs = output_faces(problem, layout)
    hammers = [
        p for p in layout.placements if tiles.get(p.machine_id, nbt.Compound()).get("mID") == 611
    ]
    assert len(hammers) == 3
    for placement in hammers:
        tile = tiles[placement.machine_id]
        auto = outputs[placement.machine_id].auto_face
        assert auto is not None
        assert tile["mMainFacing"] == nbt.Int(_FORGE[placement.orientation])
        assert isinstance(tile["mMainFacing"], nbt.Int)
        assert tile["mFacing"] == _FORGE[auto]
        assert isinstance(tile["mFacing"], nbt.Short)
        assert (tile["mItemTransfer"], tile["mFluidTransfer"]) == (1, 0)
        assert tile["mHasBeenUpdated"] == 1
        assert tile["mAllowInputFromOutputSide"] == 0
        assert (tile["mDisableFilter"], tile["mDisableMultiStack"]) == (1, 1)
        assert all(isinstance(tile[k], nbt.Byte) for k in ("mItemTransfer", "mHasBeenUpdated"))


def test_no_basic_machine_outputs_through_its_working_face_or_docks_there(
    sand: tuple[InputIR, LayoutResult, nbt.Compound],
) -> None:
    # 2.9's isFacingValid refuses mFacing == mMainFacing, and the working face carries no I/O.
    _, layout, root = sand
    basic = [t for t in root["TileEntities"] if "mMainFacing" in t]
    assert basic
    assert all(int(t["mFacing"]) != int(t["mMainFacing"]) for t in basic)
    front = {p.machine_id: p.orientation for p in layout.placements}
    for route in layout.routes:
        for terminal in route.terminals:
            if terminal.machine_id in front and route.commodity is not Commodity.POWER:
                assert terminal.face is not front[terminal.machine_id]


def test_a_basic_machine_with_nothing_leaving_it_outputs_away_from_its_front() -> None:
    cell = schematic_core.Cell(
        "gregtech:gt.blockmachines", 1, nbt.Compound({"mID": nbt.Int(611), "mFacing": nbt.Short(2)})
    )
    out = schematic_core._single_block_tile(
        cell,
        "gregtech.api.metatileentity.implementations.MTEBasicMachineWithRecipe",
        Facing.WEST,
        None,
    )
    assert out.tile is not None
    assert (out.tile["mMainFacing"], out.tile["mFacing"]) == (
        _FORGE[Facing.WEST],
        _FORGE[Facing.EAST],
    )
    assert (out.tile["mItemTransfer"], out.tile["mFluidTransfer"]) == (0, 0)


def test_the_basic_machine_2_9_0_beta_3_added_is_written_with_two_facings() -> None:
    # GT5-Unofficial 5.09.54.133 adds one MTEBasicMachine subclass, the Ice Cream Machine (mID
    # 20000). Left out of BASIC_MACHINE_CLASSES it would export facing its front with no output face
    # of its own, like any block of no known class.
    cell = schematic_core.Cell(
        "gregtech:gt.blockmachines",
        1,
        nbt.Compound({"mID": nbt.Int(20000), "mFacing": nbt.Short(2)}),
    )
    out = schematic_core._single_block_tile(
        cell, "gregtech.common.tileentities.machines.basic.MTEIceCreamMachine", Facing.WEST, None
    )
    assert out.tile is not None
    assert (out.tile["mMainFacing"], out.tile["mFacing"]) == (
        _FORGE[Facing.WEST],
        _FORGE[Facing.EAST],
    )


def test_a_block_of_no_known_class_keeps_the_facing_it_had() -> None:
    cell = schematic_core.Cell(
        "gregtech:gt.blockmachines", 3, nbt.Compound({"mFacing": nbt.Short(2)})
    )
    assert schematic_core._single_block_tile(cell, "some.Other", Facing.NORTH, None) is cell
    bare = schematic_core.Cell("minecraft:stone", 0)
    assert schematic_core._single_block_tile(bare, "x", Facing.NORTH, None) is bare


# ----------------------------------------------------------------------------- storages


def test_a_super_tank_faces_its_auto_face_and_auto_outputs_fluid() -> None:
    tank = _storage("t", "Super Tank", [_port("out", IODirection.OUTPUT, Commodity.FLUID)])
    hammer = _hammer("h", [_port("in", IODirection.INPUT, Commodity.FLUID)])
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[tank, hammer],
        nets=[_net("w", ("t", "out"), ("h", "in"), Commodity.FLUID)],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[_at("t", 1, 0, 1), _at("h", 2, 0, 1)],
        auto_connections=[_auto("w", "t", Facing.EAST, "h")],
    )
    tiles = _tiles(problem, layout, _manifest())
    assert tiles["t"]["mID"] == 130  # Super Tank I
    assert tiles["t"]["mFacing"] == _FORGE[Facing.EAST]
    assert tiles["t"]["mOutputFluid"] == 1
    assert "mMainFacing" not in tiles["t"]


def test_a_super_chest_keeps_its_front_and_its_output_takes_a_conveyor(
    sand: tuple[InputIR, LayoutResult, nbt.Compound],
) -> None:
    problem, layout, _ = sand
    with pytest.warns(schematic_core.SchematicWarning, match="conveyor x1: .* of Super Chest I at"):
        tiles = _tiles(problem, layout, _manifest())
    chests = [
        p for p in layout.placements if tiles.get(p.machine_id, nbt.Compound()).get("mID") == 135
    ]
    assert chests
    for placement in chests:
        assert tiles[placement.machine_id]["mFacing"] == _FORGE[placement.orientation]
        assert "mOutputFluid" not in tiles[placement.machine_id]


# ----------------------------------------------------------------------------- covers


def test_a_second_output_face_is_named_for_a_conveyor() -> None:
    problem, layout = _two_face_hammer()
    with pytest.warns(schematic_core.SchematicWarning) as caught:
        tiles = _tiles(problem, layout, _manifest())
    covers = [str(w.message) for w in caught if "need a cover" in str(w.message)]
    assert len(covers) == 1
    assert covers[0].startswith("1 output face(s) need a cover")
    assert "conveyor x1: south face of Basic Forge Hammer at" in covers[0]
    # The auto-connection's face is the output face; the piped one is the cover.
    assert tiles["h"]["mFacing"] == _FORGE[Facing.EAST]
    assert tiles["h"]["mMainFacing"] == _FORGE[Facing.NORTH]


def test_covers_are_grouped_by_cover_and_ordered_by_position() -> None:
    """By kind, then tier, then where they stand: the order a builder fetches and fits them."""
    lv_conveyor, hv_conveyor = cover_for("conveyor", None, "LV"), cover_for("conveyor", 1.0, "LV")
    covers = [
        ("B", (2, 0, 0), CoverFace(Facing.UP, "pump", ("n1",)), cover_for("pump", None, "LV")),
        ("A", (1, 0, 0), CoverFace(Facing.EAST, "conveyor", ("n2",)), lv_conveyor),
        ("A", (0, 0, 0), CoverFace(Facing.SOUTH, "conveyor", ("n3",)), lv_conveyor),
        ("C", (3, 0, 0), CoverFace(Facing.WEST, "conveyor", ("n4",)), hv_conveyor),
    ]
    with pytest.warns(schematic_core.SchematicWarning) as caught:
        schematic_core._warn_about_covers(covers, written=False)
    message = str(caught[0].message)
    assert message.endswith(
        "LV conveyor x2: south face of A at (0, 0, 0), east face of A at (1, 0, 0); "
        "HV conveyor x1: west face of C at (3, 0, 0); "
        "LV pump x1: up face of B at (2, 0, 0)"
    )


#: ``gregtech:gt.metaitem.01``'s id in the maintainer's 2.9 "New World", read from its level.dat.
_WORLD = {"gregtech:gt.metaitem.01": 7639}


def test_without_a_world_no_cover_is_written_and_the_warning_says_how() -> None:
    problem, layout = _two_face_hammer()
    with pytest.warns(schematic_core.SchematicWarning, match=r"only for a named world \(--world\)"):
        tiles = _tiles(problem, layout, _manifest())
    assert "gt.covers" not in tiles["h"]


def test_with_a_worlds_item_ids_the_cover_is_written_on_its_face() -> None:
    """GT's own shape: the side, the world's item id with the meta above it, export, default rate."""
    problem, layout = _two_face_hammer()
    with pytest.warns(schematic_core.SchematicWarning, match="The ghost shows each one"):
        tiles = _tiles(problem, layout, _manifest(), item_ids=_WORLD)
    (cover,) = tiles["h"]["gt.covers"]
    assert cover == {"s": _FORGE[Facing.SOUTH], "id": 7639 | 32630 << 16, "d": 0, "tra": 0}
    assert "gt.covers" not in tiles["a"], "a chest that only receives needs no cover"


def test_a_written_cover_matches_a_saved_one_tag_for_tag() -> None:
    """Against the HV conveyor on the Super Chest of the maintainer's 2.9 save, from the same world.

    Same tags, same tag types, and for the same world and cover the same encoded id: the save is
    what GT itself wrote, so this is the check that GT reads ours back as that conveyor.
    """
    save = read_schematic(_ROOT / "tests/golden/schematic/sand-parallel-29-gui.schematic")
    chest = save.tile_at(2, 0, 0)
    assert chest is not None
    (saved,) = chest.raw["gt.covers"]
    problem, layout = _two_face_hammer(rate_b=1.0)  # 0.64 < 1.0 <= 3.2 items/t: an HV conveyor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", schematic_core.SchematicWarning)
        tiles = _tiles(problem, layout, _manifest(), item_ids=_WORLD)
    (written,) = tiles["h"]["gt.covers"]
    assert {k: type(v) for k, v in written.items()} == {k: type(v) for k, v in saved.items()}
    assert tiles["h"]["gt.covers"].element_type == chest.raw["gt.covers"].element_type
    assert written["id"] == saved["id"]


@pytest.mark.parametrize(
    ("rate", "tier"),
    [(None, "LV"), (0.16, "LV"), (0.5, "MV"), (1.0, "HV"), (10.0, "EV"), (100.0, "IV")],
)
def test_the_cover_tier_keeps_up_with_what_leaves_the_face(rate: float | None, tier: str) -> None:
    problem, layout = _two_face_hammer(rate_b=rate)
    with pytest.warns(schematic_core.SchematicWarning, match=f"{tier} conveyor x1: south face"):
        _tiles(problem, layout, _manifest())


def test_a_world_with_no_gt_item_is_refused_rather_than_exported_without_covers() -> None:
    problem, layout = _two_face_hammer()
    with pytest.raises(SchematicError, match=r"no gregtech:gt\.metaitem\.01"):
        build_schematic(problem, layout, manifest=_manifest(), item_ids={"minecraft:stick": 280})


def test_no_covers_and_no_filters_warn_nothing() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", schematic_core.SchematicWarning)
        schematic_core._warn_about_covers([], written=False)
        schematic_core._warn_about_filters([])
        schematic_core._warn_about_output_side([])


# ------------------------------------------------- input through the output face (#278)


def _hammers_on_one_pipe(pack: str) -> tuple[InputIR, LayoutResult]:
    """Two hammers either side of one pipe block, both feeding it item x for the chest south of it."""
    hammers = [_hammer(mid, [_port("output:x", IODirection.OUTPUT)]) for mid in ("h1", "h2")]
    chest = _storage("c", "Super Chest", [_port("in", IODirection.INPUT)])
    net = Net(
        id="x",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="h1", port_id="output:x"),
            MachineFaceRef(machine_id="h2", port_id="output:x"),
            MachineFaceRef(machine_id="c", port_id="in"),
        ],
    )
    pipe_at = CellCoord(x=1, y=0, z=0)
    pipe = Route(
        net_id="x",
        commodity=Commodity.ITEM,
        terminals=[
            Terminal(machine_id="h1", port_id="output:x", face=Facing.EAST, cell=pipe_at),
            Terminal(machine_id="h2", port_id="output:x", face=Facing.WEST, cell=pipe_at),
            Terminal(machine_id="c", port_id="in", face=Facing.NORTH, cell=pipe_at),
        ],
        segments=[],
        material=RouteMaterial(family=PipeFamily.ITEM_PIPE, material="tin", size=PipeSize.NORMAL),
    )
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[*hammers, chest],
        nets=[net],
        pack_version=pack,
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="h1", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH),
            Placement(machine_id="h2", cell=CellCoord(x=2, y=0, z=0), orientation=Facing.NORTH),
            Placement(machine_id="c", cell=CellCoord(x=1, y=0, z=1), orientation=Facing.NORTH),
        ],
        routes=[pipe],
    )
    return problem, layout


def _output_side_warnings(pack: str) -> list[str]:
    problem, layout = _hammers_on_one_pipe(pack)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", schematic_core.SchematicWarning)
        build_schematic(problem, layout, manifest=_manifest())
    return [str(w.message) for w in caught if "share an output pipe" in str(w.message)]


def test_machines_sharing_an_output_pipe_are_named_with_the_setting_to_reach_on_29() -> None:
    """A ghost-built 2.9 basic machine takes input through its output face, so the export names each
    one on a shared pipe and the state to set it to (#278). The file itself writes the setting off,
    which a machine placed by hand from the ghost does not inherit."""
    (message,) = _output_side_warnings("2.9.0-beta-2")
    assert message.startswith("2 machine(s) share an output pipe")
    assert 'until chat says "Input from Output Side forbidden"' in message
    assert message.endswith(
        "Basic Forge Hammer at (0, 0, 0), east face; Basic Forge Hammer at (2, 0, 0), west face"
    )


def test_a_28_export_names_no_machine_for_the_setting() -> None:
    # 2.8.4 machines already refuse it, and the same click would allow it.
    assert _output_side_warnings("2.8.4") == []


# ----------------------------------------------------------------------------- item filters


def _filter_line() -> tuple[InputIR, LayoutResult]:
    """A hammer feeding an Item Filter from the filter's front; the filter faces north."""
    item_filter = Machine(
        id="f",
        type="Ultra Low Voltage Item Filter",
        block_key="gregtech:gt.blockmachines@9240",
        voltage_tier="ULV",
        orientation_options=[Facing.NORTH],
        filter_items=("gregtech:gt.metaitem.01@2034",),
        faces=FaceSpec(
            ports=[
                Port(
                    id="input:r",
                    commodity=Commodity.ITEM,
                    direction=IODirection.INPUT,
                    faces=(RelativeFace.FRONT, RelativeFace.LEFT, RelativeFace.RIGHT),
                ),
                Port(
                    id="output:r",
                    commodity=Commodity.ITEM,
                    direction=IODirection.OUTPUT,
                    faces=(RelativeFace.BACK,),
                ),
            ]
        ),
    )
    hammer = _hammer("h", [_port("out", IODirection.OUTPUT)])
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[hammer, item_filter],
        nets=[_net("n", ("h", "out"), ("f", "input:r"), Commodity.ITEM)],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[_at("h", 1, 0, 0, Facing.WEST), _at("f", 1, 0, 1)],
        auto_connections=[_auto("n", "h", Facing.SOUTH, "f")],
    )
    return problem, layout


def test_an_item_filter_exports_as_mid_9240_facing_its_front_with_its_items_named() -> None:
    problem, layout = _filter_line()
    with pytest.warns(schematic_core.SchematicWarning, match="Item Filter") as caught:
        root = build_schematic(problem, layout, manifest=_manifest(with_filter=True))
    tile = next(t for t in root["TileEntities"] if int(t["mID"]) == 9240)
    # MTEBuffer pushes out of the face opposite mFacing, so facing the solver's front makes it push
    # out of the solver's back, where the filter's output is pinned.
    assert tile["mFacing"] == _FORGE[Facing.NORTH]
    assert str(tile["id"]) == "BaseMetaTileEntity"
    x, y, z = int(tile["x"]), int(tile["y"]), int(tile["z"])
    width, length = int(root["Width"]), int(root["Length"])
    assert root["Data"][(y * length + z) * width + x] == 0  # te_base_type 0 for the ULV filter
    items = [str(w.message) for w in caught if "Item Filter(s)" in str(w.message)]
    assert items == [
        "1 Item Filter(s): the export writes no inventory, so set each filter's slots to the "
        "item it lets through. Ultra Low Voltage Item Filter at (0, 0, 1): "
        "gregtech:gt.metaitem.01@2034"
    ]


def test_an_item_filter_the_manifest_lacks_is_refused_naming_the_client_pass() -> None:
    problem, layout = _filter_line()
    with pytest.raises(
        SchematicError, match=r"Item Filter cannot be exported.*client texture pass"
    ):
        build_schematic(problem, layout, manifest=_manifest())


# ----------------------------------------------------------------------------- reading it back


def test_read_schematic_reports_a_basic_machines_main_facing(
    sand: tuple[InputIR, LayoutResult, nbt.Compound],
) -> None:
    _, _, root = sand
    schematic = read_schematic(nbt.dumps("Schematic", root))
    hammers = [t for t in schematic.tile_entities if t.mid == 611]
    assert hammers
    assert all(t.main_facing is not None and t.placed_facing == t.main_facing for t in hammers)
    assert all(t.facing != t.main_facing for t in hammers)
    chests = [t for t in schematic.tile_entities if t.mid == 135]
    assert all(t.main_facing is None and t.placed_facing == t.facing for t in chests)


def test_inspect_lists_each_basic_machines_working_and_output_face(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    problem, layout = _two_face_hammer()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", schematic_core.SchematicWarning)
        root = build_schematic(problem, layout, manifest=_manifest())
    path = tmp_path / "two-face.schematic"
    path.write_bytes(nbt.dumps("Schematic", root))
    assert cli.main(["--inspect-schematic", str(path)]) == 0
    out = capsys.readouterr().out
    assert "basic machines (working face -> output face, auto-output)" in out
    assert "mID 611" in out
    assert ": north -> east, items" in out
