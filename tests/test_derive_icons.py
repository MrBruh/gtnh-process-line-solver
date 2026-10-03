"""``tools/derive_icons.py`` (#297): a NESQL export folder in, ``data/<version>/icons/`` out.

The export is the hand-written dialect fixture (``tests/fixtures/nesql/``) beside an ``image.zip``
built here, and the result is read back through the previewer's own reader, ``dataset.icons``: the
tool is right when what it writes is what that reader finds, not when it matches a stored copy. The
fixture has 17 items, so :data:`MIN_ITEMS` is lowered for the runs that should succeed; the
refusal that floor exists for runs at the real one.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gtnh_solver.dataset.icons import IconEntry, IconPack
from gtnh_solver.ir import Commodity
from tests._helpers import solid_png

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "tools" / "derive_icons.py"
_SCRIPT = _ROOT / "tests" / "fixtures" / "nesql" / "nesql-db.script"
_RUNBOOK = _ROOT / "docs" / "dataset-extraction" / "icons.md"
_LOCK: dict[str, Any] = json.loads((_ROOT / "gtnh.lock.json").read_text(encoding="utf-8"))
_PACK: str = _LOCK["pack_version"]

#: The pictures the export rendered: a few of the fixture's items and one of its fluids.
_IMAGES = {
    "item/minecraft/gravel~0.png": solid_png((130, 120, 110)),
    "item/minecraft/log~0.png": solid_png((102, 81, 49)),
    "item/minecraft/wool~32767.png": solid_png((233, 236, 236)),
    "item/IC2/itemCellEmpty~0.png": solid_png((190, 190, 200)),
    "fluid/gregtech/liquid_toluene.png": solid_png((112, 36, 0)),
}


def _tool() -> ModuleType:
    """The tool imported by path: ``tools/`` is a scripts folder, deliberately not a package."""
    spec = importlib.util.spec_from_file_location("derive_icons", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The tool with its item floor at the fixture's 17 items."""
    module = _tool()
    monkeypatch.setattr(module, "MIN_ITEMS", 17)
    return module


def _export(tmp_path: Path, *, script: bool = True, images: bool = True) -> Path:
    """An export folder as ``/nesql gtnh-test`` leaves it, with the zip's folder entries too."""
    export = tmp_path / "nesql" / "gtnh-test"
    export.mkdir(parents=True)
    if script:
        shutil.copyfile(_SCRIPT, export / "nesql-db.script")
    if images:
        with zipfile.ZipFile(export / "image.zip", "w") as archive:
            for folder in ("item/", "item/minecraft/", "fluid/"):
                archive.writestr(folder, b"")
            for name, data in _IMAGES.items():
                archive.writestr(name, data)
    return export


def _run(tool: ModuleType, export: Path, tmp_path: Path, pack: str = _PACK) -> Path:
    index: Path = tool.main(
        [str(export), "--pack-version", pack, "--data-dir", str(tmp_path / "data")]
    )
    return index


