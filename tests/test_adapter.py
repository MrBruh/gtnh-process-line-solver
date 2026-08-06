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
from gtnh_solver.adapter.core import _bounding_region, _orientations_for
from gtnh_solver.dataset import DatasetMeta, MachinePhysical, PhysicalDataset
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Net,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED
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
    # 7 nodes + 11 input storages + 2 synthesized output buffers + 4 power sources. LV and HV take
    # one source each; MV needs two, because its Coke Oven alone draws more amps than a cable
    # carries and so cannot share a run with the Distillation Tower.
    assert len(ir.machines) == 24
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
        storages=[Storage(id="s", kind="item")],
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
        storages=[Storage(id="s", kind="item")],
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


def _powered_plan(*euts: float, tier: str = "LV") -> Plan:
    """Independent powered nodes, one per ``eut``, all on ``tier`` (no edges between them)."""
    return Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id=f"r{i}",
                machine_type="M",
                duration_ticks=10.0,
                eut=eut,
                outputs=[_resource("item", f"x{i}")],
            )
            for i, eut in enumerate(euts)
        ],
        nodes=[Node(id=f"n{i}", recipe_id=f"r{i}", overclock_tier=tier) for i in range(len(euts))],
    )


def _power_nets(ir: InputIR) -> list[Net]:
    return [n for n in ir.nets if n.commodity is Commodity.POWER]


def _source_ids(ir: InputIR) -> set[str]:
    return {m.id for m in ir.machines if m.type.startswith("Power Source")}


def test_tier_within_the_cable_cap_keeps_one_unsuffixed_source() -> None:
    # 136 EU/t at LV (32 V) is 4.25 A, well under the 16x cap, so the tier stays on one run and
    # keeps the plain ids the build guide and previewer have always shown.
    ir = to_input_ir(_powered_plan(16.0, 120.0))
    assert {n.id for n in _power_nets(ir)} == {"power:LV"}
    assert _source_ids(ir) == {"power-source:LV"}


def test_tier_over_the_cable_cap_splits_into_several_sources() -> None:
    # A shared-amperage trunk sums its machines and a cable tops out at 16x, so three 200 EU/t LV
    # machines (18.75 A together) cannot share one run. The synthesis splits the tier, and each
    # group's own trunk fits: 16 A x 32 V is 512 EU/t.
    ir = to_input_ir(_powered_plan(200.0, 200.0, 200.0))
    nets = _power_nets(ir)
    assert {n.id for n in nets} == {"power:LV#1", "power:LV#2"}
    assert _source_ids(ir) == {"power-source:LV#1", "power-source:LV#2"}
    assert all(n.throughput <= 512 for n in nets)
    # the split is a partition: every powered machine hangs off exactly one source
    wired = [e.machine_id for n in nets for e in n.endpoints if e.port_id == "power:in"]
    assert sorted(wired) == ["n0", "n1", "n2"]


def test_machine_over_the_cap_alone_gets_its_own_source() -> None:
    # 600 EU/t at LV is 18.75 A, past what any cable carries. No partition can fix one machine's
    # own feed, so it lands in a group by itself and the router reports the over-cap run. The
    # synthesis must not fold it in with a neighbour and bury the problem in a shared trunk.
    ir = to_input_ir(_powered_plan(600.0, 16.0))
    nets = {n.id: n for n in _power_nets(ir)}
    assert set(nets) == {"power:LV#1", "power:LV#2"}
    heavy = next(n for n in nets.values() if n.throughput == 600.0)
    assert [e.machine_id for e in heavy.endpoints if e.port_id == "power:in"] == ["n0"]


# --------------------------------------------------- energy hatches (multi-connection power)


def _hatched_dataset(hatch_cells: int = 20, key: str = "M") -> PhysicalDataset:
    """A dataset whose one machine is a multiblock with ``hatch_cells`` interchangeable cells.

    A GT casing cell accepts a hatch of any kind, so ``energy_hatch_cells`` matches; one
    maintenance hatch is reserved. This is what tells the synthesis the machine HAS hatches - with
    no record it keeps a single connection.
    """
    return PhysicalDataset(
        meta=DatasetMeta.model_validate(
            {
                "schema": 2,
                "pack_version": "test",
                "generated_at": "2026-01-01T00:00:00Z",
                "extractor_sha": "0" * 40,
                "controller_count": 1,
            }
        ),
        machines={
            key: MachinePhysical(
                key=key,
                registry_name="test:block",
                meta=0,
                source_class="test.Controller",
                footprint=CellBox(sx=3, sy=3, sz=3),
                io_faces=frozenset({Facing.NORTH}),
                hint_layers=frozenset({0}),
                coil_layer_count=0,
                variant_count=1,
                hatch_cells=hatch_cells,
                energy_hatch_cells=hatch_cells,
                upkeep_hatch_count=1,
            )
        },
    )


