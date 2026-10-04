"""Tests for the one reading of how a single block's outputs leave it (``output_faces``, #249).

GT auto-outputs a single block through exactly one face (a basic machine's ``mFacing``, a Super
Tank's front, an Item Filter's back) and a Super Chest through none, so every other output face is a
cover. The previewer's arrows and cover markers and the ``.schematic`` export's facing both read it
from here, which is what these pin.
"""

from __future__ import annotations

import pytest

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
    MEDeviceKind,
    MEEndpoint,
    Net,
    Placement,
    Port,
    RelativeFace,
    Route,
    Terminal,
)
from gtnh_solver.output_faces import (
    _FACE_ORDER,
    BlockOutputs,
    CoverFace,
    output_faces,
    output_side_takes_input,
)
from gtnh_solver.router._grid import FACE_ORDER
from tests._me_fixtures import attached_line, gt_hatch_line, me_net
from tests._me_fixtures import endpoint as me_endpoint

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
    *,
    pack_version: str | None = None,
) -> dict[str, BlockOutputs]:
    problem = InputIR(
        bounding_region=CellBox(sx=8, sy=4, sz=8),
        machines=machines,
        nets=nets,
        pack_version=pack_version,
    )
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


@pytest.mark.parametrize(
    ("rate_a", "rate_b", "expected"),
    [(0.25, 0.5, 0.75), (0.25, None, None), (None, None, None)],
)
def test_a_cover_face_carries_the_summed_rate_of_its_ports(
    rate_a: float | None, rate_b: float | None, expected: float | None
) -> None:
    """What picks the cover's tier: every port leaving through the face, summed, and unknown when
    any of them is, since a partial sum would undersize the cover."""
    machines = [
        _block(
            "m", [_out("output:auto"), _out("output:a", rate=rate_a), _out("output:b", rate=rate_b)]
        ),
        _block("s", [_in("in")]),
        _block("c", [_in("in:a"), _in("in:b")]),
    ]
    nets = [
        _net("ns", ("m", "output:auto"), ("s", "in"), Commodity.ITEM),
        _net("na", ("m", "output:a"), ("c", "in:a"), Commodity.ITEM),
        _net("nb", ("m", "output:b"), ("c", "in:b"), Commodity.ITEM),
    ]
    auto = AutoConnection(
        net_id="ns",
        source_machine_id="m",
        source_face=Facing.SOUTH,
        target_machine_id="s",
        target_face=Facing.NORTH,
    )
    routes = [
        _pipe(
            nets[1],
            Commodity.ITEM,
            ("m", "output:a", Facing.EAST),
            ("c", "in:a", Facing.WEST),
            (2, 1, 1),
        ),
        _pipe(
            nets[2],
            Commodity.ITEM,
            ("m", "output:b", Facing.EAST),
            ("c", "in:b", Facing.WEST),
            (2, 1, 1),
        ),
    ]
    got = _solve(
        machines,
        nets,
        [_place("m", 1, 1, 1), _place("s", 1, 1, 2), _place("c", 3, 1, 1)],
        routes=routes,
        autos=[auto],
    )
    (cover,) = got["m"].covers
    assert cover.face is Facing.EAST
    assert cover.rate == expected


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


# ------------------------------------------------- input through the output face (#278)


@pytest.mark.parametrize(
    ("pack", "takes"),
    [
        ("2.9.0-beta-2", True),
        ("2.9.0", True),
        ("2.10.1", True),
        ("3.0.0", True),
        ("2.8.4", False),
        (None, True),  # unknown: say the state to reach rather than nothing
        ("nightly", True),
    ],
)
def test_a_new_basic_machine_takes_input_through_its_output_face_from_29(
    pack: str | None, takes: bool
) -> None:
    assert output_side_takes_input(pack) is takes


_SHARED = CellCoord(x=1, y=0, z=0)


def _sharing(
    a_type: str = "Macerator",
    *,
    a_filters: tuple[str, ...] = (),
    a_on_pipe: bool = True,
    autos: list[AutoConnection] = (),  # type: ignore[assignment]
    pack_version: str | None = "2.9.0-beta-2",
) -> dict[str, BlockOutputs]:
    """``a`` and ``b`` both feed ``x`` into one Super Chest ``c``, by one pipe at (1, 0, 0) that
    docks ``a``'s east face (unless ``a_on_pipe`` is off) and ``b``'s west face."""
    machines = [
        _block("a", [_out("output:x")], type_=a_type, filter_items=a_filters),
        _block("b", [_out("output:x")]),
        _block("c", [_in("input:x")], type_="Super Chest"),
    ]
    net = Net(
        id="n",
        commodity=Commodity.ITEM,
        fluid_or_item="x",
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id="a", port_id="output:x"),
            MachineFaceRef(machine_id="b", port_id="output:x"),
            MachineFaceRef(machine_id="c", port_id="input:x"),
        ],
    )
    terminals = [
        Terminal(machine_id="b", port_id="output:x", face=Facing.WEST, cell=_SHARED),
        Terminal(machine_id="c", port_id="input:x", face=Facing.NORTH, cell=_SHARED),
    ]
    if a_on_pipe:
        terminals.insert(
            0, Terminal(machine_id="a", port_id="output:x", face=Facing.EAST, cell=_SHARED)
        )
    pipe = Route(net_id="n", commodity=Commodity.ITEM, terminals=terminals, segments=[])
    placements = [_place("a", 0, 0, 0), _place("b", 2, 0, 0), _place("c", 1, 0, 1)]
    return _solve(machines, [net], placements, [pipe], autos, pack_version=pack_version)


