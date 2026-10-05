"""The colour the adapter keeps for each resource a line moves (#297).

Both gtnh-factory-flow forks give every resource a ``dominantColor`` (the average of its in-game
icon) on recipe I/O, node overrides and storages. The previewer draws it as a dot where it has no
icon to show, so the adapter keeps it in ``InputIR.resource_colors``, read from the plan, lowercased,
and only for the resources the problem moves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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


@pytest.mark.parametrize(
    "example",
    ["gtnh-sand", "gtnh-nitrobenzene", "gtnh-parallel-sand", "ev-nitrobenzene"],
)
def test_every_resource_a_shipped_line_moves_has_a_colour(example: str) -> None:
    # The MrBruh-fork nitrobenzene plan colours none of its recipe I/O, only its storages, which
    # is why storages are read at all: without them 18 of its resources would have no dot.
    ir = adapt_file(_EXAMPLES / f"{example}.json")
    assert set(ir.resource_colors) == _carried(ir)
    assert all(color == color.lower() for color in ir.resource_colors.values())


def test_a_recipes_colour_beats_a_node_overrides_and_a_storages() -> None:
    # The one disagreement in the shipped plans: ev-nitrobenzene's crop recipe outputs spruce log
    # as #3b2c18, while the node override and the storage that feed it say #55442a. Recipe I/O is
    # read first, as it is for the names.
    raw = json.loads((_EXAMPLES / "ev-nitrobenzene.json").read_text(encoding="utf-8"))
    seen = {
        res["dominantColor"]
        for recipe in raw["recipes"]
        for res in recipe["outputs"]
        if res["id"] == "minecraft:log@1"
    } | {s["dominantColor"] for s in raw["storages"] if s["resourceId"] == "minecraft:log@1"}
    assert seen == {"#3b2c18", "#55442a"}, "the fixture must still disagree with itself"
    ir = adapt_file(_EXAMPLES / "ev-nitrobenzene.json")
    assert ir.resource_colors["minecraft:log@1"] == "#3b2c18"


def _plan(
    *,
    input_color: str = "",
    output_color: str = "",
    override_color: str | None = None,
    storage_color: str = "",
    alternative_color: str = "",
) -> Plan:
    """A one-recipe line fed by a storage: a wildcard log in, ``product`` out. With
    ``override_color`` the node feeds spruce through an override of that colour."""
    recipe = Recipe(
        id="r",
        machine_type="M",
        duration_ticks=10.0,
        inputs=[
            Resource(
                kind="item",
                id="minecraft:log@32767",
                amount=1,
                dominant_color=input_color,
                alternatives=[
                    Resource(kind="item", id="unpicked", amount=1, dominant_color=alternative_color)
                ],
            )
        ],
        outputs=[Resource(kind="item", id="product", amount=1, dominant_color=output_color)],
    )
    overrides = {}
    fed = "minecraft:log@32767"
    if override_color is not None:
        fed = "minecraft:log@1"
        overrides = {0: Resource(kind="item", id=fed, amount=1, dominant_color=override_color)}
    node = Node(id="n", recipe_id="r", overclock_tier="LV", recipe_input_overrides=overrides)
    return Plan(
        schema_version=1,
        recipes=[recipe],
        nodes=[node],
        storages=[
            Storage(id="s", kind="item", resource_id=fed, dominant_color=storage_color),
        ],
        edges=[Edge(id="e", source="s", target="n", resource_kind="item", resource_id=fed)],
    )


def test_colours_are_lowercased_and_anything_else_is_dropped() -> None:
    ir = to_input_ir(_plan(input_color="#AbCdEf", output_color="red"))
    assert ir.resource_colors == {"minecraft:log@32767": "#abcdef"}


@pytest.mark.parametrize("bad", ["", "red", "#abc", "#abcdefg", "#ghijkl", "abcdef", "#abcdef\n"])
def test_a_value_that_is_not_a_colour_never_reaches_the_ir(bad: str) -> None:
    # A colour is decoration and the plan is somebody else's file, so a bad one is dropped rather
    # than failing the whole plan the way the IR's own check would.
    assert to_input_ir(_plan(output_color=bad)).resource_colors == {}


def test_only_what_the_line_moves_is_coloured() -> None:
    # The ore-dictionary alternative nobody picked is no resource of the problem.
    ir = to_input_ir(_plan(alternative_color="#010203", output_color="#040506"))
    assert ir.resource_colors == {"product": "#040506"}


def test_a_storage_colours_what_the_plan_colours_nowhere_else() -> None:
    ir = to_input_ir(_plan(storage_color="#0a0b0c"))
    assert ir.resource_colors == {"minecraft:log@32767": "#0a0b0c"}


def test_a_node_override_colours_the_resource_it_feeds_before_a_storage_does() -> None:
    ir = to_input_ir(_plan(override_color="#111111", storage_color="#222222"))
    assert ir.resource_colors == {"minecraft:log@1": "#111111"}


def _stripped(value: Any) -> Any:
    """``value`` with every ``dominantColor`` key removed, at any depth."""
    if isinstance(value, dict):
        return {k: _stripped(v) for k, v in value.items() if k != "dominantColor"}
    if isinstance(value, list):
        return [_stripped(v) for v in value]
    return value


def test_a_plan_that_colours_nothing_has_no_colours() -> None:
    raw = json.loads((_EXAMPLES / "gtnh-sand.json").read_text(encoding="utf-8"))
    assert to_input_ir(Plan.model_validate(_stripped(raw))).resource_colors == {}
