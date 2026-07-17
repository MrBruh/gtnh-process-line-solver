"""previewer.jar - the thin, network-touching shim that supplies GT iconset PNG bytes.

The texture *mapping* (:mod:`gtnh_solver.previewer.textures`) is pure and fully tested; this
module is the one place that reaches outside the process: it fetches the pinned GT5-Unofficial jar
from the GTNH Nexus once, caches it in a gitignored location **outside the repo tree**, and reads
the requested ``iconsets/*.png`` entries straight out of the zip. It hands
:func:`gtnh_solver.previewer.textures.texturize_scene` a ``png_provider`` closure so the 135 MB
download stays an injected dependency the test suite never triggers.

PNGs are LGPL: they are read from the cached jar at preview time and embedded only in the emitted
HTML, never committed. ``NOTICE`` credits GT5-Unofficial and StructureLib.
"""

from __future__ import annotations

import json
import os
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.request import urlretrieve

from .textures import PngProvider

#: The fallback GT5-Unofficial version, used when the active manifest carries no provenance version.
#: Normally the manifest's provenance supplies it (see :func:`gt5u_version_from_manifest`), so the
#: fetched jar matches the manifest the icons were extracted against.
JAR_VERSION = "5.09.51.482"


def _jar_name(gt5u_version: str) -> str:
    """The jar filename for ``gt5u_version`` (cached per version so several can coexist)."""
    return f"GT5-Unofficial-{gt5u_version}.jar"


def _jar_url(gt5u_version: str) -> str:
    """The GTNH Nexus URL for the ``gt5u_version`` jar."""
    return (
        "https://nexus.gtnewhorizons.com/repository/public/com/github/GTNewHorizons/"
        f"GT5-Unofficial/{gt5u_version}/{_jar_name(gt5u_version)}"
    )


JAR_NAME = _jar_name(JAR_VERSION)
JAR_URL = _jar_url(JAR_VERSION)


def gt5u_version_from_manifest(manifest_path: str | Path) -> str | None:
    """The GT5-Unofficial version the manifest at ``manifest_path`` was extracted against.

    Read from ``provenance.mod_versions["GT5-Unofficial"]`` so the fetched jar matches the manifest
    the icons were named against. ``None`` if the manifest is absent, unreadable, or lacks the field,
    in which case the caller keeps the pinned :data:`JAR_VERSION` default.
    """
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mods = raw.get("provenance", {}).get("mod_versions", {})
    version = mods.get("GT5-Unofficial") if isinstance(mods, dict) else None
    return version if isinstance(version, str) and version else None


#: Environment override for the cache directory; otherwise a per-user cache OUTSIDE the repo tree,
#: so the multi-megabyte jar is never at risk of being staged (no ``.gitignore`` entry needed).
_CACHE_ENV = "GTNH_SOLVER_CACHE_DIR"

#: A downloader with ``urlretrieve``'s ``(url, filename) -> Any`` shape; injectable so a test can
#: exercise the download branch without a network round-trip.
Downloader = Callable[[str, str], object]


def default_cache_dir() -> Path:
    """The jar cache directory: ``$GTNH_SOLVER_CACHE_DIR`` if set, else ``~/.cache/gtnh_solver``."""
    override = os.environ.get(_CACHE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "gtnh_solver"


def fetch_jar(
    cache_dir: str | Path | None = None,
    *,
    url: str = JAR_URL,
    jar_name: str = JAR_NAME,
    download: Downloader = urlretrieve,
) -> Path:
    """Return the cached jar path, downloading it to the cache first if it is not already there.

    Idempotent: a present cache file is returned untouched (no network). The download lands on a
    ``.part`` sibling and is renamed on success so an interrupted fetch never leaves a truncated
    jar that looks complete. ``jar_name`` is the version-specific filename, so different pack
    versions cache side by side.
    """
    directory = default_cache_dir() if cache_dir is None else Path(cache_dir)
    dest = directory / jar_name
    if dest.exists():
        return dest
    directory.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    download(url, str(partial))
    partial.replace(dest)
    return dest


def extract_icons(jar_path: str | Path, icon_paths: Mapping[str, str]) -> dict[str, bytes]:
    """Read the requested ``{icon_name: asset_path}`` PNG entries out of the jar zip.

    Icons whose asset path is not present in the jar are simply omitted (never an error), so a
    manifest that lags the jar by an icon or two degrades to a placeholder for that block rather
    than failing the whole preview.
    """
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(jar_path) as archive:
        members = set(archive.namelist())
        for icon, asset_path in icon_paths.items():
            if asset_path in members:
                out[icon] = archive.read(asset_path)
    return out


def jar_png_provider(
    cache_dir: str | Path | None = None,
    *,
    gt5u_version: str | None = None,
    url: str = JAR_URL,
    jar_name: str = JAR_NAME,
    download: Downloader = urlretrieve,
) -> PngProvider:
    """Build the ``png_provider`` closure the texturizer calls: fetch the jar, extract the icons.

    A ``gt5u_version`` (typically from the active manifest's provenance, via
    :func:`gt5u_version_from_manifest`) overrides ``url`` and ``jar_name`` so the fetched jar matches
    the manifest the icons were extracted against, caching each version separately.
    """
    if gt5u_version is not None:
        url = _jar_url(gt5u_version)
        jar_name = _jar_name(gt5u_version)

    def provider(icon_paths: Mapping[str, str]) -> dict[str, bytes]:
        if not icon_paths:
            return {}
        jar = fetch_jar(cache_dir, url=url, jar_name=jar_name, download=download)
        return extract_icons(jar, icon_paths)

    return provider
