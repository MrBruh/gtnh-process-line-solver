"""Tests for the one reading of how a single block's outputs leave it (``output_faces``, #249).

GT auto-outputs a single block through exactly one face (a basic machine's ``mFacing``, a Super
Tank's front, an Item Filter's back) and a Super Chest through none, so every other output face is a
cover. The previewer's arrows and cover markers and the ``.schematic`` export's facing both read it
from here, which is what these pin.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    AutoConnection,
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
    RelativeFace,
    Route,
    Terminal,
)
from gtnh_solver.output_faces import _FACE_ORDER, BlockOutputs, CoverFace, output_faces
from gtnh_solver.router._grid import FACE_ORDER

_SOUTH_OF_ORIGIN = (0, 0, 1)


def _block(
    mid: str,
    ports: list[Port],
    *,
    type_: str = "Macerator",
    footprint: CellBox | None = None,
    filter_items: tuple[str, ...] = (),
) -> Machine:
    return Machine(
        id=mid,
        type=type_,
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=footprint or CellBox(),
        faces=FaceSpec(ports=ports),
        filter_items=filter_items,
    )


def _out(pid: str, commodity: Commodity = Commodity.ITEM, **kw: object) -> Port:
    return Port.model_validate(
        {"id": pid, "commodity": commodity, "direction": IODirection.OUTPUT, **kw}
    )


def _in(pid: str, commodity: Commodity = Commodity.ITEM) -> Port:
    return Port(id=pid, commodity=commodity, direction=IODirection.INPUT)


def _net(nid: str, src: tuple[str, str], dst: tuple[str, str], commodity: Commodity) -> Net:
    return Net(
        id=nid,
        commodity=commodity,
        fluid_or_item=None if commodity is Commodity.POWER else nid,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=src[0], port_id=src[1]),
            MachineFaceRef(machine_id=dst[0], port_id=dst[1]),
        ],
    )


def _pipe(
    net: Net,
    commodity: Commodity,
    src: tuple[str, str, Facing],
    dst: tuple[str, str, Facing],
    cell: tuple[int, int, int],
) -> Route:
    """A one-block pipe at ``cell`` wired to both ends' faces."""
    at = CellCoord(x=cell[0], y=cell[1], z=cell[2])
    return Route(
        net_id=net.id,
        commodity=commodity,
        terminals=[
            Terminal(machine_id=src[0], port_id=src[1], face=src[2], cell=at),
            Terminal(machine_id=dst[0], port_id=dst[1], face=dst[2], cell=at),
        ],
        segments=[],
    )


def _place(mid: str, x: int, y: int, z: int, facing: Facing = Facing.NORTH) -> Placement:
    return Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=facing)


def _solve(
    machines: list[Machine],
    nets: list[Net],
    placements: list[Placement],
    routes: list[Route] = (),  # type: ignore[assignment]
    autos: list[AutoConnection] = (),  # type: ignore[assignment]
) -> dict[str, BlockOutputs]:
    problem = InputIR(bounding_region=CellBox(sx=8, sy=4, sz=8), machines=machines, nets=nets)
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        routes=list(routes),
        auto_connections=list(autos),
    )
    return output_faces(problem, layout)


def test_the_face_order_is_the_routers() -> None:
    assert _FACE_ORDER == FACE_ORDER


def test_an_auto_connection_is_the_auto_face() -> None:
    machines = [_block("m", [_out("out")]), _block("c", [_in("in")])]
    nets = [_net("n", ("m", "out"), ("c", "in"), Commodity.ITEM)]
    auto = AutoConnection(
        net_id="n",
        source_machine_id="m",
        source_face=Facing.EAST,
        target_machine_id="c",
        target_face=Facing.WEST,
    )
    got = _solve(machines, nets, [_place("m", 1, 1, 1), _place("c", 2, 1, 1)], autos=[auto])
    assert got["m"] == BlockOutputs("m", Facing.EAST, True, False, ())
    assert "c" not in got  # a block with no output leaving it is absent


