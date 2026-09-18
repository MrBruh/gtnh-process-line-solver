"""Classic (pre-1.12) NBT: the binary format a ``.schematic`` is written in.

Hand-rolled rather than taken from a library, for the reason the rest of the runtime is: the
solver's only install-time dependency is pydantic, and this is a closed, frozen format in maybe
200 lines. The reader exists for the tests as much as the writer: round-tripping the real files
in ``tests/golden/schematic/`` is how we know the writer emits something Minecraft would accept.

Big-endian throughout, gzip-wrapped on disk::

    gzip( tag_id:u1  name_len:u2  name:utf8  payload )

Python has one integer type and NBT has four, so the tag a value writes as cannot be inferred
from ``int`` alone. Values are therefore carried in thin subclasses (:class:`Byte`,
:class:`Short`, :class:`Int`, :class:`Long`) that behave exactly like ``int`` everywhere else,
and the reader hands back the same types so a parse-write-parse round trip is type-stable. A
bare ``int`` is rejected by the writer rather than guessed at: guessing is how a ``short``
silently becomes an ``int`` and shifts every following byte.
"""

from __future__ import annotations

import gzip
import struct
from typing import Any, Final

# Tag ids, in the order the format numbers them.
TAG_END: Final = 0
TAG_BYTE: Final = 1
TAG_SHORT: Final = 2
TAG_INT: Final = 3
TAG_LONG: Final = 4
TAG_FLOAT: Final = 5
TAG_DOUBLE: Final = 6
TAG_BYTE_ARRAY: Final = 7
TAG_STRING: Final = 8
TAG_LIST: Final = 9
TAG_COMPOUND: Final = 10
TAG_INT_ARRAY: Final = 11


class Byte(int):
    """TAG_Byte. Signed, -128..127."""

    __slots__ = ()


class Short(int):
    """TAG_Short. Signed, -32768..32767."""

    __slots__ = ()


class Int(int):
    """TAG_Int. Signed 32-bit."""

    __slots__ = ()


class Long(int):
    """TAG_Long. Signed 64-bit."""

    __slots__ = ()


class Float(float):
    """TAG_Float. 32-bit."""

    __slots__ = ()


class Double(float):
    """TAG_Double. 64-bit."""

    __slots__ = ()


class String(str):
    """TAG_String. Length-prefixed modified UTF-8 (plain UTF-8 here; no surrogate pairs in play)."""

    __slots__ = ()


class ByteArray(bytes):
    """TAG_Byte_Array. What ``Blocks``, ``Data`` and ``AddBlocks`` are."""

    __slots__ = ()


class IntArray(list):  # type: ignore[type-arg]
    """TAG_Int_Array."""

    __slots__ = ()


class Compound(dict):  # type: ignore[type-arg]
    """TAG_Compound: a name -> value map, terminated by TAG_End on the wire."""

    __slots__ = ()


class List(list):  # type: ignore[type-arg]
    """TAG_List: homogeneous, and its element tag is on the wire even when it is empty.

    An empty list therefore has to carry the type it *would* hold, which is why this cannot just
    be a plain ``list``: ``Entities`` is empty in every file we write, and a reader is entitled to
    care what it is empty *of*.
    """

    __slots__ = ("element_type",)

    def __init__(self, element_type: int, items: Any = ()) -> None:
        super().__init__(items)
        self.element_type = element_type


class NBTError(ValueError):
    """A malformed or unsupported NBT payload."""


def _tag_of(value: object) -> int:
    """The tag id ``value`` writes as. Order matters: every wrapper precedes its base type."""
    if isinstance(value, Byte):
        return TAG_BYTE
    if isinstance(value, Short):
        return TAG_SHORT
    if isinstance(value, Int):
        return TAG_INT
    if isinstance(value, Long):
        return TAG_LONG
    if isinstance(value, Float):
        return TAG_FLOAT
    if isinstance(value, Double):
        return TAG_DOUBLE
    if isinstance(value, ByteArray):
        return TAG_BYTE_ARRAY
    if isinstance(value, String):
        return TAG_STRING
    if isinstance(value, IntArray):
        return TAG_INT_ARRAY
    if isinstance(value, List):
        return TAG_LIST
    if isinstance(value, Compound):
        return TAG_COMPOUND
    raise NBTError(
        f"{type(value).__name__} has no NBT tag; wrap it (Byte/Short/Int/Long/String/...) rather "
        "than relying on a guess - an int written at the wrong width shifts every later byte"
    )


# --------------------------------------------------------------------------------------- writing


