"""Derive the ME render data the previewer draws AE2 cables, parts and blocks from (#337).

Drawing an ME network (#338) needs AE2's geometry and art names: how big a cable's core is, how far
an arm reaches, which boxes an import bus is built from, which icon a grey smart cable wears. They
live in Java render methods and nowhere as data. This reads them out of the two source clones pinned
to the pack (``gtnh_solver.dataset.mod_jars``: AE2 ``rv3-beta-1050-GTNH``, AE2FluidCraft
``1.5.106-gtnh``) and writes the committed ``data/ae2/<AE2 version>/render.json``. **Numbers and icon
names only**: every PNG stays in its mod jar and is fetched at preview time, like GT's
(``previewer/jar.py``).

How a number is read::

    a clone's src/main/java
      |  find the method that draws it, resolved up the class chain as Java dispatch would
      |  (a fluid bus inherits its boxes from AE2's bus, a glass cable its arm from PartCable)
      v
    its setBounds/addBox calls, evaluated: a direction switch is read at its DOWN case and its other
      |  five cases are checked to be that box turned to their side; a straight run is read along Y
      |  and its X and Z runs checked the same way
      v
    the icon each box wears: the last texture call before it, getTexture() resolved for the cable
      |  kind, an enum constant through its enum's registered name
      v
    every icon checked to exist as a PNG in the clone's resources, every number cited as
      file:line, the whole document validated by the loader that will read it (dataset.ae_render)

**What is transcribed rather than parsed** is the plumbing between methods: which connection method a
cable kind calls toward which neighbour, which bare setBounds is the glass core, the controller's
render states. Each is a table below, and the run asserts the source still says it, so a pack bump
that changes one stops the run instead of writing stale data. ``docs/spikes/329-me-ae2.md`` section 8
explains the numbers, and ``tests/test_dataset_ae_render.py`` pins its figures against the output.

Usage (from the repo root, in the dev venv)::

    python tools/derive_ae_render.py [REFERENCE_DIR]

``REFERENCE_DIR`` holds the clones as ``<artifact>-<version>/`` (``Applied-Energistics-2-Unofficial-
rv3-beta-1050-GTNH``, ``AE2FluidCraft-Rework-1.5.106-gtnh``), each a git checkout at that tag. It
defaults to ``$GTNH_REFERENCE``, else the ``gtnh-reference`` folder beside this checkout.
``tests/test_derive_ae_render.py`` re-derives when ``GTNH_REFERENCE`` is set and compares the result
with the committed file.
"""

from __future__ import annotations

import ast
import json
import operator
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gtnh_solver.dataset.ae_render import AE_RENDER_PATH, AE_RENDER_SCHEMA, AERender
from gtnh_solver.dataset.mod_jars import AE2, AE2FC, ME_PACK_VERSION, JarSpec
from gtnh_solver.ir.me import AEColor, MECableKind, MEDeviceKind

REPO = Path(__file__).resolve().parents[1]

#: Where the clones live when neither an argument nor the environment says.
REFERENCE_ENV = "GTNH_REFERENCE"
DEFAULT_REFERENCE = REPO.parent / "gtnh-reference"


class DeriveError(SystemExit):
    """The source no longer says what this tool expects: re-read it, then update the tool."""


# --- what is transcribed (each asserted against the source when it is used) -----------------------

#: The part class behind each cable kind (``appeng/items/parts/PartType.java``).
CABLE_CLASSES: dict[MECableKind, str] = {
    MECableKind.GLASS: "PartCableGlass",
    MECableKind.COVERED: "PartCableCovered",
    MECableKind.SMART: "PartCableSmart",
    MECableKind.DENSE: "PartDenseCable",
    MECableKind.DENSE_COVERED: "PartDenseCableCovered",
}

#: The method each kind's renderStatic draws a connection with, by what the neighbour reports:
#: ``dense`` is DENSE or DENSE_COVERED (``isDense``), ``smart`` is SMART (``isSmart``), ``other`` is
#: anything else. Only the dense kinds tell neighbours apart (spike 8.1).
CONNECTIONS: dict[MECableKind, dict[str, str]] = {
    MECableKind.GLASS: {"other": "renderGlassConnection"},
    MECableKind.COVERED: {"other": "renderCoveredConnection"},
    MECableKind.SMART: {"other": "renderSmartConnection"},
    MECableKind.DENSE: {
        "dense": "renderDenseConnection",
        "smart": "renderSmartConnection",
        "other": "renderCoveredConnection",
    },
    MECableKind.DENSE_COVERED: {
        "dense": "renderDenseCoveredConnection",
        "other": "renderCoveredConnection",
    },
}

#: Which of renderStatic's bare (not per-direction) setBounds calls is the core, and how many there
#: are. Glass draws a 5..11 core first, in the covered texture, when a part on its bus reports a
#: covered or smart connection type; no part this solver places does (they all report GLASS,
#: ``AEBasePart.getCableConnectionType``), so its own 6..10 core is the one recorded.
CORE_CALL: dict[MECableKind, tuple[int, int]] = {
    MECableKind.GLASS: (1, 2),
    MECableKind.COVERED: (0, 1),
    MECableKind.SMART: (0, 1),
    MECableKind.DENSE: (0, 1),
    MECableKind.DENSE_COVERED: (0, 1),
}

#: The kinds a part can sit on, which therefore draw an arm out to it. A dense cable takes no part
#: (``BusSupport.DENSE_CABLE``), so its renderStatic has no such loop.
PART_ARM_KINDS = frozenset({MECableKind.GLASS, MECableKind.COVERED, MECableKind.SMART})

#: The part class behind each ME device that is an AE2 or AE2FluidCraft part (spike 8.2). GT's own
#: ME hatches are GT blocks, drawn from GT's manifest like any other hatch.
PART_CLASSES: dict[MEDeviceKind, str] = {
    MEDeviceKind.IMPORT_BUS: "PartImportBus",
    MEDeviceKind.EXPORT_BUS: "PartExportBus",
    MEDeviceKind.STORAGE_BUS: "PartStorageBus",
    MEDeviceKind.INTERFACE: "PartInterface",
    MEDeviceKind.FLUID_IMPORT_BUS: "PartFluidImportBus",
    MEDeviceKind.FLUID_EXPORT_BUS: "PartFluidExportBus",
    MEDeviceKind.FLUID_STORAGE_BUS: "PartFluidStorageBus",
    MEDeviceKind.DUAL_INTERFACE: "PartFluidInterface",
}

