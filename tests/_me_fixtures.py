"""Hand-built ME (AE2) builds the validator's ME gate is held to (#333).

Each builder returns a ``(problem, layout)`` pair the gate must pass, built by hand from AE2's rules
(docs/DOMAIN.md, "What a valid ME build is") rather than by the router, so a router bug cannot make
both sides agree. A test breaks one rule at a time on top of them and expects its code.

- :func:`attached_line`: two single blocks on the player's main network, fed by export buses, one
  pushing its product into an interface; a dense stub on the west edge where the main network
  enters. A tree::

      z=1    .    [I]   A    .    B          A, B: machines facing north (z=0 is their front)
      z=2  STUB  c(1)  c(2) c(3) c(4)        [I] at (1,0,1): an interface part facing east, into A
                  |                           c(2) and c(4) carry an export bus facing north
                 (1,0,1)

- :func:`comb`: ``n`` single blocks along both sides of one cable row, each fed by an export bus:
  the row is where the channel limits bite (8 on a smart cable, 9 ad hoc, the budget).
- :func:`gt_hatch_line`: a multiblock whose product leaves through GT's Output Bus (ME), front
  facing a cable from the stub; or, with ``normal=True``, a normal output bus with an interface part
  in front of it.
- :func:`acceptor_comb`: a two-machine subnet :func:`comb` powered by an Energy Acceptor on the
  row's east end, fed by a power source through two blocks of cable (#336)::

      z=0   (C)  m0    .    .    .    .    .        C: the controller, with ``with_controller``
      z=1    c  c(2)  c(0) c(0) c(0) c(0)  ACC      c: a smart cable carrying both channels
      z=2    .   m1    .    .    .    .    #        #: LV cable, from the source's east face to the
      z=3    .    .    .    .   SRC   #    #           acceptor's south face; SRC's front faces south
"""

from __future__ import annotations

from gtnh_solver.ir import (
    AEColor,
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
    InputIR,
    IODirection,
    LayoutResult,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    MECableCell,
    MECableKind,
    MECards,
    MEConfig,
    MEDeviceKind,
    MEDeviceSpec,
    MEEndpoint,
    MEMode,
    MENetworkLayout,
    MENetworkSpec,
    MEPlacedDevice,
    MEPower,
    MERole,
    Net,
    PlacedHatch,
    Placement,
    Port,
    Route,
    Segment,
    Terminal,
)

MAIN = "main"
SUB = "sub"


def coord(x: int, y: int, z: int) -> CellCoord:
    return CellCoord(x=x, y=y, z=z)


def at(machine_id: str, x: int, y: int, z: int, facing: Facing) -> Placement:
    return Placement(machine_id=machine_id, cell=coord(x, y, z), orientation=facing)


def cable(x: int, y: int, z: int, kind: MECableKind = MECableKind.SMART) -> MECableCell:
    return MECableCell(cell=coord(x, y, z), kind=kind)


def endpoint(
    endpoint_id: str,
    ports: tuple[str, ...],
    kind: MEDeviceKind,
    *,
    network: str = MAIN,
    cards: MECards | None = None,
    gt_mid: int | None = None,
    hatch_kind: str | None = None,
    share: float = 1.0,
    config: tuple[str, ...] = (),
) -> MEEndpoint:
    return MEEndpoint(
        id=endpoint_id,
        network=network,
        ports=ports,
        device=MEDeviceSpec(kind=kind, gt_mid=gt_mid, cards=cards or MECards(), config=config),
        hatch_kind=hatch_kind,
        share=share,
    )


def device(
    machine_id: str,
    built: MEEndpoint,
    cell: tuple[int, int, int],
    side: Facing,
) -> MEPlacedDevice:
    """The device that builds ``built`` as specified, at ``cell`` facing ``side``."""
    return MEPlacedDevice(
        machine_id=machine_id,
        endpoint_id=built.id,
        kind=built.device.kind,
        cell=coord(*cell),
        side=side,
        gt_mid=built.device.gt_mid,
        cards=built.device.cards,
        config=built.device.config,
    )


