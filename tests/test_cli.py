"""Tests for the ``gtnh-solve`` CLI - the one real Phase 1 entry point.

Drives ``main`` with argv lists and asserts the exit code (0 valid / 1 infeasible / 2 load
error / 3 internal error) plus what lands on stdout/stderr, against the real fixtures. With no
artifact flag stdout is the ``LayoutResult`` contract as JSON and nothing else, so a test reads it
back through :func:`_published` rather than looking for text in it.

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
from typing import Any, Final

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver import __version__
from gtnh_solver.adapter import (
    AdapterWarning,
    MachineHandler,
    Node,
    Plan,
    PlanProducer,
    Recipe,
    RecipeSource,
    Resource,
    adapt_file,
    load_plan,
    to_input_ir,
)
from gtnh_solver.cli import (
    _load_physical_or_warn,
    _manifest_says_single_block,
    _unconnected_nets,
    _warn_if_plan_pack_undumped,
    _warn_incomplete_export,
    main,
)
from gtnh_solver.dataset import (
    DatasetError,
    DatasetMeta,
    PhysicalDataset,
    load_physical_dataset,
)
from gtnh_solver.dataset import roots as dataset_roots
from gtnh_solver.ir import (
    LAYOUT_RESULT_VERSION,
    Commodity,
    Infeasibility,
    InputIR,
    LayoutResult,
    LayoutStatus,
    METoggles,
)
from gtnh_solver.previewer.textures import TextureManifest
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

    A flag test only needs *a* real, valid layout to publish or preview; re-solving a real line
    per test is what made this file the slowest in the suite.
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


def _published(out: str) -> LayoutResult:
    """Read stdout back as the contract, asserting it is that JSON and nothing else.

    ``model_validate_json`` refuses anything but one JSON document, so a stray line of prose on
    stdout (a warning printed to the wrong stream) fails here rather than slipping through. The
    second half is the round trip: the parsed model dumps back to exactly the JSON that was
    printed, so nothing the CLI wrote was dropped or reinterpreted on the way in. And it is ASCII,
    which is what keeps it intact through a console or a redirect of any encoding.
    """
    layout = LayoutResult.model_validate_json(out)
    assert json.loads(out) == layout.model_dump(mode="json")
    assert out.isascii()
    return layout


def test_cli_solves_sand_and_prints_the_layout_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    # The real end-to-end run, and the one the default output exists for: a VALID layout, printed
    # as the contract a script can consume (docs/IR.md), that reads back through the contract.
    code = main([_SAND])
    captured = capsys.readouterr()
    assert code == 0
    layout = _published(captured.out)
    assert layout.status is LayoutStatus.VALID
    assert layout.infeasibility is None
    assert layout.version == LAYOUT_RESULT_VERSION  # published, not changed: the schema is as-is
    assert len(layout.placements) >= 3  # the three hammers, at least
    # the synthesized power network is part of the published layout, not only of a rendering
    assert any(r.commodity is Commodity.POWER for r in layout.routes)


def test_cli_seed_is_accepted(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # exit 0 alone would still pass if --seed were parsed and then dropped, so pin the value the
    # CLI actually handed the solver
    assert main([_SAND, "--seed", "3"]) == 0
    _published(capsys.readouterr().out)
    assert solve_calls[-1]["seed"] == 3


def test_cli_fast_flag_skips_optimization(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # --fast is the CLI's only handle on the constructive path, so what it owns is optimize=False
    # reaching the solver (that the constructive layout is itself valid for sand is the solver
    # lane's test_fast_mode_uses_constructive_placement).
    code = main([_SAND, "--fast"])
    assert code == 0
    _published(capsys.readouterr().out)
    assert solve_calls[-1]["optimize"] is False


def test_cli_objective_flag_is_accepted(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # --objective selects what the optimizer treats as compact, so it only means anything on the
    # optimizing path: assert the choice arrives there, rather than that argparse swallowed it.
    assert main([_SAND, "--objective", "volume"]) == 0
    _published(capsys.readouterr().out)
    assert solve_calls[-1]["objective"] == "volume"
    assert solve_calls[-1]["optimize"] is True


def test_cli_rejects_an_unknown_objective(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    with pytest.raises(SystemExit):
        main([_SAND, "--objective", "tiny"])  # argparse rejects values outside the choices
    assert "--objective" in capsys.readouterr().err
    assert not solve_calls  # rejected at parse time, before any solving work


def test_cli_jobs_reaches_the_solver(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # --jobs only changes how long a solve takes, never its layout, so nothing downstream would
    # notice it being dropped: pin the value the CLI hands the solver.
    assert main([_SAND, "--jobs", "3"]) == 0
    _published(capsys.readouterr().out)
    assert solve_calls[-1]["jobs"] == 3


def test_cli_jobs_defaults_to_one_per_cpu(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    assert main([_SAND]) == 0
    _published(capsys.readouterr().out)
    assert solve_calls[-1]["jobs"] == (os.cpu_count() or 1)


def test_cli_rejects_fewer_than_one_job(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    with pytest.raises(SystemExit):
        main([_SAND, "--jobs", "0"])
    assert "--jobs" in capsys.readouterr().err
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
    assert captured.out == ""  # --preview makes the page the answer; no layout JSON on stdout
    assert "wrote preview" in captured.err


@pytest.mark.parametrize(
    "flags",
    [("--preview",), ("--schematic",), ("--preview", "--schematic")],
    ids=["preview", "schematic", "both"],
)
def test_cli_an_artifact_flag_keeps_stdout_empty(
    flags: tuple[str, ...],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # Either artifact, or both, is the answer the run was asked for, so stdout stays quiet the way
    # it did for the text guide: the layout JSON is the default only when nothing else was asked.
    names = {"--preview": "view.html", "--schematic": "line.schematic"}
    argv = [_SAND]
    for flag in flags:
        argv += [flag, str(tmp_path / names[flag])]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    for flag in flags:
        assert (tmp_path / names[flag]).stat().st_size > 0
        assert f"wrote {flag.lstrip('-')} to" in captured.err  # the confirmations are on stderr


@pytest.mark.parametrize("flag", ["-o", "--output"])
def test_cli_the_retired_output_flag_is_a_usage_error(
    flag: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # -o wrote the text build guide, which is gone (#203). It is not kept or repurposed for the
    # JSON, which goes to a file with `> layout.json`: an argparse usage error at parse time,
    # before any solving, so an old script fails loudly instead of quietly writing something new.
    target = tmp_path / "guide.txt"
    with pytest.raises(SystemExit) as exc:
        main([_SAND, flag, str(target)])
    assert exc.value.code == 2  # argparse's own usage-error code
    captured = capsys.readouterr()
    assert f"unrecognized arguments: {flag}" in captured.err
    assert captured.out == ""
    assert not target.exists()
    assert not solve_calls


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

_UNDUMPED = "warning: no local dump for the plan's pack"


def _undumped(plan: Plan) -> tuple[Plan, PhysicalDataset, InputIR]:
    """``plan`` adapted the way a fresh clone adapts it: against the committed sample."""
    physical = _load_physical_or_warn()
    assert physical is not None
    assert not physical.meta.census, "the suite is pinned to the committed two-machine sample"
    return plan, physical, to_input_ir(plan, physical=physical)


def _one_node_plan(machine_type: str, *, kind: str = "", tier: str = "LV") -> Plan:
    """A one-node 2.8.4 plan running ``machine_type``, through an arodoid handler of ``kind``."""
    handlers = [MachineHandler(id="h", kind=kind, label=machine_type)] if kind else []
    return Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r",
                machine_type=machine_type,
                eut=30.0,
                duration_ticks=10.0,
                outputs=[Resource(kind="item", id="x", amount=1.0)],
                machine_handlers=handlers,
                source=RecipeSource(dataset_version_id="stable-2.8.4"),
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier=tier, machine_handler_id="h")],
    )


def _warning_for(plan: Plan, capsys: pytest.CaptureFixture[str]) -> str:
    """What the #207 check prints for ``plan`` on a fresh clone, or ``""``."""
    plan, physical, problem = _undumped(plan)
    _warn_if_plan_pack_undumped(plan, None, physical, problem)
    return capsys.readouterr().err


