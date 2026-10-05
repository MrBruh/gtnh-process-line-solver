"""The ME (AE2) cable-tree router (router.me, #334).

Headline: each ME network is laid as a forest of cable, one tree per place its channels enter (an
attach stub, a cell beside a controller, or one ad-hoc tree), with every device a leaf on it; a
new cable touches no AE block but the one it grows from (the halo), so the graph AE builds from the
blocks is exactly the forest the router meant. Every count is AE's own on a tree, and a budget
the network can never meet is refused before anything is laid.

Two checks back every layout here. :func:`_check_layout` re-derives the router's own promises from
the blocks it returns (trees, roots, the halo, channel counts). The validator's ME gate (#333),
written without the router, then has to pass the same layout: the property test at the bottom
holds every network the router reports as laid to it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from gtnh_solver.dataset.me import CABLE_CAPACITY, PART_CABLES
from gtnh_solver.ir import (
    CellBox,
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
    MECableKind,
    MEConfig,
    MEDeviceKind,
    MEEndpoint,
    MEMode,
    MENetworkLayout,
    MENetworkSpec,
    MERole,
    Net,
    PlacedHatch,
    Placement,
    Port,
    RelativeFace,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, in_region, occupied_cells
from gtnh_solver.router import MERouteResult, route_me
from gtnh_solver.router import me as router_me
from gtnh_solver.validator import validate
from tests._helpers import property_examples
from tests._me_fixtures import MAIN, SUB, at, controller, coord, endpoint, stub

_ITEM, _FLUID = Commodity.ITEM, Commodity.FLUID
_IN, _OUT = IODirection.INPUT, IODirection.OUTPUT
_HORIZONTAL = (Facing.NORTH, Facing.SOUTH, Facing.EAST, Facing.WEST)
#: Every slot kind the test multiblocks take a hatch of: what an ME endpoint may need on them.
_IO_KINDS = ("OutputBus", "InputBus", "OutputHatch", "InputHatch")

#: A second subnet's id, for the property's lines with two.
SUB2 = "sub2"
ATTACHED = MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED)
#: A subnet: ad hoc, unless the problem gives it a controller.
SUBNET = MENetworkSpec(id=SUB, mode=MEMode.SUBNET)


# ------------------------------------------------------------------ building problems


def _port(
    port_id: str,
    direction: IODirection,
    commodity: Commodity = _ITEM,
    faces: tuple[RelativeFace, ...] | None = None,
) -> Port:
    """An unrated port, so the validator's rate rule has nothing to hold the device to."""
    return Port(id=port_id, commodity=commodity, direction=direction, faces=faces)


def _feed(port: str = "in", network: str = MAIN, endpoint_id: str = "feed") -> MEEndpoint:
    return endpoint(endpoint_id, (port,), MEDeviceKind.EXPORT_BUS, network=network)


def _take(port: str = "out", network: str = MAIN, endpoint_id: str = "out") -> MEEndpoint:
    return endpoint(endpoint_id, (port,), MEDeviceKind.INTERFACE, network=network)


def _gt_hatch(
    port: str, kind: MEDeviceKind, gt_mid: int, hatch_kind: str, network: str = MAIN
) -> MEEndpoint:
    """One of GT's own ME hatches serving ``port``, in a slot of ``hatch_kind``."""
    return endpoint(port, (port,), kind, network=network, gt_mid=gt_mid, hatch_kind=hatch_kind)


def _single(
    mid: str,
    endpoints: Sequence[MEEndpoint],
    ports: Sequence[Port] | None = None,
    facing: Facing = Facing.NORTH,
) -> Machine:
    """A single block with ``endpoints``; its ports default to one per endpoint port, an item
    input for an export bus and an item output otherwise."""
    if ports is None:
        ports = [
            _port(p, _IN if e.device.kind is MEDeviceKind.EXPORT_BUS else _OUT)
            for e in endpoints
            for p in e.ports
        ]
    return Machine(
        id=mid,
        type="t",
        voltage_tier="LV",
        orientation_options=[facing],
        faces=FaceSpec(ports=list(ports)),
        me_endpoints=tuple(endpoints),
    )


def _link(
    mid: str,
    kind: MEDeviceKind = MEDeviceKind.STORAGE_BUS,
    *,
    network: str = SUB,
    facing: Facing = Facing.WEST,
    extra: Sequence[MEEndpoint] = (),
) -> Machine:
    """A link: a cable on the region's edge with one storage bus on its front, facing out."""
    return Machine(
        id=mid,
        type="ME Cable",
        voltage_tier="LV",
        orientation_options=[facing],
        me_role=MERole.LINK,
        me_network=network,
        outside_front=True,
        me_endpoints=(endpoint("bus", (), kind, network=network), *extra),
    )


def _acceptor(mid: str = "acc", *, network: str = SUB) -> Machine:
    return Machine(
        id=mid,
        type="ME Energy Acceptor",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        me_role=MERole.ACCEPTOR,
        me_network=network,
    )


def _multiblock(mid: str, endpoints: Sequence[MEEndpoint], ports: Sequence[Port]) -> Machine:
    """A 2x2x2 multiblock facing north whose every casing cell takes an I/O hatch."""
    footprint = CellBox(sx=2, sy=2, sz=2)
    slots = tuple(
        HatchSlot(offset=coord(x, y, z), kinds=_IO_KINDS)
        for x in range(2)
        for y in range(2)
        for z in range(2)
    )
    return Machine(
        id=mid,
        type="Multiblock",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=footprint,
        faces=FaceSpec(ports=list(ports)),
        hatch_slots=slots,
        hatch_cells=len(slots),
        me_endpoints=tuple(endpoints),
    )


def _problem(
    region: CellBox,
    machines: Sequence[Machine],
    networks: Sequence[MENetworkSpec],
    reserved: Iterable[Cell] = (),
) -> InputIR:
    """The problem, with one net per machine port an ME endpoint serves, riding its network (a
    port split across two endpoints has one net)."""
    nets: list[Net] = []
    for machine in machines:
        ports = {p.id: p for p in machine.faces.ports}
        served = {port: e.network for e in machine.me_endpoints for port in e.ports}
        for port, network in served.items():
            nets.append(
                Net(
                    id=f"{machine.id}.{port}",
                    commodity=ports[port].commodity,
                    fluid_or_item=f"{machine.id}.{port}",
                    throughput=1.0,
                    endpoints=[MachineFaceRef(machine_id=machine.id, port_id=port)],
                    me_network=network,
                )
            )
    return InputIR(
        bounding_region=region,
        machines=list(machines),
        nets=nets,
        reserved_cells=[coord(*c) for c in reserved],
        me=MEConfig(networks=list(networks)),
    )


# ------------------------------------------------------------------ checking what is laid


def _neighbours(cell: Cell) -> list[Cell]:
    x, y, z = cell
    return [(x + dx, y + dy, z + dz) for dx, dy, dz in FACE_DELTAS.values()]


def _step(cell: Cell, face: Facing) -> Cell:
    dx, dy, dz = FACE_DELTAS[face]
    return (cell[0] + dx, cell[1] + dy, cell[2] + dz)


