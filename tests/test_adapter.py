"""Tests for the gtnh-factory-flow export adapter.

Two layers: integration against the committed real fixtures (the whole Phase 1 slice so far -
export -> InputIR -> placement -> validator), and synthetic unit cases for the mapping branches
(throughput sources, storage sinks, the v2 ``resolved`` cross-checks, and the fail-loud paths).
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterError,
    AdapterWarning,
    Edge,
    Node,
    Plan,
    Recipe,
    ResolvedBlock,
    ResolvedMachine,
    ResolvedPower,
    Resource,
    Storage,
    adapt_file,
    load_plan,
    to_input_ir,
)
from gtnh_solver.ir import Commodity, InputIR, IODirection, LayoutResult, LayoutStatus, Net
from gtnh_solver.placement import place
from gtnh_solver.validator import validate
from tests._helpers import PLACEMENT_CODES

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_SAND = _EXAMPLES / "gtnh-sand.json"
_NITROBENZENE = _EXAMPLES / "gtnh-nitrobenzene.json"
# The v2 re-export of the nitrobenzene plan. Kept a fixture, NOT the example: its resolved
# block models overclocking (e.g. the LCR draws 2880 EU/t resolved vs 480 recipe-derived), so
# adapting it changes the power numbers the example-pinned tests depend on.
_NITROBENZENE_V2 = Path(__file__).resolve().parent / "fixtures" / "gtnh-nitrobenzene-v2.json"


def _resource(kind: str, rid: str, amount: float = 1.0) -> Resource:
    return Resource(kind=kind, id=rid, amount=amount)


def _net_for_edge(ir: InputIR, edge_id: str = "e") -> Net:
    # The adapter contract is a net *per edge* (net.id == edge.id), deterministic but not pinned to
    # emit position - so select the net by its edge id, not by ``nets[0]``.
    return next(n for n in ir.nets if n.id == edge_id)


# ----------------------------------------------------------------- real fixtures


def test_load_plan_parses_sand() -> None:
    plan = load_plan(_SAND)
    assert plan.schema_version == 2
    assert len(plan.nodes) == 3
    assert len(plan.edges) == 3
    assert any(r.machine_type == "Forge Hammer" for r in plan.recipes)


def test_load_plan_parses_sand_v2_metadata_and_resolved() -> None:
    # The v2 additive fields parse typed: exporter identity, dataset pin, and the resolved
    # throughput block (power total, per-machine EU/t, per-edge rates, external I/O).
    plan = load_plan(_SAND)
    assert plan.app is not None
    assert plan.app.name == "gtnh-factory-flow"
    assert plan.dataset_version_id == "stable-2.8.4"
    assert plan.resolved is not None
    assert plan.resolved.power is not None
    assert plan.resolved.power.total_eut == 48
    assert [rm.total_eut for rm in plan.resolved.machines] == [16, 16, 16]
    assert {rn.edge_id for rn in plan.resolved.nets} == {e.id for e in plan.edges}
    assert plan.resolved.external_io is not None
    assert [f.id for f in plan.resolved.external_io.outputs] == ["minecraft:sand"]


def test_adapt_sand_to_input_ir() -> None:
    ir = adapt_file(_SAND)
    # 3 Forge Hammers + 2 Super Chests (the input source + the synthesized output buffer) + LV source
    assert len(ir.machines) == 6
    assert len(ir.nets) == 5  # 3 item edges + 1 synthesized output-collection net + 1 LV power net
    types = {m.type for m in ir.machines}
    assert "Forge Hammer" in types
    assert "Super Chest" in types  # item storages: the input source and the output buffer (#16)
    assert "Power Source (LV)" in types  # the export carries no source; the adapter invents one
    assert len([n for n in ir.nets if n.commodity is Commodity.ITEM]) == 4  # incl. the sand output
    assert len([n for n in ir.nets if n.commodity is Commodity.POWER]) == 1


def test_adapt_sand_end_to_end_places_and_validates() -> None:
    # The Phase 1 slice so far: real export -> InputIR -> placement -> validator certifies.
    ir = adapt_file(_SAND)
    result = place(ir)
    assert result.ok
    layout = LayoutResult(status=LayoutStatus.VALID, seed=0, placements=list(result.placements))
    assert PLACEMENT_CODES.isdisjoint(validate(ir, layout).codes())


def test_throughput_is_positive_for_sand_material_nets() -> None:
    ir = adapt_file(_SAND)
    assert all(n.throughput > 0 for n in ir.nets)


def test_adapt_nitrobenzene_has_fluids_and_places() -> None:
    ir = adapt_file(_NITROBENZENE)
    # 7 nodes + 11 input storages + 2 synthesized output buffers + 3 power sources (LV/MV/HV)
    assert len(ir.machines) == 23
    assert any(n.commodity is Commodity.FLUID for n in ir.nets)
    assert any(n.commodity is Commodity.ITEM for n in ir.nets)
    assert any(n.commodity is Commodity.POWER for n in ir.nets)
    assert "Super Tank" in {m.type for m in ir.machines}  # fluid storages
    assert place(ir).ok


# ----------------------------------------------------------------- synthetic mapping


def test_unknown_recipe_raises() -> None:
    plan = Plan(schema_version=1, nodes=[Node(id="n", recipe_id="missing", overclock_tier="LV")])
    with pytest.raises(AdapterError):
        to_input_ir(plan)


def test_multi_instance_node_is_rejected() -> None:
    # machineCount > 1 cannot be mapped yet: a net endpoint can't address one instance of a
    # group, so the adapter fails loud rather than emit a placed-but-unwired layout.
    plan = Plan(
        schema_version=1,
        recipes=[Recipe(id="r", machine_type="M", outputs=[_resource("item", "x")])],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV", machine_count=2)],
    )
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
    assert _net_for_edge(ir).throughput == 0.5  # 2 amount * 1 parallel * 1 count / 4 ticks


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
    assert _net_for_edge(to_input_ir(plan)).throughput == 1.5  # 3 / 2


def test_synthesizes_one_power_source_and_net_per_tier() -> None:
    # Two powered nodes on different tiers -> one source + one power net each; an unpowered
    # storage stays out of the power network.
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r1",
                machine_type="M",
                duration_ticks=10.0,
                eut=16.0,
                outputs=[_resource("item", "x")],
            ),
            Recipe(
                id="r2",
                machine_type="M",
                duration_ticks=10.0,
                eut=120.0,
                inputs=[_resource("item", "x")],
            ),
        ],
        nodes=[
            Node(id="n1", recipe_id="r1", overclock_tier="LV"),
            Node(id="n2", recipe_id="r2", overclock_tier="MV"),
        ],
        edges=[Edge(id="e", source="n1", target="n2", resource_kind="item", resource_id="x")],
    )
    ir = to_input_ir(plan)
    sources = {m.type for m in ir.machines if m.type.startswith("Power Source")}
    assert sources == {"Power Source (LV)", "Power Source (MV)"}  # one per tier in use
    power_nets = [n for n in ir.nets if n.commodity is Commodity.POWER]
    assert len(power_nets) == 2
    n1 = next(m for m in ir.machines if m.id == "n1")
    assert any(
        p.commodity is Commodity.POWER and p.direction is IODirection.INPUT for p in n1.faces.ports
    )  # the powered machine gained a power input port


def test_recipe_output_ports_carry_throughput() -> None:
    # a machine output port records the rate it produces, so a dangling boundary output (no net)
    # still has a reportable throughput (#16). The sand line's Forge Hammers output 0.1 items/t.
    ir = adapt_file(_SAND)
    hammer = next(m for m in ir.machines if m.type == "Forge Hammer")
    out = next(
        p
        for p in hammer.faces.ports
        if p.direction is IODirection.OUTPUT and p.commodity is Commodity.ITEM
    )
    assert out.rate == pytest.approx(0.1)


def test_parallel_scales_eut_so_power_amperage_is_sized_for_it() -> None:
    # A node running 4 recipes in parallel draws 4x the recipe's EU/t. The synthesized power net
    # must size amperage from the scaled draw, not the single-recipe eut (otherwise the cable is
    # under-sized for parallel > 1). The powered machine carries eut = recipe.eut * parallel.
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r",
                machine_type="M",
                duration_ticks=10.0,
                eut=30.0,
                outputs=[_resource("item", "x")],
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV", parallel=4)],
    )
    ir = to_input_ir(plan)
    powered = next(m for m in ir.machines if m.id == "n")
    assert powered.eut == 120.0  # 30 EU/t * 4 parallel


def test_unpowered_plan_synthesizes_no_power() -> None:
    # eut defaults to 0 (no eut in the recipe) -> nothing draws power -> no source, no power net.
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(id="r", machine_type="M", duration_ticks=4.0, outputs=[_resource("item", "x")])
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
        storages=[Storage(id="s", kind="item", resource_id="x")],
        edges=[Edge(id="e", source="n", target="s", resource_kind="item", resource_id="x")],
    )
    ir = to_input_ir(plan)
    assert not any(n.commodity is Commodity.POWER for n in ir.nets)
    assert not any(m.type.startswith("Power Source") for m in ir.machines)


def test_storage_to_storage_edge_has_zero_throughput() -> None:
    plan = Plan(
        schema_version=1,
        storages=[
            Storage(id="a", kind="item", resource_id="R"),
            Storage(id="b", kind="item", resource_id="R"),
        ],
        edges=[Edge(id="e", source="a", target="b", resource_kind="item", resource_id="R")],
    )
    assert _net_for_edge(to_input_ir(plan)).throughput == 0.0


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
    assert _net_for_edge(to_input_ir(plan)).throughput == 0.0


# ----------------------------------------------------------------- v2 resolved block (#2)


def _v2_plan_with_resolved(resolved: ResolvedBlock) -> Plan:
    """A one-node v2 plan (recipe draws 30 EU/t at LV) carrying the given resolved block."""
    return Plan(
        schema_version=2,
        recipes=[
            Recipe(
                id="r",
                machine_type="M",
                duration_ticks=10.0,
                eut=30.0,
                outputs=[_resource("item", "x")],
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV")],
        resolved=resolved,
    )


def test_v2_export_round_trips_resolved_power_against_synthesis() -> None:
    # Round-trip (#2): the real v2 sand export parses, its resolved figures agree with the
    # recipe-derived synthesis (no AdapterWarning fires), and the synthesized per-tier power
    # nets sum to resolved.power.totalEut.
    plan = load_plan(_SAND)
    assert plan.resolved is not None
    assert plan.resolved.power is not None
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ir = to_input_ir(plan)
    power_total = sum(n.throughput for n in ir.nets if n.commodity is Commodity.POWER)
    assert power_total == pytest.approx(plan.resolved.power.total_eut)  # 48 EU/t on the LV net


def test_v1_plan_without_resolved_adapts_identically() -> None:
    # Backward compat: stripping the v2 additive fields from the sand export leaves a v1 plan
    # whose adapted IR is identical - the resolved block only overrides what the synthesis
    # already computes, and on sand the two agree exactly.
    data = json.loads(_SAND.read_text(encoding="utf-8"))
    v2_ir = to_input_ir(Plan.model_validate(data))
    for key in ("app", "datasetVersionId", "resolved"):
        data.pop(key)
    data["schemaVersion"] = 1
    v1_plan = Plan.model_validate(data)
    assert v1_plan.app is None
    assert v1_plan.dataset_version_id is None
    assert v1_plan.resolved is None
    assert to_input_ir(v1_plan) == v2_ir


def test_nitrobenzene_v2_fixture_resolved_wins_over_synthesis() -> None:
    # The real v2 re-export of the nitrobenzene plan: two nodes' resolved EU/t diverge from
    # recipe.eut * parallel because the exporter models overclocking (the LCR node draws
    # 2880 EU/t resolved vs 480 synthesized). Adapting it warns per divergent node and carries
    # the resolved figures into the machines and the per-tier power nets.
    plan = load_plan(_NITROBENZENE_V2)
    assert plan.schema_version == 2
    assert plan.resolved is not None
    assert plan.resolved.power is not None
    with pytest.warns(AdapterWarning, match="resolved EU/t"):
        ir = to_input_ir(plan)
    resolved_eut = {rm.node_id: rm.total_eut for rm in plan.resolved.machines}
    machine_eut = {m.id: m.eut for m in ir.machines if m.id in resolved_eut}
    assert machine_eut == pytest.approx(resolved_eut)
    assert max(resolved_eut.values()) == 2880.0  # the overclocked LCR, not the recipe's 480
    power_total = sum(n.throughput for n in ir.nets if n.commodity is Commodity.POWER)
    assert power_total == pytest.approx(plan.resolved.power.total_eut)


def test_mismatching_resolved_eut_warns_and_resolved_wins() -> None:
    # The exporter's balancer models overclocking, so its EU/t may exceed recipe.eut *
    # parallel. The adapter trusts it (the cable must be sized for the real draw) but flags
    # the divergence instead of hiding it.
    plan = _v2_plan_with_resolved(
        ResolvedBlock(
            machines=[ResolvedMachine(node_id="n", eut_per_machine=120.0, total_eut=120.0)]
        )
    )
    with pytest.warns(AdapterWarning, match="node 'n'"):
        ir = to_input_ir(plan)
    assert next(m for m in ir.machines if m.id == "n").eut == 120.0
    power_net = next(n for n in ir.nets if n.commodity is Commodity.POWER)
    assert power_net.throughput == 120.0  # amperage is sized from the resolved draw


def test_inconsistent_resolved_power_total_warns() -> None:
    # resolved.power.totalEut disagreeing with the sum of the synthesized power nets means the
    # export contradicts itself; the adapter warns and keeps the per-net figures.
    plan = _v2_plan_with_resolved(
        ResolvedBlock(
            machines=[ResolvedMachine(node_id="n", eut_per_machine=30.0, total_eut=30.0)],
            power=ResolvedPower(total_eut=999.0),
        )
    )
    with pytest.warns(AdapterWarning, match="power total"):
        ir = to_input_ir(plan)
    power_net = next(n for n in ir.nets if n.commodity is Commodity.POWER)
    assert power_net.throughput == 30.0


def test_resolved_without_the_node_falls_back_to_synthesis() -> None:
    # A resolved block that does not cover a node is not a mismatch: that node's EU/t comes
    # from the recipe synthesis, silently (a partial resolved block stays usable).
    plan = _v2_plan_with_resolved(
        ResolvedBlock(machines=[ResolvedMachine(node_id="other", total_eut=1.0)])
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ir = to_input_ir(plan)
    assert next(m for m in ir.machines if m.id == "n").eut == 30.0
