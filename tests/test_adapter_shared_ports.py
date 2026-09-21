"""Tests for merging the nets that meet at one multiblock port (#213).

A plan draws one edge per consumer, so a multiblock output feeding two machines is two edges out of
one port. Mapped one net per edge, that port docked two terminals, and the router put its hatch
behind the first while the validator checked it against the last: ``hatch_terminal_mismatch`` on
every attempt, with every net routed. In game one port is one hatch feeding one pipe network, so
the adapter now maps those edges to one net.

What has to hold:

- the union is transitive, so a port fed by two producers and another feeding two consumers join
  when an edge links them;
- only a multiblock's port merges. A storage has no hatch, and a single block has none either, so
  their shared ports keep one net per edge and the lines that share only those do not move;
- a merged net carries what its producers move, each once. Every edge off one output is rated at
  that output's full rate, so summing the edges would double it;
- a port has one resource, so edges disagreeing on it are refused.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterError,
    Edge,
    MachineHandler,
    Node,
    Plan,
    Recipe,
    Resource,
    Storage,
    load_plan,
    to_input_ir,
)
from gtnh_solver.adapter.core import _is_multiblock
from gtnh_solver.dataset import load_physical_dataset
from gtnh_solver.ir import Commodity, InputIR, LayoutStatus, Net
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import hatched_dataset

_ROOT = Path(__file__).resolve().parents[1]
_EV_NITROBENZENE = _ROOT / "examples" / "ev-nitrobenzene.json"
#: The committed two-controller sample, passed explicitly: the regression below turns on the
#: Vacuum Freezer's real casing cells, which a local ``data/<version>/`` dump must not change.
_FIXTURE_DATASET = _ROOT / "data" / "multiblocks"

_MULTIBLOCK = "multiblock"
_SINGLE = "single"

# A handler-declared multiblock with no runtime figures is powered from its base EU/t, which the
# adapter rightly warns about (``_check_power_provenance``). These plans carry no runtime figures
# because nothing here is about power; the warning is expected noise, not a finding.
pytestmark = pytest.mark.filterwarnings("ignore:plan carries no resolved throughput block")


def _recipe(
    rid: str,
    *,
    kind: str | None,
    inputs: Sequence[tuple[str, float]] = (),
    outputs: Sequence[tuple[str, float]] = (),
    machine_type: str | None = None,
) -> Recipe:
    """A 20-tick fluid recipe, so an amount of 20 is 1 mB/t. ``kind`` is the handler's, or none."""
    return Recipe(
        id=rid,
        machine_type=machine_type or rid,
        eut=30.0,
        duration_ticks=20.0,
        inputs=[Resource(kind="fluid", id=res, amount=amount) for res, amount in inputs],
        outputs=[Resource(kind="fluid", id=res, amount=amount) for res, amount in outputs],
        machine_handlers=(
            [MachineHandler(id="h", kind=kind, label=machine_type or rid)] if kind else []
        ),
    )


def _edge(eid: str, source: str, target: str, resource: str, kind: str = "fluid") -> Edge:
    return Edge(id=eid, source=source, target=target, resource_kind=kind, resource_id=resource)


def _plan(
    recipes: list[Recipe],
    edges: list[Edge],
    *,
    counts: dict[str, int] | None = None,
    storages: Sequence[str] = (),
) -> Plan:
    """One node per recipe, named after it, at ``counts`` machines (default 1)."""
    counts = counts or {}
    return Plan(
        schema_version=1,
        recipes=recipes,
        nodes=[
            Node(id=r.id, recipe_id=r.id, overclock_tier="LV", machine_count=counts.get(r.id, 1))
            for r in recipes
        ],
        storages=[Storage(id=sid, kind="fluid") for sid in storages],
        edges=edges,
    )


def _material(ir: InputIR) -> list[Net]:
    return [n for n in ir.nets if n.commodity is not Commodity.POWER]


def _net(ir: InputIR, net_id: str) -> Net:
    return next(n for n in ir.nets if n.id == net_id)


