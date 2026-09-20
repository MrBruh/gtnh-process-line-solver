"""Tests for dataset path resolution across version-namespaced local folders (``dataset/roots.py``).

The generated dumps live in ``data/<version>/{multiblocks,textures}/`` and the committed fixtures at
``data/{multiblocks,textures}/``; resolution picks the newest local version that provides a sub-path,
else the fixtures. These drive that entirely on a temp tree, never the real ``data/``.
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gtnh_solver.dataset.roots import (
    DatasetWarning,
    generated_at,
    list_versions,
    resolve_dataset_path,
)


def _mkdir(p: Path, *, mtime: float) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.utime(p, (mtime, mtime))
    return p


def _mkfile(p: Path, *, mtime: float) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    os.utime(p.parent, (mtime, mtime))
    return p


def _manifest(p: Path, *, stamp: str | None, mtime: float = 1000) -> Path:
    """A texture manifest at ``p``, stamped the way the extractor stamps one (in ``provenance``)."""
    provenance = {} if stamp is None else {"generated_at": stamp}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"provenance": provenance, "blocks": {}}), encoding="utf-8")
    os.utime(p.parent.parent, (mtime, mtime))
    return p


def _multiblocks(p: Path, *, stamp: str | None, mtime: float = 1000) -> Path:
    """A multiblock dump directory at ``p``, stamped in its ``_meta.json`` sidecar."""
    p.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"schema": 2} if stamp is None else {"generated_at": stamp}
    (p / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    os.utime(p.parent, (mtime, mtime))
    return p


def test_list_versions_empty_without_data(tmp_path: Path) -> None:
    assert list_versions(tmp_path) == []
    assert list_versions(tmp_path / "absent") == []  # a missing dir is empty, not an error


def test_list_versions_excludes_reserved_dirs_and_files(tmp_path: Path) -> None:
    (tmp_path / "multiblocks").mkdir()
    (tmp_path / "textures").mkdir()
    (tmp_path / "2.8.4").mkdir()
    (tmp_path / "2.9.0-beta-1").mkdir()
    (tmp_path / "stray.json").write_text("{}", encoding="utf-8")  # a file, not a version
    assert {p.name for p in list_versions(tmp_path)} == {"2.8.4", "2.9.0-beta-1"}


def test_list_versions_newest_first(tmp_path: Path) -> None:
    _mkdir(tmp_path / "2.8.4", mtime=1000)
    _mkdir(tmp_path / "2.9.0", mtime=2000)
    assert [p.name for p in list_versions(tmp_path)] == ["2.9.0", "2.8.4"]


def test_resolve_explicit_version_pins_even_if_absent(tmp_path: Path) -> None:
    got = resolve_dataset_path("textures/manifest.json", version="2.8.4", data_dir=tmp_path)
    assert got == tmp_path / "2.8.4" / "textures" / "manifest.json"  # returned even though absent


def test_resolve_falls_back_to_committed_when_no_versions(tmp_path: Path) -> None:
    assert resolve_dataset_path("multiblocks", data_dir=tmp_path) == tmp_path / "multiblocks"


def test_resolve_per_subpath_across_partial_versions(tmp_path: Path) -> None:
    # Newest (2.9.0) has textures but no multiblocks; older (2.8.4) has multiblocks.
    _mkfile(tmp_path / "2.9.0" / "textures" / "manifest.json", mtime=2000)
    _mkdir(tmp_path / "2.8.4" / "multiblocks", mtime=1000)
    os.utime(tmp_path / "2.8.4", (1000, 1000))
    assert resolve_dataset_path("textures/manifest.json", data_dir=tmp_path) == (
        tmp_path / "2.9.0" / "textures" / "manifest.json"
    )
    # 2.9.0 lacks multiblocks, so it falls through to 2.8.4 which has it.
    assert resolve_dataset_path("multiblocks", data_dir=tmp_path) == (
        tmp_path / "2.8.4" / "multiblocks"
    )


def test_resolve_falls_back_when_no_version_has_subpath(tmp_path: Path) -> None:
    _mkdir(
        tmp_path / "2.9.0" / "textures", mtime=2000
    )  # a textures dir but no manifest, no multiblocks
    assert resolve_dataset_path("multiblocks", data_dir=tmp_path) == tmp_path / "multiblocks"


# ------------------------------------------------------- dating a dump, and the shadowing it hides


def test_generated_at_dates_both_shapes_of_dump(tmp_path: Path) -> None:
    # The manifest stamps itself inside ``provenance``; a multiblock dump stamps its ``_meta.json``
    # sidecar. One reader, so a caller can compare either sub-path against its committed twin.
    manifest = _manifest(
        tmp_path / "2.8.4" / "textures" / "manifest.json", stamp="2026-09-18T00:00:00Z"
    )
    directory = _multiblocks(tmp_path / "2.8.4" / "multiblocks", stamp="2026-07-02T00:00:00Z")
    assert generated_at(manifest) == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert generated_at(directory) == datetime(2026, 7, 2, tzinfo=timezone.utc)


def test_generated_at_offsets_are_read_as_written(tmp_path: Path) -> None:
    # Not every writer says "Z". An explicit offset has to compare against a "Z" stamp correctly,
    # which it only does if both come back as aware datetimes.
    path = _manifest(tmp_path / "textures" / "manifest.json", stamp="2026-09-18T02:00:00+02:00")
    assert generated_at(path) == datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda p: p / "absent.json", id="no-file"),
        pytest.param(
            lambda p: _manifest(p / "textures" / "manifest.json", stamp=None), id="no-stamp"
        ),
        pytest.param(
            lambda p: _manifest(p / "textures" / "manifest.json", stamp="last tuesday"),
            id="not-iso",
        ),
        pytest.param(lambda p: _multiblocks(p / "multiblocks", stamp=None), id="unstamped-sidecar"),
        pytest.param(lambda p: _mkdir(p / "empty", mtime=1000), id="no-sidecar"),
    ],
)
def test_generated_at_abstains_rather_than_raising(tmp_path: Path, make: object) -> None:
    # Total by design: failing to date a dump must leave a caller no worse off than not looking.
    assert generated_at(make(tmp_path)) is None  # type: ignore[operator]


def test_a_stale_local_dump_warns_that_it_shadows_the_committed_data(tmp_path: Path) -> None:
    """The #166 defect, made reproducible: presence alone used to decide this, silently.

    A dump generated before the extractor learned to write a field goes on shadowing committed data
    that has it, for as long as it sits on disk - which is how a manifest with ``te_base_type``
    ended up blamed for an export that never read it. The local dump still wins (it is the one with
    the coverage), so the fix is that it stops winning quietly.
    """
    _manifest(tmp_path / "textures" / "manifest.json", stamp="2026-09-18T21:29:30.149Z")
    local = _manifest(
        tmp_path / "2.8.4" / "textures" / "manifest.json", stamp="2026-09-08T00:00:00Z", mtime=2000
    )
    with pytest.warns(DatasetWarning) as caught:
        assert resolve_dataset_path("textures/manifest.json", data_dir=tmp_path) == local
    message = str(caught[0].message)
    assert str(local) in message, "name the dump that won"
    assert str(tmp_path / "textures" / "manifest.json") in message, "and the data it shadowed"
    assert "2026-09-08" in message, "date the dump"
    assert "2026-09-18" in message, "and the data, since the comparison is the finding"


def test_a_stale_multiblock_dump_warns_too(tmp_path: Path) -> None:
    # The check is per sub-path, like the resolution it rides on, so the dump half is not exempt.
    _multiblocks(tmp_path / "multiblocks", stamp="2026-07-02T00:00:00Z")
    local = _multiblocks(
        tmp_path / "2.8.4" / "multiblocks", stamp="2026-01-01T00:00:00Z", mtime=2000
    )
    with pytest.warns(DatasetWarning, match="older than the committed"):
        assert resolve_dataset_path("multiblocks", data_dir=tmp_path) == local


@pytest.mark.parametrize(
    ("committed_stamp", "local_stamp", "why"),
    [
        ("2026-09-08T00:00:00Z", "2026-09-18T00:00:00Z", "local dump is newer"),
        ("2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z", "same generation"),
        (None, "2026-09-08T00:00:00Z", "committed data states no date"),
        ("2026-09-18T00:00:00Z", None, "local dump states no date"),
    ],
)
def test_resolution_is_silent_when_the_dates_do_not_prove_staleness(
    tmp_path: Path, committed_stamp: str | None, local_stamp: str | None, why: str
) -> None:
    # An undated dump is not evidence of being old, and crying stale on one would teach every
    # reader to tune the warning out - which would cost the one case that matters.
    _manifest(tmp_path / "textures" / "manifest.json", stamp=committed_stamp)
    local = _manifest(
        tmp_path / "2.8.4" / "textures" / "manifest.json", stamp=local_stamp, mtime=2000
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DatasetWarning)
        assert resolve_dataset_path("textures/manifest.json", data_dir=tmp_path) == local, why


def test_nothing_is_shadowed_when_there_is_no_committed_counterpart(tmp_path: Path) -> None:
    # A sub-path the repo ships nothing for cannot be shadowed, whatever the dump's date.
    local = _manifest(
        tmp_path / "2.8.4" / "textures" / "manifest.json", stamp="2020-01-01T00:00:00Z", mtime=2000
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DatasetWarning)
        assert resolve_dataset_path("textures/manifest.json", data_dir=tmp_path) == local


def test_an_explicit_pin_is_taken_at_its_word(tmp_path: Path) -> None:
    # Asking for a version by name is not shadowing: the caller already chose, and second-guessing
    # a pin is exactly what ``--dataset-version`` exists to prevent.
    _manifest(tmp_path / "textures" / "manifest.json", stamp="2026-09-18T00:00:00Z")
    _manifest(tmp_path / "2.8.4" / "textures" / "manifest.json", stamp="2020-01-01T00:00:00Z")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DatasetWarning)
        got = resolve_dataset_path("textures/manifest.json", version="2.8.4", data_dir=tmp_path)
    assert got == tmp_path / "2.8.4" / "textures" / "manifest.json"
