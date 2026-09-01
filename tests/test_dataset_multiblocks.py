"""Golden + adapter tests for the multiblock physical dataset.

Two layers:
- **Golden facts** (plan section 7): ground truths that only change when GTNH changes them - the
  EBF is 3x3x4 with exactly two coil layers and hints on its hatch layer; the Vacuum Freezer is
  3x3x3. These catch an extractor regression (bad sweep, bad scan bound, facing bug).
- **Adapter unit + integration**: the interpretation branches (footprint/face/coil derivation,
  primary-variant selection, the bbox-mismatch and duplicate-key error paths) and the opt-in wiring
  into the gtnh-factory-flow adapter.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    Edge,
    MachineBlock,
    Node,
    Plan,
    Recipe,
    RecipeSource,
    ResolvedBlock,
    ResolvedMachine,
    Resource,
    to_input_ir,
)
from gtnh_solver.dataset import (
    DatasetError,
    MultiblockDoc,
    PhysicalDataset,
    load_physical_dataset,
    to_physical,
)
from gtnh_solver.ir import CellBox, Facing, LayoutStatus
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate
from tests._helpers import PLACEMENT_CODES

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "multiblocks"


@pytest.fixture(scope="module")
def dataset() -> PhysicalDataset:
    return load_physical_dataset(_DATA_DIR)


# --------------------------------------------------------------------------- golden facts


def test_ebf_main_piece_is_3x3x4(dataset: PhysicalDataset) -> None:
    ebf = dataset.get("Electric Blast Furnace")
    assert ebf is not None
    assert ebf.footprint == CellBox(sx=3, sy=4, sz=3)  # 3 wide, 3 deep, 4 tall


def test_ebf_has_exactly_two_coil_layers(dataset: PhysicalDataset) -> None:
    ebf = dataset.get("Electric Blast Furnace")
    assert ebf is not None
    assert ebf.coil_layer_count == 2


def test_ebf_hints_on_the_hatch_layer(dataset: PhysicalDataset) -> None:
    ebf = dataset.get("Electric Blast Furnace")
    assert ebf is not None
    assert ebf.hint_layers == frozenset({0})  # hatch dots ride the bottom casing ring
    # the hatch ring is on the bottom, so its faces are the four walls plus the floor (never the top)
    assert Facing.NORTH in ebf.io_faces
    assert Facing.UP not in ebf.io_faces


def test_vacuum_freezer_is_3x3x3(dataset: PhysicalDataset) -> None:
    vf = dataset.get("Vacuum Freezer")
    assert vf is not None
    assert vf.footprint == CellBox(sx=3, sy=3, sz=3)
    assert vf.coil_layer_count == 0  # a freezer has no heating coils


# ------------------------------------------------------------------ dataset load / lookup


def test_load_physical_dataset_keys_by_display_name(dataset: PhysicalDataset) -> None:
    assert set(dataset.machines) == {"Electric Blast Furnace", "Vacuum Freezer"}
    assert dataset.get("Nonexistent Machine") is None
    assert dataset.meta.controller_count == 2


def test_default_data_dir_resolves_to_the_committed_dump() -> None:
    # No explicit directory -> the source-relative default; proves the packaged path resolves.
    ds = load_physical_dataset()
    assert ds.get("Electric Blast Furnace") is not None


# ------------------------------------------------- controller-block join (gtnh-factory-flow #25)
#
# The exporter names a machine by its localized recipe-map name, which for a GT++ machine is NOT
# the controller block's own name this dump is keyed by ("Chemical Plant" vs "ExxonMobil Chemical
# Plant"). A plan carrying `recipe.source.machineBlock` joins on the block identity instead.


def test_block_key_is_registry_name_at_meta(dataset: PhysicalDataset) -> None:
    ebf = dataset.get("Electric Blast Furnace")
    assert ebf is not None
    assert ebf.block_key == "gregtech:gt.blockmachines@1000"
    assert set(dataset.by_block_key) == {
        "gregtech:gt.blockmachines@1000",
        "gregtech:gt.blockmachines@1001",
    }


def test_block_key_resolves_a_machine_whose_name_does_not_match(dataset: PhysicalDataset) -> None:
    # The whole point: the name is wrong (as it is for every GT++ machine) and the lookup still
    # lands on the right controller, because the block key is an exact identity.
    record = dataset.get(
        "Some Localized Recipe Map Name", block_key="gregtech:gt.blockmachines@1000"
    )
    assert record is not None
    assert record.key == "Electric Blast Furnace"


def test_unknown_block_key_falls_back_to_the_name(dataset: PhysicalDataset) -> None:
    # The dump is a partial snapshot (failed controllers are simply absent), so a block key it does
    # not know must degrade to the name lookup rather than turn a resolvable machine into a miss.
    record = dataset.get("Electric Blast Furnace", block_key="gregtech:gt.blockmachines@99999")
    assert record is not None
    assert record.key == "Electric Blast Furnace"


def test_lookup_without_a_block_key_is_unchanged(dataset: PhysicalDataset) -> None:
    # Every pre-#25 plan takes this path; it must behave exactly as it did before the join existed.
    assert dataset.get("Electric Blast Furnace") is dataset.get(
        "Electric Blast Furnace", block_key=None
    )
    assert dataset.get("Nonexistent Machine", block_key=None) is None


# ------------------------------------------------- layer-indexed height selection (GitHub #98)
#
# A Distillation Tower routes the recipe's fluid output i to structure layer i and nowhere else, so
# a tower with fewer output layers than the recipe has fluid outputs is still a LEGAL build that
# silently voids the remainder. Sizing it to the recipe is therefore a correctness matter, not
# cosmetics - and mis-sizing DOWNWARD is the dangerous direction.


def _tower_doc(
    heights: Iterable[int],
    *,
    output_layers_per_step: int = 1,
    kinds: list[str] | None = None,
) -> MultiblockDoc:
    """A synthetic parametric tower: one variant per height, 3x3 base, hatch slots up each layer.

    ``output_layers_per_step`` models a machine whose "output layer" spans several block layers (the
    Mega Distillation Tower's is a 5-block band), which is what must NOT be selected on. ``kinds``
    widens what each slot accepts, for tests that need energy-capable cells.
    """
    slot_kinds = kinds if kinds is not None else ["OutputHatch"]
    variants = []
    for h in heights:
        blocks = [
            {"d": [x, y, z], "block": "casing", "meta": 0}
            for y in range(h)
            for x in range(3)
            for z in range(3)
        ]
        slots = [
            {"d": [1, y, 1], "kinds": slot_kinds}
            for y in range(1, h)
            if (y - 1) % output_layers_per_step == 0 or output_layers_per_step == 1
        ]
        variants.append(
            {"trigger_stack_size": h, "blocks": blocks, "hatch_slots": slots, "bbox": [3, h, 3]}
        )
    return MultiblockDoc.model_validate(
        {
            "schema": 2,
            "controller": {
                "registry_name": "r",
                "meta": 0,
                "display_name": "Tower",
                "source_class": "C",
            },
            "variants": variants,
        }
    )


def test_layer_indexed_tower_is_sized_to_the_recipe() -> None:
    record = to_physical(_tower_doc(range(3, 13)))  # heights 3..12 -> 2..11 output layers
    assert record.is_layer_indexed
    assert record.footprint_for(1) == CellBox(sx=3, sy=3, sz=3)  # one output: the minimum tower
    assert record.footprint_for(5) == CellBox(sx=3, sy=6, sz=3)  # H = max(3, N+1)
    assert record.footprint_for(11) == CellBox(sx=3, sy=12, sz=3)


def test_a_recipe_needing_more_outputs_than_any_form_takes_the_largest() -> None:
    # Over-reserving is safe; under-reserving voids product. An impossible ask must not silently
    # land on a small tower.
    record = to_physical(_tower_doc(range(3, 13)))
    assert record.footprint_for(99) == CellBox(sx=3, sy=12, sz=3)


def test_a_banded_tower_declines_selection_and_takes_the_largest() -> None:
    """The Mega Distillation Tower case: its output layer is a 5-block band.

    Its per-layer hatch count runs ahead of its routable-output count, so selecting on it would pick
    a tower several times too short and void fluids. An unreadable growth pattern must fall back.
    """
    record = to_physical(_tower_doc([11, 16, 21], output_layers_per_step=5))
    assert not record.is_layer_indexed
    assert record.footprint_for(2) == record.footprint  # the largest form, not a guess


def test_a_fixed_shape_machine_ignores_the_recipe() -> None:
    doc = MultiblockDoc.model_validate(
        {
            "schema": 2,
            "controller": {
                "registry_name": "r",
                "meta": 0,
                "display_name": "Fixed",
                "source_class": "C",
            },
            "variants": [{"trigger_stack_size": 1, "blocks": _cube_blocks(3), "bbox": [3, 3, 3]}],
        }
    )
    record = to_physical(doc)
    assert not record.is_layer_indexed  # a single form is not a family
    assert record.footprint_for(7) == CellBox(sx=3, sy=3, sz=3)


def test_a_dump_without_hatch_slots_keeps_the_old_behaviour() -> None:
    # A pre-v2 dump records no hatch data; every machine must keep resolving to its largest form
    # rather than collapsing to the smallest because capacity reads as zero.
    doc = _tower_doc(range(3, 6))
    for variant in doc.variants:
        variant.hatch_slots.clear()
    record = to_physical(doc)
    assert not record.is_layer_indexed
    assert record.footprint_for(4) == CellBox(sx=3, sy=5, sz=3)  # the largest form


def test_hatch_counts_come_from_the_selected_form_not_the_largest() -> None:
    """A form's hatch ceiling is its own, not the tallest form's.

    ``_tower_doc`` puts one slot per layer above the base, so a height-h tower has h-1 cells. The
    record-level count stays the largest form's (it describes ``footprint``); reading a reserved
    form's ceiling goes through ``variant_for``.
    """
    record = to_physical(_tower_doc(range(3, 13)))
    assert record.hatch_cells == 11  # the 12-tall form, which `footprint` also describes

    small = record.variant_for(1)
    assert (small.footprint, small.hatch_cells) == (CellBox(sx=3, sy=3, sz=3), 2)
    mid = record.variant_for(5)
    assert (mid.footprint, mid.hatch_cells) == (CellBox(sx=3, sy=6, sz=3), 5)
    # Past every form's capacity the largest is still the answer, counts included.
    assert record.variant_for(99).hatch_cells == 11


def test_energy_hatch_budget_is_charged_against_the_reserved_form() -> None:
    # The budget is a ceiling on power connections, so taking it from a taller form than the one
    # the builder raises would certify a machine with more energy hatches than it has cells.
    record = to_physical(_tower_doc(range(3, 13), kinds=["OutputHatch", "Energy"]))
    assert record.energy_hatch_budget(fluid_outputs=1) == 2  # the 3-tall form has 2 cells
    assert record.energy_hatch_budget(fluid_outputs=5) == 5  # the 6-tall form has 5
    assert record.energy_hatch_budget(routed_ports=3, fluid_outputs=5) == 2  # buses take theirs


def test_every_dumped_controller_agrees_on_which_form_it_reserved(
    dataset: PhysicalDataset,
) -> None:
    """The footprint and the hatch counts must never describe different built forms.

    The invariant this lane exists to establish, asserted over the whole dump rather than one
    machine: whatever ``footprint_for`` reserves, ``variant_for`` returns the shape of, so a caller
    cannot pick up one form's box and another form's ceiling.
    """
    for name, record in dataset.machines.items():
        for fluid_outputs in range(0, 13):
            shape = record.variant_for(fluid_outputs)
            assert shape.footprint == record.footprint_for(fluid_outputs), name
            assert shape.hatch_cells >= shape.energy_hatch_cells, name
            assert shape.hatch_cells >= shape.upkeep_hatch_count, name


def test_adapter_sizes_a_tower_from_the_recipes_fluid_outputs() -> None:
    """End to end: two recipes on the same machine type get different reserved heights."""
    dataset = PhysicalDataset(
        meta=load_physical_dataset(_DATA_DIR).meta,
        machines={"Tower": to_physical(_tower_doc(range(3, 13)))},
    )
    for fluids, expected_height in ((1, 3), (5, 6)):
        outputs = [Resource(kind="fluid", id=f"fluid:{i}", amount=1.0) for i in range(fluids)]
        recipe = Recipe(
            id="r", machine_type="Tower", eut=480.0, duration_ticks=100.0, outputs=outputs
        )
        plan = Plan(
            schema_version=1,
            recipes=[recipe],
            nodes=[Node(id="n", recipe_id="r", overclock_tier="MV")],
        )
        machine = next(m for m in to_input_ir(plan, physical=dataset).machines if m.id == "n")
        assert machine.footprint.sy == expected_height, f"{fluids} fluid outputs"
        # The hatch ceiling must describe the SAME form. _tower_doc gives a height-h tower h-1
        # slots, so a 3-tall tower has 2 cells and a 6-tall one 5; before this was fixed both were
        # charged the 12-tall form's 11, a ceiling the reserved shape cannot host.
        assert machine.hatch_cells == expected_height - 1, f"{fluids} fluid outputs"


# --------------------------------------------------------------- interpretation branches


def _cube_blocks(size: int) -> list[dict[str, object]]:
    return [
        {"d": [x, y, z], "block": "casing", "meta": 0}
        for x in range(size)
        for y in range(size)
        for z in range(size)
    ]


def test_hint_positions_map_to_all_six_faces() -> None:
    # A hint centred on each face of a 3x3x3 box should mark every one of the six faces I/O-capable.
    doc = MultiblockDoc.model_validate(
        {
            "schema": 1,
            "controller": {
                "registry_name": "r",
                "meta": 0,
                "display_name": "Cube",
                "source_class": "C",
            },
            "variants": [
                {
                    "trigger_stack_size": 1,
                    "blocks": _cube_blocks(3),
                    "hints": [
                        {"d": [1, 1, 0], "hint": 1},  # north
                        {"d": [1, 1, 2], "hint": 1},  # south
                        {"d": [0, 1, 1], "hint": 1},  # west
                        {"d": [2, 1, 1], "hint": 1},  # east
                        {"d": [1, 0, 1], "hint": 1},  # down
                        {"d": [1, 2, 1], "hint": 1},  # up
                    ],
                    "bbox": [3, 3, 3],
                }
            ],
        }
    )
    record = to_physical(doc)
    assert record.io_faces == frozenset(Facing)
    assert record.hint_layers == frozenset({0, 1, 2})


def test_primary_variant_is_the_largest_built_form() -> None:
    # A small variant plus a bigger one; the footprint must come from the bigger (fully-built) one.
    doc = MultiblockDoc.model_validate(
        {
            "schema": 1,
            "controller": {
                "registry_name": "r",
                "meta": 0,
                "display_name": "Grower",
                "source_class": "C",
            },
            "variants": [
                {
                    "trigger_stack_size": 1,
                    "blocks": _cube_blocks(2),
                    "bbox": [2, 2, 2],
                },
                {
                    "trigger_stack_size": 2,
                    "blocks": _cube_blocks(4),
                    "bbox": [4, 4, 4],
                },
            ],
        }
    )
    record = to_physical(doc)
    assert record.footprint == CellBox(sx=4, sy=4, sz=4)
    assert record.variant_count == 2


def test_bbox_mismatch_raises_dataset_error() -> None:
    doc = MultiblockDoc.model_validate(
        {
            "schema": 1,
            "controller": {
                "registry_name": "r",
                "meta": 0,
                "display_name": "Liar",
                "source_class": "C",
            },
            # blocks span 3x3x3 but the bbox claims a fourth layer -> extractor scan bug
            "variants": [{"trigger_stack_size": 1, "blocks": _cube_blocks(3), "bbox": [3, 4, 3]}],
        }
    )
    with pytest.raises(DatasetError, match="bbox"):
        to_physical(doc)


def test_duplicate_machine_key_raises(tmp_path: Path) -> None:
    meta = {
        "schema": 1,
        "pack_version": "test",
        "generated_at": "now",
        "extractor_sha": "0",
        "controller_count": 2,
    }
    (tmp_path / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    doc = {
        "schema": 1,
        "controller": {"registry_name": "r", "meta": 0, "display_name": "Dup", "source_class": "C"},
        "variants": [{"trigger_stack_size": 1, "blocks": _cube_blocks(1), "bbox": [1, 1, 1]}],
    }
    (tmp_path / "a.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(DatasetError, match="Dup"):
        load_physical_dataset(tmp_path)


# ---------------------------------------------------------- opt-in gtnh-factory-flow wiring


def _one_machine_plan(machine_type: str, block_key: str | None = None) -> Plan:
    recipe = Recipe(
        id="r",
        machine_type=machine_type,
        eut=480.0,
        duration_ticks=100.0,
        inputs=[Resource(kind="item", id="minecraft:iron_ingot", amount=1.0)],
        outputs=[Resource(kind="item", id="minecraft:iron_block", amount=1.0)],
        source=(
            RecipeSource(machine_block=MachineBlock(id=block_key))
            if block_key is not None
            else None
        ),
    )
    node = Node(id="n", recipe_id="r", overclock_tier="MV")
    return Plan(schema_version=1, recipes=[recipe], nodes=[node])


def test_adapter_stamps_real_footprint_when_dataset_knows_the_type(
    dataset: PhysicalDataset,
) -> None:
    ir = to_input_ir(_one_machine_plan("Electric Blast Furnace"), physical=dataset)
    machine = next(m for m in ir.machines if m.id == "n")
    assert machine.footprint == CellBox(sx=3, sy=4, sz=3)


def test_adapter_defaults_to_single_block_without_a_dataset() -> None:
    ir = to_input_ir(_one_machine_plan("Electric Blast Furnace"))
    machine = next(m for m in ir.machines if m.id == "n")
    assert machine.footprint == CellBox()  # 1x1x1


def test_adapter_falls_back_for_a_type_the_dataset_lacks(dataset: PhysicalDataset) -> None:
    ir = to_input_ir(_one_machine_plan("Some Unknown Machine"), physical=dataset)
    machine = next(m for m in ir.machines if m.id == "n")
    assert machine.footprint == CellBox()  # 1x1x1 fallback


def test_adapter_resolves_the_footprint_from_the_exported_controller_block(
    dataset: PhysicalDataset,
) -> None:
    """A GT++-style plan: the machine type does not match the dump, the block key does.

    This is the Chemical Plant / Coke Oven case from GitHub #98 in miniature. Without the block key
    this machine silently reserves 1x1x1 and renders as a lone cube; with it, the real multiblock
    footprint lands and the previewer expands the full structure.
    """
    plan = _one_machine_plan(
        "A Recipe Map Name The Dump Never Heard Of",
        block_key="gregtech:gt.blockmachines@1000",
    )
    machine = next(m for m in to_input_ir(plan, physical=dataset).machines if m.id == "n")
    assert machine.footprint == CellBox(sx=3, sy=4, sz=3)  # the EBF's real shape
    assert machine.block_key == "gregtech:gt.blockmachines@1000"


def test_adapter_reads_the_controller_block_from_the_resolved_block(
    dataset: PhysicalDataset,
) -> None:
    # gtnh-factory-flow #25 mirrors machineBlock into `resolved.machines[]`; accept it from there
    # too, so a plan whose resolved block is richer than its recipes still joins.
    plan = _one_machine_plan("Unmatched Name")
    plan = plan.model_copy(
        update={
            "resolved": ResolvedBlock(
                machines=[
                    ResolvedMachine(
                        node_id="n",
                        machine_block=MachineBlock(id="gregtech:gt.blockmachines@1001"),
                        total_eut=480.0,  # matches the recipe, so no EU/t-mismatch warning fires
                    )
                ]
            )
        }
    )
    machine = next(m for m in to_input_ir(plan, physical=dataset).machines if m.id == "n")
    assert machine.footprint == CellBox(sx=3, sy=3, sz=3)  # the Vacuum Freezer's real shape


def test_adapter_leaves_block_key_none_for_a_pre_25_plan(dataset: PhysicalDataset) -> None:
    machine = next(
        m
        for m in to_input_ir(_one_machine_plan("Electric Blast Furnace"), physical=dataset).machines
        if m.id == "n"
    )
    assert machine.block_key is None
    assert machine.footprint == CellBox(sx=3, sy=4, sz=3)  # still resolved, by name


# ---------------------------------------------- real footprints solve to a non-overlapping layout


def _two_machine_line(upstream: str, downstream: str) -> Plan:
    """A two-node line: ``upstream`` feeds an item to ``downstream`` (both dataset-known types)."""
    up = Recipe(
        id="r1",
        machine_type=upstream,
        eut=480.0,
        duration_ticks=100.0,
        inputs=[Resource(kind="item", id="minecraft:iron_ingot", amount=1.0)],
        outputs=[Resource(kind="item", id="minecraft:iron_block", amount=1.0)],
    )
    down = Recipe(
        id="r2",
        machine_type=downstream,
        eut=120.0,
        duration_ticks=100.0,
        inputs=[Resource(kind="item", id="minecraft:iron_block", amount=1.0)],
        outputs=[Resource(kind="item", id="minecraft:cooled_block", amount=1.0)],
    )
    nodes = [
        Node(id="up", recipe_id="r1", overclock_tier="MV"),
        Node(id="down", recipe_id="r2", overclock_tier="MV"),
    ]
    edge = Edge(
        id="e", source="up", target="down", resource_kind="item", resource_id="minecraft:iron_block"
    )
    return Plan(schema_version=1, recipes=[up, down], nodes=nodes, edges=[edge])


def test_real_footprint_region_fits_the_tallest_multiblock(dataset: PhysicalDataset) -> None:
    # The EBF is 3x3x4 (4 tall); the region the adapter sizes must clear it (the old hardcoded
    # height of 4 gave zero routing headroom, and any taller multiblock was infeasible outright).
    ir = to_input_ir(
        _two_machine_line("Electric Blast Furnace", "Vacuum Freezer"), physical=dataset
    )
    tallest = max(m.footprint.sy for m in ir.machines)
    assert ir.bounding_region.sy > tallest  # headroom above the tallest machine


def test_real_footprint_layout_places_without_overlap(dataset: PhysicalDataset) -> None:
    # The point of GAP A: with real multi-cell footprints the solver must still produce a VALID,
    # non-overlapping layout (the validator's MACHINE_OVERLAP gate), not certify machines packed
    # into a shared 1x1x1 reservation. Exercises the real dataset path end to end.
    ir = to_input_ir(
        _two_machine_line("Electric Blast Furnace", "Vacuum Freezer"), physical=dataset
    )
    ebf = next(m for m in ir.machines if m.id == "up")
    assert ebf.footprint.volume > 1  # a real multi-cell reservation, not the crude 1x1x1 default

    layout = solve(ir, seed=0)
    assert layout.status is LayoutStatus.VALID  # fully routed, not a region-too-small infeasibility

    report = validate(ir, layout)
    assert report.ok  # the independent gate agrees: no overlap / out-of-bounds / bad orientation
    codes = {v.code for v in report.violations}
    assert codes.isdisjoint(PLACEMENT_CODES)