def _components(cells: set[Cell]) -> list[set[Cell]]:
    left, pieces = set(cells), []
    while left:
        stack = [left.pop()]
        piece = set(stack)
        while stack:
            for n in _neighbours(stack.pop()):
                if n in left:
                    left.discard(n)
                    piece.add(n)
                    stack.append(n)
        pieces.append(piece)
    return pieces


def _subtree_counts(piece: set[Cell], root: Cell, at_cell: Counter[Cell]) -> dict[Cell, int]:
    """Each cell's channel devices at or beyond it, on the tree ``piece`` rooted at ``root``."""
    order, parent = [root], {root: root}
    for cell in order:
        for n in _neighbours(cell):
            if n in piece and n not in parent:
                parent[n] = cell
                order.append(n)
    counts = {c: at_cell[c] for c in piece}
    for cell in reversed(order[1:]):
        counts[parent[cell]] += counts[cell]
    return counts


def _bodies(problem: InputIR, placements: Sequence[Placement]) -> dict[str, set[Cell]]:
    machines = {m.id: m for m in problem.machines}
    return {
        p.machine_id: set(occupied_cells(p.cell, machines[p.machine_id].footprint, p.orientation))
        for p in placements
    }


def _check_layout(
    problem: InputIR,
    placements: Sequence[Placement],
    result: MERouteResult,
    extra: Collection[Cell] = (),
) -> None:
    """The router's own promises, re-derived from the blocks it returns.

    For every network laid: its cable is in the region, on no machine (but its own stubs and
    links), reserved cell or other route; each piece is a tree with exactly one root (a stub, the
    one cell beside a controller, or, ad hoc, the network's only piece); no cable touches another
    network's cable or AE block; an acceptor is touched exactly once; no part sits on dense cable;
    each endpoint is built once; and each cable's ``me_channels`` is the device count of its
    subtree, within its kind's capacity. And every network with something to build is either
    laid or failed, all of them laid when the result is ok.
    """
    region = problem.bounding_region
    bodies = _bodies(problem, placements)
    every_body = set().union(*bodies.values()) if bodies else set()
    reserved = {c.as_tuple() for c in problem.reserved_cells}

    def infra(network_id: str, *roles: MERole) -> set[Cell]:
        return {
            c
            for m in problem.machines
            if m.me_network == network_id and m.id in bodies and (not roles or m.me_role in roles)
            for c in bodies[m.id]
        }

    laid = {n.id: n for n in result.networks}
    for network in result.networks:
        spec = problem.me.network(network.id)
        cells = network.cells()
        kinds = {c.cell.as_tuple(): c for c in network.cables}
        own_cables = infra(network.id, MERole.ATTACH, MERole.LINK)
        for cell in cells:
            assert in_region(cell, region), cell
            assert cell not in reserved, cell
            assert cell not in extra, cell
            assert cell not in every_body or cell in own_cables, cell
        foreign: set[Cell] = set()
        for other in problem.me.networks:
            if other.id != network.id:
                foreign |= infra(other.id)
                if other.id in laid:
                    foreign |= laid[other.id].cells()
        for cell in cells:
            assert not foreign.intersection(_neighbours(cell)), f"{cell} touches another network"

        stubs = infra(network.id, MERole.ATTACH)
        controllers = infra(network.id, MERole.CONTROLLER)
        pieces = _components(cells)
        roots: dict[int, Cell | None] = {}
        for i, piece in enumerate(pieces):
            edges = sum(1 for c in piece for n in _neighbours(c) if n in piece) // 2
            assert edges == len(piece) - 1, f"a piece of {network.id!r} is not a tree"
            if spec.mode is MEMode.ATTACHED:
                found = piece & stubs
            elif controllers:
                found = {c for c in piece if controllers.intersection(_neighbours(c))}
            else:
                assert len(pieces) == 1, "an ad-hoc network is one tree"
                found = set()
            if spec.mode is MEMode.ATTACHED or controllers:
                assert len(found) == 1, f"a piece of {network.id!r} has roots {found}"
            roots[i] = next(iter(found), None)
        for cell in stubs:
            assert kinds[cell].kind is MECableKind.DENSE

        for m in problem.machines:
            if m.me_network == network.id and m.me_role is MERole.ACCEPTOR:
                body = bodies[m.id]
                around = {n for c in body for n in _neighbours(c)} - body
                touches = len(around & cells) + bool(around & controllers)
                assert touches == (1 if cells or controllers else 0), f"{m.id} touched {touches}x"

        at_cell: Counter[Cell] = Counter()
        for device in network.devices:
            cell = device.cell.as_tuple()
            if device.gt_mid is None:
                assert kinds[cell].kind in PART_CABLES, f"a part on {kinds[cell].kind} at {cell}"
                at_cell[cell] += 1
            else:
                faced = _step(cell, device.side)
                assert faced in kinds, f"GT ME hatch at {cell} faces no cable"
                at_cell[faced] += 1
        wanted = {
            (m.id, e.id)
            for m in problem.machines
            for e in m.me_endpoints
            if e.network == network.id
        }
        built = Counter((d.machine_id, d.endpoint_id) for d in network.devices)
        assert set(built) == wanted, built
        assert set(built.values()) <= {1}, built

        for i, piece in enumerate(pieces):
            candidates = [roots[i]] if roots[i] is not None else sorted(piece)
            matches = []
            for root in candidates:
                assert root is not None
                counts = _subtree_counts(piece, root, at_cell)
                matches.append(all(kinds[c].me_channels == counts[c] for c in piece))
            assert any(matches), f"channel counts of {network.id!r} are not its subtrees'"
        for cable in network.cables:
            assert cable.me_channels <= CABLE_CAPACITY[cable.kind], cable

    with_work = [
        n.id
        for n in problem.me.networks
        if any(e.network == n.id for m in problem.machines for e in m.me_endpoints)
        or any(m.me_network == n.id for m in problem.machines)
    ]
    assert sorted([*laid, *result.failed_networks]) == sorted(with_work)
    assert result.ok is (not result.failed_networks)
    if result.ok:
        assert set(laid) == set(with_work)


def _hatches(problem: InputIR, result: MERouteResult) -> list[PlacedHatch]:
    """The hatch each ME terminal stands for, keyed by the casing cell behind it, the way the end-
    to-end build hands them to the hatch placer (#335). Never as a route's terminals: those are
    deduped by machine and port, which would drop one hatch of a port split across two endpoints.
    """
    machines = {m.id: m for m in problem.machines}
    hatches: dict[tuple[str, Cell], PlacedHatch] = {}
    for terminal in result.terminals:
        (kind,) = {
            e.hatch_kind
            for e in machines[terminal.machine_id].me_endpoints
            if terminal.port_id in e.ports and e.hatch_kind is not None
        }
        casing = _step(terminal.cell.as_tuple(), OPPOSITE_FACE[terminal.face])
        key = (terminal.machine_id, casing)
        assert key not in hatches, f"two ME hatches on one casing cell {key}"
        hatches[key] = PlacedHatch(
            machine_id=terminal.machine_id,
            kind=kind,
            cell=coord(*casing),
            facing=terminal.face,
            port_id=terminal.port_id,
        )
    return list(hatches.values())