def test_machines_whose_outputs_share_a_pipe_must_refuse_input_through_it_on_29() -> None:
    """Each one's output face is on a pipe that carries the other's output, and a GT pipe delivers
    to any inventory that takes the stack: on 2.9 a new basic machine takes it through its output
    face, so one run dry jams on a sibling's product (#278)."""
    got = _sharing()
    assert got["a"].forbid_input_from_output
    assert got["b"].forbid_input_from_output


def test_a_28_plan_marks_nothing() -> None:
    # 2.8.4's basic machines refuse input through the output face by default; the same screwdriver
    # click would ALLOW it, so a 2.8.4 build must not be told to make it.
    got = _sharing(pack_version="2.8.4")
    assert not got["a"].forbid_input_from_output
    assert not got["b"].forbid_input_from_output


def test_a_machine_alone_on_its_output_pipe_is_not_marked() -> None:
    machines = [_block("m", [_out("output:x")]), _block("c", [_in("input:x")])]
    net = _net("x", ("m", "output:x"), ("c", "input:x"), Commodity.ITEM)
    pipe = _pipe(
        net,
        Commodity.ITEM,
        ("m", "output:x", Facing.EAST),
        ("c", "input:x", Facing.WEST),
        (1, 0, 0),
    )
    placements = [_place("m", 0, 0, 0), _place("c", 2, 0, 0)]
    got = _solve(machines, [net], placements, [pipe], pack_version="2.9.0-beta-2")
    assert not got["m"].forbid_input_from_output


def test_only_a_basic_machine_is_marked() -> None:
    # An Item Filter refuses items at its back with no setting to change, so it is never told to set
    # one; the machine beside it still is, since the filter's output passes its output face.
    got = _sharing("Ultra Low Voltage Item Filter", a_filters=("x",))
    assert not got["a"].forbid_input_from_output
    assert got["b"].forbid_input_from_output


def test_a_machine_ejecting_straight_into_its_consumer_is_not_on_the_pipe() -> None:
    # #270: ``a`` stands against the chest and auto-outputs into it, so its output face touches the
    # chest, not the pipe, and ``b`` is the only machine feeding that pipe. Neither is marked.
    auto = AutoConnection(
        net_id="n",
        source_machine_id="a",
        source_face=Facing.SOUTH,
        target_machine_id="c",
        target_face=Facing.NORTH,
    )
    got = _sharing(a_on_pipe=False, autos=[auto])
    assert got["a"].auto_face is Facing.SOUTH
    assert not got["a"].forbid_input_from_output
    assert not got["b"].forbid_input_from_output


# ------------------------------------------------------------------ an ME interface (#335)


def _attached_with(out: MEEndpoint, *extra_ports: Port) -> tuple[InputIR, LayoutResult]:
    """The ME fixtures' attached line, with machine ``a``'s product built as ``out`` instead of an
    interface, on the same cable cell, and ``extra_ports`` added to ``a``, each on a net on ME."""
    problem, layout = attached_line()
    a = problem.machines[0]
    machine = a.model_copy(
        update={
            "faces": a.faces.model_copy(update={"ports": [*a.faces.ports, *extra_ports]}),
            "me_endpoints": (a.me_endpoints[0], out),
        }
    )
    nets = [
        *problem.nets,
        *(
            me_net(p.id, ("a", p.id)).model_copy(update={"commodity": p.commodity})
            for p in extra_ports
        ),
    ]
    problem = InputIR.model_validate(
        {
            **problem.model_dump(),
            "machines": [machine.model_dump(), *(m.model_dump() for m in problem.machines[1:])],
            "nets": [n.model_dump() for n in nets],
        }
    )
    network = layout.me_networks[0]
    devices = [
        d.model_copy(update={"endpoint_id": out.id, "kind": out.device.kind})
        if d.endpoint_id == "out"
        else d
        for d in network.devices
    ]
    layout = layout.model_copy(
        update={"me_networks": [network.model_copy(update={"devices": devices})]}
    )
    return problem, layout


def test_an_me_interface_takes_the_blocks_push_through_its_face() -> None:
    # The interface part sits east of its cable cell, against ``a``'s west face.
    problem, layout = attached_line()
    got = output_faces(problem, layout)
    assert got["a"] == BlockOutputs(
        machine_id="a", auto_face=Facing.WEST, auto_items=True, auto_fluids=False, covers=()
    )
    assert "b" not in got  # nothing leaves b on a face: its only port is an input


def test_a_dual_interface_takes_items_and_fluids_through_one_face() -> None:
    dual = me_endpoint("out", ("out", "fout"), MEDeviceKind.DUAL_INTERFACE)
    problem, layout = _attached_with(dual, _out("fout", Commodity.FLUID))
    got = output_faces(problem, layout)["a"]
    assert got.auto_face is Facing.WEST
    assert got.auto_items
    assert got.auto_fluids


def test_an_output_an_import_bus_pulls_leaves_through_no_face() -> None:
    pulled = me_endpoint("out", ("out",), MEDeviceKind.IMPORT_BUS)
    problem, layout = _attached_with(pulled)
    assert "a" not in output_faces(problem, layout)


def test_a_multiblocks_interface_faces_a_hatch_not_a_face_of_the_block() -> None:
    problem, layout = gt_hatch_line(normal=True)
    assert output_faces(problem, layout) == {}
