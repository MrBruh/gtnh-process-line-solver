"""Placement treats an ME device like a pipe's terminal (#335).

A net riding ME is not piped, but the device serving its port still takes a face of its machine and
a cell beside it: a part on the cable in front of the face it works through, or a cable in front of
a GT ME hatch. So the crowding gate, the single-block face count and the annealer's face term all
charge one face per device (a Dual Interface carrying two ports is one device). And the annealer
pulls an ME network's machines together, stub included, the way it pulls a net's, so the cable tree
the router lays is short.
"""

from __future__ import annotations

from gtnh_solver.ir import (
    AEColor,
    CellBox,
    Commodity,
    Facing,
    InputIR,
    IODirection,
    Machine,
    MEConfig,
    MEDeviceKind,
    MEMode,
    MENetworkSpec,
    MERole,
    Net,
    Placement,
    Port,
    RelativeFace,
)
from gtnh_solver.ir.nets import me_device_ports, me_network_machines
from gtnh_solver.placement import crowded_machines, optimize_placement, single_block_shortfalls
from gtnh_solver.placement.search import _bodies, _cost_nets, _net_adjacency
from tests._helpers import at, hub_line, on_me
from tests._me_fixtures import MAIN, SUB, attached_line, endpoint, item_port, me_net, single


def _networks(*ids: str) -> MEConfig:
    """``ids`` as ME networks: :data:`MAIN` attached, any other a subnet of a colour of its own."""
    colours = iter(c for c in AEColor if c is not AEColor.FLUIX)
    return MEConfig(
        networks=[
            MENetworkSpec(id=nid, mode=MEMode.ATTACHED)
            if nid == MAIN
            else MENetworkSpec(id=nid, mode=MEMode.SUBNET, colour=next(colours))
            for nid in dict.fromkeys(ids)
        ]
    )


def _me_line(*networks: str, region: CellBox | None = None) -> InputIR:
    """A single block ``m<i>`` per entry of ``networks``, pushing its product into an interface on
    that network: one device each, so one face and one dock cell each."""
    return InputIR(
        bounding_region=region if region is not None else CellBox(sx=3, sy=1, sz=1),
        machines=[
            single(
                f"m{i}",
                [item_port("out", IODirection.OUTPUT)],
                [endpoint("o", ("out",), MEDeviceKind.INTERFACE, network=network)],
            )
            for i, network in enumerate(networks)
        ],
        nets=[
            me_net(f"p{i}", (f"m{i}", "out"), network=network) for i, network in enumerate(networks)
        ],
        me=_networks(*networks),
    )


def _with_devices(problem: InputIR, machine_id: str, *devices: tuple[str, ...]) -> InputIR:
    """``problem`` with one ME device on ``machine_id`` per entry of ``devices``, serving those new
    item ports on :data:`MAIN`: a Dual Interface where an entry names two ports, else an
    interface."""
    machines = []
    for machine in problem.machines:
        if machine.id == machine_id:
            ports = [item_port(p, IODirection.OUTPUT) for served in devices for p in served]
            built = [
                endpoint(
                    f"d{i}",
                    served,
                    MEDeviceKind.DUAL_INTERFACE if len(served) > 1 else MEDeviceKind.INTERFACE,
                )
                for i, served in enumerate(devices)
            ]
            machine = machine.model_copy(
                update={
                    "faces": machine.faces.model_copy(
                        update={"ports": [*machine.faces.ports, *ports]}
                    ),
                    "me_endpoints": tuple(built),
                }
            )
        machines.append(machine)
    nets: list[Net] = [
        *problem.nets,
        *(me_net(f"me-{p}", (machine_id, p)) for served in devices for p in served),
    ]
    return InputIR.model_validate(
        {
            **problem.model_dump(),
            "machines": [m.model_dump() for m in machines],
            "nets": [n.model_dump() for n in nets],
            "me": _networks(MAIN).model_dump(),
        }
    )


def _link(machine_id: str = "link", *, network: str = SUB) -> Machine:
    """A link: a cable bus on the edge whose one storage bus faces out of the build."""
    return Machine(
        id=machine_id,
        type="ME Smart Cable",
        voltage_tier="LV",
        orientation_options=[Facing.WEST],
        me_role=MERole.LINK,
        me_network=network,
        outside_front=True,
        me_endpoints=(endpoint("bus", (), MEDeviceKind.STORAGE_BUS, network=network),),
    )


# ------------------------------------------------------------------ what docks


