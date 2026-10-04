"""Tests for reading a world's numeric item ids (:mod:`gtnh_solver.schematic.world`).

The ids are per world, so the export can only name a cover correctly from the target world's own
``level.dat``; these pin that the table is read the way FML writes it, items only, and that every
way of not having one is a refusal that names the file rather than a silent empty table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.schematic import SchematicError, item_ids, nbt


def _level(path: Path, root: nbt.Compound) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "level.dat").write_bytes(nbt.dumps("", root))
    return path


def _fml(entries: list[tuple[str, int]]) -> nbt.Compound:
    table = nbt.List(
        nbt.TAG_COMPOUND,
        [nbt.Compound({"K": nbt.String(k), "V": nbt.Int(v)}) for k, v in entries],
    )
    return nbt.Compound({"FML": nbt.Compound({"ItemData": table})})


def test_the_items_are_read_and_the_blocks_left_out(tmp_path: Path) -> None:
    """FML keeps blocks and items in one list, ``\\x01`` and ``\\x02`` before the name."""
    world = _level(
        tmp_path / "w",
        _fml([("\x01minecraft:stone", 1), ("\x02gregtech:gt.metaitem.01", 7639)]),
    )
    assert item_ids(world) == {"gregtech:gt.metaitem.01": 7639}


def test_the_level_dat_itself_can_be_given(tmp_path: Path) -> None:
    world = _level(tmp_path / "w", _fml([("\x02gregtech:gt.metaitem.01", 7436)]))
    assert item_ids(world / "level.dat") == {"gregtech:gt.metaitem.01": 7436}


def test_a_folder_with_no_level_dat_is_refused_by_path(tmp_path: Path) -> None:
    with pytest.raises(SchematicError, match=r"no level.dat at"):
        item_ids(tmp_path)


def test_a_world_never_opened_with_forge_has_no_table_and_is_refused(tmp_path: Path) -> None:
    world = _level(tmp_path / "w", nbt.Compound({"Data": nbt.Compound()}))
    with pytest.raises(SchematicError, match="no FML item table"):
        item_ids(world)


def test_a_file_that_is_not_nbt_is_refused(tmp_path: Path) -> None:
    (tmp_path / "level.dat").write_bytes(b"not a level.dat")
    with pytest.raises(SchematicError, match=r"not a readable level.dat"):
        item_ids(tmp_path)
