"""The committed ME render data: AE2's cable, part and block geometry and icon names (#337).

Drawing an ME network (#338) needs what AE2's render methods know and no data file says: a smart
cable's core is 5..11, an arm toward a dense neighbour widens to 4..12, an import bus is three boxes
deep, a grey glass cable wears ``MECable_Grey`` while every other grey wears ``..._Gray``.
``tools/derive_ae_render.py`` reads all of it out of the pinned AE2 and AE2FluidCraft source
(``dataset.mod_jars``) into ``data/ae2/<AE2 version>/render.json``; this module loads that file into
typed, validated models. ``docs/spikes/329-me-ae2.md`` section 8 explains every number.

**Numbers and icon names only.** An icon is a block-atlas name, ``modid:Name``, which
:func:`asset_path` turns into the ``assets/<modid>/textures/blocks/Name.png`` path inside that mod's
jar; the previewer fetches the PNG from the jar at preview time
(:func:`gtnh_solver.previewer.jar.multi_jar_png_provider`), so no AE2 art is ever committed. AE2's
art is CC BY-NC-SA 3.0 (spike 9.2), which is the previewer's business when it embeds one, not this
loader's.

**Why it lives in** ``dataset`` **and not the previewer.** It is committed, pinned data with a
schema, like the multiblock fixtures beside it, and it is pure: no network, no Pillow. Keeping it
here lets the derivation tool validate its own output through these models (what the tool writes is
exactly what loads), and keeps the previewer a consumer of it, as it is of every other dataset.

**Frames.** A box is ``(x0, y0, z0, x1, y1, z1)`` in sixteenths of a block, every one within 0..16
with each minimum below its maximum (checked on load):

- a **cable** box is given toward DOWN, ``y = 0`` the face toward what it connects to; the deriver
  checked AE2's other five cases are that box turned (``toward`` in the tool);
- a **part** box is in the part's own frame, ``z = 16`` the face it sits on, and
  :attr:`AERender.part_frame` says where that frame's axes point on each side.

**Not a shadowable dataset.** The file is keyed by AE2's version, not the pack's, and no local dump
ever replaces it, so it is read from the repo's ``data/`` directly rather than through
``roots.resolve_dataset_path``; ``roots`` reserves the ``ae2`` folder so it is never mistaken for a
generated pack version.
"""

from __future__ import annotations

import json
from collections.abc import Set as AbstractSet
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from gtnh_solver.ir.me import AEColor, MECableKind, MEDeviceKind

from .mod_jars import AE2
from .schema import DatasetSchemaError

#: The render data's own schema version; the file's top-level ``schema`` must match it.
AE_RENDER_SCHEMA = 1

#: The committed render data for the pinned AE2 (this file is ``src/gtnh_solver/dataset/...``, so
#: ``parents[3]`` is the repo root).
AE_RENDER_PATH = Path(__file__).resolve().parents[3] / "data" / "ae2" / AE2.version / "render.json"

#: The ME devices that are AE2 or AE2FluidCraft parts, which is every one the render data draws; GT's
#: own ME hatches are GT blocks and drawn from GT's texture manifest.
PART_DEVICES: frozenset[MEDeviceKind] = frozenset(
    {
        MEDeviceKind.IMPORT_BUS,
        MEDeviceKind.EXPORT_BUS,
        MEDeviceKind.STORAGE_BUS,
        MEDeviceKind.INTERFACE,
        MEDeviceKind.FLUID_IMPORT_BUS,
        MEDeviceKind.FLUID_EXPORT_BUS,
        MEDeviceKind.FLUID_STORAGE_BUS,
        MEDeviceKind.DUAL_INTERFACE,
    }
)

#: The AE2 block devices an ME network is built with.
MEBlock = Literal["controller", "interface", "drive", "energy_acceptor"]
ME_BLOCKS: tuple[MEBlock, ...] = ("controller", "interface", "drive", "energy_acceptor")

#: A side, as the scene names facings.
Side = Literal["down", "up", "north", "south", "west", "east"]
SIDES: tuple[Side, ...] = ("down", "up", "north", "south", "west", "east")