def test_each_device_docks_through_the_first_port_it_serves() -> None:
    dual = _with_devices(hub_line(1), "hub", ("mi", "mf"), ("x",)).machines[0]
    # The Dual Interface's second port rides along on its face: one device, one dock.
    assert me_device_ports(dual) == [("mi", MAIN), ("x", MAIN)]


def test_a_links_storage_bus_docks_nothing_on_the_line() -> None:
    # It serves no port of the line and faces out of the build, at the player's interface.
    assert me_device_ports(_link()) == []


# ------------------------------------------------------------------ the single-block face count


def test_an_me_device_takes_a_face_like_a_pipe() -> None:
    assert single_block_shortfalls(_with_devices(hub_line(4), "hub", ("x",))) == {}
    assert single_block_shortfalls(_with_devices(hub_line(5), "hub", ("x",))) == {"hub": 6}


def test_a_dual_interface_serving_two_ports_takes_one_face() -> None:
    # What the one-device rule is for (#329): an item and a fluid output into one Dual Interface on
    # the output face, where two interfaces would need a face each.
    assert single_block_shortfalls(_with_devices(hub_line(4), "hub", ("mi", "mf"))) == {}
    assert single_block_shortfalls(_with_devices(hub_line(4), "hub", ("mi",), ("mf",))) == {
        "hub": 6
    }


def test_a_net_on_me_with_no_device_still_takes_no_face() -> None:
    # The pre-#335 shorthand (``on_me``) names no device, so nothing docks.
    assert single_block_shortfalls(on_me(hub_line(7), Commodity.FLUID)) == {}


def test_a_pinned_device_port_competes_for_its_face() -> None:
    # A pinned block is judged by its pins: the device's port and a piped port pinned to one face
    # cannot both have it.
    def pinned(device_face: RelativeFace) -> InputIR:
        block = single(
            "m",
            [
                Port(
                    id="in",
                    commodity=Commodity.ITEM,
                    direction=IODirection.INPUT,
                    faces=(RelativeFace.BACK,),
                ),
                Port(
                    id="out",
                    commodity=Commodity.ITEM,
                    direction=IODirection.OUTPUT,
                    faces=(device_face,),
                ),
            ],
            [endpoint("o", ("out",), MEDeviceKind.INTERFACE)],
        )
        source = single("s", [item_port("out", IODirection.OUTPUT)], [])
        return InputIR(
            bounding_region=CellBox(sx=4, sy=1, sz=4),
            machines=[block, source],
            nets=[
                Net.model_validate(
                    {
                        "id": "feed",
                        "commodity": "item",
                        "fluid_or_item": "feed",
                        "throughput": 1.0,
                        "endpoints": [
                            {"machine_id": "s", "port_id": "out"},
                            {"machine_id": "m", "port_id": "in"},
                        ],
                    }
                ),
                me_net("product", ("m", "out")),
            ],
            me=_networks(MAIN),
        )

    assert single_block_shortfalls(pinned(RelativeFace.LEFT)) == {}
    assert single_block_shortfalls(pinned(RelativeFace.BACK)) == {"m": 2}


# ------------------------------------------------------------------ the crowding gate


def test_an_me_device_needs_a_cell_to_dock_on() -> None:
    # Its net is not piped, but its part still needs a cable in front of the face it works through.
    walled_in = _me_line(MAIN, region=CellBox(sx=1, sy=1, sz=1))
    assert crowded_machines(walled_in, [at("m0", 0, 0, 0)]) == ("m0",)


def test_devices_on_one_network_may_share_a_cable_cell() -> None:
    # Two parts on one cable, each facing its own machine: the router's tap.
    one_network = _me_line(MAIN, MAIN)
    assert crowded_machines(one_network, [at("m0", 0, 0, 0), at("m1", 2, 0, 0)]) == ()


def test_devices_on_different_networks_may_not() -> None:
    # A cable cell belongs to one network: two networks' cables never touch, let alone share.
    two_networks = _me_line(MAIN, SUB)
    assert crowded_machines(two_networks, [at("m0", 0, 0, 0), at("m1", 2, 0, 0)]) == ("m0", "m1")