def _refs(net: Net) -> list[tuple[str, str]]:
    return [(e.machine_id, e.port_id) for e in net.endpoints]


def _fan_out(kind: str | None = _MULTIBLOCK) -> Plan:
    """A reactor whose acid feeds a plant and an overflow tank: two edges out of one port."""
    return _plan(
        [
            _recipe("reactor", kind=kind, outputs=[("acid", 100.0), ("steam", 20.0)]),
            _recipe("plant", kind=None, inputs=[("acid", 60.0)]),
        ],
        [_edge("e1", "reactor", "plant", "acid"), _edge("e2", "reactor", "tank", "acid")],
        storages=["tank"],
    )


# ------------------------------------------------------------------ what merges


def test_two_edges_out_of_one_multiblock_port_are_one_net() -> None:
    ir = to_input_ir(_fan_out())
    net = _net(ir, "e1+e2")
    assert [n.id for n in _material(ir)] == ["e1+e2", "output-net:reactor:steam"]
    assert _refs(net) == [
        ("reactor", "output:acid"),
        ("plant", "input:acid"),
        ("tank", "input:acid"),
    ]
    assert (net.commodity, net.fluid_or_item) == (Commodity.FLUID, "acid")


def test_two_edges_into_one_multiblock_input_are_one_net() -> None:
    plan = _plan(
        [
            _recipe("oven", kind=None, outputs=[("tar", 20.0)]),
            _recipe("extractor", kind=None, outputs=[("tar", 40.0)]),
            _recipe("tower", kind=_MULTIBLOCK, inputs=[("tar", 60.0)]),
        ],
        [_edge("e1", "oven", "tower", "tar"), _edge("e2", "extractor", "tower", "tar")],
        counts={"oven": 2},
    )
    net = _net(to_input_ir(plan), "e1+e2")
    # Producers first, then consumers, each once: the tower's input is listed once, not twice.
    assert _refs(net) == [
        ("oven#1", "output:tar"),
        ("oven#2", "output:tar"),
        ("extractor", "output:tar"),
        ("tower", "input:tar"),
    ]


def test_the_union_is_transitive_and_keeps_plan_order() -> None:
    # e1 and e2 share nothing; e3 links them (the mixer's output to e2, the column's input to e1).
    # The unrelated feed edge stays its own net, ahead of the merged one as it is in the plan.
    plan = _plan(
        [
            _recipe("still", kind=None, inputs=[("feed", 20.0)], outputs=[("oil", 20.0)]),
            _recipe("mixer", kind=_MULTIBLOCK, outputs=[("oil", 40.0)]),
            _recipe("column", kind=_MULTIBLOCK, inputs=[("oil", 60.0)]),
        ],
        [
            _edge("e0", "source", "still", "feed"),
            _edge("e1", "still", "column", "oil"),
            _edge("e2", "mixer", "tank", "oil"),
            _edge("e3", "mixer", "column", "oil"),
        ],
        storages=["source", "tank"],
    )
    ir = to_input_ir(plan)
    assert [n.id for n in _material(ir)] == ["e0", "e1+e2+e3"]
    assert _refs(_net(ir, "e1+e2+e3")) == [
        ("still", "output:oil"),
        ("mixer", "output:oil"),
        ("column", "input:oil"),
        ("tank", "input:oil"),
    ]
    # Two producers, each counted once: 1 mB/t from the still and 2 from the mixer.
    assert _net(ir, "e1+e2+e3").throughput == pytest.approx(3.0)


def test_a_structure_record_makes_a_machine_a_multiblock_without_a_handler() -> None:
    # The MrBruh fork emits no handlers, so a dump is the only evidence there; it must be enough.
    ir = to_input_ir(_fan_out(kind=None), physical=hatched_dataset(key="reactor"))
    assert [n.id for n in _material(ir)] == ["e1+e2", "output-net:reactor:steam"]


