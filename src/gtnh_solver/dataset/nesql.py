"""A NESQL export's items and fluids, reduced to the icon index ``dataset.icons`` reads (#297).

NESQL Exporter (ShadowTheAge's fork, pinned in ``gtnh.lock.json`` under ``tools``) runs inside a
full pack instance and leaves two files: an HSQLDB database script, ``nesql-db.script``, and the
icons it rendered, ``image.zip``. This module reads the two tables an icon index needs out of the
script and builds the index. It does no I/O of its own: ``tools/derive_icons.py`` hands it the
script's lines and the archive's member names (``docs/dataset-extraction/icons.md``)::

    nesql-db.script --read_script--> ITEM, FLUID rows --+
    image.zip ------member names-------------------------+--build_icon_index--> index doc + stats

**The script.** The exporter ends with ``SHUTDOWN COMPACT``, which leaves every MEMORY table whole in
the script: a ``CREATE MEMORY TABLE PUBLIC.<T>(...)`` line giving the column order, then, after a
``SET SCHEMA PUBLIC`` line, one ``INSERT INTO <T> VALUES(...)`` line per row. A string is ``'...'``
with ``''`` for a quote. HSQLDB 2.7.2 (the exporter's pin) writes every character outside
``0x20..0x7f`` as ``\\uXXXX``, one per UTF-16 unit, so a character beyond the BMP is a surrogate
pair, and a backslash as ``\\u005c`` when a ``u`` follows it, so it cannot read as an escape; any
other backslash is written as is (``org.hsqldb.lib.StringConverter.stringToUnicodeBytes``). The
reader takes those, ``NULL``, ``TRUE``/``FALSE`` and integers, and refuses with
:class:`NesqlError` whatever else it meets rather than guess: a CACHED or TEXT table, whose rows
are not in the script at all; a binary or decimal literal; a row whose arity is not its table's.

**The join.** A plan names an item ``mod:internal@damage`` and a fluid by its registry name, so an
item is keyed by its lowercased ``MOD_ID:INTERNAL_NAME`` with ``@ITEM_DAMAGE`` unless that is 0
(:func:`item_id`), and a fluid by its lowercased ``INTERNAL_NAME``. NESQL's ``ID`` column is never
joined on: it is ``i~<mod>~<name>~<damage>[~<nbt hash>]`` with the ``:`` and other unsafe characters
stripped, so it is not the plan's spelling. Rows that share a key are NBT variants of one item, and
the NBT-free one is its picture; a key with only NBT variants takes the lowest ``ID`` of them, and
the stats count those keys so a run says how many it had to pick.

**The names.** ``LOCALIZED_NAME`` is the English display name as the game draws it, ``\\u00a7``
formatting codes and all, and GT:NH draws some symbols from private-use glyphs of its own font.
:func:`clean_name` strips the codes and swaps the glyphs it knows for the Unicode symbol they look
like; an unknown one stays in and is counted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from .icons import INDEX_SCHEMA

type Value = str | int | bool | None
"""One column's value in a row, as the script spells it: a string, an integer, a boolean or NULL."""

type Row = dict[str, Value]
"""One row of a table, by column name."""


class NesqlError(ValueError):
    """A NESQL export this module will not read: a table whose rows are not in the script, a value
    or line it does not understand, a row that does not fit its table, or a row with no usable key.
    The message names the line, or the row, where it is known."""


#: The schema the exporter's tables are in, and so the only one rows are read from. HSQLDB keeps
#: tables of its own in the same script under another ``SET SCHEMA`` (``SYSTEM_LOBS.BLOCKS``).
_SCHEMA: Final = "PUBLIC"

#: The table type whose rows the script carries. CACHED rows live in ``nesql-db.data`` and TEXT
#: rows in a CSV file beside it, so a script that declares either for a wanted table cannot be read.
_IN_SCRIPT: Final = "MEMORY"

