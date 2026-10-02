"""The preview's icon pass (``previewer.icons`` and ``write_preview``, #297).

What a page embeds from an icon index: a picture of each fluid and item the line moves and nothing
else, the export's name only where the plan has none, and, without a usable index, no pictures at
all but a page all the same. The index is hand-made under ``tmp_path`` with ``DEFAULT_DATA``
pointed at it, as ``test_schematic`` stages a dump.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset import roots
from gtnh_solver.dataset.icons import IconPack
from gtnh_solver.ir import Commodity, InputIR, LayoutResult
from gtnh_solver.previewer import ICON_RUNBOOK, write_preview
from gtnh_solver.previewer.icons import carried_kinds, resource_art
from tests._helpers import solid_png, write_icon_index

_STONE = solid_png((84, 84, 84))
_GRAVEL = solid_png((130, 120, 110))
_DIAMOND = solid_png((80, 220, 220))


def _sand_index(folder: Path, *, sand: bool = True) -> Path:
    """An index naming two of the sand line's items with pictures, sand by name only (unless
    ``sand`` is off), and a diamond the line never touches."""
    items: dict[str, dict[str, str | None]] = {
        "minecraft:stone": {"name": "Export Stone", "png": "item/minecraft/stone~0.png"},
        "minecraft:gravel": {"name": "Export Gravel", "png": "item/minecraft/gravel~0.png"},
        "minecraft:diamond": {"name": "Diamond", "png": "item/minecraft/diamond~0.png"},
    }
    if sand:
        items["minecraft:sand"] = {"name": "Export Sand", "png": None}
    return write_icon_index(
        folder,
        items=items,
        images={
            "item/minecraft/stone~0.png": _STONE,
            "item/minecraft/gravel~0.png": _GRAVEL,
            "item/minecraft/diamond~0.png": _DIAMOND,
        },
    )


def _scene_of(page: Path) -> dict[str, Any]:
    line = next(
        ln
        for ln in page.read_text(encoding="utf-8").splitlines()
        if ln.startswith("const SCENE = ")
    )
    scene: dict[str, Any] = json.loads(
        line.removeprefix("const SCENE = ").removesuffix(";").replace("<\\/", "</")
    )
    return scene


def _labels(scene: dict[str, Any]) -> set[str]:
    """Every resource label the page prints, on any surface."""
    labels = {c["label"] for m in scene["machines"] for c in m["contents"]}
    labels |= {r["label"] for route in scene["routes"] for r in route["resources"]}
    for flow in [*scene["io"]["inputs"], *scene["io"]["outputs"]]:
        labels |= {r["label"] for r in flow["resources"]}
    return labels


def _png_of(uri: str) -> bytes:
    prefix = "data:image/png;base64,"
    assert uri.startswith(prefix)
    return base64.b64decode(uri.removeprefix(prefix))


def test_carried_kinds_are_the_lines_resources_and_what_each_is(
    solved_sand: tuple[InputIR, LayoutResult],
) -> None:
    ir, _ = solved_sand
    assert carried_kinds(ir) == {
        "minecraft:cobblestone": Commodity.ITEM,
        "minecraft:gravel": Commodity.ITEM,
        "minecraft:sand": Commodity.ITEM,
        "minecraft:stone": Commodity.ITEM,
    }


def test_only_what_the_line_carries_is_embedded(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult]
) -> None:
    ir, _ = solved_sand
    with IconPack.load(_sand_index(tmp_path / "icons")) as pack:
        names, icons = resource_art(ir, pack)
    assert set(icons) == {"minecraft:stone", "minecraft:gravel"}  # no diamond, no picture of sand
    assert _png_of(icons["minecraft:stone"]) == _STONE
    assert names == {
        "minecraft:stone": "Export Stone",
        "minecraft:gravel": "Export Gravel",
        "minecraft:sand": "Export Sand",
    }


def test_a_page_embeds_the_icons_of_its_plans_pack(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    ir, layout = solved_sand
    assert ir.pack_version == "2.8.4"
    _sand_index(tmp_path / "2.8.4" / "icons")
    monkeypatch.setattr(roots, "DEFAULT_DATA", tmp_path)
    scene = _scene_of(write_preview(ir, layout, tmp_path / "view.html", textures=False))
    assert set(scene["icons"]) == {"minecraft:stone", "minecraft:gravel"}
    assert _png_of(scene["icons"]["minecraft:gravel"]) == _GRAVEL
    # The plan colours every resource, and keeps doing so: an icon wins over a dot in the viewer.
    assert set(scene["resourceColors"]) == set(carried_kinds(ir))


def test_the_plans_name_wins_then_the_exports_then_the_id(
    tmp_path: Path, solved_sand: tuple[InputIR, LayoutResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sand line shows two resources on its surfaces (its two chests): stone and sand.
    ir, layout = solved_sand
    ir = ir.model_copy(update={"resource_names": {"minecraft:stone": "Plan Stone"}})
    monkeypatch.setattr(roots, "DEFAULT_DATA", tmp_path)
    _sand_index(tmp_path / "2.8.4" / "icons")
    labels = _labels(_scene_of(write_preview(ir, layout, tmp_path / "view.html", textures=False)))
    assert labels == {"Plan Stone (minecraft:stone)", "Export Sand (minecraft:sand)"}
    _sand_index(tmp_path / "2.8.4" / "icons", sand=False)  # now nobody names the sand
    labels = _labels(_scene_of(write_preview(ir, layout, tmp_path / "view.html", textures=False)))
    assert labels == {"Plan Stone (minecraft:stone)", "minecraft:sand"}


def test_an_unusable_index_still_writes_the_page(
    tmp_path: Path,
    solved_sand: tuple[InputIR, LayoutResult],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ir, layout = solved_sand
    index = _sand_index(tmp_path / "2.8.4" / "icons")
    index.write_text("{garbage", encoding="utf-8")
    monkeypatch.setattr(roots, "DEFAULT_DATA", tmp_path)
    with caplog.at_level(logging.WARNING, logger="gtnh_solver.previewer"):
        page = write_preview(ir, layout, tmp_path / "view.html", textures=False)
    assert "icon pass skipped" in caplog.text
    scene = _scene_of(page)
    assert scene["icons"] == {}
    assert scene["resourceColors"]  # the page falls back to the plan's colours


def test_a_bad_image_skips_the_icon_pass_rather_than_half_drawing_it(
    tmp_path: Path,
    solved_sand: tuple[InputIR, LayoutResult],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ir, layout = solved_sand
    write_icon_index(
        tmp_path / "2.8.4" / "icons",
        items={"minecraft:stone": {"name": "Stone", "png": "stone.png"}},
        images={"stone.png": b"not a png"},
    )
    monkeypatch.setattr(roots, "DEFAULT_DATA", tmp_path)
    with caplog.at_level(logging.WARNING, logger="gtnh_solver.previewer"):
        scene = _scene_of(write_preview(ir, layout, tmp_path / "view.html", textures=False))
    assert "icon pass skipped" in caplog.text
    assert scene["icons"] == {}


def test_no_index_names_the_runbook(
    tmp_path: Path,
    solved_sand: tuple[InputIR, LayoutResult],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The suite's pinned data holds no icon index, which is a fresh clone's (and CI's) state.
    ir, layout = solved_sand
    with caplog.at_level(logging.INFO, logger="gtnh_solver.previewer"):
        scene = _scene_of(write_preview(ir, layout, tmp_path / "view.html", textures=False))
    assert ICON_RUNBOOK in caplog.text
    assert scene["icons"] == {}