def _write_payload(out: bytearray, value: object) -> None:
    tag = _tag_of(value)
    if tag == TAG_BYTE:
        out += struct.pack(">b", value)
    elif tag == TAG_SHORT:
        out += struct.pack(">h", value)
    elif tag == TAG_INT:
        out += struct.pack(">i", value)
    elif tag == TAG_LONG:
        out += struct.pack(">q", value)
    elif tag == TAG_FLOAT:
        out += struct.pack(">f", value)
    elif tag == TAG_DOUBLE:
        out += struct.pack(">d", value)
    elif tag == TAG_BYTE_ARRAY:
        assert isinstance(value, ByteArray)
        out += struct.pack(">i", len(value)) + value
    elif tag == TAG_STRING:
        assert isinstance(value, String)
        encoded = value.encode("utf-8")
        out += struct.pack(">H", len(encoded)) + encoded
    elif tag == TAG_INT_ARRAY:
        assert isinstance(value, IntArray)
        out += struct.pack(">i", len(value))
        for item in value:
            out += struct.pack(">i", item)
    elif tag == TAG_LIST:
        assert isinstance(value, List)
        out += struct.pack(">Bi", value.element_type, len(value))
        for item in value:
            _write_payload(out, item)
    elif tag == TAG_COMPOUND:
        assert isinstance(value, Compound)
        for key, item in value.items():
            _write_named(out, key, item)
        out += struct.pack(">B", TAG_END)


def _write_named(out: bytearray, name: str, value: object) -> None:
    encoded = name.encode("utf-8")
    out += struct.pack(">B", _tag_of(value)) + struct.pack(">H", len(encoded)) + encoded
    _write_payload(out, value)


def dumps(name: str, root: Compound, *, compress: bool = True) -> bytes:
    """Serialize ``root`` as the named root tag. Gzipped unless ``compress`` is off.

    ``mtime=0`` keeps the bytes a pure function of the input: a schematic regenerated from the
    same layout is byte-identical, which is what lets a caller diff two runs.
    """
    out = bytearray()
    _write_named(out, name, root)
    if not compress:
        return bytes(out)
    return gzip.compress(bytes(out), mtime=0)


# --------------------------------------------------------------------------------------- reading


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise NBTError(f"truncated payload: wanted {n} bytes at {self.pos}")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def unpack(self, fmt: str, size: int) -> Any:
        return struct.unpack(fmt, self.take(size))[0]

    def string(self) -> String:
        return String(self.take(self.unpack(">H", 2)).decode("utf-8"))

    def payload(self, tag: int) -> Any:
        if tag == TAG_BYTE:
            return Byte(self.unpack(">b", 1))
        if tag == TAG_SHORT:
            return Short(self.unpack(">h", 2))
        if tag == TAG_INT:
            return Int(self.unpack(">i", 4))
        if tag == TAG_LONG:
            return Long(self.unpack(">q", 8))
        if tag == TAG_FLOAT:
            return Float(self.unpack(">f", 4))
        if tag == TAG_DOUBLE:
            return Double(self.unpack(">d", 8))
        if tag == TAG_BYTE_ARRAY:
            return ByteArray(self.take(self.unpack(">i", 4)))
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_LIST:
            element = int(self.unpack(">B", 1))
            count = int(self.unpack(">i", 4))
            return List(element, [self.payload(element) for _ in range(count)])
        if tag == TAG_COMPOUND:
            out = Compound()
            while True:
                child = int(self.unpack(">B", 1))
                if child == TAG_END:
                    return out
                # Name first, into a local. `out[self.string()] = self.payload(child)` reads them
                # in the wrong ORDER: Python evaluates an assignment's right-hand side before the
                # subscript, so the payload would be parsed from the bytes holding the name.
                key = str(self.string())
                out[key] = self.payload(child)
        if tag == TAG_INT_ARRAY:
            count = int(self.unpack(">i", 4))
            return IntArray(int(self.unpack(">i", 4)) for _ in range(count))
        raise NBTError(f"unknown tag id {tag} at offset {self.pos}")


def loads(raw: bytes) -> tuple[str, Compound]:
    """Parse a (optionally gzipped) NBT payload into ``(root name, root compound)``."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    reader = _Reader(raw)
    tag = int(reader.unpack(">B", 1))
    if tag != TAG_COMPOUND:
        raise NBTError(f"root tag is {tag}, expected a compound ({TAG_COMPOUND})")
    name = str(reader.string())
    root = reader.payload(TAG_COMPOUND)
    assert isinstance(root, Compound)
    return name, root