def item_port(port_id: str, direction: IODirection, rate: float = 1.0) -> Port:
    return Port(id=port_id, commodity=Commodity.ITEM, direction=direction, rate=rate)


def stub(machine_id: str = "stub", *, network: str = MAIN, facing: Facing = Facing.WEST) -> Machine:
    """An attach stub: the dense cable the main network enters through, front facing outside."""
    return Machine(
        id=machine_id,
        type="ME Dense Smart Cable",
        voltage_tier="LV",
        orientation_options=[facing],
        me_role=MERole.ATTACH,
        me_network=network,
        outside_front=True,
    )


def controller(machine_id: str = "ctrl", *, network: str = SUB) -> Machine:
    return Machine(
        id=machine_id,
        type="ME Controller",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        me_role=MERole.CONTROLLER,
        me_network=network,
    )


def single(
    machine_id: str, ports: list[Port], endpoints: list[MEEndpoint], *facings: Facing
) -> Machine:
    return Machine(
        id=machine_id,
        type="t",
        voltage_tier="LV",
        orientation_options=list(facings or (Facing.NORTH,)),
        faces=FaceSpec(ports=ports),
        me_endpoints=tuple(endpoints),
    )


def me_net(net_id: str, *ends: tuple[str, str], network: str = MAIN) -> Net:
    return Net(
        id=net_id,
        commodity=Commodity.ITEM,
        fluid_or_item=net_id,
        throughput=1.0,
        endpoints=[MachineFaceRef(machine_id=m, port_id=p) for m, p in ends],
        me_network=network,
    )


def attached_line() -> tuple[InputIR, LayoutResult]:
    """The module docstring's attached line: valid as built."""
    feed_a = endpoint("feed", ("in",), MEDeviceKind.EXPORT_BUS, cards=MECards(acceleration=1))
    out_a = endpoint("out", ("out",), MEDeviceKind.INTERFACE)
    feed_b = endpoint("feed", ("in",), MEDeviceKind.EXPORT_BUS, cards=MECards(acceleration=1))
    a = single(
        "a",
        [item_port("in", IODirection.INPUT), item_port("out", IODirection.OUTPUT)],
        [feed_a, out_a],
        Facing.NORTH,
        Facing.SOUTH,
    )
    b = single("b", [item_port("in", IODirection.INPUT)], [feed_b])
    problem = InputIR(
        bounding_region=CellBox(sx=6, sy=2, sz=4),
        machines=[a, b, stub()],
        nets=[me_net("stone", ("a", "in")), me_net("mid", ("a", "out"), ("b", "in"))],
        me=MEConfig(networks=[MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED)]),
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            at("stub", 0, 0, 2, Facing.WEST),
            at("a", 2, 0, 1, Facing.NORTH),
            at("b", 4, 0, 1, Facing.NORTH),
        ],
        me_networks=[
            MENetworkLayout(
                id=MAIN,
                colour=AEColor.FLUIX,
                cables=[
                    cable(0, 0, 2, MECableKind.DENSE),
                    cable(1, 0, 2),
                    cable(2, 0, 2),
                    cable(3, 0, 2),
                    cable(4, 0, 2),
                    cable(1, 0, 1),
                ],
                devices=[
                    device("a", feed_a, (2, 0, 2), Facing.NORTH),
                    device("a", out_a, (1, 0, 1), Facing.EAST),
                    device("b", feed_b, (4, 0, 2), Facing.NORTH),
                ],
            )
        ],
    )
    return problem, layout


#: The comb's machine slots, in the order :func:`comb` fills them: north of the row, then south,
#: one column at a time.
_COMB_SLOTS = [(x, z) for x in range(1, 6) for z in (0, 2)]