def _layout(
    problem: InputIR, placements: Sequence[Placement], result: MERouteResult
) -> LayoutResult:
    """The layout a build of ``result`` is: its placements, its ME networks, and the hatches its
    terminals imply (:func:`_hatches`)."""
    return LayoutResult(
        status=LayoutStatus.VALID,
        seed=0,
        placements=list(placements),
        hatches=_hatches(problem, result),
        me_networks=list(result.networks),
    )


def _without(problem: InputIR, failed: Collection[str]) -> InputIR:
    """``problem`` with the ``failed`` networks gone: their endpoints and nets dropped, and their
    infrastructure kept as plain blocks, so the validator judges only what was laid. Each network
    left keeps the colour it was laid in: a subnet left unset is dealt the first colour no other
    asks for, which dropping one before it would otherwise change."""
    if not failed:
        return problem
    machines = []
    for m in problem.machines:
        update: dict[str, object] = {
            "me_endpoints": tuple(e for e in m.me_endpoints if e.network not in failed)
        }
        if m.me_network in failed:
            update |= {"me_role": None, "me_network": None, "me_endpoints": ()}
        machines.append(m.model_copy(update=update))
    return InputIR.model_validate(
        {
            **problem.model_dump(),
            "machines": [m.model_dump() for m in machines],
            "nets": [n.model_dump() for n in problem.nets if n.me_network not in failed],
            "me": MEConfig(
                networks=[
                    n.model_copy(update={"colour": problem.me.colour(n.id)})
                    if n.mode is MEMode.SUBNET
                    else n
                    for n in problem.me.networks
                    if n.id not in failed
                ]
            ).model_dump(),
        }
    )


def _assert_validates(
    problem: InputIR, placements: Sequence[Placement], result: MERouteResult
) -> None:
    judged = _without(problem, result.failed_networks)
    report = validate(judged, _layout(judged, placements, result))
    assert report.ok, str(report)


def _route(
    problem: InputIR,
    placements: Sequence[Placement],
    *,
    claimed_cells: Mapping[str, Collection[Cell]] | None = None,
    endpoint_faces: Mapping[tuple[str, str], Collection[Facing]] | None = None,
) -> MERouteResult:
    """``route_me``, with both checks run on whatever it lays."""
    result = route_me(
        problem,
        placements,
        claimed_cells=claimed_cells or {},
        endpoint_faces=endpoint_faces or {},
    )
    _check_layout(problem, placements, result)
    _assert_validates(problem, placements, result)
    return result


def _network(result: MERouteResult, network_id: str = MAIN) -> MENetworkLayout:
    return next(n for n in result.networks if n.id == network_id)


def _load(result: MERouteResult, cell: Cell, network_id: str = MAIN) -> int:
    return next(
        c.me_channels for c in _network(result, network_id).cables if c.cell == coord(*cell)
    )


# ------------------------------------------------------------------ the shapes a network takes


def test_an_attached_line_is_one_tree_from_its_stub() -> None:
    """The sand line in miniature: three single blocks fed by export buses, one handing its
    product to an interface, all on the main network through one stub on the west edge."""
    machines = [
        _single("m1", [_feed()]),
        _single("m2", [_feed(), _take()]),
        _single("m3", [_feed()]),
        stub(),
    ]
    placements = [
        at("m1", 1, 0, 1, Facing.NORTH),
        at("m2", 3, 0, 1, Facing.NORTH),
        at("m3", 5, 0, 1, Facing.NORTH),
        at("stub", 0, 0, 2, Facing.WEST),
    ]
    problem = _problem(CellBox(sx=7, sy=1, sz=4), machines, [ATTACHED])
    result = _route(problem, placements)

    assert result.ok
    assert result.terminals == ()
    network = _network(result)
    assert len(network.devices) == 4
    assert _load(result, (0, 0, 2)) == 4  # the stub carries every device
    stub_cable = next(c for c in network.cables if c.cell == coord(0, 0, 2))
    assert stub_cable.kind is MECableKind.DENSE
    assert {c.kind for c in network.cables if c is not stub_cable} == {MECableKind.SMART}
    bodies = _bodies(problem, placements)
    for device in network.devices:
        # A part faces its machine, and never through the machine's front (north).
        assert _step(device.cell.as_tuple(), device.side) in bodies[device.machine_id]
        assert device.side is not Facing.SOUTH


def test_two_machines_tap_one_shared_cell() -> None:
    """Two machines pinned to face one cell from either side: one cable, a part on each side."""
    machines = [
        _single("a", [_feed()], [_port("in", _IN, faces=(RelativeFace.RIGHT,))]),
        _single("b", [_feed()], [_port("in", _IN, faces=(RelativeFace.LEFT,))]),
        stub(facing=Facing.SOUTH),
    ]
    placements = [
        at("a", 0, 0, 1, Facing.NORTH),
        at("b", 2, 0, 1, Facing.NORTH),
        at("stub", 1, 0, 3, Facing.SOUTH),
    ]
    problem = _problem(CellBox(sx=3, sy=1, sz=4), machines, [ATTACHED])
    result = _route(problem, placements)

    network = _network(result)
    assert network.cells() == {(1, 0, 1), (1, 0, 2), (1, 0, 3)}
    assert {(d.cell.as_tuple(), d.side) for d in network.devices} == {
        ((1, 0, 1), Facing.WEST),
        ((1, 0, 1), Facing.EAST),
    }
    assert _load(result, (1, 0, 1)) == 2


def _fishbone() -> tuple[InputIR, list[Placement]]:
    """Twelve machines flanking three branches off one trunk: the trunk must carry all twelve,
    so no part may sit on it, though every machine at x = 1 is right beside it."""
    machines: list[Machine] = [stub()]
    placements = [at("stub", 0, 0, 2, Facing.WEST)]
    for x in (1, 3, 4, 6, 7, 9):
        for z in (1, 0):  # the trunk-side machine first, so the greedy pass docks it on the trunk
            mid = f"m{x}{z}"
            machines.append(_single(mid, [_feed()]))
            placements.append(at(mid, x, 0, z, Facing.NORTH))
    region = CellBox(sx=10, sy=1, sz=3)
    return _problem(region, machines, [ATTACHED], reserved=[(0, 0, 0), (0, 0, 1)]), placements


def test_a_trunk_past_eight_channels_is_dense_and_takes_no_part() -> None:
    problem, placements = _fishbone()
    result = _route(problem, placements)

    assert result.ok, result.infeasibility
    network = _network(result)
    assert len(network.devices) == 12
    assert _load(result, (0, 0, 2)) == 12
    heavy = {c.cell.as_tuple(): c for c in network.cables if c.me_channels > 8}
    assert (1, 0, 2) in heavy  # beside m11, but it carries all twelve
    assert all(c.kind is MECableKind.DENSE for c in heavy.values())
    assert not any(d.cell.as_tuple() in heavy for d in network.devices)


