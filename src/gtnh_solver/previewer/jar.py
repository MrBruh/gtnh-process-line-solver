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

import os
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.request import urlretrieve

from .textures import PngProvider

#: The pinned jar - version-locked so a texture matches the dataset it was extracted against. A
#: pack bump changes this alongside the manifest (lane 6 / the texture workflow), not by hand here.
JAR_VERSION = "5.09.51.482"
JAR_NAME = f"GT5-Unofficial-{JAR_VERSION}.jar"
JAR_URL = (
    "https://nexus.gtnewhorizons.com/repository/public/com/github/GTNewHorizons/"
    f"GT5-Unofficial/{JAR_VERSION}/{JAR_NAME}"
)

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
    download: Downloader = urlretrieve,
) -> Path:
    """Return the cached jar path, downloading it to the cache first if it is not already there.

    Idempotent: a present cache file is returned untouched (no network). The download lands on a
    ``.part`` sibling and is renamed on success so an interrupted fetch never leaves a truncated
    jar that looks complete.
    """
    directory = default_cache_dir() if cache_dir is None else Path(cache_dir)
    dest = directory / JAR_NAME
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
    url: str = JAR_URL,
    download: Downloader = urlretrieve,
) -> PngProvider:
    """Build the ``png_provider`` closure the texturizer calls: fetch the jar, extract the icons."""

    def provider(icon_paths: Mapping[str, str]) -> dict[str, bytes]:
        if not icon_paths:
            return {}
        jar = fetch_jar(cache_dir, url=url, download=download)
        return extract_icons(jar, icon_paths)

    return provider
