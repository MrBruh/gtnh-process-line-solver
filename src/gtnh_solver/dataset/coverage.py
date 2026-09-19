"""Measure what a dataset cannot draw, so a gap is a number instead of a surprise (GitHub #98).

Three questions, none of which the dump answers about itself, and each of which fails differently::

    data/<version>/multiblocks/_meta.json   ->  which controllers never dumped at all
    multiblocks/*.json  x  manifest blocks  ->  which (block, meta) has no sprite NAME
    manifest icons      x  the GT jar       ->  which resolved name has no sprite BYTES

**Ranking is by multiblocks touched, not by raw count.** The manifest's own ``gaps`` list runs to
thousands and is dominated by fluid and ore blocks nothing ever places; a family that 11 controllers
are built from matters more than 40 metas of a block no line uses. Sorting the other way is how the
wrong lane gets worked on (``docs/dataset-extraction/texture-resolution.md``, trap 6).

**The third question is the one nothing else asks.** A pair whose name did not resolve is recorded
in ``gaps`` and renders as the missing-texture checkerboard, which is visible. A name that resolved
against a sprite the jar does not carry is recorded nowhere: :func:`extract_icons` drops an absent
path by design, so the block quietly falls back to a placeholder. It needs the jar to see, which is
why it is optional here rather than assumed - a checkout with no cached jar reports the first two
questions and says plainly that it skipped the third, instead of reporting a clean bill it did not
earn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gtnh_solver.previewer.textures import TextureManifest

from .schema import ControllerFailure, MultiblockDoc, load_meta, load_multiblock_doc


@dataclass(frozen=True)
class BlockGap:
    """A ``(block, meta)`` a multiblock can place that the manifest has no entry for.

    ``placed_in`` and ``substituted_in`` are kept apart because conflating them is a measurable
    mistake, not a stylistic one. ``IC2:blockAlloyGlass`` sits in 4 controllers' block lists and is
    offered as a ``glass`` channel substitution in 35 more: report only the first and it looks like
    a rounding error, report only the sum and it looks like 37 builds are broken today. Both
    numbers are true and they answer different questions.
    """

    block: str
    meta: int
    placed_in: tuple[str, ...]
    substituted_in: tuple[str, ...]
    reason: str

    @property
    def key(self) -> str:
        return f"{self.block}|{self.meta}"

    @property
    def multiblocks(self) -> tuple[str, ...]:
        """Every controller that can end up with this block, either way."""
        return tuple(sorted(set(self.placed_in) | set(self.substituted_in)))


@dataclass(frozen=True)
class AssetGap:
    """A sprite the manifest names whose PNG is not in the jar the previewer fetches."""

    icon: str
    path: str
    multiblocks: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    """What one dataset can and cannot draw for the multiblocks it ships."""

    pack_version: str
    controllers: int
    controller_failures: tuple[ControllerFailure, ...]
    referenced_pairs: int
    unresolved: tuple[BlockGap, ...]
    absent_assets: tuple[AssetGap, ...]
    jar_checked: bool

    @property
    def multiblocks_with_gaps(self) -> int:
        """Controllers placing at least one block with no sprite name."""
        return len({name for gap in self.unresolved for name in gap.multiblocks})

    @property
    def resolved_pairs(self) -> int:
        return self.referenced_pairs - len(self.unresolved)


def _referenced(doc: MultiblockDoc) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """``(placed, substitutable)`` ``(block, meta)`` sets for one controller.

    A substitution is a real block a builder puts down (the glass or coil tier a channel swaps in),
    so a missing sprite for one is a missing sprite. It is not the same claim as a block in the
    structure, though: only one alternative is placed per build, and which one depends on the tier
    chosen. See :class:`BlockGap`.
    """
    placed = {(block.block, block.meta) for variant in doc.variants for block in variant.blocks}
    substitutable = {(sub.block, sub.meta) for subs in doc.substitutions.values() for sub in subs}
    return placed, substitutable - placed


def _gap_reasons(raw: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    """``(block, meta) -> the extractor's recorded reason``, first one wins (they agree per pair)."""
    reasons: dict[tuple[str, int], str] = {}
    for gap in raw.get("gaps", []):
        block, meta = gap.get("block"), gap.get("meta")
        if isinstance(block, str) and isinstance(meta, int):
            reasons.setdefault((block, meta), str(gap.get("reason", "")))
    return reasons


def _entry_icons(entry: Mapping[str, Any]) -> set[str]:
    """Every icon name a manifest entry's layer stacks reference, across all sides and states."""
    icons: set[str] = set()
    sides = entry.get("sides", {})
    if not isinstance(sides, dict):
        return icons
    for states in sides.values():
        if not isinstance(states, dict):
            continue
        for layers in states.values():
            if not isinstance(layers, list):
                continue
            for layer in layers:
                if isinstance(layer, dict) and isinstance(layer.get("icon"), str):
                    icons.add(layer["icon"])
    return icons


