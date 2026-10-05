"""The validator's ME (AE2) gate (#333): one rule broken at a time on hand-built builds.

The builds are ``tests/_me_fixtures.py``, each valid as built; every test here breaks one rule from
docs/DOMAIN.md ("What a valid ME build is") and expects its code, so a rule the gate stops enforcing
fails a test. The channel pathing is also tested on its own, against the hand trace in
``docs/spikes/329-me-ae2.md`` section 2.7 and two graphs with cycles (spike 2.5).
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gtnh_solver.ir import (
    AEColor,
    Facing,
    InputIR,
    LayoutResult,
    MECableCell,
    MECableKind,
    MECards,
    MEConfig,
    MEDeviceKind,
    MEMode,
    MENetworkLayout,
    MENetworkSpec,
    Placement,
)
from gtnh_solver.validator import me as vme
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode
from tests._helpers import property_examples
from tests._me_fixtures import (
    MAIN,
    SUB,
    attached_line,
    cable,
    comb,
    controller,
    coord,
    device,
    endpoint,
    gt_hatch_line,
)

_ME_CODES = {c for c in ViolationCode if c.value.startswith("me_")}


def _codes(problem: InputIR, layout: LayoutResult) -> set[ViolationCode]:
    return set(validate(problem, layout).codes())


def _network(layout: LayoutResult, network_id: str = MAIN) -> MENetworkLayout:
    return next(n for n in layout.me_networks if n.id == network_id)


def _with_network(layout: LayoutResult, network: MENetworkLayout) -> LayoutResult:
    others = [n for n in layout.me_networks if n.id != network.id]
    return layout.model_copy(update={"me_networks": [*others, network]})


def _cables(layout: LayoutResult, cables: list[MECableCell]) -> LayoutResult:
    return _with_network(layout, _network(layout).model_copy(update={"cables": cables}))


# ------------------------------------------------------------------ the builds are valid


@pytest.mark.parametrize(
    "build",
    [
        attached_line,
        lambda: comb(8),
        lambda: comb(8, mode=MEMode.SUBNET),
        lambda: comb(8, mode=MEMode.SUBNET, with_controller=True),
        gt_hatch_line,
        lambda: gt_hatch_line(normal=True),
    ],
    ids=["attached", "comb8", "adhoc8", "controller8", "gt_hatch", "normal_hatch"],
)
def test_a_build_by_the_rules_validates(build: object) -> None:
    problem, layout = build()  # type: ignore[operator]
    report = validate(problem, layout)
    assert report.ok, str(report)
    assert report.unbuilt_me_nets == ()


# ------------------------------------------------------------------ rule 1: the ground


def test_a_cable_off_the_ground_is_refused() -> None:
    problem, layout = attached_line()
    reserved = problem.model_copy(update={"reserved_cells": [coord(3, 0, 2)]})
    assert ViolationCode.ROUTE_ON_RESERVED in _codes(reserved, layout)
    through = _cables(layout, [*_network(layout).cables, cable(4, 0, 1)])  # inside machine b
    assert ViolationCode.ROUTE_THROUGH_MACHINE in _codes(problem, through)
    outside = _cables(layout, [*_network(layout).cables, cable(1, 0, 4)])
    assert ViolationCode.ROUTE_OUT_OF_BOUNDS in _codes(problem, outside)


def test_a_stub_is_a_dense_cable_of_its_network() -> None:
    problem, layout = attached_line()
    cables = [cable(0, 0, 2) if c.cell == coord(0, 0, 2) else c for c in _network(layout).cables]
    assert ViolationCode.ME_INFRASTRUCTURE in _codes(problem, _cables(layout, cables))


# ------------------------------------------------------------------ rule 2: every device in place


def test_a_part_on_a_dense_cable_is_refused() -> None:
    problem, layout = attached_line()
    cables = [
        cable(2, 0, 2, MECableKind.DENSE) if c.cell == coord(2, 0, 2) else c
        for c in _network(layout).cables
    ]
    assert ViolationCode.ME_PART_ON_DENSE in _codes(problem, _cables(layout, cables))


def test_a_part_must_face_its_machine_and_not_its_front() -> None:
    problem, layout = attached_line()
    network = _network(layout)
    turned = [
        d.model_copy(update={"side": Facing.EAST})
        if d.endpoint_id == "feed" and d.machine_id == "a"
        else d
        for d in network.devices
    ]
    codes = _codes(problem, _with_network(layout, network.model_copy(update={"devices": turned})))
    assert ViolationCode.ME_DEVICE_PLACEMENT in codes
    # A's front turned to the cable its export bus works through: the front carries no I/O.
    facing_front = layout.model_copy(
        update={
            "placements": [
                Placement(machine_id="a", cell=coord(2, 0, 1), orientation=Facing.SOUTH)
                if p.machine_id == "a"
                else p
                for p in layout.placements
            ]
        }
    )
    assert ViolationCode.ME_DEVICE_PLACEMENT in _codes(problem, facing_front)


def test_two_parts_on_one_side_are_refused() -> None:
    problem, layout = attached_line()
    network = _network(layout)
    moved = [
        d.model_copy(update={"cell": coord(2, 0, 2), "side": Facing.NORTH})
        if d.endpoint_id == "out"
        else d
        for d in network.devices
    ]
    codes = _codes(problem, _with_network(layout, network.model_copy(update={"devices": moved})))
    assert ViolationCode.ME_DEVICE_PLACEMENT in codes


def test_every_endpoint_is_built_once_as_specified() -> None:
    problem, layout = attached_line()
    network = _network(layout)
    missing = network.model_copy(update={"devices": network.devices[:2]})
    assert ViolationCode.ME_ENDPOINT_MISSING in _codes(problem, _with_network(layout, missing))
    wrong = [
        d.model_copy(update={"kind": MEDeviceKind.IMPORT_BUS}) if d.machine_id == "b" else d
        for d in network.devices
    ]
    codes = _codes(problem, _with_network(layout, network.model_copy(update={"devices": wrong})))
    assert ViolationCode.ME_DEVICE_MISMATCH in codes
    twice = network.model_copy(update={"devices": [*network.devices, network.devices[2]]})
    assert ViolationCode.ME_DEVICE_MISMATCH in _codes(problem, _with_network(layout, twice))


def test_a_gt_me_hatch_takes_its_slot_front_out() -> None:
    problem, layout = gt_hatch_line()
    hatch = layout.hatches[0].model_copy(update={"facing": Facing.EAST})
    codes = _codes(problem, layout.model_copy(update={"hatches": [hatch]}))
    assert ViolationCode.ME_DEVICE_PLACEMENT in codes


def test_a_part_must_face_the_normal_hatch_it_serves() -> None:
    problem, layout = gt_hatch_line(normal=True)
    hatch = layout.hatches[0].model_copy(update={"kind": "InputBus"})
    codes = _codes(problem, layout.model_copy(update={"hatches": [hatch]}))
    assert ViolationCode.ME_DEVICE_PLACEMENT in codes


def _slotless(problem: InputIR, layout: LayoutResult) -> tuple[InputIR, LayoutResult]:
    """``gt_hatch_line(normal=True)`` with no slot recorded for the multiblock, so no hatch on it:
    what a plan adapted without the machine's structure gets (#335)."""
    machines = [
        m.model_copy(update={"hatch_slots": (), "hatch_cells": 0}) if m.id == "mb" else m
        for m in problem.machines
    ]
    return problem.model_copy(update={"machines": machines}), layout.model_copy(
        update={"hatches": []}
    )


