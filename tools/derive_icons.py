"""Derive the icon index a preview reads, from a NESQL export (#297).

``dataset/icons.py`` reads ``data/<version>/icons/index.json`` and the ``images.zip`` beside it.
This makes both from what NESQL Exporter leaves in ``<game dir>/nesql/<name>/`` after
``/nesql <name>`` in a full pack instance (``docs/dataset-extraction/icons.md`` has the whole run)::

    <export>/nesql-db.script --read_script--> ITEM, FLUID, METADATA rows --+
    <export>/image.zip ------member names-----------------------------------+--build_icon_index
        |
        +--> data/<version>/icons/images.zip   the export's own archive, copied as is
        +--> data/<version>/icons/index.json   written last, to a temp file and then os.replace

The reading and the join are ``gtnh_solver.dataset.nesql``'s. No image is unpacked: the archive is
read for its member names, and for one PNG header, the size the export rendered its icons at.

**Refused, and nothing written:**

- a missing ``nesql-db.script`` or ``image.zip``, or a script with no ITEM, FLUID or METADATA table;
- fewer than :data:`MIN_ITEMS` items. NEI's item list was empty when the export ran (the exporter
  only refuses that itself while its NEI plugin is on);
- a ``--pack-version`` other than ``gtnh.lock.json``'s ``pack_version``. **The export records
  nothing about the pack it ran in**: its METADATA table holds the exporter's version and a
  timestamp, nothing else, so no check of the export against the pack is possible. What can be held
  is the lock, which pins this exporter for its pack, and whose exporter commit the index is
  stamped with: an index for another pack would carry a provenance that is not true of it. To index
  another pack, bump the lock (and re-check the exporter) first;
- anything in the script the reader does not understand (``dataset/nesql.py``).

Usage (from the repo root)::

    python tools/derive_icons.py <game dir>/nesql/gtnh-2.9.0-beta-2 --pack-version 2.9.0-beta-2
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gtnh_solver.dataset.icons import ICON_IMAGES, ICON_INDEX
from gtnh_solver.dataset.nesql import (
    IndexStats,
    NesqlError,
    Row,
    TableStats,
    build_icon_index,
    read_script,
)

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "gtnh.lock.json"
DATA = REPO / "data"
RUNBOOK = "docs/dataset-extraction/icons.md"

#: What the exporter writes in its repository folder (``Exporter.java``).
SCRIPT = "nesql-db.script"
IMAGES = "image.zip"

#: The exporter's entry under ``tools`` in ``gtnh.lock.json``.
EXPORTER = "nesql-exporter"

#: A whole 2.9 instance's NEI list exports about 50,000 items; the forge plugin alone, which runs
#: without NEI's list, a few hundred. Well clear of both.
MIN_ITEMS = 10_000

#: The tables read: the two an index is made of, and the one that says which exporter made them.
TABLES = ("ITEM", "FLUID", "METADATA")

_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def main(argv: list[str] | None = None) -> Path:
    """Derive the index for the export ``argv`` names; returns the ``index.json`` written."""
    args = _parser().parse_args(argv)
    export, pack, data_dir = Path(args.export), str(args.pack_version), Path(args.data_dir)
    pin = exporter_pin(pack)
    script, images = export / SCRIPT, export / IMAGES
    for path in (script, images):
        if not path.is_file():
            raise SystemExit(f"{path} does not exist: not a NESQL export folder (see {RUNBOOK})")
    tables = read_tables(script)
    names, icon_px = read_archive(images)
    version = next((row.get("VERSION") for row in tables["METADATA"]), None)
    source: dict[str, Any] = {
        "exporter": f"NESQL Exporter {version}" if isinstance(version, str) else "NESQL Exporter",
        "commit": pin["commit"],
        "export": export.name,
        "pack_version": pack,
        "icon_px": icon_px,
    }
    doc, stats = build_icon_index(tables["ITEM"], tables["FLUID"], names, source=source)
    index = data_dir / pack / ICON_INDEX
    index.parent.mkdir(parents=True, exist_ok=True)
    _replace(index.parent / ICON_IMAGES, lambda tmp: shutil.copyfile(images, tmp))
    text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
    _replace(index, lambda tmp: tmp.write_text(text, encoding="utf-8"))
    print(f"wrote {index} and {ICON_IMAGES} beside it, from {export}")
    print(_report(stats))
    return index


def exporter_pin(pack_version: str, lock_path: Path = LOCK) -> dict[str, Any]:
    """The lock's exporter pin, once ``pack_version`` is the pack the lock pins it for."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    locked = lock["pack_version"]
    if pack_version != locked:
        raise SystemExit(
            f"--pack-version {pack_version} is not the pack {lock_path.name} pins ({locked}). The "
            "export records nothing about the pack it ran in, so the index is held to the lock, "
            f"which pins this exporter for {locked}; bump the lock first to index another pack"
        )
    pin: dict[str, Any] = lock["tools"][EXPORTER]
    return pin


def read_tables(script: Path) -> dict[str, list[Row]]:
    """The export's ITEM, FLUID and METADATA rows, or ``SystemExit`` for a script that is not a
    NESQL export, holds what the reader does not understand, or has too few items."""
    try:
        with script.open(encoding="utf-8") as lines:
            tables = read_script(lines, TABLES)
    except NesqlError as exc:
        raise SystemExit(f"{script}: {exc}") from None
    missing = [table for table in TABLES if table not in tables]
    if missing:
        raise SystemExit(f"{script} has no {', '.join(missing)} table: not a NESQL export")
    items = len(tables["ITEM"])
    if items < MIN_ITEMS:
        raise SystemExit(
            f"{script} has {items} items, fewer than the {MIN_ITEMS:,} a whole pack exports: NEI's "
            "item list was empty when it ran. Open NEI in the world once, then export again "
            f"(see {RUNBOOK})"
        )
    return tables


def read_archive(images: Path) -> tuple[list[str], int | None]:
    """The archive's member names, and the width of its first PNG (the icon size the export
    rendered at), or ``None`` when it holds no PNG."""
    try:
        with zipfile.ZipFile(images) as archive:
            names = archive.namelist()
            first = next((name for name in names if name.endswith(".png")), None)
            header = archive.open(first).read(len(_PNG_HEADER) + 4) if first else b""
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"{images} is not a zip archive: {exc}") from None
    if not header.startswith(_PNG_HEADER):
        return names, None
    width: int = struct.unpack(">I", header[len(_PNG_HEADER) :])[0]
    return names, width


def _replace(target: Path, write: Callable[[Path], object]) -> None:
    """Write ``target`` through a temp file beside it, moved into place only once it is whole."""
    handle, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(handle)
    temp = Path(name)
    try:
        write(temp)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _report(stats: IndexStats) -> str:
    def line(kind: str, table: TableStats) -> str:
        return (
            f"  {kind}: {table.rows} rows -> {table.keys} keys, {table.nbt_only} with only NBT "
            f"variants, {table.without_image} without an image"
        )

    return "\n".join(
        [
            line("items", stats.items),
            line("fluids", stats.fluids),
            f"  names keep {stats.unknown_glyphs} unknown font glyphs (dataset/nesql.py _GLYPHS)",
        ]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="derive_icons.py", description="Derive data/<version>/icons/ from a NESQL export."
    )
    parser.add_argument("export", help="the export folder: <game dir>/nesql/<name>")
    parser.add_argument(
        "--pack-version", required=True, help="the pack it was exported from; must be the lock's"
    )
    parser.add_argument(
        "--data-dir", default=str(DATA), help="the data/ folder to write into (default: the repo's)"
    )
    return parser


if __name__ == "__main__":
    main()