def comb(
    n: int,
    *,
    mode: MEMode = MEMode.ATTACHED,
    budget: int = 32,
    with_controller: bool = False,
) -> tuple[InputIR, LayoutResult]:
    """``n`` (1 to 10) single blocks along a cable row at z = 1, each fed by an export bus.

    Attached: a dense stub at (0, 0, 1) on the west edge. A subnet: (0, 0, 1) is a plain smart
    cable, with a controller at (0, 0, 0) when ``with_controller``, and ad hoc otherwise.
    """
    network = MAIN if mode is MEMode.ATTACHED else SUB
    machines: list[Machine] = []
    nets: list[Net] = []
    placements: list[Placement] = []
    devices: list[MEPlacedDevice] = []
    for i, (x, z) in enumerate(_COMB_SLOTS[:n]):
        mid = f"m{i}"
        facing = Facing.NORTH if z == 0 else Facing.SOUTH
        feed = endpoint("feed", ("in",), MEDeviceKind.EXPORT_BUS, network=network)
        machines.append(single(mid, [item_port("in", IODirection.INPUT, rate=0.1)], [feed], facing))
        nets.append(me_net(f"n{i}", (mid, "in"), network=network))
        placements.append(at(mid, x, 0, z, facing))
        devices.append(device(mid, feed, (x, 0, 1), Facing.NORTH if z == 0 else Facing.SOUTH))
    cables = [cable(x, 0, 1) for x in range(1, 6)]
    if mode is MEMode.ATTACHED:
        machines.append(stub(network=network))
        placements.append(at("stub", 0, 0, 1, Facing.WEST))
        cables.insert(0, cable(0, 0, 1, MECableKind.DENSE))
        spec = MENetworkSpec(id=network, mode=mode, me_channel_budget=budget)
        colour = AEColor.FLUIX
    else:
        cables.insert(0, cable(0, 0, 1))
        spec = MENetworkSpec(id=network, mode=mode, colour=AEColor.ORANGE)
        colour = AEColor.ORANGE
        if with_controller:
            machines.append(controller(network=network))
            placements.append(at("ctrl", 0, 0, 0, Facing.NORTH))
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=3),
        machines=machines,
        nets=nets,
        me=MEConfig(networks=[spec]),
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=placements,
        me_networks=[MENetworkLayout(id=network, colour=colour, cables=cables, devices=devices)],
    )
    return problem, layout


def gt_hatch_line(*, normal: bool = False, subnet: bool = False) -> tuple[InputIR, LayoutResult]:
    """A 2x1x1 multiblock at (1, 0, 1) facing north, its product leaving through the casing cell at
    (1, 0, 1): GT's Output Bus (ME) with its front south onto a cable from the stub, or with
    ``normal`` a normal output bus there and an interface part on that cable facing back at it.
    With ``subnet`` the network is an orange ad-hoc subnet instead, its two cables smart and no
    stub: nothing of its own stores energy for a flush."""
    if normal:
        out = endpoint("out", ("out",), MEDeviceKind.INTERFACE, hatch_kind="OutputBus")
    else:
        out = endpoint(
            "out", ("out",), MEDeviceKind.GT_OUTPUT_BUS_ME, gt_mid=2710, hatch_kind="OutputBus"
        )
    mb = Machine(
        id="mb",
        type="Multiblock",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=2, sy=1, sz=1),
        faces=FaceSpec(ports=[item_port("out", IODirection.OUTPUT)]),
        hatch_slots=(
            HatchSlot(offset=coord(0, 0, 0), kinds=("OutputBus",)),
            HatchSlot(offset=coord(1, 0, 0), kinds=("InputBus",)),
        ),
        hatch_cells=2,
        me_endpoints=(out,),
    )
    spec = (
        MENetworkSpec(id=MAIN, mode=MEMode.SUBNET, colour=AEColor.ORANGE)
        if subnet
        else MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED)
    )
    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=1, sz=4),
        machines=[mb] if subnet else [mb, stub()],
        nets=[me_net("prod", ("mb", "out"))],
        me=MEConfig(networks=[spec]),
    )
    built = (
        device("mb", out, (1, 0, 2), Facing.NORTH)
        if normal
        else device("mb", out, (1, 0, 1), Facing.SOUTH)
    )
    layout = LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=[
            *([] if subnet else [at("stub", 0, 0, 2, Facing.WEST)]),
            at("mb", 1, 0, 1, Facing.NORTH),
        ],
        hatches=[
            PlacedHatch(
                machine_id="mb",
                kind="OutputBus",
                cell=coord(1, 0, 1),
                facing=Facing.SOUTH,
                port_id="out",
            )
        ],
        me_networks=[
            MENetworkLayout(
                id=MAIN,
                colour=AEColor.ORANGE if subnet else AEColor.FLUIX,
                cables=[
                    cable(0, 0, 2, MECableKind.SMART if subnet else MECableKind.DENSE),
                    cable(1, 0, 2),
                ],
                devices=[built],
            )
        ],
    )
    return problem, layout