def _hatches(ir: InputIR, machine_id: str) -> list[str]:
    machine = next(m for m in ir.machines if m.id == machine_id)
    return [p.id for p in machine.power_input_ports]


def test_a_machine_with_no_structural_record_keeps_one_connection() -> None:
    # Without a dataset there is no evidence the machine even has hatches (a single-block machine
    # has none), so splitting its intake would invent structure the layout cannot justify. It
    # keeps the plain port id every consumer has always seen, and no rate.
    ir = to_input_ir(_powered_plan(60.0))
    assert _hatches(ir, "n0") == ["power:in"]
    port = next(p for p in ir.machines[0].faces.ports if p.id == "power:in")
    assert port.rate is None
    assert port.max_amps is None


def test_a_draw_past_one_hatch_is_split_across_several() -> None:
    # A GT energy hatch accepts 2 A, so 60 EU/t at LV (3.75 A at the designed run length) needs
    # two of them. Each carries its own share of the draw, and the shares must add back up to the
    # machine's eut or part of it would go unsized.
    ir = to_input_ir(_powered_plan(60.0), physical=_hatched_dataset())
    assert _hatches(ir, "n0") == ["power:in#1", "power:in#2"]
    machine = ir.machines[0]
    assert [p.rate for p in machine.power_input_ports] == [30.0, 30.0]
    assert all(p.max_amps == 2.0 for p in machine.power_input_ports)


def test_hatches_of_one_machine_spread_across_several_cable_runs() -> None:
    # The partitioner works in hatches, not machines: six 96 EU/t LV machines draw 18 A together,
    # past the 16x cap, so the tier splits - and because the unit is a hatch, the split can cut
    # through a machine rather than having to keep it whole.
    ir = to_input_ir(_powered_plan(*([96.0] * 6)), physical=_hatched_dataset())
    nets = _power_nets(ir)
    assert {n.id for n in nets} == {"power:LV#1", "power:LV#2"}
    # every hatch of every machine is wired exactly once, across the two runs
    wired = sorted(
        (e.machine_id, e.port_id) for n in nets for e in n.endpoints if "power:in" in e.port_id
    )
    assert len(wired) == 18  # 6 machines x 3 hatches
    assert len(set(wired)) == 18


def test_a_draw_needing_too_many_hatches_is_supplied_at_a_higher_tier() -> None:
    # TEMPORARY workaround (adapter.power._supply_tier): 200 EU/t at LV would want seven hatches,
    # which is a symptom of an upstream tier error rather than a real build. The layout supplies
    # it at MV instead, where one hatch carries it - what a player would do.
    ir = to_input_ir(_powered_plan(200.0), physical=_hatched_dataset())
    machine = ir.machines[0]
    assert machine.voltage_tier == "MV"
    assert _hatches(ir, "n0") == ["power:in"]
    assert {n.id for n in _power_nets(ir)} == {"power:MV"}


def test_the_tier_upgrade_does_not_touch_the_recipes_draw() -> None:
    # It changes only the voltage the layout supplies. A real tier change also re-overclocks,
    # moving both eut and the parallel count, which only the exporter can do - so eut is left
    # exactly as the export stated it, and the workaround stays honest about what it did.
    ir = to_input_ir(_powered_plan(200.0), physical=_hatched_dataset())
    assert ir.machines[0].eut == 200.0


def test_a_machine_too_small_to_host_its_hatches_is_left_for_the_validator() -> None:
    # Allocation is not capped by the free cells: giving it fewer hatches than its draw needs
    # would under-size the feed and certify a machine that cannot draw its own load. The excess is
    # reported against hatch_cells by the validator instead.
    ir = to_input_ir(_powered_plan(60.0), physical=_hatched_dataset(hatch_cells=1))
    machine = ir.machines[0]
    assert len(machine.power_input_ports) == 2
    assert machine.hatch_cells == 1


def test_a_node_named_like_a_synthetic_source_is_rejected() -> None:
    # The synthesis invents source machines with ids of its own; an export node already holding
    # one would silently collide and give the net two machines under one id. Fail loud instead.
    plan = Plan(
        schema_version=1,
        recipes=[Recipe(id="r", machine_type="M", eut=16.0, outputs=[_resource("item", "x")])],
        nodes=[Node(id="power-source:LV", recipe_id="r", overclock_tier="LV")],
    )
    with pytest.raises(AdapterError, match="collides"):
        to_input_ir(plan)