#: ``CREATE [<type> ...] TABLE <schema>.<name>(``. Matched only on a line that starts ``CREATE ``.
_CREATE: Final = re.compile(r"CREATE (?P<kind>(?:\w+ )*?)TABLE (?P<name>[^\s(]+)\(")

#: What may lead a part of a CREATE line's column list other than a column name.
_CONSTRAINTS: Final = frozenset({"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"})

_INTEGER: Final = re.compile(r"-?[0-9]+")
_HEX4: Final = re.compile(r"[0-9a-fA-F]{4}")

_INSERT: Final = "INSERT INTO "
_VALUES: Final = " VALUES("

#: ShadowTheAge's table of GT:NH's private-use font glyphs and the Unicode symbol each is drawn as,
#: mirrored from export/FontCharactersFixer.cs of https://github.com/ShadowTheAge/gtnh
#: (MIT License, Copyright (c) 2025 ShadowTheAge; see NOTICE). Escapes, so no lookalike character
#: hides in the source.
_GLYPHS: Final[dict[str, str]] = {
    "\ue000": "\u25b3",  # white up-pointing triangle
    "\ue001": "\u25bd",  # white down-pointing triangle
    "\ue002": "\u25b3",
    "\ue003": "\u25bd",
    "\ue004": "\u25b3",
    "\ue005": "\u25bd",
    "\ue006": "\u2742",  # circled open centre eight pointed star
    "\ue007": "\u26cf",  # pick
    "\ue008": "\u21f2",  # south east arrow to corner
    "\ue009": "\u21f1",  # north west arrow to corner
    "\ue00a": "\u26a1",  # high voltage sign
    "\ue00c": "\u2742",
    "\ue00d": "\u205b",  # four dot mark
    "\ue00e": "\u235d",  # APL functional symbol up shoe jot
    "\ue00f": "\u2298",  # circled division slash
    "\ue010": "\u2070",  # superscript zero
    "\ue011": "\u00b9",  # superscript one
    "\ue012": "\u00b2",  # superscript two
    "\ue013": "\u00b3",  # superscript three
    "\ue014": "\u2074",  # superscript four
    "\ue015": "\u2075",
    "\ue016": "\u2076",
    "\ue017": "\u2077",
    "\ue018": "\u2078",
    "\ue019": "\u2079",  # superscript nine
    "\ue01a": "\u2080",  # subscript zero
    "\ue01d": "\u265c",  # black chess rook
    "\ue01e": "\u265b",  # black chess queen
    "\ue01f": "\u2a02",  # n-ary circled times operator
    "\ue020": "\ufe56",  # small question mark
}

#: A formatting code (the section sign and the character after it) or a private-use glyph.
_NAME_NOISE: Final = re.compile("\u00a7.?|[\ue000-\uf8ff]", re.DOTALL)

#: What the exporter writes for both names of an item whose name threw (``ItemFactory``): no name.
_FAILED_NAME: Final = "ERROR"