def test_a_part_on_a_multiblock_with_no_recorded_slots_needs_only_face_it() -> None:
    # No slot was recorded, so no hatch is placed there, as for a pipe's terminal.
    problem, layout = _slotless(*gt_hatch_line(normal=True))
    assert ViolationCode.ME_DEVICE_PLACEMENT not in _codes(problem, layout)


def test_a_part_on_a_multiblock_with_no_recorded_slots_must_still_face_it() -> None:
    problem, layout = _slotless(*gt_hatch_line(normal=True))
    network = _network(layout)
    turned = [d.model_copy(update={"side": Facing.EAST}) for d in network.devices]
    moved = _with_network(layout, network.model_copy(update={"devices": turned}))
    assert ViolationCode.ME_DEVICE_PLACEMENT in _codes(problem, moved)


# ------------------------------------------------------------------ rule 3: one auto-output face


def test_a_single_block_auto_outputs_through_one_face() -> None:
    problem, layout = attached_line()
    a = next(m for m in problem.machines if m.id == "a")
    halves = (
        endpoint("out", ("out",), MEDeviceKind.INTERFACE, share=0.5),
        endpoint("out2", ("out",), MEDeviceKind.INTERFACE, share=0.5),
    )
    a2 = a.model_copy(update={"me_endpoints": (a.me_endpoints[0], *halves)})
    problem = problem.model_copy(
        update={"machines": [a2 if m.id == "a" else m for m in problem.machines]}
    )
    network = _network(layout)
    up = network.model_copy(
        update={
            "cables": [*network.cables, cable(2, 1, 2), cable(2, 1, 1)],
            "devices": [*network.devices, device("a", halves[1], (2, 1, 1), Facing.DOWN)],
        }
    )
    assert ViolationCode.ME_AUTO_OUTPUT_FACES in _codes(problem, _with_network(layout, up))