#: A cable connection's key: what the neighbour reports. ``dense`` covers a dense or dense covered
#: neighbour, ``smart`` a smart one, ``other`` anything else (spike 8.1).
Neighbour = Literal["dense", "smart", "other"]

_FROZEN = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _check_box(box: tuple[int, int, int, int, int, int]) -> tuple[int, int, int, int, int, int]:
    x0, y0, z0, x1, y1, z1 = box
    if not all(0 <= value <= 16 for value in box):
        raise ValueError(f"{box} reaches outside the block (0..16)")
    if not (x0 < x1 and y0 < y1 and z0 < z1):
        raise ValueError(f"{box} has a minimum that is not below its maximum")
    return box


def _check_span(span: tuple[int, int]) -> tuple[int, int]:
    if not 0 <= span[0] < span[1] <= 16:
        raise ValueError(f"{span} is not an increasing span within 0..16")
    return span


def _check_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    if not all(0 <= value <= 255 for value in rgb):
        raise ValueError(f"{rgb} is not an RGB triple")
    return rgb


#: A box in sixteenths, ``(x0, y0, z0, x1, y1, z1)``.
Box = Annotated[tuple[int, int, int, int, int, int], AfterValidator(_check_box)]
#: A block-atlas icon name, ``modid:Name``.
Icon = Annotated[str, Field(pattern=r"^[a-z0-9_]+:[\w.]+$")]
RGB = Annotated[tuple[int, int, int], AfterValidator(_check_rgb)]
_Span = Annotated[tuple[int, int], AfterValidator(_check_span)]


def asset_path(icon: str) -> str:
    """The path of ``icon``'s PNG inside its mod's jar: ``assets/<modid>/textures/blocks/<Name>.png``."""
    modid, _, name = icon.partition(":")
    if not modid or not name:
        raise ValueError(f"{icon!r} is not a modid:Name icon")
    return f"assets/{modid}/textures/blocks/{name}.png"


class CableBox(BaseModel):
    """One box a cable draws, the icon set it wears, and whether channel lights go over it.

    ``icons`` names a cable kind's icon set (:attr:`AERender.cable_icons`), which is not always the
    cable's own: a smart cable's core wears the covered set, a dense cable's arms the smart one.
    ``lights`` means the two channel-light passes (:class:`ChannelLights`) are drawn over it.
    """

    model_config = _FROZEN

    box: Box
    icons: MECableKind
    icons_source: str
    lights: bool
    source: str


class PartArm(BaseModel):
    """The arm a cable draws out to a part on its own bus, which ends where the part's arm length
    says (:attr:`PartRender.arm_length`), and only for a length below ``drawn_below_length``."""

    model_config = _FROZEN

    x: _Span
    z: _Span
    top: int = Field(ge=0, le=16)
    drawn_below_length: int
    icons: MECableKind
    icons_source: str
    lights: bool
    source: str

    def box(self, length: int) -> tuple[int, int, int, int, int, int]:
        """The arm toward DOWN for a part whose arm length is ``length``."""
        return (self.x[0], length, self.z[0], self.x[1], self.top, self.z[1])


class CableConnection(BaseModel):
    """What a cable draws toward one connected side: its arm, and the plug it adds at a device block
    (``None`` when that style draws none, as a dense-to-dense arm does not)."""

    model_config = _FROZEN

    arm: CableBox
    plug: CableBox | None


class CableRender(BaseModel):
    """One cable kind's geometry.

    ``straight`` is the bar AE2 draws instead of a core and arms on a straight run (spike 8.1 says
    when); ``part_arm`` is ``None`` for a kind that takes no parts.
    """

    model_config = _FROZEN

    core: CableBox
    straight: CableBox
    part_arm: PartArm | None
    connections: dict[Neighbour, CableConnection]

    @model_validator(mode="after")
    def _has_a_default_connection(self) -> Self:
        if "other" not in self.connections:
            raise ValueError("a cable needs an 'other' connection for any neighbour")
        return self

    def connection(self, neighbour: MECableKind | None) -> CableConnection:
        """What this kind draws toward a neighbour reporting ``neighbour`` (``None``: not a cable
        type at all, e.g. a GT ME hatch)."""
        if neighbour in (MECableKind.DENSE, MECableKind.DENSE_COVERED):
            return self.connections.get("dense", self.connections["other"])
        if neighbour is MECableKind.SMART:
            return self.connections.get("smart", self.connections["other"])
        return self.connections["other"]