def read_script(lines: Iterable[str], tables: Collection[str]) -> dict[str, list[Row]]:
    """The rows of each of ``tables`` in an HSQLDB script, each a dict by column name.

    ``tables`` are named as the script names them, upper case and unqualified (``"ITEM"``), and are
    read from the ``PUBLIC`` schema. The result has a list for every one of them the script
    creates, empty when it has no rows; one it never creates is left out, for the caller to decide
    about. Only ``CREATE ... TABLE``, ``SET SCHEMA`` and the wanted tables' ``INSERT`` lines are
    parsed; every other line is passed over by a prefix check, since a whole export runs to
    hundreds of thousands of lines. Raises :class:`NesqlError` for what the module docstring lists.
    """
    wanted = frozenset(tables)
    inserts = tuple(f"{_INSERT}{table}{_VALUES}" for table in wanted)
    columns: dict[str, list[str]] = {}
    rows: dict[str, list[Row]] = {}
    schema: str | None = None
    for number, raw in enumerate(lines, 1):
        if raw.startswith(_INSERT):
            if not raw.startswith(inserts) or schema != _SCHEMA:
                continue
            line = raw.rstrip("\r\n")
            start = line.index(_VALUES)
            table = line[len(_INSERT) : start]
            names = columns.get(table)
            if names is None:
                raise NesqlError(f"line {number}: a row of {table} before its CREATE line")
            values = _values(line, start + len(_VALUES), number)
            if len(values) != len(names):
                raise NesqlError(
                    f"line {number}: a row of {table} has {len(values)} values, but its CREATE "
                    f"line names {len(names)} columns"
                )
            rows[table].append(dict(zip(names, values, strict=True)))
        elif raw.startswith("CREATE "):
            created = _created_table(raw.rstrip("\r\n"), wanted, schema, number)
            if created is not None:
                table, names = created
                columns[table] = names
                rows[table] = []
        elif raw.startswith("SET SCHEMA "):
            schema = raw.removeprefix("SET SCHEMA ").strip().strip('"')
    return rows


def clean_name(text: str) -> tuple[str, int]:
    """``text`` as a plain display name, and how many private-use glyphs it kept for not knowing
    them: each formatting code (the section sign and the character after it, whatever case) is
    removed, each known GT:NH font glyph becomes the symbol it is drawn as, and the result is
    stripped of outer whitespace."""
    unknown = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal unknown
        found = match.group()
        if found.startswith("\u00a7"):
            return ""
        symbol = _GLYPHS.get(found)
        if symbol is None:
            unknown += 1
            return found
        return symbol

    return _NAME_NOISE.sub(swap, text).strip(), unknown


def item_id(mod_id: str, internal_name: str, damage: int) -> str:
    """An item's index key, spelled the way a plan spells it: ``mod:internal``, then ``@damage``
    unless the damage is 0, lowercased (``gregtech:gt.metaitem.01@2032``, ``minecraft:gravel``)."""
    key = f"{mod_id}:{internal_name}".lower()
    return key if damage == 0 else f"{key}@{damage}"


@dataclass(frozen=True, slots=True)
class TableStats:
    """What one table came to: its rows, the keys written, the keys that had only NBT variants
    (one of which stands in, see the module docstring), and the keys written with no image."""

    rows: int
    keys: int
    nbt_only: int
    without_image: int


@dataclass(frozen=True, slots=True)
class IndexStats:
    """What :func:`build_icon_index` wrote, per table, and how many unknown glyphs its names keep."""

    items: TableStats
    fluids: TableStats
    unknown_glyphs: int


def build_icon_index(
    item_rows: Sequence[Mapping[str, Value]],
    fluid_rows: Sequence[Mapping[str, Value]],
    zip_names: Iterable[str],
    *,
    source: Mapping[str, Any],
    generated_at: str | None = None,
) -> tuple[dict[str, Any], IndexStats]:
    """The icon index for an export's ITEM and FLUID rows, and what it came to.

    ``zip_names`` are the member names of the export's ``image.zip``. An entry's ``png`` is its
    row's ``IMAGE_FILE_PATH`` with ``/`` separators when the archive has that member, else ``None``.
    ``source`` is copied in as the index's provenance and ``generated_at`` (now, when not given)
    stamps it. The document is the format ``dataset.icons`` reads, ``generated_at`` first and every
    map sorted by key. Raises :class:`NesqlError` for a row missing a column the join needs.
    """
    members = frozenset(name.replace("\\", "/") for name in zip_names)
    items, item_stats, item_glyphs = _entries(item_rows, _item_key, members)
    fluids, fluid_stats, fluid_glyphs = _entries(fluid_rows, _fluid_key, members)
    doc: dict[str, Any] = {
        "generated_at": generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema": INDEX_SCHEMA,
        "source": dict(source),
        "items": items,
        "fluids": fluids,
    }
    return doc, IndexStats(item_stats, fluid_stats, item_glyphs + fluid_glyphs)


