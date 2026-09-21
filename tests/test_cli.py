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
import os
import shutil
from functools import cache
from pathlib import Path
from typing import Any

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver import __version__
from gtnh_solver.adapter import Plan, PlanProducer, adapt_file, load_plan, to_input_ir
from gtnh_solver.cli import _load_physical_or_warn, _warn_if_plan_pack_undumped, main
from gtnh_solver.dataset import (
    DatasetError,
    DatasetMeta,
    PhysicalDataset,
    load_physical_dataset,
)
from gtnh_solver.dataset import roots as dataset_roots
from gtnh_solver.ir import InputIR, LayoutResult, LayoutStatus
from gtnh_solver.solver import solve
from tests._helpers import hatched_dataset

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = str(_EXAMPLES / "gtnh-sand.json")
_NITROBENZENE = str(_EXAMPLES / "gtnh-nitrobenzene.json")
#: The committed fixtures: a two-machine SAMPLE declaring ``census: false``. Named here so a test
#: can pin that configuration rather than inherit whichever dump the machine happens to hold
#: (docs/TESTING.md, "CI sees a smaller dataset than you do").
_FIXTURE_DATASET = Path(__file__).resolve().parents[1] / "data" / "multiblocks"


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
    # The CLI's own contract is that it delegates to the previewer and writes the bytes out, so
    # assert that, not a three.js identifier the page happens to mention (GitHub #94): the file is
    # the page, scene payload and all. What the page then does with the scene is the previewer's
    # own tests.
    assert html.startswith("<!doctype html>")
    assert "const SCENE = " in html
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


# ------------------------------------------- a plan whose pack has no local dump (#207)


def _undumped(path: str) -> tuple[Plan, PhysicalDataset, InputIR]:
    """``path``'s plan adapted the way a fresh clone adapts it: against the committed sample."""
    plan = load_plan(path)
    physical = _load_physical_or_warn()
    assert physical is not None
    assert not physical.meta.census, "the suite is pinned to the committed two-machine sample"
    return plan, physical, to_input_ir(plan, physical=physical)


def _ebf_plan(tmp_path: Path) -> str:
    """A one-node 2.8.4 plan whose only machine the committed sample holds."""
    export = tmp_path / "ebf.json"
    recipe = {
        "id": "r",
        "machineType": "Electric Blast Furnace",
        "durationTicks": 10,
        "eut": 120,
        "outputs": [{"kind": "item", "id": "x", "amount": 1}],
        "source": {"datasetVersionId": "stable-2.8.4"},
    }
    export.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "recipes": [recipe],
                "nodes": [{"id": "n", "recipeId": "r", "overclockTier": "MV"}],
            }
        ),
        encoding="utf-8",
    )
    return str(export)