def test_a_part_that_saturates_the_trunk_is_moved_off_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fishbone's first pass docks m11 on the trunk, which then runs out at 8; the rip-up bars
    that cell from holding a part and the retry lays every machine."""
    passes: list[frozenset[Cell]] = []
    real = router_me._Grower

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object) -> None:
            super().__init__(*args)
            passes.append(self.no_part)

    monkeypatch.setattr(router_me, "_Grower", Spy)
    problem, placements = _fishbone()
    assert route_me(problem, placements).ok
    assert len(passes) >= 2
    assert passes[0] == frozenset()
    assert (1, 0, 2) in passes[-1]


def test_a_gt_me_hatch_takes_the_casing_and_faces_the_cable() -> None:
    gt = endpoint(
        "out", ("out",), MEDeviceKind.GT_OUTPUT_BUS_ME, gt_mid=2710, hatch_kind="OutputBus"
    )
    mb = _multiblock("mb", [gt], [_port("out", _OUT)])
    placements = [at("mb", 1, 0, 1, Facing.NORTH), at("stub", 0, 0, 3, Facing.WEST)]
    problem = _problem(CellBox(sx=4, sy=2, sz=4), [mb, stub()], [ATTACHED])
    result = _route(problem, placements)

    (device,) = _network(result).devices
    (terminal,) = result.terminals
    casing = device.cell.as_tuple()
    assert casing in _bodies(problem, placements)["mb"]
    assert device.side is terminal.face
    assert terminal.port_id == "out"
    assert _step(casing, device.side) == terminal.cell.as_tuple()
    assert terminal.cell.as_tuple() in _network(result).cells()
    (hatch,) = _layout(problem, placements, result).hatches
    assert (hatch.cell.as_tuple(), hatch.facing, hatch.kind) == (casing, terminal.face, "OutputBus")


def test_a_normal_hatch_gets_an_interface_in_front_of_it() -> None:
    take = endpoint("out", ("out",), MEDeviceKind.INTERFACE, hatch_kind="OutputBus")
    mb = _multiblock("mb", [take], [_port("out", _OUT)])
    placements = [at("mb", 1, 0, 1, Facing.NORTH), at("stub", 0, 0, 3, Facing.WEST)]
    problem = _problem(CellBox(sx=4, sy=2, sz=4), [mb, stub()], [ATTACHED])
    result = _route(problem, placements)

    (device,) = _network(result).devices
    (terminal,) = result.terminals
    assert device.cell == terminal.cell  # the part sits on the dock cell ...
    assert device.side is not terminal.face  # ... facing back at the hatch
    assert _step(terminal.cell.as_tuple(), device.side) in _bodies(problem, placements)["mb"]
    (hatch,) = _layout(problem, placements, result).hatches
    assert hatch.facing is terminal.face


def test_a_controller_subnet_roots_its_trees_beside_the_controller() -> None:
    machines = [
        _single("a", [_feed(network=SUB)]),
        _single("b", [_feed(network=SUB)]),
        controller(),
    ]
    placements = [
        at("a", 0, 0, 0, Facing.NORTH),
        at("b", 4, 0, 0, Facing.NORTH),
        at("ctrl", 2, 0, 2, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [SUBNET])
    result = _route(problem, placements)

    network = _network(result, SUB)
    assert network.colour is problem.me.colour(SUB)
    assert len(network.devices) == 2
    assert not network.cells() & {(2, 0, 2)}  # the controller is a block, not a cable


def _adhoc_comb(n: int) -> tuple[InputIR, list[Placement]]:
    """``n`` single blocks along both sides of a row, on a subnet with no controller."""
    slots = [(x, z) for x in range(1, 6) for z in (0, 2)]
    machines, placements = [], []
    for i, (x, z) in enumerate(slots[:n]):
        facing = Facing.NORTH if z == 0 else Facing.SOUTH
        machines.append(_single(f"m{i}", [_feed(network=SUB)], facing=facing))
        placements.append(at(f"m{i}", x, 0, z, facing))
    return _problem(CellBox(sx=7, sy=1, sz=3), machines, [SUBNET]), placements


def test_an_adhoc_subnet_of_eight_is_one_smart_tree() -> None:
    problem, placements = _adhoc_comb(8)
    result = _route(problem, placements)

    network = _network(result, SUB)
    assert len(network.devices) == 8
    assert {c.kind for c in network.cables} == {MECableKind.SMART}


def test_a_ninth_adhoc_device_is_refused() -> None:
    problem, placements = _adhoc_comb(9)
    result = _route(problem, placements)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "me_adhoc"
    assert result.failed_networks == (SUB,)
    assert result.networks == ()


def test_an_attached_network_over_its_budget_is_refused() -> None:
    machines = [_single("a", [_feed()]), _single("b", [_feed()]), stub()]
    spec = MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED, me_channel_budget=1)
    problem = _problem(CellBox(sx=5, sy=1, sz=3), machines, [spec])
    result = route_me(problem, [at("stub", 0, 0, 1, Facing.WEST)])
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "me_channel_budget"


def test_more_than_a_stub_carries_is_refused() -> None:
    machines = [_single(f"m{i}", [_feed()]) for i in range(33)] + [stub()]
    spec = MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED, me_channel_budget=64)
    problem = _problem(CellBox(sx=5, sy=1, sz=3), machines, [spec])
    result = route_me(problem, [at("stub", 0, 0, 1, Facing.WEST)])
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "me_channels"


def test_two_networks_side_by_side_keep_a_cell_apart() -> None:
    """An attached network and a controller subnet serving neighbouring machines: neither's cable
    touches the other's cable or the controller, whatever their colours."""
    machines = [
        _single("a", [_feed()]),
        _single("b", [_feed(network=SUB)]),
        _single("c", [_feed()]),
        stub(),
        controller(),
    ]
    placements = [
        at("a", 1, 0, 1, Facing.NORTH),
        at("b", 2, 0, 1, Facing.NORTH),
        at("c", 3, 0, 1, Facing.NORTH),
        at("stub", 0, 0, 2, Facing.WEST),
        at("ctrl", 2, 0, 4, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=5, sy=2, sz=5), machines, [ATTACHED, SUBNET])
    result = _route(problem, placements)

    assert result.ok, result.infeasibility
    main, sub = _network(result, MAIN).cells(), _network(result, SUB).cells()
    assert not any(n in sub for c in main for n in _neighbours(c))


def test_each_link_is_a_smart_leaf_with_its_bus_on_its_front() -> None:
    machines = [
        _single("a", [_feed(network=SUB)]),
        controller(),
        _link("items", MEDeviceKind.STORAGE_BUS, facing=Facing.WEST),
        _link("fluids", MEDeviceKind.FLUID_STORAGE_BUS, facing=Facing.EAST),
    ]
    placements = [
        at("a", 2, 0, 0, Facing.NORTH),
        at("ctrl", 2, 0, 3, Facing.NORTH),
        at("items", 0, 0, 2, Facing.WEST),
        at("fluids", 4, 0, 2, Facing.EAST),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [SUBNET])
    result = _route(problem, placements)

    network = _network(result, SUB)
    for link, cell, front in (
        ("items", (0, 0, 2), Facing.WEST),
        ("fluids", (4, 0, 2), Facing.EAST),
    ):
        (bus,) = [d for d in network.devices if d.machine_id == link]
        assert (bus.cell.as_tuple(), bus.side) == (cell, front)
        cable = next(c for c in network.cables if c.cell == coord(*cell))
        assert (cable.kind, cable.me_channels) == (MECableKind.SMART, 1)


