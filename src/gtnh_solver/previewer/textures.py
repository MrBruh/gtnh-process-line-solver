"""previewer.textures - resolve a machine to a GregTech block texture and embed it in the scene.

The previewer draws each machine as a box; this module skins that box with the machine's real
GT casing texture instead of a flat colour, so a layout reads like the structure it builds. The
resolution chain is::

    machine ``type`` (display name)
        -> data/multiblocks/<...>.json   (the doc whose controller.display_name matches)
        -> a representative block          (the controller's own block if the texture manifest
                                            resolves it, else the dominant *resolvable* casing
                                            block the doc's primary variant places)
        -> data/textures/manifest.json     (block+meta -> per-side iconset name)
        -> iconset PNG bytes               (extracted from the GT5-Unofficial jar, injected)
        -> a ``data:image/png;base64,`` URI on the machine box's six materials.

Everything here is **pure and unit-tested**: it maps names to names and, given PNG *bytes*,
names to ``data:`` URIs. The 135 MB jar fetch itself is a thin, untested shim
(:mod:`gtnh_solver.previewer.jar`) injected as ``png_provider`` - matching how ``scene.py`` keeps
the un-CI-testable WebGL last mile a static template while the mapping stays testable.

**Graceful degradation is the contract.** A machine whose type has no committed doc, or whose
blocks are all in the manifest's ``gaps`` (unresolved), or whose PNG bytes could not be fetched,
simply keeps its flat colour placeholder box. Nothing here raises on a miss - it returns ``None``
or omits the machine - so a preview always renders. PNGs are LGPL and are never committed; they
are fetched at preview time and embedded only in the emitted HTML.
"""

from __future__ import annotations

import base64
import json
import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gtnh_solver.dataset.schema import MultiblockDoc, Variant, load_multiblock_doc

_log = logging.getLogger(__name__)

#: Repo ``data/`` (this file is ``src/gtnh_solver/previewer/textures.py`` -> three parents up to
#: the source root, then the repo root). Resolves in the editable/dev install the repo runs as;
#: :func:`texturize_scene` takes explicit paths for any other layout (and every test passes them).
_DATA = Path(__file__).resolve().parents[3] / "data"
DEFAULT_MULTIBLOCKS_DIR = _DATA / "multiblocks"
DEFAULT_MANIFEST_PATH = _DATA / "textures" / "manifest.json"

#: A ``png_provider``: given ``{icon_name: asset_path_in_jar}`` it returns ``{icon_name: bytes}``
#: for the icons it could supply (missing icons are simply omitted, never an error). The real one
#: reads the GT5-Unofficial jar (:func:`gtnh_solver.previewer.jar.jar_png_provider`); tests inject
#: a fake so no network runs in the suite.
PngProvider = Callable[[Mapping[str, str]], dict[str, bytes]]

# A GT block's six faces are indexed by ForgeDirection (0=down/-Y, 1=up/+Y, 2=north/-Z,
# 3=south/+Z, 4=west/-X, 5=east/+X). three.js BoxGeometry takes its six materials in the order
# [+X east, -X west, +Y up, -Y down, +Z south, -Z north]. This maps a GT side index to the slot
# it occupies in that six-material array, so the icons land on the right faces of the box.
_GT_SIDE_TO_THREE_SLOT = {0: 3, 1: 2, 2: 5, 3: 4, 4: 1, 5: 0}
_FACE_SLOTS = 6


@dataclass(frozen=True)
class TextureSummary:
    """What :func:`texturize_scene` resolved - a small, loggable report for the CLI/verification.

    ``textured_types`` are the machine types skinned with a real GT texture; ``placeholder_types``
    kept their flat colour box (no committed doc, an all-gapped block, or unfetched PNG bytes).
    ``embedded_icons`` counts the distinct PNGs embedded as ``data:`` URIs in the page.
    """

    textured_types: tuple[str, ...]
    placeholder_types: tuple[str, ...]
    embedded_icons: int


