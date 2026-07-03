"""Render a reviewable Markdown summary of a dataset update for the PR body (issue #47).

Step 4 of ``update-dataset.yml`` opens a PR whose summary must make regressions obvious at
a glance: "Distillation Tower gained a layer variant" and "40 controllers vanished" should
both jump out. This module compares the pre-update and freshly generated dataset and emits
that summary. It is stdlib-only and network-free; the workflow feeds it local files.

Inputs (all optional; a first run has no "old" side):
  * old/new ``data/multiblocks/_meta.json`` - for controller counts and the failure list.
  * old/new controller-name listings - the ``*.json`` basenames in ``data/multiblocks/``,
    which give added/removed controllers without coupling to the per-file schema.
  * a changed-controller listing - ``git diff --name-only`` basenames (content changes on
    controllers that stayed present).

Contract: ``data/multiblocks/_meta.json`` (owned by lanes 1/2; read tolerantly here)
------------------------------------------------------------------------------------
Per the extraction plan, ``_meta.json`` records at least::

    {
        "schema": 1,
        "pack_version": "...",
        "mods": {...},
        "controller_count": 812,
        "failures": [{"registry_name": "gregtech:...", "reason": "..."}, ...],
    }

We read ``controller_count`` and ``failures`` defensively: ``failures`` entries may be
objects (``registry_name``/``reason``) or bare strings, and a missing count falls back to
the controller listing length.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# Keep the PR body readable: long name lists are truncated with a "... and N more" note.
_MAX_LISTED = 40


def _controller_count(meta: Mapping[str, Any], fallback: int) -> int:
    value = meta.get("controller_count")
    return value if isinstance(value, int) else fallback


def _failures(meta: Mapping[str, Any]) -> list[str]:
    raw = meta.get("failures")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    rendered: list[str] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            name = entry.get("registry_name", "?")
            reason = entry.get("reason", "?")
            rendered.append(f"{name}: {reason}")
        else:
            rendered.append(str(entry))
    return rendered


def _bullets(names: Sequence[str]) -> list[str]:
    if not names:
        return ["_none_"]
    shown = [f"- `{name}`" for name in names[:_MAX_LISTED]]
    if len(names) > _MAX_LISTED:
        shown.append(f"- ... and {len(names) - _MAX_LISTED} more")
    return shown


def render_summary(
    old_meta: Mapping[str, Any],
    new_meta: Mapping[str, Any],
    old_controllers: Sequence[str],
    new_controllers: Sequence[str],
    changed_controllers: Sequence[str],
) -> str:
    """Render the dataset-diff portion of the PR body as Markdown."""
    old_set = set(old_controllers)
    new_set = set(new_controllers)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    # A "changed" controller must still be present on both sides (git can list a rename).
    changed = sorted(c for c in set(changed_controllers) if c in new_set and c in old_set)

    old_count = _controller_count(old_meta, len(old_set))
    new_count = _controller_count(new_meta, len(new_set))
    delta = new_count - old_count
    failures = _failures(new_meta)

    lines: list[str] = [
        "## Dataset diff",
        "",
        f"Controllers: **{old_count} -> {new_count}** ({delta:+d})",
        "",
        f"### Added ({len(added)})",
        *_bullets(added),
        "",
        f"### Removed ({len(removed)})",
        *_bullets(removed),
        "",
        f"### Changed ({len(changed)})",
        *_bullets(changed),
        "",
        f"### Extractor failures ({len(failures)})",
        *_bullets(failures),
    ]
    return "\n".join(lines) + "\n"


def _read_json_object(path: Path | None) -> Mapping[str, Any]:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, Mapping) else {}


def _read_listing(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _optional(value: Any) -> Path | None:
    return Path(value) if value is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a dataset-update PR summary.")
    parser.add_argument("--old-meta", type=Path)
    parser.add_argument("--new-meta", type=Path)
    parser.add_argument("--old-list", type=Path)
    parser.add_argument("--new-list", type=Path)
    parser.add_argument("--changed-list", type=Path)
    parser.add_argument("--out", type=Path, help="write the Markdown summary here")
    args = parser.parse_args(argv)

    summary = render_summary(
        _read_json_object(_optional(args.old_meta)),
        _read_json_object(_optional(args.new_meta)),
        _read_listing(_optional(args.old_list)),
        _read_listing(_optional(args.new_list)),
        _read_listing(_optional(args.changed_list)),
    )
    if args.out is not None:
        Path(args.out).write_text(summary, encoding="utf-8")
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
