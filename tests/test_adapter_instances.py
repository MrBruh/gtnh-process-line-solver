"""Tests for a node that stands for several parallel machines (`machineCount > 1`, issue #76).

The whole change turned out to need **no IR concept**: `Net.endpoints` is already an unbounded list,
so N producers and M consumers share one net, the router chains them and the validator permits
several producers. What the adapter has to get right instead is which figures are *per machine* and
which are *per group*, because the two were indistinguishable while `machineCount` was forced to 1:

- `Port.rate` and `Machine.eut` are **per machine** - a port belongs to one physical block, and the
  power synthesis gives each machine its own hatches and sums them.
- `Net.throughput` is **per group** - one shared bus carries what all N machines move.

A node with one machine keeps its bare id, so nothing that existed before this moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    Edge,
    Node,
    Plan,
    Recipe,
    ResolvedBlock,
    ResolvedMachine,
    Resource,
    Storage,
    load_plan,
    to_input_ir,
)
from gtnh_solver.adapter.core import _instance_ids
from gtnh_solver.ir import Commodity, InputIR, Net

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_PARALLEL_SAND = _EXAMPLES / "gtnh-parallel-sand.json"
_EV_NITROBENZENE = _EXAMPLES / "ev-nitrobenzene.json"


def _plan(count: int, *, resolved_eut: float | None = None, with_sink: bool = True) -> Plan:
    """One node of ``count`` machines, fed by a storage and (optionally) draining to another."""
    edges = [Edge(id="in", source="feed", target="n", resource_kind="item", resource_id="ore")]
    storages = [Storage(id="feed", kind="item")]
    if with_sink:
        edges.append(
            Edge(id="out", source="n", target="drain", resource_kind="item", resource_id="dust")
        )
        storages.append(Storage(id="drain", kind="item"))
    resolved = (
        ResolvedBlock(machines=[ResolvedMachine(node_id="n", total_eut=resolved_eut)])
        if resolved_eut is not None
        else None
    )
    return Plan(
        schema_version=2 if resolved is not None else 1,
        resolved=resolved,
        recipes=[
            Recipe(
                id="r",
                machine_type="Macerator",
                eut=30.0,
                duration_ticks=20.0,
                inputs=[Resource(kind="item", id="ore", amount=1.0)],
                outputs=[Resource(kind="item", id="dust", amount=2.0)],
            )
        ],
        nodes=[Node(id="n", recipe_id="r", overclock_tier="LV", machine_count=count)],
        storages=storages,
        edges=edges,
    )


def _machines(ir: InputIR, machine_type: str) -> list[str]:
    return [m.id for m in ir.machines if m.type == machine_type]


def _net(ir: InputIR, net_id: str) -> Net:
    return next(n for n in ir.nets if n.id == net_id)


# ------------------------------------------------------------------ instance ids


def test_a_single_machine_node_keeps_its_bare_id() -> None:
    # Load-bearing: every layout, golden file and preview that existed before parallel nodes must be
    # byte-identical, and they are only if N=1 does not start suffixing.
    node = Node(id="n", recipe_id="r", overclock_tier="LV", machine_count=1)
    assert _instance_ids(node) == ["n"]


def test_several_machines_are_suffixed_from_one() -> None:
    node = Node(id="n", recipe_id="r", overclock_tier="LV", machine_count=3)
    assert _instance_ids(node) == ["n#1", "n#2", "n#3"]


@pytest.mark.parametrize("count", [1, 2, 5])
def test_the_mapping_emits_exactly_that_many_machines(count: int) -> None:
    assert len(_machines(to_input_ir(_plan(count)), "Macerator")) == count


# ------------------------------------------------------------------ per machine vs per group


def test_every_instance_carries_the_same_per_machine_draw() -> None:
    ir = to_input_ir(_plan(3))
    draws = {m.eut for m in ir.machines if m.type == "Macerator"}
    assert draws == {30.0}  # not 90: the synthesis sums these over the shared net


def test_a_port_rate_stays_per_machine() -> None:
    ir = to_input_ir(_plan(3))
    machine = next(m for m in ir.machines if m.type == "Macerator")
    out = next(p for p in machine.faces.ports if p.id == "output:dust")
    assert out.rate == pytest.approx(0.1)  # 2 per 20 ticks, one machine


def test_a_net_throughput_is_the_whole_group() -> None:
    # The pipe carries what all three machines move. Sizing it from one would under-provision it by
    # the machine count.
    ir = to_input_ir(_plan(3))
    assert _net(ir, "out").throughput == pytest.approx(0.3)
    assert _net(ir, "in").throughput == pytest.approx(0.15)


def test_the_power_net_sums_the_group() -> None:
    ir = to_input_ir(_plan(3))
    power = next(n for n in ir.nets if n.commodity is Commodity.POWER)
    assert power.throughput == pytest.approx(90.0)


def test_a_resolved_group_total_is_divided_back_to_one_machine() -> None:
    # resolved.totalEut is eutPerMachine x machineCount x parallel. Handing that to each of N
    # machines would count the group N times over.
    ir = to_input_ir(_plan(3, resolved_eut=90.0))
    assert {m.eut for m in ir.machines if m.type == "Macerator"} == {30.0}
    power = next(n for n in ir.nets if n.commodity is Commodity.POWER)
    assert power.throughput == pytest.approx(90.0)


# ------------------------------------------------------------------ shared nets


def test_one_net_reaches_every_instance_on_both_sides() -> None:
    ir = to_input_ir(_plan(3))
    feed = _net(ir, "in")
    assert len(feed.endpoints) == 4  # the storage plus three machines
    assert {e.machine_id for e in feed.endpoints} == {"feed", "n#1", "n#2", "n#3"}


def test_a_storage_endpoint_is_not_expanded() -> None:
    # A storage is one block; only a node stands for several machines.
    ir = to_input_ir(_plan(2))
    assert [e.machine_id for e in _net(ir, "in").endpoints].count("feed") == 1


def test_an_unconsumed_output_collects_into_one_buffer_for_the_whole_group() -> None:
    # Not one chest per machine: a real line runs all three into a single chest on a shared bus.
    ir = to_input_ir(_plan(3, with_sink=False))
    buffers = [m.id for m in ir.machines if m.id.startswith("output-buffer:")]
    assert buffers == ["output-buffer:n:dust"]
    net = _net(ir, "output-net:n:dust")
    assert {e.machine_id for e in net.endpoints} == {"n#1", "n#2", "n#3", "output-buffer:n:dust"}
    assert net.throughput == pytest.approx(0.3)  # the three instances' rates summed


# ------------------------------------------------------------------ the real fixture


def test_the_committed_parallel_fixture_expands_and_wires() -> None:
    """``gtnh-parallel-sand.json`` is the acceptance case #76 was filed for: 3 nodes at count 3.

    It asserts the adapter's half only; that the line now lays out is
    ``test_solver.test_solve_the_parallel_line_reaches_a_valid_layout``.
    """
    plan = load_plan(_PARALLEL_SAND)
    assert {node.machine_count for node in plan.nodes} == {3}
    ir = to_input_ir(plan)
    forge_hammers = _machines(ir, "Forge Hammer")
    assert len(forge_hammers) == 9
    assert all("#" in mid for mid in forge_hammers)
    # Every machine is wired: nine instances all appear on some material net.
    material = {
        e.machine_id for n in ir.nets if n.commodity is not Commodity.POWER for e in n.endpoints
    }
    assert set(forge_hammers) <= material


def test_the_real_2_9_line_maps_every_machine_and_net() -> None:
    """``ev-nitrobenzene.json`` is the one real GTNH 2.9 line committed (#204): nine nodes, all
    multiblocks, four of them at ``machineCount`` 2 or 3, and a Dangote Distillus at 12x parallel.

    The adapter's half only, with no dataset (so every multiblock is 1x1x1 here) and no solve, which
    keeps the default suite fast. 34 machines is the 14 process machines the counts expand
    to, the plan's 17 storages and one power source per tier (EV, HV, MV); 27 nets is 21 fluid,
    3 item and 3 power. That is the plan's 28 edges less four: four pairs of edges meet at a
    multiblock port, and each pair is one net (#213, ``test_adapter_shared_ports``). The 12x
    parallel is reported, not modelled, so it has to warn.
    """
    plan = load_plan(_EV_NITROBENZENE)
    with pytest.warns(AdapterWarning, match=r"Dangote Distillus at 12x parallel"):
        ir = to_input_ir(plan)
    assert len(ir.machines) == 34
    assert len(ir.nets) == 27
