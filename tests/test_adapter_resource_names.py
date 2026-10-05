"""The display names the adapter keeps for the resources a line moves (#296).

Both gtnh-factory-flow forks export a name for every resource: an edge's ``label`` and a recipe
input or output's ``displayName``. The adapter used to drop both, so the previewer could only print
raw ids. It now keeps them in ``InputIR.resource_names``, read from the plan and never authored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gtnh_solver.adapter import Edge, Node, Plan, Recipe, Resource, Storage, adapt_file, to_input_ir
from gtnh_solver.ir import Commodity, InputIR
from gtnh_solver.ir.nets import port_resource

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _carried(ir: InputIR) -> set[str]:
    """Every resource the problem moves: on a net, through a non-power port, or by a filter."""
    carried = {resource for net in ir.nets for resource in net.resources}
    for machine in ir.machines:
        carried.update(machine.filter_items)
        carried.update(
            port_resource(port)
            for port in machine.faces.ports
            if port.commodity is not Commodity.POWER
        )
    return carried


def test_a_mrbruh_plan_keeps_the_names_it_exported() -> None:
    # The sand line's three edges name stone, cobblestone and gravel; the sand it makes is on no
    # edge (the adapter closes the line with a collection buffer), so its name is the recipe's.
    ir = adapt_file(_EXAMPLES / "gtnh-sand.json")
    assert ir.resource_names == {
        "minecraft:cobblestone": "Cobblestone",
        "minecraft:gravel": "Gravel",
        "minecraft:sand": "Sand",
        "minecraft:stone": "Stone",
    }


def test_an_arodoid_plan_names_exactly_what_the_line_moves() -> None:
    """Every resource the 2.9 line carries is named, and nothing else is listed.

    The plan lists hundreds of ore-dictionary alternatives on its inputs, each with a name of its
    own. Only the ones a node actually feeds are resources of the problem, so the rest stay out.
    """
    plan = json.loads((_EXAMPLES / "ev-nitrobenzene.json").read_text(encoding="utf-8"))
    ir = adapt_file(_EXAMPLES / "ev-nitrobenzene.json")
    carried = _carried(ir)
    assert set(ir.resource_names) == carried
    alternatives = {
        alt["id"]
        for recipe in plan["recipes"]
        for res in recipe["inputs"]
        for alt in res.get("alternatives", [])
    }
    assert alternatives - carried, "the fixture must list alternatives the line does not feed"
    assert ir.resource_names["nitrobenzene"] == "Nitrobenzene"


def test_an_edge_label_beats_a_recipe_name() -> None:
    # The one disagreement in the shipped plans: the coke oven's wildcard log is "Oak Log" on its
    # edge and "Oak Wood" on the recipe input. The edge is what becomes the net, so its name wins.
    ir = adapt_file(_EXAMPLES / "gtnh-nitrobenzene.json")
    assert ir.resource_names["minecraft:log@32767"] == "Oak Log"


def _stripped(value: Any) -> Any:
    """``value`` with every ``displayName`` and ``label`` key removed, at any depth."""
    if isinstance(value, dict):
        return {k: _stripped(v) for k, v in value.items() if k not in {"displayName", "label"}}
    if isinstance(value, list):
        return [_stripped(v) for v in value]
    return value


def test_a_plan_that_names_nothing_has_no_names() -> None:
    raw = json.loads((_EXAMPLES / "gtnh-sand.json").read_text(encoding="utf-8"))
    ir = to_input_ir(Plan.model_validate(_stripped(raw)))
    assert ir.resource_names == {}


def _override_plan(*, edge_label: str = "") -> Plan:
    """A wildcard-log recipe whose node pins the spruce it is fed, from a storage."""
    recipe = Recipe(
        id="r",
        machine_type="M",
        duration_ticks=10.0,
        inputs=[Resource(kind="item", id="minecraft:log@32767", amount=1, display_name="Oak Wood")],
        outputs=[Resource(kind="item", id="product", amount=1)],
    )
    spruce = Resource(kind="item", id="minecraft:log@1", amount=1, display_name="Spruce Wood")
    node = Node(id="n", recipe_id="r", overclock_tier="LV", recipe_input_overrides={0: spruce})
    edge = Edge(
        id="e",
        source="s",
        target="n",
        resource_kind="item",
        resource_id="minecraft:log@1",
        label=edge_label,
    )
    return Plan(
        schema_version=1,
        recipes=[recipe],
        nodes=[node],
        storages=[Storage(id="s", kind="item")],
        edges=[edge],
    )


def test_a_node_override_names_the_resource_it_feeds() -> None:
    # The node feeds spruce, so the machine's port is spruce and the recipe's wildcard is no
    # resource of the problem: its "Oak Wood" is not listed, and spruce takes the override's name.
    ir = to_input_ir(_override_plan())
    assert ir.resource_names == {"minecraft:log@1": "Spruce Wood"}


def test_an_edge_label_beats_a_node_override_name() -> None:
    ir = to_input_ir(_override_plan(edge_label="Spruce Log"))
    assert ir.resource_names == {"minecraft:log@1": "Spruce Log"}
