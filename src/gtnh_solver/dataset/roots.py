"""Resolve dataset paths across version-namespaced local folders.

The extractor's generated datasets live in gitignored, per-version folders
(``data/<version>/{multiblocks,textures}/``), regenerated on demand so several pack versions
coexist without overwriting. The two committed fixtures plus the small example-scoped texture
manifest live at the fixed ``data/multiblocks/`` and ``data/textures/manifest.json`` and are the
fallback when no generated version is present.

Resolution is **per sub-path**: for ``"multiblocks"`` or ``"textures/manifest.json"`` this returns
the newest local ``data/<version>/`` that actually provides it, else the committed fixture path.
So a fresh clone renders from the fixtures, a machine that has run the extractor renders from its
full local dump, and a texture-only local run (textures but no multiblocks) still falls back to the
committed multiblock fixtures. An explicit ``version`` pins one folder. See
``docs/dataset-extraction/plan.md``.
"""

from __future__ import annotations

from pathlib import Path

#: The repo ``data/`` directory. This file is ``src/gtnh_solver/dataset/roots.py``, so ``parents[3]``
#: is the repo root; resolves in the editable/dev install the repo is used through.
DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data"

#: Fixed sub-directories of ``data/`` that hold the committed fixtures - never a generated version
#: folder, so :func:`list_versions` skips them.
_RESERVED = frozenset({"multiblocks", "textures"})


def list_versions(data_dir: str | Path | None = None) -> list[Path]:
    """Generated ``data/<version>/`` folders, newest (most recently modified) first.

    Newest is by modification time, so "the version you most recently generated" wins by default;
    a caller that wants a specific one passes ``version`` to :func:`resolve_dataset_path`.
    """
    base = DEFAULT_DATA if data_dir is None else Path(data_dir)
    if not base.is_dir():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir() and d.name not in _RESERVED]
    return sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)


def resolve_dataset_path(
    rel: str, *, version: str | None = None, data_dir: str | Path | None = None
) -> Path:
    """The path for dataset sub-path ``rel`` (e.g. ``"multiblocks"`` or ``"textures/manifest.json"``).

    An explicit ``version`` pins ``data/<version>/<rel>`` (returned even if absent, so the caller
    reports a clear miss rather than silently using a different version). Otherwise the newest local
    ``data/<version>/`` that actually contains ``rel`` wins; if none do, the committed fixtures at
    ``data/<rel>``.
    """
    base = DEFAULT_DATA if data_dir is None else Path(data_dir)
    if version is not None:
        return base / version / rel
    for vdir in list_versions(base):
        candidate = vdir / rel
        if candidate.exists():
            return candidate
    return base / rel
