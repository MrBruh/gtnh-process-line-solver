"""previewer.textures - expand each machine into per-block textured cubes (lane 7 v2).

The solver runs on a coarse cell grid where a multiblock is one integer box; block accuracy is
materialised only at preview time. This module does that materialisation (plan section 5.6): for
each placed machine it looks up the extracted :class:`MultiblockDoc`, selects the representative
variant, and expands that variant's ``blocks`` list into ONE textured cube per constituent block at
its ``d = [dx, dy, dz]`` offset. Each cube's six faces are textured independently from the layered
manifest (lane 6 v2), each face's ``ITexture`` layer stack pre-baked to a flat PNG (:mod:`.bake`)
and embedded as a ``data:`` URI. A single stretched casing box over the whole multiblock is exactly
the v1 defect this replaces (principle 6): it erased the coils, glass, and hatch faces that make a
layout readable.

The pipeline is pure and unit-tested end to end given PNG *bytes*; the 135 MB jar fetch is the one
untested shim (:mod:`.jar`), injected as ``png_provider``. **Graceful degradation is the contract**:
a machine with no committed doc, or whose blocks all fail to resolve, keeps its flat placeholder box;
a single unresolved face on an otherwise-textured cube falls back to the flat colour there. Nothing
here raises on a miss, and a Pillow-less install (no ``preview`` extra) degrades the whole pass to
placeholders rather than failing. PNGs are LGPL and never committed; they are fetched at preview
time and embedded only in the emitted HTML.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gtnh_solver.dataset.roots import resolve_dataset_path
from gtnh_solver.dataset.schema import MultiblockDoc, Variant, load_multiblock_doc

from .bake import BakeUnavailableError, bake_layers

_log = logging.getLogger(__name__)

#: Repo ``data/`` (this file is ``src/gtnh_solver/previewer/textures.py`` -> three parents up to
#: the source root, then the repo root). ``texturize_scene`` takes explicit paths for any other
#: layout (and every test passes them), so this default only matters for the dev/editable install.
_DATA = Path(__file__).resolve().parents[3] / "data"
DEFAULT_MULTIBLOCKS_DIR = _DATA / "multiblocks"
DEFAULT_MANIFEST_PATH = _DATA / "textures" / "manifest.json"

#: A ``png_provider``: given ``{icon_name: asset_path_in_jar}`` it returns ``{icon_name: bytes}``
#: for the icons it could supply (missing icons are simply omitted, never an error). The real one
#: reads the GT5-Unofficial jar (:func:`gtnh_solver.previewer.jar.jar_png_provider`); tests inject a
#: fake so no network runs in the suite.
PngProvider = Callable[[Mapping[str, str]], dict[str, bytes]]

#: GT/Minecraft ForgeDirection face order: 0 down (-Y), 1 up (+Y), 2 north (-Z), 3 south (+Z),
#: 4 west (-X), 5 east (+X). The manifest keys per-side layers by these names.
_SIDE_NAMES = ("DOWN", "UP", "NORTH", "SOUTH", "WEST", "EAST")

#: three.js ``BoxGeometry`` takes six materials in the order [+X east, -X west, +Y up, -Y down,
#: +Z south, -Z north]. This maps a GT side index to the slot it occupies, so a face's texture
#: lands on the right side of the cube.
_GT_SIDE_TO_THREE_SLOT = {0: 3, 1: 2, 2: 5, 3: 4, 4: 1, 5: 0}
_FACE_SLOTS = 6

#: The render state the idle preview shows. GT overlays have an ``active`` variant too; the
#: previewer draws machines at rest by default and lets the viewer toggle to the running skin.
_STATE = "inactive"

#: The running-machine render state. Its baked face is emitted only where it actually differs from
#: the idle bake (an ``_ACTIVE`` overlay), so a plain casing carries no second texture (see
#: :func:`texturize_scene`). The viewer's state toggle swaps to it.
_STATE_ACTIVE = "active"

#: Horizontal ForgeDirection sides as (dx, dz) unit vectors, for the yaw that orients a machine's
#: blocks to its placed ``front`` (the dump builds every controller facing NORTH / -Z).
_SIDE_VEC = {2: (0, -1), 3: (0, 1), 4: (-1, 0), 5: (1, 0)}
_VEC_SIDE = {v: s for s, v in _SIDE_VEC.items()}
#: Clockwise 90-degree steps (viewed from +Y) from the dump's NORTH front to each placed facing.
_FRONT_CW_STEPS = {"north": 0, "east": 1, "south": 2, "west": 3}

#: The GT single-block machine name prefix per voltage tier. A plan export names a single-block
#: machine generically ("Forge Hammer"), but the manifest keys it by its in-game tier-prefixed name
#: ("Basic Forge Hammer" at LV, "Advanced Forge Hammer" at MV). Only LV and MV share one prefix
#: across every single-block family; above MV the scheme diverges per family ("Advanced X II/III/IV",
#: "Universal", "Elite", steam-only variants), so there is no reliable generic tier->name rule there.
#: Those tiers (and an unknown/absent tier) fall back to the ``_FALLBACK_PREFIX`` variant, an honest
#: preview stand-in because GT single-block skins are near identical across tiers.
_TIER_PREFIX = {"LV": "Basic", "MV": "Advanced"}
_FALLBACK_PREFIX = "Basic"

#: Runs of non-alphanumeric characters, collapsed to one space when normalizing a machine name so
#: matching tolerates case, punctuation, and whitespace differences between plan and manifest.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_name(name: str) -> str:
    """Casefold ``name`` and collapse non-alphanumeric runs to single spaces, for tolerant lookup."""
    return _NON_ALNUM.sub(" ", name.casefold()).strip()


def _tier_prefixes(tier: str | None) -> list[str]:
    """Ordered name-prefix candidates for a machine at ``tier``: its GT prefix then the Basic fallback.

    LV/MV map to their shared prefix; every other (or unknown) tier resolves through ``Basic`` alone,
    the honest lowest-tier stand-in for the tiers whose GT naming is not a determinable generic rule.
    """
    prefix = _TIER_PREFIX.get(tier or "")
    if prefix and prefix != _FALLBACK_PREFIX:
        return [prefix, _FALLBACK_PREFIX]
    return [_FALLBACK_PREFIX]


#: Roman-numeral (and digit) tier tokens a tiered-storage name ends with: the manifest keys Super
#: Tank / Super Chest as "Super Tank I".."IX", but a plan names them generically ("Super Tank"). A
#: generic name maps to the LOWEST such variant, an honest stand-in (the tiers share a near-identical
#: skin, like the Basic fallback for voltage tiers).
_TIER_TOKENS: dict[str, int] = {
    roman: rank
    for rank, roman in enumerate(["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix"], start=1)
}
_TIER_TOKENS.update({str(n): n for n in range(1, 10)})


def _split_tier_suffix(norm_name: str) -> tuple[str, int] | None:
    """``(base, rank)`` when ``norm_name`` ends with a tier token, else ``None``.

    ``"super tank iii"`` -> ``("super tank", 3)``; ``"forge hammer"`` -> ``None``.
    """
    base, _, last = norm_name.rpartition(" ")
    rank = _TIER_TOKENS.get(last)
    return (base, rank) if base and rank is not None else None


@dataclass(frozen=True)
class TextureSummary:
    """What :func:`texturize_scene` resolved - a small, loggable report for the CLI/verification.

    ``textured_types`` are machine types expanded into real per-block cubes; ``placeholder_types``
    kept their flat colour box (no committed doc, an all-unresolved variant, no PNG bytes, or no
    Pillow). ``block_cubes`` is the total textured cubes emitted; ``embedded_icons`` the distinct
    baked idle-state face PNGs in the page; ``embedded_active_icons`` the extra running-state face
    PNGs (only the faces whose active bake differs from idle, e.g. an ``_ACTIVE`` overlay).
    """

    textured_types: tuple[str, ...]
    placeholder_types: tuple[str, ...]
    block_cubes: int
    embedded_icons: int
    embedded_active_icons: int = 0


class TextureManifest:
    """A loaded layered ``data/textures/manifest.json`` (lane 6 v2, schema 2).

    Answers the two questions the previewer asks: the ordered ``ITexture`` layer stack for a
    ``(block, meta, side, state)``, and the jar path of an icon so its PNG can be fetched. Never
    touches the network or the filesystem beyond the one JSON it is built from.
    """

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._blocks: Mapping[str, Any] = raw.get("blocks", {})
        self._icons: Mapping[str, str] = raw.get("icons", {})
        # Reverse index: a single-block machine's display name -> its (block, meta), so a machine
        # type with no multiblock doc (the whole structure IS one block) still resolves to a cube.
        # A normalized index alongside it lets a plan's generically named machine match its
        # tier-prefixed manifest key without an exact-string collision (see ``mte_block``).
        self._mte_by_name: dict[str, tuple[str, int]] = {}
        for key, entry in self._blocks.items():
            name = entry.get("display_name")
            if entry.get("kind") == "mte" and name and "|" in key:
                block, meta = key.rsplit("|", 1)
                self._mte_by_name.setdefault(name, (block, int(meta)))
        self._mte_by_norm: dict[str, tuple[str, int]] = {}
        for name, block_meta in self._mte_by_name.items():
            self._mte_by_norm.setdefault(_normalize_name(name), block_meta)
        # Tiered-storage index: "super tank" (generic) -> the LOWEST "Super Tank I".."IX" variant,
        # since the plan names such families generically and the tiers share a skin (see mte_block).
        self._mte_tiered: dict[str, tuple[str, int]] = {}
        tiered_rank: dict[str, int] = {}
        for norm, block_meta in self._mte_by_norm.items():
            split = _split_tier_suffix(norm)
            if split is None:
                continue
            base, rank = split
            if base not in tiered_rank or rank < tiered_rank[base]:
                tiered_rank[base] = rank
                self._mte_tiered[base] = block_meta

    @classmethod
    def load(cls, path: str | Path) -> TextureManifest:
        """Parse ``manifest.json`` at ``path`` into a :class:`TextureManifest`."""
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def layers(self, block: str, meta: int, side: str, state: str = _STATE) -> list[dict[str, Any]]:
        """The ordered layer stack for ``(block, meta, side, state)``, or ``[]`` if unresolved.

        Falls back from the exact side to the block's ``"all"`` side entry (casings texture every
        face alike), and from the exact state to ``"inactive"`` then to whatever single state the
        entry carries, so a block that only stores one state still resolves.
        """
        entry = self._blocks.get(f"{block}|{meta}")
        if entry is None:
            return []
        sides = entry.get("sides", {})
        side_entry = sides.get(side) or sides.get("all")
        if side_entry is None:
            return []
        chosen = side_entry.get(state) or side_entry.get(_STATE)
        if chosen is None and side_entry:
            chosen = next(iter(side_entry.values()))
        return list(chosen or [])

    def icon_path(self, icon: str) -> str | None:
        """The path inside the mod jar for ``icon`` (e.g. ``assets/gregtech/.../NAME.png``)."""
        return self._icons.get(icon)

    def mte_block(self, display_name: str, tier: str | None = None) -> tuple[str, int] | None:
        """The ``(block, meta)`` of the single-block machine ``display_name`` (at ``tier``), or ``None``.

        Lets a machine type with no committed multiblock doc (a 1x1x1 machine whose whole structure
        is its own block) resolve to that block so it renders as one textured cube. A plan export
        names such a machine generically ("Forge Hammer", "Super Tank", "Chemical Plant"), but the
        manifest keys it by its full in-game name. Resolution tries, in order:

        1. the exact name (a plan already carrying the full name still works);
        2. a normalized (case/punctuation/whitespace) match;
        3. the voltage-tier prefix plus a ``Basic`` fallback (``Basic Forge Hammer``, ``_TIER_PREFIX``);
        4. the lowest tier of a tiered-storage family (``Super Tank`` -> ``Super Tank I``);
        5. a flavor-prefixed in-game name (``Chemical Plant`` -> ``ExxonMobil Chemical Plant``).

        A genuinely unknown machine returns ``None`` and keeps its placeholder box, never mis-mapped.
        """
        exact = self._mte_by_name.get(display_name)
        if exact is not None:
            return exact
        query = _normalize_name(display_name)
        normalized = self._mte_by_norm.get(query)
        if normalized is not None:
            return normalized
        for prefix in _tier_prefixes(tier):
            hit = self._mte_by_norm.get(_normalize_name(f"{prefix} {display_name}"))
            if hit is not None:
                return hit
        tiered = self._mte_tiered.get(query)
        if tiered is not None:
            return tiered
        return self._flavor_prefixed(query)

    def _flavor_prefixed(self, query_norm: str) -> tuple[str, int] | None:
        """A manifest name of the form ``"<flavor> <query>"`` (the query as a whole-word suffix).

        e.g. ``"chemical plant"`` -> ``"ExxonMobil Chemical Plant"``; ``"coke oven"`` ->
        ``"Industrial Coke Oven"``. Picks the shortest such name (fewest extra words) for
        determinism, so a plan's generic name matches a flavor-prefixed in-game one; ``None`` if
        nothing matches.
        """
        suffix = " " + query_norm
        candidates = [n for n in self._mte_by_norm if n.endswith(suffix)]
        if not candidates:
            return None
        return self._mte_by_norm[min(candidates, key=lambda n: (len(n), n))]


def load_multiblock_docs(data_dir: str | Path) -> dict[str, MultiblockDoc]:
    """Load every ``data/multiblocks/<name>.json`` under ``data_dir``, keyed for lookup.

    Each doc is indexed under BOTH its controller display name and its controller block key
    (``"<registry_name>@<meta>"``), because a plan can name a machine either way: an export from
    before gtnh-factory-flow #25 only has the localized recipe-map name, while a newer one carries
    the exact block id (see :func:`_machine_cubes`, which prefers the block key). The two key spaces
    cannot collide - a block key always ends in ``@<int>`` after a registry path, which no GT
    display name is - so one flat dict serves both without an ambiguity guard.

    Skips ``_meta.json`` and returns ``{}`` if the directory is absent, so a checkout without a
    committed dump texturizes nothing rather than failing. If two files claim one display name
    (schema-forbidden), the first sorted wins - the previewer only needs one representative form.
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
        docs.setdefault(f"{doc.controller.registry_name}@{doc.controller.meta}", doc)
    return docs


