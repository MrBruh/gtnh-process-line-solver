"""Item and fluid icons, by the plan's own resource ids, from a local icon index (#297).

The previewer names every fluid and item a line moves (#296) but has no picture of one: a plan
carries no image data, and the extractor's dev environment lacks the mods most items come from. A
full pack instance has them all, and a NESQL export run inside one renders every item and fluid
(``docs/dataset-extraction/icons.md``). That export is reduced to an **icon index**, which this
module reads::

    data/<version>/icons/
        index.json    {"generated_at": "<ISO-8601>",          <- first, for roots.generated_at
                       "schema": 1,
                       "source": {"exporter", "commit", "export", "pack_version", "icon_px"},
                       "items":  {"minecraft:gravel": {"name": "Gravel", "png": "item/..."}},
                       "fluids": {"liquid_toluene":  {"name": "Toluene", "png": null}}}
        images.zip    the export's own images; each "png" names one of its members, or is null

An item is keyed ``mod:internal`` with ``@damage`` after it unless the damage is 0, lowercased: the
spelling a plan uses (``gregtech:gt.metaitem.01@2032``). A fluid is keyed by its registry name.

**Display only, so it may cross packs.** Block ids never do (``cli._dataset_version_for``), but an
icon drawn from another pack's export is at worst a stale picture beside the right id, so
:func:`resolve_icon_index` falls back to the newest pack that has an index, and says so. Nothing
here joins the two dump halves (``cli._DUMP_HALVES``), since no solve or export needs an icon.

Nothing in the index is trusted further than a picture needs: an entry must be a name and a member
path, and a member must be a PNG of at most :data:`MAX_PNG_BYTES`. Anything else raises
:class:`IconPackError`, which the previewer reports and draws the plan's colours instead.
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from gtnh_solver.ir import Commodity

from .roots import resolve_dataset_path

#: Where an icon index sits inside a ``data/<version>/`` folder.
ICON_INDEX: Final = "icons/index.json"

#: The images an index names, beside it.
ICON_IMAGES: Final = "images.zip"

#: The one index layout this reader knows. A different number is refused, never guessed at.
INDEX_SCHEMA: Final = 1

#: The largest image read out of ``images.zip``. A 64 px icon is a few KB; the cap is there so a
#: corrupt or hostile archive cannot make a preview inflate something huge into the page.
MAX_PNG_BYTES: Final = 256 * 1024

#: Forge's ``OreDictionary.WILDCARD_VALUE``: ``minecraft:log@32767`` is "any log".
WILDCARD_DAMAGE: Final = 32767

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

#: The index's three maps, each of which must be a JSON object (``source`` may be left out).
_MAPS: Final = ("source", "items", "fluids")

_log = logging.getLogger(__name__)


class IconPackError(ValueError):
    """An icon index, or an image it names, that cannot be used: not the index layout, an entry
    that is not a name and a path, or a member that is missing, too large, or not a PNG."""


@dataclass(frozen=True, slots=True)
class IconEntry:
    """One item or fluid in the index: its English display name and its image's member path in
    ``images.zip``, or ``None`` when the export rendered no image for it."""

    name: str
    png: str | None


def resolve_icon_index(pack: str | None, data_dir: str | Path | None = None) -> Path | None:
    """The icon index to draw ``pack``'s icons from, or ``None`` when no pack has one.

    ``pack``'s own ``data/<pack>/icons/index.json`` when it exists, else the newest local pack's
    (by folder modification time, as every unpinned dataset lookup resolves), logged at INFO since
    the pictures then come from another pack. Icons are display only, so that is a fair stand-in
    where a block id would not be (module docstring).
    """
    if pack is not None:
        own = resolve_dataset_path(ICON_INDEX, version=pack, data_dir=data_dir)
        if own.is_file():
            return own
    newest = resolve_dataset_path(ICON_INDEX, data_dir=data_dir)
    if not newest.is_file():
        return None
    if pack is not None:
        _log.info("pack %s has no icon index; drawing icons from %s instead", pack, newest)
    return newest


class IconPack:
    """A loaded icon index and the ``images.zip`` beside it. Use it as a context manager, or call
    :meth:`close`, so the archive is not left open."""

    def __init__(
        self,
        path: Path,
        source: dict[str, Any],
        items: dict[str, Any],
        fluids: dict[str, Any],
    ) -> None:
        self.path = path
        self.source = source
        self._items = items
        self._fluids = fluids
        self._lowest: dict[str, str] | None = None
        self._zip: zipfile.ZipFile | None = None

    @classmethod
    def load(cls, path: str | Path) -> IconPack:
        """Read the index at ``path``; ``images.zip`` beside it is opened on the first image read.

        Raises :class:`IconPackError` for a file that is not JSON or not this layout, and
        ``OSError`` for one that cannot be read. Entries are checked as they are looked up rather
        than all at once, since a preview asks for a few dozen of an index's tens of thousands.
        """
        index = Path(path)
        try:
            doc = json.loads(index.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise IconPackError(f"{index} is not JSON: {exc}") from None
        if not isinstance(doc, dict):
            raise IconPackError(f"{index} is not an icon index (not a JSON object)")
        if doc.get("schema") != INDEX_SCHEMA:
            raise IconPackError(
                f"{index} has icon index schema {doc.get('schema')!r}; this reader knows "
                f"{INDEX_SCHEMA}"
            )
        maps: dict[str, Any] = {
            what: doc.get(what, {} if what == "source" else None) for what in _MAPS
        }
        for what, value in maps.items():
            if not isinstance(value, dict):
                raise IconPackError(f"{index} is not an icon index (no {what} map)")
        return cls(index, maps["source"], maps["items"], maps["fluids"])

    def lookup(self, resource: str, kind: Commodity) -> IconEntry | None:
        """The entry for the plan's ``resource`` of ``kind``, or ``None`` when the index has none.

        Lowercased, then exact. An item asked for as ``@32767`` (any damage) that the export has
        no row for takes the lowest damage of the same item, the one NEI lists first: the plan
        accepts any of them, and a picture of one is a picture of the item. Power has no icon.
        """
        if kind is Commodity.POWER:
            return None
        table = self._items if kind is Commodity.ITEM else self._fluids
        key = resource.lower()
        raw = table.get(key)
        if raw is None and kind is Commodity.ITEM:
            base, damage = _split_damage(key)
            if damage == WILDCARD_DAMAGE:
                lowest = self._lowest_damage().get(base)
                raw = table.get(lowest) if lowest is not None else None
        if raw is None:
            return None
        return _entry(raw, resource, self.path)

    def png(self, entry: IconEntry) -> bytes | None:
        """The image ``entry`` names, or ``None`` when it names none. Raises
        :class:`IconPackError` for a member the archive lacks, one over :data:`MAX_PNG_BYTES`,
        or one that is not a PNG."""
        if entry.png is None:
            return None
        archive = self._archive()
        try:
            info = archive.getinfo(entry.png)
        except KeyError:
            raise IconPackError(f"{archive.filename} has no {entry.png!r}") from None
        if info.file_size > MAX_PNG_BYTES:
            raise IconPackError(
                f"{entry.png!r} is {info.file_size} bytes, over the {MAX_PNG_BYTES} an icon may be"
            )
        data = archive.read(info)
        if not data.startswith(_PNG_SIGNATURE):
            raise IconPackError(f"{entry.png!r} in {archive.filename} is not a PNG")
        return data

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _archive(self) -> zipfile.ZipFile:
        if self._zip is None:
            images = self.path.parent / ICON_IMAGES
            try:
                self._zip = zipfile.ZipFile(images)
            except zipfile.BadZipFile as exc:
                raise IconPackError(f"{images} is not a zip archive: {exc}") from None
        return self._zip

    def _lowest_damage(self) -> dict[str, str]:
        """Each item, without its damage, to the key of its lowest-damage row. Built on the first
        wildcard lookup only, since most previews make none."""
        if self._lowest is None:
            lowest: dict[str, tuple[int, str]] = {}
            for key in self._items:
                base, damage = _split_damage(key)
                if base not in lowest or damage < lowest[base][0]:
                    lowest[base] = (damage, key)
            self._lowest = {base: key for base, (_, key) in lowest.items()}
        return self._lowest


def _split_damage(key: str) -> tuple[str, int]:
    """An item key's id and damage: ``"minecraft:log@1"`` -> ``("minecraft:log", 1)``, and a key
    with no ``@<digits>`` suffix has damage 0."""
    base, sep, damage = key.rpartition("@")
    if sep and damage.isdigit():
        return base, int(damage)
    return key, 0


def _entry(raw: object, resource: str, index: Path) -> IconEntry:
    if not isinstance(raw, dict):
        raise IconPackError(f"{index}: the entry for {resource!r} is not an object")
    name, png = raw.get("name"), raw.get("png")
    if not isinstance(name, str) or not (png is None or isinstance(png, str)):
        raise IconPackError(f"{index}: the entry for {resource!r} is not a name and an image path")
    return IconEntry(name=name, png=png)
