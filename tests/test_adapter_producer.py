"""Tests for producer detection: which gtnh-factory-flow fork exported a plan.

The point of this lane is that an arodoid plan used to load with **zero** warnings and then
size its power from pre-overclock EU/t figures. So these tests care about two things: that the two
forks are told apart by structure rather than by ``schemaVersion`` (which both spell as a small
integer, incompatibly), and that the resulting warning fires on exactly the plans it should and
stays quiet on the rest. A warning that cries wolf is worse than none, because the next reader
filters ``AdapterWarning`` out.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver.adapter import (
    AdapterWarning,
    MachineHandler,
    Node,
    Plan,
    PlanProducer,
    Recipe,
    RecipeSource,
    ResolvedBlock,
    Resource,
    RuntimeCalculation,
    RuntimeVariant,
    describe_markers,
    detect_producer,
    load_plan,
    plan_pack_version,
    resolve_producer,
    strip_dataset_channel,
    to_input_ir,
)
from gtnh_solver.adapter.core import (
    _check_dataset_version,
    _check_power_provenance,
    _effective_handler,
)
from gtnh_solver.cli import _dataset_version_for, main
from gtnh_solver.dataset import DatasetMeta, PhysicalDataset, load_physical_dataset
from gtnh_solver.ir import InputIR, LayoutResult, METoggles

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"
#: The committed arodoid-fork exports: no ``resolved`` block, ``machineHandlers`` present. The sand
#: line is a 9-hammer toy; ``ev-nitrobenzene`` is a real 2.9 line (multiblocks, #204).
_PARALLEL_SAND = _EXAMPLES / "gtnh-parallel-sand.json"
_EV_NITROBENZENE = _EXAMPLES / "ev-nitrobenzene.json"


def _plan(
    *handlers: MachineHandler,
    resolved: ResolvedBlock | None = None,
    schema_version: int = 1,
    handler_id: str = "",
    eut: float = 30.0,
    dataset_version: str = "",
) -> Plan:
    """A one-node plan whose recipe lists ``handlers``, for the detection/provenance branches."""
    return Plan(
        schema_version=schema_version,
        resolved=resolved,
        recipes=[
            Recipe(
                id="r",
                machine_type="Recipe Map Name",
                eut=eut,
                duration_ticks=10.0,
                outputs=[Resource(kind="item", id="x", amount=1.0)],
                machine_handlers=list(handlers),
                source=RecipeSource(dataset_version_id=dataset_version),
            )
        ],
        nodes=[
            Node(id="n", recipe_id="r", overclock_tier="LV", machine_handler_id=handler_id),
        ],
    )


def _dataset(pack_version: str, *, census: bool = True) -> PhysicalDataset:
    """An empty dataset that only claims a pack version, which is all the version check reads."""
    return PhysicalDataset(
        meta=DatasetMeta(
            schema=2,
            pack_version=pack_version,
            generated_at="2026-09-18T00:00:00Z",
            extractor_sha="0" * 40,
            controller_count=0,
            census=census,
        ),
        machines={},
    )


def _multiblock(handler_id: str = "mb", label: str = "Controller Name") -> MachineHandler:
    return MachineHandler(id=handler_id, kind="multiblock", label=label)


def _single(handler_id: str = "sb", label: str = "Basic Machine") -> MachineHandler:
    return MachineHandler(id=handler_id, kind="single", label=label)


# ------------------------------------------------------------------ detection, real fixtures


@pytest.mark.parametrize("path", [_SAND, _NITROBENZENE])
def test_committed_mrbruh_fixtures_detect_as_mrbruh(path: Path) -> None:
    assert detect_producer(load_plan(path)) is PlanProducer.MRBRUH_V2


@pytest.mark.parametrize("path", [_PARALLEL_SAND, _EV_NITROBENZENE], ids=lambda p: p.name)
def test_committed_arodoid_fixture_detects_despite_schema_version_1(path: Path) -> None:
    # The whole hazard in one assertion: this plan says schemaVersion 1, exactly as an old MrBruh
    # plan would, and is told apart only by carrying machineHandlers.
    plan = load_plan(path)
    assert plan.schema_version == 1
    assert plan.resolved is None
    assert detect_producer(plan) is PlanProducer.ARODOID_V1


# ------------------------------------------------------------------ detection, branches


def test_resolved_block_alone_identifies_mrbruh() -> None:
    assert detect_producer(_plan(resolved=ResolvedBlock())) is PlanProducer.MRBRUH_V2


def test_schema_version_2_alone_identifies_mrbruh() -> None:
    assert detect_producer(_plan(schema_version=2)) is PlanProducer.MRBRUH_V2


def test_machine_handlers_alone_identify_arodoid() -> None:
    assert detect_producer(_plan(_single())) is PlanProducer.ARODOID_V1


def test_no_markers_is_undetermined() -> None:
    # A hand-built or minimal plan carries neither marker. Undetermined, not an error - which is
    # why detection must stay silent here (most of the suite builds plans like this).
    assert detect_producer(_plan()) is None


def test_both_markers_is_undetermined() -> None:
    # Would mean a fork grew the other's field and this heuristic needs revisiting, so it must not
    # silently pick a side.
    assert detect_producer(_plan(_single(), resolved=ResolvedBlock())) is None


def test_detection_emits_no_warning_even_when_undetermined() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert detect_producer(_plan()) is None
        assert resolve_producer(_plan()) is None


# ------------------------------------------------------------------ resolve precedence


def test_explicit_pin_wins_over_the_markers() -> None:
    # The override exists to correct a wrong or impossible detection, so it is taken on trust and
    # deliberately not cross-checked against markers that say otherwise.
    plan = _plan(resolved=ResolvedBlock())
    assert resolve_producer(plan, PlanProducer.ARODOID_V1) is PlanProducer.ARODOID_V1


def test_no_pin_falls_back_to_detection() -> None:
    assert resolve_producer(_plan(_single())) is PlanProducer.ARODOID_V1


def test_describe_markers_names_every_signal() -> None:
    described = describe_markers(_plan(_single()))
    assert "schemaVersion=1" in described
    assert "resolved=absent" in described
    assert "app=absent" in described
    assert "machineHandlers=present" in described


# ------------------------------------------------------------------ effective handler


def test_explicit_handler_id_selects_that_handler() -> None:
    plan = _plan(_multiblock("first"), _multiblock("second"), handler_id="second")
    handler = _effective_handler(plan.recipes[0], plan.nodes[0])
    assert handler is not None
    assert handler.id == "second"


def test_absent_handler_id_selects_the_first_as_default() -> None:
    # Verified against every node of the committed arodoid fixture: no id means the default,
    # and the default is the head of the list.
    plan = _plan(_multiblock("first"), _multiblock("second"))
    handler = _effective_handler(plan.recipes[0], plan.nodes[0])
    assert handler is not None
    assert handler.id == "first"


def test_unknown_handler_id_falls_back_to_the_default() -> None:
    plan = _plan(_multiblock("first"), handler_id="not-in-the-list")
    handler = _effective_handler(plan.recipes[0], plan.nodes[0])
    assert handler is not None
    assert handler.id == "first"


def test_no_handlers_is_no_information() -> None:
    # Must not read as "single block": several arodoid machine types carry an empty list.
    plan = _plan()
    assert _effective_handler(plan.recipes[0], plan.nodes[0]) is None


# ------------------------------------------------------------------ power provenance warning


def test_warns_when_a_multiblock_has_no_resolved_figures() -> None:
    plan = _plan(_multiblock(label="Industrial Centrifuge"))
    with pytest.warns(AdapterWarning, match="understates the real draw"):
        _check_power_provenance(plan, PlanProducer.ARODOID_V1)


def test_the_warning_names_the_controller_and_the_producer() -> None:
    plan = _plan(_multiblock(label="Industrial Centrifuge"))
    with pytest.warns(AdapterWarning) as caught:
        _check_power_provenance(plan, PlanProducer.ARODOID_V1)
    message = str(caught[0].message)
    # The controller's own name, not the recipe-map name, because that is what a builder places.
    assert "Industrial Centrifuge" in message
    assert "arodoid-v1" in message


def test_quiet_for_a_single_block_machine() -> None:
    # The committed arodoid fixture is exactly this shape (LV Forge Hammers). Base recipe
    # EU/t is *exact* for a single block at its own tier, so warning here would be crying wolf.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_power_provenance(_plan(_single()), PlanProducer.ARODOID_V1)


def test_quiet_when_a_runtime_variant_covers_the_node() -> None:
    # A matched variant is post-overclock, straight from GT's own calculator, so the draw is not a
    # fallback and warning would cry wolf on every arodoid plan forever.
    plan = _plan(_multiblock())
    plan.recipes[0].runtime_calculation = RuntimeCalculation(
        variants=[RuntimeVariant(id="tier-lv", overclock_tier="LV", eut=30.0, duration_ticks=10.0)]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_power_provenance(plan, PlanProducer.ARODOID_V1)


def test_quiet_when_the_plan_carries_resolved_figures() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_power_provenance(
            _plan(_multiblock(), resolved=ResolvedBlock()), PlanProducer.MRBRUH_V2
        )


def test_quiet_when_no_handler_declares_the_machine_kind() -> None:
    # Without handlers there is no evidence the machine is a multiblock, so there is nothing to
    # claim; the undetermined-producer report on the CLI covers this case instead.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_power_provenance(_plan(), None)


def test_the_committed_arodoid_fixture_stays_quiet_through_the_mapping() -> None:
    # End to end on the real fixture: detection fires, the provenance warning correctly does not.
    plan = load_plan(_PARALLEL_SAND)
    for node in plan.nodes:  # machineCount > 1 is still rejected; not what this test is about
        node.machine_count = 1
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        to_input_ir(plan)


# ------------------------------------------------------------------ CLI wiring


@pytest.fixture
def stub_solve(monkeypatch: pytest.MonkeyPatch, solved_sand: tuple[InputIR, LayoutResult]) -> None:
    """Swap the CLI's solver for the session-cached sand layout.

    These tests are about flag plumbing, not solving, and the cached layout is a real one. Only
    usable where the export under test *is* sand, since the guide is rendered against the pair.
    """
    _, layout = solved_sand

    def cached_solve(problem: InputIR, **kwargs: object) -> LayoutResult:
        return layout

    monkeypatch.setattr(cli_module, "solve", cached_solve)


def test_cli_threads_the_pinned_producer_into_the_mapping(
    monkeypatch: pytest.MonkeyPatch,
    stub_solve: None,
) -> None:
    captured: dict[str, PlanProducer | None] = {}

    def spy(
        plan: Plan,
        *,
        physical: PhysicalDataset | None = None,
        producer: PlanProducer | None = None,
        me_toggles: METoggles | None = None,
    ) -> InputIR:
        captured["producer"] = producer
        return to_input_ir(plan, physical=physical, producer=producer, me_toggles=me_toggles)

    monkeypatch.setattr(cli_module, "to_input_ir", spy)
    assert main([str(_SAND), "--plan-schema", "arodoid-v1"]) == 0
    assert captured["producer"] is PlanProducer.ARODOID_V1


def test_cli_auto_detects_when_the_flag_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
    stub_solve: None,
) -> None:
    captured: dict[str, PlanProducer | None] = {}

    def spy(
        plan: Plan,
        *,
        physical: PhysicalDataset | None = None,
        producer: PlanProducer | None = None,
        me_toggles: METoggles | None = None,
    ) -> InputIR:
        captured["producer"] = producer
        return to_input_ir(plan, physical=physical, producer=producer, me_toggles=me_toggles)

    monkeypatch.setattr(cli_module, "to_input_ir", spy)
    assert main([str(_SAND)]) == 0
    assert captured["producer"] is PlanProducer.MRBRUH_V2


def test_cli_reports_an_undetermined_producer_on_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The advice is "pass --plan-schema", which only the CLI can give, so this is the one place the
    # undetermined case is reported at all.
    plan = _plan(eut=0.0)
    export = tmp_path / "markerless.json"
    export.write_text(plan.model_dump_json(by_alias=True), encoding="utf-8")
    assert main([str(export)]) == 0
    err = capsys.readouterr().err
    assert "could not tell which gtnh-factory-flow fork" in err
    assert "--plan-schema" in err


def test_cli_rejects_an_unknown_plan_schema() -> None:
    with pytest.raises(SystemExit) as exc:
        main([str(_SAND), "--plan-schema", "samiracle64-v0"])
    assert exc.value.code == 2


# ------------------------------------------------------------------ pack version, extraction


@pytest.mark.parametrize(
    ("dataset_version_id", "expected"),
    [
        ("stable-2.8.4", "2.8.4"),
        ("local-2.9.0-beta-2", "2.9.0-beta-2"),
        ("nightly-3.0.0", "3.0.0"),
        # Already bare: "beta-2" does not start with a digit, so there is nothing to strip.
        ("2.9.0-beta-2", "2.9.0-beta-2"),
        ("2.8.4", "2.8.4"),
        ("", ""),
    ],
)
def test_strip_dataset_channel(dataset_version_id: str, expected: str) -> None:
    assert strip_dataset_channel(dataset_version_id) == expected


def test_plan_pack_version_reads_the_recipe_source() -> None:
    assert plan_pack_version(_plan(dataset_version="local-2.9.0-beta-2")) == "2.9.0-beta-2"


def test_plan_pack_version_is_none_when_unstated() -> None:
    assert plan_pack_version(_plan()) is None


def test_plan_pack_version_is_none_when_recipes_disagree() -> None:
    # A plan spanning two datasets has no single answer, and picking a winner would silently size
    # the layout against one of them.
    plan = _plan(dataset_version="stable-2.8.4")
    plan.recipes.append(
        Recipe(
            id="r2",
            machine_type="Other",
            source=RecipeSource(dataset_version_id="local-2.9.0-beta-2"),
        )
    )
    assert plan_pack_version(plan) is None


def test_plan_pack_version_ignores_recipes_that_state_nothing() -> None:
    # Real plans carry non-GregTech recipe kinds with no dataset id; they must not read as conflict.
    plan = _plan(dataset_version="stable-2.8.4")
    plan.recipes.append(Recipe(id="r2", machine_type="Crop Farm"))
    assert plan_pack_version(plan) == "2.8.4"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (_SAND, "2.8.4"),
        (_NITROBENZENE, "2.8.4"),
        (_PARALLEL_SAND, "2.9.0-beta-2"),
        (_EV_NITROBENZENE, "2.9.0-beta-2"),
    ],
)
def test_plan_pack_version_on_the_committed_fixtures(path: Path, expected: str) -> None:
    assert plan_pack_version(load_plan(path)) == expected


# ------------------------------------------------------------------ pack version, mismatch warning


def test_warns_when_the_plan_and_the_dataset_are_different_packs() -> None:
    plan = _plan(dataset_version="local-2.9.0-beta-2")
    with pytest.warns(AdapterWarning, match="balanced against GTNH 2.9.0-beta-2"):
        _check_dataset_version(plan, _dataset("2.8.4"))


def test_the_mismatch_warning_names_both_packs_and_the_remedy() -> None:
    plan = _plan(dataset_version="local-2.9.0-beta-2")
    with pytest.warns(AdapterWarning) as caught:
        _check_dataset_version(plan, _dataset("2.8.4"))
    message = str(caught[0].message)
    assert "2.9.0-beta-2" in message
    assert "2.8.4" in message
    assert "--dataset-version 2.9.0-beta-2" in message


def test_quiet_when_the_packs_agree() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_dataset_version(_plan(dataset_version="stable-2.8.4"), _dataset("2.8.4"))


def test_quiet_when_no_dataset_is_loaded() -> None:
    # Every machine is 1x1x1 anyway, so there is no join to get wrong.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_dataset_version(_plan(dataset_version="local-2.9.0-beta-2"), None)


def test_quiet_when_the_plan_states_no_pack() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_dataset_version(_plan(), _dataset("2.8.4"))


def test_quiet_against_a_non_census_sample_whatever_pack_it_claims() -> None:
    # The committed fixtures are a two-machine sample declaring pack_version 2.9.0-beta-1, and they
    # are what a fresh clone resolves to. Trusting that nominal version would greet every new
    # contributor with a mismatch against the shipped 2.8.4 examples.
    plan = _plan(dataset_version="stable-2.8.4")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_dataset_version(plan, _dataset("2.9.0-beta-1", census=False))


def test_the_committed_fixture_dump_does_not_warn_against_the_shipped_examples() -> None:
    # The fresh-clone path, asserted on the real files rather than a stand-in.
    fixtures = load_physical_dataset(Path(__file__).resolve().parents[1] / "data" / "multiblocks")
    assert not fixtures.meta.census
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_dataset_version(load_plan(_SAND), fixtures)


def test_the_committed_arodoid_fixture_warns_against_the_2_8_4_dump() -> None:
    # The real case this check exists for: a 2.9 plan resolving footprints against a 2.8.4 dump.
    plan = load_plan(_PARALLEL_SAND)
    with pytest.warns(AdapterWarning, match="2.9.0-beta-2"):
        _check_dataset_version(plan, _dataset("2.8.4"))


# ------------------------------------------------------------------ dataset version selection


def test_an_explicit_pin_is_never_second_guessed(tmp_path: Path) -> None:
    # Asking for a version with no dump should fail visibly, not be silently swapped.
    assert _dataset_version_for(_plan(dataset_version="stable-2.8.4"), "9.9.9") == "9.9.9"


def _stage_dump(root: Path, version: str, *, multiblocks: bool, manifest: bool) -> Path:
    """A ``data/<version>/`` folder holding whichever halves of a dump are asked for.

    Presence is all ``_dataset_version_for`` reads, so the halves are empty stand-ins.
    """
    folder = root / version
    folder.mkdir(parents=True)
    if multiblocks:
        (folder / "multiblocks").mkdir()
    if manifest:
        (folder / "textures").mkdir()
        (folder / "textures" / "manifest.json").write_text("{}", encoding="utf-8")
    return folder


def test_the_plan_pack_is_used_when_a_dump_provides_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _stage_dump(tmp_path, "2.9.0-beta-2", multiblocks=True, manifest=True)
    monkeypatch.setattr(cli_module, "list_versions", lambda: [folder])
    plan = _plan(dataset_version="local-2.9.0-beta-2")
    assert _dataset_version_for(plan, None) == "2.9.0-beta-2"
    assert capsys.readouterr().err == "", "a whole dump is followed without comment"


def test_an_unavailable_plan_pack_falls_back_rather_than_pinning_a_missing_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pinning it would lose every real footprint to the 1x1x1 default; falling back keeps
    # best-effort footprints, and the adapter's mismatch warning still says the packs differ.
    folder = _stage_dump(tmp_path, "2.8.4", multiblocks=True, manifest=True)
    monkeypatch.setattr(cli_module, "list_versions", lambda: [folder])
    plan = _plan(dataset_version="local-2.9.0-beta-2")
    assert _dataset_version_for(plan, None) is None


def test_a_version_folder_holding_neither_half_does_not_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An interrupted texture run can leave the folder, even its textures/ subfolder, with nothing
    # usable in it: there is no half to follow, so it is the no-dump case, not a partial one.
    folder = _stage_dump(tmp_path, "2.9.0-beta-2", multiblocks=False, manifest=False)
    (folder / "textures").mkdir()
    monkeypatch.setattr(cli_module, "list_versions", lambda: [folder])
    plan = _plan(dataset_version="local-2.9.0-beta-2")
    assert _dataset_version_for(plan, None) is None
    assert capsys.readouterr().err == ""


def test_a_structures_only_dump_is_followed_and_names_the_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#206: declining the folder would take the manifest from whichever pack is newest instead.

    Resolution is per sub-path, so a 2.8.4 dump sitting next to this one would supply the sprites
    and the ``.schematic`` ids for a 2.9 plan, and nothing would say so. Following the plan's pack
    turns that into a visible gap, and the warning is what makes it visible.
    """
    folder = _stage_dump(tmp_path, "2.9.0-beta-2", multiblocks=True, manifest=False)
    other = _stage_dump(tmp_path, "2.8.4", multiblocks=True, manifest=True)
    monkeypatch.setattr(cli_module, "list_versions", lambda: [other, folder])  # 2.8.4 is newer
    plan = _plan(dataset_version="local-2.9.0-beta-2")

    assert _dataset_version_for(plan, None) == "2.9.0-beta-2"
    err = capsys.readouterr().err
    assert err.startswith("warning: ")
    assert str(folder / "textures/manifest.json") in err, "the missing half is named by its path"
    assert "placeholder boxes" in err
    assert "--schematic cannot export" in err
    assert "runClient" in err, "and the run that makes it"
    assert "-PtextureOut=../../data/2.9.0-beta-2/textures" in err
    assert "multiblocks" not in err, "the half that is present is not complained about"