# ------------------------------------------------------------------ rule 4: rates and cards


def _recard(problem: InputIR, layout: LayoutResult, cards: MECards) -> tuple[InputIR, LayoutResult]:
    """Fit ``a``'s export bus with ``cards``, in the problem and the layout alike."""
    a = next(m for m in problem.machines if m.id == "a")
    feed = endpoint("feed", ("in",), MEDeviceKind.EXPORT_BUS, cards=cards)
    a2 = a.model_copy(update={"me_endpoints": (feed, a.me_endpoints[1])})
    problem = problem.model_copy(
        update={"machines": [a2 if m.id == "a" else m for m in problem.machines]}
    )
    network = _network(layout)
    devices = [
        d.model_copy(update={"cards": cards})
        if (d.machine_id, d.endpoint_id) == ("a", "feed")
        else d
        for d in network.devices
    ]
    return problem, _with_network(layout, network.model_copy(update={"devices": devices}))


def test_a_bus_too_slow_for_its_port_is_refused() -> None:
    # 1 item/t through a bus with no card: one item an operation, an operation per 5 ticks.
    problem, layout = _recard(*attached_line(), MECards())
    assert ViolationCode.ME_DEVICE_RATE_SHORT in _codes(problem, layout)


def test_a_bus_takes_four_cards() -> None:
    problem, layout = _recard(*attached_line(), MECards(acceleration=4, capacity=1))
    assert ViolationCode.ME_UPGRADE_SLOTS in _codes(problem, layout)


def test_a_gt_me_output_bus_flushes_39_items_a_tick() -> None:
    problem, layout = gt_hatch_line()
    mb = problem.machines[0]
    fast = mb.model_copy(
        update={
            "faces": mb.faces.model_copy(
                update={"ports": [mb.faces.ports[0].model_copy(update={"rate": 40.0})]}
            )
        }
    )
    problem = problem.model_copy(update={"machines": [fast, problem.machines[1]]})
    assert ViolationCode.ME_DEVICE_RATE_SHORT in _codes(problem, layout)


# ------------------------------------------------------------------ rule 5: one network a piece