#: The AE2 block devices an ME network is built with, by the name the render data keys them by.
BLOCK_CLASSES: dict[str, str] = {
    "controller": "BlockController",
    "interface": "BlockInterface",
    "drive": "BlockDrive",
    "energy_acceptor": "BlockEnergyAcceptor",
}

#: The controller's looks (``RenderBlockController.renderInWorld``), for a Fluix controller, which is
#: what an export writes (``paintedColor`` 16, spike 7.4): the texture id it passes to
#: ``BlockController.getRenderTexture`` (``None`` keeps the block's own faces) and the lights it draws
#: on top. A column is a controller between two others on one axis; an inside one, on two or more
#: axes, alternates A and B by the parity of ``|x| + |y| + |z|``. The lights are the original
#: textures: AE2's ``controllerAnimation`` setting may draw generated ones over them in game.
CONTROLLER_STATES: dict[str, tuple[int | None, str | None]] = {
    "off": (None, None),
    "powered": (0, "BlockControllerLights"),
    "conflict": (0, "BlockControllerConflict"),
    "column_off": (2, None),
    "column_powered": (1, "BlockControllerColumnLights"),
    "column_conflict": (1, "BlockControllerColumnConflict"),
    "inside_a": (3, None),
    "inside_b": (4, None),
}

#: AE2's ``AECableType`` names, in the cable-kind vocabulary (``NONE`` has no kind).
_CABLE_TYPES = {
    "GLASS": MECableKind.GLASS,
    "COVERED": MECableKind.COVERED,
    "SMART": MECableKind.SMART,
    "DENSE": MECableKind.DENSE,
    "DENSE_COVERED": MECableKind.DENSE_COVERED,
}

#: The ``get<Family>Texture`` methods, by the cable kind whose icon set each one returns.
_FAMILIES = {
    "Glass": MECableKind.GLASS,
    "Covered": MECableKind.COVERED,
    "Smart": MECableKind.SMART,
    "Dense": MECableKind.DENSE,
    "DenseCovered": MECableKind.DENSE_COVERED,
}

_SIDES = ("DOWN", "UP", "NORTH", "SOUTH", "WEST", "EAST")

#: A part item's icon is ``ItemPart.<PartType constant>`` (``NameResolver``, ``ItemMultiPart``).
_ITEM_PART_PREFIX = "ItemPart."


# --- reading Java ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clone:
    """One pinned source checkout: the jar it is the source of, and where it sits on disk."""

    spec: JarSpec
    root: Path

    @property
    def java(self) -> Path:
        return self.root / "src" / "main" / "java"

    def has_icon(self, name: str) -> bool:
        """Whether ``assets/<modid>/textures/blocks/<name>.png`` exists in the clone's resources."""
        blocks = self.root / "src" / "main" / "resources" / "assets" / self.spec.modid
        return (blocks / "textures" / "blocks" / f"{name}.png").is_file()


