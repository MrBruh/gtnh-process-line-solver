"""Tests for reading a ``.schematic`` back (:mod:`gtnh_solver.schematic.read`).

The reader is proven two ways, because each catches what the other cannot. **Against the goldens**
(files Schematica itself wrote) it has to agree with a format nobody here controls, which is what
pins the packing conventions. **Against our own exporter**, round-tripped, it has to agree with
:mod:`gtnh_solver.schematic.core` cell for cell, which is what keeps the two from drifting apart
later: the reader is that module's inverse, and an inverse that is never composed with its original
is just a second guess at the format.

The two files differ in a way that matters here: the hand-built reference needs ``AddBlocks``
(``gt.blockmachines`` is id 2417 in it), while our exporter numbers ids compactly from 1 and omits
the array entirely. Both paths are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import roots
from gtnh_solver.previewer.textures import TextureManifest
from gtnh_solver.schematic import (
    SchematicError,
    build_schematic,
    nbt,
    read_schematic,
    write_schematic,
)
from gtnh_solver.solver import solve

_GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden" / "schematic"
_COMMITTED_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "textures" / "manifest.json"
_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"
_GT_BLOCK = "gregtech:gt.blockmachines"


# ----------------------------------------------------------------------------------- the goldens


def test_the_reference_build_decodes_with_nothing_unmapped() -> None:
    """Every cell of the hand-built reference names a real block.

    The strongest whole-file check available: ``SchematicaMapping`` is the file's own account of
    which ids it uses, so a single cell decoding to an id outside it means the block array was
    unpacked wrong. 1386 cells, 12 distinct blocks, no ``<unmapped:N>``.
    """
    schematic = read_schematic(_GOLDEN / "nitrobenzene-reference.schematic")

    assert schematic.size == (18, 7, 11)
    assert schematic.volume == 1386
    assert schematic.materials == "Alpha"
    assert not [name for name in schematic.histogram(air=True) if name.startswith("<unmapped:")]
    assert schematic.histogram()[_GT_BLOCK] == 105
    assert schematic.histogram()["gregtech:gt.blockcasings4"] == 100


def test_add_blocks_takes_the_high_nibble_on_an_even_cell() -> None:
    """The packing order, pinned by the one consequence that cannot be coincidence.

    ``gt.blockmachines`` is id 2417 in this file, so every one of its 105 cells needs a nonzero
    ``AddBlocks`` nibble; ids under 256 do not. Reading the nibble from the wrong half of the byte
    still yields a number, which is why this is measured rather than recalled: the wrong order
    leaves 224 cells unmapped here and strands 47 tile entities in air, while the right one leaves
    none of either. Cell (8, 0, 0) is the sample, and it holds a machine in the real build.
    """
    schematic = read_schematic(_GOLDEN / "nitrobenzene-reference.schematic")

    assert schematic.names[2417] == _GT_BLOCK  # over a byte, so it exists only via AddBlocks
    assert schematic.block_at(8, 0, 0)[0] == _GT_BLOCK
    # Every solid cell in this file needs the array: the lowest id it uses at all is 583.
    assert sum(1 for i in schematic.block_ids if i > 0xFF) == sum(schematic.histogram().values())


def test_every_tile_entity_stands_on_a_block_that_can_host_one() -> None:
    """Cell order, pinned by the invariant that survives it being wrong.

    ``TileEntities`` carries its own x/y/z, and the grid is indexed ``(y * Length + z) * Width + x``.
    Transpose those and the entries land on other cells, mostly air: this file is 74% air, so a
    wrong order scatters most of the 116 tile entities into it.
    """
    schematic = read_schematic(_GOLDEN / "nitrobenzene-reference.schematic")

    assert len(schematic.tile_entities) == 116
    assert all(
        schematic.block_at(*tile.pos)[0] != "minecraft:air" for tile in schematic.tile_entities
    )
    assert all(
        schematic.block_at(*tile.pos)[0] == _GT_BLOCK
        for tile in schematic.tile_entities
        if tile.mid is not None
    )


def test_a_non_gt_tile_entity_reads_as_one_rather_than_failing() -> None:
    """An EnderIO reservoir has no ``mID``, which is a fact about the block, not a parse error."""
    schematic = read_schematic(_GOLDEN / "nitrobenzene-reference.schematic")

    reservoirs = [t for t in schematic.tile_entities if t.id == "blockReservoirTileEntity"]
    assert len(reservoirs) == 4
    assert all(t.mid is None for t in reservoirs)
    # raw keeps what this dataclass does not model, nested compounds included
    assert all(t.raw["tank"]["FluidName"] == "water" for t in reservoirs)


def test_the_sand_golden_reads_its_machines_and_facings() -> None:
    """The small golden, whose every cell is a GT machine or cable."""
    schematic = read_schematic(_GOLDEN / "sand.schematic")

    assert schematic.size == (5, 2, 1)
    assert schematic.histogram() == {_GT_BLOCK: 9}
    assert {t.mid for t in schematic.tile_entities} == {135, 611, 1246, 1247, 15498}
    chest = schematic.tile_at(0, 0, 0)
    assert chest is not None
    assert chest.mid == 135
    assert chest.facing == 3
    assert schematic.block_at(0, 0, 0) == (_GT_BLOCK, 1)  # Data is the TE class, not the machine


# ------------------------------------------------------------------------- round trip vs. core


def test_our_own_export_reads_back_cell_for_cell() -> None:
    """Compose the reader with the writer: every cell and tile entity survives the trip.

    This is the check that keeps the two halves honest as the exporter changes. It also covers the
    no-``AddBlocks`` path, since our ids are allocated compactly from 1 and never reach 256.
    """
    ir = adapt_file(str(_SAND))
    manifest = TextureManifest.load(_COMMITTED_MANIFEST)
    root = build_schematic(ir, solve(ir, optimize=False), manifest=manifest)
    written = nbt.dumps("Schematic", root)

    schematic = read_schematic(written)

    assert "AddBlocks" not in schematic.root
    assert schematic.size == (int(root["Width"]), int(root["Height"]), int(root["Length"]))
    ids = {int(v): str(k) for k, v in root["SchematicaMapping"].items()}
    for index, block_id in enumerate(schematic.block_ids):
        assert block_id == root["Blocks"][index]
        assert schematic.name_of(block_id) == ids[block_id]
    assert [t.mid for t in schematic.tile_entities] == [int(t["mID"]) for t in root["TileEntities"]]


def test_write_schematic_then_read_schematic_agree_on_disk(tmp_path: Path) -> None:
    """The file the CLI writes is the file the reader reads: gzip wrapper included."""
    ir = adapt_file(str(_SAND))
    path = write_schematic(ir, solve(ir, optimize=False), tmp_path / "line.schematic")

    schematic = read_schematic(path)

    assert schematic.materials == "Alpha"
    assert schematic.tile_entities
    assert read_schematic(path.read_bytes()).block_ids == schematic.block_ids  # path or bytes


# ------------------------------------------------------------------------------ refusing to guess


def test_an_id_without_a_name_is_flagged_rather_than_invented() -> None:
    """A plain MCEdit file has no ``SchematicaMapping``, so its ids are nameless.

    Naming them anyway would be the failure this project treats as unrecoverable elsewhere: a
    confident wrong answer nothing downstream can detect. An explicit marker is recoverable.
    """
    root = nbt.Compound(
        {
            "Width": nbt.Short(1),
            "Height": nbt.Short(1),
            "Length": nbt.Short(2),
            "Materials": nbt.String("Alpha"),
            "Blocks": nbt.ByteArray(bytes([7, 0])),
            "Data": nbt.ByteArray(bytes([3, 0])),
        }
    )

    schematic = read_schematic(nbt.dumps("Schematic", root))

    assert schematic.block_at(0, 0, 0) == ("<unmapped:7>", 3)
    assert schematic.block_at(0, 0, 1) == ("minecraft:air", 0)  # id 0 is air in every dialect
    assert schematic.histogram() == {"<unmapped:7>": 1}


@pytest.mark.parametrize(
    ("tag", "message"),
    [("Width", "Width"), ("Blocks", "Blocks"), ("Data", "Data")],
)
def test_a_file_missing_a_structural_tag_is_refused(tag: str, message: str) -> None:
    root = nbt.Compound(
        {
            "Width": nbt.Short(1),
            "Height": nbt.Short(1),
            "Length": nbt.Short(1),
            "Blocks": nbt.ByteArray(b"\x01"),
            "Data": nbt.ByteArray(b"\x00"),
        }
    )
    del root[tag]

    with pytest.raises(SchematicError, match=message):
        read_schematic(nbt.dumps("Schematic", root))


def test_a_truncated_grid_is_refused_rather_than_padded_with_air() -> None:
    """Decoding the part that fits would read a truncated file as a build with holes in it."""
    root = nbt.Compound(
        {
            "Width": nbt.Short(4),
            "Height": nbt.Short(1),
            "Length": nbt.Short(1),
            "Blocks": nbt.ByteArray(bytes([1, 1])),  # 2 bytes for a 4-cell volume
            "Data": nbt.ByteArray(bytes([0, 0, 0, 0])),
        }
    )

    with pytest.raises(SchematicError, match="Blocks holds 2 bytes but 4x1x1 needs 4"):
        read_schematic(nbt.dumps("Schematic", root))


def test_bytes_that_are_not_nbt_are_refused_with_a_readable_reason() -> None:
    with pytest.raises(SchematicError, match="not a readable"):
        read_schematic(b"this is not a schematic")


def test_a_cell_outside_the_volume_is_an_error_not_a_wrap() -> None:
    schematic = read_schematic(_GOLDEN / "sand.schematic")

    with pytest.raises(SchematicError, match=r"outside a \(5, 2, 1\) schematic"):
        schematic.block_at(5, 0, 0)


# -------------------------------------------------------------------------------------------- cli


def test_cli_inspect_prints_the_blocks_and_machines(capsys: pytest.CaptureFixture[str]) -> None:
    from gtnh_solver.cli import main

    assert main(["--inspect-schematic", str(_GOLDEN / "sand.schematic")]) == 0
    out = capsys.readouterr().out
    assert "5x2x1 = 10 cells, 9 solid" in out
    assert f"      9  {_GT_BLOCK}" in out
    assert "Basic Forge Hammer" in out  # the mID resolved through the committed manifest
    assert "Debug Power Generator" in out


def test_cli_inspect_says_which_manifest_could_not_name_an_mid(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved mID names the manifest that was asked, because the committed one is scoped.

    Against it most of a real build's machines resolve to nothing, and a bare "not found" reads
    like a corrupt file when it only means the small manifest was asked.

    Hiding any locally staged dump is what makes that the manifest under test. The CLI resolves
    the newest ``data/<version>/`` by default and a full dump names every mID in this golden, so
    wherever one is staged this used to assert the opposite of what it says, and failed.
    """
    from gtnh_solver.cli import main

    monkeypatch.setattr(roots, "list_versions", lambda *args, **kwargs: [])
    assert main(["--inspect-schematic", str(_GOLDEN / "nitrobenzene-reference.schematic")]) == 0
    captured = capsys.readouterr()
    assert "ExxonMobil Chemical Plant" in captured.out  # in the committed manifest
    assert "NOT IN THIS MANIFEST" in captured.out  # 5151, the titanium pipe, is not
    assert "did not resolve" in captured.err
    assert "example-scoped" in captured.err


def test_cli_inspect_reports_an_unreadable_file_as_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from gtnh_solver.cli import main

    broken = tmp_path / "broken.schematic"
    broken.write_bytes(b"not nbt at all")

    assert main(["--inspect-schematic", str(broken)]) == 2
    assert "could not read" in capsys.readouterr().err