def test_cli_is_quiet_about_an_undumped_pack_whose_machines_are_all_single_blocks(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # The fresh-clone sand line: its Forge Hammer found no structure, but the manifest records it
    # as a basic machine, so nothing is lost and a new contributor's first run stays quiet.
    assert main([_SAND]) == 0
    assert _UNDUMPED not in capsys.readouterr().err


def test_the_undumped_pack_warning_names_exactly_the_multiblock_types(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The line where it matters: each of these reserves one cell, which is why the shipped
    # nitrobenzene line is infeasible on a fresh clone (test_cli_solves_nitrobenzene).
    err = _warning_for(load_plan(_NITROBENZENE), capsys)
    assert err.startswith(f"{_UNDUMPED} 2.8.4, so these reserve 1x1x1 footprints")
    assert (
        ": Chemical Plant, Coke Oven, Distillation Tower, Large Chemical Reactor. Fix: make "
    ) in err
    assert "1x1x1 footprints" in err
    assert "lone controllers" in err
    missing = str(dataset_roots.DEFAULT_DATA / "2.8.4" / "multiblocks")
    assert missing in err
    assert "tools/gtnh-extractor/README.md" in err
    assert len(err) - len(missing) < 300, "a warning is read, so it stays short"


def test_a_handler_saying_single_keeps_a_type_off_the_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No manifest entry, so only the plan can say it: and it does.
    assert _warning_for(_one_node_plan("Mystery Machine", kind="single"), capsys) == ""


def test_a_type_nothing_identifies_is_listed_rather_than_guessed_away(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ": Mystery Machine. Fix:" in _warning_for(_one_node_plan("Mystery Machine"), capsys)


def test_a_handler_saying_multiblock_outranks_a_manifest_saying_single(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The manifest records a Forge Hammer as a basic machine, but the plan names the machine the
    # node was built in, and a handler kind is not a heuristic.
    err = _warning_for(_one_node_plan("Forge Hammer", kind="multiblock"), capsys)
    assert ": Forge Hammer. Fix:" in err
    assert _warning_for(_one_node_plan("Forge Hammer"), capsys) == "", (
        "the manifest alone clears it"
    )


def test_with_no_manifest_to_consult_every_unsized_type_stays_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(_FIXTURE_DATASET, tmp_path / "multiblocks")  # structures, but no manifest
    monkeypatch.setattr(dataset_roots, "DEFAULT_DATA", tmp_path)
    assert ": Forge Hammer. Fix:" in _warning_for(load_plan(_SAND), capsys)


@pytest.mark.parametrize(
    ("source_class", "single"),
    [
        ("gregtech.api.metatileentity.implementations.MTEBasicMachineWithRecipe", True),
        ("gregtech.api.metatileentity.implementations.MTEBasicMachine", True),
        ("gregtech.common.tileentities.machines.steam.MTESteamForgeHammerBronze", True),
        ("gregtech.common.tileentities.machines.multi.MTEDistillationTower", False),
        ("gregtech.common.tileentities.machines.multi.steam.MTESteamMegaThing", False),
        ("", False),
    ],
)
def test_the_single_block_class_heuristic(source_class: str, single: bool) -> None:
    manifest = TextureManifest(
        {
            "blocks": {
                "gregtech:gt.blockmachines|1": {
                    "kind": "mte",
                    "display_name": "Thing",
                    "source_class": source_class,
                    "sides": {},
                }
            }
        }
    )
    assert _manifest_says_single_block(manifest, "Thing", "LV") is single
    assert _manifest_says_single_block(manifest, "Other Thing", "LV") is False, "no entry: unknown"
    assert _manifest_says_single_block(None, "Thing", "LV") is False, "no manifest: unknown"


def test_the_undumped_pack_warning_is_quiet_when_the_plans_dump_loaded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A derived or pinned version means a dump of the plan's pack (or the user's pick) is in use.
    plan, physical, problem = _undumped(load_plan(_NITROBENZENE))
    _warn_if_plan_pack_undumped(plan, "2.8.4", physical, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_leaves_a_census_to_the_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Another pack's census draws the adapter's mismatch warning; saying it twice teaches readers
    # to skip both.
    plan, _, problem = _undumped(load_plan(_NITROBENZENE))
    census = _empty_dataset()
    assert census.meta.census, "DatasetMeta defaults census to True"
    _warn_if_plan_pack_undumped(plan, None, census, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_leaves_a_failed_load_to_its_own_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # _load_physical_or_warn already said "using 1x1x1 footprints" when it returned None.
    plan, _, problem = _undumped(load_plan(_NITROBENZENE))
    _warn_if_plan_pack_undumped(plan, None, None, problem)
    assert capsys.readouterr().err == ""


def test_the_undumped_pack_warning_is_quiet_for_a_plan_stating_no_pack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = load_plan(_NITROBENZENE)
    for recipe in plan.recipes:
        recipe.source = None
    assert _warning_for(plan, capsys) == ""


def test_the_undumped_pack_warning_is_quiet_when_the_sample_covers_the_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every machine type found its structure, so nothing fell to 1x1x1 and there is nothing to say.
    plan = _one_node_plan("Electric Blast Furnace", kind="multiblock", tier="MV")
    _, _, problem = _undumped(plan)
    (furnace,) = [m for m in problem.machines if m.type == "Electric Blast Furnace"]
    assert furnace.footprint.volume > 1, "the sample holds the EBF, so it keeps its real footprint"
    assert _warning_for(plan, capsys) == ""


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
    captured = capsys.readouterr()
    layout = _published(captured.out)  # the layout is published either way
    assert not _line_resolves_multiblocks(), "the suite is pinned to the committed fixtures"
    assert code == 1
    assert layout.status is not LayoutStatus.VALID
    assert layout.infeasibility is not None
    assert f"[{layout.status.value}] {layout.infeasibility.constraint}:" in captured.err
    # And the run says why, which it did not before #207: the plan's pack has no dump here.
    assert f"{_UNDUMPED} 2.8.4" in captured.err


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
    captured = capsys.readouterr()
    assert code == 1
    assert "partial_invalid" in captured.err
    # An infeasible run still publishes its layout: the JSON carries the same verdict the stderr
    # line states, so a script reads it from stdout and a person reads it on stderr.
    layout = _published(captured.out)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "voltage_tier"
    assert f"[partial_invalid] voltage_tier: {layout.infeasibility.detail}" in captured.err


#: A one-node ULV plan: it parses and maps, but 8 V does not survive the run the adapter sizes
#: energy hatches for, so the adapter itself declares it infeasible (see the test below).
_ULV_PLAN: Final = {
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
    export.write_text(json.dumps(_ULV_PLAN), encoding="utf-8")
    monkeypatch.setattr(cli_module, "_load_physical_or_warn", lambda *_, **__: hatched_dataset())
    code = main([str(export), "--seed", "7"])
    captured = capsys.readouterr()
    err = captured.err
    assert code == 1
    assert "[infeasible] voltage_drop:" in err
    assert "ULV" in err
    assert "try: " in err  # the relaxation, the same line a solver infeasibility prints
    assert not solve_calls  # the verdict is the adapter's; nothing was solved to reach it
    # stdout reads the same whichever stage said no: the layout the solver returns when nothing
    # fits (the verdict and the seed, nothing placed), carrying the adapter's own infeasibility.
    layout = _published(captured.out)
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "voltage_drop"
    assert f"[infeasible] voltage_drop: {layout.infeasibility.detail}" in err
    assert (layout.seed, layout.placements, layout.routes) == (7, [], [])


def test_cli_an_adapter_infeasibility_with_an_artifact_flag_keeps_stdout_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # The artifact rule holds on the adapter's early exit too: asked for a preview, the run
    # publishes no JSON, and with no layout to draw one from it reports the verdict and exits 1.
    export = tmp_path / "ulv.json"
    export.write_text(json.dumps(_ULV_PLAN), encoding="utf-8")
    monkeypatch.setattr(cli_module, "_load_physical_or_warn", lambda *_, **__: hatched_dataset())
    target = tmp_path / "view.html"
    assert main([str(export), "--preview", str(target)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[infeasible] voltage_drop:" in captured.err
    assert not target.exists()


def test_cli_missing_export_arg_returns_2(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    code = main([])
    assert code == 2
    captured = capsys.readouterr()
    assert "required" in captured.err
    assert captured.out == ""  # no layout, so nothing to publish: stdout is JSON or nothing
    assert not solve_calls  # the exit-2 paths bail early; they must not solve first


def test_cli_file_not_found_returns_2(
    capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    code = main(["does-not-exist.json"])
    assert code == 2
    captured = capsys.readouterr()
    assert "could not load" in captured.err
    assert captured.out == ""
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
    captured = capsys.readouterr()
    err = captured.err
    assert code == cli_module.INTERNAL_ERROR_EXIT == 3
    assert "internal error: RuntimeError: boom" in err
    assert "Traceback" in err  # the useful half of an internal error
    assert captured.out == ""  # a crash publishes no layout, and no half of one


def _exploding_json(layout: LayoutResult) -> str:
    raise RuntimeError("serialize")


def test_cli_a_layout_that_will_not_serialize_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # Publishing the layout is part of the guarded stretch: every layout the solver returns is a
    # valid contract, so one that cannot be dumped is a bug here, not a verdict about the plan.
    monkeypatch.setattr(cli_module, "_layout_json", _exploding_json)
    code = main([_SAND])
    captured = capsys.readouterr()
    assert code == cli_module.INTERNAL_ERROR_EXIT
    assert "internal error: RuntimeError: serialize" in captured.err
    assert captured.out == ""


def test_cli_an_adapter_verdict_that_will_not_serialize_is_an_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # The same guard on the adapter's early exit, which publishes a layout of its own.
    export = tmp_path / "ulv.json"
    export.write_text(json.dumps(_ULV_PLAN), encoding="utf-8")
    monkeypatch.setattr(cli_module, "_load_physical_or_warn", lambda *_, **__: hatched_dataset())
    monkeypatch.setattr(cli_module, "_layout_json", _exploding_json)
    code = main([str(export)])
    captured = capsys.readouterr()
    assert code == cli_module.INTERNAL_ERROR_EXIT
    assert "internal error: RuntimeError: serialize" in captured.err
    assert captured.out == ""


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


def test_cli_a_schematic_that_is_not_a_write_failure_is_an_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    solve_calls: list[dict[str, object]],
) -> None:
    # The same guard for the export (#212): a 2.9 frame box once crashed the lowering with a bare
    # ValueError, which reached the shell as a traceback on exit 1, the code for an infeasibility.
    def exploding_export(*args: object, **kwargs: object) -> None:
        raise ValueError("byte must be in range(0, 256)")

    monkeypatch.setattr(cli_module, "write_schematic", exploding_export)
    code = main([_SAND, "--schematic", str(tmp_path / "line.schematic")])
    err = capsys.readouterr().err
    assert code == cli_module.INTERNAL_ERROR_EXIT
    assert "internal error: ValueError: byte must be in range(0, 256)" in err


def test_cli_unwritable_preview_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # an unwritable output path (OSError) is reported and exits 2 per the documented 0/1/2
    # contract, not dumped as a raw traceback (GitHub #39)
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
    ("flag", "name"), [("--preview", "view.html"), ("--schematic", "line.schematic")]
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
    # missing level would not prove the parents=True half. Both flags, because "put it here" has
    # to mean the same thing for each of them.
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
        me_toggles: METoggles | None = None,
    ) -> InputIR:
        captured["physical"] = physical
        return to_input_ir(plan, physical=physical, producer=producer, me_toggles=me_toggles)

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
    _published(out)  # the layout JSON on stdout stays pipeable


def test_cli_warnings_leave_stdout_pure_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    # Three kinds of advisory at once, against one parse of stdout: the CLI's own warning (a
    # dataset version with no dump), its coverage note (unmeasured power intake, which a run with
    # no dataset always makes), and an adapter warning (a resolved EU/t that disagrees with the
    # recipe). `gtnh-solve plan.json | python -m json.tool` has to survive all of them, so each
    # lands on stderr, or in Python's warning channel (stderr outside pytest), and none on stdout.
    plan = _sand_plan()
    plan["resolved"]["machines"][0]["totalEut"] *= 3  # the adapter trusts it, and says so
    export = tmp_path / "noisy.json"
    export.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.warns(AdapterWarning) as warned:
        code = main([str(export), "--dataset-version", "does-not-exist"])
    assert code == 0
    assert any("resolved EU/t" in str(w.message) for w in warned)
    out, err = capsys.readouterr()
    assert "warning: physical multiblock dataset unavailable" in err
    assert "note: power intake unmeasured" in err
    _published(out)


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


# ------------------------------------------------ an export of a layout that is not a build (#214)

_INCOMPLETE = "INCOMPLETE"


def _partial(layout: LayoutResult, *, drop_first_auto: bool = True) -> LayoutResult:
    """``layout`` recast as a partial result, optionally missing its first auto-connection.

    The sand line connects all its item nets by auto-output, so dropping one leaves exactly that net
    unconnected: a real partial layout's shape, without paying for a solve that fails.
    """
    autos = layout.auto_connections[1:] if drop_first_auto else layout.auto_connections
    return layout.model_copy(
        update={
            "status": LayoutStatus.PARTIAL_INVALID,
            "auto_connections": autos,
            "infeasibility": Infeasibility(
                constraint="routing", detail="a net could not be routed"
            ),
        }
    )


@pytest.fixture
def partial_solve(monkeypatch: pytest.MonkeyPatch, sand_layout: LayoutResult) -> str:
    """Make the CLI's solve return a partial sand layout; the id of the net it leaves unconnected."""
    monkeypatch.setattr(cli_module, "solve", lambda problem, **_: _partial(sand_layout))
    return sand_layout.auto_connections[0].net_id


def test_a_partial_layout_exported_as_a_schematic_warns_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], partial_solve: str
) -> None:
    target = tmp_path / "line.schematic"
    code = main([_SAND, "--schematic", str(target)])
    err = capsys.readouterr().err
    assert code == 1, "the verdict keeps its own exit code"
    assert target.is_file(), "the file is still written: a partial layout is worth looking at"
    warning = next(line for line in err.splitlines() if _INCOMPLETE in line)
    assert warning.startswith("warning: the layout is partial_invalid (routing)")
    assert "--schematic" in warning
    assert "--preview" not in warning
    assert f"1 of 5 net(s) are unconnected ({partial_solve})" in warning
    assert "Do not build it as-is" in warning
    assert err.index(_INCOMPLETE) < err.index("wrote schematic"), "said before the file, not after"


def test_a_partial_preview_warns_and_names_both_artifacts_when_both_are_asked_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], partial_solve: str
) -> None:
    main([_SAND, "--preview", str(tmp_path / "v.html"), "--schematic", str(tmp_path / "s.sch")])
    err = capsys.readouterr().err
    assert err.count(_INCOMPLETE) == 1, "one warning for the run, not one per file"
    assert "the --preview and --schematic written below is INCOMPLETE" in err


def test_a_valid_layout_exports_without_the_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], solve_calls: list[dict[str, object]]
) -> None:
    assert main([_SAND, "--schematic", str(tmp_path / "line.schematic")]) == 0
    assert _INCOMPLETE not in capsys.readouterr().err


def test_a_partial_layout_with_no_artifact_asked_for_does_not_warn(
    capsys: pytest.CaptureFixture[str], partial_solve: str
) -> None:
    # Nothing is written, so there is no file to mislead anyone; the verdict still prints.
    assert main([_SAND]) == 1
    err = capsys.readouterr().err
    assert _INCOMPLETE not in err
    assert "[partial_invalid] routing" in err


def test_a_partial_layout_with_every_net_connected_says_it_fails_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sand_layout: LayoutResult,
) -> None:
    partial = _partial(sand_layout, drop_first_auto=False)
    monkeypatch.setattr(cli_module, "solve", lambda problem, **_: partial)
    main([_SAND, "--schematic", str(tmp_path / "line.schematic")])
    assert "every net is connected, but the layout fails validation" in capsys.readouterr().err


def test_many_unconnected_nets_are_summarised(
    sand_layout: LayoutResult, capsys: pytest.CaptureFixture[str]
) -> None:
    problem = adapt_file(_SAND)
    stripped = sand_layout.model_copy(update={"routes": [], "auto_connections": []})
    assert _unconnected_nets(problem, stripped) == [net.id for net in problem.nets]
    many = problem.model_copy(update={"nets": problem.nets * 3})  # 15 ids, beyond the 3 named
    _warn_incomplete_export(many, _partial(stripped), ["--schematic"])
    err = capsys.readouterr().err
    assert "15 of 15 net(s) are unconnected" in err
    assert " and 12 more)" in err


def test_an_me_toggled_net_is_not_counted_as_unconnected(sand_layout: LayoutResult) -> None:
    problem = adapt_file(_SAND)
    stripped = sand_layout.model_copy(update={"routes": [], "auto_connections": []})
    toggled = problem.model_copy(update={"me_toggles": METoggles(items=True)})
    remaining = _unconnected_nets(toggled, stripped)
    assert remaining, "the power net is still physical"
    assert all(net.commodity is not Commodity.ITEM for net in toggled.nets if net.id in remaining)
