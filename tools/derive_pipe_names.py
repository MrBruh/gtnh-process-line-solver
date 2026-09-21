"""Derive the cable/pipe name list the newer-pack test reads, from a local dump.

``tests/test_dataset_pipes.py`` checks that every cable and pipe the stand-in policy draws resolves
against GT's whole cable, wire and pipe namespace at the pack that renamed them (#176). That
namespace only exists in an extractor dump, and nothing a dumper produced is committed, not even a
filtered slice of it. So the list is written to ``tests/fixtures/local/`` (gitignored as a whole
directory) and the test skips wherever nobody has generated it, CI included.

The derivation is a filter and nothing else: every ``blocks`` entry whose ``kind`` is ``"pipe"``,
its ``display_name``, sorted and de-duplicated, with the manifest's own provenance alongside.
Nothing from ``gtnh_solver`` is imported, so the list cannot agree with ``dataset/pipes.py`` by
construction, which is the whole point of checking the policy against it.

Usage (from the repo root, with a dump staged under ``data/``)::

    python tools/derive_pipe_names.py data/<version>/textures/manifest.json

This writes ``tests/fixtures/local/manifest_pipe_names_<major>.<minor>.json``, named for the pack
the manifest records, so a 2.9 dump yields the ``manifest_pipe_names_2.9.json`` the test reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

LOCAL_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "local"

#: A full dump names well over a thousand pipes (1191 at 2.9.0-beta-2); the committed, pruned
#: manifest names a couple of dozen, and those were chosen by resolving the stand-in policy itself.
#: A list taken from a pruned manifest would agree with the policy by construction and make the
#: check vacuous, so the run refuses one rather than writing it.
MIN_FULL_DUMP_PIPES = 1000


def pipe_names(manifest: dict[str, Any]) -> list[str]:
    """Every ``kind: "pipe"`` display name in ``manifest``, sorted, each once."""
    return sorted(
        {
            str(entry["display_name"])
            for entry in manifest["blocks"].values()
            if entry.get("kind") == "pipe" and entry.get("display_name")
        }
    )


def output_path(manifest: dict[str, Any], out_dir: Path = LOCAL_FIXTURES) -> Path:
    """Where the list for ``manifest``'s pack goes: ``2.9.0-beta-2`` becomes ``..._2.9.json``."""
    pack = str(manifest.get("provenance", {}).get("pack_version") or "")
    if not pack:
        raise SystemExit("the manifest records no provenance.pack_version to name the list after")
    return out_dir / f"manifest_pipe_names_{'.'.join(pack.split('.')[:2])}.json"


def derive(manifest: dict[str, Any], source: str) -> dict[str, Any]:
    """The list document for ``manifest``, or ``SystemExit`` if it is not a full dump."""
    names = pipe_names(manifest)
    if len(names) < MIN_FULL_DUMP_PIPES:
        raise SystemExit(
            f"{source} names {len(names)} pipes, fewer than the {MIN_FULL_DUMP_PIPES} a full dump "
            "carries; it looks pruned (the committed data/textures/manifest.json is), so a list "
            "taken from it would agree with the stand-in policy by construction"
        )
    provenance = manifest["provenance"]
    return {
        "note": (
            "Every kind=pipe display_name in one real GTNH texture manifest: GT's whole cable, "
            "wire and pipe namespace at that pack. Dumper output, so it is local-only and never "
            "committed (tests/fixtures/local/ is gitignored). Derived by "
            "tools/derive_pipe_names.py, which filters the manifest's blocks on kind and takes "
            "display_name; no solver code takes part."
        ),
        "source": source,
        "pack_version": provenance["pack_version"],
        "mod_versions": provenance.get("mod_versions", {}),
        "generated_at": provenance.get("generated_at"),
        "pipe_entries": sum(1 for e in manifest["blocks"].values() if e.get("kind") == "pipe"),
        "display_names": names,
    }


def main(argv: list[str] | None = None, out_dir: Path = LOCAL_FIXTURES) -> Path:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit(
            "usage: python tools/derive_pipe_names.py data/<version>/textures/manifest.json"
        )
    source = Path(args[0])
    manifest = json.loads(source.read_text(encoding="utf-8"))
    doc = derive(manifest, source.as_posix())
    out = output_path(manifest, out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} from {source}: {len(doc['display_names'])} names")
    return out


if __name__ == "__main__":
    main()
