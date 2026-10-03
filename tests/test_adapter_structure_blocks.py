"""What a plan says a multiblock's tiered parts are built from (``adapter/structure_blocks.py``, #312).

First the recipe's special value, which exporters state in up to three places. Then the blocks
each machine is stamped with: the three shipped plans end to end against the committed dataset,
and each rung of each precedence ladder on a one-plant plan. Last, the coil a blast furnace's
recipes need hot enough (#318), on a one-furnace plan.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    Node,
    Plan,
    Recipe,
    adapt_file,
    load_plan,
    to_input_ir,
)
from gtnh_solver.adapter.structure_blocks import CoilHeat, PlannedStructure, recipe_special_value
from gtnh_solver.dataset import (
    CHEMICAL_PLANT,
    COIL,
    HEATING_COILS,
    MACHINE_CASING,
    PIPE,
    SOLID_CASING,
    PhysicalDataset,
    load_physical_dataset,
)
from gtnh_solver.ir import Machine, StructureBlock

_REPO = Path(__file__).resolve().parents[1]
_EXAMPLES = _REPO / "examples"
_DATASET = load_physical_dataset(_REPO / "data" / "multiblocks")
_PLANT = "ExxonMobil Chemical Plant"

#: The blocks the plans below resolve to, by name.
_STABLE_TITANIUM = StructureBlock(block="gregtech:gt.blockcasings4", meta=2)
_HSS_G = StructureBlock(block="gregtech:gt.blockcasings5", meta=4)


def _pipe(meta: int) -> StructureBlock:
    return StructureBlock(block="gregtech:gt.blockcasings2", meta=meta)


def _machine_casing(meta: int) -> StructureBlock:
    return StructureBlock(block="gregtech:gt.blockcasings", meta=meta)


def _heating_coil(meta: int) -> StructureBlock:
    return StructureBlock(block="gregtech:gt.blockcasings5", meta=meta)


def _recipe(**fields: Any) -> Recipe:
    """A Chemical Plant recipe carrying only ``fields`` beyond its identity, as an export spells them."""
    return Recipe.model_validate({"id": "r", "machineType": "Chemical Plant", **fields})


def _plant_recipe(example: str) -> Recipe:
    plan = load_plan(str(_EXAMPLES / example))
    return next(r for r in plan.recipes if "Chemical Plant" in r.machine_type)


# --------------------------------------------------------------------------- special value


@pytest.mark.parametrize(
    "fields",
    [
        {"specialValue": 4},
        {"metadata": {"recipeMapId": "gtpp.recipe.fluidchemicaleactor", "specialValue": 4}},
        {"nei": {"additionalInfo": ["Total: 9,600 EU", "Special value: 4"]}},
        {"nei": {"additionalInfo": ["special  VALUE:4"]}},
    ],
    ids=["top-level", "metadata", "nei", "nei-spacing-and-case"],
)
def test_each_place_a_special_value_is_stated_is_read(fields: dict[str, Any]) -> None:
    assert recipe_special_value(_recipe(**fields)) == 4


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"nei": None},
        {"nei": {"additionalInfo": None}},
        {"nei": {"additionalInfo": ["Total: 9,600 EU"]}},
        {"metadata": {"specialValue": None}},
    ],
    ids=["nothing", "nei-null", "lines-null", "no-matching-line", "metadata-null"],
)
def test_a_recipe_stating_none_has_none(fields: dict[str, Any]) -> None:
    assert recipe_special_value(_recipe(**fields)) is None


def test_agreeing_places_are_read_once_without_a_warning(recwarn: pytest.WarningsRecorder) -> None:
    recipe = _recipe(
        specialValue=4,
        metadata={"specialValue": 4},
        nei={"additionalInfo": ["Special value: 4"]},
    )
    assert recipe_special_value(recipe) == 4
    assert not [w for w in recwarn if issubclass(w.category, AdapterWarning)]


def test_disagreeing_places_warn_and_take_the_highest() -> None:
    recipe = _recipe(specialValue=2, nei={"additionalInfo": ["Special value: 5"]})
    with pytest.warns(AdapterWarning, match=r"states special values \[2, 5\]"):
        assert recipe_special_value(recipe) == 5


def test_a_negative_special_value_reads_as_the_lowest_requirement() -> None:
    assert recipe_special_value(_recipe(nei={"additionalInfo": ["Special value: -1"]})) == 0


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        ("ev-nitrobenzene.json", 4),
        ("gtnh-nitrobenzene.json", 4),
        ("shadow-nitrobenzene.json", None),
    ],
)
def test_the_shipped_plants_state_their_special_value(example: str, expected: int | None) -> None:
    """arodoid states it three times, MrBruh's fork only in NEI, and a converted ShadowTheAge plan
    not at all (it names the casing itself, ``machineConfigTiers.solidCasing``)."""
    assert recipe_special_value(_plant_recipe(example)) == expected


# --------------------------------------------------------------------------- the shipped plans


def _adapt(example: str) -> list[Machine]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return adapt_file(str(_EXAMPLES / example), physical=_DATASET).machines


def _by_controller(machines: list[Machine], block_key: str) -> list[Machine]:
    found = [m for m in machines if m.block_key == block_key]
    assert found, f"no {block_key} in the plan"
    return found


def test_the_arodoid_plant_is_built_from_what_its_node_and_recipe_say() -> None:
    """``examples/ev-nitrobenzene.json``: special value 4 (Stable Titanium), ``pipeCasing``
    Tungstensteel, ``coilTier`` HSS-G, and EV supply (the EV machine casing)."""
    machines = _adapt("ev-nitrobenzene.json")
    for plant in _by_controller(machines, CHEMICAL_PLANT):
        assert plant.structure_blocks == {
            SOLID_CASING: _STABLE_TITANIUM,
            COIL: _HSS_G,
            MACHINE_CASING: _machine_casing(4),
            PIPE: _pipe(15),
        }


def test_the_nodes_coil_reaches_every_multiblock_with_a_coil_choice() -> None:
    """The same plan's Industrial Coke Ovens and Large Fluid Extractor name HSS-G as well; the coil
    rule is not the plant's alone. Only the coil: their other channels are not this rule's."""
    machines = _adapt("ev-nitrobenzene.json")
    for key in ("gregtech:gt.blockmachines@15543", "gregtech:gt.blockmachines@2730"):
        for machine in _by_controller(machines, key):
            assert machine.structure_blocks == {COIL: _HSS_G}


def test_the_mrbruh_plant_reads_its_special_value_from_nei_and_warns_of_its_coil() -> None:
    with pytest.warns(AdapterWarning, match="names no heating coil for its Chemical Plant"):
        machines = adapt_file(str(_EXAMPLES / "gtnh-nitrobenzene.json"), physical=_DATASET).machines
    (plant,) = _by_controller(machines, CHEMICAL_PLANT)
    assert plant.structure_blocks == {
        SOLID_CASING: _STABLE_TITANIUM,
        MACHINE_CASING: _machine_casing(3),
        PIPE: _pipe(14),
    }


def test_the_converted_shadow_plant_takes_the_casing_it_names() -> None:
    """gtnh-shadow-convert names the casing itself and states no special value; its Bronze pipe is
    the tier GT accepts but the dump never records (``dataset.channel_blocks``)."""
    (plant,) = _by_controller(_adapt("shadow-nitrobenzene.json"), CHEMICAL_PLANT)
    assert plant.structure_blocks == {
        SOLID_CASING: _STABLE_TITANIUM,
        COIL: StructureBlock(block="gregtech:gt.blockcasings5", meta=0),
        MACHINE_CASING: _machine_casing(3),
        PIPE: _pipe(12),
    }


def test_without_a_dataset_nothing_is_stamped() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        machines = adapt_file(str(_EXAMPLES / "ev-nitrobenzene.json")).machines
    assert all(m.structure_blocks == {} for m in machines)


# --------------------------------------------------------------------------- the ladders


def _plant(
    *,
    node: dict[str, Any] | None = None,
    recipes: list[dict[str, Any]] | None = None,
    eut: float = 480.0,
    machine_type: str = _PLANT,
    physical: PhysicalDataset = _DATASET,
) -> Machine:
    """The machine the adapter makes of one node running ``recipes`` (time-shared after the first),
    each an export's recipe fields, on the committed dataset unless ``physical`` says otherwise."""
    recipe_fields = recipes if recipes is not None else [{}]
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe.model_validate(
                {
                    "id": f"r{i}",
                    "machineType": machine_type,
                    "eut": eut,
                    "durationTicks": 100.0,
                    **f,
                }
            )
            for i, f in enumerate(recipe_fields)
        ],
        nodes=[
            Node.model_validate(
                {
                    "id": "n",
                    "recipeId": "r0",
                    "overclockTier": "HV",
                    "coilTier": "kanthal",
                    "extraRecipes": [{"recipeId": f"r{i}"} for i in range(1, len(recipe_fields))],
                    **(node or {}),
                }
            )
        ],
    )
    return next(m for m in to_input_ir(plan, physical=physical).machines if m.id == "n")


