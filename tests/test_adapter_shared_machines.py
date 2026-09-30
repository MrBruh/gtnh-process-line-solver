"""Tests for ``nodes[].extraRecipes``: the arodoid fork's shared machine.

One card, several recipes, one set of machines: an LCR fed for two reactions runs whichever one its
inputs allow, so the recipes TIME-SHARE it. Section 0 is the node's own ``recipeId`` and sections
1..n are ``extraRecipes``, each with its own ingredient choices. The export does not say how the
machine's time splits between them, so the adapter takes an even split and says so.

What must hold, and what the tests below pin: the machine has every section's ports, each section
is rated at its share of the time, the machine draws what its hungriest recipe draws, a product two
sections share is one port and one net, and an ordinary node maps exactly as it did.
"""

from __future__ import annotations

import warnings

import pytest

from gtnh_solver.adapter import (
    AdapterError,
    AdapterWarning,
    Edge,
    Node,
    Plan,
    Recipe,
    RecipeSection,
    Resource,
    Storage,
    to_input_ir,
)
from gtnh_solver.adapter.core import _node_rate, _rate, _sections
from gtnh_solver.ir import InputIR, Machine

_WILDCARD_LOG = "minecraft:log@32767"
_SPRUCE_LOG = "minecraft:log@1"
_RUBBER_WOOD = "ic2:blockrubwood"


def _res(kind: str, rid: str, amount: float = 1.0) -> Resource:
    return Resource(kind=kind, id=rid, amount=amount)


def _recipe(
    rid: str,
    inputs: list[Resource],
    outputs: list[Resource],
    *,
    eut: float = 0.0,
    duration_ticks: float = 10.0,
) -> Recipe:
    return Recipe(
        id=rid,
        machine_type="M",
        eut=eut,
        duration_ticks=duration_ticks,
        inputs=inputs,
        outputs=outputs,
    )


def _card(*extra: str, machine_count: int = 1) -> Node:
    """A node running recipe ``a`` that also time-shares the recipes ``extra`` names."""
    return Node(
        id="n",
        recipe_id="a",
        overclock_tier="LV",
        machine_count=machine_count,
        extra_recipes=[RecipeSection(recipe_id=rid) for rid in extra],
    )


#: Recipe a turns 10 ``in_a`` into 10 ``x`` and recipe b turns 20 ``in_b`` into 30 ``x`` and 10
#: ``y``, both in 10 ticks. Run flat out that is 1/t of in_a against 2/t of in_b, so an even split
#: is visibly not either recipe's own figure.
_A = _recipe("a", [_res("item", "in_a", 10)], [_res("item", "x", 10)], eut=30)
_B = _recipe(
    "b", [_res("item", "in_b", 20)], [_res("item", "x", 30), _res("item", "y", 10)], eut=120
)


def _machine(ir: InputIR, machine_id: str = "n") -> Machine:
    return next(m for m in ir.machines if m.id == machine_id)


def _port_rates(machine: Machine) -> dict[str, float]:
    return {p.id: p.rate or 0.0 for p in machine.faces.ports}


def _shared_plan(*edges: Edge, machine_count: int = 1) -> Plan:
    return Plan(
        schema_version=1,
        recipes=[_A, _B],
        nodes=[_card("b", machine_count=machine_count)],
        storages=[Storage(id="s", kind="item")],
        edges=list(edges),
    )


def _map(plan: Plan) -> InputIR:
    with pytest.warns(AdapterWarning, match="time-shares 2 recipes"):
        return to_input_ir(plan)


# ------------------------------------------------------------------ parsing


def test_extra_recipes_parse_as_sections_with_their_own_overrides() -> None:
    # As the export spells it: camelCase, and override keys as JSON strings.
    node = Node.model_validate(
        {
            "id": "n",
            "recipeId": "a",
            "overclockTier": "ULV",
            "extraRecipes": [
                {
                    "recipeId": "a",
                    "recipeInputOverrides": {"0": {"kind": "item", "id": _RUBBER_WOOD}},
                }
            ],
        }
    )
    assert [section.recipe_id for section in node.extra_recipes] == ["a"]
    assert node.extra_recipes[0].recipe_input_overrides[0].id == _RUBBER_WOOD


# ------------------------------------------------------------------ sections


def test_an_ordinary_node_is_one_section_and_itself() -> None:
    node = _card()
    assert _sections(node, {"a": _A}) == [(_A, node)]


def test_a_section_sees_its_own_recipe_on_the_nodes_machines() -> None:
    node = _card("b", machine_count=3)
    (_, first), (recipe, section) = _sections(node, {"a": _A, "b": _B})
    assert first is node
    assert recipe is _B
    assert (section.id, section.recipe_id, section.machine_count) == ("n", "b", 3)
    assert section.extra_recipes == []


