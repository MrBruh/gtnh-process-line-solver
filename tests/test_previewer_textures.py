"""Tests for the previewer texture pipeline (issue #50): the pure resolution + embedding seam.

The chain machine ``type`` -> multiblock doc -> representative block -> manifest icon -> PNG
bytes -> ``data:`` URI is fully unit-tested here with fixture data and a fake PNG; the 135 MB jar
fetch (``previewer/jar.py``) is exercised only through an injected fake downloader, so no network
runs in the suite. The end-to-end proof against the real jar lives outside CI (see the PR notes).
"""

from __future__ import annotations

import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from gtnh_solver.ir import (
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
    Placement,
    Port,
)
from gtnh_solver.previewer import build_scene, render_html, write_preview
from gtnh_solver.previewer import jar as jar_mod
from gtnh_solver.previewer.textures import (
    TextureManifest,
    TextureSummary,
    apply_textures,
    load_multiblock_docs,
    resolve_face_icons,
    resolve_scene_types,
)
from gtnh_solver.previewer.textures import (
    texturize_scene as texturize,
)

_DATA = Path(__file__).resolve().parents[1] / "data"
_MULTIBLOCKS = _DATA / "multiblocks"
_MANIFEST = _DATA / "textures" / "manifest.json"

_HEATPROOF = "gregtech:iconsets/MACHINE_HEATPROOFCASING"
_HEATPROOF_PATH = "assets/gregtech/textures/blocks/iconsets/MACHINE_HEATPROOFCASING.png"


def _fake_png(color: int = 0x7F) -> bytes:
    """A minimal but structurally valid 1x1 PNG (signature + IHDR + IDAT + IEND)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, truecolour
    raw = bytes([0, color, color, color])  # one filtered scanline: filter byte + RGB
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _fake_jar(path: Path, members: dict[str, bytes]) -> Path:
    """Write a zip standing in for the GT5-Unofficial jar, with the given asset entries."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _ebf_machine() -> Machine:
    return Machine(
        id="ebf",
        type="Electric Blast Furnace",
        footprint=CellBox(sx=3, sy=4, sz=3),
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)]
        ),
    )


def _ebf_scene() -> dict:
    machine = _ebf_machine()
    problem = InputIR(bounding_region=CellBox(sx=8, sy=8, sz=8), machines=[machine])
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="ebf", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH)
        ],
    )
    return build_scene(problem, layout)


# --- TextureManifest -------------------------------------------------------------------------


def test_manifest_resolves_uniform_all_block_to_six_faces() -> None:
    manifest = TextureManifest.load(_MANIFEST)
    faces = manifest.block_face_icons("gregtech:gt.blockcasings", 11)
    assert faces == [_HEATPROOF] * 6  # an "all" entry fills every face
    assert manifest.icon_asset_path(_HEATPROOF) == _HEATPROOF_PATH


def test_manifest_per_side_block_lands_icons_on_the_right_faces() -> None:
    # A per-side block ({ "0".."5": icon }) maps ForgeDirection index -> three.js material slot.
    manifest = TextureManifest(
        {
            "blocks": {"m:b": {"metas": {"0": {"0": "top", "5": "east", "2": "north"}}}},
            "icons": {},
        }
    )
    faces = manifest.block_face_icons("m:b", 0)
    assert faces is not None
    # slot order [ +X east, -X west, +Y up, -Y down, +Z south, -Z north ]
    assert faces[0] == "east"  # GT side 5 -> slot 0
    assert faces[3] == "top"  # GT side 0 (down) -> slot 3
    assert faces[5] == "north"  # GT side 2 -> slot 5
    assert [faces[1], faces[2], faces[4]] == [None, None, None]  # unmapped faces stay empty


def test_manifest_accepts_bare_string_side_entry() -> None:
    manifest = TextureManifest({"blocks": {"m:b": {"metas": {"3": "flat"}}}, "icons": {}})
    assert manifest.block_face_icons("m:b", 3) == ["flat"] * 6