def _sv(value: int) -> dict[str, Any]:
    return {"specialValue": value}


def _quiet(**kwargs: Any) -> Machine:
    """:func:`_plant`, failing on any AdapterWarning about a tiered part."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        machine = _plant(**kwargs)
    noisy = [str(w.message) for w in caught if issubclass(w.category, AdapterWarning)]
    assert not [m for m in noisy if "casing" in m or "coil" in m], noisy
    return machine


@pytest.mark.parametrize(
    ("value", "meta_block"),
    [
        (0, ("miscutils:gtplusplus.blockspecialcasings.2", 0)),
        (1, ("gregtech:gt.blockcasings2", 0)),
        (4, ("gregtech:gt.blockcasings4", 2)),
        (7, ("miscutils:gtplusplus.blockspecialcasings.2", 3)),
    ],
)
def test_the_solid_casing_is_the_cheapest_whose_tier_meets_the_special_value(
    value: int, meta_block: tuple[str, int]
) -> None:
    casing = _quiet(recipes=[_sv(value)]).structure_blocks[SOLID_CASING]
    assert (casing.block, casing.meta) == meta_block


def test_a_time_shared_plant_is_cased_for_its_most_demanding_recipe() -> None:
    with pytest.warns(AdapterWarning, match="time-shares"):
        plant = _plant(recipes=[_sv(2), _sv(5), _sv(1)])
    assert plant.structure_blocks[SOLID_CASING] == StructureBlock(
        block="gregtech:gt.blockcasings4", meta=0
    )  # tungstensteel, tier 5


def test_a_special_value_above_every_casing_warns_and_takes_the_highest() -> None:
    with pytest.warns(AdapterWarning, match="above every Chemical Plant solid casing"):
        plant = _plant(recipes=[_sv(9)])
    assert plant.structure_blocks[SOLID_CASING] == StructureBlock(
        block="miscutils:gtplusplus.blockspecialcasings.2", meta=3
    )


def test_no_special_value_and_no_casing_warns_and_builds_bronze() -> None:
    with pytest.warns(AdapterWarning, match="state no special value"):
        plant = _plant()
    assert plant.structure_blocks[SOLID_CASING] == StructureBlock(
        block="miscutils:gtplusplus.blockspecialcasings.2", meta=0
    )


def test_a_named_casing_that_meets_the_requirement_is_kept() -> None:
    plant = _quiet(node={"machineConfigTiers": {"solidCasing": "tungstensteel"}}, recipes=[_sv(4)])
    assert plant.structure_blocks[SOLID_CASING] == StructureBlock(
        block="gregtech:gt.blockcasings4", meta=0
    )


def test_a_named_casing_below_the_requirement_is_raised_with_a_warning() -> None:
    with pytest.warns(
        AdapterWarning, match=r"names steel solid casing \(tier 1\), below the tier 4"
    ):
        plant = _plant(node={"machineConfigTiers": {"solidCasing": "steel"}}, recipes=[_sv(4)])
    assert plant.structure_blocks[SOLID_CASING] == _STABLE_TITANIUM


def test_an_unknown_casing_key_warns_and_falls_back_to_the_requirement() -> None:
    with pytest.warns(AdapterWarning, match="'unobtainium', which is not a solid casing"):
        plant = _plant(
            node={"machineConfigTiers": {"solidCasing": "unobtainium"}}, recipes=[_sv(4)]
        )
    assert plant.structure_blocks[SOLID_CASING] == _STABLE_TITANIUM


@pytest.mark.parametrize(
    ("node", "controls", "meta"),
    [
        ({"machineConfigTiers": {"pipeCasing": "steel"}}, [], 13),
        ({}, [{"id": "pipeCasing", "defaultKey": "titanium"}], 14),
        (
            {"machineConfigTiers": {"pipeCasing": "steel"}},
            [{"id": "pipeCasing", "defaultKey": "titanium"}],
            13,
        ),
        ({}, [], 12),
    ],
    ids=["node", "control-default", "node-beats-default", "nothing-stated-is-bronze"],
)
def test_the_pipe_casing_ladder(
    node: dict[str, Any], controls: list[dict[str, Any]], meta: int
) -> None:
    plant = _quiet(node=node, recipes=[{**_sv(0), "machineConfigControls": controls}])
    assert plant.structure_blocks[PIPE] == _pipe(meta)


def test_a_handlers_pipe_default_comes_before_its_recipes() -> None:
    recipe = {
        **_sv(0),
        "machineConfigControls": [{"id": "pipeCasing", "defaultKey": "steel"}],
        "machineHandlers": [
            {
                "id": "h",
                "kind": "multiblock",
                "label": _PLANT,
                "machineConfigControls": [{"id": "pipeCasing", "defaultKey": "titanium"}],
            }
        ],
    }
    assert _quiet(recipes=[recipe]).structure_blocks[PIPE] == _pipe(14)


@pytest.mark.parametrize("key", ["ptfe", "pbi"])
def test_a_pipe_the_plant_refuses_builds_as_tungstensteel_with_a_warning(key: str) -> None:
    with pytest.warns(AdapterWarning, match=f"{key!r}, a pipe casing its machine does not accept"):
        plant = _plant(node={"machineConfigTiers": {"pipeCasing": key}}, recipes=[_sv(0)])
    assert plant.structure_blocks[PIPE] == _pipe(15)


def test_an_unknown_pipe_key_warns_and_falls_through_to_the_default() -> None:
    recipe = {**_sv(0), "machineConfigControls": [{"id": "pipeCasing", "defaultKey": "titanium"}]}
    with pytest.warns(AdapterWarning, match="'copper', which is not a pipe casing"):
        plant = _plant(node={"machineConfigTiers": {"pipeCasing": "copper"}}, recipes=[recipe])
    assert plant.structure_blocks[PIPE] == _pipe(14)


@pytest.mark.parametrize(
    ("node", "controls", "meta"),
    [
        ({"coilTier": "nichrome"}, [], 2),
        ({"coilTier": "", "machineConfigTiers": {"heatingCoil": "tpv"}}, [], 3),
        ({"coilTier": ""}, [{"id": "heatingCoil", "defaultKey": "kanthal"}], 1),
        ({"coilTier": "hss_s"}, [{"id": "heatingCoil", "defaultKey": "kanthal"}], 9),
    ],
    ids=["coil-tier", "config-tier", "control-default", "coil-tier-beats-default"],
)
def test_the_coil_ladder(node: dict[str, Any], controls: list[dict[str, Any]], meta: int) -> None:
    plant = _quiet(node=node, recipes=[{**_sv(0), "machineConfigControls": controls}])
    assert plant.structure_blocks[COIL] == StructureBlock(
        block="gregtech:gt.blockcasings5", meta=meta
    )


def test_an_unknown_coil_warns_and_falls_through() -> None:
    recipe = {**_sv(0), "machineConfigControls": [{"id": "heatingCoil", "defaultKey": "kanthal"}]}
    with pytest.warns(AdapterWarning, match="'adamantium', which is not a heating coil"):
        plant = _plant(node={"coilTier": "adamantium"}, recipes=[recipe])
    assert plant.structure_blocks[COIL] == StructureBlock(block="gregtech:gt.blockcasings5", meta=1)


def test_the_hand_written_ebf_takes_a_coil_it_records() -> None:
    """The EBF fixture lists only coil metas 0-3, numbered from 0: a block, not a channel value,
    is what the coil is matched by."""
    ebf = _quiet(node={"coilTier": "nichrome"}, machine_type="Electric Blast Furnace")
    assert ebf.structure_blocks == {COIL: StructureBlock(block="gregtech:gt.blockcasings5", meta=2)}


def test_a_coil_the_machine_does_not_record_warns_and_is_left_as_dumped() -> None:
    with pytest.warns(AdapterWarning, match="does not accept gregtech:gt.blockcasings5@4"):
        ebf = _plant(node={"coilTier": "hss_g"}, machine_type="Electric Blast Furnace")
    assert ebf.structure_blocks == {}


def test_a_machine_without_a_coil_channel_ignores_the_nodes_coil() -> None:
    freezer = _quiet(node={"coilTier": "hss_g"}, machine_type="Vacuum Freezer")
    assert freezer.structure_blocks == {}


def test_a_fixed_single_coil_is_not_the_plans_to_choose() -> None:
    """A coil channel of one block is a fixed part; arodoid writes a ``coilTier`` on every node, so
    it must pass silently rather than warn on every such machine."""
    record = _DATASET.by_block_key[CHEMICAL_PLANT]
    fixed = replace(
        record,
        key="Fixed Coil Machine",
        meta=1,
        substitutions=((COIL, (("gregtech:gt.blockcasings5", 0),)),),
    )
    dataset = PhysicalDataset(meta=_DATASET.meta, machines={fixed.key: fixed}, records=(fixed,))
    machine = _quiet(node={"coilTier": "hss_g"}, machine_type=fixed.key, physical=dataset)
    assert machine.structure_blocks == {}


def test_the_casing_rules_are_the_plants_alone() -> None:
    """Another controller with the plant's channels gets only its coil: what the casing, pipe and
    machine casing mean is read off the plant's own structure definition."""
    record = _DATASET.by_block_key[CHEMICAL_PLANT]
    other = replace(record, key="Other Plant", meta=1)
    dataset = PhysicalDataset(meta=_DATASET.meta, machines={other.key: other}, records=(other,))
    machine = _quiet(recipes=[_sv(4)], machine_type=other.key, physical=dataset)
    assert machine.structure_blocks.keys() == {COIL}