def primary_variant(doc: MultiblockDoc) -> Variant:
    """The variant standing for the machine's built form: the one placing the most blocks.

    Mirrors the dataset adapter's primary-variant choice (largest form, trigger stack as the
    deterministic tie-break), so the expanded cubes match the footprint the solver reserved.
    """
    return max(doc.variants, key=lambda v: (len(v.blocks), v.trigger_stack_size))


def variant_for_size(doc: MultiblockDoc, size: Sequence[int] | None) -> Variant:
    """The variant whose bbox is exactly ``size``, else :func:`primary_variant`.

    A parametric machine has many forms and the adapter already chose one, sizing it to the recipe
    (``MachinePhysical.footprint_for``). The reserved ``size`` on the scene machine IS that choice,
    so matching it here is what keeps the two passes in agreement without threading the decision
    through the IR. Getting this wrong is silent, not loud: ``expand_machine`` clamps every cube to
    the reserved box, so rendering a taller form than was reserved would quietly draw a truncated
    tower rather than fail.
    """
    if size is not None:
        want = tuple(size)
        for variant in doc.variants:
            if tuple(variant.bbox) == want:
                return variant
    return primary_variant(doc)


def _rotate(dx: int, dz: int, steps: int) -> tuple[int, int]:
    """Rotate a horizontal offset ``steps`` clockwise 90-degree turns (viewed from +Y)."""
    for _ in range(steps % 4):
        dx, dz = -dz, dx
    return dx, dz