def test_manifest_missing_block_or_meta_is_none() -> None:
    manifest = TextureManifest.load(_MANIFEST)
    assert manifest.block_face_icons("gregtech:gt.blockcasings", 999) is None  # meta absent
    assert manifest.block_face_icons("no:such.block", 0) is None  # block absent
    assert manifest.icon_asset_path("no:such/icon") is None


def test_manifest_per_side_with_no_known_face_is_none() -> None:
    manifest = TextureManifest({"blocks": {"m:b": {"metas": {"0": {"9": "x"}}}}, "icons": {}})
    assert manifest.block_face_icons("m:b", 0) is None  # side 9 maps to no slot


# --- doc loading + representative-block resolution -------------------------------------------


def test_load_multiblock_docs_keys_by_display_name_and_skips_meta() -> None:
    docs = load_multiblock_docs(_MULTIBLOCKS)
    assert "Electric Blast Furnace" in docs
    assert "Vacuum Freezer" in docs
    assert all("_meta" not in name for name in docs)  # _meta.json excluded


def test_load_multiblock_docs_missing_dir_is_empty() -> None:
    assert load_multiblock_docs(_DATA / "does-not-exist") == {}


def test_resolve_ebf_falls_back_to_dominant_heatproof_casing() -> None:
    # The controller block (gt.blockmachines:1000) is a gapped hull, so resolution falls back to
    # the dominant resolvable casing the primary variant places: the heat-proof casing shell.
    docs = load_multiblock_docs(_MULTIBLOCKS)
    manifest = TextureManifest.load(_MANIFEST)
    faces = resolve_face_icons(docs["Electric Blast Furnace"], manifest)
    assert faces == [_HEATPROOF] * 6


def test_resolve_prefers_controller_block_when_manifest_has_it() -> None:
    # When the controller's own block resolves, it wins over the casing fallback.
    docs = load_multiblock_docs(_MULTIBLOCKS)
    doc = docs["Electric Blast Furnace"]
    manifest = TextureManifest(
        {
            "blocks": {doc.controller.registry_name: {"metas": {str(doc.controller.meta): "ctrl"}}},
            "icons": {},
        }
    )
    assert resolve_face_icons(doc, manifest) == ["ctrl"] * 6


def test_resolve_returns_none_when_nothing_in_manifest() -> None:
    docs = load_multiblock_docs(_MULTIBLOCKS)
    empty = TextureManifest({"blocks": {}, "icons": {}})
    assert resolve_face_icons(docs["Electric Blast Furnace"], empty) is None


def test_resolve_scene_types_only_documented_machines() -> None:
    scene = _ebf_scene()
    docs = load_multiblock_docs(_MULTIBLOCKS)
    manifest = TextureManifest.load(_MANIFEST)
    type_faces = resolve_scene_types(scene, docs, manifest)
    assert set(type_faces) == {"Electric Blast Furnace"}


# --- embedding -------------------------------------------------------------------------------


def test_apply_textures_embeds_data_uri_and_marks_machine() -> None:
    scene = _ebf_scene()
    textured = apply_textures(
        scene,
        {"Electric Blast Furnace": [_HEATPROOF] * 6},
        {_HEATPROOF: _fake_png()},
    )
    assert textured == {"Electric Blast Furnace"}
    assert scene["textures"][_HEATPROOF].startswith("data:image/png;base64,")
    assert scene["machines"][0]["texture"] == [_HEATPROOF] * 6


def test_apply_textures_missing_bytes_stays_placeholder() -> None:
    scene = _ebf_scene()
    textured = apply_textures(scene, {"Electric Blast Furnace": [_HEATPROOF] * 6}, {})
    assert textured == set()  # no PNG bytes supplied
    assert "texture" not in scene["machines"][0]
    assert scene["textures"] == {}


def test_apply_textures_partial_faces_drop_unfetched_sides() -> None:
    scene = _ebf_scene()
    faces = [_HEATPROOF, "other", None, None, None, None]
    apply_textures(scene, {"Electric Blast Furnace": faces}, {_HEATPROOF: _fake_png()})
    # only the fetched icon rides; the unfetched "other" and the empty slots become None
    assert scene["machines"][0]["texture"] == [_HEATPROOF, None, None, None, None, None]


