"""Derive the small committed texture manifest from a full local one.

The full texture manifest (~6 MB, ~1470 blocks) is local and version-namespaced
(``data/<version>/textures/manifest.json``, gitignored). This prunes it to just the blocks the
shipped example lines and the two committed multiblock fixtures need, plus the icons those blocks
reference, and writes the small committed ``data/textures/manifest.json`` so
``gtnh-solve --preview examples/*.json`` skins out of the box. Rerun when the examples change.

**Cables and pipes are kept the same way, and for the same reason.** ``cable.tin.02`` contains
no machine-type name either, so the name rule can never reach one. The stand-in policy in
``dataset/pipes.py`` says which material each tier is drawn as; this asks it, for every tier the
examples use and every gauge the router can size to, and keeps exactly those.

**Hatches are kept by resolution, not by name.** A hatch can never match an example machine's name
("Input Bus (LV)" contains no machine type), so keeping them needs a second rule: for every hatch
kind at every voltage tier the examples use, ask the previewer's own
``TextureManifest.hatch_block`` which block it would draw, and keep exactly that. Asking the same
function the previewer will ask is what guarantees the committed manifest holds precisely what a
preview looks up, rather than a hand-kept list that drifts from it.

Usage (from the repo root, in the dev venv)::

    python tools/derive_small_manifest.py [FULL_MANIFEST]

``FULL_MANIFEST`` defaults to the newest local ``data/<version>/textures/manifest.json``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from gtnh_solver.adapter import adapt_file
from gtnh_solver.dataset import (
    CABLE_MATERIAL_BY_TIER,
    CABLE_THICKNESSES,
    DEFAULT_PIPE_SIZE,
    PIPE_MATERIAL,
    cable_display_name,
    list_versions,
    load_physical_dataset,
    pipe_display_name,
)
from gtnh_solver.previewer.textures import HATCH_KIND_BY_CLASS, TextureManifest

REPO = Path.cwd()
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_FULL_MIN_BLOCKS = 100  # a real dump has ~1470 blocks; the small one has a few dozen


def _norm(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def _is_full(manifest: dict[str, Any]) -> bool:
    return manifest.get("provenance", {}).get("coverage", {}).get("blocks", 0) >= _FULL_MIN_BLOCKS


def _find_full_manifest() -> Path:
    """The newest local ``data/<version>/textures/manifest.json`` that is a full dump."""
    for vdir in list_versions():
        candidate = vdir / "textures" / "manifest.json"
        if candidate.is_file() and _is_full(json.loads(candidate.read_text(encoding="utf-8"))):
            return candidate
    raise SystemExit(
        "no full local manifest under data/<version>/textures/; pass one explicitly or run the "
        "extractor first (see docs/dataset-extraction/implementation.md)"
    )


def _example_types_and_tiers() -> tuple[set[str], set[str]]:
    """Normalized machine-type names and voltage tiers the shipped example lines reference."""
    physical = load_physical_dataset()
    types: set[str] = set()
    tiers: set[str] = set()
    for example in sorted((REPO / "examples").glob("*.json")):
        for machine in adapt_file(str(example), physical=physical).machines:
            types.add(_norm(machine.type))
            tiers.add(machine.voltage_tier)
    return types, tiers


def _hatch_keys(full: dict[str, Any], tiers: set[str]) -> set[str]:
    """``"<block>|<meta>"`` for every hatch a preview of the examples could resolve.

    Every ``HatchElement`` kind the solver places, at every tier the examples use, resolved through
    the previewer's own lookup so the two can never disagree. A kind with no block at all (nothing
    in the dump implements it) simply contributes nothing.
    """
    manifest = TextureManifest(full)
    keys: set[str] = set()
    for kind in sorted(set(HATCH_KIND_BY_CLASS.values())):
        for tier in sorted(tiers):
            found = manifest.hatch_block(kind, tier)
            if found is not None:
                keys.add(f"{found[0]}|{found[1]}")
    return keys


def _route_keys(full: dict[str, Any], tiers: set[str]) -> set[str]:
    """``"<block>|<meta>"`` for every cable and pipe a preview of the examples could draw.

    Cables and pipes are ``kind: "pipe"`` entries whose names ("cable.tin.02", "gt_pipe_bronze")
    contain no machine type, so the name rule at the call site can never keep one - the same hole
    hatches have, closed the same way. The policy in ``dataset/pipes.py`` is asked directly rather
    than a list being kept here, so the committed manifest cannot drift from what a preview looks up.

    Every gauge is kept for each tier the examples use, not just the gauges those lines happen to
    route today: cable thickness follows summed amperage, so re-solving a line at a different seed
    can move a segment between gauges, and a preview that silently lost its cable at 8x would be a
    puzzling bug rather than an obvious one. Six entries per tier is a rounding error in the file.
    """
    by_name = {
        str(entry["display_name"]): key
        for key, entry in full["blocks"].items()
        if entry.get("kind") == "pipe" and entry.get("display_name")
    }
    wanted = {
        cable_display_name(CABLE_MATERIAL_BY_TIER[tier], gauge)
        for tier in tiers
        if tier in CABLE_MATERIAL_BY_TIER
        for gauge in CABLE_THICKNESSES
    }
    wanted |= {
        pipe_display_name(material, DEFAULT_PIPE_SIZE) for material in PIPE_MATERIAL.values()
    }
    return {by_name[name] for name in wanted if name in by_name}


def _fixture_block_keys() -> set[str]:
    """``"<block>|<meta>"`` keys the two committed multiblock fixtures place."""
    keys: set[str] = set()
    for path in sorted((REPO / "data" / "multiblocks").glob("*.json")):
        if path.name == "_meta.json":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for variant in doc.get("variants", []):
            for block in variant.get("blocks", []):
                keys.add(f"{block['block']}|{block['meta']}")
        for subs in doc.get("substitutions", {}).values():
            for sub in subs:
                keys.add(f"{sub['block']}|{sub['meta']}")
    return keys


def main() -> None:
    if not (REPO / "pyproject.toml").is_file():
        raise SystemExit(f"run from the repo root (cwd={REPO} has no pyproject.toml)")
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else _find_full_manifest()
    full = json.loads(source.read_text(encoding="utf-8"))
    if not _is_full(full):
        raise SystemExit(f"{source} looks already pruned, not a full manifest")

    types, tiers = _example_types_and_tiers()
    fixture_keys = _fixture_block_keys()
    hatch_keys = _hatch_keys(full, tiers)
    route_keys = _route_keys(full, tiers)
    keep: dict[str, Any] = {}
    for key, entry in full["blocks"].items():
        if key in hatch_keys or key in route_keys:
            keep[key] = entry  # neither matches a machine name; see the module docstring
        elif entry.get("kind") == "mte":
            name = _norm(entry.get("display_name") or "")
            if name and any(t and t in name for t in types):
                keep[key] = entry
        elif key in fixture_keys:
            keep[key] = entry

    used_icons: set[str] = set()
    for entry in keep.values():
        for states in entry.get("sides", {}).values():
            for layers in states.values():
                for layer in layers:
                    used_icons.add(layer["icon"])
    icons = {i: full["icons"][i] for i in sorted(used_icons) if i in full["icons"]}

    mte_kept = sum(1 for e in keep.values() if e.get("kind") == "mte")
    small = {
        "schema": full["schema"],
        "method": full["method"],
        "provenance": {
            **full["provenance"],
            "coverage": {"blocks": len(keep), "mte": mte_kept, "icons": len(icons), "gaps": 0},
            "note": (
                "SMALL committed manifest: only the blocks the shipped example lines and the two "
                "multiblock fixtures need - plus every hatch kind at the tiers those lines use, "
                "since a hatch matches no machine name - so `gtnh-solve --preview examples/*.json` "
                "skins out of the box. The full dump is local and version-namespaced "
                "(data/<version>/textures/manifest.json), never committed. Regenerate with "
                "tools/derive_small_manifest.py."
            ),
        },
        "asset_root": full["asset_root"],
        "blocks": {k: keep[k] for k in sorted(keep)},
        "icons": icons,
        "gaps": [],
    }
    out = REPO / "data" / "textures" / "manifest.json"
    out.write_text(json.dumps(small, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"wrote {out.relative_to(REPO)} from {source}: "
        f"{len(keep)} blocks ({mte_kept} MTE), {len(icons)} icons"
    )


if __name__ == "__main__":
    main()