class CableIcons(BaseModel):
    """One cable kind's icon set: an icon per colour, Fluix included."""

    model_config = _FROZEN

    by_colour: dict[AEColor, Icon]
    source: str

    @model_validator(mode="after")
    def _every_colour(self) -> Self:
        missing = set(AEColor) - set(self.by_colour)
        if missing:
            raise ValueError(f"no icon for {sorted(c.value for c in missing)}")
        return self


class ColourTints(BaseModel):
    """An AE colour's tints (``AEColor``): ``black_variant`` and ``white_variant`` tint the channel
    lights and a part's status lights; ``medium_variant`` is the colour as AE shows it plainly."""

    model_config = _FROZEN

    ae_name: str
    ordinal: int
    black_variant: RGB
    medium_variant: RGB
    white_variant: RGB
    source: str


class ChannelLights(BaseModel):
    """The lights a smart or dense cable shows its channel count with (spike 8.3).

    Two passes, each a white mask drawn fullbright and tinted by the cable colour's field named in
    ``pass_tints``. ``by_count`` gives both passes' icons for each count a side can show.
    """

    model_config = _FROZEN

    by_count: dict[int, tuple[Icon, Icon]]
    count_when_unpowered: int
    dense_cap: int
    dense_per_count: int
    max_count: int
    pass_tints: tuple[str, str]
    counts_source: str
    source: str

    @model_validator(mode="after")
    def _every_count(self) -> Self:
        if set(self.by_count) != set(range(self.max_count + 1)):
            raise ValueError(f"lights must cover every count 0..{self.max_count}")
        return self

    def shown_count(self, channels: int, *, dense: bool = False, powered: bool = True) -> int:
        """The count a side shows for ``channels`` in use: capped, a dense-to-dense side in fours,
        and nothing at all on an unpowered network."""
        if not powered:
            return self.count_when_unpowered
        if dense:
            return min(channels, self.dense_cap) // self.dense_per_count
        return min(channels, self.max_count)

    def icons(self, count: int) -> tuple[str, str]:
        """The (first pass, second pass) icons for a shown ``count``."""
        return self.by_count[max(0, min(count, self.max_count))]


class PartFrame(BaseModel):
    """Where a part's own x, y and z axes point when it sits on each side (z at the side itself)."""

    model_config = _FROZEN

    axes: dict[Side, dict[Literal["x", "y", "z"], Side]]
    source: str

    @model_validator(mode="after")
    def _every_side(self) -> Self:
        if set(self.axes) != set(SIDES) or any(a["z"] != s for s, a in self.axes.items()):
            raise ValueError("a part frame per side, its z axis at that side")
        return self


class PartRenderBox(BaseModel):
    """One box a part draws, in its own frame, with its icons: ``back`` toward the cable (z = 0),
    ``front`` toward the block it works on (z = 16), ``sides`` the other four. The box with
    ``status_lights`` also gets :attr:`PartRender.status_lights` over its sides."""

    model_config = _FROZEN

    box: Box
    sides: Icon
    back: Icon
    front: Icon
    status_lights: bool
    icons_source: str
    source: str


class PartRender(BaseModel):
    """One part's geometry: the boxes it collides as, the boxes it is drawn as, and its arm length
    (how close to the cable's centre the arm the cable draws out to it ends)."""

    model_config = _FROZEN

    class_name: str = Field(alias="class")
    mod: str
    arm_length: int = Field(ge=0, le=16)
    arm_length_source: str
    boxes: tuple[Box, ...] = Field(min_length=1)
    boxes_source: str
    render: tuple[PartRenderBox, ...] = Field(min_length=1)
    front_source: str
    status_lights: Icon
    status_lights_source: str


class ControllerState(BaseModel):
    """One controller look: the icon on all six faces, and the lights drawn over it, if any."""

    model_config = _FROZEN

    icon: Icon
    lights: Icon | None


