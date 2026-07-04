"""Tests for the jar shim (`previewer/jar.py`): the one network-touching seam, exercised offline.

The download itself is injected, so these prove the caching, extraction, and provider wiring without
ever hitting the network: a fake jar (a real in-memory zip) stands in for the 135 MB GT5-Unofficial
jar, and a fake downloader records calls and writes that zip to the requested path.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from gtnh_solver.previewer.jar import (
    JAR_NAME,
    default_cache_dir,
    extract_icons,
    fetch_jar,
    jar_png_provider,
)


def _fake_jar(path: Path, entries: dict[str, bytes]) -> None:
    """Write a real zip at ``path`` with ``{asset_path: bytes}`` members - a stand-in GT jar."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


_ASSETS = {
    "assets/gregtech/textures/blocks/iconsets/MACHINE_HEATPROOFCASING.png": b"\x89PNG-casing",
    "assets/gregtech/textures/blocks/iconsets/OVERLAY_FRONT.png": b"\x89PNG-overlay",
}


def test_default_cache_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GTNH_SOLVER_CACHE_DIR", str(tmp_path / "cache"))
    assert default_cache_dir() == tmp_path / "cache"
    monkeypatch.delenv("GTNH_SOLVER_CACHE_DIR", raising=False)
    assert default_cache_dir().name == "gtnh_solver"  # falls back to ~/.cache/gtnh_solver


def test_fetch_jar_downloads_once_then_caches(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def download(url: str, filename: str) -> None:
        calls.append((url, filename))
        _fake_jar(Path(filename), _ASSETS)

    first = fetch_jar(tmp_path, url="http://example/jar", download=download)
    assert first == tmp_path / JAR_NAME
    assert first.exists()
    assert len(calls) == 1
    assert calls[0][1].endswith(".part"), "downloads land on a .part sibling, then rename"
    # A second call is a cache hit: no second download.
    second = fetch_jar(tmp_path, url="http://example/jar", download=download)
    assert second == first
    assert len(calls) == 1


def test_extract_icons_reads_present_and_omits_missing(tmp_path: Path) -> None:
    jar = tmp_path / JAR_NAME
    _fake_jar(jar, _ASSETS)
    icons = extract_icons(
        jar,
        {
            "gregtech:iconsets/MACHINE_HEATPROOFCASING": (
                "assets/gregtech/textures/blocks/iconsets/MACHINE_HEATPROOFCASING.png"
            ),
            "gregtech:iconsets/ABSENT": "assets/gregtech/textures/blocks/iconsets/ABSENT.png",
        },
    )
    assert set(icons) == {"gregtech:iconsets/MACHINE_HEATPROOFCASING"}  # missing one omitted
    assert icons["gregtech:iconsets/MACHINE_HEATPROOFCASING"] == b"\x89PNG-casing"


def test_jar_png_provider_fetches_then_extracts(tmp_path: Path) -> None:
    def download(url: str, filename: str) -> None:
        _fake_jar(Path(filename), _ASSETS)

    provider = jar_png_provider(tmp_path, url="http://example/jar", download=download)
    out = provider(
        {
            "gregtech:iconsets/OVERLAY_FRONT": (
                "assets/gregtech/textures/blocks/iconsets/OVERLAY_FRONT.png"
            )
        }
    )
    assert out == {"gregtech:iconsets/OVERLAY_FRONT": b"\x89PNG-overlay"}


def test_jar_png_provider_no_icons_never_fetches(tmp_path: Path) -> None:
    calls: list[str] = []

    def download(url: str, filename: str) -> None:
        calls.append(url)

    provider = jar_png_provider(tmp_path, url="http://example/jar", download=download)
    assert provider({}) == {}
    assert calls == [], "an empty icon set must not trigger a 135 MB download"