def test_a_network_in_two_pieces_is_split_and_starved() -> None:
    problem, layout = attached_line()
    cut = [c for c in _network(layout).cables if c.cell != coord(3, 0, 2)]
    codes = _codes(problem, _cables(layout, cut))
    assert {ViolationCode.ME_NETWORK_SPLIT, ViolationCode.ME_CHANNEL_STARVED} <= codes


def test_a_subnet_touching_the_main_network_is_merged() -> None:
    problem, layout = attached_line()
    problem = problem.model_copy(
        update={
            "me": MEConfig(
                networks=[
                    *problem.me.networks,
                    MENetworkSpec(id=SUB, mode=MEMode.SUBNET, colour=AEColor.ORANGE),
                ]
            )
        }
    )
    # Orange beside Fluix: Fluix joins every colour.
    sub = MENetworkLayout(id=SUB, colour=AEColor.ORANGE, cables=[cable(3, 0, 3)])
    layout = layout.model_copy(update={"me_networks": [*layout.me_networks, sub]})
    assert ViolationCode.ME_NETWORK_MERGE in _codes(problem, layout)


def test_two_subnets_of_different_colours_may_touch() -> None:
    problem, layout = comb(4, mode=MEMode.SUBNET)
    problem = problem.model_copy(
        update={
            "me": MEConfig(
                networks=[
                    *problem.me.networks,
                    MENetworkSpec(id="other", mode=MEMode.SUBNET, colour=AEColor.BLUE),
                ]
            )
        }
    )
    other = MENetworkLayout(id="other", colour=AEColor.BLUE, cables=[cable(6, 0, 1)])
    layout = layout.model_copy(update={"me_networks": [*layout.me_networks, other]})
    assert ViolationCode.ME_NETWORK_MERGE not in _codes(problem, layout)


def test_the_networks_colour_and_identity_are_the_problems() -> None:
    problem, layout = attached_line()
    orange = _network(layout).model_copy(update={"colour": AEColor.ORANGE})
    assert ViolationCode.ME_NETWORK_COLOUR in _codes(problem, _with_network(layout, orange))
    ghost = MENetworkLayout(id="ghost", colour=AEColor.RED)
    layout = layout.model_copy(update={"me_networks": [*layout.me_networks, ghost]})
    assert ViolationCode.ME_UNKNOWN_NETWORK in _codes(problem, layout)


# ------------------------------------------------------------------ rules 6 to 8: channels


def test_nine_devices_overload_a_smart_cable() -> None:
    codes = _codes(*comb(9))
    assert ViolationCode.ME_CABLE_OVERLOAD in codes
    assert ViolationCode.ME_ATTACH_BUDGET not in codes


def test_an_attached_network_spends_at_most_its_budget() -> None:
    assert ViolationCode.ME_ATTACH_BUDGET in _codes(*comb(9, budget=8))
    assert ViolationCode.ME_ATTACH_BUDGET not in _codes(*comb(8, budget=8))


def test_nine_ad_hoc_devices_lose_every_channel() -> None:
    assert ViolationCode.ME_ADHOC_OVERFLOW in _codes(*comb(9, mode=MEMode.SUBNET))


def test_a_controller_lifts_the_ad_hoc_limit_but_not_cable_capacity() -> None:
    codes = _codes(*comb(9, mode=MEMode.SUBNET, with_controller=True))
    assert ViolationCode.ME_ADHOC_OVERFLOW not in codes
    assert ViolationCode.ME_CABLE_OVERLOAD in codes


def test_an_attached_network_with_its_own_controller_conflicts() -> None:
    problem, layout = comb(4)
    problem = problem.model_copy(update={"machines": [*problem.machines, controller(network=MAIN)]})
    layout = layout.model_copy(
        update={
            "placements": [
                *layout.placements,
                Placement(machine_id="ctrl", cell=coord(0, 0, 0), orientation=Facing.NORTH),
            ]
        }
    )
    assert ViolationCode.ME_CONTROLLER_CONFLICT in _codes(problem, layout)