def acceptor_comb(
    *, with_controller: bool = False, eut: float = 30.0
) -> tuple[InputIR, LayoutResult]:
    """A subnet :func:`comb` of two machines powered by an Energy Acceptor rated ``eut`` EU/t
    (module docstring), its cables carrying the channels AE routes through them. Only a subnet
    takes an acceptor: an attached network is the player's base's to power.

    What the network draws, by hand (spike 6): two export buses idle at 1 AE/t each and extract
    0.1 items/t each. Ad hoc, every node (six cables, two buses, the acceptor) pays for both
    channels: 9 x 2 = 18, so (2 + 18/128 + 0.2) x 10 = 23.40625 AE/t, 11.703125 EU/t. With the
    controller (3 AE/t idle), the channel term is twice the channels through every node, the root
    cable and the cable holding both buses 2 each, the buses 1 each: 2 x 6 = 12, so (5 + 12/128 +
    0.2) x 10 = 52.9375 AE/t, 26.46875 EU/t.
    """
    problem, layout = comb(2, mode=MEMode.SUBNET, with_controller=with_controller)
    (network,) = layout.me_networks
    spec = problem.me.networks[0].model_copy(update={"power": MEPower.ACCEPTOR})
    power_in = Port(id="power:in", commodity=Commodity.POWER, direction=IODirection.INPUT)
    acceptor = Machine(
        id="acc",
        type="ME Energy Acceptor",
        voltage_tier="LV",
        eut=eut,
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[power_in]),
        me_role=MERole.ACCEPTOR,
        me_network=network.id,
    )
    source = Machine(
        id="src",
        type="Power Source (LV)",
        voltage_tier="LV",
        orientation_options=[Facing.SOUTH],
        faces=FaceSpec(
            ports=[Port(id="power:out", commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
    power = Net(
        id="power:LV",
        commodity=Commodity.POWER,
        throughput=eut,
        endpoints=[
            MachineFaceRef(machine_id="src", port_id="power:out"),
            MachineFaceRef(machine_id="acc", port_id="power:in"),
        ],
    )
    problem = InputIR(
        bounding_region=CellBox(sx=7, sy=1, sz=4),
        machines=[*problem.machines, acceptor, source],
        nets=[*problem.nets, power],
        me=MEConfig(networks=[spec]),
    )
    # Both buses sit on the cable at x = 1, so the cells from the root to there carry both.
    cables = [
        c.model_copy(update={"me_channels": 2 if c.cell.x <= 1 else 0}) for c in network.cables
    ]
    route = Route(
        net_id="power:LV",
        commodity=Commodity.POWER,
        terminals=[
            Terminal(machine_id="src", port_id="power:out", face=Facing.EAST, cell=coord(5, 0, 3)),
            Terminal(machine_id="acc", port_id="power:in", face=Facing.SOUTH, cell=coord(6, 0, 2)),
        ],
        segments=[
            Segment(start=coord(5, 0, 3), end=coord(6, 0, 3), channel=0),
            Segment(start=coord(6, 0, 3), end=coord(6, 0, 2), channel=0),
        ],
        thickness_per_segment=[1, 1],
    )
    layout = layout.model_copy(
        update={
            "placements": [
                *layout.placements,
                at("acc", 6, 0, 1, Facing.NORTH),
                at("src", 4, 0, 3, Facing.SOUTH),
            ],
            "routes": [route],
            "me_networks": [network.model_copy(update={"cables": cables})],
        }
    )
    return problem, layout
