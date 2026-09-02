"""Tests for the ``gtnh-solve`` CLI - the one real Phase 1 entry point.

Drives ``main`` with argv lists and asserts the exit code (0 valid / 1 infeasible / 2 load
error) plus what lands on stdout/stderr, against the real fixtures.

Only the two end-to-end tests (sand and nitrobenzene) solve for real. Everything else here is
about flag plumbing or the 0/1/2 contract, so it takes the ``solve_calls`` fixture: one cached
real layout plus a record of the keywords the CLI handed the solver. Prefer that fixture for
anything new, unless the test is genuinely about solving.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver import __version__
from gtnh_solver.adapter import adapt_file
from gtnh_solver.cli import _load_physical_or_warn, main
from gtnh_solver.dataset import DatasetError, DatasetMeta, PhysicalDataset
from gtnh_solver.ir import InputIR, LayoutResult, LayoutStatus
from gtnh_solver.solver import solve

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = str(_EXAMPLES / "gtnh-sand.json")
_NITROBENZENE = str(_EXAMPLES / "gtnh-nitrobenzene.json")


@pytest.fixture(scope="session")
def sand_layout() -> LayoutResult:
    """One genuine annealed solve of the sand example, computed once for the whole session.

    A flag test only needs *a* real, valid layout to render a guide or a preview from; re-solving
    a real line per test is what made this file the slowest in the suite.
    """
    layout = solve(adapt_file(_SAND, physical=_load_physical_or_warn()))
    assert layout.status is LayoutStatus.VALID  # a stand-in for a real solve must itself be real
    return layout


@pytest.fixture
def solve_calls(
    monkeypatch: pytest.MonkeyPatch, sand_layout: LayoutResult
) -> list[dict[str, object]]:
    """Swap the CLI's solver for a recorder over the cached layout; one dict per call.

    Recording is what keeps the flag tests honest: a ``--seed`` or ``--objective`` the CLI parsed
    and then quietly dropped would still exit 0, but would not show up here.
    """
    calls: list[dict[str, object]] = []

    def recording_solve(problem: InputIR, **kwargs: object) -> LayoutResult:
        calls.append(kwargs)
        return sand_layout

    monkeypatch.setattr(cli_module, "solve", recording_solve)
    return calls


def test_cli_solves_sand_and_prints_guide(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([_SAND])
    out = capsys.readouterr().out
    assert code == 0
    assert "# Build guide" in out
    assert "Forge Hammer" in out
    assert "## Power" in out  # the synthesized power network shows up in the guide


def test_cli_writes_to_output_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    target = tmp_path / "guide.txt"
    code = main([_SAND, "-o", str(target)])
    assert code == 0
    assert "# Build guide" in target.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert captured.out == ""  # the guide went to the file, not stdout
    assert str(target) in captured.err  # a confirmation went to stderr


def test_cli_seed_is_accepted(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # exit 0 alone would still pass if --seed were parsed and then dropped, so pin the value the
    # CLI actually handed the solver
    assert main([_SAND, "--seed", "3"]) == 0
    assert "# Build guide" in capsys.readouterr().out
    assert solve_calls[-1]["seed"] == 3


def test_cli_fast_flag_skips_optimization(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # --fast is the CLI's only handle on the constructive path, so what it owns is optimize=False
    # reaching the solver (that the constructive layout is itself valid for sand is the solver
    # lane's test_fast_mode_uses_constructive_placement).
    code = main([_SAND, "--fast"])
    assert code == 0
    assert "# Build guide" in capsys.readouterr().out
    assert solve_calls[-1]["optimize"] is False


def test_cli_objective_flag_is_accepted(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # --objective selects what the optimizer treats as compact, so it only means anything on the
    # optimizing path: assert the choice arrives there, rather than that argparse swallowed it.
    assert main([_SAND, "--objective", "volume"]) == 0
    assert "# Build guide" in capsys.readouterr().out
    assert solve_calls[-1]["objective"] == "volume"
    assert solve_calls[-1]["optimize"] is True


def test_cli_rejects_an_unknown_objective(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    with pytest.raises(SystemExit):
        main([_SAND, "--objective", "tiny"])  # argparse rejects values outside the choices
    assert "--objective" in capsys.readouterr().err
    assert not solve_calls  # rejected at parse time, before any solving work


def test_cli_preview_writes_self_contained_html(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    target = tmp_path / "view.html"
    code = main([_SAND, "--preview", str(target)])
    assert code == 0
    html = target.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "OrbitControls" in html  # the camera controls are in the page
    captured = capsys.readouterr()
    assert captured.out == ""  # --preview alone suppresses the stdout guide dump
    assert "wrote preview" in captured.err


def test_cli_guide_and_preview_together(
    tmp_path: Path, solve_calls: list[dict[str, object]]
) -> None:
    guide_file = tmp_path / "guide.txt"
    preview_file = tmp_path / "view.html"
    code = main([_SAND, "-o", str(guide_file), "--preview", str(preview_file)])
    assert code == 0
    assert "# Build guide" in guide_file.read_text(encoding="utf-8")
    assert "<!doctype html>" in preview_file.read_text(encoding="utf-8")


def test_cli_list_dataset_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_module, "list_versions", lambda: [Path("data/2.9.0"), Path("data/2.8.4")]
    )
    code = main(["--list-dataset-versions"])
    assert code == 0
    assert capsys.readouterr().out.split() == ["2.9.0", "2.8.4"]  # newest first, folder names


def test_cli_list_dataset_versions_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "list_versions", list)  # no versions present -> []
    code = main(["--list-dataset-versions"])
    assert code == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "no generated dataset versions" in out.err


def test_cli_dataset_version_unknown_falls_back(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # An unknown version resolves to an absent data/<v>/multiblocks; the load warns and falls back
    # to 1x1x1 footprints instead of failing the run. (That the fallback still solves valid is
    # test_cli_falls_back_when_dataset_load_fails, which keeps its real solve.)
    code = main([_SAND, "--dataset-version", "does-not-exist"])
    assert code == 0
    assert "physical multiblock dataset unavailable" in capsys.readouterr().err


@cache
def _line_resolves_multiblocks() -> bool:
    """Whether the resolved dataset actually knows the nitrobenzene line's machines.

    Generated dumps are local and version-namespaced by policy, so a checkout has only the two
    committed fixtures (Electric Blast Furnace, Vacuum Freezer) and every machine on this line
    falls back to the 1x1x1 default. That decides the outcome below, so the test reads it instead
    of assuming the author's machine.
    """
    ir = adapt_file(_NITROBENZENE, physical=_load_physical_or_warn())
    return any(m.footprint.volume > 1 for m in ir.machines)


def test_cli_solves_nitrobenzene(capsys: pytest.CaptureFixture[str]) -> None:
    """End to end on the multiblock line, asserting what the available dataset actually allows.

    With the real structure dump this is VALID, and the multi-hatch power model is what makes it
    so: the Coke Oven draws far more than one energy hatch can take, and before that model the MV
    net was rejected outright. With fixtures alone every machine is a 1x1x1 block, the HV
    Distillation Tower needs 7 connections against 5 usable faces, and the honest answer is an
    explicit face-reachability infeasibility.

    Both are asserted rather than folded into a disjunction, because each is a real property of
    its own configuration. This test read the fixtures case until the energy-hatch model landed
    and replaced it with an unconditional exit 0 - true on a machine carrying the full dump, and
    the reason CI has been red since that work was first pushed.
    """
    code = main([_NITROBENZENE])
    assert "# Build guide" in capsys.readouterr().out  # the guide is emitted either way
    if _line_resolves_multiblocks():
        assert code == 0
    else:
        assert code == 1


def test_cli_partial_invalid_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # An off-ladder voltage tier has no voltage to size amperage against, so the power router
    # reports it and the layout comes back infeasible - the exit-1 path, on stderr.
    export = tmp_path / "off-ladder.json"
    export.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "recipes": [
                    {
                        "id": "r",
                        "machineType": "M",
                        "durationTicks": 10,
                        "eut": 32,
                        "outputs": [{"kind": "item", "id": "x", "amount": 1}],
                    }
                ],
                "nodes": [{"id": "n", "recipeId": "r", "overclockTier": "ZZZ"}],
            }
        ),
        encoding="utf-8",
    )
    code = main([str(export)])
    err = capsys.readouterr().err
    assert code == 1
    assert "partial_invalid" in err


def test_cli_missing_export_arg_returns_2(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    code = main([])
    assert code == 2
    assert "required" in capsys.readouterr().err
    assert not solve_calls  # the exit-2 paths bail early; they must not solve first


def test_cli_file_not_found_returns_2(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    code = main(["does-not-exist.json"])
    assert code == 2
    assert "could not load" in capsys.readouterr().err
    assert not solve_calls


def test_cli_malformed_export_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    code = main([str(bad)])
    assert code == 2
    assert "could not load" in capsys.readouterr().err
    assert not solve_calls


def test_cli_unwritable_output_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # an unwritable output path (parent dir missing -> OSError) is reported and exits 2 per the
    # documented 0/1/2 contract, not dumped as a raw traceback (GitHub #39)
    target = tmp_path / "missing-dir" / "guide.txt"
    code = main([_SAND, "-o", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "could not write" in err
    assert str(target) in err


def test_cli_unwritable_preview_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # same guard on the --preview write path
    target = tmp_path / "missing-dir" / "view.html"
    code = main([_SAND, "--preview", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "could not write" in err
    assert str(target) in err


def test_cli_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "gtnh-solve" in capsys.readouterr().out


def test_package_exposes_a_nonempty_version_string() -> None:
    # The package exports a version; the CLI's --version reports it. (Folded in from the retired
    # tests/test_smoke.py scaffolding.)
    assert isinstance(__version__, str)
    assert __version__


# ------------------------------------------- physical dataset wiring + graceful fallback (GAP A)


def _empty_dataset() -> PhysicalDataset:
    meta = DatasetMeta.model_validate(
        {
            "schema": 1,
            "pack_version": "test",
            "generated_at": "now",
            "extractor_sha": "0",
            "controller_count": 0,
        }
    )
    return PhysicalDataset(meta=meta, machines={})


def test_load_physical_returns_the_real_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    # The healthy path: the committed dump loads, so multiblocks can get real footprints, and a
    # healthy load is silent (no spurious warning on the normal run).
    ds = _load_physical_or_warn()
    assert ds is not None
    assert ds.get("Electric Blast Furnace") is not None
    assert capsys.readouterr().err == ""


def test_cli_threads_the_dataset_into_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # Prove the wiring: the CLI must hand the loaded physical dataset to adapt_file so multiblocks
    # resolve to real footprints (not the 1x1x1 default the no-arg adapt would give).
    captured: dict[str, object] = {}
    real = cli_module.adapt_file

    def spy(path: str, *, physical: object = None) -> object:
        captured["physical"] = physical
        return real(path, physical=physical)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "adapt_file", spy)
    assert main([_SAND]) == 0
    dataset = captured["physical"]
    assert isinstance(dataset, PhysicalDataset)
    assert dataset.get("Electric Blast Furnace") is not None


def test_cli_falls_back_when_dataset_load_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A broken dataset must NOT crash the CLI or change the 0/1/2 contract: it warns and falls back
    # to 1x1x1 footprints, so sand still solves valid (exit 0).
    def boom(*_a: object, **_k: object) -> PhysicalDataset:
        raise DatasetError("simulated bad scan bound")

    monkeypatch.setattr(cli_module, "load_physical_dataset", boom)
    assert _load_physical_or_warn() is None
    assert "unavailable" in capsys.readouterr().err
    assert main([_SAND]) == 0
    assert "using 1x1x1 footprints" in capsys.readouterr().err


def test_cli_falls_back_on_an_empty_dataset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty dump knows no machine types, so it is equivalent to no dataset: warn and fall back.
    monkeypatch.setattr(cli_module, "load_physical_dataset", lambda *a, **k: _empty_dataset())
    assert _load_physical_or_warn() is None
    assert "empty" in capsys.readouterr().err