def _rotate_side(side: int, steps: int) -> int:
    """Rotate a GT side index by ``steps`` clockwise turns; vertical faces (down/up) are unchanged."""
    vec = _SIDE_VEC.get(side)
    if vec is None:
        return side
    return _VEC_SIDE[_rotate(vec[0], vec[1], steps)]


@dataclass(frozen=True)
class BlockCube:
    """One constituent block ready to render: its world cell, identity, and the yaw applied."""

    cell: tuple[int, int, int]
    block: str
    meta: int
    steps: int  # clockwise yaw turns applied to orient the machine to its placed front


def _place_blocks(
    doc: MultiblockDoc, cell: list[int], steps: int, size: Sequence[int] | None = None
) -> list[BlockCube]:
    """Rotate the chosen variant's blocks by ``steps`` and land the min corner on ``cell``.

    ``size`` selects WHICH form to place (see :func:`variant_for_size`); without it the largest one
    stands, as before.
    """
    placed: list[tuple[tuple[int, int, int], str, int]] = []
    for b in variant_for_size(doc, size).blocks:
        dx, dy, dz = b.d
        rx, rz = _rotate(dx, dz, steps)
        placed.append(((rx, dy, rz), b.block, b.meta))
    if not placed:
        return []
    min_x = min(p[0][0] for p in placed)
    min_y = min(p[0][1] for p in placed)
    min_z = min(p[0][2] for p in placed)
    return [
        BlockCube(
            cell=(cell[0] + (x - min_x), cell[1] + (y - min_y), cell[2] + (z - min_z)),
            block=block,
            meta=meta,
            steps=steps,
        )
        for (x, y, z), block, meta in placed
    ]