def test_an_adhoc_subnet_of_links_grows_from_the_first() -> None:
    """With no device to seed it, an ad-hoc subnet's first link is its tree's first cell, and the
    second link and the acceptor both grow from it: a link is a cable like any other."""
    machines = [
        _link("items", facing=Facing.WEST),
        _link("fluids", MEDeviceKind.FLUID_STORAGE_BUS, facing=Facing.EAST),
        _acceptor(),
    ]
    placements = [
        at("items", 0, 0, 1, Facing.WEST),
        at("fluids", 4, 0, 1, Facing.EAST),
        at("acc", 2, 0, 3, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [SUBNET])
    result = _route(problem, placements)
    network = _network(result, SUB)
    assert {(0, 0, 1), (4, 0, 1)} <= network.cells()
    assert _load(result, (0, 0, 1), SUB) == 2  # the seed carries both storage buses


def test_an_acceptor_is_touched_exactly_once() -> None:
    machines = [_single("a", [_feed(network=SUB)]), controller(), _acceptor()]
    placements = [
        at("a", 0, 0, 0, Facing.NORTH),
        at("ctrl", 2, 0, 3, Facing.NORTH),
        at("acc", 4, 0, 0, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [SUBNET])
    result = _route(problem, placements)
    cells = _network(result, SUB).cells()
    assert sum(n in cells for n in _neighbours((4, 0, 0))) == 1


def test_an_acceptor_beside_the_controller_needs_no_cable() -> None:
    machines = [controller(), _acceptor()]
    placements = [at("ctrl", 1, 0, 1, Facing.NORTH), at("acc", 2, 0, 1, Facing.NORTH)]
    problem = _problem(CellBox(sx=4, sy=1, sz=3), machines, [SUBNET])
    result = _route(problem, placements)
    assert result.ok
    assert _network(result, SUB).cables == []


def test_an_acceptor_between_two_stubs_is_refused() -> None:
    """Touching two of the network's stubs, it would close a loop through the main network."""
    machines = [_single("a", [_feed()]), stub("s1"), stub("s2"), _acceptor(network=MAIN)]
    placements = [
        at("a", 2, 0, 1, Facing.NORTH),
        at("s1", 0, 0, 0, Facing.WEST),
        at("s2", 0, 0, 2, Facing.WEST),
        at("acc", 0, 0, 1, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=4, sy=1, sz=3), machines, [ATTACHED])
    result = _route(problem, placements)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "me_infrastructure"
    assert "acc" in result.infeasibility.detail


def test_a_dual_interface_docks_on_a_face_both_its_ports_allow() -> None:
    dual = endpoint("out", ("items", "fluid"), MEDeviceKind.DUAL_INTERFACE)
    ports = [
        _port("items", _OUT, faces=(RelativeFace.BACK, RelativeFace.LEFT)),
        _port("fluid", _OUT, _FLUID, faces=(RelativeFace.BACK, RelativeFace.RIGHT)),
    ]
    machines = [_single("a", [dual], ports), stub()]
    placements = [at("a", 2, 0, 1, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED])
    result = _route(problem, placements)
    (device,) = _network(result).devices
    assert device.side is Facing.NORTH  # sits south of the machine: its back, facing it


def test_an_endpoint_face_pin_narrows_where_it_docks() -> None:
    machines = [_single("a", [_take()]), stub()]
    placements = [at("a", 2, 0, 1, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED])
    free = _route(problem, placements)
    assert _network(free).devices[0].side is Facing.EAST  # docked west of it, nearest the stub

    pinned = _route(problem, placements, endpoint_faces={("a", "out"): [Facing.EAST]})
    (device,) = _network(pinned).devices
    assert (device.cell.as_tuple(), device.side) == ((3, 0, 1), Facing.WEST)

    for pin in ([Facing.NORTH], []):  # its front, which carries no I/O; and no face at all
        nowhere = route_me(problem, placements, endpoint_faces={("a", "out"): pin})
        assert nowhere.infeasibility is not None
        assert nowhere.infeasibility.constraint == "face_reachability"
        assert "pinned to faces" in nowhere.infeasibility.detail


def test_a_face_pin_on_no_device_is_a_programming_error() -> None:
    machines = [_single("a", [_take()]), stub(), _link("items", network=MAIN)]
    placements = [at("a", 2, 0, 1, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED])
    for key in (("a", "nope"), ("b", "out"), ("items", "bus")):  # a link's bus is no device
        with pytest.raises(ValueError, match="endpoint_faces"):
            route_me(problem, placements, endpoint_faces={key: [Facing.EAST]})


def test_a_dual_interface_whose_ports_share_no_face_is_refused() -> None:
    dual = endpoint("out", ("items", "fluid"), MEDeviceKind.DUAL_INTERFACE)
    ports = [
        _port("items", _OUT, faces=(RelativeFace.LEFT,)),
        _port("fluid", _OUT, _FLUID, faces=(RelativeFace.RIGHT,)),
    ]
    machines = [_single("a", [dual], ports), stub()]
    placements = [at("a", 2, 0, 1, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED])
    result = route_me(problem, placements)
    assert result.infeasibility is not None
    assert "share no face" in result.infeasibility.detail


def test_claimed_cells_are_not_docked_on_again() -> None:
    """What a machine's other connections hold: a single block's dock cell, a multiblock's casing
    cell. The device docks elsewhere."""
    machines = [_single("a", [_take()]), stub()]
    placements = [at("a", 2, 0, 1, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED])
    dock = _network(_route(problem, placements)).devices[0].cell.as_tuple()
    held = _route(problem, placements, claimed_cells={"a": [dock]})
    assert _network(held).devices[0].cell.as_tuple() != dock

    gt = _gt_hatch("out", MEDeviceKind.GT_OUTPUT_BUS_ME, 2710, "OutputBus")
    mb = _multiblock("mb", [gt], [_port("out", _OUT)])
    placements = [at("mb", 1, 0, 1, Facing.NORTH), at("stub", 0, 0, 3, Facing.WEST)]
    problem = _problem(CellBox(sx=4, sy=2, sz=4), [mb, stub()], [ATTACHED])
    casing = _network(_route(problem, placements)).devices[0].cell.as_tuple()
    held = _route(problem, placements, claimed_cells={"mb": [casing]})
    assert _network(held).devices[0].cell.as_tuple() != casing


def test_a_port_split_across_two_hatches_gets_both() -> None:
    """A port too fast for one device has two endpoints (``share`` 0.5 each): two GT ME hatches on
    two casing cells, each with its own terminal, though the two terminals name one port."""
    ends = [
        endpoint(
            f"out{i}",
            ("out",),
            MEDeviceKind.GT_OUTPUT_BUS_ME,
            gt_mid=2710,
            hatch_kind="OutputBus",
            share=0.5,
        )
        for i in (1, 2)
    ]
    mb = _multiblock("mb", ends, [_port("out", _OUT)])
    placements = [at("mb", 1, 0, 1, Facing.NORTH), at("stub", 0, 0, 3, Facing.WEST)]
    problem = _problem(CellBox(sx=4, sy=2, sz=4), [mb, stub()], [ATTACHED])
    result = _route(problem, placements)
    assert len(result.terminals) == 2
    assert {t.port_id for t in result.terminals} == {"out"}
    assert len({d.cell for d in _network(result).devices}) == 2


# ------------------------------------------------------------------ refusals and failures


def _refused(problem: InputIR, placements: Sequence[Placement]) -> str:
    result = route_me(problem, placements)
    _check_layout(problem, placements, result)
    assert result.infeasibility is not None
    return result.infeasibility.constraint


def test_infrastructure_that_contradicts_the_mode_is_refused() -> None:
    # An attached network with a controller of its own.
    attached = _problem(
        CellBox(sx=4, sy=1, sz=4),
        [_single("a", [_feed()]), stub(), controller(network=MAIN)],
        [ATTACHED],
    )
    placed = [
        at("a", 2, 0, 0, Facing.NORTH),
        at("stub", 0, 0, 1, Facing.WEST),
        at("ctrl", 2, 0, 3, Facing.NORTH),
    ]
    assert _refused(attached, placed) == "me_infrastructure"
    # A subnet with an attach stub.
    subnet = _problem(
        CellBox(sx=4, sy=1, sz=4), [_single("a", [_feed(network=SUB)]), stub(network=SUB)], [SUBNET]
    )
    assert _refused(subnet, placed[:2]) == "me_infrastructure"
    # An attached network with no stub.
    bare = _problem(CellBox(sx=4, sy=1, sz=4), [_single("a", [_feed()])], [ATTACHED])
    assert _refused(bare, placed[:1]) == "me_infrastructure"
    # Unplaced infrastructure.
    assert _refused(attached, placed[:1]) == "me_infrastructure"


def test_a_link_with_two_buses_is_refused() -> None:
    second = endpoint("bus2", (), MEDeviceKind.FLUID_STORAGE_BUS, network=SUB)
    machines = [_single("a", [_feed(network=SUB)]), _link("items", extra=[second])]
    placed = [at("a", 2, 0, 0, Facing.NORTH), at("items", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=4, sy=1, sz=4), machines, [SUBNET])
    assert _refused(problem, placed) == "me_infrastructure"


def test_two_networks_whose_blocks_touch_are_both_refused() -> None:
    machines = [
        _single("a", [_feed()]),
        _single("b", [_feed(network=SUB)], facing=Facing.SOUTH),
        stub(),
        controller(),
    ]
    placed = [
        at("a", 3, 0, 0, Facing.NORTH),
        at("b", 3, 0, 3, Facing.SOUTH),
        at("stub", 0, 0, 1, Facing.WEST),
        at("ctrl", 0, 0, 2, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED, SUBNET])
    result = route_me(problem, placed)
    _check_layout(problem, placed, result)
    assert result.failed_networks == (MAIN, SUB)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "me_infrastructure"


def test_a_network_refused_already_keeps_its_own_reason_beside_a_clash() -> None:
    """A subnet's stub is refused for being a subnet's; touching the main network's stub, it
    still refuses the main network too."""
    machines = [_single("a", [_feed()]), stub("s1"), stub("s2", network=SUB)]
    placed = [
        at("a", 3, 0, 0, Facing.NORTH),
        at("s1", 0, 0, 1, Facing.WEST),
        at("s2", 0, 0, 2, Facing.WEST),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED, SUBNET])
    result = route_me(problem, placed)
    _check_layout(problem, placed, result)
    assert result.failed_networks == (MAIN, SUB)
    assert result.infeasibility is not None
    assert repr(SUB) in result.infeasibility.detail  # the main network's reason names the subnet


def test_two_stubs_of_one_network_touching_are_refused() -> None:
    machines = [_single("a", [_feed()]), stub("s1"), stub("s2")]
    placed = [
        at("a", 3, 0, 0, Facing.NORTH),
        at("s1", 0, 0, 1, Facing.WEST),
        at("s2", 0, 0, 2, Facing.WEST),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED])
    assert _refused(problem, placed) == "me_infrastructure"