def test_a_section_naming_an_unknown_recipe_is_refused() -> None:
    with pytest.raises(AdapterError, match="unknown recipe 'nope'"):
        _sections(_card("nope"), {"a": _A})


def test_one_section_is_rated_exactly_as_before() -> None:
    node = _card()
    assert _node_rate([(_A, node)], "x", outputs=True) == _rate(_A, "x", node, outputs=True)


# ------------------------------------------------------------------ through the mapping


def test_a_shared_machine_has_every_sections_ports() -> None:
    ports = _port_rates(_machine(_map(_shared_plan())))
    assert {pid for pid in ports if not pid.startswith("power")} == {
        "input:in_a",
        "input:in_b",
        "output:x",
        "output:y",
    }


def test_each_section_is_rated_at_an_even_share_of_the_time() -> None:
    ports = _port_rates(_machine(_map(_shared_plan())))
    assert ports["input:in_a"] == pytest.approx(0.5)  # 1/t flat out, half the time
    assert ports["input:in_b"] == pytest.approx(1.0)  # 2/t flat out, half the time
    assert ports["output:y"] == pytest.approx(0.5)
    # A product both make is one port, at the mean of 1/t and 3/t.
    assert ports["output:x"] == pytest.approx(2.0)


def test_a_net_carries_the_whole_groups_share() -> None:
    ir = _map(
        _shared_plan(
            Edge(id="e", source="s", target="n", resource_kind="item", resource_id="in_b"),
            machine_count=3,
        )
    )
    assert next(net for net in ir.nets if net.id == "e").throughput == pytest.approx(3.0)


def test_a_shared_machine_draws_what_its_hungriest_recipe_draws() -> None:
    assert _machine(_map(_shared_plan())).eut == 120


def test_the_time_split_is_warned_once_per_node() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        to_input_ir(_shared_plan())
    assert len([w for w in caught if "time-shares" in str(w.message)]) == 1


# ------------------------------------------------------------------ repeated connections


def test_a_connection_each_section_draws_is_one_net() -> None:
    # Both sections make x, and the export draws a wire from each to the same storage: one pipe
    # from one port, so one net, carrying the port's rate once rather than twice.
    ir = _map(
        _shared_plan(
            Edge(id="e1", source="n", target="s", resource_kind="item", resource_id="x"),
            Edge(id="e2", source="n", target="s", resource_kind="item", resource_id="x"),
        )
    )
    nets = [net for net in ir.nets if net.fluid_or_item == "x"]
    assert [net.id for net in nets] == ["e1+e2"]
    assert nets[0].throughput == pytest.approx(2.0)
    assert len(nets[0].endpoints) == 2


def test_connections_to_different_places_stay_apart() -> None:
    plan = _shared_plan(
        Edge(id="e1", source="n", target="s", resource_kind="item", resource_id="x"),
        Edge(id="e2", source="n", target="t", resource_kind="item", resource_id="x"),
    )
    plan.storages.append(Storage(id="t", kind="item"))
    ir = _map(plan)
    assert sorted(net.id for net in ir.nets if net.fluid_or_item == "x") == ["e1", "e2"]


# ------------------------------------------------------------------ the real case


def test_a_coke_oven_burning_two_logs_loads() -> None:
    # A community plan in miniature: one Coke Oven card runs its charcoal recipe twice, once on
    # spruce and once on rubber wood (a listed alternative of the wildcard log), fed from two
    # storages. The rubber-wood edge used to name a port the machine did not have.
    log = Resource(
        kind="item",
        id=_WILDCARD_LOG,
        amount=1,
        alternatives=[_res("item", _WILDCARD_LOG), _res("item", _RUBBER_WOOD)],
    )
    oven = _recipe("oven", [log], [_res("item", "minecraft:coal@1")])
    node = Node(
        id="n",
        recipe_id="oven",
        overclock_tier="ULV",
        recipe_input_overrides={0: _res("item", _SPRUCE_LOG)},
        extra_recipes=[
            RecipeSection(recipe_id="oven", recipe_input_overrides={0: _res("item", _RUBBER_WOOD)})
        ],
    )
    plan = Plan(
        schema_version=1,
        recipes=[oven],
        nodes=[node],
        storages=[Storage(id="spruce", kind="item"), Storage(id="rubber", kind="item")],
        edges=[
            Edge(
                id="e1", source="spruce", target="n", resource_kind="item", resource_id=_SPRUCE_LOG
            ),
            Edge(
                id="e2", source="rubber", target="n", resource_kind="item", resource_id=_RUBBER_WOOD
            ),
        ],
    )
    ports = _port_rates(_machine(_map(plan)))
    assert ports[f"input:{_SPRUCE_LOG}"] == pytest.approx(0.05)  # 1 per 10 ticks, half the time
    assert ports[f"input:{_RUBBER_WOOD}"] == pytest.approx(0.05)
    assert ports["output:minecraft:coal@1"] == pytest.approx(0.1)  # both make charcoal
