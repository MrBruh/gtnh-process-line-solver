"""Tests for the text build guide.

Mostly integration against the real sand line (the artifact a player would actually read),
plus an empty-layout case and a power-route case for the fallback branches.
"""

from __future__ import annotations

from pathlib import Path

from gtnh_solver.adapter import adapt_file
from gtnh_solver.buildguide import build_guide
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    Net,
    Placement,
    Port,
)
from gtnh_solver.router import route_power
from gtnh_solver.solver import solve

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _sand_guide() -> str:
    ir = adapt_file(_SAND)
    return build_guide(ir, solve(ir))


def test_build_guide_sand_has_all_sections() -> None:
    guide = _sand_guide()
    for marker in (
        "# Build guide",
        "## Bill of materials",
        "## Connections",
        "## Layout",
        "### Layer y = 0",
        "## Key",
    ):
        assert marker in guide, f"missing section: {marker}"


def test_build_guide_bom_counts_machines_by_type() -> None:
    guide = _sand_guide()
    assert "3  x  Forge Hammer" in guide
    assert "1  x  Super Chest" in guide


def test_build_guide_sand_auto_feeds_items_and_cables_power() -> None:
    guide = _sand_guide()
    assert "minecraft:gravel" in guide
    assert "(auto-output)" in guide  # the item chain auto-feeds (no item pipes)...
    assert "power cable" in guide  # ...but the synthesized power net still needs a cable
    assert "## Power" in guide  # and the guide tells the builder to feed it externally


def test_build_guide_is_deterministic() -> None:
    assert _sand_guide() == _sand_guide()


def test_build_guide_handles_empty_layout() -> None:
    ir = adapt_file(_SAND)
    guide = build_guide(ir, LayoutResult(status=LayoutStatus.VALID, seed=0))
    assert "(empty)" in guide  # no placements/routes
    assert "(no pipes)" in guide  # no routing in the BoM


def test_build_guide_renders_power_route_with_fallback_label() -> None:
    a = Machine(
        id="a",
        type="Dynamo",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="pa", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
    b = Machine(
        id="b",
        type="Machine",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="pb", commodity=Commodity.POWER, direction=IODirection.INPUT)]
        ),
    )
    net = Net(
        id="p",
        commodity=Commodity.POWER,
        throughput=32.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="pa"),
            MachineFaceRef(machine_id="b", port_id="pb"),
        ],
    )
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[a, b], nets=[net])
    placements = [
        Placement(machine_id="a", cell=CellCoord(x=1, y=0, z=1), orientation=Facing.NORTH),
        Placement(machine_id="b", cell=CellCoord(x=3, y=0, z=1), orientation=Facing.NORTH),
    ]
    rr = route_power(problem, placements)
    layout = LayoutResult(
        status=LayoutStatus.VALID, seed=0, placements=placements, routes=list(rr.routes)
    )
    guide = build_guide(problem, layout)
    assert "power cable" in guide  # BoM label
    assert "power" in guide  # connections falls back to the commodity (power has no fluid_or_item)
    assert "## Power" in guide  # the external-power-source note (a is a power source)