def test_controllers_that_do_not_touch_are_refused() -> None:
    """Two controllers joined only by cable are a conflict AE gives every device 0 channels for."""
    machines = [_single("a", [_feed(network=SUB)]), controller("c1"), controller("c2")]
    placed = [
        at("a", 2, 0, 0, Facing.NORTH),
        at("c1", 0, 0, 3, Facing.NORTH),
        at("c2", 4, 0, 3, Facing.NORTH),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [SUBNET])
    assert _refused(problem, placed) == "me_infrastructure"


@pytest.mark.parametrize(
    ("cells", "ok"),
    [
        ([(0, 0, 0), (1, 0, 0)], True),
        ([(0, 0, 0), (2, 0, 0)], False),  # not touching
        ([(x, 0, 0) for x in range(8)], False),  # wider than 7
        ([(1, 0, 0), (0, 0, 0), (2, 0, 0), (1, 0, 1), (1, 0, 2)], True),  # one axis through (1,0,0)
        ([(1, 0, 1), (0, 0, 1), (2, 0, 1), (1, 0, 0), (1, 0, 2)], False),  # a cross: two axes
    ],
)
def test_the_controller_cluster_rule(cells: list[Cell], ok: bool) -> None:
    assert router_me._one_cluster(cells) is ok


def test_a_device_on_the_wrong_block_is_refused() -> None:
    """A link's storage bus on another network, and a controller naming a device: both refused."""
    foreign_bus = _link("items").model_copy(
        update={"me_endpoints": (endpoint("bus", (), MEDeviceKind.STORAGE_BUS, network=MAIN),)}
    )
    machines = [_single("a", [_feed()]), stub(), foreign_bus]
    placed = [
        at("a", 2, 0, 0, Facing.NORTH),
        at("stub", 0, 0, 1, Facing.WEST),
        at("items", 4, 0, 2, Facing.EAST),
    ]
    problem = _problem(CellBox(sx=5, sy=1, sz=4), machines, [ATTACHED, SUBNET])
    result = route_me(problem, placed)
    assert result.failed_networks == (MAIN, SUB)  # and the link carries no bus of its own
    assert result.infeasibility is not None
    assert "another network" in result.infeasibility.detail

    busy = controller().model_copy(
        update={"me_endpoints": (endpoint("bus", (), MEDeviceKind.STORAGE_BUS, network=SUB),)}
    )
    problem = _problem(CellBox(sx=5, sy=1, sz=4), [busy], [SUBNET])
    assert _refused(problem, [at("ctrl", 2, 0, 2, Facing.NORTH)]) == "me_infrastructure"


def test_no_cable_takes_the_cell_in_front_of_a_stub() -> None:
    """A stub misplaced inward still keeps the side its main-network feed uses free of cable: the
    machine docks east of itself, though south, in front of the stub, is the first face tried."""
    machines = [_single("a", [_feed()]), stub()]
    placed = [at("a", 0, 0, 0, Facing.NORTH), at("stub", 1, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=3, sy=1, sz=3), machines, [ATTACHED])
    result = route_me(problem, placed)
    assert result.ok, result.infeasibility
    assert (0, 0, 1) not in _network(result).cells()


