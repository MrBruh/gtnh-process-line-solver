"""Tests for the dataset coverage check (:mod:`gtnh_solver.dataset.coverage`, GitHub #98).

The tool's whole job is to stop a coverage number from being wrong in a way nobody notices, so the
tests are about the ways it could be: counting the wrong thing, ranking by the wrong key, or
implying it checked something it did not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.dataset.coverage import format_report, measure
from gtnh_solver.previewer.jar import cached_jar


def _doc(
    name: str,
    blocks: list[tuple[str, int]],
    *,
    substitutions: dict[str, list[tuple[str, int]]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": 2,
        "controller": {
            "registry_name": "gregtech:gt.blockmachines",
            "meta": abs(hash(name)) % 1000,
            "display_name": name,
            "source_class": "C",
        },
        "variants": [
            {
                "trigger_stack_size": 1,
                "blocks": [{"d": [0, 0, 0], "block": b, "meta": m} for b, m in blocks],
                "bbox": [1, 1, 1],
            }
        ],
        "substitutions": {
            channel: [{"channel_value": i, "block": b, "meta": m} for i, (b, m) in enumerate(subs)]
            for channel, subs in (substitutions or {}).items()
        },
    }


def _dataset(tmp_path: Path, docs: dict[str, dict[str, Any]], *, meta: bool = True) -> Path:
    directory = tmp_path / "multiblocks"
    directory.mkdir()
    for filename, doc in docs.items():
        (directory / f"{filename}.json").write_text(json.dumps(doc), encoding="utf-8")
    if meta:
        (directory / "_meta.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "pack_version": "2.8.4",
                    "generated_at": "2026-01-01T00:00:00Z",
                    "extractor_sha": "abc",
                    "controller_count": len(docs),
                    "failures": [{"registry_name": "gregtech:meta.9", "reason": "boom"}],
                }
            ),
            encoding="utf-8",
        )
    return directory


def _manifest(
    blocks: dict[str, list[str]], icons: dict[str, str], gaps: Any = ()
) -> dict[str, Any]:
    """A manifest where each block maps to the icon names its one side uses."""
    return {
        "schema": 2,
        "blocks": {
            key: {
                "kind": "block",
                "sides": {
                    "all": {"inactive": [{"icon": i, "rgba": [255] * 4, "glow": False} for i in ic]}
                },
            }
            for key, ic in blocks.items()
        },
        "icons": icons,
        "gaps": list(gaps),
    }


# ------------------------------------------------------------------------------- what is counted


def test_substitutable_blocks_are_counted_but_not_conflated_with_placed_ones(
    tmp_path: Path,
) -> None:
    """The distinction this tool exists to preserve.

    Measured on the real dump: ``IC2:blockAlloyGlass`` is in 4 controllers' block lists and is a
    ``glass`` channel alternative in 33 more. Reporting only the first reads as a rounding error;
    reporting only the sum reads as 37 broken builds. Both numbers ship.
    """
    directory = _dataset(
        tmp_path,
        {
            "a": _doc("Places It", [("mod:glass", 0)]),
            "b": _doc(
                "Offers It", [("mod:casing", 0)], substitutions={"glass": [("mod:glass", 0)]}
            ),
            "c": _doc(
                "Offers It Too", [("mod:casing", 0)], substitutions={"glass": [("mod:glass", 0)]}
            ),
        },
    )
    manifest = _manifest({"mod:casing|0": ["ic"]}, {"ic": "assets/mod/c.png"})

    coverage = measure(directory, manifest)

    assert len(coverage.unresolved) == 1
    gap = coverage.unresolved[0]
    assert gap.key == "mod:glass|0"
    assert gap.placed_in == ("Places It",)
    assert gap.substituted_in == ("Offers It", "Offers It Too")
    assert gap.multiblocks == ("Offers It", "Offers It Too", "Places It")
    assert "1 placed + 2 substitutable" in format_report(coverage)


def test_a_block_placed_and_substitutable_in_one_doc_counts_as_placed(tmp_path: Path) -> None:
    """Otherwise one controller is counted twice and the totals drift above the dump's size."""
    directory = _dataset(
        tmp_path,
        {"a": _doc("Both", [("mod:glass", 0)], substitutions={"glass": [("mod:glass", 0)]})},
    )

    gap = measure(directory, _manifest({}, {})).unresolved[0]

    assert gap.placed_in == ("Both",)
    assert gap.substituted_in == ()
    assert gap.multiblocks == ("Both",)


def test_the_recorded_gap_reason_is_carried_through(tmp_path: Path) -> None:
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 3)])})
    manifest = _manifest({}, {}, gaps=[{"block": "mod:x", "meta": 3, "reason": "getIcon was null"}])

    assert measure(directory, manifest).unresolved[0].reason == "getIcon was null"
    assert "[getIcon was null]" in format_report(measure(directory, manifest))


def test_a_pair_with_no_recorded_gap_says_so_rather_than_showing_blank(tmp_path: Path) -> None:
    """A missing block the extractor never even recorded is a different bug from one it flagged."""
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 3)])})

    assert measure(directory, _manifest({}, {})).unresolved[0].reason == ""
    assert "[no recorded gap]" in format_report(measure(directory, _manifest({}, {})))