def test_a_net_on_me_nothing_serves_is_an_abstention_not_a_violation() -> None:
    # Every net on ME before the end-to-end build: no endpoint, no device, no stub.
    problem, layout = attached_line()
    bare = problem.model_copy(
        update={
            "machines": [
                m.model_copy(update={"me_endpoints": ()})
                for m in problem.machines
                if m.me_role is None
            ]
        }
    )
    unbuilt = layout.model_copy(
        update={
            "me_networks": [],
            "placements": [p for p in layout.placements if p.machine_id != "stub"],
        }
    )
    report = validate(bare, unbuilt)
    assert report.ok, str(report)
    assert report.unbuilt_me_nets == ("stone", "mid")


def test_a_subnet_on_the_edge_is_reported() -> None:
    problem, layout = comb(2, mode=MEMode.SUBNET)
    report = validate(problem, layout)
    assert (0, 0, 1) in report.me_boundary_exposure


# ------------------------------------------------------------------ the pathing on its own


def _graph(
    spec: dict[str, tuple[int, int, bool]], links: list[tuple[str, str]]
) -> tuple[vme._Graph, dict[str, int]]:
    """A graph from ``name -> (class, capacity, needs a channel)`` and its links; a name starting
    ``C`` is a controller, ``R`` an attach stub."""
    graph = vme._Graph()
    index: dict[str, int] = {}
    for name, (cls, capacity, channel) in spec.items():
        index[name] = graph.add(
            vme._Node(
                key=(name,),
                network=MAIN,
                colour=AEColor.FLUIX,
                capacity=capacity,
                cls=cls,
                channel=channel,
                label=name,
                root=name.startswith("R"),
                controller=name.startswith("C"),
            )
        )
    for a, b in links:
        graph.join(index[a], index[b])
    return graph, index


def _loads(
    graph: vme._Graph, index: dict[str, int], roots: list[str], controllers: list[str]
) -> dict[str, int]:
    labels, parents = vme._pathing(
        graph,
        list(range(len(graph.nodes))),
        [index[r] for r in roots],
        [index[c] for c in controllers],
    )
    below = vme._subtree_devices(graph, labels, parents)
    return {name: len(below.get(i, ())) for name, i in index.items() if i in labels}


def test_the_spike_trace_starves_one_bus_at_s1() -> None:
    """Spike 2.7: a controller, three dense cables, three smart ones, nine buses (four on S1, four
    on S2, one on S3). S1 is the bottleneck: nine devices below a cable that carries eight."""
    dense, smart, part = (vme._DENSE, 32, False), (vme._PREFERRED, 8, False), (vme._OTHER, 8, True)
    spec = {"C": (vme._OTHER, 0, False), "D1": dense, "D2": dense, "D3": dense}
    spec |= {"S1": smart, "S2": smart, "S3": smart}
    buses = {f"S{s}b{i}": part for s, n in ((1, 4), (2, 4), (3, 1)) for i in range(n)}
    spec |= buses
    links = [("C", "D1"), ("D1", "D2"), ("D2", "D3"), ("D3", "S1"), ("S1", "S2"), ("S2", "S3")]
    links += [(name[:2], name) for name in buses]
    graph, index = _graph(spec, links)
    loads = _loads(graph, index, [], ["C"])
    assert loads["D1"] == loads["D3"] == loads["S1"] == 9
    assert (loads["S2"], loads["S3"]) == (5, 1)
    over = [n for n, load in loads.items() if load > spec[n][1]]
    assert over == ["S1"]


