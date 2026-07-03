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
from pathlib import Path

import pytest

from gtnh_solver.adapter import Node, Plan, Recipe, Resource, to_input_ir
from gtnh_solver.dataset import (
    DatasetError,
    MultiblockDoc,
    PhysicalDataset,
    load_physical_dataset,
    to_physical,
)
from gtnh_solver.ir import CellBox, Facing

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


def _one_machine_plan(machine_type: str) -> Plan:
    recipe = Recipe(
        id="r",
        machine_type=machine_type,
        eut=480.0,
        duration_ticks=100.0,
        inputs=[Resource(kind="item", id="minecraft:iron_ingot", amount=1.0)],
        outputs=[Resource(kind="item", id="minecraft:iron_block", amount=1.0)],
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
