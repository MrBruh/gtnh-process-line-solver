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
        "## System inputs / outputs",
        "## Connections",
        "## Layout",
        "### Layer y = 0",
        "## Key",
    ):
        assert marker in guide, f"missing section: {marker}"


def test_build_guide_bom_counts_machines_by_type() -> None:
    guide = _sand_guide()
    assert "3  x  Forge Hammer" in guide
    assert "2  x  Super Chest" in guide  # the input source + the synthesized output buffer (#16)


def test_build_guide_sand_auto_feeds_items_and_cables_power() -> None:
    guide = _sand_guide()
    assert "minecraft:gravel" in guide
    assert "(auto-output)" in guide  # the item chain auto-feeds (no item pipes)...
    assert "power cable" in guide  # ...but the synthesized power net still needs a cable
    assert "## Power" in guide  # and the guide tells the builder to feed it externally


def test_build_guide_is_deterministic() -> None:
    assert _sand_guide() == _sand_guide()


def test_build_guide_placement_table_has_coords_and_front() -> None:
    guide = _sand_guide()
    assert "## Placement" in guide
    assert "at (0, 0, 0)" in guide  # exact build coordinate per machine
    assert "front north" in guide  # ...and which way the front (no-I/O) face points


def test_build_guide_power_note_states_feed_spec_as_tier_amps_eut() -> None:
    # the source must be fed as a wiring spec, not a bare cable thickness (GitHub #15 B2): the sand
    # trunk roots at 4x, the source is LV (32 V), so 4 A buys up to 32*4 = 128 EU/t.
    guide = _sand_guide()
    assert "feed LV (32 V), >=4 A -> up to 128 EU/t" in guide


def test_build_guide_system_io_loads_input_chest_and_collects_output_product() -> None:
    # GitHub #15/#16: the guide says what to load the boundary input with (resource + rate) and
    # which buffer collects the finished product, so it is buildable without reading the source plan.
    guide = _sand_guide()
    assert "## System inputs / outputs" in guide
    assert "load Super Chest at (0, 0, 0) with minecraft:stone (~0.1 items/t)" in guide
    assert "minecraft:sand collected by Super Chest at (4, 0, 0) (~0.1 items/t)" in guide


def test_build_guide_system_io_falls_back_without_a_sourcing_net() -> None:
    # A boundary storage whose output feeds no net (so no typed rate), and a producer whose output
    # no net consumes: the resource is recovered from the port id and the rate is simply omitted.
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
    guide = build_guide(problem, layout)
    assert "load Super Chest at (0, 0, 0) with thing" in guide  # resource from id
    assert "with thing (~" not in guide  # ...and no rate suffix, since no net gives a throughput
    assert "out exits Maker at (2, 0, 0) - place a Super Chest/Tank to collect it" in guide


def test_build_guide_power_connection_lists_per_segment_thickness() -> None:
    guide = _sand_guide()
    assert "lay along:" in guide  # the exact cells to lay the cable
    assert "=4x=" in guide  # the trunk root segment...
    assert "=1x=" in guide  # ...tapering to the far end


def _item_fork_problem() -> InputIR:
    out_port = Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)
    in_port = Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT)

    def producer(mid: str) -> Machine:
        return Machine(
            id=mid,
            type="Maker",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(ports=[out_port]),
        )

    def consumer(mid: str) -> Machine:
        return Machine(
            id=mid,
            type="Taker",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(ports=[in_port]),
        )

    def item_net(nid: str, src: str, dst: str) -> Net:
        return Net(
            id=nid,
            commodity=Commodity.ITEM,
            fluid_or_item="thing",
            throughput=1.0,
            endpoints=[
                MachineFaceRef(machine_id=src, port_id="out"),
                MachineFaceRef(machine_id=dst, port_id="in"),
            ],
        )

    # m1 feeds m2 (auto-output) and m3 (piped - its single auto-output is spent on the first)
    return InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=[producer("m1"), consumer("m2"), consumer("m3")],
        nets=[item_net("n1", "m1", "m2"), item_net("n2", "m1", "m3")],
    )


def test_build_guide_pipe_lists_covers_and_cells() -> None:
    # the piped item net's terminals need conveyor covers (docs/DOMAIN.md), and the guide gives
    # the exact cells to lay the pipe along - the buildable detail the old sketch lacked.
    problem = _item_fork_problem()
    layout = solve(problem)
    assert layout.status is LayoutStatus.VALID
    guide = build_guide(problem, layout)
    assert "conveyor cover (output)" in guide  # the source end pushes via a conveyor
    assert "conveyor cover (input)" in guide  # the sink end pulls via a conveyor
    assert "lay along:" in guide


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
