"""Derive the small committed texture manifest from a full local one.

The full texture manifest (~6 MB, ~1470 blocks) is local and version-namespaced
(``data/<version>/textures/manifest.json``, gitignored). This prunes it to just the blocks the
shipped example lines and the two committed multiblock fixtures need, plus the icons those blocks
reference, and writes the small committed ``data/textures/manifest.json`` so
``gtnh-solve --preview examples/*.json`` skins out of the box. Rerun when the examples change.

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
from gtnh_solver.dataset import list_versions, load_physical_dataset

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


def _example_types() -> set[str]:
    """Normalized machine-type names the shipped example lines reference."""
    physical = load_physical_dataset()
    types: set[str] = set()
    for example in sorted((REPO / "examples").glob("*.json")):
        for machine in adapt_file(str(example), physical=physical).machines:
            types.add(_norm(machine.type))
    return types


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

    types = _example_types()
    fixture_keys = _fixture_block_keys()
    keep: dict[str, Any] = {}
    for key, entry in full["blocks"].items():
        if entry.get("kind") == "mte":
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
                "multiblock fixtures need, so `gtnh-solve --preview examples/*.json` skins out of the "
                "box. The full dump is local and version-namespaced "
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
