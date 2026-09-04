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

**Routes are skinned the same way** (#4). A cable or pipe is materially isotropic - one sprite on
all six faces, its shape coming from geometry - so it needs only the two looks GT itself draws: the
**open end** the cable runs out of (the wire core plus its insulation ring) and the **closed** face
it does not (solid insulation, or the pipe's barrel). Two bakes per block, shared by every cell of
that gauge, riding the same icon fetch and the same pool as the machine faces.

The pipeline is pure and unit-tested end to end given PNG *bytes*; the 135 MB jar fetch is the one
untested shim (:mod:`.jar`), injected as ``png_provider``. **Graceful degradation is the contract**:
a machine with no committed doc, or whose blocks all fail to resolve, keeps its flat placeholder box;
a single unresolved face on an otherwise-textured cube draws the missing-texture checkerboard. A
route degrades differently and deliberately: an unresolved cable keeps its **flat coloured bar**,
never a checkerboard. A checkerboarded casing reads as "this block has no sprite" against the blocks
around it that do; a checkerboarded noodle threaded through a layout reads as damage. Nothing
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

#: The two looks a cable or pipe has, which is GT's own axis for them: the face an open end shows
#: (``materialicons/<SET>/wire`` plus ``INSULATION_<size>`` on a cable, the bore sprite on a pipe)
#: and the face a closed side shows (``INSULATION_FULL``, or the pipe's barrel). The extractor
#: writes both under ``sides.all`` for an isotropic pipe; see docs/DOMAIN.md.
_PIPE_ROLES = ("open", "closed")

#: The side key an isotropic pipe's layers live under. A pipe the extractor found **not** isotropic
#: is written per side and files a gap, so this lookup then resolves nothing and the route keeps its
#: honest flat bar - rather than one face's sprite being painted on all six.
_PIPE_SIDE = "all"

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
#: The scene's lowercase facing names to GT side indices, so a hatch's own facing resolves to the
#: manifest's side key. Vertical facings are here too, which is the whole point: they are 75% of
#: sand's terminals and could not be expressed at all before.
_FACING_TO_SIDE = {"down": 0, "up": 1, "north": 2, "south": 3, "west": 4, "east": 5}
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

#: The GT class behind each ``HatchElement`` kind the solver places, from that enum's own
#: ``mteClasses()``. Joining on the class rather than the display name is what makes the lookup
#: robust: GT names these two different ways ("Input Bus (LV)" against "LV Energy Hatch"), and a
#: subclass (an ME stocking bus, a GT++ hatch) keeps its parent's kind exactly as GT's adders do.
HATCH_KIND_BY_CLASS = {
    "gregtech.api.metatileentity.implementations.MTEHatchInputBus": "InputBus",
    "gregtech.api.metatileentity.implementations.MTEHatchOutputBus": "OutputBus",
    "gregtech.api.metatileentity.implementations.MTEHatchInput": "InputHatch",
    "gregtech.api.metatileentity.implementations.MTEHatchOutput": "OutputHatch",
    "gregtech.api.metatileentity.implementations.MTEHatchEnergy": "Energy",
    "gregtech.api.metatileentity.implementations.MTEHatchDynamo": "Dynamo",
    "gregtech.api.metatileentity.implementations.MTEHatchMaintenance": "Maintenance",
    "gregtech.api.metatileentity.implementations.MTEHatchMuffler": "Muffler",
}

#: The voltage ladder as it appears inside a hatch's display name, low to high. Longest-first in
#: the pattern so ``LuV`` is never read as ``L`` + ``uV``; ``\b`` keeps ``LV`` out of ``ULV``.
_TIER_LADDER = (
    "ULV",
    "LV",
    "MV",
    "HV",
    "EV",
    "IV",
    "LuV",
    "ZPM",
    "UV",
    "UHV",
    "UEV",
    "UIV",
    "UMV",
    "UXV",
    "MAX",
)
_TIER_TOKEN = re.compile(r"\b(" + "|".join(sorted(_TIER_LADDER, key=len, reverse=True)) + r")\b")

#: The dumped side a hatch's overlays live on. ``TextureDumper`` pins ``aFacing`` to NORTH for
#: every MTE it walks, so the front stack is always recorded there whatever the block's real front.
_FRONT_IN_DUMP = "NORTH"

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
    #: ``"<block>|<meta>"`` for every constituent block that resolved NO face at all, so its cubes
    #: draw the missing-texture checkerboard. Reported rather than swallowed because the CLI is where
    #: a gap is actionable: the machine is
    #: still flagged ``expanded`` and keeps no placeholder label, so nothing else surfaces the gap
    #: (GitHub #98). A non-empty list means the texture manifest needs a re-dump, not that the
    #: structure is wrong.
    unskinned_blocks: tuple[str, ...] = ()
    #: Route cells drawn as real GT cable/pipe blocks, and cells that kept the flat coloured bar.
    route_cells_textured: int = 0
    route_cells_flat: int = 0
    #: Dataset names (``"cable.tin.02"``) a route asked for that the manifest could not supply - a
    #: missing entry, or a pipe the extractor found non-isotropic. Reported for the same reason as
    #: ``unskinned_blocks``: the flat bar is a *correct* render, so nothing else would say the
    #: manifest is short, and the fix is a re-dump rather than a code change.
    unresolved_route_blocks: tuple[str, ...] = ()


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
        # Hatch index: (HatchElement kind, tier) -> (block, meta). Keyed off the MTE's
        # ``source_class`` rather than its display name, because the names are not one shape -
        # "Input Bus (LV)" against "LV Energy Hatch" - while the class is exactly what
        # ``HatchElement.mteClasses()`` names, so the join is GT's own. Ties break on the shortest
        # display name, which picks the plain "Maintenance Hatch" over the "Auto Maintenance Hatch"
        # that shares its class (and needs LuV to craft).
        self._hatches: dict[tuple[str, str], tuple[str, int]] = {}
        hatch_names: dict[tuple[str, str], str] = {}
        for key, entry in self._blocks.items():
            kind = HATCH_KIND_BY_CLASS.get(str(entry.get("source_class", "")))
            name = entry.get("display_name")
            if kind is None or not name or "|" not in key:
                continue
            match = _TIER_TOKEN.search(name)
            index = (kind, match.group(0) if match else "")
            if index in hatch_names and len(hatch_names[index]) <= len(name):
                continue
            block, meta = key.rsplit("|", 1)
            hatch_names[index] = name
            self._hatches[index] = (block, int(meta))
        # Pipe index: a cable/pipe dataset name ("cable.tin.02", "gt_pipe_bronze") -> its
        # (block, meta). EXACT names only, with none of ``mte_block``'s normalizing fallback ladder:
        # both sides of this join are generated from the same policy table (``dataset/pipes.py``),
        # so a near-miss means the manifest is short, not that the name needs massaging - and a
        # fuzzy match here is precisely how a route would render as a confidently wrong cable.
        self._pipes_by_name: dict[str, tuple[str, int]] = {}
        for key, entry in self._blocks.items():
            name = entry.get("display_name")
            if entry.get("kind") == "pipe" and name and "|" in key:
                block, meta = key.rsplit("|", 1)
                self._pipes_by_name.setdefault(name, (block, int(meta)))
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

    def hatch_block(self, kind: str, tier: str | None) -> tuple[str, int] | None:
        """The ``(block, meta)`` of the ``kind`` hatch at ``tier``, or ``None`` if unresolvable.

        Falls back from the exact tier to the family's **untiered** entry (the maintenance hatch is
        the only one), then to the nearest tier at or below the one asked for, then to the lowest
        there is. GT does not tier every family the whole way up - the muffler stops at UHV - so a
        machine above a family's ceiling must still resolve to *a* block rather than lose its skin;
        the block chosen is always one that exists, and the previewer draws it at the cell the
        solver picked either way.
        """
        exact = self._hatches.get((kind, tier or ""))
        if exact is not None:
            return exact
        untiered = self._hatches.get((kind, ""))
        if untiered is not None:
            return untiered
        ladder = [t for t in _TIER_LADDER if (kind, t) in self._hatches]
        if not ladder:
            return None
        wanted = _TIER_LADDER.index(tier) if tier in _TIER_LADDER else len(_TIER_LADDER)
        below = [t for t in ladder if _TIER_LADDER.index(t) <= wanted]
        return self._hatches[(kind, below[-1] if below else ladder[0])]

    def pipe_layers(self, block: str, meta: int, role: str) -> list[dict[str, Any]]:
        """The layer stack for one look of a cable or pipe, or ``[]`` - an EXACT lookup.

        Deliberately not :meth:`layers`, whose fallbacks are wrong here in both directions. Its
        side fallback (exact side, else ``"all"``) would let a pipe the extractor found *not*
        isotropic - written per side, with a gap filed - resolve one face's sprite onto all six.
        Its state fallback (any single stored state when the asked-for one is absent) would answer
        "closed" with the open end's stack, drawing a cable that is open on every face. Both are
        reasonable for a casing and neither is here, so this reads ``sides.all[role]`` or nothing
        and lets the caller keep the flat bar.
        """
        entry = self._blocks.get(f"{block}|{meta}")
        if entry is None:
            return []
        side = entry.get("sides", {}).get(_PIPE_SIDE)
        if not isinstance(side, dict):
            return []
        return list(side.get(role) or [])

    def pipe_block(self, display_name: str) -> tuple[str, int] | None:
        """The ``(block, meta)`` of the cable or pipe named ``display_name``, or ``None``.

        The analogue of :meth:`hatch_block` for routes, and deliberately the strictest lookup in
        this class: an exact name or nothing. ``dataset/pipes.py`` generates the name a route
        publishes and the extractor recorded the same string, so there is no locale, tier prefix or
        flavour word to reconcile - and a route that resolves to *some other* cable is the
        unrecoverable failure (docs/dataset-extraction/texture-resolution.md), where a route that
        resolves to nothing merely keeps its flat bar.
        """
        return self._pipes_by_name.get(display_name)

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
    """One constituent block ready to render: its world cell, identity, and how it is oriented.

    A block is oriented one of two ways, and they are not interchangeable. An ordinary structure
    block turns with its **machine**: ``steps`` is the machine's yaw, and the texture for a given
    world face is read from whichever GT side that yaw maps it back to. A **hatch** turns on its
    own: ``facing`` names the world side its front points at, chosen by the router per hatch, and
    the yaw is irrelevant to it - see :func:`_face_icons`.

    ``idle_state`` / ``active_state`` are the dumped state names to read for the resting and
    running looks. They exist because the maintenance hatch's are inverted: GT flips it to
    ``active`` the moment it joins a formed multiblock, so its dumped ``inactive`` stack is
    ``OVERLAY_MAINTENANCE + OVERLAY_DUCTTAPE`` - the *broken* look - and the previewer's plain
    default would draw every machine in the line as needing repair.
    """

    cell: tuple[int, int, int]
    block: str
    meta: int
    steps: int  # clockwise yaw turns applied to orient the machine to its placed front
    facing: str | None = None  # a hatch's own world-space front side, e.g. "WEST"
    idle_state: str = _STATE
    active_state: str = _STATE_ACTIVE


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


def expand_machine(
    machine: Mapping[str, Any], doc: MultiblockDoc, manifest: TextureManifest | None = None
) -> list[BlockCube]:
    """Expand a scene machine into per-block cubes, kept strictly inside its reserved footprint.

    The dump builds every controller facing NORTH; a machine placed facing ``front`` yaw-rotates its
    blocks so the controller's front overlay points the way the solver oriented it, then translates
    the variant's minimum corner onto the placement ``cell``.

    **No overlap (wall-sharing is out of scope here).** The scene's ``size`` is the reserved box *as
    placed* - the solver rotates it with the machine - so a yaw no longer pushes a non-cubic
    machine's blocks outside it and the old fall-back to the native orientation is gone. The hard
    clamp stays: any cube outside the reserved box is discarded, so one machine's blocks can never
    spill into a neighbour's cells.

    With a ``manifest``, the machine's ``hatches`` then **replace** the casing cubes they sit on: a
    hatch is not an extra block, it is one of the structure's own cells built as something else,
    which is why it spends the casing budget. Each carries its own facing rather than the machine's
    yaw. Without a manifest the structure is expanded bare, which is what the doc-only callers want.
    """
    cell = machine["cell"]
    size = machine.get("size", [1, 1, 1])
    steps = _FRONT_CW_STEPS.get(str(machine.get("front", "north")), 0)
    cubes = [
        c for c in _place_blocks(doc, cell, steps, size) if _within_footprint(c.cell, cell, size)
    ]
    if manifest is None:
        return cubes
    return _substitute_hatches(cubes, machine, manifest)


def _substitute_hatches(
    cubes: list[BlockCube], machine: Mapping[str, Any], manifest: TextureManifest
) -> list[BlockCube]:
    """Replace each casing cube the machine's hatches occupy with that hatch's own block.

    A hatch whose block cannot be resolved is left as plain casing rather than dropped: the cell is
    genuinely occupied either way, and losing the cube would open a hole in the structure. That is
    the same graceful-degradation contract the rest of the module keeps.
    """
    hatches = machine.get("hatches") or ()
    if not hatches:
        return cubes
    tier = machine.get("voltage_tier")
    replacement: dict[tuple[int, int, int], BlockCube] = {}
    for hatch in hatches:
        found = manifest.hatch_block(str(hatch["kind"]), tier if isinstance(tier, str) else None)
        if found is None:
            continue
        block, meta = found
        at = (int(hatch["cell"][0]), int(hatch["cell"][1]), int(hatch["cell"][2]))
        idle, active = _hatch_states(str(hatch["kind"]))
        replacement[at] = BlockCube(
            cell=at,
            block=block,
            meta=meta,
            steps=0,  # a hatch is oriented by its own facing, never by the machine's yaw
            facing=_SIDE_NAMES[_FACING_TO_SIDE[str(hatch["facing"])]],
            idle_state=idle,
            active_state=active,
        )
    return [replacement.pop(c.cell, c) for c in cubes] + list(replacement.values())


def _hatch_states(kind: str) -> tuple[str, str]:
    """Which dumped states are this hatch's resting and running looks.

    Only the maintenance hatch differs, and it is fully inverted: GT flips it to ``active`` the
    moment it joins a formed multiblock, so the dumped ``inactive`` stack is
    ``OVERLAY_MAINTENANCE + OVERLAY_DUCTTAPE`` - a hatch that needs repair. Reading the states
    straight would draw every machine in the line duct-taped, which is not merely ugly: it is the
    one skin a builder is meant to react to. It has no meaningful running look either, so both map
    to ``active``.
    """
    if kind == "Maintenance":
        return _STATE_ACTIVE, _STATE_ACTIVE
    return _STATE, _STATE_ACTIVE


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
    A doc-less MULTIblock (a bigger footprint whose structure failed extraction) must NOT collapse to
    a lone controller cube - it yields nothing and keeps its placeholder box, so its true reserved
    footprint still shows.

    ``auto_out_face`` (machine id -> auto-output face) lets a boundary-storage block point its output
    glyph the way it actually ejects rather than its placed front (see :func:`_glyph_steps`).
    """
    doc = docs.get(machine.get("block_key") or "") or docs.get(machine["type"])
    if doc is not None:
        return expand_machine(machine, doc, manifest)
    single = manifest.mte_block(machine["type"], machine.get("voltage_tier"))
    if single is not None and tuple(machine.get("size", (1, 1, 1))) == (1, 1, 1):
        block, meta = single
        cell = machine["cell"]
        steps = _glyph_steps(machine, auto_out_face)
        return [BlockCube((cell[0], cell[1], cell[2]), block, meta, steps)]
    return []


def _face_icons(
    cube: BlockCube, manifest: TextureManifest
) -> tuple[list[str | None], dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]]:
    """The six per-face texture keys for ``cube`` (three.js slot order) and the stacks behind them.

    A face's key is ``None`` when it resolves to no manifest layer. Returns the key list plus, per
    distinct key, the ``(idle, running)`` layer stacks - handed back rather than re-derived from the
    key by the caller, because a hatch's stack is a *splice* that the key alone cannot reproduce.

    **An ordinary block turns with its machine.** The yaw says which native GT side supplies a given
    world face, so the overlay the dump put on the controller's NORTH face follows the placed front.

    **A hatch turns on its own**, and needs no rotation at all: the router already chose its facing
    in world space, so the world face IS the side to look up. What it needs instead is a splice,
    because the extractor pins ``aFacing`` to NORTH for every MTE it walks and therefore only ever
    recorded the front overlays on that one side. ``MTEHatch.getTexture`` computes its background
    without consulting either ``side`` or ``aFacing`` and adds the overlays only where
    ``side == aFacing``, so::

        face(side) = own background                              if side != facing
                   = own background ++ NORTH's overlays          if side == facing

    Both halves are facing-invariant, which is what makes this exact rather than approximate - a
    six-facing re-dump would write byte-identical stacks. Taking the background from the target
    side's **own** layer 0 is essential and not a detail: UP and DOWN carry ``MACHINE_<TIER>_TOP`` /
    ``_BOTTOM`` against the horizontals' ``_SIDE`` in every hatch entry in the pack, so reading it
    off a fixed side would be wrong on all of them - and 75% of sand's terminals are vertical.
    """
    faces: list[str | None] = [None] * _FACE_SLOTS
    stacks: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for side in range(_FACE_SLOTS):
        if cube.facing is None:
            source = _SIDE_NAMES[_rotate_side(side, -cube.steps)]
            idle = manifest.layers(cube.block, cube.meta, source, cube.idle_state)
            running = manifest.layers(cube.block, cube.meta, source, cube.active_state)
            key = f"{cube.block}|{cube.meta}|{source}|{cube.idle_state}"
        else:
            source = _SIDE_NAMES[side]
            idle = _hatch_layers(manifest, cube, source, cube.idle_state)
            running = _hatch_layers(manifest, cube, source, cube.active_state)
            # The facing MUST be in the key. Without it an UP-facing and a NORTH-facing hatch of
            # the same type collide in the texture pool and one silently gets the other's bake.
            key = f"{cube.block}|{cube.meta}|{source}|{cube.idle_state}|{cube.facing}"
        if not idle:
            continue
        faces[_GT_SIDE_TO_THREE_SLOT[side]] = key
        stacks[key] = (idle, running)
    return faces, stacks


def _hatch_layers(
    manifest: TextureManifest, cube: BlockCube, render_side: str, state: str
) -> list[dict[str, Any]]:
    """One hatch face: its own background, plus the dump's front overlays where it faces us.

    Layer 0 alone on a non-facing side, never the recorded stack: the dump's own NORTH entry
    already carries the front overlays, so handing it back would leave a hatch wearing its sign on
    whichever side happened to be north regardless of where the router pointed it.
    """
    own = manifest.layers(cube.block, cube.meta, render_side, state)
    if not own:
        return own
    if render_side != cube.facing:
        return [own[0]]
    front = manifest.layers(cube.block, cube.meta, _FRONT_IN_DUMP, state)
    # front[1:] is safe: a hatch's front layer 0 is the same background as its horizontal sides in
    # every entry in the pack, so the slice never eats an overlay.
    return [own[0], *front[1:]]


def _png_data_uri(png: bytes) -> str:
    """Encode raw PNG bytes as a self-contained ``data:image/png;base64,...`` URI."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _route_face_keys(
    scene: Mapping[str, Any],
    manifest: TextureManifest,
    key_layers: dict[str, list[dict[str, Any]]],
    needed_icons: set[str],
) -> tuple[dict[str, dict[str, str]], set[str]]:
    """Resolve every cable/pipe the scene's routes name into its two baked-face keys.

    Returns ``{dataset name: {role: pool key}}`` for the blocks that resolved, and the set of names
    that did not. Fills ``key_layers`` with the stacks to bake and adds their icons to
    ``needed_icons``, which the machine pass shares, so the whole page is still one jar fetch and
    one texture pool - but the stacks are kept apart from the machine ones because they bake
    differently (``normalize_tint``; see :func:`~gtnh_solver.previewer.bake.bake_layers`).

    A block is taken **only if both roles resolve**. Half a pipe - an open end with no barrel - is
    the render that looks like a bug rather than like missing data, and the flat bar beside it is
    already a correct answer.
    """
    resolved: dict[str, dict[str, str]] = {}
    unresolved: set[str] = set()
    for route in scene.get("routes", []):
        for cell in route.get("cells", []):
            name = cell.get("block")
            if not name or name in resolved or name in unresolved:
                continue
            found = manifest.pipe_block(name)
            if found is None:
                unresolved.add(name)
                continue
            block, meta = found
            roles: dict[str, str] = {}
            stacks: dict[str, list[dict[str, Any]]] = {}
            for role in _PIPE_ROLES:
                layers = manifest.pipe_layers(block, meta, role)
                if not layers:
                    break
                roles[role] = f"{block}|{meta}|{role}"
                stacks[roles[role]] = layers
            if len(roles) != len(_PIPE_ROLES):
                unresolved.add(name)
                continue
            resolved[name] = roles
            for key, layers in stacks.items():
                key_layers.setdefault(key, layers)
                needed_icons.update(layer["icon"] for layer in layers)
    return resolved, unresolved


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
    # The manifest is the one hard requirement - nothing resolves without it. A missing *multiblock*
    # dump is no longer fatal to the whole pass: single-block machines resolve straight off the
    # manifest, and so do routes, neither of which needs a structure doc.
    if not Path(mf_path).is_file():
        _log.info("textures: no manifest; all %d types placeholder", len(all_types))
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
    # Constituent blocks that resolve no face at all (see TextureSummary).
    unskinned: set[str] = set()
    for machine in scene["machines"]:
        machine_cubes = _machine_cubes(machine, docs, manifest, auto_out_face)
        if not machine_cubes:
            continue  # no doc and not a known single-block machine -> keep the placeholder box
        machine["expanded"] = True
        for cube in machine_cubes:
            faces, stacks = _face_icons(cube, manifest)
            if all(face is None for face in faces):
                unskinned.add(f"{cube.block}|{cube.meta}")
            for key, (idle, running) in stacks.items():
                needed_icons.update(layer["icon"] for layer in idle)
                if key in key_layers:
                    continue
                key_layers[key] = idle
                if running != idle:
                    key_layers_active[key] = running
                    needed_icons.update(layer["icon"] for layer in running)
            cubes.append(
                {
                    "cell": list(cube.cell),
                    "machine": machine["id"],
                    "block": cube.block,
                    "meta": cube.meta,
                    "texture": faces,
                }
            )

    # Routes join the machine pass here, before the fetch, so a page with cables in it still makes
    # exactly one jar call and shares one texture pool with the casings.
    route_key_layers: dict[str, list[dict[str, Any]]] = {}
    route_faces, unresolved_routes = _route_face_keys(
        scene, manifest, route_key_layers, needed_icons
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
        # Cable and pipe sprites are greyscale - the material IS the multiply (docs/DOMAIN.md) - so
        # they bake with the tint applied raw, GT's own arithmetic. Under the casing normalisation a
        # cable's [64,64,64] insulation becomes identity, which both washes the block out and
        # collapses the dark insulated face into the bright open end until the two look alike.
        for key, layers in route_key_layers.items():
            baked = bake_layers(layers, icon_png, normalize_tint=False)
            if baked is not None:
                pool[key] = _png_data_uri(baked)
    except BakeUnavailableError as exc:
        _log.warning("textures: %s; falling back to placeholder boxes", exc)
        for machine in scene["machines"]:
            machine.pop("expanded", None)
        return TextureSummary((), all_types, 0, 0)

    # Hand each route cell the two pool keys its block baked to, or nothing - in which case the
    # viewer keeps the flat coloured bar it drew before any of this. Both roles or neither.
    textured_cells = flat_cells = 0
    for route in scene.get("routes", []):
        for cell in route.get("cells", []):
            roles = route_faces.get(cell.get("block") or "")
            usable = roles is not None and all(key in pool for key in roles.values())
            cell["tex"] = dict(roles) if usable and roles is not None else None
            textured_cells, flat_cells = (
                (textured_cells + 1, flat_cells) if usable else (textured_cells, flat_cells + 1)
            )

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
        unskinned_blocks=tuple(sorted(unskinned)),
        route_cells_textured=textured_cells,
        route_cells_flat=flat_cells,
        unresolved_route_blocks=tuple(sorted(unresolved_routes)),
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
    if textured_cells or flat_cells:
        _log.info(
            "textures: %d/%d route cell(s) drawn as real cable/pipe blocks%s",
            textured_cells,
            textured_cells + flat_cells,
            f"; unresolved: {', '.join(summary.unresolved_route_blocks)}"
            if summary.unresolved_route_blocks
            else "",
        )
    if summary.unskinned_blocks:
        # The checkerboard makes the gap visible in the render; this warning is what makes it
        # actionable in a build log. Warn, not info: it means the manifest needs a re-dump.
        _log.warning(
            "textures: %d constituent block type(s) have no sprite and draw the missing-texture "
            "checkerboard: %s",
            len(summary.unskinned_blocks),
            ", ".join(summary.unskinned_blocks),
        )
    return summary