def test_a_cycle_with_a_unique_shortest_path_is_a_tree() -> None:
    """R reaches C through A in two hops and through B in three: AE always takes A."""
    smart = (vme._PREFERRED, 8, False)
    spec = {"R": (vme._DENSE, 32, False), "A": smart, "B": smart, "B2": smart, "C": smart}
    spec |= {f"p{i}": (vme._OTHER, 8, True) for i in range(6)}
    links = [("R", "A"), ("A", "C"), ("R", "B"), ("B", "B2"), ("B2", "C")]
    links += [("C", f"p{i}") for i in range(6)]
    graph, index = _graph(spec, links)
    loads = _loads(graph, index, ["R"], [])
    assert (loads["A"], loads["C"]) == (6, 6)
    assert loads["B"] == loads["B2"] == 0


def test_a_tie_charges_both_ways_soundly() -> None:
    """C is as near R through A as through B, so AE may route its six devices either way: both
    branches are charged them, and A, with three of its own, is over capacity on one of AE's two
    trees, so it is flagged."""
    smart = (vme._PREFERRED, 8, False)
    spec = {"R": (vme._DENSE, 32, False), "A": smart, "B": smart, "C": smart}
    spec |= {f"c{i}": (vme._OTHER, 8, True) for i in range(6)}
    spec |= {f"a{i}": (vme._OTHER, 8, True) for i in range(3)}
    links = [("R", "A"), ("R", "B"), ("A", "C"), ("B", "C")]
    links += [("C", f"c{i}") for i in range(6)] + [("A", f"a{i}") for i in range(3)]
    graph, index = _graph(spec, links)
    loads = _loads(graph, index, ["R"], [])
    assert (loads["A"], loads["B"]) == (9, 6)


def test_queues_are_sticky_dense_first() -> None:
    """A dense cable reached only through a smart one is in the smart queue (spike 2.1): its label
    counts the smart hop, so the all-dense way wins even when longer."""
    dense, smart = (vme._DENSE, 32, False), (vme._PREFERRED, 8, False)
    spec = {"R": dense, "S": smart, "D1": dense, "D2": dense, "X": dense}
    spec |= {"p": (vme._OTHER, 8, True)}
    links = [("R", "S"), ("S", "X"), ("R", "D1"), ("D1", "D2"), ("D2", "X"), ("X", "p")]
    graph, index = _graph(spec, links)
    loads = _loads(graph, index, ["R"], [])
    assert loads["D1"] == loads["D2"] == 1
    assert loads["S"] == 0


def test_pathing_codes_are_all_me_codes() -> None:
    assert ViolationCode.ME_CABLE_OVERLOAD in _ME_CODES
    assert all(c.value.startswith("me_") for c in _ME_CODES)


_CELLS = st.tuples(
    st.integers(min_value=-1, max_value=6),
    st.integers(min_value=-1, max_value=2),
    st.integers(min_value=-1, max_value=4),
)


@settings(max_examples=property_examples(200), suppress_health_check=[HealthCheck.too_slow])
@given(
    cables=st.lists(st.tuples(_CELLS, st.sampled_from(list(MECableKind))), max_size=12),
    moves=st.lists(st.tuples(_CELLS, st.sampled_from(list(Facing))), max_size=3),
    colour=st.sampled_from(list(AEColor)),
)
def test_the_me_gate_reports_but_never_raises(
    cables: list[tuple[tuple[int, int, int], MECableKind]],
    moves: list[tuple[tuple[int, int, int], Facing]],
    colour: AEColor,
) -> None:
    """Any arrangement of cables and devices, however broken, is reported, never raised."""
    problem, layout = attached_line()
    network = _network(layout)
    devices = [
        d.model_copy(update={"cell": coord(*cell), "side": side})
        for d, (cell, side) in zip(network.devices, moves, strict=False)
    ] + network.devices[len(moves) :]
    seen: dict[tuple[int, int, int], MECableKind] = dict(cables)
    built = network.model_copy(
        update={
            "colour": colour,
            "cables": [MECableCell(cell=coord(*c), kind=k) for c, k in seen.items()],
            "devices": devices,
        }
    )
    report = validate(problem, _with_network(layout, built))
    assert isinstance(report.ok, bool)
