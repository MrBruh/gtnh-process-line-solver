"""Tests for ``nodes[].recipeInputOverrides``: per-node ingredient choices on a shared recipe.

Two unlike things arrive under one field, and the whole point of this lane is that they are told
apart:

**Narrowing** a wildcard (``minecraft:log@32767`` -> ``minecraft:log@1``) is the exporter recording
which item the player actually feeds the machine. Ignoring it is a hard failure, because the edge
names the concrete id and the machine has no such port.

**Substituting** a different resource (``oxygen`` -> ``water``) appears in real plans and must NOT
be applied: doing so drops a required input and duplicates another, silently shrinking the port set.
The adapter cannot tell a stale plan from a deliberate swap, so it keeps the recipe's input and says
so out loud.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    Edge,
    Node,
    Plan,
    Recipe,
    Resource,
    Storage,
    load_plan,
    to_input_ir,
)
from gtnh_solver.adapter.core import _check_input_overrides, _effective_inputs, _refines
from gtnh_solver.ir import InputIR, IODirection

_WILDCARD_LOG = "minecraft:log@32767"
_SPRUCE_LOG = "minecraft:log@1"  # vanilla log metas: 0 oak, 1 spruce, 2 birch
_BIRCH_LOG = "minecraft:log@2"
_PARALLEL_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-parallel-sand.json"


def _res(kind: str, rid: str, amount: float = 1.0) -> Resource:
    return Resource(kind=kind, id=rid, amount=amount)


def _recipe(*inputs: Resource) -> Recipe:
    return Recipe(
        id="r",
        machine_type="M",
        duration_ticks=10.0,
        inputs=list(inputs),
        outputs=[_res("item", "product")],
    )


def _node(node_id: str = "n", **overrides: Resource) -> Node:
    return Node(
        id=node_id,
        recipe_id="r",
        overclock_tier="LV",
        recipe_input_overrides={int(k.removeprefix("i")): v for k, v in overrides.items()},
    )


# ------------------------------------------------------------------ the classification


def test_an_identical_override_refines_trivially() -> None:
    assert _refines(_res("item", _SPRUCE_LOG), _res("item", _SPRUCE_LOG))


def test_a_wildcard_meta_narrowed_to_a_concrete_one_refines() -> None:
    # 32767 is Forge's OreDictionary.WILDCARD_VALUE. Exactly the real case: the Industrial Coke
    # Oven's charcoal recipe accepts any log, and the node pins it to the spruce the player feeds.
    assert _refines(_res("item", _WILDCARD_LOG), _res("item", _SPRUCE_LOG))


def test_a_different_registry_name_does_not_refine() -> None:
    assert not _refines(_res("fluid", "oxygen", 3000), _res("fluid", "water", 1000))


def test_a_different_kind_does_not_refine() -> None:
    assert not _refines(_res("item", "x"), _res("fluid", "x"))


def test_two_concrete_metas_do_not_refine() -> None:
    # Neither generalises the other, so this is a swap, not a narrowing.
    assert not _refines(_res("item", _BIRCH_LOG), _res("item", _SPRUCE_LOG))


# ------------------------------------------------------------------ resolution


def test_a_refining_override_is_applied() -> None:
    recipe = _recipe(_res("item", _WILDCARD_LOG, 16))
    effective = _effective_inputs(recipe, _node(i0=_res("item", _SPRUCE_LOG, 16)))
    assert [r.id for r in effective] == [_SPRUCE_LOG]


def test_a_substituting_override_is_not_applied() -> None:
    recipe = _recipe(_res("fluid", "oxygen", 3000), _res("fluid", "water", 1000))
    effective = _effective_inputs(recipe, _node(i0=_res("fluid", "water", 1000)))
    assert [r.id for r in effective] == ["oxygen", "water"]


def test_resolution_never_mutates_the_shared_recipe() -> None:
    # One recipe is shared by every node that runs it, so resolving in place would leak one node's
    # ingredient choice into its siblings.
    recipe = _recipe(_res("item", _WILDCARD_LOG, 16))
    _effective_inputs(recipe, _node(i0=_res("item", _SPRUCE_LOG, 16)))
    assert [r.id for r in recipe.inputs] == [_WILDCARD_LOG]


def test_an_out_of_range_index_is_ignored() -> None:
    recipe = _recipe(_res("item", _WILDCARD_LOG, 16))
    effective = _effective_inputs(recipe, _node(i5=_res("item", _SPRUCE_LOG, 16)))
    assert [r.id for r in effective] == [_WILDCARD_LOG]


def test_no_overrides_returns_the_recipe_inputs() -> None:
    recipe = _recipe(_res("item", "x"))
    assert _effective_inputs(recipe, _node()) == recipe.inputs


# ------------------------------------------------------------------ the warning


def test_a_substitution_warns_and_names_both_resources() -> None:
    recipe = _recipe(_res("fluid", "oxygen", 3000), _res("fluid", "water", 1000))
    with pytest.warns(AdapterWarning, match="substitutes a different resource") as caught:
        _check_input_overrides(recipe, _node(i0=_res("fluid", "water", 1000)))
    message = str(caught[0].message)
    assert "oxygen" in message
    assert "water" in message


def test_a_refinement_does_not_warn() -> None:
    recipe = _recipe(_res("item", _WILDCARD_LOG, 16))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check_input_overrides(recipe, _node(i0=_res("item", _SPRUCE_LOG, 16)))


def test_an_out_of_range_index_warns() -> None:
    recipe = _recipe(_res("item", _WILDCARD_LOG, 16))
    with pytest.warns(AdapterWarning, match="only 1 input"):
        _check_input_overrides(recipe, _node(i5=_res("item", _SPRUCE_LOG, 16)))


# ------------------------------------------------------------------ through the mapping


def _fed_plan(*nodes: Node) -> Plan:
    """A wildcard-input recipe fed from a storage by an edge naming the CONCRETE id."""
    return Plan(
        schema_version=1,
        recipes=[_recipe(_res("item", _WILDCARD_LOG, 16))],
        nodes=list(nodes),
        storages=[Storage(id="s", kind="item")],
        edges=[
            Edge(
                id=f"e{i}",
                source="s",
                target=node.id,
                resource_kind="item",
                resource_id=node.recipe_input_overrides[0].id,
            )
            for i, node in enumerate(nodes)
        ],
    )


def _input_ports(ir: InputIR, machine_id: str) -> set[str]:
    machine = next(m for m in ir.machines if m.id == machine_id)
    return {p.id for p in machine.faces.ports if p.direction is IODirection.INPUT}


def test_an_edge_naming_the_concrete_id_now_resolves() -> None:
    # The regression: without the override the machine has only "input:minecraft:log@32767", and
    # InputIR's referential-integrity check rejects the net as naming an unknown port.
    ir = to_input_ir(_fed_plan(_node(i0=_res("item", _SPRUCE_LOG, 16))))
    assert f"input:{_SPRUCE_LOG}" in _input_ports(ir, "n")


def test_the_concretised_port_carries_a_real_rate() -> None:
    # _rate sums amounts by resource id, so matching against the recipe's own "@32767" list would
    # find nothing and rate the net at zero. 16 items per 10 ticks.
    ir = to_input_ir(_fed_plan(_node(i0=_res("item", _SPRUCE_LOG, 16))))
    net = next(n for n in ir.nets if n.id == "e0")
    assert net.throughput == pytest.approx(1.6)


def test_two_nodes_on_one_recipe_keep_their_own_ingredient() -> None:
    # The per-node property, asserted end to end rather than on the helper.
    plan = _fed_plan(
        _node("spruce", i0=_res("item", _SPRUCE_LOG, 16)),
        _node("birch", i0=_res("item", _BIRCH_LOG, 16)),
    )
    ir = to_input_ir(plan)
    assert _input_ports(ir, "spruce") == {f"input:{_SPRUCE_LOG}"}
    assert _input_ports(ir, "birch") == {f"input:{_BIRCH_LOG}"}


def test_the_committed_fixture_applies_its_overrides_without_warning() -> None:
    # Every override in gtnh-parallel-sand.json is inert (it names the id the recipe already has),
    # which is the common case and must stay silent.
    plan = load_plan(_PARALLEL_SAND)
    assert any(node.recipe_input_overrides for node in plan.nodes)
    for node in plan.nodes:
        node.machine_count = 1  # machineCount > 1 is a separate lane
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        to_input_ir(plan)
