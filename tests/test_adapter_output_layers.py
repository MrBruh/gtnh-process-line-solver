"""Tests for the layer the adapter names on each tower fluid output (``core._output_layers``, #299).

GT fills a Distillation Tower by layer: recipe fluid output ``i`` goes only to the output hatches
on the tower's ``i``-th layer above its base (``MTEDistillationTower.addFluidOutputs``). The dump
records each output cell's layer; the adapter names each fluid output port's, from the fluid's place
among the recipe's fluid outputs, and refuses a plan no tower could honour.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    InfeasiblePlanError,
    MachineConfigControl,
    Node,
    Plan,
    Recipe,
    RecipeSection,
    Resource,
    adapt_file,
    to_input_ir,
)
from gtnh_solver.dataset import (
    DatasetMeta,
    MachinePhysical,
    PhysicalDataset,
    load_physical_dataset,
)
from gtnh_solver.dataset.multiblocks import VariantShape
from gtnh_solver.dataset.schema import SCHEMA_VERSION
from gtnh_solver.ir import Commodity, Facing, InputIR, IODirection, Machine
from tests._helpers import layered_tower

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _ROOT / "data" / "multiblocks"
_NITROBENZENE = _ROOT / "examples" / "gtnh-nitrobenzene.json"

#: The tower this file's plans run on, by the name the dataset below records it under.
_TOWER = "Test Tower"


def _shape(layers: int, stack: int, *, record_layers: bool = True) -> VariantShape:
    """One built form of a tower with ``layers`` output layers, from the shared test tower."""
    tower = layered_tower(outputs=(), layers=layers)
    slots = (
        tower.hatch_slots
        if record_layers
        else tuple(s.model_copy(update={"output_layer": None}) for s in tower.hatch_slots)
    )
    return VariantShape(
        footprint=tower.footprint,
        output_layers=layers,
        hatch_cells=len(slots),
        energy_hatch_cells=8,
        upkeep_hatch_count=1,
        slots=slots,
        trigger_stack_size=stack,
    )


def _dataset(*, record_layers: bool = True) -> PhysicalDataset:
    """A dump whose one controller is a tower built two to four layers tall."""
    variants = tuple(_shape(n, n - 1, record_layers=record_layers) for n in (2, 3, 4))
    record = MachinePhysical(
        key=_TOWER,
        registry_name="test:tower",
        meta=0,
        source_class="test.Tower",
        footprint=variants[-1].footprint,
        io_faces=frozenset(Facing),
        hint_layers=frozenset(),
        coil_layer_count=0,
        variant_count=len(variants),
        variants=variants,
        hatch_cells=variants[-1].hatch_cells,
        energy_hatch_cells=8,
        upkeep_hatch_count=1,
    )
    meta = DatasetMeta.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "pack_version": "test",
            "generated_at": "2026-01-01T00:00:00Z",
            "extractor_sha": "0" * 40,
            "controller_count": 1,
            "census": False,
        }
    )
    return PhysicalDataset(meta=meta, machines={_TOWER: record}, records=(record,))


def _fluid(rid: str, name: str = "") -> Resource:
    return Resource(kind="fluid", id=rid, amount=100.0, display_name=name)


def _recipe(rid: str, outputs: list[Resource], **extra: object) -> Recipe:
    return Recipe(
        id=rid,
        machine_type=_TOWER,
        duration_ticks=20.0,
        inputs=[_fluid("feed")],
        outputs=outputs,
        **extra,
    )


def _plan(*recipes: Recipe) -> Plan:
    first, *rest = recipes
    node = Node(
        id="dt",
        recipe_id=first.id,
        overclock_tier="LV",
        extra_recipes=[RecipeSection(recipe_id=r.id) for r in rest],
    )
    return Plan(schema_version=1, recipes=list(recipes), nodes=[node])


def _adapt(plan: Plan, physical: PhysicalDataset | None = None) -> InputIR:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        return to_input_ir(plan, physical=physical if physical is not None else _dataset())


def _tower(problem: InputIR) -> Machine:
    return next(m for m in problem.machines if m.id == "dt")


def _layers(machine: Machine) -> dict[str, int | None]:
    return {p.id: p.output_layer for p in machine.faces.ports}


# ------------------------------------------------------------------------------ the mapping


def test_the_nitrobenzene_tower_maps_each_product_to_its_place_among_the_fluids() -> None:
    # Wood tar's five products, in the order GT's recipe lists them (DistilleryRecipes.java), each
    # filled from its own layer of the 3x6x3 tower; the committed fixture records those layers.
    problem = adapt_file(_NITROBENZENE, physical=load_physical_dataset(_FIXTURES))
    (tower,) = [m for m in problem.machines if len(m.output_layers) == 5]
    assert {p.id: p.output_layer for p in tower.faces.ports if p.output_layer is not None} == {
        "output:creosote": 0,
        "output:phenol": 1,
        "output:benzene": 2,
        "output:liquid_toluene": 3,
        "output:1,3dimethylbenzene": 4,
    }


def test_fluid_outputs_take_their_index_among_the_fluids_alone() -> None:
    # An item output is not counted: GT's list is the recipe's fluid outputs.
    plan = _plan(_recipe("r", [Resource(kind="item", id="ash"), _fluid("light"), _fluid("heavy")]))
    layers = _layers(_tower(_adapt(plan)))
    assert layers["output:light"] == 0
    assert layers["output:heavy"] == 1


def test_items_inputs_and_power_carry_no_layer() -> None:
    plan = _plan(_recipe("r", [Resource(kind="item", id="ash"), _fluid("light")]))
    tower = _tower(_adapt(plan))
    for port in tower.faces.ports:
        if port.commodity is not Commodity.FLUID or port.direction is not IODirection.OUTPUT:
            assert port.output_layer is None, port.id


def test_the_form_is_sized_for_the_most_fluids_and_every_layer_is_named() -> None:
    plan = _plan(_recipe("r", [_fluid("a"), _fluid("b"), _fluid("c")]))
    tower = _tower(_adapt(plan))
    assert tower.output_layers == {0, 1, 2}
    assert sorted(v for v in _layers(tower).values() if v is not None) == [0, 1, 2]


# ------------------------------------------------------------------------------ time-sharing


def test_time_shared_recipes_may_put_different_fluids_on_one_layer() -> None:
    # They never run at once, so layer 0 takes "a" while recipe one runs and "c" while two does.
    plan = _plan(_recipe("one", [_fluid("a"), _fluid("b")]), _recipe("two", [_fluid("c")]))
    layers = _layers(_tower(_adapt(plan)))
    assert layers["output:a"] == 0
    assert layers["output:c"] == 0
    assert layers["output:b"] == 1


def test_time_shared_recipes_may_share_a_fluid_at_the_same_index() -> None:
    plan = _plan(_recipe("one", [_fluid("a"), _fluid("b")]), _recipe("two", [_fluid("a")]))
    assert _layers(_tower(_adapt(plan)))["output:a"] == 0


def test_one_fluid_at_two_indices_across_time_shared_recipes_is_refused() -> None:
    # One hatch stands on one layer, so the tower would send "b" into the hatch piped for "c".
    plan = _plan(
        _recipe("one", [_fluid("a"), _fluid("b", "Butane")]),
        _recipe("two", [_fluid("b", "Butane"), _fluid("c")]),
    )
    with pytest.raises(InfeasiblePlanError) as caught:
        _adapt(plan)
    infeasibility = caught.value.infeasibility
    assert infeasibility.constraint == "output_layer"
    detail = infeasibility.detail
    # The tower, the fluid and both outputs, numbered from 1 as NEI shows them.
    assert "'dt'" in detail
    assert _TOWER in detail
    assert "Butane (b)" in detail
    assert "fluid output 2 of recipe 'one'" in detail
    assert "fluid output 1 of recipe 'two'" in detail
    assert "split" in (infeasibility.suggested_relaxation or "")


def test_one_fluid_twice_in_one_recipe_is_refused() -> None:
    plan = _plan(_recipe("r", [_fluid("a"), _fluid("b"), _fluid("a")]))
    with pytest.raises(InfeasiblePlanError, match="both fluid output 1 and fluid output 3"):
        _adapt(plan)


# ------------------------------------------------------------------------ what turns it off


def test_a_dump_with_no_layers_names_none() -> None:
    # A tower read from a dump that records no layers fills first fit as far as we know: nothing
    # to tie its outputs to, so nothing is refused either.
    plan = _plan(
        _recipe("one", [_fluid("a"), _fluid("b")]), _recipe("two", [_fluid("b"), _fluid("a")])
    )
    tower = _tower(_adapt(plan, _dataset(record_layers=False)))
    assert tower.output_layers == frozenset()
    assert all(v is None for v in _layers(tower).values())


def test_a_machine_with_no_dataset_record_names_none() -> None:
    plan = _plan(_recipe("r", [_fluid("a"), _fluid("b")]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        tower = _tower(to_input_ir(plan))
    assert all(p.output_layer is None for p in tower.faces.ports)


def test_a_form_the_plan_pins_too_short_is_refused() -> None:
    # The plan names the trigger stack that builds the two-layer form, and the recipe has three
    # fluids: the third would leave through a layer the tower does not have.
    pinned = MachineConfigControl(id="cokeOvenSlices", default_key="slice-1")
    plan = _plan(
        _recipe(
            "r",
            [_fluid("a"), _fluid("b"), _fluid("c", "Coal Tar")],
            machine_config_controls=[pinned],
        )
    )
    with pytest.raises(InfeasiblePlanError) as caught:
        _adapt(plan)
    assert caught.value.infeasibility.constraint == "output_layer"
    assert "fluid output 3 (Coal Tar (c))" in caught.value.infeasibility.detail
    assert "has 2 output layer(s)" in caught.value.infeasibility.detail