def test_an_off_ladder_tier_keeps_one_connection_and_its_own_tier() -> None:
    # A tier that is not on the ladder has no voltage to size hatches or an upgrade against. The
    # synthesis neither splits nor re-tiers it: the unknown tier is a violation the validator
    # reports, and guessing here would turn a reported problem into a silently different layout.
    ir = to_input_ir(_powered_plan(600.0, tier="ZZZ"), physical=_hatched_dataset())
    machine = ir.machines[0]
    assert machine.voltage_tier == "ZZZ"
    assert _hatches(ir, "n0") == ["power:in"]


def test_a_draw_no_tier_can_carry_keeps_its_own_tier() -> None:
    # The upgrade walks the ladder and stops at the first tier needing few enough hatches. A draw
    # so large that even MAX does not qualify exhausts the ladder, and the machine keeps the tier
    # the export gave it rather than being silently promoted to MAX for no benefit.
    ir = to_input_ir(_powered_plan(1e13, tier="UXV"), physical=_hatched_dataset())
    assert ir.machines[0].voltage_tier == "UXV"


def test_unknown_tier_keeps_one_net_for_the_validator_to_report() -> None:
    # An off-ladder tier has no voltage on the ladder, so the synthesis cannot size amps for it.
    # It keeps the single-net shape rather than raising: the unknown tier is a violation the
    # validator reports, and moving it earlier would turn a reported problem into a hard failure.
    ir = to_input_ir(_powered_plan(600.0, 600.0, tier="ZZZ"))
    assert {n.id for n in _power_nets(ir)} == {"power:ZZZ"}
    assert _source_ids(ir) == {"power-source:ZZZ"}


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
        storages=[Storage(id="s", kind="item")],
        edges=[Edge(id="e", source="n", target="s", resource_kind="item", resource_id="x")],
    )
    ir = to_input_ir(plan)
    assert not any(n.commodity is Commodity.POWER for n in ir.nets)
    assert not any(m.type.startswith("Power Source") for m in ir.machines)


def test_storage_to_storage_edge_has_zero_throughput() -> None:
    plan = Plan(
        schema_version=1,
        storages=[
            Storage(id="a", kind="item"),
            Storage(id="b", kind="item"),
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
        storages=[Storage(id="s", kind="item")],
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


# ----------------------------------------------- footprint-aware region + orientation constraint


def test_bounding_region_matches_the_old_sizing_for_unit_machines() -> None:
    # For an all-1x1x1 line the footprint-aware sizing must reproduce the historical side x 4 x side
    # region exactly, so the shipped single-block examples are byte-for-byte unchanged.
    for n in (1, 3, 5, 12):
        region = _bounding_region([CellBox() for _ in range(n)])
        side = max(8, n * 2)
        assert region == CellBox(sx=side, sy=4, sz=side)


def test_bounding_region_clears_the_tallest_machine() -> None:
    # A tall multiblock (a Distillation Tower is 10+ high) must fit: a hardcoded height of 4 would
    # make placement infeasible before it even runs. Height clears the tallest footprint.
    region = _bounding_region([CellBox(sx=3, sy=12, sz=3), CellBox()])
    assert region.sy > 12


def test_bounding_region_fits_the_summed_footprint_area() -> None:
    # A few large-based machines whose summed floor area exceeds the count-based generosity: the
    # floor must still hold them (side^2 >= summed footprint area), with routing slack on top.
    footprints = [CellBox(sx=5, sy=1, sz=5) for _ in range(4)]  # 100 cells of floor area
    region = _bounding_region(footprints)
    assert region.sx * region.sz >= sum(fp.sx * fp.sz for fp in footprints)
    assert region.sx >= max(fp.sx for fp in footprints)  # never narrower than one machine


def test_bounding_region_empty_is_a_defensive_default() -> None:
    # Synthesis always adds at least a power source in practice, but the sizing must not divide by
    # an empty list; it returns the historical minimum region instead.
    assert _bounding_region([]) == CellBox(sx=8, sy=4, sz=8)


def test_orientations_square_base_keeps_all_four() -> None:
    # A square-base footprint (sx == sz) is rotation-invariant about the vertical axis, so all four
    # horizontal facings are safe - occupied_cells expands the same box for each. The EBF (3x4x3)
    # and every 1x1x1 block are square-base.
    assert _orientations_for(CellBox(sx=3, sy=4, sz=3)) == list(HORIZONTAL_FACINGS_ORDERED)
    assert _orientations_for(CellBox()) == list(HORIZONTAL_FACINGS_ORDERED)


def test_orientations_non_square_base_is_pinned_to_one() -> None:
    # A non-square base (sx != sz) would swap extents under a 90-degree turn, which occupied_cells
    # does not yet model, so it is pinned to a single default orientation until rotation lands.
    for footprint in (CellBox(sx=2, sy=1, sz=5), CellBox(sx=7, sy=3, sz=2)):
        assert _orientations_for(footprint) == [HORIZONTAL_FACINGS_ORDERED[0]]