class BlockRender(BaseModel):
    """An AE2 block device: its six faces in AE2's own frame (``south`` its front), the cable type
    it reports to a cable beside it (which arm and plug that cable draws), and, for the controller,
    its looks by neighbours and power."""

    model_config = _FROZEN

    class_name: str = Field(alias="class")
    cable_type: MECableKind
    cable_type_source: str
    faces: dict[Side, Icon]
    faces_source: str
    states: dict[str, ControllerState] | None
    states_source: str | None = None

    @model_validator(mode="after")
    def _every_face(self) -> Self:
        if set(self.faces) != set(SIDES):
            raise ValueError("a block needs an icon on every face")
        return self


class SourceRepo(BaseModel):
    """One source checkout the numbers were read from."""

    model_config = _FROZEN

    repo: str
    tag: str
    commit: str
    cited_from: str


class Provenance(BaseModel):
    model_config = _FROZEN

    derived_by: str
    pack_version: str
    spike: str
    sources: dict[str, SourceRepo]


class AERender(BaseModel):
    """The whole render data. Complete by construction: every cable kind, colour, part and block
    device the vocabulary names is present, or the file does not load."""

    model_config = _FROZEN

    schema_version: int = Field(alias="schema")
    note: str
    provenance: Provenance
    colours: dict[AEColor, ColourTints]
    cables: dict[MECableKind, CableRender]
    cable_icons: dict[MECableKind, CableIcons]
    channel_lights: ChannelLights
    part_frame: PartFrame
    parts: dict[MEDeviceKind, PartRender]
    blocks: dict[MEBlock, BlockRender]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        gaps: dict[str, AbstractSet[str]] = {
            "colours": set(AEColor) - set(self.colours),
            "cables": set(MECableKind) - set(self.cables),
            "cable_icons": set(MECableKind) - set(self.cable_icons),
            "parts": PART_DEVICES ^ set(self.parts),
            "blocks": set(ME_BLOCKS) - set(self.blocks),
        }
        # An enum key is named by its value, as the file spells it: str() of a str enum gives the
        # class-qualified name since Python 3.11.
        named = {
            key: sorted(item.value if isinstance(item, Enum) else item for item in gap)
            for key, gap in gaps.items()
            if gap
        }
        if named:
            raise ValueError(f"the render data is incomplete: {named}")
        return self

    def cable_icon(self, icons: MECableKind, colour: AEColor) -> str:
        """The icon ``icons``' set (a :attr:`CableBox.icons`) wears in ``colour``."""
        return self.cable_icons[icons].by_colour[colour]

    def icon_names(self) -> frozenset[str]:
        """Every icon the data names: what a preview of any ME network could ask a jar for."""
        names = {icon for set_ in self.cable_icons.values() for icon in set_.by_colour.values()}
        names.update(icon for pair in self.channel_lights.by_count.values() for icon in pair)
        for part in self.parts.values():
            names.add(part.status_lights)
            names.update(icon for box in part.render for icon in (box.sides, box.back, box.front))
        for block in self.blocks.values():
            names.update(block.faces.values())
            for state in (block.states or {}).values():
                names.add(state.icon)
                if state.lights is not None:
                    names.add(state.lights)
        return frozenset(names)


def load_ae_render(path: str | Path | None = None) -> AERender:
    """The render data at ``path`` (default: the committed file for the pinned AE2), validated.

    A file whose ``schema`` is not :data:`AE_RENDER_SCHEMA` is refused with
    :class:`~gtnh_solver.dataset.schema.DatasetSchemaError` before any field is read, as the
    multiblock dataset's loader does: a stale file must not half-load.
    """
    source = AE_RENDER_PATH if path is None else Path(path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    declared = raw.get("schema") if isinstance(raw, dict) else None
    if declared != AE_RENDER_SCHEMA:
        raise DatasetSchemaError(
            f"{source} declares render schema {declared!r}; this build reads {AE_RENDER_SCHEMA}. "
            "Re-run tools/derive_ae_render.py."
        )
    return AERender.model_validate(raw)
