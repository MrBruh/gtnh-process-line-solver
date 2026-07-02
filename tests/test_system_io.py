"""Tests for the shared boundary/power derivation (``system_io``) the guide and previewer share.

Mostly against the real sand line (the artifact both surfaces render), plus a hand-built case for
the fallbacks: a boundary storage with no sourcing net (no rate) and a dangling output whose
resource is recovered from an unprefixed port id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtnh_solver.adapter import adapt_file
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
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.solver import solve
from gtnh_solver.system_io import (
    BoundaryFlow,
    SystemIO,
    is_boundary_storage,
    port_resource,
    system_io,
)

_SAND = Path(__file__).resolve().parents[1] / "examples" / "gtnh-sand.json"


def _sand_io() -> SystemIO:
    # The fast (constructive) solve: deterministic layout coordinates that the exact-cell
    # assertions below can rely on; system_io does not care which placer produced them.
    ir = adapt_file(_SAND)
    return system_io(ir, solve(ir, optimize=False))


def test_sand_input_is_the_super_chest_with_its_typed_rate() -> None:
    io = _sand_io()
    assert len(io.inputs) == 1
    stone = io.inputs[0]
    assert (stone.machine_type, stone.resource, stone.cell) == (
        "Super Chest",
        "minecraft:stone",
        (0, 0, 0),
    )
    assert stone.commodity is Commodity.ITEM
    assert stone.rate == pytest.approx(0.1)


def test_sand_output_is_collected_by_a_synthesized_buffer() -> None:
    io = _sand_io()
    # the final sand is wired to a synthesized output Super Chest (#16), a boundary storage that
    # only sinks -> a system output whose rate is the net feeding the buffer
    assert len(io.outputs) == 1
    out = io.outputs[0]
    assert (out.machine_type, out.resource) == ("Super Chest", "minecraft:sand")
    assert out.rate == pytest.approx(0.1)


def test_sand_power_totals_eut_and_sums_amps_by_tier() -> None:
    io = _sand_io()
    assert io.power_total == pytest.approx(48.0)  # 3 Forge Hammers x 16 EU/t
    assert io.power_amps_by_tier == {"LV": 3}  # each draws ceil(16 / 32) = 1 A on the LV cable


def test_falls_back_without_a_sourcing_net_and_on_unprefixed_ids() -> None:
    chest = Machine(
        id="chest",
        type="Super Chest",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="output:thing", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )
    maker = Machine(
        id="maker",
        type="Maker",
        voltage_tier="LV",
        eut=8.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)]
        ),
    )
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=[chest, maker])
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="chest", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH),
            Placement(machine_id="maker", cell=CellCoord(x=2, y=0, z=0), orientation=Facing.NORTH),
        ],
    )
    io = system_io(problem, layout)
    # storage output with no net -> resource from the ``output:`` prefix, rate omitted
    assert io.inputs == [
        BoundaryFlow("chest", "Super Chest", (0, 0, 0), "thing", Commodity.ITEM, None)
    ]
    # dangling output on a plain (non-``{dir}:``-prefixed) port id -> id used verbatim
    assert io.outputs == [BoundaryFlow("maker", "Maker", (2, 0, 0), "out", Commodity.ITEM, None)]
    assert io.power_total == pytest.approx(8.0)
    assert io.power_amps_by_tier == {"LV": 1}  # ceil(8 / 32) = 1 A (no power route -> distance 0)


def test_power_amps_account_for_cable_loss_over_distance() -> None:
    # 16 EU/t at LV is 1 amp at the source, but along a 20-block cable the delivered voltage is
    # 12 V, so the source must actually supply ceil(16 / 12) = 2 amps. The summary reflects what the
    # builder feeds, loss included - not the lossless ideal.
    src = Machine(
        id="src",
        type="Power Source (LV)",
        voltage_tier="LV",
        eut=0.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
    m0 = Machine(
        id="m0",
        type="M",
        voltage_tier="LV",
        eut=16.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT)]
        ),
    )
    n = 20
    problem = InputIR(
        bounding_region=CellBox(sx=n + 4, sy=4, sz=4),
        machines=[src, m0],
        nets=[
            Net(
                id="pw",
                commodity=Commodity.POWER,
                throughput=16.0,
                endpoints=[
                    MachineFaceRef(machine_id="src", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id="src", cell=CellCoord(x=0, y=0, z=0), orientation=Facing.NORTH),
            Placement(machine_id="m0", cell=CellCoord(x=n, y=0, z=0), orientation=Facing.NORTH),
        ],
        routes=[
            Route(
                net_id="pw",
                commodity=Commodity.POWER,
                terminals=[
                    Terminal(
                        machine_id="src",
                        port_id="po",
                        face=Facing.SOUTH,
                        cell=CellCoord(x=0, y=0, z=1),
                    ),
                    Terminal(
                        machine_id="m0",
                        port_id="pi",
                        face=Facing.SOUTH,
                        cell=CellCoord(x=n, y=0, z=1),
                    ),
                ],
                segments=[
                    Segment(
                        start=CellCoord(x=i, y=0, z=1), end=CellCoord(x=i + 1, y=0, z=1), channel=0
                    )
                    for i in range(n)
                ],
                thickness_per_segment=[2] * n,
            )
        ],
    )
    io = system_io(problem, layout)
    assert io.power_total == pytest.approx(16.0)
    assert io.power_amps_by_tier == {"LV": 2}  # loss over 20 blocks doubles the amps vs lossless


def test_helper_predicates() -> None:
    assert is_boundary_storage("Super Tank")
    assert not is_boundary_storage("Forge Hammer")
    out = Port(id="output:minecraft:sand", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)
    assert port_resource(out) == "minecraft:sand"
    bare = Port(id="widget", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)
    assert port_resource(bare) == "widget"  # no ``output:`` prefix -> used as-is