def _entries(
    rows: Sequence[Mapping[str, Value]],
    key_of: Callable[[Mapping[str, Value]], str],
    members: frozenset[str],
) -> tuple[dict[str, dict[str, str | None]], TableStats, int]:
    """One table's index entries by key, sorted, with its stats and its names' unknown glyphs.

    Rows sharing a key are its NBT variants: the NBT-free row (NBT empty or NULL) is the entry, and
    with none, or with several (two spellings that only differ in case), the lowest ``ID`` is."""
    groups: dict[str, list[Mapping[str, Value]]] = {}
    for row in rows:
        groups.setdefault(key_of(row), []).append(row)
    out: dict[str, dict[str, str | None]] = {}
    nbt_only = without_image = glyphs = 0
    for key in sorted(groups):
        plain = [row for row in groups[key] if not _optional_text(row, "NBT")]
        nbt_only += not plain
        row = min(plain or groups[key], key=lambda r: _text(r, "ID"))
        name, unknown = _display_name(row)
        glyphs += unknown
        path = _optional_text(row, "IMAGE_FILE_PATH")
        png = path.replace("\\", "/") if path else None
        if png not in members:
            png = None
            without_image += 1
        out[key] = {"name": name, "png": png}
    return out, TableStats(len(rows), len(out), nbt_only, without_image), glyphs


def _item_key(row: Mapping[str, Value]) -> str:
    return item_id(_text(row, "MOD_ID"), _text(row, "INTERNAL_NAME"), _int(row, "ITEM_DAMAGE"))


def _fluid_key(row: Mapping[str, Value]) -> str:
    return _text(row, "INTERNAL_NAME").lower()


def _created_table(
    line: str, wanted: frozenset[str], schema: str | None, number: int
) -> tuple[str, list[str]] | None:
    """A wanted table's name and column names from its CREATE line, or ``None`` for any other line.
    Refuses a wanted table that is not a MEMORY table, since its rows are not in the script."""
    match = _CREATE.match(line)
    if match is None:
        return None
    qualifier, _, table = match.group("name").replace('"', "").rpartition(".")
    if table not in wanted or (qualifier or schema) != _SCHEMA:
        return None
    kind = match.group("kind").strip()
    if kind != _IN_SCRIPT:
        raise NesqlError(
            f"line {number}: {table} is a {kind or 'plain'} table, not a {_IN_SCRIPT} one, so its "
            "rows are not in the script; this reader takes MEMORY tables only"
        )
    if not line.endswith(")"):
        raise NesqlError(f"line {number}: the CREATE line of {table} does not end its column list")
    names = []
    for part in _split_top_level(line[match.end() : -1], number):
        head = part.strip()
        if head.startswith('"'):
            name = head[1 : head.find('"', 1)]
        else:
            name = head.split(" ", 1)[0].split("(", 1)[0]
        if name and name not in _CONSTRAINTS:
            names.append(name)
    if not names or len(set(names)) != len(names):
        raise NesqlError(f"line {number}: cannot read the columns of {table}")
    return table, names


