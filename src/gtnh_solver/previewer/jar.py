"""previewer.jar - the thin, network-touching shim that supplies texture PNG bytes from mod jars.

The texture *mapping* (:mod:`gtnh_solver.previewer.textures`) is pure and fully tested; this
module is the one place that reaches outside the process: it fetches a pinned mod jar from the GTNH
Nexus once, caches it in a gitignored location **outside the repo tree**, and reads the requested
``assets/<modid>/...png`` entries straight out of the zip. It hands
:func:`gtnh_solver.previewer.textures.texturize_scene` a ``png_provider`` closure so the download
stays an injected dependency the test suite never triggers.

Two providers, one per need::

    jar_png_provider(gt5u_version=...)       one jar, GT5-Unofficial, every path to it: today's
                                             preview, at the version the manifest was read at

    multi_jar_png_provider(primary, extras)  several jars, routed by asset namespace:
        {icon: "assets/<modid>/..."}
          |  group by modid: an extra jar claims its own modid (AE2 appliedenergistics2,
          |  FC ae2fc), and the primary takes every path no extra claims. GT's jar carries
          |  its addons' namespaces too (miscutils, bartworks, ...), so it stays the default
          v
        per jar that was asked for at least one icon, and only then: fetch it (once per
        provider) and read its icons. A GT-only page never downloads AE2.

**The primary jar fails loudly, an extra one quietly.** A failed primary fetch raises exactly as
:func:`jar_png_provider` does, so the caller's existing degrade path keeps owning it
(``write_preview`` logs and falls back to placeholder boxes). A failed extra fetch, or an extra jar
that will not open or inflate, is logged once and only that jar's icons go missing: an AE2 download
failing must not cost a page the GT textures that did arrive. A cached extra that is not a usable
zip is deleted as well, so one bad download does not fail every later preview.

PNGs are read from the cached jar at preview time and embedded only in the emitted HTML, never
committed. **Their licence is the jar's, not this project's**: GT's art is LGPL like its code, but
AE2's textures are CC BY-NC-SA 3.0 although its code is LGPL (``docs/spikes/329-me-ae2.md``
section 9.2), so a preview that embeds AE2 art must carry AE2's credit and a link to that licence.
AE2FluidCraft declares LGPL-3.0 throughout, but nobody has checked whether its sprites derive from
AE2's art, so a preview with FC art is shared on AE2's terms too. ``NOTICE`` credits each mod whose
jar is read.
"""

from __future__ import annotations

import json
import logging
import os
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from urllib.request import urlretrieve

from gtnh_solver.dataset.mod_jars import JarSpec, gt5u_jar

from .textures import PngProvider

_log = logging.getLogger(__name__)

#: The fallback GT5-Unofficial version, used when the active manifest carries no provenance version.
#: Normally the manifest's provenance supplies it (see :func:`gt5u_version_from_manifest`), so the
#: fetched jar matches the manifest the icons were extracted against.
JAR_VERSION = "5.09.51.482"

JAR_NAME = gt5u_jar(JAR_VERSION).jar_name
JAR_URL = gt5u_jar(JAR_VERSION).url


def gt5u_version_from_manifest(manifest_path: str | Path) -> str | None:
    """The GT5-Unofficial version the manifest at ``manifest_path`` was extracted against.

    Read from ``provenance.mod_versions["GT5-Unofficial"]`` so the fetched jar matches the manifest
    the icons were named against. ``None`` if the manifest is absent, unreadable, or lacks the field,
    in which case the caller keeps the pinned :data:`JAR_VERSION` default.
    """
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except OSError, ValueError:
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


def cached_jar(manifest_path: str | Path, cache_dir: str | Path | None = None) -> Path | None:
    """The already-cached jar matching ``manifest_path``'s version, or ``None``. Never downloads.

    :func:`fetch_jar` exists for the preview path, where the 135 MB download is the point. A caller
    that merely wants to ASK something of the jar (which sprites it carries, say) must not trigger
    one as a side effect, and must be able to tell "the jar says no" from "there was no jar" - so
    this answers ``None`` rather than fetching, and the caller reports which happened.
    """
    version = gt5u_version_from_manifest(manifest_path) or JAR_VERSION
    directory = default_cache_dir() if cache_dir is None else Path(cache_dir)
    candidate = directory / gt5u_jar(version).jar_name
    return candidate if candidate.is_file() else None


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
        url = gt5u_jar(gt5u_version).url
        jar_name = gt5u_jar(gt5u_version).jar_name

    def provider(icon_paths: Mapping[str, str]) -> dict[str, bytes]:
        if not icon_paths:
            return {}
        jar = fetch_jar(cache_dir, url=url, jar_name=jar_name, download=download)
        return extract_icons(jar, icon_paths)

    return provider