def test_a_re_tiered_plant_takes_the_machine_casing_of_the_tier_it_is_supplied_at() -> None:
    """A draw no 3 HV hatches can take is supplied at EV (``power._supply_tier``), and the export
    places EV hatches; an HV machine casing would keep the plant from forming."""
    plant = _quiet(recipes=[_sv(4)], eut=8000.0)
    assert plant.voltage_tier == "EV"
    assert plant.structure_blocks[MACHINE_CASING] == _machine_casing(4)


@pytest.mark.parametrize("missing", [MACHINE_CASING, SOLID_CASING, PIPE])
def test_a_plant_part_the_dump_does_not_record_is_not_planned(missing: str) -> None:
    """The dump says which tiered parts a structure has; a channel it does not record is drawn as
    dumped, silently, whatever the plan says about it."""
    record = _DATASET.by_block_key[CHEMICAL_PLANT]
    bare = replace(record, substitutions=tuple(c for c in record.substitutions if c[0] != missing))
    dataset = PhysicalDataset(meta=_DATASET.meta, machines={bare.key: bare}, records=(bare,))
    plant = _quiet(recipes=[_sv(4)], physical=dataset)
    assert plant.structure_blocks.keys() == {COIL, MACHINE_CASING, PIPE, SOLID_CASING} - {missing}