@pytest.mark.parametrize(
    ("record", "kind", "expected"),
    [
        (True, None, True),
        (False, _MULTIBLOCK, True),
        (False, _SINGLE, False),
        (False, None, False),
    ],
)
def test_either_piece_of_evidence_makes_a_multiblock(
    record: bool, kind: str | None, expected: bool
) -> None:
    recipe = _recipe("m", kind=kind)
    node = Node(id="m", recipe_id="m", overclock_tier="LV")
    physical = hatched_dataset(key="m").machines["m"] if record else None
    assert _is_multiblock(recipe, node, physical) is expected


# ------------------------------------------------------------------ what does not merge


def test_a_storage_shared_port_keeps_one_net_per_edge() -> None:
    # A storage has no hatch, and the shipped MrBruh lines share only storage ports, so this is what
    # keeps their layouts exactly where they were. The consumers are multiblocks on purpose: only
    # the port being shared decides, and their two input ports are two different ports.
    plan = _plan(
        [
            _recipe("left", kind=_MULTIBLOCK, inputs=[("water", 20.0)]),
            _recipe("right", kind=_MULTIBLOCK, inputs=[("water", 40.0)]),
        ],
        [_edge("e1", "tank", "left", "water"), _edge("e2", "tank", "right", "water")],
        storages=["tank"],
    )
    ir = to_input_ir(plan)
    assert [n.id for n in _material(ir)] == ["e1", "e2"]
    assert _refs(_net(ir, "e1")) == [("tank", "output:water"), ("left", "input:water")]


@pytest.mark.parametrize("kind", [_SINGLE, None])
def test_a_single_block_shared_port_keeps_one_net_per_edge(kind: str | None) -> None:
    # Out of scope for #213: a single block has no hatch for the router and validator to disagree
    # about, and no handler plus no record is not evidence of a multiblock.
    ir = to_input_ir(_fan_out(kind=kind))
    assert [n.id for n in _material(ir)] == ["e1", "e2", "output-net:reactor:steam"]


def test_a_lone_edge_keeps_its_own_id_and_rating() -> None:
    plan = _plan(
        [_recipe("reactor", kind=_MULTIBLOCK, outputs=[("acid", 100.0)])],
        [_edge("only", "reactor", "tank", "acid")],
        storages=["tank"],
    )
    net = _net(to_input_ir(plan), "only")
    assert _refs(net) == [("reactor", "output:acid"), ("tank", "input:acid")]
    assert net.throughput == pytest.approx(5.0)


# ------------------------------------------------------------------ throughput


def test_a_merged_fan_out_carries_its_producer_once() -> None:
    # Each edge alone is rated at the reactor's full 5 mB/t, because the plan gives an edge no rate
    # of its own. The port moves 5 mB/t, not 10.
    assert _net(to_input_ir(_fan_out()), "e1+e2").throughput == pytest.approx(5.0)


def test_a_merged_fan_in_carries_every_producer() -> None:
    # Two ovens at 1 mB/t each and an extractor at 2: the tower's input takes all three.
    plan = _plan(
        [
            _recipe("oven", kind=None, outputs=[("tar", 20.0)]),
            _recipe("extractor", kind=None, outputs=[("tar", 40.0)]),
            _recipe("tower", kind=_MULTIBLOCK, inputs=[("tar", 60.0)]),
        ],
        [_edge("e1", "oven", "tower", "tar"), _edge("e2", "extractor", "tower", "tar")],
        counts={"oven": 2},
    )
    assert _net(to_input_ir(plan), "e1+e2").throughput == pytest.approx(4.0)


def test_two_storages_feeding_one_multiblock_input_count_its_demand_once() -> None:
    # A storage has no recipe, so a storage-fed edge is rated at what its consumer takes. Two tanks
    # topping up one input together supply that one demand, not two of it.
    plan = _plan(
        [_recipe("tower", kind=_MULTIBLOCK, inputs=[("tar", 60.0)])],
        [_edge("e1", "a", "tower", "tar"), _edge("e2", "b", "tower", "tar")],
        storages=["a", "b"],
    )
    assert _net(to_input_ir(plan), "e1+e2").throughput == pytest.approx(3.0)