def asset_modid(asset_path: str) -> str | None:
    """The ``<modid>`` of an ``assets/<modid>/...`` path, or ``None`` for a path of any other shape."""
    parts = asset_path.split("/", 2)
    if len(parts) == 3 and parts[0] == "assets" and parts[1]:
        return parts[1]
    return None


#: What a failed optional jar raises: a download or disk error (``urlretrieve``'s ``URLError`` is
#: an ``OSError``), a cached file that is not a zip, or a member that will not inflate.
_JAR_FAILURES = (OSError, zipfile.BadZipFile, zlib.error)

#: The failures that mean the cached file itself is bad: a captive portal's HTML page saved under
#: the jar's name, a truncated or bit-rotted copy. Cached, it would fail every preview from now on,
#: so it is deleted and the next preview downloads it again.
_CORRUPT_JAR = (zipfile.BadZipFile, zlib.error)


def multi_jar_png_provider(
    primary: JarSpec,
    extras: Iterable[JarSpec] = (),
    *,
    cache_dir: str | Path | None = None,
    download: Downloader = urlretrieve,
) -> PngProvider:
    """A ``png_provider`` reading each icon from the jar its ``assets/<modid>/`` namespace names.

    ``extras`` each claim their own :attr:`~JarSpec.modid`; ``primary`` serves every other path,
    including one with no ``assets/<modid>/`` shape at all, so a primary-only provider routes exactly
    as :func:`jar_png_provider` does. Two jars claiming one modid is a configuration error, raised
    here rather than settled by order.

    Each jar is fetched lazily, the first time a call asks for one of its icons, and at most once per
    provider: its path is remembered, and so is an extra's failure, so a page that asks twice neither
    re-reads the cache nor retries a download that already failed. The module docstring says why the
    primary jar's failure propagates and an extra's is logged instead.
    """
    claimed: dict[str, JarSpec] = {}
    for spec in extras:
        if spec.modid in claimed or spec.modid == primary.modid:
            raise ValueError(f"two jars claim the assets/{spec.modid}/ namespace")
        claimed[spec.modid] = spec
    located: dict[JarSpec, Path | None] = {}

    def jar_for(asset_path: str) -> JarSpec:
        modid = asset_modid(asset_path)
        return primary if modid is None else claimed.get(modid, primary)

    def read(spec: JarSpec, wanted: dict[str, str]) -> dict[str, bytes]:
        if spec not in located:
            located[spec] = _fetch_spec(spec, cache_dir, download)
        jar = located[spec]
        return {} if jar is None else extract_icons(jar, wanted)

    def provider(icon_paths: Mapping[str, str]) -> dict[str, bytes]:
        by_jar: dict[JarSpec, dict[str, str]] = {}
        for icon, asset_path in icon_paths.items():
            by_jar.setdefault(jar_for(asset_path), {})[icon] = asset_path
        out: dict[str, bytes] = {}
        for spec, wanted in by_jar.items():
            if spec == primary:
                out.update(read(spec, wanted))
                continue
            try:
                out.update(read(spec, wanted))
            except _JAR_FAILURES as exc:
                cached = located.get(spec)
                discarded = (
                    _discard(cached) if isinstance(exc, _CORRUPT_JAR) and cached is not None else ""
                )
                _log.warning(
                    "textures: the %s jar (%s) is unavailable, so its %d icon(s) stay unskinned: "
                    "%s%s",
                    spec.modid,
                    spec.url,
                    len(wanted),
                    exc,
                    discarded,
                )
                located[spec] = None
        return out

    return provider


def _fetch_spec(spec: JarSpec, cache_dir: str | Path | None, download: Downloader) -> Path:
    """:func:`fetch_jar` for one pinned :class:`JarSpec`, cached under its own file name."""
    return fetch_jar(cache_dir, url=spec.url, jar_name=spec.jar_name, download=download)


def _discard(jar: Path) -> str:
    """Delete a corrupt cached jar, and say so for the warning that reports it.

    Never raises: it runs while an optional jar's failure is being absorbed, and an error escaping
    here would reach ``write_preview`` and cost the page its GT textures too.
    """
    try:
        jar.unlink(missing_ok=True)
    except OSError as exc:
        return f"; the corrupt cached copy {jar} could not be deleted ({exc}): delete it by hand"
    return f"; deleted the corrupt cached copy {jar}, so the next preview downloads it again"
