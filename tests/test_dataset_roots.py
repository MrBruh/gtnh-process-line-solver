"""Tests for dataset path resolution across version-namespaced local folders (``dataset/roots.py``).

The generated dumps live in ``data/<version>/{multiblocks,textures}/`` and the committed fixtures at
``data/{multiblocks,textures}/``; resolution picks the newest local version that provides a sub-path,
else the fixtures. These drive that entirely on a temp tree, never the real ``data/``.
"""

from __future__ import annotations

import os
from pathlib import Path

from gtnh_solver.dataset.roots import list_versions, resolve_dataset_path


def _mkdir(p: Path, *, mtime: float) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.utime(p, (mtime, mtime))
    return p


def _mkfile(p: Path, *, mtime: float) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
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