@dataclass(frozen=True)
class Source:
    """One Java file, and the path the output cites it by (relative to ``src/main/java``)."""

    cite: str
    text: str
    clone: Clone

    def line(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1

    def at(self, offset: int, end: int | None = None) -> str:
        """``path:line``, or ``path:first-last`` when ``end`` lands on a later line."""
        first = self.line(offset)
        last = first if end is None else self.line(end)
        return f"{self.cite}:{first}" if last == first else f"{self.cite}:{first}-{last}"

    @property
    def qualified(self) -> str:
        """The class's fully qualified name, from its path."""
        return self.cite.removesuffix(".java").replace("/", ".")


@dataclass(frozen=True)
class Span:
    """A brace-delimited body: ``start`` is its ``{``, ``end`` its matching ``}``."""

    src: Source
    start: int
    end: int

    def finditer(self, pattern: re.Pattern[str]) -> Iterator[re.Match[str]]:
        return pattern.finditer(self.src.text, self.start, self.end + 1)

    def search(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        return pattern.search(self.src.text, self.start, self.end + 1)

    def at(self) -> str:
        return self.src.at(self.start, self.end)


def _skip(text: str, i: int) -> int:
    """The index past a comment, string or char literal starting at ``i``, else ``i``."""
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        return text.index("*/", i) + 2
    if text[i] in "\"'":
        quote, j = text[i], i + 1
        while text[j] != quote:
            j += 2 if text[j] == "\\" else 1
        return j + 1
    return i


def _match_brace(text: str, open_index: int) -> int:
    """The index of the ``}`` matching the ``{`` at ``open_index``."""
    depth, i = 0, open_index
    while i < len(text):
        skipped = _skip(text, i)
        if skipped != i:
            i = skipped
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise DeriveError(f"unbalanced braces from offset {open_index}")


def _innermost_block(span: Span, offset: int) -> tuple[int, int]:
    """The innermost ``{...}`` inside ``span`` that contains ``offset``."""
    text, stack, i = span.src.text, [], span.start
    while i < offset:
        skipped = _skip(text, i)
        if skipped != i:
            i = skipped
            continue
        if text[i] == "{":
            stack.append(i)
        elif text[i] == "}":
            stack.pop()
        i += 1
    return stack[-1], _match_brace(text, stack[-1])


class Classes:
    """Every class of the clones, by simple name, with Java's method lookup up the chain."""

    def __init__(self, clones: Sequence[Clone]) -> None:
        self._files: dict[str, list[tuple[Clone, Path]]] = {}
        for clone in clones:
            for path in sorted(clone.java.rglob("*.java")):
                self._files.setdefault(path.stem, []).append((clone, path))
        self._sources: dict[str, Source] = {}

    def source(self, name: str) -> Source:
        if name not in self._sources:
            found = self._files.get(name, [])
            if len(found) != 1:
                raise DeriveError(f"expected one class {name} in the clones, found {len(found)}")
            clone, path = found[0]
            cite = path.relative_to(clone.java).as_posix()
            self._sources[name] = Source(cite, path.read_text(encoding="utf-8"), clone)
        return self._sources[name]

    def names_in(self, spec: JarSpec) -> list[str]:
        """Every class name the clone of ``spec`` declares, sorted."""
        return sorted(
            name for name, found in self._files.items() if any(c.spec == spec for c, _ in found)
        )

    def parent(self, name: str) -> str | None:
        """The class ``name`` extends, if it is one of the clones' classes."""
        found = re.search(
            rf"\bclass\s+{name}\b(?:<.*?>)?\s+extends\s+(\w+)", self.source(name).text, re.S
        )
        return found.group(1) if found and found.group(1) in self._files else None

    def chain(self, name: str) -> Iterator[str]:
        current: str | None = name
        while current is not None:
            yield current
            current = self.parent(current)

    def own_method(self, name: str, method: str, params: str | None = None) -> Span | None:
        """``method`` as declared in ``name`` itself, else ``None``. ``params`` picks an overload by
        its parameter list (``final`` and spacing ignored); without it, two overloads are an error."""
        src = self.source(name)
        declaration = re.compile(
            rf"^[ \t]*(?:(?:public|protected|private|static|final|abstract|synchronized)\s+)*"
            rf"[\w<>\[\]?,. ]+?\s+{method}\s*\(([^)]*)\)\s*(?:throws\s+[\w., ]+?\s*)?\{{",
            re.M,
        )
        hits = [
            hit
            for hit in declaration.finditer(src.text)
            if params is None or _parameters(hit.group(1)) == params
        ]
        if len(hits) > 1:
            raise DeriveError(f"{src.cite} declares {method}({params or '...'}) more than once")
        if not hits:
            return None
        start = hits[0].end() - 1
        return Span(src, start, _match_brace(src.text, start))

    def method(self, name: str, method: str, params: str | None = None) -> Span:
        """``method`` as an instance of ``name`` runs it: its own, else the nearest ancestor's."""
        for cls in self.chain(name):
            found = self.own_method(cls, method, params)
            if found is not None:
                return found
        raise DeriveError(f"{name} has no {method} anywhere up its class chain")


def _parameters(declared: str) -> str:
    """A parameter list as ``own_method`` compares it: no ``final``, single spaces."""
    return " ".join(declared.replace("final ", "").split())


def _require(span: Span, pattern: str, what: str) -> re.Match[str]:
    """The first match of ``pattern`` in ``span``, or stop: the transcription no longer holds."""
    found = span.search(re.compile(pattern))
    if found is None:
        raise DeriveError(f"{span.at()} no longer says {what} (looked for /{pattern}/)")
    return found


# --- numbers and boxes ----------------------------------------------------------------------------

Box = tuple[int, int, int, int, int, int]

_JAVA_SUFFIX = re.compile(r"(?<![\w.])(\d+(?:\.\d*)?)[fFdD]\b")
_OPERATORS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _evaluate(node: ast.expr, env: Mapping[str, float]) -> float:
    match node:
        case ast.Constant(value=bool()):
            pass
        case ast.Constant(value=int() | float() as value):
            return float(value)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _OPERATORS:
            return float(_OPERATORS[type(op)](_evaluate(left, env), _evaluate(right, env)))
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_evaluate(operand, env)
        case ast.Name(id=name) if name in env:
            return env[name]
    raise DeriveError(f"not an expression this tool evaluates: {ast.unparse(node)}")


def _split_args(args: str) -> list[str]:
    """``args`` split on its top-level commas."""
    parts, depth, current = [], 0, []
    for char in args:
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        depth += {"(": 1, ")": -1}.get(char, 0)
        current.append(char)
    parts.append("".join(current).strip())
    return parts


def _box(args: str, env: Mapping[str, float] | None = None, scale: float = 1.0) -> Box:
    """A six-number bounds call's arguments as a box in whole sixteenths."""
    values = [
        _evaluate(ast.parse(_JAVA_SUFFIX.sub(r"\1", arg), mode="eval").body, env or {}) * scale
        for arg in _split_args(args)
    ]
    if len(values) != 6 or any(value != round(value) for value in values):
        raise DeriveError(f"({args}) is not six whole sixteenths")
    x0, y0, z0, x1, y1, z1 = (round(value) for value in values)
    return (x0, y0, z0, x1, y1, z1)


def toward(side: str, box: Box) -> Box:
    """``box``, given toward DOWN (y = 0 is the connected face), turned to face ``side``.

    The mapping AE2's own six cases follow, which :func:`_switch_box` checks on every switch it reads.
    """
    x0, y0, z0, x1, y1, z1 = box
    turned: dict[str, Box] = {
        "DOWN": box,
        "UP": (x0, 16 - y1, z0, x1, 16 - y0, z1),
        "NORTH": (x0, z0, y0, x1, z1, y1),
        "SOUTH": (x0, z0, 16 - y1, x1, z1, 16 - y0),
        "WEST": (y0, x0, z0, y1, x1, z1),
        "EAST": (16 - y1, x0, z0, 16 - y0, x1, z1),
    }
    return turned[side]


@dataclass(frozen=True)
class DirectionSwitch:
    """A ``switch`` with one bounds call per side: the per-direction form AE2 draws arms in."""

    at: int
    end: int
    cases: dict[str, tuple[int, str]]


_SWITCH = re.compile(r"\bswitch\s*\(\s*\w+\s*\)\s*\{")
_CASE_BOX = re.compile(
    r"\bcase\s+(DOWN|UP|NORTH|SOUTH|WEST|EAST)\s*->\s*(?:rh|bch)\.(?:setBounds|addBox)\(([^;]*?)\);"
)
_BOX_CALL = re.compile(r"\b(?:rh|bch)\.(?:setBounds|addBox)\(([^;]*?)\);")
_RENDER_BOUNDS = re.compile(r"\brenderer\.setRenderBounds\(([^;]*?)\);")
_TEXTURE_CALL = re.compile(r"(?:\brh\.setTexture\(|\bIIcon\s+def\s*=\s*)this\.get(\w*)Texture\(")


def _direction_switches(span: Span) -> list[DirectionSwitch]:
    found = []
    for match in span.finditer(_SWITCH):
        open_brace = match.end() - 1
        close = _match_brace(span.src.text, open_brace)
        cases = {
            case.group(1): (case.start(), case.group(2))
            for case in _CASE_BOX.finditer(span.src.text, open_brace, close)
        }
        if set(cases) == set(_SIDES):
            found.append(DirectionSwitch(match.start(), close, cases))
    return found


def _switch_box(switch: DirectionSwitch, env: Mapping[str, float] | None = None) -> Box:
    """The switch's DOWN box, after checking the other five are it turned to their sides."""
    down = _box(switch.cases["DOWN"][1], env)
    for side in _SIDES:
        if _box(switch.cases[side][1], env) != toward(side, down):
            raise DeriveError(f"the {side} case is not the DOWN box turned to {side}")
    return down


def _bare_calls(span: Span, switches: Sequence[DirectionSwitch]) -> list[re.Match[str]]:
    """The bounds calls in ``span`` that sit in none of ``switches``."""
    return [
        call
        for call in span.finditer(_BOX_CALL)
        if not any(switch.at <= call.start() <= switch.end for switch in switches)
    ]


def _lights(span: Span, inside: int, after: int) -> bool:
    """Whether channel lights are drawn over a box: AE2 tints them with a colour's
    ``blackVariant``, after the box and within the block that holds it."""
    _, block_end = _innermost_block(span, inside)
    return "blackVariant" in span.src.text[after:block_end]


# --- the output, piece by piece -------------------------------------------------------------------


class Deriver:
    """Reads every piece of the render data out of the clones; :meth:`document` assembles them."""

    def __init__(self, clones: Mapping[str, Clone]) -> None:
        self.clones = clones
        self.classes = Classes(list(clones.values()))
        self.cable_textures = self._enum_names("CableBusTextures")
        self.block_textures = self._enum_names("ExtraBlockTextures")
        self.fc_textures = self._enum_names("FCPartsTexture")

    # icons

    def icon(self, modid: str, name: str) -> str:
        """``modid:name``, after checking the PNG it names is in that mod's resources."""
        if not self.clones[modid].has_icon(name):
            raise DeriveError(f"assets/{modid}/textures/blocks/{name}.png is not in the clone")
        return f"{modid}:{name}"

    def _enum_names(self, enum: str) -> dict[str, tuple[str, str]]:
        """``{constant: (registered name, cite)}`` for a texture enum of ``Constant("name")``."""
        src = self.classes.source(enum)
        return {
            m.group(1): (m.group(2), src.at(m.start(1)))
            for m in re.finditer(r'^\s+(\w+)\("([^"]+)"\)[,;]', src.text, re.M)
        }

    def _texture_constant(self, expression: str) -> tuple[str, str] | None:
        """The icon a ``CableBusTextures.X.getIcon()``-style expression names, and where that name
        is registered, else ``None``."""
        found = re.fullmatch(r"(CableBusTextures|FCPartsTexture)\.(\w+)\.getIcon\(\)", expression)
        if found is None:
            return None
        modid, names = (
            (AE2.modid, self.cable_textures)
            if found.group(1) == "CableBusTextures"
            else (AE2FC.modid, self.fc_textures)
        )
        name, cite = names[found.group(2)]
        return self.icon(modid, name), cite

    def part_type(self, part_class: str) -> tuple[str, str]:
        """The ``PartType`` constant a part class is registered as, and where."""
        src = self.classes.source("PartType")
        uses = list(re.finditer(rf"\b{part_class}\.class\b", src.text))
        if len(uses) != 1:
            raise DeriveError(f"PartType names {part_class} {len(uses)} times, not once")
        constants = list(re.finditer(r"^    (\w+)\(", src.text[: uses[0].start()], re.M))
        return constants[-1].group(1), src.at(constants[-1].start(1))

    def item_icon(self, part_class: str) -> tuple[str, str]:
        """The icon of the item a part is placed from (``this.getItemStack().getIconIndex()``)."""
        if self.classes.source(part_class).clone.spec == AE2:
            constant, cite = self.part_type(part_class)
            return self.icon(AE2.modid, _ITEM_PART_PREFIX + constant), cite
        # An AE2FluidCraft part is one item class each, which names the part it creates and the
        # icon it registers (getIconString, NameConst.RES_KEY being "ae2fc:").
        makers = [
            name
            for name in self.classes.names_in(AE2FC)
            if name.startswith("ItemPart")
            and f"new {part_class}(" in self.classes.source(name).text
        ]
        if len(makers) != 1:
            raise DeriveError(f"expected one AE2FluidCraft item to create {part_class}: {makers}")
        span = self.classes.method(makers[0], "getIconString")
        found = _require(span, r'return NameConst\.RES_KEY \+ "(\w+)";', "its icon name")
        return self.icon(AE2FC.modid, found.group(1)), span.src.at(found.start())

    def resolve_icon(self, part_class: str, expression: str) -> tuple[str, str]:
        """The icon a part's texture argument names, resolved for an instance of ``part_class``."""
        expression = expression.strip()
        constant = self._texture_constant(expression)
        if constant is not None:
            return constant
        if expression == "this.getItemStack().getIconIndex()":
            return self.item_icon(part_class)
        if expression == "this.getFaceIcon()":
            span = self.classes.method(part_class, "getFaceIcon")
            found = _require(span, r"return\s+(.+?);", "a single returned icon")
            return self.resolve_icon(part_class, found.group(1))
        raise DeriveError(f"{part_class}: no rule reads the texture {expression}")

    # colours

    def colours(self) -> dict[str, Any]:
        src = self.classes.source("AEColor")
        constant = re.compile(
            r'^\s+(\w+)\("gui\.appliedenergistics2\.(\w+)",\s*0x(\w+),\s*0x(\w+),\s*0x(\w+),\s*0x(\w+)\)',
            re.M,
        )
        found = list(constant.finditer(src.text))
        if len(found) != len(AEColor):
            raise DeriveError(f"AEColor declares {len(found)} colours, not {len(AEColor)}")
        out: dict[str, Any] = {}
        for colour, match in zip(AEColor, found, strict=True):
            if _snake(match.group(2)) != colour.value:
                raise DeriveError(f"AEColor {match.group(1)} is not {colour.value}")
            black, medium, white = (_rgb(match.group(i)) for i in (3, 4, 5))
            out[colour.value] = {
                "ae_name": match.group(1),
                "ordinal": colour.ordinal,
                "black_variant": black,
                "medium_variant": medium,
                "white_variant": white,
                "source": src.at(match.start(1)),
            }
        return out

    def cable_icons(self) -> dict[str, Any]:
        """Every cable kind's icon set, by colour: the ``get<Family>Texture`` switches (spike 8.3)."""
        out: dict[str, Any] = {}
        ae_names = {colour.value: match for colour, match in self._ae_colour_names().items()}
        for family_name, family in _FAMILIES.items():
            span = self.classes.method(CABLE_CLASSES[family], f"get{family_name}Texture")
            cases = {
                m.group(1): self.icon(AE2.modid, self.cable_textures[m.group(2)][0])
                for m in span.finditer(
                    re.compile(r"case (\w+) -> CableBusTextures\.(\w+)\.getIcon")
                )
            }
            default = _require(span, r"default ->", "a default (Fluix) branch")
            fluix_text = span.src.text[default.end() : span.end]
            named = re.search(r"parts\(\)\.cable(\w+)\(\)", fluix_text)
            if named is not None:
                fluix = _ITEM_PART_PREFIX + self.part_type(f"PartCable{named.group(1)}")[0]
            elif "this.getItemStack().getIconIndex()" in fluix_text:
                fluix = _ITEM_PART_PREFIX + self.part_type(CABLE_CLASSES[family])[0]
            else:
                raise DeriveError(f"{span.at()}: no rule reads the Fluix icon")
            by_colour = {}
            for colour in AEColor:
                if colour is AEColor.FLUIX:
                    by_colour[colour.value] = self.icon(AE2.modid, fluix)
                elif ae_names[colour.value] in cases:
                    by_colour[colour.value] = cases[ae_names[colour.value]]
                else:
                    raise DeriveError(f"{span.at()} has no case for {ae_names[colour.value]}")
            out[family.value] = {"by_colour": by_colour, "source": span.at()}
        return out

    def _ae_colour_names(self) -> dict[AEColor, str]:
        return {AEColor(value): entry["ae_name"] for value, entry in self.colours().items()}

    def channel_lights(self) -> dict[str, Any]:
        """The channel-light icons by count, and the counts a cable shows (spike 8.3)."""
        span = self.classes.method("PartCable", "getChannelTex")
        _require(span, r"if \(!this\.powered\) \{\s*i = 0;", "that an unpowered cable shows 0")
        branch = _require(span, r"if \(b\) \{", "the second-pass branch first")
        switches = list(span.finditer(re.compile(r"return switch \(i\) \{")))
        if len(switches) != 2 or switches[0].start() < branch.start():
            raise DeriveError(f"{span.at()}: expected the second pass's switch, then the first's")
        passes = []
        for switch in reversed(switches):  # source order is (second, first): read first, second
            body = span.src.text[switch.end() : _match_brace(span.src.text, switch.end() - 1)]
            table: dict[int, str] = {}
            for case in re.finditer(r"case ([\d, ]+) -> CableBusTextures\.(\w+);", body):
                for count in case.group(1).split(","):
                    table[int(count)] = self.cable_textures[case.group(2)][0]
            fallback = re.search(r"default -> CableBusTextures\.(\w+);", body)
            if fallback is None:
                raise DeriveError(f"{span.at()}: a light switch has no default")
            passes.append((table, self.cable_textures[fallback.group(1)][0]))
        stream = self.classes.method("PartCable", "writeToStream")
        dense = _require(
            stream, r"Math\.min\(usedChannels, (\d+)\) / (\d+)\)", "the dense count divisor"
        )
        plain = _require(stream, r"Math\.min\(usedChannels, (\d+)\) <<", "the shown-count cap")
        max_count = int(plain.group(1))
        by_count = {
            str(count): [
                self.icon(AE2.modid, table.get(count, fallback)) for table, fallback in passes
            ]
            for count in range(max_count + 1)
        }
        smart = self.classes.method("PartCable", "renderSmartConnection")
        black = smart.src.text.find("blackVariant", smart.start)
        white = smart.src.text.find("whiteVariant", smart.start)
        if not 0 < black < white < smart.end:
            raise DeriveError(f"{smart.at()}: expected the first pass tinted black, then white")
        return {
            "by_count": by_count,
            "count_when_unpowered": 0,
            "dense_cap": int(dense.group(1)),
            "dense_per_count": int(dense.group(2)),
            "max_count": max_count,
            "pass_tints": ["black_variant", "white_variant"],
            "counts_source": stream.src.at(
                min(plain.start(), dense.start()), max(plain.end(), dense.end())
            ),
            "source": span.at(),
        }

    # cables

    def _family(self, kind: MECableKind, texture: re.Match[str]) -> MECableKind:
        """The icon set a texture call draws with, for a cable of ``kind``."""
        name = texture.group(1)
        if name == "":  # this.getTexture(...): the kind's own choice
            span = self.classes.method(CABLE_CLASSES[kind], "getTexture")
            returns = list(span.finditer(re.compile(r"return this\.get(\w+)Texture\(c\);")))
            if not returns:
                raise DeriveError(f"{span.at()}: getTexture returns no texture family")
            name = returns[-1].group(1)
        return _FAMILIES[name]

    def _texture_before(self, span: Span, offset: int) -> re.Match[str]:
        before = [m for m in span.finditer(_TEXTURE_CALL) if m.start() < offset]
        if not before:
            raise DeriveError(f"{span.src.at(offset)} is drawn before any texture is set")
        return before[-1]

    def _cable_box(
        self, kind: MECableKind, span: Span, box: Box, at: int, inside: int, after: int
    ) -> dict[str, Any]:
        texture = self._texture_before(span, at)
        return {
            "box": list(box),
            "icons": self._family(kind, texture).value,
            "icons_source": span.src.at(texture.start()),
            "lights": _lights(span, inside, after),
            "source": span.src.at(at),
        }

    def _switch_entry(
        self, kind: MECableKind, span: Span, switch: DirectionSwitch
    ) -> dict[str, Any]:
        entry = self._cable_box(kind, span, _switch_box(switch), switch.at, switch.at, switch.end)
        entry["source"] = span.src.at(switch.cases["DOWN"][0])
        return entry

    def connection(self, kind: MECableKind, method: str) -> dict[str, Any]:
        """The arm (and the plug at a device block, if it draws one) of one connection method."""
        span = self.classes.method(CABLE_CLASSES[kind], method)
        switches = _direction_switches(span)
        if len(switches) not in (1, 2):
            raise DeriveError(
                f"{span.at()}: expected an arm switch, optionally after a plug switch"
            )
        *plug, arm = switches
        return {
            "arm": self._switch_entry(kind, span, arm),
            "plug": self._switch_entry(kind, span, plug[0]) if plug else None,
        }

    def cable(self, kind: MECableKind) -> dict[str, Any]:
        cls = CABLE_CLASSES[kind]
        render = self.classes.method(cls, "renderStatic")
        switches = _direction_switches(render)

        drawn = set(
            re.findall(r"this\.(render\w+Connection)\(", render.src.text[render.start : render.end])
        )
        if drawn != set(CONNECTIONS[kind].values()):
            raise DeriveError(f"{render.at()} draws connections with {sorted(drawn)}")
        if "dense" in CONNECTIONS[kind]:
            _require(
                self.classes.method(cls, "isDense"),
                r"t == AECableType\.DENSE \|\| t == AECableType\.DENSE_COVERED",
                "that a dense neighbour is DENSE or DENSE_COVERED",
            )
        if "smart" in CONNECTIONS[kind]:
            _require(
                self.classes.method(cls, "isSmart"),
                r"return t == AECableType\.SMART;",
                "that a smart neighbour is SMART",
            )

        index, count = CORE_CALL[kind]
        bare = _bare_calls(render, switches)
        if len(bare) != count:
            raise DeriveError(f"{render.at()} has {len(bare)} bare bounds calls, not {count}")
        core_call = bare[index]
        core = self._cable_box(
            kind,
            render,
            _box(core_call.group(1)),
            core_call.start(),
            core_call.start(),
            core_call.end(),
        )

        runs = list(render.finditer(_RENDER_BOUNDS))
        boxes = [_box(run.group(1), scale=16.0) for run in runs]
        along_y = [i for i, b in enumerate(boxes) if b[1] == 0 and b[4] == 16]
        if len(runs) != 3 or len(along_y) != 1:
            raise DeriveError(f"{render.at()}: expected one straight run per axis")
        y_run = runs[along_y[0]]
        y_box = boxes[along_y[0]]
        if {b for i, b in enumerate(boxes) if i != along_y[0]} != {
            toward("WEST", y_box),
            toward("NORTH", y_box),
        }:
            raise DeriveError(f"{render.at()}: the X and Z runs are not the Y run turned")
        straight = self._cable_box(kind, render, y_box, y_run.start(), y_run.start(), y_run.end())

        part_arm = None
        with_length = [s for s in switches if re.search(r"\blen\b", s.cases["DOWN"][1])]
        if len(with_length) != (1 if kind in PART_ARM_KINDS else 0):
            raise DeriveError(f"{render.at()}: {len(with_length)} part-arm switches")
        if with_length:
            part_arm = self._part_arm(kind, render, with_length[0])

        return {
            "connections": {
                neighbour: self.connection(kind, method)
                for neighbour, method in CONNECTIONS[kind].items()
            },
            "core": core,
            "part_arm": part_arm,
            "straight": straight,
        }

    def _part_arm(self, kind: MECableKind, render: Span, switch: DirectionSwitch) -> dict[str, Any]:
        """The arm drawn out to a part on the cable's own bus, which ends at the part's length."""
        at_zero = _box(switch.cases["DOWN"][1], {"len": 0})
        at_one = _box(switch.cases["DOWN"][1], {"len": 1})
        if [b - a for a, b in zip(at_zero, at_one, strict=True)] != [0, 1, 0, 0, 0, 0]:
            raise DeriveError(f"{render.src.at(switch.at)}: the length is not the arm's far end")
        for length in (3, 4, 5):
            _switch_box(switch, {"len": length})
        block_start, _ = _innermost_block(render, switch.at)
        guard_line = render.src.text[render.src.text.rfind("\n", 0, block_start) : block_start + 1]
        below = re.search(r"if \(len < (\d+)\) \{$", guard_line)
        if below is None:
            raise DeriveError(f"{render.src.at(switch.at)}: no length cut-off guards the arm")
        entry = self._cable_box(kind, render, at_zero, switch.at, switch.at, switch.end)
        del entry["box"]
        x0, _, z0, x1, top, z1 = at_zero
        entry.update(
            {
                "drawn_below_length": int(below.group(1)),
                "source": render.src.at(switch.cases["DOWN"][0]),
                "top": top,
                "x": [x0, x1],
                "z": [z0, z1],
            }
        )
        return entry

    # parts

    def part_frame(self) -> dict[str, Any]:
        """How a part's own frame (z = 16 at the face it sits on) lies on each side."""
        src = self.classes.source("BusCollisionHelper")
        case = re.compile(
            r"case (\w+) -> \{\s*this\.x = ForgeDirection\.(\w+);\s*this\.y = ForgeDirection\.(\w+);"
            r"\s*this\.z = ForgeDirection\.(\w+);\s*\}"
        )
        found = list(case.finditer(src.text))
        frame = {
            m.group(1).lower(): {k: m.group(i).lower() for k, i in (("x", 2), ("y", 3), ("z", 4))}
            for m in found
        }
        if set(frame) != {side.lower() for side in _SIDES}:
            raise DeriveError(f"{src.cite}: expected one frame per side, found {sorted(frame)}")
        if any(axes["z"] != side for side, axes in frame.items()):
            raise DeriveError(f"{src.cite}: a part's z axis does not point at its own side")
        return {"axes": frame, "source": src.at(found[0].start(), found[-1].end())}

    def part(self, device: MEDeviceKind) -> dict[str, Any]:
        cls = PART_CLASSES[device]
        leaf = self.classes.source(cls)

        arm = self.classes.method(cls, "cableConnectionRenderTo")
        length = _require(arm, r"return (\d+);", "a constant arm length")

        boxes_span = self.classes.method(cls, "getBoxes")
        box_calls = list(boxes_span.finditer(_BOX_CALL))
        if not box_calls:
            raise DeriveError(f"{boxes_span.at()} adds no boxes")

        render = self.classes.method(cls, "renderStatic")
        statement = re.compile(
            r"\brh\.setTexture\((?P<texture>.*?)\);|\brh\.setBounds\((?P<bounds>[^;]*?)\);"
            r"|\bthis\.renderLights\(",
            re.S,
        )
        drawn: list[dict[str, Any]] = []
        texture: re.Match[str] | None = None
        lights_after = False
        front_source = ""
        for found in render.finditer(statement):
            if found.group("texture") is not None:
                texture = found
            elif found.group("bounds") is not None:
                if texture is None:
                    raise DeriveError(f"{render.src.at(found.start())} draws before a texture")
                args = _split_args(texture.group("texture"))
                if len(args) != 6:
                    raise DeriveError(f"{render.src.at(texture.start())}: expected six faces")
                faces = [self.resolve_icon(cls, arg) for arg in args]
                down, up, north, south, west, east = (icon for icon, _ in faces)
                if not down == up == west == east:
                    raise DeriveError(f"{render.src.at(texture.start())}: the four sides differ")
                front_source = faces[3][1]
                drawn.append(
                    {
                        "back": north,
                        "box": list(_box(found.group("bounds"))),
                        "front": south,
                        "icons_source": render.src.at(texture.start(), texture.end()),
                        "sides": down,
                        "source": render.src.at(found.start()),
                        "status_lights": False,
                    }
                )
            else:
                lights_after = True
        if not drawn or not lights_after:
            raise DeriveError(f"{render.at()}: expected boxes, then the status lights")
        drawn[-1]["status_lights"] = True

        lights = self.classes.method(cls, "renderLights")
        faces = list(
            lights.finditer(
                re.compile(
                    r"rh\.renderFace\(x, y, z, CableBusTextures\.(\w+)\.getIcon\(\), ForgeDirection\.(\w+)"
                )
            )
        )
        if {m.group(2) for m in faces} != {"EAST", "WEST", "UP", "DOWN"} or len(
            {m.group(1) for m in faces}
        ) != 1:
            raise DeriveError(f"{lights.at()}: expected one light icon on the four sides")

        return {
            "arm_length": int(length.group(1)),
            "arm_length_source": arm.src.at(length.start()),
            "boxes": [list(_box(call.group(1))) for call in box_calls],
            "boxes_source": boxes_span.src.at(box_calls[0].start(), box_calls[-1].end()),
            "class": leaf.qualified,
            "front_source": front_source,
            "mod": leaf.clone.spec.modid,
            "render": drawn,
            "status_lights": self.icon(AE2.modid, self.cable_textures[faces[0].group(1)][0]),
            "status_lights_source": lights.src.at(faces[0].start(), faces[-1].end()),
        }

    # blocks

    def _block_faces(self, block: str) -> tuple[dict[str, str], str]:
        """A block's six faces in AE2's own frame (south its front), as ``registerBlockIcons`` picks
        them: ``<texture name><suffix>`` where that PNG exists, else the fallback icon's."""
        handler = self.classes.method("AETileBlockFeatureHandler", "register")
        _require(
            handler,
            r'setBlockTextureName\("appliedenergistics2:" \+ name\)',
            "that a block's texture name is its feature name",
        )
        icons = self._base_icons(block)
        optional = re.compile(
            r"FlippableIcon (\w+) = this\.optionalIcon\(\s*iconRegistry,\s*this\.getTextureName\(\)"
            r'(?: \+ "(\w+)")?,\s*(\w+)\);'
        )
        variables = {m.group(1): (m.group(2) or "", m.group(3)) for m in icons.finditer(optional)}
        order = _require(icons, r"info\.updateIcons\(([^)]*)\);", "the face order")
        updates = self.classes.method("BlockRenderInfo", "updateIcons")
        _require(
            updates,
            r"update\(bottom, top, north, south, east, west\)",
            "updateIcons(bottom, top, north, south, east, west)",
        )

        def resolve(variable: str) -> str:
            suffix, fallback = variables[variable]
            if fallback == "null" or self.clones[AE2.modid].has_icon(block + suffix):
                return self.icon(AE2.modid, block + suffix)
            return resolve(fallback)

        names = [arg.strip() for arg in order.group(1).split(",")]
        faces = dict(
            zip(("down", "up", "north", "south", "east", "west"), map(resolve, names), strict=True)
        )
        return faces, icons.src.at(icons.start, order.end())

    def _base_icons(self, block: str) -> Span:
        """The ``registerBlockIcons`` that picks ``block``'s faces: the nearest one up its chain,
        past overrides that call ``super`` first and only add icons of their own."""
        for cls in self.classes.chain(block):
            span = self.classes.own_method(cls, "registerBlockIcons")
            if span is not None and span.search(re.compile(r"super\.registerBlockIcons\(")) is None:
                return span
        raise DeriveError(f"{block} picks its faces nowhere up its class chain")

    def _cable_type(self, tile: str) -> tuple[MECableKind, str]:
        span = self.classes.method(tile, "getCableConnectionType")
        delegated = re.search(
            r"return this\.duality\.getCableConnectionType\(dir\);",
            span.src.text[span.start : span.end],
        )
        if delegated is not None:
            span = self.classes.method("DualityInterface", "getCableConnectionType")
        found = _require(span, r"return AECableType\.(\w+);", "a constant cable type")
        return _CABLE_TYPES[found.group(1)], span.src.at(found.start())

    def block(self, key: str) -> dict[str, Any]:
        cls = BLOCK_CLASSES[key]
        src = self.classes.source(cls)
        tile = re.search(r"this\.setTileEntity\((\w+)\.class\);", src.text)
        if tile is None:
            raise DeriveError(f"{src.cite} binds no tile entity")
        cable_type, cable_source = self._cable_type(tile.group(1))
        faces, faces_source = self._block_faces(cls)
        entry: dict[str, Any] = {
            "cable_type": cable_type.value,
            "cable_type_source": cable_source,
            "class": src.qualified,
            "faces": faces,
            "faces_source": faces_source,
            "states": None,
        }
        if key == "controller":
            entry.update(self._controller_states(faces))
        return entry

    def _controller_states(self, faces: Mapping[str, str]) -> dict[str, Any]:
        coloured = self.classes.method(
            "BlockController", "getRenderTexture", "int id, AEColor color"
        )
        _require(
            coloured,
            r"return id < 0 \? null : this\.getRenderTexture\(id\)\.getIcon\(\);",
            "that a Fluix controller wears the plain texture, and its own faces below id 0",
        )
        textures = self.classes.method("BlockController", "getRenderTexture", "int id")
        by_id = {
            int(m.group(1)): self.icon(AE2.modid, self.block_textures[m.group(2)][0])
            for m in textures.finditer(re.compile(r"case (\d+) -> ExtraBlockTextures\.(\w+);"))
        }
        render = self.classes.method("RenderBlockController", "renderInWorld")
        own = set(faces.values())
        if len(own) != 1:
            raise DeriveError("the controller's own faces differ; an 'off' state needs one icon")
        states: dict[str, Any] = {}
        for state, (texture_id, lights) in CONTROLLER_STATES.items():
            if texture_id is not None:
                _require(render, rf"textureId = {texture_id};", f"texture id {texture_id}")
            if lights is not None:
                _require(render, rf"lights = ExtraBlockTextures\.{lights};", f"the {lights} lights")
            states[state] = {
                "icon": next(iter(own)) if texture_id is None else by_id[texture_id],
                "lights": None
                if lights is None
                else self.icon(AE2.modid, self.block_textures[lights][0]),
            }
        return {"states": states, "states_source": render.at()}

    # the whole document

    def provenance(self) -> dict[str, Any]:
        return {
            "derived_by": "tools/derive_ae_render.py",
            "pack_version": ME_PACK_VERSION,
            "sources": {
                modid: {
                    "commit": _git(clone.root, "rev-parse", "HEAD"),
                    "cited_from": "src/main/java",
                    "repo": clone.spec.source_repo,
                    "tag": clone.spec.version,
                }
                for modid, clone in self.clones.items()
            },
            "spike": "docs/spikes/329-me-ae2.md",
        }

    def document(self) -> dict[str, Any]:
        doc = {
            "blocks": {key: self.block(key) for key in BLOCK_CLASSES},
            "cable_icons": self.cable_icons(),
            "cables": {kind.value: self.cable(kind) for kind in MECableKind},
            "channel_lights": self.channel_lights(),
            "colours": self.colours(),
            "note": NOTE,
            "part_frame": self.part_frame(),
            "parts": {device.value: self.part(device) for device in PART_CLASSES},
            "provenance": self.provenance(),
            "schema": AE_RENDER_SCHEMA,
        }
        AERender.model_validate(doc)  # what the previewer will load must load
        return doc


NOTE = (
    "AE2 and AE2FluidCraft render geometry and icon names for drawing an ME network (#337, #338). "
    "Numbers and icon NAMES only: every PNG is read from the pinned mod jar at preview time and is "
    "never committed. A box is [x0, y0, z0, x1, y1, z1] in sixteenths of a block. A cable box is "
    "given toward DOWN, with y = 0 the face toward what it connects to; a part box is in the part's "
    "own frame, where z = 16 is the face the part sits on (part_frame turns it to each side). An "
    "icon is a block-atlas name, modid:Name, read from assets/<modid>/textures/blocks/Name.png. A "
    "cable's connections are keyed by what the neighbour reports: dense (dense or dense_covered), "
    "smart, or other; a plug is drawn only at a device block, never at another cable bus. UV "
    "offsets and rotations are not recorded. Derived by tools/derive_ae_render.py from the clones in "
    "provenance; docs/spikes/329-me-ae2.md section 8 explains each number."
)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _rgb(hex_digits: str) -> list[int]:
    value = int(hex_digits, 16)
    return [(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]


def _git(clone: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(clone), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise DeriveError(f"git {' '.join(args)} failed in {clone}: {result.stderr.strip()}")
    return result.stdout.strip()


def clones_in(reference: Path) -> dict[str, Clone]:
    """The two pinned clones under ``reference``, each checked to be at its pinned tag."""
    clones: dict[str, Clone] = {}
    for spec in (AE2, AE2FC):
        root = reference / f"{spec.artifact}-{spec.version}"
        if not (root / "src" / "main" / "java").is_dir():
            raise DeriveError(
                f"no {spec.artifact} clone at {root}; clone {spec.source_repo} at tag "
                f"{spec.version} there (git clone --depth 1 --branch {spec.version})"
            )
        tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")
        if tag != spec.version:
            raise DeriveError(f"{root} is at {tag}, not the pinned {spec.version}")
        clones[spec.modid] = Clone(spec, root)
    return clones


#: A bracketed run of numbers or plain strings, which ``json.dumps(indent=2)`` spreads one element
#: per line; joined back onto one line so a box reads as a box.
_INLINE_ARRAY = re.compile(r'\[\s+((?:-?\d+|"[^"\n]*")(?:,\s+(?:-?\d+|"[^"\n]*"))*)\s+\]')


def render_text(doc: Mapping[str, Any]) -> str:
    """``doc`` as the committed file's text: sorted keys, two-space indent, short arrays inline."""
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)
    return _INLINE_ARRAY.sub(lambda m: "[" + re.sub(r",\s+", ", ", m.group(1)) + "]", text) + "\n"


def derive(reference: Path) -> dict[str, Any]:
    """The render document, read from the clones under ``reference``."""
    return Deriver(clones_in(reference)).document()


def main(argv: list[str] | None = None, out: Path = AE_RENDER_PATH) -> Path:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        raise SystemExit("usage: python tools/derive_ae_render.py [REFERENCE_DIR]")
    reference = Path(args[0]) if args else Path(os.environ.get(REFERENCE_ENV) or DEFAULT_REFERENCE)
    doc = derive(reference)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_text(doc), encoding="utf-8")
    print(
        f"wrote {out} from {reference}: {len(doc['cables'])} cable kinds, {len(doc['parts'])} "
        f"parts, {len(doc['blocks'])} blocks"
    )
    return out


if __name__ == "__main__":
    main()