# --- texturize_scene orchestration ----------------------------------------------------------


def test_texturize_scene_end_to_end_with_fake_provider() -> None:
    scene = _ebf_scene()
    calls: list[dict] = []

    def provider(icon_paths: dict) -> dict:
        calls.append(dict(icon_paths))
        return {icon: _fake_png() for icon in icon_paths}

    summary = texturize(
        scene, multiblocks_dir=_MULTIBLOCKS, manifest_path=_MANIFEST, png_provider=provider
    )
    assert isinstance(summary, TextureSummary)
    assert summary.textured_types == ("Electric Blast Furnace",)
    assert summary.placeholder_types == ()
    assert summary.embedded_icons == 1
    assert calls == [{_HEATPROOF: _HEATPROOF_PATH}]  # asked only for the icon actually needed
    assert scene["machines"][0]["texture"] == [_HEATPROOF] * 6


def test_texturize_scene_no_provider_call_when_no_documented_machine() -> None:
    # A scene of undocumented machines resolves nothing, so the provider (jar fetch) is never hit.
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=4, sz=4),
        machines=[
            Machine(
                id="p",
                type="Some Undocumented Machine",
                voltage_tier="LV",
                orientation_options=[Facing.NORTH],
                faces=FaceSpec(ports=[]),
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="p", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH)
        ],
    )
    scene = build_scene(problem, layout)

    def provider(icon_paths: dict) -> dict:  # pragma: no cover - must never be called here
        raise AssertionError("provider must not be called for undocumented machines")

    summary = texturize(
        scene, multiblocks_dir=_MULTIBLOCKS, manifest_path=_MANIFEST, png_provider=provider
    )
    assert summary.textured_types == ()
    assert summary.placeholder_types == ("Some Undocumented Machine",)
    assert scene["textures"] == {}


def test_texturize_scene_missing_dataset_degrades_to_placeholder() -> None:
    scene = _ebf_scene()
    summary = texturize(
        scene,
        multiblocks_dir=_DATA / "does-not-exist",
        manifest_path=_MANIFEST,
        png_provider=None,
    )
    assert summary.textured_types == ()
    assert summary.placeholder_types == ("Electric Blast Furnace",)
    assert scene["textures"] == {}


def test_texturize_scene_missing_manifest_degrades_to_placeholder() -> None:
    scene = _ebf_scene()
    summary = texturize(
        scene,
        multiblocks_dir=_MULTIBLOCKS,
        manifest_path=_DATA / "textures" / "nope.json",
        png_provider=None,
    )
    assert summary.textured_types == ()
    assert summary.placeholder_types == ("Electric Blast Furnace",)


# --- jar shim (no network) -------------------------------------------------------------------


def test_extract_icons_reads_present_skips_absent(tmp_path: Path) -> None:
    jar = _fake_jar(tmp_path / "x.jar", {_HEATPROOF_PATH: _fake_png()})
    got = jar_mod.extract_icons(jar, {_HEATPROOF: _HEATPROOF_PATH, "missing": "assets/nope.png"})
    assert set(got) == {_HEATPROOF}
    assert got[_HEATPROOF].startswith(b"\x89PNG")


def test_fetch_jar_returns_cached_without_download(tmp_path: Path) -> None:
    dest = tmp_path / jar_mod.JAR_NAME
    dest.write_bytes(b"cached")

    def download(url: str, filename: str) -> None:  # pragma: no cover - must not run on a cache hit
        raise AssertionError("download must not run when the jar is already cached")

    assert jar_mod.fetch_jar(tmp_path, download=download) == dest


def test_fetch_jar_downloads_when_absent(tmp_path: Path) -> None:
    def download(url: str, filename: str) -> None:
        Path(filename).write_bytes(b"downloaded")

    out = jar_mod.fetch_jar(tmp_path / "cache", url="http://example/x.jar", download=download)
    assert out.read_bytes() == b"downloaded"
    assert out.name == jar_mod.JAR_NAME
    assert not out.with_suffix(out.suffix + ".part").exists()  # renamed off the .part sibling


