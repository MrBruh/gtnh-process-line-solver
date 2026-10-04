"""Tests for the jar shim (`previewer/jar.py`): the one network-touching seam, exercised offline.

The download itself is injected, so these prove the caching, extraction, and provider wiring without
ever hitting the network: a fake jar (a real in-memory zip) stands in for the 135 MB GT5-Unofficial
jar, and a fake downloader records calls and writes that zip to the requested path.

The multi-jar provider (#337) is exercised the same way, with one fake jar per pinned mod, served by
URL: that each icon reaches the jar its ``assets/<modid>/`` names, that a jar is fetched only when
asked for and at most once, and that an optional jar failing costs only its own icons.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from urllib.error import URLError

import pytest

from gtnh_solver.dataset.mod_jars import AE2, AE2FC, gt5u_jar
from gtnh_solver.previewer.jar import (
    JAR_NAME,
    JAR_URL,
    asset_modid,
    cached_jar,
    default_cache_dir,
    extract_icons,
    fetch_jar,
    gt5u_version_from_manifest,
    jar_png_provider,
    multi_jar_png_provider,
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


def test_default_cache_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_gt5u_version_from_manifest_reads_provenance(tmp_path: Path) -> None:
    m = tmp_path / "manifest.json"
    m.write_text(
        json.dumps({"provenance": {"mod_versions": {"GT5-Unofficial": "5.09.52.594"}}}),
        encoding="utf-8",
    )
    assert gt5u_version_from_manifest(m) == "5.09.52.594"


def test_gt5u_version_from_manifest_missing_file_or_field(tmp_path: Path) -> None:
    assert gt5u_version_from_manifest(tmp_path / "nope.json") is None  # unreadable file
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps({"provenance": {"mod_versions": {}}}), encoding="utf-8")
    assert gt5u_version_from_manifest(m) is None  # no GT5-Unofficial entry


def test_jar_png_provider_fetches_the_version_specific_jar(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def download(url: str, filename: str) -> None:
        seen.append((url, filename))
        _fake_jar(Path(filename), _ASSETS)

    provider = jar_png_provider(tmp_path, gt5u_version="9.9.9", download=download)
    icons = {
        "gregtech:iconsets/OVERLAY_FRONT": (
            "assets/gregtech/textures/blocks/iconsets/OVERLAY_FRONT.png"
        )
    }
    provider(icons)
    url, filename = seen[0]
    assert "GT5-Unofficial-9.9.9.jar" in url  # the version-specific URL
    assert filename.endswith("GT5-Unofficial-9.9.9.jar.part")  # cached per version


# --- the GT path, unchanged by the move to JarSpec ------------------------------------------------


def test_the_gt_jar_url_and_name_are_unchanged_by_jar_specs(tmp_path: Path) -> None:
    # The literal URL the previewer fetched before JarSpec existed: the refactor must not move it.
    assert JAR_URL == (
        "https://nexus.gtnewhorizons.com/repository/public/com/github/GTNewHorizons/"
        "GT5-Unofficial/5.09.51.482/GT5-Unofficial-5.09.51.482.jar"
    )
    assert JAR_NAME == "GT5-Unofficial-5.09.51.482.jar"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"provenance": {"mod_versions": {"GT5-Unofficial": "5.09.54.133"}}}),
        encoding="utf-8",
    )
    (tmp_path / "GT5-Unofficial-5.09.54.133.jar").write_bytes(b"")
    assert cached_jar(manifest, cache_dir=tmp_path) == tmp_path / "GT5-Unofficial-5.09.54.133.jar"


# --- the multi-jar provider -----------------------------------------------------------------------

_GT = gt5u_jar("9.9.9")

#: One fake jar per pinned mod, keyed by the URL the provider must fetch it from. Each carries only
#: its own namespace, so an icon routed to the wrong jar comes back missing rather than wrong.
_JARS: dict[str, dict[str, bytes]] = {
    _GT.url: {
        "assets/gregtech/textures/blocks/iconsets/OVERLAY_FRONT.png": b"gt-overlay",
        "assets/miscutils/textures/blocks/TileEntities/Casing.png": b"gt-addon",
    },
    AE2.url: {"assets/appliedenergistics2/textures/blocks/MECable_Grey.png": b"ae2-grey"},
    AE2FC.url: {"assets/ae2fc/textures/blocks/fluid_import_face.png": b"fc-import"},
}

_GT_ICON = {"gregtech:OVERLAY_FRONT": "assets/gregtech/textures/blocks/iconsets/OVERLAY_FRONT.png"}
_ADDON_ICON = {"miscutils:Casing": "assets/miscutils/textures/blocks/TileEntities/Casing.png"}
_AE2_ICON = {
    "appliedenergistics2:MECable_Grey": "assets/appliedenergistics2/textures/blocks/MECable_Grey.png"
}
_FC_ICON = {"ae2fc:fluid_import_face": "assets/ae2fc/textures/blocks/fluid_import_face.png"}


class _Nexus:
    """A fake downloader serving :data:`_JARS` by URL; ``failing`` URLs raise as an outage would."""

    def __init__(
        self, failing: frozenset[str] = frozenset(), garbage: frozenset[str] = frozenset()
    ):
        self.calls: list[str] = []
        self.failing = failing
        self.garbage = garbage

    def __call__(self, url: str, filename: str) -> None:
        self.calls.append(url)
        if url in self.failing:
            raise URLError("nexus unreachable")
        if url in self.garbage:
            Path(filename).write_bytes(b"this is not a zip")
            return
        _fake_jar(Path(filename), _JARS[url])


def test_asset_modid_reads_the_namespace_or_nothing() -> None:
    assert asset_modid("assets/appliedenergistics2/textures/blocks/X.png") == "appliedenergistics2"
    assert asset_modid("assets/ae2fc/x.png") == "ae2fc"
    assert asset_modid("textures/blocks/X.png") is None
    assert asset_modid("assets//X.png") is None
    assert asset_modid("assets/gregtech") is None


def test_each_icon_is_read_from_the_jar_its_namespace_names(tmp_path: Path) -> None:
    nexus = _Nexus()
    provider = multi_jar_png_provider(_GT, (AE2, AE2FC), cache_dir=tmp_path, download=nexus)

    out = provider({**_GT_ICON, **_ADDON_ICON, **_AE2_ICON, **_FC_ICON})

    assert out == {
        "gregtech:OVERLAY_FRONT": b"gt-overlay",
        "miscutils:Casing": b"gt-addon",  # an unclaimed namespace stays on the primary (GT) jar
        "appliedenergistics2:MECable_Grey": b"ae2-grey",
        "ae2fc:fluid_import_face": b"fc-import",
    }
    assert sorted(nexus.calls) == sorted([_GT.url, AE2.url, AE2FC.url])
    assert (tmp_path / AE2.jar_name).is_file()  # each pin caches under its own name


def test_a_gt_only_page_never_downloads_the_me_jars(tmp_path: Path) -> None:
    nexus = _Nexus()
    provider = multi_jar_png_provider(_GT, (AE2, AE2FC), cache_dir=tmp_path, download=nexus)

    assert provider({**_GT_ICON, **_ADDON_ICON}) == {
        "gregtech:OVERLAY_FRONT": b"gt-overlay",
        "miscutils:Casing": b"gt-addon",
    }
    assert nexus.calls == [_GT.url]
    assert provider({}) == {}
    assert nexus.calls == [_GT.url], "an empty icon set fetches nothing"


def test_a_jar_is_fetched_only_once_it_is_asked_for_and_at_most_once(tmp_path: Path) -> None:
    nexus = _Nexus()
    provider = multi_jar_png_provider(_GT, (AE2, AE2FC), cache_dir=tmp_path, download=nexus)

    provider(_AE2_ICON)
    provider(_AE2_ICON)
    provider({**_AE2_ICON, **_GT_ICON})

    assert nexus.calls == [AE2.url, _GT.url]


def test_a_failed_me_download_costs_only_its_own_icons(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    nexus = _Nexus(failing=frozenset({AE2.url}))
    provider = multi_jar_png_provider(_GT, (AE2, AE2FC), cache_dir=tmp_path, download=nexus)

    with caplog.at_level(logging.WARNING, logger="gtnh_solver.previewer.jar"):
        out = provider({**_GT_ICON, **_AE2_ICON, **_FC_ICON})

    assert out == {"gregtech:OVERLAY_FRONT": b"gt-overlay", "ae2fc:fluid_import_face": b"fc-import"}
    assert "appliedenergistics2" in caplog.text
    assert "unskinned" in caplog.text
    assert not (tmp_path / AE2.jar_name).exists(), "a failed download leaves no jar behind"
    # The failure is remembered: asking again neither retries the download nor raises.
    assert provider(_AE2_ICON) == {}
    assert nexus.calls.count(AE2.url) == 1


def test_an_me_jar_that_is_not_a_zip_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    nexus = _Nexus(garbage=frozenset({AE2FC.url}))
    provider = multi_jar_png_provider(_GT, (AE2, AE2FC), cache_dir=tmp_path, download=nexus)

    with caplog.at_level(logging.WARNING, logger="gtnh_solver.previewer.jar"):
        out = provider({**_FC_ICON, **_AE2_ICON})

    assert out == {"appliedenergistics2:MECable_Grey": b"ae2-grey"}
    assert "ae2fc" in caplog.text
    assert provider(_FC_ICON) == {}  # remembered as unusable, never re-read


def test_an_icon_its_jar_lacks_is_simply_missing(tmp_path: Path) -> None:
    provider = multi_jar_png_provider(_GT, (AE2,), cache_dir=tmp_path, download=_Nexus())

    out = provider(
        {
            **_AE2_ICON,
            "appliedenergistics2:Absent": "assets/appliedenergistics2/textures/blocks/Absent.png",
        }
    )

    assert out == {"appliedenergistics2:MECable_Grey": b"ae2-grey"}


def test_the_primary_jar_still_fails_loudly(tmp_path: Path) -> None:
    # As jar_png_provider does, so write_preview's own fallback (placeholder boxes) keeps owning it.
    nexus = _Nexus(failing=frozenset({_GT.url}))
    provider = multi_jar_png_provider(_GT, (AE2,), cache_dir=tmp_path, download=nexus)

    with pytest.raises(URLError):
        provider(_GT_ICON)


def test_a_primary_only_provider_routes_like_the_single_jar_one(tmp_path: Path) -> None:
    nexus = _Nexus()
    provider = multi_jar_png_provider(_GT, cache_dir=tmp_path, download=nexus)

    # Every namespace, and a path of no namespace at all, goes to the one jar.
    out = provider({**_GT_ICON, **_AE2_ICON, "odd": "not/an/assets/path.png"})

    assert out == {"gregtech:OVERLAY_FRONT": b"gt-overlay"}
    assert nexus.calls == [_GT.url]


def test_two_jars_claiming_one_namespace_is_refused() -> None:
    with pytest.raises(ValueError, match="appliedenergistics2"):
        multi_jar_png_provider(_GT, (AE2, AE2))
    with pytest.raises(ValueError, match="gregtech"):
        multi_jar_png_provider(_GT, (gt5u_jar("1.0"),))