def _within_footprint(pos: tuple[int, int, int], origin: list[int], size: list[int]) -> bool:
    """Whether cell ``pos`` lies inside the reserved footprint ``[origin, origin + size)``."""
    return all(origin[i] <= pos[i] < origin[i] + size[i] for i in range(3))


def expand_machine(machine: Mapping[str, Any], doc: MultiblockDoc) -> list[BlockCube]:
    """Expand a scene machine into per-block cubes, kept strictly inside its reserved footprint.

    The dump builds every controller facing NORTH; a machine placed facing ``front`` yaw-rotates its
    blocks so the controller's front overlay points the way the solver oriented it, then translates
    the variant's minimum corner onto the placement ``cell``.

    **No overlap (wall-sharing is out of scope here).** The solver reserves the *unrotated* footprint
    (``occupied_cells`` does not yet rotate non-cubic footprints, a documented TODO), so a yaw that
    would push a non-cubic machine's blocks past that footprint is dropped in favour of the native
    orientation, which fills the reserved footprint exactly. A final hard clamp discards any cube
    still outside the footprint, so one machine's blocks can never spill into a neighbour's cells.
    """
    cell = machine["cell"]
    size = machine.get("size", [1, 1, 1])
    steps = _FRONT_CW_STEPS.get(str(machine.get("front", "north")), 0)
    cubes = _place_blocks(doc, cell, steps, size)
    if steps and not all(_within_footprint(c.cell, cell, size) for c in cubes):
        cubes = _place_blocks(
            doc, cell, 0, size
        )  # native orientation fits the reserved footprint exactly
    return [c for c in cubes if _within_footprint(c.cell, cell, size)]