def test_cli_warns_when_the_plans_pack_has_no_local_dump(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    """The fresh-clone case, through ``main``: nothing else on this path says a word about it.

    Sand has no multiblock, and that is fine: the warning names what found no structure and lets
    the reader see a Forge Hammer is a single block, rather than guessing which ones are.
    """
    assert main([_SAND]) == 0
    err = capsys.readouterr().err
    assert "warning: the plan was balanced against GTNH 2.8.4, which has no local dump" in err
    assert "2-controller sample" in err
    assert "(Forge Hammer)" in err
    assert "1x1x1 footprint" in err
    assert "lone controller" in err
    assert str(dataset_roots.DEFAULT_DATA / "2.8.4" / "multiblocks") in err
    assert "runServer -PdatasetOut=../../data/2.8.4 -PpackVersion=2.8.4" in err


def test_the_undumped_pack_warning_names_every_machine_type_that_found_no_structure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The line where it matters: each of these multiblocks reserves one cell, which is why the
    # shipped nitrobenzene line is infeasible on a fresh clone (test_cli_solves_nitrobenzene).
    plan, physical, problem = _undumped(_NITROBENZENE)
    _warn_if_plan_pack_undumped(plan, None, physical, problem)
    err = capsys.readouterr().err
    assert (
        "4 machine type(s) found none in it "
        "(Chemical Plant, Coke Oven, Distillation Tower, Large Chemical Reactor)"
    ) in err


def test_the_undumped_pack_warning_is_quiet_when_the_plans_dump_loaded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A derived or pinned version means a dump of the plan's pack (or the user's pick) is in use.
    plan, physical, problem = _undumped(_NITROBENZENE)
    _warn_if_plan_pack_undumped(plan, "2.8.4", physical, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_leaves_a_census_to_the_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Another pack's census draws the adapter's mismatch warning; saying it twice teaches readers
    # to skip both.
    plan, _, problem = _undumped(_NITROBENZENE)
    census = _empty_dataset()
    assert census.meta.census, "DatasetMeta defaults census to True"
    _warn_if_plan_pack_undumped(plan, None, census, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_leaves_a_failed_load_to_its_own_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # _load_physical_or_warn already said "using 1x1x1 footprints" when it returned None.
    plan, _, problem = _undumped(_NITROBENZENE)
    _warn_if_plan_pack_undumped(plan, None, None, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_is_quiet_for_a_plan_stating_no_pack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan, physical, problem = _undumped(_NITROBENZENE)
    for recipe in plan.recipes:
        recipe.source = None
    _warn_if_plan_pack_undumped(plan, None, physical, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_is_quiet_when_the_sample_covers_the_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every machine type found its structure, so nothing fell to 1x1x1 and there is nothing to say.
    plan, physical, problem = _undumped(_ebf_plan(tmp_path))
    (furnace,) = [m for m in problem.machines if m.type == "Electric Blast Furnace"]
    assert furnace.footprint.volume > 1, "the sample holds the EBF, so it keeps its real footprint"
    _warn_if_plan_pack_undumped(plan, None, physical, problem)
    assert capsys.readouterr().err == ""


@cache
def _line_resolves_multiblocks() -> bool:
    """Whether the dataset the CLI resolves knows the nitrobenzene line's machines.

    Generated dumps are local and version-namespaced by policy, and the suite pins resolution to
    the committed data (``conftest._pinned_dataset_root``), which is two fixtures (Electric Blast
    Furnace, Vacuum Freezer) that this line uses neither of. So every machine on it falls back to
    the 1x1x1 default, and this reads that off the CLI's own load path rather than asserting it
    from the outside.
    """
    ir = adapt_file(_NITROBENZENE, physical=_load_physical_or_warn())
    return any(m.footprint.volume > 1 for m in ir.machines)


def test_cli_solves_nitrobenzene(capsys: pytest.CaptureFixture[str]) -> None:
    """End to end on the multiblock line, asserting what the pinned dataset actually allows.

    With fixtures alone every machine is a 1x1x1 block, the HV Distillation Tower needs 7
    connections against 5 usable faces, and the honest answer is an explicit face-reachability
    infeasibility. With the real structure dump the same line is VALID instead, and the multi-hatch
    power model is what makes it so: the Coke Oven draws far more than one energy hatch can take,
    and before that model the MV net was rejected outright.

    Which of the two this asserts used to depend on the machine it ran on. It does not any more
    (#182): the pin puts every run in the fixtures configuration, so the configuration is asserted
    first and the outcome unconditionally after it. The full-dump outcome is a real property of a
    configuration the suite no longer resolves, and would need that dump staged to be tested.
    """
    code = main([_NITROBENZENE])
    assert "# Build guide" in capsys.readouterr().out  # the guide is emitted either way
    assert not _line_resolves_multiblocks(), "the suite is pinned to the committed fixtures"
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


def test_cli_a_tier_too_low_to_power_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # A ULV multiblock: the export parses and maps, but 8 V does not survive the 16-block run the
    # adapter sizes energy hatches for, so no layout exists to solve for. The answer is the exit-1
    # infeasibility, printed in the same shape as the solver's own verdict - not exit 2, which
    # would say the file could not be read, and not the raw UnpowerableError traceback this used
    # to be (#112). A structural record is what makes the sizing happen at all, so the dataset is
    # pinned rather than inherited from whichever dump the machine holds.
    export = tmp_path / "ulv.json"
    export.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "recipes": [
                    {
                        "id": "r",
                        "machineType": "M",
                        "durationTicks": 10,
                        "eut": 6,
                        "outputs": [{"kind": "item", "id": "x", "amount": 1}],
                    }
                ],
                "nodes": [{"id": "n", "recipeId": "r", "overclockTier": "ULV"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "_load_physical_or_warn", lambda *_, **__: hatched_dataset())
    code = main([str(export)])
    err = capsys.readouterr().err
    assert code == 1
    assert "[infeasible] voltage_drop:" in err
    assert "ULV" in err
    assert "try: " in err  # the relaxation, the same line a solver infeasibility prints
    assert not solve_calls  # the verdict is the adapter's; nothing was solved to reach it


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


def _under_a_file(tmp_path: Path, name: str) -> Path:
    """A target whose directory exists as a regular FILE, so no amount of mkdir can write it.

    A merely missing directory used to be how these tests got an unwritable path; the CLI now
    creates it (#150), so the guard is exercised with a parent that genuinely cannot be one.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    return blocker / name


def _spliced(tmp_path: Path, name: str, plan: Any, literal: str) -> str:
    """Write ``plan`` to ``name``, putting ``literal`` where a ``"REPLACE_ME"`` string stands.

    The two figures under test cannot be written through ``json.dumps``: ``1e400`` round-trips as
    ``Infinity``, which no real exporter emits, and a 310-digit int has to reach the parser
    exactly as written. So they go in as raw JSON text.
    """
    export = tmp_path / name
    export.write_text(json.dumps(plan).replace('"REPLACE_ME"', literal), encoding="utf-8")
    return str(export)


def _sand_plan() -> Any:
    return json.loads(Path(_SAND).read_text(encoding="utf-8"))


def test_cli_non_finite_figure_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # An export whose totalEut is 1e400 (inf, once parsed) is a plan that could not be loaded, so
    # it must exit 2. It used to raise OverflowError out of power synthesis: an ArithmeticError,
    # missed by the load guard, which left a traceback and exit 1 - the code that means the solver
    # returned an explicit infeasibility (#115).
    plan = _sand_plan()
    plan["resolved"]["machines"][0]["totalEut"] = "REPLACE_ME"
    export = _spliced(tmp_path, "inf.json", plan, "1e400")
    code = main([export])
    assert code == 2
    assert "could not load" in capsys.readouterr().err
    assert not solve_calls


def test_cli_unbounded_multiplier_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # Same contract for the other unvalidated numeric: a 310-digit parallel, which used to reach
    # core._rate and raise "int too large to convert to float".
    plan = _sand_plan()
    plan["nodes"][0]["parallel"] = "REPLACE_ME"
    export = _spliced(tmp_path, "parallel.json", plan, "9" * 310)
    code = main([export])
    assert code == 2
    assert "could not load" in capsys.readouterr().err
    assert not solve_calls


def test_cli_arithmetic_the_contracts_miss_is_still_a_load_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # The contracts now refuse both figures #115 found, but the mapping multiplies far more than
    # those two, and OverflowError is an ArithmeticError rather than a ValueError. The load guard
    # takes the whole family, so the next one of these reads as exit 2 and not as a traceback.
    def overflowing(*args: object, **kwargs: object) -> InputIR:
        raise OverflowError("int too large to convert to float")

    monkeypatch.setattr(cli_module, "to_input_ir", overflowing)
    code = main([_SAND])
    assert code == 2
    assert "could not load" in capsys.readouterr().err
    assert not solve_calls


def test_cli_an_unexpected_exception_is_an_internal_error_not_an_infeasibility(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Everything the solve stretch knows how to fail at comes back as an Infeasibility, so an
    # exception raised there is a bug in this program. It gets its own exit code: a caller keying
    # on the exit code must not read a crash as exit 1 ("an explicit infeasibility") or as exit 2
    # ("the export could not be loaded"), and the traceback is kept because filing it is the only
    # thing to do with it.
    def exploding_solve(problem: InputIR, **kwargs: object) -> LayoutResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "solve", exploding_solve)
    code = main([_SAND])
    err = capsys.readouterr().err
    assert code == cli_module.INTERNAL_ERROR_EXIT == 3
    assert "internal error: RuntimeError: boom" in err
    assert "Traceback" in err  # the useful half of an internal error


def test_cli_a_preview_that_is_not_a_write_failure_is_an_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # The preview builds a whole scene before it writes anything, and that half had no guard at
    # all: only OSError was caught, so a scene-build bug escaped as a traceback. An OSError is
    # still the user's problem (exit 2, "could not write"); anything else is ours.
    def exploding_preview(*args: object, **kwargs: object) -> None:
        raise RuntimeError("scene")

    monkeypatch.setattr(cli_module, "write_preview", exploding_preview)
    code = main([_SAND, "--preview", str(tmp_path / "view.html")])
    err = capsys.readouterr().err
    assert code == cli_module.INTERNAL_ERROR_EXIT
    assert "internal error: RuntimeError: scene" in err


def test_cli_unwritable_output_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # an unwritable output path (OSError) is reported and exits 2 per the documented 0/1/2
    # contract, not dumped as a raw traceback (GitHub #39)
    target = _under_a_file(tmp_path, "guide.txt")
    code = main([_SAND, "-o", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "could not write" in err
    assert str(target) in err


def test_cli_unwritable_preview_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # same guard on the --preview write path
    target = _under_a_file(tmp_path, "view.html")
    code = main([_SAND, "--preview", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "could not write" in err
    assert str(target) in err


def test_cli_unwritable_schematic_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # and on the --schematic one, whose writer already made its directory: the three agree
    target = _under_a_file(tmp_path, "line.schematic")
    code = main([_SAND, "--schematic", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "could not write" in err
    assert str(target) in err


def _stage_pack(root: Path, version: str, *, mtime: float, typed: bool = True) -> None:
    """A whole ``data/<version>/`` dump built from the committed data, dated ``mtime``.

    ``typed=False`` empties the manifest's ``blocks``, so an export that consults it refuses every
    block by name - which makes "which manifest did the export read" observable from the outside.
    """
    shutil.copytree(_FIXTURE_DATASET, root / version / "multiblocks")
    raw = json.loads((_FIXTURE_DATASET.parent / "textures" / "manifest.json").read_text("utf-8"))
    if not typed:
        raw["blocks"] = {}
    manifest = root / version / "textures" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    os.utime(root / version, (mtime, mtime))


def test_cli_schematic_follows_the_plans_pack_like_the_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    """#206: ``--schematic`` was handed ``--dataset-version`` as typed, not the derived version.

    So unpinned, it resolved the manifest by modification time while ``--preview`` and the adapter
    followed the plan's pack: on a machine holding a newer dump of another pack, one solve came out
    as a preview of one pack and an export built from the other's block ids. Here the newer pack's
    manifest types nothing, so reading it is an exit-2 refusal rather than a quiet mismatch.
    """
    data = tmp_path / "data"
    _stage_pack(data, "2.8.4", mtime=1_000_000)  # the sand plan's own pack
    _stage_pack(data, "9.9.9", mtime=2_000_000, typed=False)  # newer, and some other pack
    monkeypatch.setattr(dataset_roots, "DEFAULT_DATA", data)

    target = tmp_path / "line.schematic"
    assert main([_SAND, "--schematic", str(target)]) == 0, capsys.readouterr().err
    assert target.is_file()


def test_cli_schematic_with_a_pinned_version_missing_its_manifest_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # A pinned version with no manifest used to escape write_schematic as FileNotFoundError and
    # read "could not write line.schematic", blaming the one path that was fine (#206).
    target = tmp_path / "line.schematic"
    code = main([_SAND, "--dataset-version", "does-not-exist", "--schematic", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "cannot export" in err
    assert "could not write" not in err
    assert str(Path("does-not-exist") / "textures" / "manifest.json") in err
    assert "runClient" in err
    assert not target.exists()


@pytest.mark.parametrize(
    ("flag", "name"),
    [("-o", "guide.txt"), ("--preview", "view.html"), ("--schematic", "line.schematic")],
)
def test_cli_creates_the_directory_an_output_sits_in(
    flag: str,
    name: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # Every other write test builds its target straight under tmp_path, which pytest always
    # creates, so this case could never arise in them. It is the documented workflow's case,
    # though: out/ is gitignored, so on a fresh clone `--preview out/sand.html` failed on its
    # first run, after the whole solve and texture bake (#150). Two levels deep, because one
    # missing level would not prove the parents=True half. All three flags, because "put it
    # here" has to mean the same thing for each of them.
    target = tmp_path / "out" / "nested" / name
    code = main([_SAND, flag, str(target)])
    assert code == 0
    assert target.is_file()
    assert target.stat().st_size > 0
    assert "could not write" not in capsys.readouterr().err


def test_cli_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "gtnh-solve" in capsys.readouterr().out


def test_package_exposes_a_nonempty_version_string() -> None:
    # The package exports a version; the CLI's --version reports it. (Folded in from the retired
    # tests/test_smoke.py scaffolding.) That it is a `str` is mypy's job, not a test's (#94); that
    # it is not empty is this one's.
    assert __version__


# ------------------------------------------- physical dataset wiring + graceful fallback (GAP A)


def _empty_dataset() -> PhysicalDataset:
    meta = DatasetMeta.model_validate(
        {
            "schema": 2,
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
    # Prove the wiring: the CLI must hand the loaded physical dataset to the mapping so multiblocks
    # resolve to real footprints (not the 1x1x1 default the no-arg adapt would give).
    captured: dict[str, PhysicalDataset | None] = {}

    # Calls the real to_input_ir through its own module rather than through `cli_module`, which
    # only re-exports it: same function, and the spy carries to_input_ir's actual signature, so a
    # change to it fails here instead of being absorbed by an `object` parameter.
    def spy(
        plan: Plan,
        *,
        physical: PhysicalDataset | None = None,
        producer: PlanProducer | None = None,
    ) -> InputIR:
        captured["physical"] = physical
        return to_input_ir(plan, physical=physical, producer=producer)

    monkeypatch.setattr(cli_module, "to_input_ir", spy)
    assert main([_SAND]) == 0
    dataset = captured["physical"]
    assert isinstance(dataset, PhysicalDataset)
    assert dataset.get("Electric Blast Furnace") is not None


@pytest.fixture
def sample_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the CLI to the committed fixtures, whatever dump this checkout happens to hold.

    The abstention tests below are *about* the note, and only a **sample** dump produces one: a
    miss in a two-machine sample is no evidence, so the adapter states no ceiling and the
    validator abstains. In a **census** dump a miss is positive evidence the machine is a basic
    machine, `max_amps` is stated, the intake is measured, and there is correctly no note at all
    (`dataset/schema.py`: `census` defaults to True, so any pre-#129 local dump reads as one).

    Without this pin the tests assert the sample answer while silently inheriting whichever
    configuration the machine has, which passes in CI and fails on any checkout with a real local
    dump (#134). Branching on what resolved is the wrong tool here: the census side emits nothing
    to assert, so the branch would collapse into a disjunction that holds in every configuration
    and therefore pins nothing (docs/TESTING.md).
    """
    monkeypatch.setattr(
        cli_module,
        "load_physical_dataset",
        lambda *_a, **_k: load_physical_dataset(_FIXTURE_DATASET),
    )


def test_cli_says_how_many_machines_went_unmeasured_for_power_intake(
    sample_dataset: None, solve_calls: list[dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    # Against a SAMPLE dump nothing says whether a machine is a basic machine or a multiblock, so
    # the validator's under-supply check abstains and the run must SAY so. Silence there used to
    # be indistinguishable from "checked and fine" (#114). Sand is three Forge Hammers and the
    # committed fixtures are a two-machine sample, so all three go unmeasured.
    assert main([_SAND]) == 0
    err = capsys.readouterr().err
    assert "power intake unmeasured for 3 of 3 powered machine(s)" in err


def test_the_unmeasured_note_is_advisory_and_does_not_change_the_exit_code(
    sample_dataset: None, solve_calls: list[dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    # A check that could not run proves nothing either way, so it must not fail the layout: the
    # note rides stderr next to the dataset warnings and the 0/1/2 contract is untouched.
    assert main([_SAND]) == 0
    out, err = capsys.readouterr()
    assert "note:" in err
    assert "note:" not in out  # the build guide on stdout stays pipeable


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