def _split_top_level(body: str, number: int) -> list[str]:
    """A CREATE line's column list split at the commas outside parentheses and quotes."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    for i, char in enumerate(body):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    if quote or depth:
        raise NesqlError(f"line {number}: unbalanced quotes or parentheses in a CREATE line")
    parts.append(body[start:])
    return parts


def _values(line: str, start: int, number: int) -> list[Value]:
    """The values of an INSERT line from ``start`` (just after ``VALUES(``) to its closing ``)``."""
    values: list[Value] = []
    end = len(line)
    i = start
    while True:
        if i >= end:
            raise NesqlError(f"line {number}: the row ends before its closing parenthesis")
        if line[i] == "'":
            pieces = []
            j = i + 1
            while True:
                close = line.find("'", j)
                if close < 0:
                    raise NesqlError(f"line {number}: a string is never closed")
                pieces.append(line[j:close])
                if line.startswith("'", close + 1):
                    pieces.append("'")
                    j = close + 2
                    continue
                i = close + 1
                break
            values.append(_unescape("".join(pieces), number))
        else:
            stop = i
            while stop < end and line[stop] not in ",)":
                stop += 1
            values.append(_scalar(line[i:stop], number))
            i = stop
        if i >= end:
            raise NesqlError(f"line {number}: the row ends before its closing parenthesis")
        if line[i] == ")" and i == end - 1:
            return values
        if line[i] != ",":
            raise NesqlError(f"line {number}: unexpected text after a value at column {i + 1}")
        i += 1


def _scalar(token: str, number: int) -> Value:
    """An unquoted value: ``NULL``, ``TRUE``, ``FALSE`` or an integer, and nothing else."""
    if token == "NULL":
        return None
    if token in ("TRUE", "FALSE"):
        return token == "TRUE"
    if _INTEGER.fullmatch(token):
        return int(token)
    what = "a binary literal" if token[:2] in ("X'", "x'") else f"the value {token[:40]!r}"
    raise NesqlError(
        f"line {number}: cannot read {what}; this reader knows strings, integers, booleans and "
        "NULL, which is all an ITEM or FLUID row holds"
    )


def _unescape(raw: str, number: int) -> str:
    """A string literal's text: each ``\\uXXXX`` decoded in one pass, a surrogate pair joined into
    the one character it encodes (a lone surrogate, which no text can hold, becomes U+FFFD), and
    any other backslash kept as it is, as HSQLDB writes it."""
    if "\\u" not in raw:
        return raw
    out: list[str] = []
    i = 0
    while (k := raw.find("\\u", i)) >= 0:
        out.append(raw[i:k])
        code = _hex4(raw, k, number)
        i = k + 6
        if 0xD800 <= code <= 0xDBFF and raw.startswith("\\u", i):
            low = _hex4(raw, i, number)
            if 0xDC00 <= low <= 0xDFFF:
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                i += 6
        out.append("\ufffd" if 0xD800 <= code <= 0xDFFF else chr(code))
    out.append(raw[i:])
    return "".join(out)


def _hex4(raw: str, at: int, number: int) -> int:
    """The code unit of the ``\\uXXXX`` escape at ``at``."""
    digits = raw[at + 2 : at + 6]
    if not _HEX4.fullmatch(digits):
        raise NesqlError(f"line {number}: a \\u escape without four hex digits ({digits!r})")
    return int(digits, 16)


def _display_name(row: Mapping[str, Value]) -> tuple[str, int]:
    """The row's cleaned English name and its unknown glyphs (:func:`clean_name`), or ``""`` for
    the exporter's stand-in for a name that threw, which no preview should show."""
    localized = _text(row, "LOCALIZED_NAME")
    if localized == _FAILED_NAME and row.get("UNLOCALIZED_NAME") == _FAILED_NAME:
        return "", 0
    return clean_name(localized)


def _text(row: Mapping[str, Value], column: str) -> str:
    value = row.get(column)
    if not isinstance(value, str):
        raise NesqlError(f"a row has no text {column} ({value!r}): {_describe(row)}")
    return value


def _optional_text(row: Mapping[str, Value], column: str) -> str | None:
    value = row.get(column)
    if value is not None and not isinstance(value, str):
        raise NesqlError(f"a row's {column} is not text ({value!r}): {_describe(row)}")
    return value


def _int(row: Mapping[str, Value], column: str) -> int:
    value = row.get(column)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NesqlError(f"a row has no integer {column} ({value!r}): {_describe(row)}")
    return value


def _describe(row: Mapping[str, Value]) -> str:
    """Enough of a row to find it in the script."""
    return repr(row.get("ID", dict(row)))