def _glyph_steps(machine: Mapping[str, Any], auto_out_face: Mapping[str, str] | None) -> int:
    """Clockwise yaw turns that orient a single-block machine's front glyph to the manifest NORTH.

    A boundary-storage block (Super Tank / Super Chest) auto-outputs *from its front face*, so its
    output glyph (``OVERLAY_STANK`` / ``OVERLAY_SCHEST``) should face the auto-output direction. The
    placer's ``front`` does not track the eject face (it defaults every machine to NORTH), which
    would leave that glyph pointing away from where the block actually ejects, so a storage block
    with a *horizontal* auto-output orients to that face instead. Every other machine (and a storage
    block with a vertical eject, which a side glyph can't point at) keeps its placed front.
    """
    if machine.get("role") == "storage" and auto_out_face:
        face = auto_out_face.get(str(machine.get("id")))
        if face in _FRONT_CW_STEPS:  # horizontal eject only
            return _FRONT_CW_STEPS[face]
    return _FRONT_CW_STEPS.get(str(machine.get("front", "north")), 0)


def _machine_cubes(
    machine: Mapping[str, Any],
    docs: Mapping[str, MultiblockDoc],
    manifest: TextureManifest,
    auto_out_face: Mapping[str, str] | None = None,
) -> list[BlockCube]:
    """The per-block cubes for a machine: its multiblock doc if committed, else a single-block cube.

    The doc is looked up by the machine's ``block_key`` FIRST and by its ``type`` only as a
    fallback, mirroring :meth:`~gtnh_solver.dataset.multiblocks.PhysicalDataset.get`: the block key
    is an exact controller identity, while ``type`` is the exporter's localized recipe-map name that
    for a GT++ machine never matches the dump's controller-block name. Both resolve through the same
    dict (see :func:`load_multiblock_docs`). Keeping the two lookups in the same precedence order is
    load-bearing - the adapter reserved the footprint via ``PhysicalDataset.get``, so if this pass
    resolved a *different* doc the rendered cubes would not match the reserved box.

    A machine whose type has a dumped :class:`MultiblockDoc` expands to that structure. A genuine
    single-block machine (a 1x1x1 footprint) is the trivial one-cube case, resolved by its plan name
    plus voltage tier against the manifest's tier-prefixed keys (see :meth:`TextureManifest.mte_block`).
    A doc-less MULTIblock (a bigger footprint whose structure failed extraction, e.g.
    the dynamic-height Distillation Tower) must NOT collapse to a lone controller cube - it yields
    nothing and keeps its placeholder box, so its true reserved footprint still shows.

    ``auto_out_face`` (machine id -> auto-output face) lets a boundary-storage block point its output
    glyph the way it actually ejects rather than its placed front (see :func:`_glyph_steps`).
    """
    doc = docs.get(machine.get("block_key") or "") or docs.get(machine["type"])
    if doc is not None:
        return expand_machine(machine, doc)
    single = manifest.mte_block(machine["type"], machine.get("voltage_tier"))
    if single is not None and tuple(machine.get("size", (1, 1, 1))) == (1, 1, 1):
        block, meta = single
        cell = machine["cell"]
        steps = _glyph_steps(machine, auto_out_face)
        return [BlockCube((cell[0], cell[1], cell[2]), block, meta, steps)]
    return []