def test_a_machine_that_can_dock_only_on_the_trunk_is_refused() -> None:
    """``x`` can only dock on the cell all eight machines beyond it are fed through, which takes no
    part once it carries more than eight; the network is refused, naming ``x``."""
    machines: list[Machine] = [_single("x", [_feed()]), stub()]
    placements = [at("x", 1, 0, 0, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    for x in range(2, 6):
        for z, facing in ((0, Facing.NORTH), (2, Facing.SOUTH)):
            machines.append(_single(f"m{x}{z}", [_feed()], facing=facing))
            placements.append(at(f"m{x}{z}", x, 0, z, facing))
    walls = [(0, 0, 0), (0, 0, 2), (1, 0, 2), (6, 0, 0), (6, 0, 2)]
    problem = _problem(CellBox(sx=7, sy=1, sz=3), machines, [ATTACHED], reserved=walls)
    result = route_me(problem, placements)
    _check_layout(problem, placements, result)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "me_channels"
    assert "'x'" in result.infeasibility.detail


def test_an_unreachable_link_or_acceptor_is_a_routing_failure() -> None:
    wall = [(2, 0, z) for z in range(3)]
    for far in (_link("far", facing=Facing.EAST), _acceptor("far")):
        machines = [_single("a", [_feed(network=SUB)]), controller(), far]
        placed = [
            at("a", 0, 0, 0, Facing.NORTH),
            at("ctrl", 1, 0, 2, Facing.NORTH),
            at("far", 4, 0, 1, far.orientation_options[0]),
        ]
        problem = _problem(CellBox(sx=5, sy=1, sz=3), machines, [SUBNET], reserved=wall)
        assert _refused(problem, placed) == "routing"


def test_an_adhoc_tree_seeds_only_clear_of_other_networks() -> None:
    """Every dock cell of the ad-hoc subnet's one machine touches the main network's stub."""
    machines = [_single("b", [_feed(network=SUB)]), stub()]
    placed = [at("b", 1, 0, 0, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(CellBox(sx=2, sy=1, sz=2), machines, [ATTACHED, SUBNET])
    result = route_me(problem, placed)
    _check_layout(problem, placed, result)
    assert result.failed_networks == (SUB,)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "routing"


def test_an_unplaced_machine_is_a_face_reachability_failure() -> None:
    machines = [_single("a", [_feed()]), stub()]
    problem = _problem(CellBox(sx=4, sy=1, sz=3), machines, [ATTACHED])
    result = route_me(problem, [at("stub", 0, 0, 1, Facing.WEST)])
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"
    assert result.failed_networks == (MAIN,)
    assert result.networks == ()


def test_two_devices_of_one_machine_on_its_one_free_cell_are_refused() -> None:
    """The machine's export bus and interface both want its one free cell from the same side: the
    first to dock claims it, so whichever comes second has nowhere left, in either order."""
    machines = [_single("a", [_feed(), _take()]), stub()]
    placed = [at("a", 1, 0, 0, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    problem = _problem(
        CellBox(sx=3, sy=1, sz=2), machines, [ATTACHED], reserved=[(0, 0, 0), (2, 0, 0)]
    )
    result = route_me(problem, placed)
    _check_layout(problem, placed, result)
    assert result.infeasibility is not None
    assert result.infeasibility.constraint == "face_reachability"
    assert "no free cell" in result.infeasibility.detail


def test_a_machine_walled_off_from_the_stub_is_a_routing_failure() -> None:
    """Reserved cells cut the region in two: the machine has free faces, but no way to them."""
    machines = [_single("a", [_feed()]), stub()]
    placed = [at("a", 3, 0, 1, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    wall = [(2, 0, z) for z in range(3)]
    problem = _problem(CellBox(sx=5, sy=1, sz=3), machines, [ATTACHED], reserved=wall)
    assert _refused(problem, placed) == "routing"


def test_a_machine_with_no_free_face_is_a_face_reachability_failure() -> None:
    machines = [_single("a", [_feed()]), stub()]
    placed = [at("a", 1, 0, 0, Facing.NORTH), at("stub", 0, 0, 1, Facing.WEST)]
    walled = [(0, 0, 0), (2, 0, 0), (1, 0, 1)]
    problem = _problem(CellBox(sx=3, sy=1, sz=2), machines, [ATTACHED], reserved=walled)
    assert _refused(problem, placed) == "face_reachability"


def test_no_me_networks_is_an_empty_ok_result() -> None:
    problem = _problem(CellBox(sx=3, sy=1, sz=3), [_single("a", [])], [])
    assert route_me(problem, [at("a", 1, 0, 1, Facing.NORTH)]) == MERouteResult()
    idle = _problem(CellBox(sx=3, sy=1, sz=3), [_single("a", [])], [ATTACHED])
    assert route_me(idle, [at("a", 1, 0, 1, Facing.NORTH)]).ok


def test_routing_is_deterministic() -> None:
    problem, placements = _fishbone()
    assert route_me(problem, placements) == route_me(problem, placements)


# ------------------------------------------------------------------ the property


def _outward(cell: Cell, region: CellBox) -> list[Facing]:
    """The horizontal faces of ``cell`` that look out of the region: where a stub or link fits."""
    x, _, z = cell
    faces = []
    if x == 0:
        faces.append(Facing.WEST)
    if x == region.sx - 1:
        faces.append(Facing.EAST)
    if z == 0:
        faces.append(Facing.NORTH)
    if z == region.sz - 1:
        faces.append(Facing.SOUTH)
    return faces


@st.composite
def _me_problems(draw: st.DrawFn) -> tuple[InputIR, list[Placement], list[Cell]]:
    """A small region with single blocks, maybe a 2x2x2 multiblock, an attached network with one
    or two stubs and/or a subnet (a controller or ad hoc, maybe a link and an acceptor), and some
    reserved cells and other routes' cells in the way."""
    region = CellBox(
        sx=draw(st.integers(4, 8)), sy=draw(st.integers(1, 3)), sz=draw(st.integers(4, 8))
    )
    cells = sorted(
        (x, y, z) for x in range(region.sx) for y in range(region.sy) for z in range(region.sz)
    )
    taken: set[Cell] = set()
    machines: list[Machine] = []
    placements: list[Placement] = []

    def pick(options: Sequence[Cell]) -> Cell | None:
        free = [c for c in options if c not in taken]
        return free[draw(st.integers(0, len(free) - 1))] if free else None

    def place(machine: Machine, cell: Cell, facing: Facing) -> None:
        machines.append(machine)
        placements.append(at(machine.id, *cell, facing))
        taken.update(occupied_cells(coord(*cell), machine.footprint, facing))

    edge = [c for c in cells if _outward(c, region)]
    networks: list[MENetworkSpec] = []
    if draw(st.booleans()):
        budget = draw(st.sampled_from([32, 32, 3]))
        networks.append(MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED, me_channel_budget=budget))
        for i in range(draw(st.integers(1, 2))):
            cell = pick(edge)
            if cell is not None:
                facing = draw(st.sampled_from(_outward(cell, region)))
                place(stub(f"stub{i}", facing=facing), cell, facing)
    for sid in (SUB, SUB2):  # up to two subnets, each a controller or ad hoc
        wanted: bool = not networks or draw(st.booleans())
        if not wanted:
            continue
        networks.append(MENetworkSpec(id=sid, mode=MEMode.SUBNET))
        if draw(st.booleans()) and (cell := pick(cells)) is not None:
            place(controller(f"{sid}-ctrl", network=sid), cell, Facing.NORTH)
        if draw(st.booleans()) and (cell := pick(edge)) is not None:
            facing = draw(st.sampled_from(_outward(cell, region)))
            kind = draw(st.sampled_from([MEDeviceKind.STORAGE_BUS, MEDeviceKind.FLUID_STORAGE_BUS]))
            place(_link(f"{sid}-link", kind, network=sid, facing=facing), cell, facing)
        if draw(st.booleans()) and (cell := pick(cells)) is not None:
            place(_acceptor(f"{sid}-acc", network=sid), cell, Facing.NORTH)
    ids = [n.id for n in networks]

    def on() -> str:
        return draw(st.sampled_from(ids))

    if region.sy >= 2 and draw(st.booleans()):
        corners = [
            c
            for c in cells
            if c[0] + 1 < region.sx
            and c[1] + 1 < region.sy
            and c[2] + 1 < region.sz
            and not taken.intersection(
                occupied_cells(coord(*c), CellBox(sx=2, sy=2, sz=2), Facing.NORTH)
            )
        ]
        if corners:
            # Up to three hatches on one structure, each maybe on its own network: an item output
            # through GT's ME bus or an interface before a normal bus, an item input through an
            # export bus before an input bus, and a fluid output through GT's ME hatch.
            ends: list[MEEndpoint] = []
            ports: list[Port] = []
            out = draw(st.sampled_from([None, "gt", "split", "interface"]))
            if out == "gt":
                ends.append(
                    _gt_hatch("out", MEDeviceKind.GT_OUTPUT_BUS_ME, 2710, "OutputBus", on())
                )
            elif out == "split":  # a port too fast for one hatch: two, half its rate each
                network = on()
                ends += [
                    endpoint(
                        f"out{i}",
                        ("out",),
                        MEDeviceKind.GT_OUTPUT_BUS_ME,
                        network=network,
                        gt_mid=2710,
                        hatch_kind="OutputBus",
                        share=0.5,
                    )
                    for i in (1, 2)
                ]
            elif out == "interface":
                ends.append(
                    endpoint(
                        "out",
                        ("out",),
                        MEDeviceKind.INTERFACE,
                        network=on(),
                        hatch_kind="OutputBus",
                    )
                )
            if out is not None:
                ports.append(_port("out", _OUT))
            if draw(st.booleans()):
                ends.append(
                    _gt_hatch("fout", MEDeviceKind.GT_OUTPUT_HATCH_ME, 2713, "OutputHatch", on())
                )
                ports.append(_port("fout", _OUT, _FLUID))
            if not ends or draw(st.booleans()):
                ends.append(
                    endpoint(
                        "in", ("in",), MEDeviceKind.EXPORT_BUS, network=on(), hatch_kind="InputBus"
                    )
                )
                ports.append(_port("in", _IN))
            corner = corners[draw(st.integers(0, len(corners) - 1))]
            place(_multiblock("mb", ends, ports), corner, Facing.NORTH)

    def pins() -> tuple[RelativeFace, ...] | None:
        """No pin mostly, else a few faces: two machines pinned toward one cell share it."""
        if draw(st.integers(0, 3)):
            return None
        faces = [f for f in RelativeFace if f is not RelativeFace.FRONT]
        return tuple(draw(st.lists(st.sampled_from(faces), min_size=1, max_size=3, unique=True)))

    for i in range(draw(st.integers(1, 5))):
        cell = pick(cells)
        if cell is None:
            break
        recipe = draw(st.sampled_from(["feed", "take", "fluid", "dual", "both"]))
        if recipe == "feed":
            ends, ports = [_feed(network=on())], [_port("in", _IN, faces=pins())]
        elif recipe == "take":
            ends, ports = [_take(network=on())], [_port("out", _OUT, faces=pins())]
        elif recipe == "fluid":
            ends = [endpoint("feed", ("in",), MEDeviceKind.FLUID_EXPORT_BUS, network=on())]
            ports = [_port("in", _IN, _FLUID)]
        elif recipe == "dual":
            ends = [endpoint("out", ("items", "fluid"), MEDeviceKind.DUAL_INTERFACE, network=on())]
            ports = [_port("items", _OUT, faces=pins()), _port("fluid", _OUT, _FLUID, faces=pins())]
        else:
            ends = [_feed(network=on()), _take(network=on())]
            ports = [_port("in", _IN), _port("out", _OUT)]
        facing = draw(st.sampled_from(_HORIZONTAL))
        place(_single(f"m{i}", ends, ports, facing), cell, facing)

    blocked = [c for c in cells if c not in taken]
    reserved = draw(st.lists(st.sampled_from(blocked), max_size=3, unique=True)) if blocked else []
    taken.update(reserved)
    left = [c for c in cells if c not in taken]
    extra = draw(st.lists(st.sampled_from(left), max_size=3, unique=True)) if left else []
    return _problem(region, machines, networks, reserved), placements, extra


@st.composite
def _combs(draw: st.DrawFn) -> tuple[InputIR, list[Placement], list[Cell]]:
    """9 to 20 machines along both sides of one corridor, fed from a stub at its end (or one at
    each end), or from a controller there: more devices than a cable holding a part carries, so
    the trunk near the root has to stay bare and dense, and too many for a flat region to fit."""
    n = draw(st.integers(9, 20))
    half = (n + 1) // 2
    region = CellBox(sx=half + 2 + draw(st.integers(0, 2)), sy=draw(st.integers(1, 2)), sz=3)
    root = draw(st.sampled_from(["stub", "stubs", "controller"]))
    network = SUB if root == "controller" else MAIN
    machines: list[Machine] = []
    placements: list[Placement] = []
    if root == "controller":
        machines.append(controller(network=SUB))
        placements.append(at("ctrl", 0, 0, 1, Facing.NORTH))
        spec = MENetworkSpec(id=SUB, mode=MEMode.SUBNET)
    else:
        machines.append(stub("stub0"))
        placements.append(at("stub0", 0, 0, 1, Facing.WEST))
        if root == "stubs":
            machines.append(stub("stub1", facing=Facing.EAST))
            placements.append(at("stub1", region.sx - 1, 0, 1, Facing.EAST))
        spec = MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED, me_channel_budget=64)
    slots = [(x, z) for x in range(1, half + 1) for z in (0, 2)][:n]
    for i, (x, z) in enumerate(slots):
        facing = Facing.NORTH if z == 0 else Facing.SOUTH
        machines.append(_single(f"m{i}", [_feed(network=network)], facing=facing))
        placements.append(at(f"m{i}", x, 0, z, facing))
    taken = {p.cell.as_tuple() for p in placements}
    free = sorted(
        (x, y, z)
        for x in range(region.sx)
        for y in range(region.sy)
        for z in range(region.sz)
        if (x, y, z) not in taken
    )
    reserved = draw(st.lists(st.sampled_from(free), max_size=2, unique=True))
    return _problem(region, machines, [spec], reserved), placements, []


@settings(
    max_examples=property_examples(200),
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(st.one_of(_me_problems(), _combs()))
def test_whatever_is_laid_is_a_forest_ae_runs(
    case: tuple[InputIR, list[Placement], list[Cell]],
) -> None:
    """Any problem: every network the router lays keeps its own promises and passes the
    validator's independent ME gate, and every other network is an explicit failure."""
    problem, placements, extra = case
    result = route_me(problem, placements, extra_obstacles=extra)
    event("all laid" if result.ok else f"failed: {result.infeasibility.constraint}")  # type: ignore[union-attr]
    _check_layout(problem, placements, result, extra)
    _assert_validates(problem, placements, result)
