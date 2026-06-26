"""Tests for the ``gtnh-solve`` CLI - the one real Phase 1 entry point.

Drives ``main`` with argv lists and asserts the exit code (0 valid / 1 infeasible / 2 load
error) plus what lands on stdout/stderr, against the real fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.cli import main

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = str(_EXAMPLES / "gtnh-sand.json")
_NITROBENZENE = str(_EXAMPLES / "gtnh-nitrobenzene.json")


def test_cli_solves_sand_and_prints_guide(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([_SAND])
    out = capsys.readouterr().out
    assert code == 0
    assert "# Build guide" in out
    assert "Forge Hammer" in out
    assert "## Power" in out  # the synthesized power network shows up in the guide


def test_cli_writes_to_output_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "guide.txt"
    code = main([_SAND, "-o", str(target)])
    assert code == 0
    assert "# Build guide" in target.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert captured.out == ""  # the guide went to the file, not stdout
    assert str(target) in captured.err  # a confirmation went to stderr


def test_cli_seed_is_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([_SAND, "--seed", "3"]) == 0
    assert "# Build guide" in capsys.readouterr().out


def test_cli_partial_invalid_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    # nitrobenzene's multiblocks overflow crude 1x1x1 faces -> partial_invalid, reported on stderr
    code = main([_NITROBENZENE])
    err = capsys.readouterr().err
    assert code == 1
    assert "partial_invalid" in err


def test_cli_missing_export_arg_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == 2
    assert "required" in capsys.readouterr().err


def test_cli_file_not_found_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["does-not-exist.json"])
    assert code == 2
    assert "could not load" in capsys.readouterr().err


def test_cli_malformed_export_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    code = main([str(bad)])
    assert code == 2
    assert "could not load" in capsys.readouterr().err


def test_cli_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "gtnh-solve" in capsys.readouterr().out