def test_controller_failures_come_from_the_dump_meta(tmp_path: Path) -> None:
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 0)])})

    coverage = measure(directory, _manifest({}, {}))

    assert [f.registry_name for f in coverage.controller_failures] == ["gregtech:meta.9"]
    assert coverage.pack_version == "2.8.4"
    assert "gregtech:meta.9: boom" in format_report(coverage)


def test_a_dataset_without_meta_still_measures(tmp_path: Path) -> None:
    """The committed fixtures have a ``_meta.json``, but a hand-assembled directory may not."""
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 0)])}, meta=False)

    coverage = measure(directory, _manifest({}, {}))

    assert coverage.pack_version == "unknown"
    assert coverage.controllers == 1
    assert coverage.controller_failures == ()


def test_resolved_pairs_are_not_reported_as_gaps(tmp_path: Path) -> None:
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 0), ("mod:y", 1)])})
    manifest = _manifest({"mod:x|0": ["i"], "mod:y|1": ["i"]}, {"i": "assets/mod/i.png"})

    coverage = measure(directory, manifest)

    assert coverage.unresolved == ()
    assert coverage.referenced_pairs == 2
    assert coverage.resolved_pairs == 2
    assert coverage.multiblocks_with_gaps == 0


# -------------------------------------------------------------------------------------- ranking


def test_gaps_rank_by_multiblocks_touched_not_by_how_many_metas_a_family_has(
    tmp_path: Path,
) -> None:
    """Trap 6 from the texture-resolution notes, encoded.

    A family with 40 unusable metas that nothing places matters less than one block 11 controllers
    are built from. Ranking by raw count is how the wrong lane gets worked on.
    """
    docs = {f"m{i}": _doc(f"M{i}", [("mod:popular", 0)]) for i in range(5)}
    docs["lonely"] = _doc("Lonely", [("mod:niche", m) for m in range(20)])
    directory = _dataset(tmp_path, docs)

    coverage = measure(directory, _manifest({}, {}))

    assert coverage.unresolved[0].key == "mod:popular|0"
    assert len(coverage.unresolved[0].multiblocks) == 5
    assert (
        len(coverage.unresolved) == 21
    )  # the niche family is still all reported, just ranked below


# -------------------------------------------------------------------------------- sprite bytes


def test_a_resolved_name_whose_png_is_missing_from_the_jar_is_reported(tmp_path: Path) -> None:
    """The class nothing else surfaces: the name resolved, so there is no gap record anywhere."""
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 0)])})
    manifest = _manifest(
        {"mod:x|0": ["here", "gone"]},
        {
            "here": "assets/mod/here.png",
            "gone": "assets/mod/gone.png",
        },
    )

    coverage = measure(directory, manifest, jar_assets=frozenset({"assets/mod/here.png"}))

    assert coverage.unresolved == ()  # it resolved fine; that is the point
    assert [g.path for g in coverage.absent_assets] == ["assets/mod/gone.png"]
    assert coverage.absent_assets[0].multiblocks == ("A",)
    assert "name resolves but the jar has no PNG (1)" in format_report(coverage)


def test_without_a_jar_the_sprite_bytes_question_is_skipped_not_passed(tmp_path: Path) -> None:
    """A report must never imply a question it did not ask came back clean."""
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 0)])})
    manifest = _manifest({"mod:x|0": ["gone"]}, {"gone": "assets/mod/gone.png"})

    coverage = measure(directory, manifest)

    assert coverage.jar_checked is False
    assert coverage.absent_assets == ()
    report = format_report(coverage)
    assert "NOT CHECKED" in report
    assert "every resolved name has a PNG" not in report


def test_a_fully_covered_dataset_says_the_bytes_are_there(tmp_path: Path) -> None:
    directory = _dataset(tmp_path, {"a": _doc("A", [("mod:x", 0)])})
    manifest = _manifest({"mod:x|0": ["i"]}, {"i": "assets/mod/i.png"})

    coverage = measure(directory, manifest, jar_assets=frozenset({"assets/mod/i.png"}))

    assert coverage.jar_checked is True
    assert "every resolved name has a PNG in the jar" in format_report(coverage)


# ------------------------------------------------------------------------------------ the jar


def test_cached_jar_never_downloads(tmp_path: Path) -> None:
    """It answers None rather than fetching: asking the jar a question is not asking for 135 MB."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"provenance": {"mod_versions": {"GT5-Unofficial": "9.9"}}}))

    assert cached_jar(manifest, cache_dir=tmp_path) is None

    (tmp_path / "GT5-Unofficial-9.9.jar").write_bytes(b"not really a jar")
    found = cached_jar(manifest, cache_dir=tmp_path)
    assert found is not None
    assert found.name == "GT5-Unofficial-9.9.jar"


# ----------------------------------------------------------------------------------------- cli


def test_cli_dataset_coverage_reports_the_committed_fixtures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gtnh_solver.cli import main

    assert main(["--dataset-coverage"]) == 0
    captured = capsys.readouterr()
    assert "dataset coverage: pack" in captured.out
    assert "referenced (block, meta) pairs resolve to a sprite" in captured.out
    assert "dataset:" in captured.err  # which dataset answered, on stderr


def test_cli_dataset_coverage_reports_a_missing_dataset_as_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gtnh_solver import cli as cli_module

    monkeypatch.setattr(cli_module, "resolve_dataset_path", lambda *a, **k: tmp_path / "nope")

    assert cli_module.main(["--dataset-coverage"]) == 2
    assert "no multiblock dataset" in capsys.readouterr().err