def test_a_second_piped_output_face_takes_a_conveyor() -> None:
    # Two item outputs piped out of two faces: GT auto-outputs through one, and the other needs a
    # conveyor cover, which is what the previewer marks and the export warns about.
    machines = [
        _block("m", [_out("output:a"), _out("output:b")]),
        _block("a", [_in("in")]),
        _block("b", [_in("in")]),
    ]
    nets = [
        _net("na", ("m", "output:a"), ("a", "in"), Commodity.ITEM),
        _net("nb", ("m", "output:b"), ("b", "in"), Commodity.ITEM),
    ]
    routes = [
        _pipe(
            nets[0],
            Commodity.ITEM,
            ("m", "output:a", Facing.EAST),
            ("a", "in", Facing.WEST),
            (2, 1, 1),
        ),
        _pipe(
            nets[1],
            Commodity.ITEM,
            ("m", "output:b", Facing.SOUTH),
            ("b", "in", Facing.NORTH),
            (1, 1, 2),
        ),
    ]
    got = _solve(
        machines,
        nets,
        [_place("m", 1, 1, 1), _place("a", 3, 1, 1), _place("b", 1, 1, 3)],
        routes=routes,
    )
    # A tie, one output each: the routers' face order puts south first.
    assert got["m"].auto_face is Facing.SOUTH
    assert got["m"].covers == (CoverFace(Facing.EAST, "conveyor", ("na",)),)


def test_an_auto_connection_wins_over_a_piped_output() -> None:
    machines = [
        _block("m", [_out("output:a"), _out("output:b")]),
        _block("a", [_in("in")]),
        _block("b", [_in("in")]),
    ]
    nets = [
        _net("na", ("m", "output:a"), ("a", "in"), Commodity.ITEM),
        _net("nb", ("m", "output:b"), ("b", "in"), Commodity.ITEM),
    ]
    auto = AutoConnection(
        net_id="na",
        source_machine_id="m",
        source_face=Facing.EAST,
        target_machine_id="a",
        target_face=Facing.WEST,
    )
    route = _pipe(
        nets[1],
        Commodity.ITEM,
        ("m", "output:b", Facing.SOUTH),
        ("b", "in", Facing.NORTH),
        (1, 1, 2),
    )
    got = _solve(
        machines,
        nets,
        [_place("m", 1, 1, 1), _place("a", 2, 1, 1), _place("b", 1, 1, 3)],
        routes=[route],
        autos=[auto],
    )
    assert got["m"].auto_face is Facing.EAST
    assert got["m"].covers == (CoverFace(Facing.SOUTH, "conveyor", ("nb",)),)


def test_a_fluid_on_a_cover_face_takes_a_pump() -> None:
    machines = [
        _block("m", [_out("output:a"), _out("output:w", Commodity.FLUID)]),
        _block("a", [_in("in")]),
        _block("t", [_in("in", Commodity.FLUID)], type_="Super Tank"),
    ]
    nets = [
        _net("na", ("m", "output:a"), ("a", "in"), Commodity.ITEM),
        _net("nw", ("m", "output:w"), ("t", "in"), Commodity.FLUID),
    ]
    auto = AutoConnection(
        net_id="na",
        source_machine_id="m",
        source_face=Facing.EAST,
        target_machine_id="a",
        target_face=Facing.WEST,
    )
    route = _pipe(
        nets[1], Commodity.FLUID, ("m", "output:w", Facing.UP), ("t", "in", Facing.DOWN), (1, 2, 1)
    )
    got = _solve(
        machines,
        nets,
        [_place("m", 1, 1, 1), _place("a", 2, 1, 1), _place("t", 1, 3, 1)],
        routes=[route],
        autos=[auto],
    )
    assert (got["m"].auto_items, got["m"].auto_fluids) == (True, False)
    assert got["m"].covers == (CoverFace(Facing.UP, "pump", ("nw",)),)


def test_a_super_chest_never_auto_outputs() -> None:
    # MTEDigitalChestBase has no output at all: taking items out of one is always a conveyor.
    machines = [_block("s", [_out("out")], type_="Super Chest"), _block("c", [_in("in")])]
    nets = [_net("n", ("s", "out"), ("c", "in"), Commodity.ITEM)]
    auto = AutoConnection(
        net_id="n",
        source_machine_id="s",
        source_face=Facing.EAST,
        target_machine_id="c",
        target_face=Facing.WEST,
    )
    got = _solve(machines, nets, [_place("s", 1, 1, 1), _place("c", 2, 1, 1)], autos=[auto])
    assert got["s"] == BlockOutputs(
        "s", None, False, False, (CoverFace(Facing.EAST, "conveyor", ("n",)),)
    )


