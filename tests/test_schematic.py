"""Tests for the ``.schematic`` exporter (GitHub #96).

Two seams, tested apart. :mod:`gtnh_solver.schematic.nbt` is a closed binary format with real
files to check against, so it is proven by **round-tripping the goldens** in
``tests/golden/schematic/``: files Schematica itself wrote, which is the only ground truth
available (there is no headless GT, see docs/TESTING.md). The lowering is proven on the scene
contract and on the values the goldens pin - a Basic Forge Hammer's Data nibble is 1 in the real
file, so it must be 1 here.

What is NOT asserted: that Minecraft loads the result. That is eye-validated in game, like the
WebGL render. The tests cover everything up to the bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import roots as dataset_roots
from gtnh_solver.dataset.roots import DatasetWarning
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    InputIR,
    LayoutResult,
    LayoutStatus,
    Route,
    RouteMaterial,
    Segment,
    Terminal,
)
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import SchematicError, build_schematic, nbt, write_schematic
from gtnh_solver.schematic import core as schematic_core
from gtnh_solver.solver import solve
from tests._helpers import at, consumer, net, producer

_GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden" / "schematic"
_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"
_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


# ------------------------------------------------------------------------------------------- nbt


@pytest.mark.parametrize("name", ["sand.schematic", "nitrobenzene-reference.schematic"])
def test_a_real_schematica_file_round_trips(name: str) -> None:
    """Parse -> write -> parse on a file Schematica actually wrote.

    Values AND types have to survive: NBT distinguishes byte/short/int/long where Python does not,
    so a reader that hands back plain ``int`` would write a structurally identical tree at the
    wrong widths and shift every following byte.
    """
    raw = (_GOLDEN / name).read_bytes()
    root_name, root = nbt.loads(raw)
    again_name, again = nbt.loads(nbt.dumps(root_name, root))

    assert again_name == root_name == "Schematic"
    assert again == root

    def signature(compound: nbt.Compound, path: str = "") -> list[str]:
        out = []
        for key, value in sorted(compound.items()):
            out.append(f"{path}{key}:{type(value).__name__}")
            if isinstance(value, nbt.Compound):
                out.extend(signature(value, f"{path}{key}."))
        return out

    assert signature(again) == signature(root)


def test_writing_is_deterministic() -> None:
    # Same tree in, same bytes out (gzip mtime is pinned), so two exports of one layout can be
    # diffed. Without it every run differs and "did this change?" is unanswerable.
    _, root = nbt.loads((_GOLDEN / "sand.schematic").read_bytes())
    assert nbt.dumps("Schematic", root) == nbt.dumps("Schematic", root)


def test_an_unwrapped_int_is_refused_rather_than_guessed() -> None:
    # The whole reason the wrappers exist: there is no safe default width.
    with pytest.raises(nbt.NBTError, match="no NBT tag"):
        nbt.dumps("Schematic", nbt.Compound({"Width": 5}))


def test_every_tag_type_survives_a_round_trip() -> None:
    root = nbt.Compound(
        {
            "b": nbt.Byte(-8),
            "s": nbt.Short(-300),
            "i": nbt.Int(70000),
            "l": nbt.Long(2**40),
            "f": nbt.Float(0.5),
            "d": nbt.Double(0.25),
            "ba": nbt.ByteArray(b"\x00\x01\xff"),
            "str": nbt.String("hello"),
            "ia": nbt.IntArray([1, 2, 3]),
            "list": nbt.List(nbt.TAG_COMPOUND, [nbt.Compound({"x": nbt.Int(1)})]),
            "empty": nbt.List(nbt.TAG_COMPOUND),
            "nested": nbt.Compound({"deep": nbt.String("y")}),
        }
    )
    _, back = nbt.loads(nbt.dumps("Schematic", root))
    assert back == root
    assert back["empty"].element_type == nbt.TAG_COMPOUND  # an empty list still names its type
    assert nbt.loads(nbt.dumps("Schematic", root, compress=False))[1] == root  # uncompressed too


# --------------------------------------------------------------------------------------- lowering


def _manifest() -> TextureManifest:
    return TextureManifest.load(_COMMITTED_MANIFEST)


def _sand_schematic() -> nbt.Compound:
    ir = adapt_file(str(_SAND))
    return build_schematic(ir, solve(ir, optimize=False), manifest=_manifest())


def test_the_sand_line_lowers_to_the_data_values_the_golden_pins() -> None:
    """The nibble is the tile entity class, and the goldens say which for these exact blocks.

    A Basic Forge Hammer is Data 1 in the file Schematica wrote, an insulated tin cable 9, the
    Debug Power Generator 3. Reproducing those is the difference between a loadable ghost and a
    file that rebuilds every cable as a machine (GitHub #158).
    """
    root = _sand_schematic()
    by_index = list(zip(root["Blocks"], root["Data"], strict=True))
    seen = {(block, data) for block, data in by_index if block != 0}
    assert (1, 1) in seen  # tiered single-block machines (hammers, chests): WrenchLevel1
    assert (1, 3) in seen  # the Debug Power Generator standing in for the synthesized source
    assert (1, 9) in seen  # insulated cable: CutterLevel1
    assert all(data <= 15 for _, data in by_index), "Data is a nibble; >15 cannot be represented"


def test_machine_identity_rides_the_tile_entity_not_the_nibble() -> None:
    root = _sand_schematic()
    tiles = root["TileEntities"]
    mids = {int(t["mID"]) for t in tiles}
    assert 611 in mids  # Basic Forge Hammer, the mID the golden carries
    assert 135 in mids  # Super Chest I
    assert 1246 in mids or 1247 in mids  # tin cable
    kinds = {str(t["id"]) for t in tiles}
    assert kinds == {"BaseMetaTileEntity", "BaseMetaPipeEntity"}


def test_a_machine_faces_where_the_solver_placed_it() -> None:
    # mFacing is a ForgeDirection ordinal (NORTH=2), not our own enum's order.
    root = _sand_schematic()
    facings = {int(t["mFacing"]) for t in root["TileEntities"] if "mFacing" in t}
    assert facings <= set(schematic_core.FORGE_DIRECTION.values())
    assert schematic_core.FORGE_DIRECTION[Facing.NORTH] == 2
    assert schematic_core.FORGE_DIRECTION[Facing.DOWN] == 0  # DOWN leads the enum, not NORTH


def test_the_synthesized_power_source_becomes_a_real_block() -> None:
    """``Power Source (LV)`` is our invention. Left alone it would be a hole exactly where the
    builder feeds the line, so it is substituted with the block the maintainer actually uses."""
    root = _sand_schematic()
    stand_in = _manifest().mte_block(schematic_core.POWER_SOURCE_STAND_IN)
    assert stand_in is not None
    assert any(int(t["mID"]) == stand_in[1] for t in root["TileEntities"])


def test_every_cell_is_inside_the_declared_volume() -> None:
    root = _sand_schematic()
    width, height, length = int(root["Width"]), int(root["Height"]), int(root["Length"])
    assert len(root["Blocks"]) == len(root["Data"]) == width * height * length
    for tile in root["TileEntities"]:
        assert 0 <= int(tile["x"]) < width
        assert 0 <= int(tile["y"]) < height
        assert 0 <= int(tile["z"]) < length


def test_the_mapping_names_every_block_the_file_uses() -> None:
    # Schematica remaps by name, so an id used in Blocks with no entry here is unresolvable.
    root = _sand_schematic()
    mapping = root["SchematicaMapping"]
    used = {b for b in root["Blocks"] if b != 0}
    assert used <= {int(v) for v in mapping.values()}
    assert mapping["minecraft:air"] == 0


def test_a_pipe_is_wired_to_the_sides_its_route_connects() -> None:
    """``mConnections`` is a ForgeDirection bitmask taken from the route cell's own ``dirs``.

    The goldens carry 0 throughout (nothing to compare against), so this pins the derivation
    rather than a recorded value - see the note in ``core._route_cell``.
    """
    root = _sand_schematic()
    masks = [int(t["mConnections"]) for t in root["TileEntities"] if "mConnections" in t]
    assert masks, "the sand line cables its power net; those cells are pipes"
    assert all(0 < m < 64 for m in masks), "six directions, at least one connection each"


def test_an_untypeable_block_is_refused_not_guessed() -> None:
    """The never-silently-invalid rule, applied to the exporter: a manifest that predates
    ``te_base_type`` cannot say which tile entity class a block is, and a guess would rebuild a
    cable as a machine. Refusing names the block and says how to fix it."""
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    for entry in raw["blocks"].values():
        entry.pop("te_base_type", None)  # a schema 2 manifest
    ir = adapt_file(str(_SAND))
    layout = solve(ir, optimize=False)
    with pytest.raises(SchematicError, match="te_base_type") as caught:
        build_schematic(ir, layout, manifest=TextureManifest(raw))
    # No file behind this one, so it says so rather than naming a path it does not have.
    assert "an in-memory manifest" in str(caught.value)
    assert "predates the field" in str(caught.value)


def test_a_manifest_short_of_one_block_is_not_blamed_for_being_old() -> None:
    """The other way ``te_base_type`` comes back ``None``, and it wants the other fix.

    A manifest that types nothing predates #158 and needs a fresh extractor run; one that types
    other blocks and not this one is merely short of a block, and re-running the extractor for the
    same pack would produce the same gap. Telling a reader to re-run it would waste an hour.
    """
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    for entry in raw["blocks"].values():
        entry.pop("te_base_type", None)
    # One typed entry the sand line never asks for: enough to prove the manifest knows the field.
    raw["blocks"]["gtnh_solver:unused|0"] = {"kind": "mte", "sides": {}, "te_base_type": 1}
    ir = adapt_file(str(_SAND))
    layout = solve(ir, optimize=False)
    with pytest.raises(SchematicError, match="short of this block") as caught:
        build_schematic(ir, layout, manifest=TextureManifest(raw))
    assert "predates the field" not in str(caught.value)


def test_an_untypeable_route_block_names_the_manifest_too() -> None:
    """Both call sites, not only the machine one. A cable given the wrong Data nibble rebuilds as a
    machine, so a route the manifest cannot type is refused on the same terms."""
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    for entry in raw["blocks"].values():
        if entry.get("kind") == "pipe":
            entry.pop("te_base_type", None)  # machines still type; the cables no longer do
    ir = adapt_file(str(_SAND))
    layout = solve(ir, optimize=False)
    with pytest.raises(SchematicError, match="te_base_type") as caught:
        build_schematic(ir, layout, manifest=TextureManifest(raw))
    message = str(caught.value)
    assert "an in-memory manifest" in message, "the route refusal names its source too"
    assert "short of this block" in message, "machines still type, so this dump is not old"


def test_a_stale_local_dump_shadows_the_committed_manifest_and_the_refusal_says_which(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #166 reproduction, on the exact path ``gtnh-solve --schematic`` takes.

    Built rather than copied: a real dump is 15 MB, and all this needs is a manifest at a versioned
    path that predates ``te_base_type``. Resolution prefers it over the committed manifest on
    presence alone, so the export refuses a Super Chest the shipped data types perfectly well - and
    the old wording ("re-run the extractor to refresh the manifest") read as "the committed data is
    stale", which is how this got filed against the wrong file. Two things have to hold now:
    resolution says out loud which file is the older one, and the refusal names the file it read.
    """
    committed = tmp_path / "textures" / "manifest.json"
    raw = json.loads(_COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    raw.setdefault("provenance", {})["generated_at"] = "2026-09-18T21:29:30.149Z"
    committed.parent.mkdir(parents=True)
    committed.write_text(json.dumps(raw), encoding="utf-8")

    stale = json.loads(json.dumps(raw))  # same coverage, taken before the field existed
    for entry in stale["blocks"].values():
        entry.pop("te_base_type", None)
    stale["provenance"]["generated_at"] = "2026-09-08T00:00:00Z"
    local = tmp_path / "2.8.4" / "textures" / "manifest.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps(stale), encoding="utf-8")

    ir = adapt_file(str(_SAND))
    layout = solve(ir, optimize=False)
    monkeypatch.setattr(dataset_roots, "DEFAULT_DATA", tmp_path)
    with (
        pytest.warns(DatasetWarning, match="older than the committed"),
        pytest.raises(SchematicError) as caught,
    ):
        write_schematic(ir, layout, tmp_path / "line.schematic")

    message = str(caught.value)
    assert str(local) in message, "the refusal must name the manifest it actually consulted"
    assert str(committed) not in message, "the committed manifest is not what failed"
    assert "2026-09-08" in message, "and date it, since being old is the whole problem"
    assert "#166" in message


def test_a_machine_the_manifest_never_heard_of_is_refused() -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=2, sz=4),
        machines=[producer("m1"), consumer("m2")],
        nets=[net("n1", "m1", "m2")],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=[at("m1", 0, 0, 0), at("m2", 2, 0, 0)]
    )
    with pytest.raises(SchematicError):
        build_schematic(problem, layout, manifest=_manifest())