class TextureManifest:
    """A loaded ``data/textures/manifest.json``: block+meta -> per-side iconset name -> jar path.

    A light typed wrapper over the raw manifest (lane 6's output). It answers the two questions
    the previewer asks: which icon sits on each face of a block+meta, and where that icon's PNG
    lives inside the mod jar. It never reaches for the network or the filesystem beyond the one
    JSON it is constructed from.
    """

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._blocks: Mapping[str, Any] = raw.get("blocks", {})
        self._icons: Mapping[str, str] = raw.get("icons", {})

    @classmethod
    def load(cls, path: str | Path) -> TextureManifest:
        """Parse ``manifest.json`` at ``path`` into a :class:`TextureManifest`."""
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def block_face_icons(self, block: str, meta: int) -> list[str | None] | None:
        """The six per-face iconset names for ``block``+``meta`` in three.js material order.

        Returns ``None`` when the block or meta is absent from the manifest (unresolved / gapped).
        A uniform block (manifest ``"all"`` entry, or a bare string) fills all six slots; a
        per-side block fills each face from its ForgeDirection index and leaves unmapped faces
        ``None`` (the viewer falls back to the flat colour there). Returns ``None`` only when the
        block resolves to no usable face at all.
        """
        block_entry = self._blocks.get(block)
        if block_entry is None:
            return None
        sides = block_entry.get("metas", {}).get(str(meta))
        if sides is None:
            return None
        if isinstance(sides, str):
            return [sides] * _FACE_SLOTS
        all_sides = sides.get("all")
        if all_sides is not None:
            return [all_sides] * _FACE_SLOTS
        faces: list[str | None] = [None] * _FACE_SLOTS
        for side_key, icon in sides.items():
            slot = _GT_SIDE_TO_THREE_SLOT.get(int(side_key))
            if slot is not None:
                faces[slot] = icon
        return faces if any(faces) else None

    def icon_asset_path(self, icon: str) -> str | None:
        """The path inside the mod jar for ``icon`` (e.g. ``assets/gregtech/.../NAME.png``)."""
        return self._icons.get(icon)


def load_multiblock_docs(data_dir: str | Path) -> dict[str, MultiblockDoc]:
    """Load every ``data/multiblocks/<name>.json`` under ``data_dir``, keyed by display name.

    Skips ``_meta.json`` (the run summary) and returns ``{}`` if the directory is absent, so a
    checkout without a committed dump texturizes nothing rather than failing. On the (schema-
    forbidden) case of two files claiming one display name, the first sorted wins - the previewer
    only needs *a* representative texture, so it stays lenient where the dataset adapter is strict.
    """
    directory = Path(data_dir)
    if not directory.is_dir():
        return {}
    docs: dict[str, MultiblockDoc] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "_meta.json":
            continue
        doc = load_multiblock_doc(path)
        docs.setdefault(doc.controller.display_name, doc)
    return docs


def _largest_variant(doc: MultiblockDoc) -> Variant:
    """The variant standing for the machine's built form: the one placing the most blocks.

    Mirrors the dataset adapter's primary-variant choice (largest form, trigger stack as the
    deterministic tie-break) without importing its private helper, so the representative texture
    matches the footprint the solver reserved.
    """
    return max(doc.variants, key=lambda v: (len(v.blocks), v.trigger_stack_size))


def resolve_face_icons(doc: MultiblockDoc, manifest: TextureManifest) -> list[str | None] | None:
    """The six per-face iconset names to skin ``doc``'s machine, or ``None`` if nothing resolves.

    Prefers the controller's own block; if the manifest cannot resolve it (the ``gt.blockmachines``
    controller hulls are composite tile-entity overlays and sit in the manifest's ``gaps``), falls
    back to the **dominant resolvable block** the primary variant places - the casing shell that
    visually defines the multiblock (e.g. the Electric Blast Furnace's heat-proof casing). The
    fallback is deterministic: most-placed block first, ties broken by registry name then meta.
    """
    controller = doc.controller
    faces = manifest.block_face_icons(controller.registry_name, controller.meta)
    if faces is not None:
        return faces

    counts = Counter((b.block, b.meta) for b in _largest_variant(doc).blocks)
    for (block, meta), _count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        faces = manifest.block_face_icons(block, meta)
        if faces is not None:
            return faces
    return None