def test_jar_png_provider_fetches_then_extracts(tmp_path: Path) -> None:
    _fake_jar(tmp_path / jar_mod.JAR_NAME, {_HEATPROOF_PATH: _fake_png()})
    provider = jar_mod.jar_png_provider(tmp_path)  # cache hit -> no download
    got = provider({_HEATPROOF: _HEATPROOF_PATH})
    assert set(got) == {_HEATPROOF}


def test_jar_png_provider_empty_request_skips_fetch(tmp_path: Path) -> None:
    provider = jar_mod.jar_png_provider(tmp_path)  # no jar present; must not be fetched
    assert provider({}) == {}


def test_default_cache_dir_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GTNH_SOLVER_CACHE_DIR", str(tmp_path / "c"))
    assert jar_mod.default_cache_dir() == tmp_path / "c"
    monkeypatch.delenv("GTNH_SOLVER_CACHE_DIR", raising=False)
    assert jar_mod.default_cache_dir() == Path.home() / ".cache" / "gtnh_solver"


# --- html wiring + write_preview graceful degradation ---------------------------------------


def test_render_html_wires_textured_materials() -> None:
    html = render_html(_ebf_scene())
    assert "SCENE.textures" in html  # the embedded texture pool
    assert "NearestFilter" in html  # crisp GT pixel art
    assert "machineMaterials" in html  # per-face material builder


def test_render_html_embeds_data_uri_for_textured_machine() -> None:
    scene = _ebf_scene()
    apply_textures(scene, {"Electric Blast Furnace": [_HEATPROOF] * 6}, {_HEATPROOF: _fake_png()})
    html = render_html(scene)
    assert "data:image/png;base64," in html  # the PNG rides the page as a data URI
    assert json.dumps(scene) in html  # inlined verbatim


def test_write_preview_degrades_when_texture_pass_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A texture pass that blows up (e.g. offline jar fetch) must never block the preview.
    import gtnh_solver.previewer as previewer

    problem = InputIR(bounding_region=CellBox(sx=8, sy=8, sz=8), machines=[_ebf_machine()])
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="ebf", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH)
        ],
    )

    def boom(*_a: object, **_k: object) -> TextureSummary:
        raise RuntimeError("network down")

    monkeypatch.setattr(previewer, "texturize_scene", boom)
    out = write_preview(problem, layout, tmp_path / "v.html")
    assert out.exists()
    assert "gtnh-solve preview" in out.read_text(encoding="utf-8")


def test_write_preview_textures_off_skips_pass(tmp_path: Path) -> None:
    problem = InputIR(bounding_region=CellBox(sx=8, sy=8, sz=8), machines=[_ebf_machine()])
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="ebf", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH)
        ],
    )
    out = write_preview(problem, layout, tmp_path / "v.html", textures=False)
    assert out.exists()
    # textures off -> no texture pool key injected, no data URI
    assert "data:image/png;base64," not in out.read_text(encoding="utf-8")


# --- CLI texture-summary logging -------------------------------------------------------------

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def test_enable_previewer_logging_is_idempotent() -> None:
    import logging

    from gtnh_solver import cli

    logger = logging.getLogger("gtnh_solver")
    saved = logger.handlers[:]
    saved_level = logger.level
    for handler in saved:
        logger.removeHandler(handler)
    try:
        cli._enable_previewer_logging()
        cli._enable_previewer_logging()  # second call must not add a second handler
        assert len(logger.handlers) == 1
        assert logger.level == logging.INFO
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.setLevel(saved_level)


def test_cli_preview_logs_texture_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    from gtnh_solver import cli

    logger = logging.getLogger("gtnh_solver")
    saved = logger.handlers[:]
    saved_level = logger.level
    for handler in saved:
        logger.removeHandler(handler)  # force the handler (bound to capsys' stderr) to be created
    try:
        rc = cli.main([str(_SAND), "--fast", "--preview", str(tmp_path / "sand.html")])
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.setLevel(saved_level)
    assert rc == 0
    assert (tmp_path / "sand.html").exists()
    # the sand machines are undocumented, so every type falls back to a placeholder box
    assert "textures:" in capsys.readouterr().err