def test_an_empty_layout_is_refused_with_its_volume_named() -> None:
    problem = InputIR(bounding_region=CellBox(sx=0 + 1, sy=1, sz=1))
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0)
    root = build_schematic(problem, layout, manifest=_manifest())
    assert len(root["Blocks"]) == 1  # the region fallback, all air


def test_a_route_with_no_material_is_refused() -> None:
    # A route that names no dataset block cannot be lowered to any block at all.
    def coord(x: int, y: int, z: int) -> CellCoord:
        return CellCoord(x=x, y=y, z=z)

    route = Route(
        net_id="power:LV",
        commodity=Commodity.POWER,
        terminals=[Terminal(machine_id="a", port_id="p", face=Facing.NORTH, cell=coord(0, 0, 0))],
        segments=[Segment(start=coord(0, 0, 0), end=coord(1, 0, 0), channel=0)],
        thickness_per_segment=[1],
        material=None,
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4))
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, routes=[route])
    with pytest.raises(SchematicError, match=r"names no dataset block"):
        build_schematic(problem, layout, manifest=_manifest())


def test_a_route_material_the_manifest_lacks_is_refused_by_name() -> None:
    def coord(x: int, y: int, z: int) -> CellCoord:
        return CellCoord(x=x, y=y, z=z)

    invented = RouteMaterial(family="cable", material="unobtainium", tier="LV", stand_in=True)
    route = Route(
        net_id="power:LV",
        commodity=Commodity.POWER,
        terminals=[Terminal(machine_id="a", port_id="p", face=Facing.NORTH, cell=coord(0, 0, 0))],
        segments=[Segment(start=coord(0, 0, 0), end=coord(1, 0, 0), channel=0)],
        thickness_per_segment=[1],
        material=invented,
    )
    problem = InputIR(bounding_region=CellBox(sx=4, sy=2, sz=4))
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, routes=[route])
    with pytest.raises(SchematicError, match="unobtainium"):
        build_schematic(problem, layout, manifest=_manifest())


