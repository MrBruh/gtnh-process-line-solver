"""The icon index reader (``dataset.icons``, #297): how a plan's id finds its entry, what an image
must be before it is embedded, and which pack's index a preview draws from.

Every index here is hand-made in ``tmp_path`` (``tests._helpers.write_icon_index``): no export is
committed, and the reader's rules are about the index's shape, not about one real export.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import pytest

from gtnh_solver.dataset.icons import (
    ICON_INDEX,
    MAX_PNG_BYTES,
    IconEntry,
    IconPack,
    IconPackError,
    resolve_icon_index,
)
from gtnh_solver.ir import Commodity
from tests._helpers import solid_png, write_icon_index

_GRAVEL = solid_png((130, 120, 110))
_TOLUENE = solid_png((112, 36, 0))


def _pack(tmp_path: Path) -> IconPack:
    index = write_icon_index(
        tmp_path / "icons",
        items={
            "minecraft:gravel": {"name": "Gravel", "png": "item/minecraft/gravel~0.png"},
            "minecraft:log": {"name": "Oak Wood", "png": None},
            "minecraft:log@1": {"name": "Spruce Wood", "png": None},
            "minecraft:log@3": {"name": "Jungle Wood", "png": None},
            "gregtech:gt.metaitem.01@2032": {"name": "Iron Dust", "png": None},
            "minecraft:wool@32767": {"name": "Any Wool", "png": None},
            "minecraft:wool": {"name": "White Wool", "png": None},
            "minecraft:planks@2": {"name": "Birch Planks", "png": None},
            "minecraft:planks@5": {"name": "Dark Oak Planks", "png": None},
        },
        fluids={"liquid_toluene": {"name": "Toluene", "png": "fluid/gregtech/toluene.png"}},
        images={"item/minecraft/gravel~0.png": _GRAVEL, "fluid/gregtech/toluene.png": _TOLUENE},
    )
    return IconPack.load(index)


def _name(pack: IconPack, resource: str) -> str | None:
    entry = pack.lookup(resource, Commodity.ITEM)
    return entry.name if entry is not None else None


def test_an_item_and_a_fluid_are_found_by_the_plans_id(tmp_path: Path) -> None:
    with _pack(tmp_path) as pack:
        gravel = pack.lookup("minecraft:gravel", Commodity.ITEM)
        assert gravel == IconEntry(name="Gravel", png="item/minecraft/gravel~0.png")
        assert pack.png(gravel) == _GRAVEL
        toluene = pack.lookup("liquid_toluene", Commodity.FLUID)
        assert toluene is not None
        assert pack.png(toluene) == _TOLUENE


def test_the_kind_picks_the_table(tmp_path: Path) -> None:
    # An item and a fluid are separate namespaces; power has no icon at all.
    with _pack(tmp_path) as pack:
        assert pack.lookup("liquid_toluene", Commodity.ITEM) is None
        assert pack.lookup("minecraft:gravel", Commodity.FLUID) is None
        assert pack.lookup("minecraft:gravel", Commodity.POWER) is None


def test_a_lookup_ignores_case(tmp_path: Path) -> None:
    # The index keys are lowercased when it is derived, so a plan's mixed-case mod id still joins.
    with _pack(tmp_path) as pack:
        assert pack.lookup("GregTech:gt.metaitem.01@2032", Commodity.ITEM) is not None


def test_an_exact_damage_is_never_widened(tmp_path: Path) -> None:
    with _pack(tmp_path) as pack:
        assert _name(pack, "minecraft:log@1") == "Spruce Wood"
        assert pack.lookup("minecraft:log@2", Commodity.ITEM) is None
        assert pack.lookup("minecraft:stone", Commodity.ITEM) is None


def test_a_wildcard_takes_its_own_row_first(tmp_path: Path) -> None:
    with _pack(tmp_path) as pack:
        assert _name(pack, "minecraft:wool@32767") == "Any Wool"


def test_a_wildcard_with_no_row_of_its_own_takes_the_lowest_damage(tmp_path: Path) -> None:
    # "Any log" draws as the log NEI lists first; damage 0 is written with no suffix at all.
    with _pack(tmp_path) as pack:
        assert _name(pack, "minecraft:log@32767") == "Oak Wood"
        assert _name(pack, "minecraft:planks@32767") == "Birch Planks"
        assert pack.lookup("minecraft:glass@32767", Commodity.ITEM) is None


def test_an_entry_with_no_image_has_a_name_and_no_png(tmp_path: Path) -> None:
    with _pack(tmp_path) as pack:
        entry = pack.lookup("minecraft:log@3", Commodity.ITEM)
        assert entry is not None
        assert pack.png(entry) is None


@pytest.mark.parametrize(
    ("member", "data", "message"),
    [
        ("missing.png", None, "has no 'missing.png'"),
        ("fake.png", b"GIF89a not a png", "is not a PNG"),
        ("huge.png", solid_png((0, 0, 0)) + b"\0" * MAX_PNG_BYTES, "over the"),
    ],
    ids=["missing", "not-a-png", "too-large"],  # bytes make unstable ids under xdist
)
def test_an_image_that_is_not_a_small_png_is_refused(
    tmp_path: Path, member: str, data: bytes | None, message: str
) -> None:
    images = {member: data} if data is not None else {}
    index = write_icon_index(
        tmp_path / "icons", items={"x:y": {"name": "Y", "png": member}}, images=images
    )
    with IconPack.load(index) as pack:
        entry = pack.lookup("x:y", Commodity.ITEM)
        assert entry is not None
        with pytest.raises(IconPackError, match=message):
            pack.png(entry)


def test_an_images_archive_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    index = write_icon_index(tmp_path / "icons", items={"x:y": {"name": "Y", "png": "y.png"}})
    (tmp_path / "icons" / "images.zip").write_bytes(b"not a zip")
    with IconPack.load(index) as pack:
        entry = pack.lookup("x:y", Commodity.ITEM)
        assert entry is not None
        with pytest.raises(IconPackError, match="not a zip archive"):
            pack.png(entry)


@pytest.mark.parametrize(
    "raw", [["Y", "y.png"], {"png": "y.png"}, {"name": 3, "png": None}, {"name": "Y", "png": 7}]
)
def test_an_entry_that_is_not_a_name_and_a_path_is_refused(tmp_path: Path, raw: object) -> None:
    index = write_icon_index(tmp_path / "icons", items={"x:y": raw})
    with IconPack.load(index) as pack, pytest.raises(IconPackError, match="'x:y'"):
        pack.lookup("x:y", Commodity.ITEM)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "is not JSON"),
        ("[1, 2]", "not a JSON object"),
        ('{"schema": 2, "items": {}, "fluids": {}}', "schema 2"),
        ('{"schema": 1, "fluids": {}}', "no items map"),
        ('{"schema": 1, "items": {}, "fluids": []}', "no fluids map"),
        ('{"schema": 1, "source": [], "items": {}, "fluids": {}}', "no source map"),
    ],
)
def test_a_file_that_is_not_an_icon_index_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    index = tmp_path / "index.json"
    index.write_text(text, encoding="utf-8")
    with pytest.raises(IconPackError, match=message):
        IconPack.load(index)


def test_closing_a_pack_closes_its_archive_and_is_safe_twice(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    entry = pack.lookup("minecraft:gravel", Commodity.ITEM)
    assert entry is not None
    pack.png(entry)
    archive = pack._zip
    assert isinstance(archive, zipfile.ZipFile)
    pack.close()
    pack.close()
    assert archive.fp is None  # the file handle is released


def test_a_packs_own_index_wins(tmp_path: Path) -> None:
    write_icon_index(tmp_path / "2.9.0-beta-2" / "icons")
    write_icon_index(tmp_path / "2.8.4" / "icons")
    found = resolve_icon_index("2.8.4", data_dir=tmp_path)
    assert found == tmp_path / "2.8.4" / ICON_INDEX


def test_a_pack_with_no_index_borrows_the_newest_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Icons are display only, so a 2.8.4 plan may draw 2.9's pictures; block ids never cross packs.
    write_icon_index(tmp_path / "2.9.0-beta-2" / "icons")
    (tmp_path / "2.8.4" / "multiblocks").mkdir(parents=True)
    with caplog.at_level(logging.INFO, logger="gtnh_solver.dataset.icons"):
        found = resolve_icon_index("2.8.4", data_dir=tmp_path)
    assert found == tmp_path / "2.9.0-beta-2" / ICON_INDEX
    assert "pack 2.8.4 has no icon index" in caplog.text


def test_an_unnamed_pack_takes_the_newest_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_icon_index(tmp_path / "2.9.0-beta-2" / "icons")
    with caplog.at_level(logging.INFO, logger="gtnh_solver.dataset.icons"):
        found = resolve_icon_index(None, data_dir=tmp_path)
    assert found == tmp_path / "2.9.0-beta-2" / ICON_INDEX
    assert caplog.text == ""


def test_no_index_anywhere_is_none(tmp_path: Path) -> None:
    (tmp_path / "2.8.4" / "textures").mkdir(parents=True)
    assert resolve_icon_index("2.8.4", data_dir=tmp_path) is None
    assert resolve_icon_index(None, data_dir=tmp_path) is None