def resolve_scene_types(
    scene: Mapping[str, Any], docs: Mapping[str, MultiblockDoc], manifest: TextureManifest
) -> dict[str, list[str | None]]:
    """For each distinct machine ``type`` in ``scene`` that resolves, its six per-face icon names."""
    type_faces: dict[str, list[str | None]] = {}
    for machine_type in {m["type"] for m in scene["machines"]}:
        doc = docs.get(machine_type)
        if doc is None:
            continue
        faces = resolve_face_icons(doc, manifest)
        if faces is not None:
            type_faces[machine_type] = faces
    return type_faces


def _png_data_uri(png: bytes) -> str:
    """Encode raw PNG bytes as a self-contained ``data:image/png;base64,...`` URI."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def apply_textures(
    scene: dict[str, Any],
    type_faces: Mapping[str, Sequence[str | None]],
    icon_png: Mapping[str, bytes],
) -> set[str]:
    """Embed ``icon_png`` as ``data:`` URIs and stamp each machine's per-face texture on ``scene``.

    Writes a shared ``scene["textures"]`` pool (icon name -> data URI, deduped so a casing used by
    many machines embeds once) and gives every machine whose type resolved and whose icons were
    actually supplied a ``texture`` field: a six-element list of icon names (or ``None`` per face)
    in three.js material order. Faces whose PNG bytes were not supplied fall back to the flat
    colour; a machine that got no usable face keeps its placeholder box entirely. Returns the set
    of machine types that ended up textured.
    """
    pool = {icon: _png_data_uri(png) for icon, png in icon_png.items()}
    scene["textures"] = pool
    textured: set[str] = set()
    for machine in scene["machines"]:
        faces = type_faces.get(machine["type"])
        if faces is None:
            continue
        resolved = [icon if (icon in pool) else None for icon in faces]
        if any(resolved):
            machine["texture"] = resolved
            textured.add(machine["type"])
    return textured


def texturize_scene(
    scene: dict[str, Any],
    *,
    multiblocks_dir: str | Path | None = None,
    manifest_path: str | Path | None = None,
    png_provider: PngProvider | None = None,
) -> TextureSummary:
    """Resolve every machine in ``scene`` to its GT texture and embed the PNGs, in place.

    Ties the pure pieces together: load the docs + manifest, resolve each machine type to its
    icons, ask ``png_provider`` for only the icons actually needed (an empty need never calls the
    provider, so a scene of undocumented machines triggers no jar fetch), embed them, and log a
    textured-vs-placeholder summary. Missing data (no dump, no manifest) degrades to all
    placeholders. Returns a :class:`TextureSummary`.
    """
    all_types = tuple(sorted({m["type"] for m in scene["machines"]}))
    mb_dir = DEFAULT_MULTIBLOCKS_DIR if multiblocks_dir is None else Path(multiblocks_dir)
    mf_path = DEFAULT_MANIFEST_PATH if manifest_path is None else Path(manifest_path)

    docs = load_multiblock_docs(mb_dir)
    if not docs or not Path(mf_path).is_file():
        scene.setdefault("textures", {})
        _log.info(
            "textures: no dataset/manifest available; all %d types placeholder", len(all_types)
        )
        return TextureSummary(textured_types=(), placeholder_types=all_types, embedded_icons=0)

    manifest = TextureManifest.load(mf_path)
    type_faces = resolve_scene_types(scene, docs, manifest)
    wanted = {icon for faces in type_faces.values() for icon in faces if icon is not None}
    icon_paths = {
        icon: path for icon in wanted if (path := manifest.icon_asset_path(icon)) is not None
    }
    icon_png = png_provider(icon_paths) if (png_provider is not None and icon_paths) else {}

    textured = apply_textures(scene, type_faces, icon_png)
    placeholder = tuple(t for t in all_types if t not in textured)
    summary = TextureSummary(
        textured_types=tuple(sorted(textured)),
        placeholder_types=placeholder,
        embedded_icons=len(scene.get("textures", {})),
    )
    _log.info(
        "textures: %d/%d machine types skinned (%s); placeholder: %s; %d PNG(s) embedded",
        len(summary.textured_types),
        len(all_types),
        ", ".join(summary.textured_types) or "none",
        ", ".join(summary.placeholder_types) or "none",
        summary.embedded_icons,
    )
    return summary