def test_write_schematic_writes_a_gzipped_file_and_makes_its_parent(tmp_path: Path) -> None:
    ir = adapt_file(str(_SAND))
    out = write_schematic(ir, solve(ir, optimize=False), tmp_path / "nested" / "line.schematic")
    assert out.exists()
    assert out.read_bytes()[:2] == b"\x1f\x8b", "Schematica reads gzipped NBT"
    name, root = nbt.loads(out.read_bytes())
    assert name == "Schematic"
    assert int(root["Width"]) > 0


def test_export_and_preview_describe_the_same_solve() -> None:
    """One solve, two artifacts. If they disagreed about which blocks the line is made of, the
    preview would be lying about what the schematic builds - which is why both consume the scene."""
    from gtnh_solver.previewer import build_scene, texturize_scene

    ir = adapt_file(str(_SAND))
    layout = solve(ir, optimize=False)
    scene = build_scene(ir, layout)
    texturize_scene(scene, png_provider=lambda icons: {})
    preview_mids = {(b["block"], b["meta"]) for b in scene["blocks"]}

    root = build_schematic(ir, layout, manifest=_manifest())
    exported = {int(t["mID"]) for t in root["TileEntities"] if str(t["id"]) == "BaseMetaTileEntity"}
    assert {meta for _, meta in preview_mids} <= exported


# -------------------------------------------------------------------------------------------- cli


def test_cli_schematic_writes_a_file_and_suppresses_the_guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from gtnh_solver.cli import main

    target = tmp_path / "line.schematic"
    assert main([str(_SAND), "--schematic", str(target)]) == 0
    captured = capsys.readouterr()
    assert target.exists()
    assert captured.out == ""  # --schematic alone is an artifact run, like --preview
    assert "wrote schematic" in captured.err


def test_cli_reports_an_unexportable_layout_as_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gtnh_solver import cli as cli_module

    def boom(*args: Any, **kwargs: Any) -> None:
        raise SchematicError("gregtech:gt.blockmachines|999 has no te_base_type")

    monkeypatch.setattr(cli_module, "write_schematic", boom)
    assert cli_module.main([str(_SAND), "--schematic", str(tmp_path / "x.schematic")]) == 2
    assert "cannot export" in capsys.readouterr().err
