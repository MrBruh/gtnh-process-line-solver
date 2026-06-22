"""Tests for the gtnh-factory-flow export adapter.

Two layers: integration against the committed real fixtures (the whole Phase 1 slice so far -
export -> InputIR -> placement -> validator), and synthetic unit cases for the mapping branches
(throughput sources, storage sinks, and the fail-loud paths).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterError,
    Edge,
    Node,
    Plan,
    Recipe,
    Resource,
    Storage,
    adapt_file,
    load_plan,
    to_input_ir,
)
from gtnh_solver.ir import Commodity, LayoutResult, LayoutStatus
from gtnh_solver.placement import place
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"

_PLACEMENT_CODES = {
    ViolationCode.MACHINE_OVERLAP,
    ViolationCode.MACHINE_OUT_OF_BOUNDS,
    ViolationCode.MACHINE_ON_RESERVED,
    ViolationCode.BAD_ORIENTATION,
    ViolationCode.PLACEMENT_COUNT_MISMATCH,
    ViolationCode.UNKNOWN_MACHINE,
}


def _resource(kind: str, rid: str, amount: float = 1.0) -> Resource:
    return Resource(kind=kind, id=rid, amount=amount)


# ----------------------------------------------------------------- real fixtures


def test_load_plan_parses_sand() -> None:
    plan = load_plan(_SAND)
    assert plan.schema_version == 1
    assert len(plan.nodes) == 3
    assert len(plan.edges) == 3
    assert any(r.machine_type == "Forge Hammer" for r in plan.recipes)


def test_adapt_sand_to_input_ir() -> None:
    ir = adapt_file(_SAND)
    assert len(ir.machines) == 4  # 3 Forge Hammers + 1 Stone storage source
    assert len(ir.nets) == 3
    assert all(n.commodity is Commodity.ITEM for n in ir.nets)
    types = {m.type for m in ir.machines}
    assert "Forge Hammer" in types
    assert "Super Chest" in types  # the item storage source (covers ride on it, not the pipe)


def test_adapt_sand_end_to_end_places_and_validates() -> None:
    # The Phase 1 slice so far: real export -> InputIR -> placement -> validator certifies.
    ir = adapt_file(_SAND)
    result = place(ir)
    assert result.ok
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=list(result.placements))
    assert _PLACEMENT_CODES.isdisjoint(validate(ir, layout).codes())


def test_throughput_is_positive_for_sand_material_nets() -> None:
    ir = adapt_file(_SAND)
    assert all(n.throughput > 0 for n in ir.nets)


def test_adapt_nitrobenzene_has_fluids_and_places() -> None:
    ir = adapt_file(_NITROBENZENE)
    assert len(ir.machines) == 18  # 7 nodes + 11 storages
    assert any(n.commodity is Commodity.FLUID for n in ir.nets)
    assert any(n.commodity is Commodity.ITEM for n in ir.nets)
    assert "Super Tank" in {m.type for m in ir.machines}  # fluid storages
    assert place(ir).ok


# ----------------------------------------------------------------- synthetic mapping


def test_unknown_recipe_raises() -> None:
    plan = Plan(schema_version=1, nodes=[Node(id="n", recipe_id="missing", overclock_tier="LV")])
    with pytest.raises(AdapterError):
        to_input_ir(plan)


def test_unsupported_resource_kind_raises() -> None:
    plan = Plan(
        schema_version=1,
        recipes=[Recipe(id="r", machine_type="M", outputs=[_resource("energy", "x")])],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
    )
    with pytest.raises(AdapterError):
        to_input_ir(plan)


def test_storage_sink_routes_with_throughput_from_producer() -> None:
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r", machine_type="M", duration_ticks=4.0, outputs=[_resource("item", "R", 2.0)]
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
        storages=[Storage(id="s", kind="item", resource_id="R")],
        edges=[Edge(id="e", source="n", target="s", resource_kind="item", resource_id="R")],
    )
    ir = to_input_ir(plan)
    assert len(ir.machines) == 2
    assert ir.nets[0].throughput == 0.5  # 2 amount * 1 parallel * 1 count / 4 ticks


def test_throughput_falls_back_to_consumer_demand() -> None:
    # Edge sourced from a storage (no recipe) -> rate comes from the consuming node.
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r", machine_type="M", duration_ticks=2.0, inputs=[_resource("item", "R", 3.0)]
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
        storages=[Storage(id="s", kind="item", resource_id="R")],
        edges=[Edge(id="e", source="s", target="n", resource_kind="item", resource_id="R")],
    )
    assert to_input_ir(plan).nets[0].throughput == 1.5  # 3 / 2


def test_storage_to_storage_edge_has_zero_throughput() -> None:
    plan = Plan(
        schema_version=1,
        storages=[
            Storage(id="a", kind="item", resource_id="R"),
            Storage(id="b", kind="item", resource_id="R"),
        ],
        edges=[Edge(id="e", source="a", target="b", resource_kind="item", resource_id="R")],
    )
    assert to_input_ir(plan).nets[0].throughput == 0.0


def test_zero_duration_recipe_yields_zero_rate() -> None:
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r", machine_type="M", duration_ticks=0.0, outputs=[_resource("item", "R", 5.0)]
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
        storages=[Storage(id="s", kind="item", resource_id="R")],
        edges=[Edge(id="e", source="n", target="s", resource_kind="item", resource_id="R")],
    )
    assert to_input_ir(plan).nets[0].throughput == 0.0
