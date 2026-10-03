"""``dataset.nesql`` (#297): a NESQL export's ITEM and FLUID rows, and the icon index made of them.

The export is an HSQLDB script, read here line by line rather than through a database, so every rule
of that dialect the reader relies on is pinned: the column order a CREATE line gives, the string
escapes, the values, and each refusal. ``tests/fixtures/nesql/nesql-db.script`` is hand-written in
the dialect (its shape checked against a script HSQLDB 2.7.2 wrote through the exporter's own
Hibernate mapping); its rows are made up to hit the rules, and its columns are deliberately not in
the order the exporter writes them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from gtnh_solver.dataset.icons import INDEX_SCHEMA
from gtnh_solver.dataset.nesql import (
    _GLYPHS,
    IndexStats,
    NesqlError,
    Row,
    TableStats,
    Value,
    build_icon_index,
    clean_name,
    item_id,
    read_script,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nesql" / "nesql-db.script"

#: A one-table script for the dialect's rules: ``T(A, B)`` in PUBLIC.
_TABLE = "CREATE MEMORY TABLE PUBLIC.T(A VARCHAR(255) NOT NULL PRIMARY KEY,B INTEGER)"

#: The members of the export's ``image.zip`` the fixture's rows find, folders included as the
#: exporter's zip file system writes them. Every other row's image is missing.
_ZIP = (
    "item/",
    "item/minecraft/",
    "item/minecraft/gravel~0.png",
    "item/minecraft/log~0.png",
    "item/minecraft/wool~0.png",
    "item/minecraft/wool~32767.png",
    "item/IC2/itemCellEmpty~0.png",
    "item/gregtech/gt.metaitem.01~2032.png",
    "item/gregtech/gt.metaitem.01~32405.png",
    "item/gregtech/gt.metaitem.01~32405~Qm9vdA.png",
    "item/gregtech/gt.metatool.01~16~QmFy.png",
    "item/gregtech/gt.metatool.01~16~Zm9v.png",
    "item/gregtech/gt.metaitem.01~32597.png",
    "fluid/gregtech/liquid_toluene.png",
    "fluid/gregtech/hydrogen.png",
)

_SOURCE = {
    "exporter": "NESQL Exporter test",
    "commit": "0" * 40,
    "export": "fixture",
    "pack_version": "test",
    "icon_px": 64,
}


def _fixture(*tables: str) -> dict[str, list[Row]]:
    with _FIXTURE.open(encoding="utf-8") as script:
        return read_script(script, tables)


def _script(*lines: str, create: str = _TABLE, tables: Sequence[str] = ("T",)) -> list[Row]:
    """The rows of ``T`` in a script of ``create``, then ``SET SCHEMA PUBLIC``, then ``lines``."""
    return read_script([create, "SET SCHEMA PUBLIC", *lines], tables)["T"]


def _a(literal: str) -> Value:
    """What the reader makes of ``literal`` as the ``A`` of a row."""
    return _script(f"INSERT INTO T VALUES({literal},1)")[0]["A"]


def _b(literal: str) -> Value:
    """What the reader makes of ``literal`` as the ``B`` of a row."""
    return _script(f"INSERT INTO T VALUES('x',{literal})")[0]["B"]


def _hsqldb_literal(text: str) -> str:
    """``text`` as HSQLDB 2.7.2 writes a string into a script (``org.hsqldb.lib.StringConverter
    .stringToUnicodeBytes``): per UTF-16 unit, ``0x20..0x7f`` as is with a quote doubled, a
    backslash as is unless a ``u`` follows it, then as the escape of a backslash, and every other
    unit as a lowercase four-digit escape."""
    data = text.encode("utf-16-be")
    units = [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]
    out = ["'"]
    for i, unit in enumerate(units):
        if unit == 0x5C:
            out.append("\\u005c" if units[i + 1 : i + 2] == [0x75] else "\\")
        elif 0x20 <= unit <= 0x7F:
            out.append("''" if unit == 0x27 else chr(unit))
        else:
            out.append(f"\\u{unit:04x}")
    out.append("'")
    return "".join(out)


# --- the script -------------------------------------------------------------------------------


def test_a_rows_columns_come_from_its_create_line() -> None:
    # The fixture's columns are not in the exporter's order, so a reader that assumed it would
    # put every value under the wrong name.
    tables = _fixture("ITEM", "FLUID")
    assert tables["ITEM"][0] == {
        "ID": "i~minecraft~gravel~0",
        "LOCALIZED_NAME": "Gravel",
        "MOD_ID": "minecraft",
        "ITEM_DAMAGE": 0,
        "INTERNAL_NAME": "gravel",
        "NBT": "",
        "IMAGE_FILE_PATH": "item/minecraft/gravel~0.png",
        "ITEM_ID": 13,
        "MAX_DAMAGE": 0,
        "MAX_STACK_SIZE": 64,
        "UNLOCALIZED_NAME": "tile.gravel",
    }
    hydrogen = tables["FLUID"][1]
    assert (hydrogen["INTERNAL_NAME"], hydrogen["DENSITY"], hydrogen["GASEOUS"]) == (
        "hydrogen",
        -100,
        True,
    )


def test_only_the_wanted_tables_are_read() -> None:
    # ITEM_TOOLTIP's rows start "INSERT INTO ITEM", and MOB_INFO_DROPS holds a double the reader
    # would refuse, and a CACHED table sits among them: none of it is touched.
    tables = _fixture("ITEM", "FLUID", "METADATA")
    assert {name: len(rows) for name, rows in tables.items()} == {
        "ITEM": 17,
        "FLUID": 3,
        "METADATA": 1,
    }
    assert tables["METADATA"] == [
        {"ID": 0, "CREATION_TIME_MILLIS": 1790985600000, "VERSION": "0.5.7-ShadowTheAge"}
    ]


def test_an_unwanted_tables_rows_would_have_been_refused() -> None:
    # What the gate saves: the two irrelevant tables the fixture carries are unreadable.
    with pytest.raises(NesqlError, match=r"5\.0E-1"):
        _fixture("MOB_INFO_DROPS")
    with pytest.raises(NesqlError, match="CACHED table"):
        _fixture("RECIPE")


def test_a_wanted_table_the_script_never_creates_is_left_out() -> None:
    assert set(_fixture("ITEM", "ASPECT")) == {"ITEM"}


def test_a_table_created_with_no_rows_is_an_empty_list() -> None:
    assert _script() == []


def test_rows_are_read_from_the_public_schema_only() -> None:
    # HSQLDB keeps its own tables in the same script (SYSTEM_LOBS.BLOCKS); a namesake there is
    # not the export's table.
    lines = [
        "CREATE MEMORY TABLE SYSTEM_LOBS.T(A VARCHAR(255),B INTEGER)",
        _TABLE,
        "SET SCHEMA SYSTEM_LOBS",
        "INSERT INTO T VALUES('lob',1)",
        "SET SCHEMA PUBLIC",
        "INSERT INTO T VALUES('public',2)",
    ]
    assert read_script(lines, ["T"]) == {"T": [{"A": "public", "B": 2}]}


def test_windows_line_endings_read_the_same() -> None:
    text = _FIXTURE.read_text(encoding="utf-8")
    crlf = text.replace("\n", "\r\n").splitlines(keepends=True)
    assert read_script(crlf, ["ITEM", "FLUID"]) == _fixture("ITEM", "FLUID")


def test_a_create_lines_constraints_and_nested_commas_are_not_columns() -> None:
    create = (
        "CREATE MEMORY TABLE PUBLIC.T(A DECIMAL(10,2) NOT NULL,\"Mixed\" VARCHAR(255) DEFAULT 'a,b',"
        "PRIMARY KEY(A),CONSTRAINT FK1 FOREIGN KEY(A) REFERENCES PUBLIC.U(ID),"
        "CONSTRAINT UK1 UNIQUE(A),UNIQUE(A),CHECK(A>0))"
    )
    assert _script("INSERT INTO T VALUES(1,'m')", create=create) == [{"A": 1, "Mixed": "m"}]


@pytest.mark.parametrize(
    ("literal", "text"),
    [
        ("'It''s'", "It's"),
        ("''", ""),
        ("''''", "'"),
        ("'Smile \\ud83d\\ude00'", "Smile \N{GRINNING FACE}"),
        ("'\\u00A7eUpper hex'", "\N{SECTION SIGN}eUpper hex"),
        ("'tab\\u0009here'", "tab\there"),
        ("'a\\u005cub'", "a\\ub"),
        ("'a\\b'", "a\\b"),
        ("'\\\\u00e9'", "\\\N{LATIN SMALL LETTER E WITH ACUTE}"),
        ("'\\'", "\\"),
        ("'\\ud83d!'", "\N{REPLACEMENT CHARACTER}!"),
        ("'\\ude00'", "\N{REPLACEMENT CHARACTER}"),
        ("'\\ud83d\\u0041'", "\N{REPLACEMENT CHARACTER}A"),
    ],
    ids=[
        "doubled-quote",
        "empty",
        "only-a-quote",
        "surrogate-pair",
        "upper-case-hex",
        "control-character",
        "escaped-backslash-before-u",
        "bare-backslash",
        "bare-backslash-before-an-escape",
        "trailing-backslash",
        "lone-high-surrogate",
        "lone-low-surrogate",
        "high-surrogate-before-a-non-surrogate",
    ],
)
def test_a_string_literal_reads_as_hsqldb_wrote_it(literal: str, text: str) -> None:
    assert _a(literal) == text


@pytest.mark.parametrize(
    ("literal", "value"),
    [("NULL", None), ("TRUE", True), ("FALSE", False), ("0", 0), ("-100", -100), ("32767", 32767)],
)
def test_an_unquoted_value_is_null_a_boolean_or_an_integer(literal: str, value: Value) -> None:
    read = _b(literal)
    assert read == value
    assert type(read) is type(value)


@given(st.text(st.one_of(st.characters(codec="utf-8"), st.sampled_from("\\u'0aF"))))
@example("\\u0041")
@example("\\\\u")
@example("it's \\")
def test_any_text_reads_back_from_the_literal_hsqldb_writes_for_it(text: str) -> None:
    assert _a(_hsqldb_literal(text)) == text


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (["CREATE CACHED TABLE PUBLIC.T(A INTEGER)"], "CACHED table"),
        (["CREATE TEXT TABLE PUBLIC.T(A INTEGER)"], "TEXT table"),
        (["CREATE TABLE PUBLIC.T(A INTEGER)"], "plain table"),
        (["CREATE MEMORY TABLE PUBLIC.T(A INTEGER"], "does not end its column list"),
        (["CREATE MEMORY TABLE PUBLIC.T(A VARCHAR(255)"], "unbalanced"),
        (["CREATE MEMORY TABLE PUBLIC.T(A VARCHAR(255) DEFAULT 'x)"], "unbalanced"),
        (["CREATE MEMORY TABLE PUBLIC.T(A INTEGER,A INTEGER)"], "cannot read the columns"),
        (["SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',1)", _TABLE], "before its CREATE"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',X'0a')"], "binary literal"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',5.0E-1)"], r"5\.0E-1"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES(,1)"], "the value ''"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x')"], "1 values, but .* 2"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',1,2)"], "3 values, but .* 2"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x,1)"], "never closed"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',1"], "ends before"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',"], "ends before"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x'y,1)"], "unexpected text"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('x',1) "], "unexpected text"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('\\uzz12',1)"], "four hex digits"),
        ([_TABLE, "SET SCHEMA PUBLIC", "INSERT INTO T VALUES('\\u00e',1)"], "four hex digits"),
    ],
)
def test_what_the_reader_does_not_understand_is_refused(lines: list[str], message: str) -> None:
    with pytest.raises(NesqlError, match=message):
        read_script(lines, ["T"])


def test_a_refusal_is_a_value_error_naming_its_line() -> None:
    with pytest.raises(ValueError, match="line 3: "):
        _script("INSERT INTO T VALUES('x')")


# --- names and keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "name", "unknown"),
    [
        ("Gravel", "Gravel", 0),
        ("  padded  ", "padded", 0),
        ("\N{SECTION SIGN}eEmpty Cell", "Empty Cell", 0),
        ("\N{SECTION SIGN}LIron\N{SECTION SIGN}r Dust", "Iron Dust", 0),
        ("Ends with a code\N{SECTION SIGN}", "Ends with a code", 0),
        (chr(0xE00A) + " Orb", "\N{HIGH VOLTAGE SIGN} Orb", 0),
        ("x" + chr(0xE012), "x\N{SUPERSCRIPT TWO}", 0),
        ("Mystery " + chr(0xF8FF), "Mystery " + chr(0xF8FF), 1),
        (chr(0xE0FF) + chr(0xE00B), chr(0xE0FF) + chr(0xE00B), 2),
    ],
)
def test_a_name_loses_its_codes_and_swaps_the_glyphs_it_knows(
    raw: str, name: str, unknown: int
) -> None:
    assert clean_name(raw) == (name, unknown)


def test_every_known_glyph_becomes_a_symbol_outside_the_private_use_area() -> None:
    assert len(_GLYPHS) == 30  # ShadowTheAge's table, entry for entry
    for glyph in _GLYPHS:
        symbol, unknown = clean_name(glyph)
        assert unknown == 0
        assert len(symbol) == 1
        assert not 0xE000 <= ord(symbol) <= 0xF8FF


@pytest.mark.parametrize(
    ("mod", "internal", "damage", "key"),
    [
        ("minecraft", "gravel", 0, "minecraft:gravel"),
        ("GregTech", "gt.metaitem.01", 2032, "gregtech:gt.metaitem.01@2032"),
        ("minecraft", "log", 32767, "minecraft:log@32767"),
        ("IC2", "itemCellEmpty", 0, "ic2:itemcellempty"),
    ],
)
def test_an_item_is_keyed_as_a_plan_spells_it(
    mod: str, internal: str, damage: int, key: str
) -> None:
    assert item_id(mod, internal, damage) == key


# --- the index --------------------------------------------------------------------------------


def _index(zip_names: Sequence[str] = _ZIP) -> tuple[dict[str, Any], IndexStats]:
    tables = _fixture("ITEM", "FLUID")
    return build_icon_index(
        tables["ITEM"],
        tables["FLUID"],
        zip_names,
        source=_SOURCE,
        generated_at="2026-10-02T00:00:00Z",
    )


def test_the_index_is_the_format_the_reader_takes() -> None:
    doc, _ = _index()
    # generated_at first, so roots.generated_at finds it in its window; every map sorted.
    assert list(doc) == ["generated_at", "schema", "source", "items", "fluids"]
    assert doc["generated_at"] == "2026-10-02T00:00:00Z"
    assert doc["schema"] == INDEX_SCHEMA
    assert doc["source"] == _SOURCE
    assert list(doc["items"]) == sorted(doc["items"])
    assert list(doc["fluids"]) == sorted(doc["fluids"])


def test_every_item_is_keyed_named_and_pictured_by_the_rules() -> None:
    doc, _ = _index()
    assert doc["items"] == {
        "dreamcraft:item.broken": {"name": "", "png": None},  # the exporter's "ERROR" stand-in
        "dreamcraft:item.slash": {"name": "Back\\slash \\upward", "png": None},
        "dreamcraft:item.smile": {"name": "Smile \N{GRINNING FACE}", "png": None},
        "etfuturum:dragon_breath": {"name": "Dragon's Breath", "png": None},
        "gregtech:gt.metaitem.01@2032": {
            "name": "Iron Dust",
            "png": "item/gregtech/gt.metaitem.01~2032.png",
        },
        # The NBT-free row, though its NBT variant comes first in the script.
        "gregtech:gt.metaitem.01@32405": {
            "name": "Small Battery Hull",
            "png": "item/gregtech/gt.metaitem.01~32405.png",
        },
        "gregtech:gt.metaitem.01@32597": {
            "name": "\N{HIGH VOLTAGE SIGN} Lapotronic Energy Orb",
            "png": "item/gregtech/gt.metaitem.01~32597.png",
        },
        "gregtech:gt.metaitem.01@32598": {"name": "Mystery Orb " + chr(0xF8FF), "png": None},
        # Only NBT variants: the lowest ID stands in.
        "gregtech:gt.metatool.01@16": {
            "name": "Steel Wrench",
            "png": "item/gregtech/gt.metatool.01~16~QmFy.png",
        },
        # The script's backslash path, written with / as the archive names it.
        "ic2:itemcellempty": {"name": "Empty Cell", "png": "item/IC2/itemCellEmpty~0.png"},
        "minecraft:gravel": {"name": "Gravel", "png": "item/minecraft/gravel~0.png"},
        "minecraft:log": {"name": "Oak Wood", "png": "item/minecraft/log~0.png"},
        "minecraft:log@1": {"name": "Spruce Wood", "png": None},
        "minecraft:wool": {"name": "White Wool", "png": "item/minecraft/wool~0.png"},
        "minecraft:wool@32767": {"name": "Any Wool", "png": "item/minecraft/wool~32767.png"},
    }


def test_every_fluid_is_keyed_by_its_registry_name() -> None:
    doc, _ = _index()
    assert doc["fluids"] == {
        "hydrogen": {"name": "Hydrogen Gas", "png": "fluid/gregtech/hydrogen.png"},
        "liquid_toluene": {"name": "Toluene", "png": "fluid/gregtech/liquid_toluene.png"},
        "nitrobenzene": {"name": "Nitrobenzene", "png": None},
    }


def test_the_stats_say_what_the_rows_came_to() -> None:
    _, stats = _index()
    assert stats == IndexStats(
        items=TableStats(rows=17, keys=15, nbt_only=1, without_image=6),
        fluids=TableStats(rows=3, keys=3, nbt_only=0, without_image=1),
        unknown_glyphs=1,
    )


def test_an_archive_named_with_backslashes_still_matches() -> None:
    doc, _ = _index([name.replace("/", "\\") for name in _ZIP])
    assert doc["items"]["minecraft:gravel"]["png"] == "item/minecraft/gravel~0.png"


def test_with_no_stamp_given_the_index_is_stamped_now() -> None:
    doc, _ = build_icon_index([], [], [], source={})
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["generated_at"])
    assert (doc["items"], doc["fluids"]) == ({}, {})


def _item(row_id: str, *, nbt: str | None = "", mod: str = "m", name: str = "N") -> Row:
    return {
        "ID": row_id,
        "MOD_ID": mod,
        "INTERNAL_NAME": "thing",
        "ITEM_DAMAGE": 0,
        "NBT": nbt,
        "LOCALIZED_NAME": name,
        "UNLOCALIZED_NAME": "item.thing",
        "IMAGE_FILE_PATH": f"item/{row_id}.png",
    }


def test_the_nbt_free_row_wins_whatever_its_id() -> None:
    rows = [_item("a", nbt="{x:1}", name="Variant"), _item("z", nbt=None, name="Plain")]
    doc, stats = build_icon_index(rows, [], [], source={})
    assert doc["items"]["m:thing"]["name"] == "Plain"
    assert stats.items.nbt_only == 0


def test_two_spellings_of_one_key_take_the_lowest_id() -> None:
    # Lowercasing could fold two mods' spellings together; the choice is then deterministic.
    rows = [_item("b", mod="M", name="Upper"), _item("a", mod="m", name="Lower")]
    doc, _ = build_icon_index(rows, [], [], source={})
    assert doc["items"] == {"m:thing": {"name": "Lower", "png": None}}


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("MOD_ID", None, "no text MOD_ID"),
        ("ITEM_DAMAGE", "0", "no integer ITEM_DAMAGE"),
        ("ITEM_DAMAGE", True, "no integer ITEM_DAMAGE"),
        ("NBT", 7, "NBT is not text"),
        ("ID", None, "no text ID"),
    ],
)
def test_a_row_without_what_the_join_needs_is_refused(
    column: str, value: Value, message: str
) -> None:
    row = _item("a") | {column: value}
    with pytest.raises(NesqlError, match=message):
        build_icon_index([row], [], [], source={})