def measure(
    multiblocks_dir: str | Path,
    manifest_raw: Mapping[str, Any],
    *,
    jar_assets: frozenset[str] | None = None,
) -> Coverage:
    """Measure ``multiblocks_dir`` against ``manifest_raw``.

    ``jar_assets`` is the jar's member list (``zipfile.ZipFile(...).namelist()``). Passing ``None``
    skips the sprite-bytes question rather than guessing at it, and :attr:`Coverage.jar_checked`
    records which happened so a report can never imply an unasked question came back clean.
    """
    directory = Path(multiblocks_dir)
    manifest = TextureManifest(manifest_raw)
    blocks: Mapping[str, Any] = manifest_raw.get("blocks", {})
    icons: Mapping[str, str] = manifest_raw.get("icons", {})
    reasons = _gap_reasons(manifest_raw)

    meta_path = directory / "_meta.json"
    meta = load_meta(meta_path) if meta_path.is_file() else None

    placed_by: dict[tuple[str, int], set[str]] = {}
    substituted_by: dict[tuple[str, int], set[str]] = {}
    docs = 0
    for path in sorted(directory.glob("*.json")):
        if path.name == "_meta.json":
            continue
        doc = load_multiblock_doc(path)
        docs += 1
        placed, substitutable = _referenced(doc)
        for pair in placed:
            placed_by.setdefault(pair, set()).add(doc.controller.display_name)
        for pair in substitutable:
            substituted_by.setdefault(pair, set()).add(doc.controller.display_name)
    touched = {
        pair: placed_by.get(pair, set()) | substituted_by.get(pair, set())
        for pair in set(placed_by) | set(substituted_by)
    }

    unresolved: list[BlockGap] = []
    asset_users: dict[tuple[str, str], set[str]] = {}
    for (block, block_meta), names in touched.items():
        if not manifest.has_block(block, block_meta):
            unresolved.append(
                BlockGap(
                    block=block,
                    meta=block_meta,
                    placed_in=tuple(sorted(placed_by.get((block, block_meta), set()))),
                    substituted_in=tuple(sorted(substituted_by.get((block, block_meta), set()))),
                    reason=reasons.get((block, block_meta), ""),
                )
            )
            continue
        if jar_assets is None:
            continue
        for icon in _entry_icons(blocks.get(f"{block}|{block_meta}", {})):
            path_in_jar = icons.get(icon)
            if path_in_jar is not None and path_in_jar not in jar_assets:
                asset_users.setdefault((icon, path_in_jar), set()).update(names)

    return Coverage(
        pack_version=meta.pack_version if meta is not None else "unknown",
        controllers=docs,
        controller_failures=tuple(meta.failures) if meta is not None else (),
        referenced_pairs=len(touched),
        unresolved=tuple(sorted(unresolved, key=lambda g: (-len(g.multiblocks), g.key))),
        absent_assets=tuple(
            AssetGap(icon=icon, path=path, multiblocks=tuple(sorted(users)))
            for (icon, path), users in sorted(
                asset_users.items(), key=lambda kv: (-len(kv[1]), kv[0])
            )
        ),
        jar_checked=jar_assets is not None,
    )


def format_report(coverage: Coverage, *, limit: int = 15) -> str:
    """A human-readable report. ``limit`` caps each ranked list; totals always state the full count."""
    lines: list[str] = [
        f"dataset coverage: pack {coverage.pack_version}, {coverage.controllers} controller(s)",
        f"  {coverage.resolved_pairs}/{coverage.referenced_pairs} referenced (block, meta) pairs "
        f"resolve to a sprite",
    ]
    if coverage.unresolved:
        lines.append(
            f"  {len(coverage.unresolved)} unresolved, touching "
            f"{coverage.multiblocks_with_gaps} multiblock(s)"
        )

    if coverage.controller_failures:
        lines.append(f"\ncontrollers that did not dump ({len(coverage.controller_failures)})")
        for failure in coverage.controller_failures:
            lines.append(f"  {failure.registry_name}: {failure.reason}")

    if coverage.unresolved:
        lines.append(
            f"\nno sprite name, ranked by multiblocks touched ({len(coverage.unresolved)})"
        )
        for gap in coverage.unresolved[:limit]:
            reason = f"  [{gap.reason}]" if gap.reason else "  [no recorded gap]"
            split = f"{len(gap.placed_in)} placed + {len(gap.substituted_in)} substitutable"
            lines.append(f"  {len(gap.multiblocks):4d} mb ({split})  {gap.key}{reason}")
        if len(coverage.unresolved) > limit:
            lines.append(f"  ... and {len(coverage.unresolved) - limit} more")

    if not coverage.jar_checked:
        lines.append("\nsprite bytes: NOT CHECKED (no jar given; nothing is claimed about them)")
    elif coverage.absent_assets:
        lines.append(f"\nname resolves but the jar has no PNG ({len(coverage.absent_assets)})")
        for asset in coverage.absent_assets[:limit]:
            lines.append(f"  {len(asset.multiblocks):4d} mb  {asset.path}")
        if len(coverage.absent_assets) > limit:
            lines.append(f"  ... and {len(coverage.absent_assets) - limit} more")
    else:
        lines.append("\nsprite bytes: every resolved name has a PNG in the jar")
    return "\n".join(lines) + "\n"
