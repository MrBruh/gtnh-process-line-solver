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
``docs/dataset-extraction/requirements.md`` (commit and delivery policy).

**A local dump can be older than the committed data it shadows, and that is now reported** (#166).
"Newest" is newest by folder mtime among the *local* dumps only; the committed data never competes
for the slot, because it is example-scoped (73 blocks against a full dump's 11k) and handing a real
dump's place to it would lose every footprint and sprite the dump exists to provide. But the
committed data is regenerated too, and picks up whatever the extractor learned to write since -
``te_base_type`` (#158) is the case that bit: a dump taken before that field existed goes on
shadowing a committed manifest that has it, for as long as it sits on disk, and the ``.schematic``
export then refuses a machine the shipped data could have typed. So each resolution compares the two
``generated_at`` stamps and warns (:class:`DatasetWarning`) when the local one is older. The local
dump still wins - it is the one with the coverage - but it no longer wins *silently*.
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime
from pathlib import Path

#: The repo ``data/`` directory. This file is ``src/gtnh_solver/dataset/roots.py``, so ``parents[3]``
#: is the repo root; resolves in the editable/dev install the repo is used through.
DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data"

#: Fixed sub-directories of ``data/`` that hold the committed fixtures - never a generated version
#: folder, so :func:`list_versions` skips them.
_RESERVED = frozenset({"multiblocks", "textures"})

#: A generated multiblock dump keeps its provenance in a sidecar; the texture manifest keeps its own
#: inline. Either way the stamp is :data:`_STAMP`, so one reader dates both sub-paths.
_META_SIDECAR = "_meta.json"

#: How much of a dataset JSON to read when looking for its generation stamp. The full texture
#: manifest is ~15 MB and this runs on every resolve, so parsing it to read one string is not
#: affordable; the extractor writes provenance ahead of the bulk ``blocks`` map, so the stamp sits in
#: the first few hundred bytes. A miss reads as "not stated" and never warns, which is exactly what
#: an unstamped dump deserves.
_STAMP_WINDOW = 64 * 1024

#: ``"generated_at": "<ISO-8601>"`` as the extractor writes it, matched on bytes so the window can be
#: read without decoding a partial multi-byte character at its edge.
_STAMP = re.compile(rb'"generated_at"\s*:\s*"([^"]*)"')


class DatasetWarning(UserWarning):
    """A recoverable dataset-resolution finding: a local dump older than the committed data.

    A warning rather than an error, because the resolved dump is still the useful one: for almost
    everything the solver does (footprints, sprites) an old full dump beats the example-scoped
    fixtures. Only a consumer needing a field the dump predates actually fails, and that refusal
    names the file it read.
    """


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


def generated_at(path: str | Path) -> datetime | None:
    """When the extractor wrote the dataset at ``path``, or ``None`` if it does not say.

    ``path`` is a resolved dataset sub-path: a multiblock *directory* (stamped in its ``_meta.json``
    sidecar) or the texture *manifest* file itself (stamped in its ``provenance`` block). Both are
    read the same way - see :data:`_STAMP_WINDOW` for why the file is not parsed.

    Deliberately total: a missing file, an unreadable one, no stamp, or a stamp that is not
    ISO-8601 all give ``None``, which callers read as "cannot be compared". Failing to date a dump
    must never leave anyone worse off than not looking.
    """
    target = Path(path)
    if target.is_dir():
        target = target / _META_SIDECAR
    try:
        with target.open("rb") as handle:
            head = handle.read(_STAMP_WINDOW)
    except OSError:
        return None
    found = _STAMP.search(head)
    if found is None:
        return None
    # The extractor writes UTC with a trailing "Z", which fromisoformat only accepts from 3.11 and
    # this project supports 3.10.
    text = found.group(1).decode("utf-8", "replace")
    try:
        return datetime.fromisoformat(f"{text[:-1]}+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None


def resolve_dataset_path(
    rel: str, *, version: str | None = None, data_dir: str | Path | None = None
) -> Path:
    """The path for dataset sub-path ``rel`` (e.g. ``"multiblocks"`` or ``"textures/manifest.json"``).

    An explicit ``version`` pins ``data/<version>/<rel>`` (returned even if absent, so the caller
    reports a clear miss rather than silently using a different version). Otherwise the newest local
    ``data/<version>/`` that actually contains ``rel`` wins; if none do, the committed fixtures at
    ``data/<rel>``. A local dump generated before the committed data still wins, and warns - see the
    module docstring.
    """
    base = DEFAULT_DATA if data_dir is None else Path(data_dir)
    if version is not None:
        return base / version / rel
    for vdir in list_versions(base):
        candidate = vdir / rel
        if candidate.exists():
            _warn_if_stale(rel, candidate, base / rel)
            return candidate
    return base / rel


def _warn_if_stale(rel: str, local: Path, committed: Path) -> None:
    """Warn when ``local`` shadows a ``committed`` counterpart that was generated later.

    Both stamps have to be readable for this to say anything: an undated dump is not evidence of
    being old. The cross-pack case (a 2.9 dump older than a 2.8.4-derived committed manifest) warns
    too, and rightly - the claim is about *when* the dump was taken, not which pack it covers, and a
    dump taken before a field existed lacks that field whatever pack it is for.
    """
    if not committed.exists():
        return
    local_stamp = generated_at(local)
    committed_stamp = generated_at(committed)
    if local_stamp is None or committed_stamp is None or local_stamp >= committed_stamp:
        return
    warnings.warn(
        f"the local dataset dump {local} (generated {local_stamp.isoformat()}) is older than the "
        f"committed {committed} (generated {committed_stamp.isoformat()}) it shadows, so any field "
        f"the extractor learned to write in between is missing from it - te_base_type, which the "
        f".schematic export needs, is one (#158). Re-run the extractor for this pack, or pass "
        f"--dataset-version to pin another dump; the local {rel} is used either way, because it is "
        f"the one with the coverage (#166).",
        DatasetWarning,
        stacklevel=3,
    )
