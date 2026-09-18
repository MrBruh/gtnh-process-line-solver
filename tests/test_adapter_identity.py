"""Tests for joining a plan's machine to its record in the physical structure dump.

The exporter names a machine by its localized **recipe map** ("Blast Furnace"); the dump is keyed by
the **controller's** own display name ("Electric Blast Furnace"). For a GT++ machine the two differ,
so joining on the recipe-map name alone resolved 5 of 9 nodes on one real plan and 34 of 53 on
another, silently dropping the rest to a 1x1x1 footprint - a real multiblock modelled as one block,
which is the "coarse-cell abstraction can lie" failure arriving quietly.

The load-bearing subtlety is that a **census** miss means opposite things depending on
``handler.kind``, and conflating them is how a name-alias table swallows a genuine extraction gap.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    Resource,
    load_plan,
    to_input_ir,
)
from gtnh_solver.adapter.core import (
    _block_key_for,
    _candidate_names,
    _classify_census_miss,
    _physical_record,
)
from gtnh_solver.dataset import (
    DatasetMeta,
    MachinePhysical,
    PhysicalDataset,
    load_physical_dataset,
)
from gtnh_solver.ir import CellBox, Facing

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_FIXTURES = [
    _EXAMPLES / "gtnh-sand.json",
    _EXAMPLES / "gtnh-nitrobenzene.json",
    _EXAMPLES / "gtnh-parallel-sand.json",
]


def _record(key: str, block: str = "test:block", meta: int = 0) -> MachinePhysical:
    return MachinePhysical(
        key=key,
        registry_name=block,
        meta=meta,
        source_class="test.Controller",
        footprint=CellBox(sx=3, sy=3, sz=3),
        io_faces=frozenset({Facing.NORTH}),
        hint_layers=frozenset({0}),
        coil_layer_count=0,
        variant_count=1,
        hatch_cells=6,
        energy_hatch_cells=6,
        upkeep_hatch_count=1,
    )


def _dump(
    *records: MachinePhysical, census: bool = True, pack_version: str = "test"
) -> PhysicalDataset:
    return PhysicalDataset(
        meta=DatasetMeta.model_validate(
            {
                "schema": 2,
                "pack_version": pack_version,
                "generated_at": "2026-01-01T00:00:00Z",
                "extractor_sha": "0" * 40,
                "controller_count": len(records),
                "census": census,
            }
        ),
        machines={record.key: record for record in records},
    )


def _recipe(machine_type: str, *handlers: MachineHandler) -> Recipe:
    return Recipe(
        id="r",
        machine_type=machine_type,
        duration_ticks=10.0,
        eut=30.0,
        outputs=[Resource(kind="item", id="x", amount=1.0)],
        machine_handlers=list(handlers),
    )


def _node(handler_id: str = "") -> Node:
    return Node(id="n", recipe_id="r", overclock_tier="LV", machine_handler_id=handler_id)


# ------------------------------------------------------------------ candidate names


def test_the_controller_name_is_tried_before_the_recipe_map_name() -> None:
    # The handler's label is the controller, which is what the dump is keyed by.
    recipe = _recipe("Blast Furnace", MachineHandler(id="h", kind="multiblock", label="Volcanus"))
    assert _candidate_names(recipe, _node()) == ["Volcanus", "Blast Furnace"]


def test_an_alias_follows_the_name_it_translates() -> None:
    recipe = _recipe("Chemical Plant")
    assert _candidate_names(recipe, _node()) == ["Chemical Plant", "ExxonMobil Chemical Plant"]


def test_a_plan_without_handlers_falls_back_to_the_recipe_map_name() -> None:
    assert _candidate_names(_recipe("Large Chemical Reactor"), _node()) == [
        "Large Chemical Reactor"
    ]


def test_candidates_are_deduplicated() -> None:
    # A handler whose label already equals the recipe-map name must not be tried twice.
    recipe = _recipe(
        "Distillation Tower",
        MachineHandler(id="h", kind="multiblock", label="Distillation Tower"),
    )
    assert _candidate_names(recipe, _node()) == ["Distillation Tower"]


# ------------------------------------------------------------------ record resolution


def test_the_controller_block_id_beats_every_name() -> None:
    # An exact registry@meta identity cannot be wrong; a name match can.
    # The dump holds a machine literally named "M", which is also this recipe's map name, so a name
    # match would find it. The block id must win anyway and pick the other record.
    dump = _dump(_record("Right One", block="gregtech:gt.blockmachines", meta=998), _record("M"))
    record = _physical_record(_recipe("M"), _node(), dump, "gregtech:gt.blockmachines@998")
    assert record is not None
    assert record.key == "Right One"


def test_the_handler_label_resolves_what_the_recipe_map_name_cannot() -> None:
    dump = _dump(_record("Industrial Maceration Stack"))
    recipe = _recipe(
        "Macerator",
        MachineHandler(id="h", kind="multiblock", label="Industrial Maceration Stack"),
    )
    record = _physical_record(recipe, _node(), dump, None)
    assert record is not None
    assert record.key == "Industrial Maceration Stack"


def test_an_alias_resolves_a_machine_with_no_handler_to_bridge_it() -> None:
    # "Chemical Plant" carries an empty machineHandlers list in real plans, so the alias is the only
    # route to the controller the dump knows.
    dump = _dump(_record("ExxonMobil Chemical Plant"))
    record = _physical_record(_recipe("Chemical Plant"), _node(), dump, None)
    assert record is not None
    assert record.key == "ExxonMobil Chemical Plant"


def test_an_unknown_machine_resolves_to_nothing() -> None:
    assert _physical_record(_recipe("Nothing Like It"), _node(), _dump(_record("M")), None) is None


def test_no_dataset_resolves_to_nothing() -> None:
    assert _physical_record(_recipe("M"), _node(), None, None) is None


# ------------------------------------------------------------------ what a census miss means


def test_a_single_block_handler_makes_a_miss_positive_evidence() -> None:
    # kind "single" plus a census miss proves the machine is basic, so GT's maxAmperesIn applies.
    recipe = _recipe("Forge Hammer", MachineHandler(id="h", kind="single", label="Forge Hammer"))
    claimed: set[str] = set()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _classify_census_miss(recipe, _node(), claimed)
    assert claimed == {"n"}


def test_no_handler_keeps_the_previous_reading_of_a_miss() -> None:
    # Every MrBruh-fork plan is this shape, and a census miss there has always meant single block.
    claimed: set[str] = set()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _classify_census_miss(_recipe("Forge Hammer"), _node(), claimed)
    assert claimed == {"n"}


def test_a_multiblock_handler_makes_a_miss_an_extraction_gap() -> None:
    # The opposite reading: the export says this IS a multiblock, so the dump is incomplete. Claiming
    # it single-block would state the wrong intake formula AND reserve 1x1x1 for a real structure.
    recipe = _recipe(
        "Fluid Solidifier", MachineHandler(id="h", kind="multiblock", label="Mass Solidifier")
    )
    claimed: set[str] = set()
    with pytest.warns(AdapterWarning, match="census but does not contain") as caught:
        _classify_census_miss(recipe, _node(), claimed)
    assert claimed == set()  # deliberately unclaimed
    assert "Mass Solidifier" in str(caught[0].message)


# ------------------------------------------------------------------ through the mapping


def test_a_single_block_plan_measures_its_intake_against_a_census() -> None:
    # The committed arodoid fixture is this shape: LV Forge Hammers, correctly absent from a
    # multiblock census, so their intake ceiling is known and nothing is reported.
    plan = load_plan(_EXAMPLES / "gtnh-parallel-sand.json")
    for node in plan.nodes:
        node.machine_count = 1
    # The dump must claim the plan's own pack, or the version cross-check speaks up instead.
    dump = _dump(_record("Some Other Machine"), pack_version="2.9.0-beta-2")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        to_input_ir(plan, physical=dump)


def test_a_missing_multiblock_is_reported_through_the_mapping() -> None:
    plan = Plan(
        schema_version=1,
        recipes=[
            _recipe(
                "Fluid Solidifier",
                MachineHandler(id="h", kind="multiblock", label="Mass Solidifier"),
            )
        ],
        nodes=[_node()],
    )
    with pytest.warns(AdapterWarning, match="census but does not contain"):
        ir = to_input_ir(plan, physical=_dump(_record("Some Other Machine")))
    # Still mapped, with the crude default: reported, not fatal.
    assert ir.machines[0].footprint.volume == 1


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.name)
def test_the_committed_fixtures_resolve_exactly_as_before(path: Path) -> None:
    """The ladder must be additive: these three join by controller-block id or not at all.

    The MrBruh fixtures carry ``machineBlock``, so the exact identity already resolved every one of
    their machines and the name ladder adds nothing. If that ever changes, their footprints change
    and every golden layout built on them moves.
    """
    dump = load_physical_dataset()
    plan = load_plan(path)
    recipes = {r.id: r for r in plan.recipes}
    resolved = {m.node_id: m for m in plan.resolved.machines} if plan.resolved else {}
    for node in plan.nodes:
        recipe = recipes[node.recipe_id]
        block_key = _block_key_for(recipe, resolved.get(node.id))
        before = dump.get(recipe.machine_type, block_key=block_key)
        after = _physical_record(recipe, node, dump, block_key)
        assert (before is None) == (after is None)
        if before is not None and after is not None:
            assert before.key == after.key