def test_a_supplied_tier_with_no_accepted_machine_casing_warns_and_draws_the_dumps() -> None:
    planned = PlannedStructure(node_id="n", machine_casings=(("gregtech:gt.blockcasings", 3),))
    with pytest.warns(AdapterWarning, match="supplied at 'EV', which has no machine casing"):
        assert planned.blocks("EV") == {}


def test_a_dump_contradicting_the_rule_leaves_the_part_as_dumped() -> None:
    """When a dump lists a block the cited rule does not accept, the dump's list wins
    (``dataset.channel_blocks``), and a block the rule then picks on its own may not be in it."""
    record = _DATASET.by_block_key[CHEMICAL_PLANT]
    stray = ("gregtech:gt.blockcasings8", 1)
    contradicting = replace(
        record,
        substitutions=tuple(
            (channel, (blocks[0], stray)) if channel in (PIPE, SOLID_CASING) else (channel, blocks)
            for channel, blocks in record.substitutions
        ),
    )
    dataset = PhysicalDataset(
        meta=_DATASET.meta, machines={contradicting.key: contradicting}, records=(contradicting,)
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plant = _plant(recipes=[_sv(4)], physical=dataset)
    refused = [
        str(w.message) for w in caught if "its machine's dump does not accept" in str(w.message)
    ]
    assert len(refused) == 2  # the titanium solid casing and the bronze pipe
    assert plant.structure_blocks.keys() == {COIL, MACHINE_CASING}


# --------------------------------------------------------------------------- the coil's heat
#
# An EBF reaches its coil's heat plus 100 K per tier above MV; the plans below supply it at HV
# (``_plant``'s node), so +100 K: Cupronickel 1901 K, Kanthal 2801 K, Nichrome 3701 K, TPV 4601 K.

_EBF = "Electric Blast Furnace"


def _furnace(key: str = _EBF, meta: int = 1000) -> PhysicalDataset:
    """The committed EBF's structure as controller ``meta``, named ``key``, accepting every coil.

    The committed fixture records only Cupronickel to TPV, which is all most cases need."""
    ebf = _DATASET.by_block_key["gregtech:gt.blockmachines@1000"]
    furnace = replace(
        ebf,
        key=key,
        meta=meta,
        substitutions=((COIL, tuple(rung.block_id for rung in HEATING_COILS)),),
    )
    return PhysicalDataset(meta=_DATASET.meta, machines={furnace.key: furnace}, records=(furnace,))


def test_a_coil_hot_enough_for_its_recipes_is_kept() -> None:
    ebf = _quiet(node={"coilTier": "nichrome"}, recipes=[_sv(3701)], machine_type=_EBF)
    assert ebf.structure_blocks == {COIL: _heating_coil(2)}


def test_a_named_coil_too_cold_is_raised_with_a_warning() -> None:
    with pytest.warns(
        AdapterWarning,
        match=r"^node 'n' names kanthal heating coil \(2801 K at HV\), below the 3000 K its "
        r"recipes need; building it with nichrome$",
    ):
        ebf = _plant(recipes=[_sv(3000)], machine_type=_EBF)
    assert ebf.structure_blocks == {COIL: _heating_coil(2)}


def test_no_named_coil_and_the_dumps_too_cold_is_raised_with_a_warning() -> None:
    with pytest.warns(
        AdapterWarning,
        match=r"^node 'n' names no heating coil its machine accepts, so it would be drawn with the "
        r"dump's cupronickel \(1901 K at HV\), below the 2000 K its recipes need; building it "
        r"with kanthal$",
    ):
        ebf = _plant(node={"coilTier": ""}, recipes=[_sv(2000)], machine_type=_EBF)
    assert ebf.structure_blocks == {COIL: _heating_coil(1)}


def test_no_named_coil_and_the_dumps_hot_enough_is_left_as_dumped() -> None:
    ebf = _quiet(node={"coilTier": ""}, recipes=[_sv(1901)], machine_type=_EBF)
    assert ebf.structure_blocks == {}


def test_a_furnace_whose_recipes_state_no_heat_keeps_its_named_coil() -> None:
    """A converted ShadowTheAge plan states no special value; its converter warns of a coil too
    cold itself, so nothing is checked and nothing warns here."""
    ebf = _quiet(node={"coilTier": "cupronickel"}, machine_type=_EBF)
    assert ebf.structure_blocks == {COIL: _heating_coil(0)}


@pytest.mark.parametrize(
    ("physical", "heat", "top", "reach", "meta"),
    [(_DATASET, 5000, "tpv", 4601, 3), (_furnace(), 20000, "eternal", 13601, 13)],
    ids=["above-the-committed-fixtures-tpv", "above-eternal"],
)
def test_a_heat_above_every_accepted_coil_builds_the_hottest_with_a_warning(
    physical: PhysicalDataset, heat: int, top: str, reach: int, meta: int
) -> None:
    with pytest.warns(
        AdapterWarning,
        match=rf"^node 'n' runs a recipe of {heat} K, above the {reach} K of {top} at HV, the "
        rf"hottest coil its machine's dump records; building it with {top}, on which GT will "
        r"still refuse that recipe$",
    ):
        ebf = _plant(recipes=[_sv(heat)], machine_type=_EBF, physical=physical)
    assert ebf.structure_blocks == {COIL: _heating_coil(meta)}


def test_the_raise_follows_coil_tier_not_meta() -> None:
    """HSS-G (meta 4, 5501 K at HV) is followed by HSS-S (meta 9, 6401 K), not Naquadah (meta 5)."""
    with pytest.warns(AdapterWarning, match="building it with hss_s$"):
        ebf = _plant(
            node={"coilTier": "hss_g"}, recipes=[_sv(6000)], machine_type=_EBF, physical=_furnace()
        )
    assert ebf.structure_blocks == {COIL: _heating_coil(9)}


def test_an_lv_hatch_costs_100_k() -> None:
    with pytest.warns(AdapterWarning, match=r"names cupronickel heating coil \(1701 K at LV\)"):
        ebf = _plant(
            node={"overclockTier": "LV", "coilTier": "cupronickel"},
            recipes=[_sv(1750)],
            eut=30.0,
            machine_type=_EBF,
        )
    assert ebf.voltage_tier == "LV"
    assert ebf.structure_blocks == {COIL: _heating_coil(1)}


def test_the_coil_is_settled_at_the_tier_the_machine_is_supplied_at() -> None:
    """Nichrome reaches 3701 K at HV and 3801 K at EV, so a 3750 K recipe raises it at HV only."""
    planned = PlannedStructure(
        node_id="n",
        coil_heat=CoilHeat(
            required=3750, tier_bonus=True, coils=HEATING_COILS, named=HEATING_COILS[2]
        ),
    )
    with pytest.warns(AdapterWarning, match=r"nichrome heating coil \(3701 K at HV\)"):
        assert planned.blocks("HV") == {COIL: _heating_coil(3)}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert planned.blocks("EV") == {COIL: _heating_coil(2)}


def test_a_re_tiered_furnace_is_coiled_for_the_tier_it_is_supplied_at() -> None:
    """The heat is read after the power synthesis, which can raise the tier the plan states."""
    ebf = _quiet(node={"coilTier": "nichrome"}, recipes=[_sv(3750)], eut=8000.0, machine_type=_EBF)
    assert ebf.voltage_tier == "EV"
    assert ebf.structure_blocks == {COIL: _heating_coil(2)}


def test_a_volcanus_reads_the_coil_alone() -> None:
    """The Nichrome an HV EBF keeps for a 3701 K recipe (3601 K + 100 K) is too cold in a Volcanus,
    which adds no tier bonus."""
    with pytest.warns(AdapterWarning, match=r"names nichrome heating coil \(3601 K at HV\)"):
        volcanus = _plant(
            node={"coilTier": "nichrome"},
            recipes=[_sv(3701)],
            machine_type="Volcanus",
            physical=_furnace("Volcanus", 963),
        )
    assert volcanus.structure_blocks == {COIL: _heating_coil(3)}


def test_a_coil_that_sets_only_speed_is_never_checked() -> None:
    oven = _quiet(
        recipes=[_sv(20000)], machine_type="Pyrolyse Oven", physical=_furnace("Pyrolyse Oven", 1159)
    )
    assert oven.structure_blocks == {COIL: _heating_coil(1)}


def test_a_time_shared_furnace_is_coiled_for_its_hottest_recipe() -> None:
    with (
        pytest.warns(AdapterWarning, match="time-shares"),
        pytest.warns(AdapterWarning, match="below the 3500 K its recipes need"),
    ):
        ebf = _plant(recipes=[_sv(2000), _sv(3500), _sv(1000)], machine_type=_EBF)
    assert ebf.structure_blocks == {COIL: _heating_coil(2)}


def test_a_plants_disagreeing_special_values_warn_once() -> None:
    """The heat check asks whether a machine is heat-gated before it reads a special value, so a
    Chemical Plant's recipe is read for its casing alone."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _plant(recipes=[{"specialValue": 2, "nei": {"additionalInfo": ["Special value: 5"]}}])
    assert sum("states special values" in str(w.message) for w in caught) == 1
