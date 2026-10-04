"""Tests for the boundary/power derivation (``system_io``) the previewer renders.

Mostly against the real sand line (the artifact the previewer renders), plus a hand-built case for
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
    MEConfig,
    MEDeviceKind,
    MEMode,
    MERole,
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
    MEFlow,
    MENetworkIO,
    SystemIO,
    is_boundary_storage,
    net_label,
    net_resource,
    port_resource,
    resource_label,
    system_io,
)
from tests._me_fixtures import (
    MAIN,
    SUB,
    attached_line,
    comb,
    item_port,
    me_net,
    single,
)
from tests._me_fixtures import endpoint as me_endpoint

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
    # Each hammer loads ~0.53 A at its delivered voltage (depths 2/3/4: 16/30 + 16/29 + 16/28
    # = 1.66); the tier rounds up ONCE to 2 A - not the 3 A per-machine rounding would charge.
    assert io.power_amps_by_tier == {"LV": 2}


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
        BoundaryFlow("chest", "Super Chest", (0, 0, 0), "thing", ("thing",), Commodity.ITEM, None)
    ]
    # dangling output on a plain (non-``{dir}:``-prefixed) port id -> id used verbatim
    assert io.outputs == [
        BoundaryFlow("maker", "Maker", (2, 0, 0), "out", ("out",), Commodity.ITEM, None)
    ]
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
    assert io.power_amps_by_source == {"src": 2}  # one source on the tier: the two agree


def _lv_source(mid: str) -> Machine:
    return Machine(
        id=mid,
        type="Power Source (LV)",
        voltage_tier="LV",
        eut=0.0,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="po", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )


def _lv_sink(mid: str, eut: float) -> Machine:
    return Machine(
        id=mid,
        type="M",
        voltage_tier="LV",
        eut=eut,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="pi", commodity=Commodity.POWER, direction=IODirection.INPUT)]
        ),
    )


def test_amps_are_attributed_per_source_not_per_tier() -> None:
    # A tier that outgrows one cable run gets several sources (adapter.power), and each is fed
    # separately. The per-source figure must charge each source only its OWN net: billing both for
    # the tier total would tell the builder to feed 13 A twice for a 13 A tier.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=4),
        machines=[
            _lv_source("src-a"),
            _lv_source("src-b"),
            _lv_sink("m0", 320.0),  # 10 A at LV (32 V)
            _lv_sink("m1", 96.0),  # 3 A
        ],
        nets=[
            Net(
                id="pw-a",
                commodity=Commodity.POWER,
                throughput=320.0,
                endpoints=[
                    MachineFaceRef(machine_id="src-a", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi"),
                ],
            ),
            Net(
                id="pw-b",
                commodity=Commodity.POWER,
                throughput=96.0,
                endpoints=[
                    MachineFaceRef(machine_id="src-b", port_id="po"),
                    MachineFaceRef(machine_id="m1", port_id="pi"),
                ],
            ),
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id=mid, cell=CellCoord(x=i, y=0, z=0), orientation=Facing.NORTH)
            for i, mid in enumerate(["src-a", "src-b", "m0", "m1"])
        ],
        routes=[],  # no cable: every machine sits at distance 0, so loss is out of the picture
    )
    io = system_io(problem, layout)
    assert io.power_amps_by_source == {"src-a": 10, "src-b": 3}
    assert io.power_amps_by_tier == {"LV": 13}  # the tier-wide total, rounded once


def _lv_hatched_sink(mid: str, eut: float, hatches: int) -> Machine:
    """A sink taking ``eut`` through ``hatches`` energy hatches, each carrying an equal share."""
    share = eut / hatches
    return Machine(
        id=mid,
        type="M",
        voltage_tier="LV",
        eut=eut,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(
                    id=f"pi{i}",
                    commodity=Commodity.POWER,
                    direction=IODirection.INPUT,
                    rate=share,
                    max_amps=2.0,
                )
                for i in range(hatches)
            ]
        ),
    )


def test_one_machines_hatches_bill_their_own_sources_separately() -> None:
    # A machine's energy hatches can sit on different nets, so no one source owns its whole draw.
    # Attribution is per connection: each source is billed only the hatches it actually feeds, so
    # here the 256 EU/t machine splits 4 A and 4 A rather than putting 8 A on either source.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=4),
        machines=[_lv_source("src-a"), _lv_source("src-b"), _lv_hatched_sink("m0", 256.0, 2)],
        nets=[
            Net(
                id="pw-a",
                commodity=Commodity.POWER,
                throughput=128.0,
                endpoints=[
                    MachineFaceRef(machine_id="src-a", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi0"),
                ],
            ),
            Net(
                id="pw-b",
                commodity=Commodity.POWER,
                throughput=128.0,
                endpoints=[
                    MachineFaceRef(machine_id="src-b", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi1"),
                ],
            ),
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id=mid, cell=CellCoord(x=i, y=0, z=0), orientation=Facing.NORTH)
            for i, mid in enumerate(["src-a", "src-b", "m0"])
        ],
        routes=[],  # no cable: every machine sits at distance 0, so loss is out of the picture
    )
    io = system_io(problem, layout)
    assert io.power_amps_by_source == {"src-a": 4, "src-b": 4}
    assert io.power_amps_by_tier == {"LV": 8}  # the machine's whole 256 EU/t, counted once
    assert io.power_total == 256.0


def test_power_net_without_one_source_is_left_out_of_the_per_source_amps() -> None:
    # Two sources on one shared-amperage net is not certifiable (the validator rejects it), so
    # there is no single source to bill the load to. The summary skips the attribution rather than
    # guessing one, and still reports the tier total.
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=4),
        machines=[_lv_source("src-a"), _lv_source("src-b"), _lv_sink("m0", 320.0)],
        nets=[
            Net(
                id="pw",
                commodity=Commodity.POWER,
                throughput=320.0,
                endpoints=[
                    MachineFaceRef(machine_id="src-a", port_id="po"),
                    MachineFaceRef(machine_id="src-b", port_id="po"),
                    MachineFaceRef(machine_id="m0", port_id="pi"),
                ],
            )
        ],
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id=mid, cell=CellCoord(x=i, y=0, z=0), orientation=Facing.NORTH)
            for i, mid in enumerate(["src-a", "src-b", "m0"])
        ],
        routes=[],
    )
    io = system_io(problem, layout)
    assert io.power_amps_by_source == {}
    assert io.power_amps_by_tier == {"LV": 10}


def test_helper_predicates() -> None:
    assert is_boundary_storage("Super Tank")
    assert not is_boundary_storage("Forge Hammer")
    out = Port(id="output:minecraft:sand", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)
    assert port_resource(out) == "minecraft:sand"
    bare = Port(id="widget", commodity=Commodity.ITEM, direction=IODirection.OUTPUT)
    assert port_resource(bare) == "widget"  # no ``output:`` prefix -> used as-is


def test_a_net_is_labelled_by_what_it_carries_a_merged_run_by_all_of_it() -> None:
    """A merged item run (#249) names no single item, so its label lists every item in the pipe in
    the order the net gives them; a one-item net and a power net read exactly as before."""
    here = [MachineFaceRef(machine_id="m", port_id="p")]
    merged = Net(
        id="t", commodity=Commodity.ITEM, items=("a", "b", "c"), throughput=0.3, endpoints=here
    )
    single = Net(
        id="s", commodity=Commodity.ITEM, fluid_or_item="a", throughput=0.1, endpoints=here
    )
    power = Net(id="p", commodity=Commodity.POWER, throughput=8.0, endpoints=here)
    assert net_resource(merged) == "a, b, c"
    assert net_resource(single) == "a"
    assert net_resource(power) is None


def _storage(mid: str, port_id: str, commodity: Commodity, direction: IODirection) -> Machine:
    return Machine(
        id=mid,
        type="Super Tank" if commodity is Commodity.FLUID else "Super Chest",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[Port(id=port_id, commodity=commodity, direction=direction)]),
    )


def test_a_boundary_flow_lists_each_resource_it_carries_one_by_one() -> None:
    """``resources`` is what a consumer that looks each resource up reads (#297): a merged run's
    several items one by one, and a fluid whose id holds a comma as the one id it is, where the
    joined ``resource`` label would split it in two."""
    feed = _storage("feed", "output:items", Commodity.ITEM, IODirection.OUTPUT)
    tank = _storage("tank", "output:1,3dimethylbenzene", Commodity.FLUID, IODirection.OUTPUT)
    bin_ = _storage("bin", "input:items", Commodity.ITEM, IODirection.INPUT)
    mixer = Machine(
        id="mixer",
        type="Mixer",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[
                Port(id="in", commodity=Commodity.ITEM, direction=IODirection.INPUT),
                Port(id="fluid", commodity=Commodity.FLUID, direction=IODirection.INPUT),
                Port(id="out", commodity=Commodity.ITEM, direction=IODirection.OUTPUT),
            ]
        ),
    )
    nets = [
        Net(
            id="run",
            commodity=Commodity.ITEM,
            items=("a", "b"),
            throughput=0.2,
            endpoints=[
                MachineFaceRef(machine_id="feed", port_id="output:items"),
                MachineFaceRef(machine_id="mixer", port_id="in"),
            ],
        ),
        Net(
            id="xylene",
            commodity=Commodity.FLUID,
            fluid_or_item="1,3dimethylbenzene",
            throughput=5.0,
            endpoints=[
                MachineFaceRef(machine_id="tank", port_id="output:1,3dimethylbenzene"),
                MachineFaceRef(machine_id="mixer", port_id="fluid"),
            ],
        ),
        Net(
            id="product",
            commodity=Commodity.ITEM,
            items=("c", "d"),
            throughput=0.1,
            endpoints=[
                MachineFaceRef(machine_id="mixer", port_id="out"),
                MachineFaceRef(machine_id="bin", port_id="input:items"),
            ],
        ),
    ]
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=2, sz=8), machines=[feed, tank, bin_, mixer], nets=nets
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            Placement(machine_id=m.id, cell=CellCoord(x=2 * i, y=0, z=0), orientation=Facing.NORTH)
            for i, m in enumerate(problem.machines)
        ],
    )
    io = system_io(problem, layout)
    carried = {f.machine_id: (f.resource, f.resources) for f in [*io.inputs, *io.outputs]}
    assert carried == {
        "feed": ("a, b", ("a", "b")),
        "tank": ("1,3dimethylbenzene", ("1,3dimethylbenzene",)),
        "bin": ("c, d", ("c", "d")),
    }


def test_a_resource_is_labelled_by_its_plan_name_with_its_id_beside_it() -> None:
    """The label a person reads (#296): the plan's display name, then the raw id a builder searches
    NEI for. With no name, or a name that only repeats the id, the id alone, as before."""
    names = {"liquid_toluene": "Toluene", "water": "water"}
    assert resource_label("liquid_toluene", names) == "Toluene (liquid_toluene)"
    assert resource_label("benzene", names) == "benzene"  # the plan names it nothing
    assert resource_label("water", names) == "water"  # the name adds nothing to the id
    assert resource_label("liquid_toluene", {}) == "liquid_toluene"


def test_a_net_label_names_each_resource_in_the_pipe() -> None:
    here = [MachineFaceRef(machine_id="m", port_id="p")]
    merged = Net(
        id="t", commodity=Commodity.ITEM, items=("a", "b", "c"), throughput=0.3, endpoints=here
    )
    power = Net(id="p", commodity=Commodity.POWER, throughput=8.0, endpoints=here)
    names = {"a": "Alpha", "c": "Gamma"}
    assert net_label(merged, names) == "Alpha (a), b, Gamma (c)"
    assert net_label(power, names) is None


# ------------------------------------------------------------------ what each ME network asks (#335)


def test_an_attached_network_spends_a_main_channel_per_device_and_names_its_stock() -> None:
    # ``stone`` feeds a from storage (nothing in the line makes it); ``mid`` runs a -> b inside.
    problem, layout = attached_line()
    (network,) = system_io(problem, layout).me
    assert network == MENetworkIO(
        network=MAIN,
        mode=MEMode.ATTACHED,
        supplies=(MEFlow("stone", ("stone",), Commodity.ITEM, 1.0),),
        absorbs=(),
        devices=3,
        main_channels=3,
    )


def test_a_link_subnet_spends_a_main_channel_per_link_and_stores_what_it_makes() -> None:
    problem, layout = comb(2, mode=MEMode.SUBNET)
    link = Machine(
        id="link",
        type="ME Smart Cable",
        voltage_tier="LV",
        orientation_options=[Facing.WEST],
        me_role=MERole.LINK,
        me_network=SUB,
        outside_front=True,
        me_endpoints=(me_endpoint("bus", (), MEDeviceKind.STORAGE_BUS, network=SUB),),
    )
    made = single("maker", [item_port("out", IODirection.OUTPUT)], [])
    problem = InputIR.model_validate(
        {
            **problem.model_dump(),
            "machines": [m.model_dump() for m in (*problem.machines, link, made)],
            "nets": [
                *(n.model_dump() for n in problem.nets),
                me_net("product", ("maker", "out"), network=SUB).model_dump(),
            ],
        }
    )
    (network,) = system_io(problem, layout).me
    assert network.mode is MEMode.SUBNET
    assert network.devices == 3  # two export buses and the link's storage bus
    assert network.main_channels == 1
    assert [f.resource for f in network.supplies] == ["n0", "n1"]
    assert [f.resource for f in network.absorbs] == ["product"]


def test_a_subnet_with_no_link_spends_no_main_channel() -> None:
    problem, layout = comb(2, mode=MEMode.SUBNET)
    (network,) = system_io(problem, layout).me
    assert network.main_channels == 0


def test_a_line_with_no_me_network_asks_nothing() -> None:
    problem, layout = attached_line()
    plain = problem.model_copy(update={"me": MEConfig()})
    assert system_io(plain, layout).me == ()