def _face_icons(cube: BlockCube, manifest: TextureManifest) -> tuple[list[str | None], set[str]]:
    """The six per-face texture keys for ``cube`` (three.js slot order) and the icons they need.

    A face's key is ``"block|meta|side|state"`` when that face resolves to at least one manifest
    layer, else ``None``. Yaw rotates which GT side a face's texture comes from, so the overlay that
    the dump put on the controller's NORTH face follows the machine's placed front. Returns the key
    list plus the set of iconset names any resolved face references (to fetch and bake).
    """
    faces: list[str | None] = [None] * _FACE_SLOTS
    needed: set[str] = set()
    for side in range(_FACE_SLOTS):
        source_side = _rotate_side(side, -cube.steps)  # which native GT side supplies this face
        layers = manifest.layers(cube.block, cube.meta, _SIDE_NAMES[source_side])
        if not layers:
            continue
        faces[_GT_SIDE_TO_THREE_SLOT[side]] = (
            f"{cube.block}|{cube.meta}|{_SIDE_NAMES[source_side]}|{_STATE}"
        )
        needed.update(layer["icon"] for layer in layers)
    return faces, needed


def _png_data_uri(png: bytes) -> str:
    """Encode raw PNG bytes as a self-contained ``data:image/png;base64,...`` URI."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def texturize_scene(
    scene: dict[str, Any],
    *,
    multiblocks_dir: str | Path | None = None,
    manifest_path: str | Path | None = None,
    version: str | None = None,
    png_provider: PngProvider | None = None,
) -> TextureSummary:
    """Expand every resolvable machine into per-block textured cubes, in place, and embed the PNGs.

    Loads the docs + layered manifest, expands each machine whose type has a committed doc, resolves
    and bakes each cube face, and writes ``scene["blocks"]`` (the per-block cubes, each carrying a
    six-slot ``texture`` list of pool keys) plus ``scene["textures"]`` (pool key -> baked ``data:``
    URI). Every expanded machine is flagged ``expanded`` so the viewer draws its cubes instead of a
    box; machines with no doc (or no baked face) keep their placeholder box. Missing data, no PNGs,
    or no Pillow all degrade to all-placeholder. Returns a :class:`TextureSummary`.

    Each face is also baked in its running (``active``) state, and ``scene["texturesActive"]`` maps a
    pool key to the running-state ``data:`` URI **only where that bake differs** from the idle one
    (an ``_ACTIVE`` overlay); an idle-identical face carries no second texture, so the viewer's state
    toggle reuses the one image and the embedded page never bloats for faces that look the same at
    rest and running. The default display stays idle.
    """
    all_types = tuple(sorted({m["type"] for m in scene["machines"]}))
    mb_dir = (
        resolve_dataset_path("multiblocks", version=version)
        if multiblocks_dir is None
        else Path(multiblocks_dir)
    )
    mf_path = (
        resolve_dataset_path("textures/manifest.json", version=version)
        if manifest_path is None
        else Path(manifest_path)
    )

    scene.setdefault("blocks", [])
    scene.setdefault("textures", {})
    scene.setdefault("texturesActive", {})
    docs = load_multiblock_docs(mb_dir)
    if not docs or not Path(mf_path).is_file():
        _log.info("textures: no dataset/manifest; all %d types placeholder", len(all_types))
        return TextureSummary((), all_types, 0, 0)

    manifest = TextureManifest.load(mf_path)

    # A boundary-storage block's output glyph should face the way it auto-outputs, not the placer's
    # default front (see _glyph_steps). First auto-output per source machine wins (storage blocks
    # have a single output).
    auto_out_face: dict[str, str] = {}
    for ac in scene.get("autoConnections", []):
        auto_out_face.setdefault(ac["source"], ac["sourceFace"])

    # Expand every machine with a committed doc (or a single-block manifest entry) into per-block
    # cubes. A cube whose faces do not resolve is kept anyway - it renders as a neutral placeholder
    # block - so the machine's full structure shows; the plan forbids collapsing to one stretched box
    # even when some textures are missing (section 5.6).
    cubes: list[dict[str, Any]] = []
    needed_icons: set[str] = set()
    key_layers: dict[str, list[dict[str, Any]]] = {}  # pool key -> idle layer stack, deduped
    # Only faces whose running stack differs from idle - the ones that can bake a distinct active
    # texture - are collected here (a plain casing is identical in both states and skipped).
    key_layers_active: dict[str, list[dict[str, Any]]] = {}
    for machine in scene["machines"]:
        machine_cubes = _machine_cubes(machine, docs, manifest, auto_out_face)
        if not machine_cubes:
            continue  # no doc and not a known single-block machine -> keep the placeholder box
        machine["expanded"] = True
        for cube in machine_cubes:
            faces, needed = _face_icons(cube, manifest)
            needed_icons |= needed
            for key in faces:
                if key is not None and key not in key_layers:
                    block, meta_s, side, state = key.split("|")
                    idle = manifest.layers(block, int(meta_s), side, state)
                    key_layers[key] = idle
                    active = manifest.layers(block, int(meta_s), side, _STATE_ACTIVE)
                    if active != idle:
                        key_layers_active[key] = active
                        needed_icons.update(layer["icon"] for layer in active)
            cubes.append(
                {
                    "cell": list(cube.cell),
                    "machine": machine["id"],
                    "block": cube.block,
                    "meta": cube.meta,
                    "texture": faces,
                }
            )

    # Fetch only the icons actually referenced, then bake each distinct (block, meta, side, state)
    # face once into a flat PNG data URI pool. A scene of undocumented types fetches nothing.
    icon_paths = {i: p for i in needed_icons if (p := manifest.icon_path(i)) is not None}
    icon_png = png_provider(icon_paths) if (png_provider is not None and icon_paths) else {}

    pool: dict[str, str] = {}
    pool_active: dict[str, str] = {}
    try:
        baked_idle: dict[str, bytes] = {}
        for key, layers in key_layers.items():
            baked = bake_layers(layers, icon_png)
            if baked is not None:
                baked_idle[key] = baked
                pool[key] = _png_data_uri(baked)
        # Bake the running state only for faces whose stack differs, and keep it only where the
        # bytes actually differ from the idle bake AND the idle face itself baked (so the toggle
        # never targets a placeholder face). Identical bakes are deduped away - the viewer reuses
        # the idle texture there.
        for key, layers in key_layers_active.items():
            idle_png = baked_idle.get(key)
            if idle_png is None:
                continue
            baked = bake_layers(layers, icon_png)
            if baked is not None and baked != idle_png:
                pool_active[key] = _png_data_uri(baked)
    except BakeUnavailableError as exc:
        _log.warning("textures: %s; falling back to placeholder boxes", exc)
        for machine in scene["machines"]:
            machine.pop("expanded", None)
        return TextureSummary((), all_types, 0, 0)

    # Null out face keys that did not bake so the viewer draws a neutral placeholder there, but keep
    # every cube so the machine's full block structure renders (never a single stretched box).
    for rendered in cubes:
        rendered["texture"] = [key if key in pool else None for key in rendered["texture"]]
    scene["blocks"] = cubes
    scene["textures"] = pool
    scene["texturesActive"] = pool_active
    expanded_types = {m["type"] for m in scene["machines"] if m.get("expanded")}
    placeholder = tuple(t for t in all_types if t not in expanded_types)
    summary = TextureSummary(
        textured_types=tuple(sorted(expanded_types)),
        placeholder_types=placeholder,
        block_cubes=len(cubes),
        embedded_icons=len(pool),
        embedded_active_icons=len(pool_active),
    )
    _log.info(
        "textures: %d/%d machine types expanded to %d textured cubes (%s); placeholder: %s; "
        "%d baked face PNG(s), %d running-state override(s)",
        len(summary.textured_types),
        len(all_types),
        summary.block_cubes,
        ", ".join(summary.textured_types) or "none",
        ", ".join(summary.placeholder_types) or "none",
        summary.embedded_icons,
        summary.embedded_active_icons,
    )
    return summary