def test_an_export_becomes_the_index_the_previewer_reads(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = _export(tmp_path)
    index = _run(tool, export, tmp_path)

    assert index == tmp_path / "data" / _PACK / "icons" / "index.json"
    # The archive is the export's own, and no temp file is left behind.
    assert sorted(path.name for path in index.parent.iterdir()) == ["images.zip", "index.json"]
    assert (index.parent / "images.zip").read_bytes() == (export / "image.zip").read_bytes()
    doc = json.loads(index.read_text(encoding="utf-8"))
    assert next(iter(doc)) == "generated_at"
    assert doc["source"] == {
        "exporter": "NESQL Exporter 0.5.7-ShadowTheAge",
        "commit": _LOCK["tools"]["nesql-exporter"]["commit"],
        "export": "gtnh-test",
        "pack_version": _PACK,
        "icon_px": 4,
    }
    out = capsys.readouterr().out
    assert "items: 17 rows -> 15 keys, 1 with only NBT variants, 11 without an image" in out
    assert "fluids: 3 rows -> 3 keys, 0 with only NBT variants, 2 without an image" in out


def test_the_index_answers_the_lookups_a_preview_makes(tool: ModuleType, tmp_path: Path) -> None:
    index = _run(tool, _export(tmp_path), tmp_path)
    with IconPack.load(index) as pack:
        gravel = pack.lookup("minecraft:gravel", Commodity.ITEM)
        assert gravel == IconEntry(name="Gravel", png="item/minecraft/gravel~0.png")
        assert pack.png(gravel) == _IMAGES["item/minecraft/gravel~0.png"]
        # A plan's mixed-case id, and a path the script wrote with backslashes.
        cell = pack.lookup("IC2:itemCellEmpty", Commodity.ITEM)
        assert cell is not None
        assert cell.name == "Empty Cell"
        assert pack.png(cell) == _IMAGES["item/IC2/itemCellEmpty~0.png"]
        # Any damage: the export's own @32767 row where it has one, else the lowest damage.
        wool = pack.lookup("minecraft:wool@32767", Commodity.ITEM)
        assert wool is not None
        assert wool.name == "Any Wool"
        log = pack.lookup("minecraft:log@32767", Commodity.ITEM)
        assert log == IconEntry(name="Oak Wood", png="item/minecraft/log~0.png")
        toluene = pack.lookup("liquid_toluene", Commodity.FLUID)
        assert toluene is not None
        assert pack.png(toluene) == _IMAGES["fluid/gregtech/liquid_toluene.png"]
        # A fluid the export rendered no picture of keeps its name.
        nitrobenzene = pack.lookup("nitrobenzene", Commodity.FLUID)
        assert nitrobenzene == IconEntry(name="Nitrobenzene", png=None)
        assert pack.png(nitrobenzene) is None
        assert pack.lookup("minecraft:stone", Commodity.ITEM) is None


def test_a_second_run_replaces_the_first(tool: ModuleType, tmp_path: Path) -> None:
    first = json.loads(_run(tool, _export(tmp_path), tmp_path).read_text(encoding="utf-8"))
    other = tmp_path / "again"
    other.mkdir()
    index = _run(tool, _export(other), tmp_path)
    assert json.loads(index.read_text(encoding="utf-8"))["items"] == first["items"]
    assert sorted(path.name for path in index.parent.iterdir()) == ["images.zip", "index.json"]


@pytest.mark.parametrize(
    ("missing", "message"),
    [("script", r"nesql-db\.script does not exist"), ("images", r"image\.zip does not exist")],
)
def test_an_export_missing_a_file_is_refused(
    tool: ModuleType, tmp_path: Path, missing: str, message: str
) -> None:
    export = _export(tmp_path, script=missing != "script", images=missing != "images")
    with pytest.raises(SystemExit, match=message):
        _run(tool, export, tmp_path)
    assert not (tmp_path / "data").exists()


def test_an_export_from_an_empty_nei_list_is_refused(tmp_path: Path) -> None:
    tool = _tool()  # the real floor
    assert tool.MIN_ITEMS == 10_000
    with pytest.raises(SystemExit, match=r"17 items, fewer than the 10,000 .*NEI"):
        _run(tool, _export(tmp_path), tmp_path)
    assert not (tmp_path / "data").exists()


def test_a_pack_other_than_the_locks_is_refused(tool: ModuleType, tmp_path: Path) -> None:
    # The export says nothing about its pack; the lock pins the exporter for one.
    with pytest.raises(SystemExit, match=r"not the pack gtnh\.lock\.json pins"):
        _run(tool, _export(tmp_path), tmp_path, pack="2.8.4")
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("SET SCHEMA PUBLIC\n", "no ITEM, FLUID, METADATA table: not a NESQL export"),
        (
            "CREATE CACHED TABLE PUBLIC.ITEM(ID VARCHAR(255) NOT NULL PRIMARY KEY)\n",
            "line 1: ITEM is a CACHED table",
        ),
    ],
    ids=["not-an-export", "cached-table"],
)
def test_a_script_that_cannot_be_read_is_refused(
    tool: ModuleType, tmp_path: Path, script: str, message: str
) -> None:
    export = _export(tmp_path)
    (export / "nesql-db.script").write_text(script, encoding="utf-8")
    with pytest.raises(SystemExit, match=message):
        _run(tool, export, tmp_path)


def test_an_archive_that_is_not_a_zip_is_refused(tool: ModuleType, tmp_path: Path) -> None:
    export = _export(tmp_path)
    (export / "image.zip").write_bytes(b"not a zip")
    with pytest.raises(SystemExit, match="is not a zip archive"):
        _run(tool, export, tmp_path)


def test_an_archive_with_no_png_records_no_icon_size(tool: ModuleType, tmp_path: Path) -> None:
    export = _export(tmp_path, images=False)
    with zipfile.ZipFile(export / "image.zip", "w") as archive:
        archive.writestr("item/", b"")
    doc = json.loads(_run(tool, export, tmp_path).read_text(encoding="utf-8"))
    assert doc["source"]["icon_px"] is None
    assert doc["items"]["minecraft:gravel"]["png"] is None


def test_the_runbook_builds_the_exporter_the_lock_pins() -> None:
    pin = _LOCK["tools"]["nesql-exporter"]
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    named = set(re.findall(r"\b[0-9a-f]{40}\b", runbook))
    assert pin["commit"] in named, "the runbook does not build the commit gtnh.lock.json pins"
    assert named <= {pin["commit"], pin["upstream_commit"]}, "the runbook names a stale commit"
    assert pin["repo"] in runbook
    assert pin["upstream"] in runbook