def test_a_super_tank_auto_outputs_through_one_face() -> None:
    machines = [
        _block("t", [_out("out", Commodity.FLUID)], type_="Super Tank"),
        _block("c", [_in("in", Commodity.FLUID)]),
    ]
    nets = [_net("n", ("t", "out"), ("c", "in"), Commodity.FLUID)]
    route = _pipe(
        nets[0], Commodity.FLUID, ("t", "out", Facing.EAST), ("c", "in", Facing.WEST), (2, 1, 1)
    )
    got = _solve(machines, nets, [_place("t", 1, 1, 1), _place("c", 3, 1, 1)], routes=[route])
    assert got["t"] == BlockOutputs("t", Facing.EAST, False, True, ())


def test_an_item_filter_auto_outputs_through_its_back() -> None:
    item_filter = _block(
        "f",
        [
            Port(id="input:r", commodity=Commodity.ITEM, direction=IODirection.INPUT),
            _out("output:r", faces=(RelativeFace.BACK,)),
        ],
        type_="Ultra Low Voltage Item Filter",
        filter_items=("r",),
    )
    machines = [item_filter, _block("c", [_in("in")])]
    nets = [_net("n", ("f", "output:r"), ("c", "in"), Commodity.ITEM)]
    route = _pipe(
        nets[0],
        Commodity.ITEM,
        ("f", "output:r", Facing.SOUTH),
        ("c", "in", Facing.NORTH),
        (1, 1, 2),
    )
    got = _solve(machines, nets, [_place("f", 1, 1, 1), _place("c", 1, 1, 3)], routes=[route])
    assert got["f"].auto_face is Facing.SOUTH
    assert got["f"].covers == ()


def test_power_multiblocks_and_unplaced_blocks_are_left_out() -> None:
    machines = [
        _block("src", [_out("power:out", Commodity.POWER)], type_="Power Source (LV)"),
        _block(
            "m",
            [_in("power:in", Commodity.POWER), _out("out")],
            footprint=CellBox(sx=3, sy=3, sz=3),
        ),
        _block("c", [_in("in")]),
        _block("ghost", [_out("out")]),
    ]
    nets = [
        _net("p", ("src", "power:out"), ("m", "power:in"), Commodity.POWER),
        _net("n", ("m", "out"), ("c", "in"), Commodity.ITEM),
    ]
    power = Route(
        net_id="p",
        commodity=Commodity.POWER,
        terminals=[
            Terminal(
                machine_id="src",
                port_id="power:out",
                face=Facing.EAST,
                cell=CellCoord(x=1, y=0, z=0),
            ),
            Terminal(
                machine_id="m", port_id="power:in", face=Facing.WEST, cell=CellCoord(x=1, y=0, z=0)
            ),
        ],
        segments=[],
        thickness_per_segment=[],
    )
    item = _pipe(
        nets[1], Commodity.ITEM, ("m", "out", Facing.EAST), ("c", "in", Facing.WEST), (5, 1, 1)
    )
    got = _solve(
        machines,
        nets,
        [_place("src", 0, 0, 0), _place("m", 2, 0, 0), _place("c", 6, 1, 1)],
        routes=[power, item],
    )
    assert got == {}  # power is no output, the multiblock ejects from hatches, ghost is unplaced


def test_the_sand_line_auto_outputs_every_hammer_and_covers_its_chest(
    solved_sand: tuple[InputIR, LayoutResult],
) -> None:
    ir, layout = solved_sand
    got = output_faces(ir, layout)
    types = {m.id: m.type for m in ir.machines}
    hammers = [b for mid, b in got.items() if types[mid] == "Forge Hammer"]
    assert hammers
    assert all(b.auto_face is not None and b.covers == () for b in hammers)
    chests = [b for mid, b in got.items() if types[mid] == "Super Chest"]
    assert chests
    assert all(b.auto_face is None and b.covers for b in chests)