# ------------------------------------------------------------------ the line around the merge


def test_an_unconsumed_output_still_collects_into_a_buffer() -> None:
    # The merged port is consumed, so it gets no buffer; the reactor's steam is not, so it does.
    ir = to_input_ir(_fan_out())
    buffers = [m.id for m in ir.machines if m.id.startswith("output-buffer:")]
    assert buffers == ["output-buffer:reactor:steam"]


def test_edges_disagreeing_on_a_ports_resource_are_refused() -> None:
    plan = _plan(
        [
            _recipe("reactor", kind=_MULTIBLOCK, outputs=[("acid", 100.0)]),
            _recipe("plant", kind=None, inputs=[("acid", 60.0)]),
        ],
        [
            _edge("e1", "reactor", "plant", "acid"),
            _edge("e2", "reactor", "tank", "acid", kind="item"),
        ],
        storages=["tank"],
    )
    with pytest.raises(AdapterError, match=r"'e1' and 'e2' meet at one multiblock port"):
        to_input_ir(plan)


# ------------------------------------------------------------------ the bug itself


def test_a_two_consumer_multiblock_output_gets_one_hatch_and_no_mismatch() -> None:
    """The #213 regression, on the committed Vacuum Freezer's real structure.

    One output feeding two tanks used to be two nets on one port: the router placed the hatch
    behind the first terminal and the validator checked it against the second, so the line came
    back ``partial_invalid`` with ``hatch_terminal_mismatch`` and every net routed.
    """
    plan = Plan(
        schema_version=1,
        recipes=[
            Recipe(
                id="r",
                machine_type="Vacuum Freezer",
                eut=120.0,
                duration_ticks=20.0,
                inputs=[Resource(kind="fluid", id="hot", amount=20.0)],
                outputs=[Resource(kind="fluid", id="cold", amount=20.0)],
            )
        ],
        nodes=[Node(id="vf", recipe_id="r", overclock_tier="MV")],
        storages=[Storage(id=sid, kind="fluid") for sid in ("feed", "a", "b")],
        edges=[
            _edge("in", "feed", "vf", "hot"),
            _edge("e1", "vf", "a", "cold"),
            _edge("e2", "vf", "b", "cold"),
        ],
    )
    ir = to_input_ir(plan, physical=load_physical_dataset(_FIXTURE_DATASET))
    assert [n.id for n in _material(ir)] == ["in", "e1+e2"]

    layout = solve(ir)
    report = validate(ir, layout)
    assert ViolationCode.HATCH_TERMINAL_MISMATCH not in {v.code for v in report.violations}
    assert layout.status is LayoutStatus.VALID, report.violations
    hatches = [h for h in layout.hatches if h.port_id == "output:cold"]
    assert [(h.machine_id, h.kind) for h in hatches] == [("vf", "OutputHatch")]
    (route,) = [r for r in layout.routes if r.net_id == "e1+e2"]
    assert sorted(t.machine_id for t in route.terminals) == ["a", "b", "vf"]


def test_the_real_2_9_line_leaves_no_multiblock_port_on_two_nets() -> None:
    """``ev-nitrobenzene.json`` had five such ports (two Centrifuge instances, a Distillation Tower
    input, two LCR outputs) across four pairs of edges, all declared multiblocks by the export's own
    handlers, so the merge needs no dataset. What stays shared is one Super Tank's water.
    """
    plan = load_plan(_EV_NITROBENZENE)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # its 12x-parallel Distillus warns; not this test's point
        ir = to_input_ir(plan)
    assert len(ir.nets) == 27  # 31 before the merge
    assert sum("+" in n.id for n in ir.nets) == 4
    storages = {s.id for s in plan.storages}
    seen: dict[tuple[str, str], str] = {}
    shared: set[tuple[str, str]] = set()
    for net in _material(ir):
        for ref in _refs(net):
            if seen.setdefault(ref, net.id) != net.id:
                shared.add(ref)
    assert {machine for machine, _ in shared} <= storages
    assert len(shared) == 1
