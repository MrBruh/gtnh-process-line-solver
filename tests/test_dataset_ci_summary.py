"""Tests for the dataset-diff PR summary (``tools/dataset_ci/dataset_summary.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dataset_ci import dataset_summary as ds

FIXTURES = Path(__file__).parent / "fixtures" / "dataset_ci"


def _meta(path: str) -> dict[str, object]:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def test_render_summary_reports_added_removed_changed_and_failures() -> None:
    old_meta = _meta("old_meta.json")
    new_meta = _meta("new_meta.json")
    summary = ds.render_summary(
        old_meta=old_meta,
        new_meta=new_meta,
        old_controllers=["ebf.json", "vacuum_freezer.json", "gone.json"],
        new_controllers=["ebf.json", "vacuum_freezer.json", "distillation_tower.json", "new.json"],
        changed_controllers=["ebf.json"],
    )
    assert "Controllers: **3 -> 4** (+1)" in summary
    assert "### Added (2)" in summary
    assert "- `distillation_tower.json`" in summary
    assert "### Removed (1)" in summary
    assert "- `gone.json`" in summary
    assert "### Changed (1)" in summary
    assert "- `ebf.json`" in summary
    # Failure list is read from new_meta (one object entry, one bare-string entry).
    assert "### Extractor failures (2)" in summary
    assert "gregtech:gt.blockmachines/9001: empty scan" in summary
    assert "- `bartworks:unknown_controller`" in summary


def test_changed_controller_absent_on_one_side_is_ignored() -> None:
    # git can list a controller that was actually added/removed; those are not "changed".
    summary = ds.render_summary(
        old_meta={},
        new_meta={},
        old_controllers=["a.json"],
        new_controllers=["a.json", "b.json"],
        changed_controllers=["b.json"],
    )
    assert "### Changed (0)" in summary
    assert "### Added (1)" in summary


def test_controller_count_falls_back_to_listing_length() -> None:
    summary = ds.render_summary(
        old_meta={},  # no controller_count
        new_meta={},
        old_controllers=["a.json", "b.json"],
        new_controllers=["a.json"],
        changed_controllers=[],
    )
    assert "Controllers: **2 -> 1** (-1)" in summary


def test_empty_sections_render_none() -> None:
    summary = ds.render_summary({}, {}, [], [], [])
    assert summary.count("_none_") == 4
    assert "Controllers: **0 -> 0** (+0)" in summary


def test_failures_tolerates_missing_and_non_sequence() -> None:
    assert ds._failures({}) == []
    assert ds._failures({"failures": "oops"}) == []
    assert ds._failures({"failures": ["a", {"registry_name": "b"}]}) == ["a", "b: ?"]


def test_long_lists_are_truncated() -> None:
    new = [f"c{i}.json" for i in range(45)]
    summary = ds.render_summary({}, {}, [], new, [])
    assert "### Added (45)" in summary
    assert "- ... and 5 more" in summary


def test_main_writes_summary_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old_list = tmp_path / "old.txt"
    new_list = tmp_path / "new.txt"
    old_list.write_text("ebf.json\ngone.json\n", encoding="utf-8")
    new_list.write_text("ebf.json\nnew.json\n", encoding="utf-8")
    out = tmp_path / "summary.md"
    code = ds.main(
        [
            "--old-meta",
            str(FIXTURES / "old_meta.json"),
            "--new-meta",
            str(FIXTURES / "new_meta.json"),
            "--old-list",
            str(old_list),
            "--new-list",
            str(new_list),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    written = out.read_text(encoding="utf-8")
    assert "## Dataset diff" in written
    assert written == capsys.readouterr().out


def test_main_first_run_with_no_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    # A first run has no old side and no listings; it must still render, not crash.
    assert ds.main([]) == 0
    assert "Controllers: **0 -> 0**" in capsys.readouterr().out