def test_a_textures_only_dump_is_followed_and_names_the_missing_structures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The mirror case: another pack's structures are joined to the plan by display name, and names
    # move between packs, so they are not borrowed either.
    folder = _stage_dump(tmp_path, "2.9.0-beta-2", multiblocks=False, manifest=True)
    other = _stage_dump(tmp_path, "2.8.4", multiblocks=True, manifest=True)
    monkeypatch.setattr(cli_module, "list_versions", lambda: [other, folder])
    plan = _plan(dataset_version="local-2.9.0-beta-2")

    assert _dataset_version_for(plan, None) == "2.9.0-beta-2"
    err = capsys.readouterr().err
    assert str(folder / "multiblocks") in err
    assert "1x1x1 footprint" in err
    assert "runServer -PdatasetOut=../../data/2.9.0-beta-2" in err
    assert "manifest.json" not in err


def test_an_explicit_pin_is_not_warned_about_whatever_it_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pin is the user's own choice, and each consumer already fails visibly on a missing half
    # (the multiblock load warns, the export refuses); a second warning here would only repeat them.
    folder = _stage_dump(tmp_path, "2.9.0-beta-2", multiblocks=True, manifest=False)
    monkeypatch.setattr(cli_module, "list_versions", lambda: [folder])
    plan = _plan(dataset_version="local-2.9.0-beta-2")
    assert _dataset_version_for(plan, "2.9.0-beta-2") == "2.9.0-beta-2"
    assert capsys.readouterr().err == ""


def test_no_stated_pack_means_the_default_resolution() -> None:
    assert _dataset_version_for(_plan(), None) is None