def test_two_devices_on_one_machine_need_two_cells() -> None:
    # A cable cell touches a single block through one side only, so it holds one of its parts.
    problem = _with_devices(_me_line(MAIN, region=CellBox(sx=2, sy=1, sz=1)), "m0", ("x",))
    problem = problem.model_copy(
        update={
            "machines": [
                m.model_copy(
                    update={
                        "me_endpoints": (
                            endpoint("o", ("out",), MEDeviceKind.INTERFACE),
                            *m.me_endpoints,
                        )
                    }
                )
                for m in problem.machines
            ]
        }
    )
    assert crowded_machines(problem, [at("m0", 0, 0, 0)]) == ("m0",)
    roomy = problem.model_copy(update={"bounding_region": CellBox(sx=3, sy=1, sz=1)})
    assert crowded_machines(roomy, [at("m0", 1, 0, 0)]) == ()


# ------------------------------------------------------------------ the annealer


def test_the_face_term_charges_one_face_per_device() -> None:
    bodies = _bodies(_with_devices(hub_line(1), "hub", ("mi", "mf"), ("x",)))
    assert bodies["hub"].port_ids == ("out0", "mi", "x")
    # A net on ME with no device behind it docks nothing, so it is charged nothing.
    assert _bodies(on_me(hub_line(1), Commodity.FLUID))["hub"].port_ids == ()


def test_an_me_network_is_pulled_together_with_its_stub() -> None:
    problem, _ = attached_line()
    assert me_network_machines(problem) == {MAIN: ["a", "b", "stub"]}
    wire_nets, power_nets = _cost_nets(problem, {})
    # Both nets ride ME, so the network's one pull replaces them; the stub is in it.
    assert wire_nets == [(["a", "b", "stub"], 1.0)]
    assert power_nets == []


def test_a_network_of_one_machine_pulls_nothing() -> None:
    wire_nets, _ = _cost_nets(_me_line(MAIN, SUB), {})
    assert wire_nets == []


def test_an_me_network_relates_its_machines_for_the_ruin() -> None:
    # The stub shares no net with anything, yet ruining near it should reach what it feeds.
    adjacency = _net_adjacency(attached_line()[0])
    assert adjacency["stub"] == {"a", "b"}
    assert adjacency["b"] == {"a", "stub"}


def test_an_attached_line_anneals_to_a_placement_the_gate_passes() -> None:
    problem, _ = attached_line()
    result = optimize_placement(problem, seed=0, max_iterations=200)
    assert result.ok
    assert crowded_machines(problem, result.placements) == ()


# ------------------------------------------------------------------ ME blocks the router would refuse


def _stub_at(machine_id: str, network: str = MAIN) -> Machine:
    return Machine(
        id=machine_id,
        type="ME Dense Smart Cable",
        voltage_tier="LV",
        orientation_options=[Facing.WEST],
        me_role=MERole.ATTACH,
        me_network=network,
        outside_front=True,
    )


def _blocks(*machines: Machine) -> InputIR:
    """``machines`` (ME blocks) on a 1x1x3 strip of the west edge, with every network they name."""
    ids = list(dict.fromkeys(m.me_network for m in machines if m.me_network is not None))
    return InputIR(
        bounding_region=CellBox(sx=1, sy=1, sz=3), machines=list(machines), me=_networks(*ids)
    )


def _strip(*ids: str) -> list[Placement]:
    return [at(mid, 0, 0, z, orientation=Facing.WEST) for z, mid in enumerate(ids) if mid]


def test_two_links_of_one_subnet_side_by_side_are_turned_away() -> None:
    # A subnet moving items and fluids has two links; touching, AE joins them into a loop.
    problem = _blocks(_link("item"), _link("fluid"))
    assert crowded_machines(problem, _strip("item", "fluid")) == ("item", "fluid")
    assert crowded_machines(problem, _strip("item", "", "fluid")) == ()


def test_two_stubs_of_one_network_side_by_side_are_turned_away() -> None:
    problem = _blocks(_stub_at("s0"), _stub_at("s1"))
    assert crowded_machines(problem, _strip("s0", "s1")) == ("s0", "s1")


def test_blocks_of_two_networks_side_by_side_are_turned_away() -> None:
    problem = _blocks(_stub_at("s0"), _link("l0"))
    assert crowded_machines(problem, _strip("s0", "l0")) == ("s0", "l0")


def test_a_link_beside_its_own_controller_is_fine() -> None:
    controller = Machine(
        id="ctrl",
        type="ME Controller",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        me_role=MERole.CONTROLLER,
        me_network=SUB,
    )
    problem = _blocks(_link("l0"), controller)
    assert crowded_machines(problem, [*_strip("l0"), at("ctrl", 0, 0, 1)]) == ()
