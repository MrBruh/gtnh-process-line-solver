"""The solve lays each ME network (#335): AE2 cable and devices, between the pipes and power.

An attempt routes the pipes, then the ME networks around them, then power around both, and turns
every connection into its hatch, an ME device's included (``solver.core._assemble``). Neither order
of ME and power always fits, so an attempt that fails also lays power first and keeps whichever
leaves fewer nets unmoved. These pin that wiring: the layout carries the networks the validator then
holds to AE2's rules, a GT ME hatch is placed as the slot kind its endpoint names, and a line with
no ME network never pays for the second order.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from gtnh_solver.adapter.me_build import build_me
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    FaceSpec,
    Facing,
    Infeasibility,
    InputIR,
    IODirection,
    LayoutStatus,
    Machine,
    MachineFaceRef,
    MEConfig,
    MEDeviceKind,
    MEMode,
    MENetworkSpec,
    MEPower,
    Net,
    Placement,
    Terminal,
)
from gtnh_solver.placement import place
from gtnh_solver.router import MERouteResult, claims_by_machine, place_hatches, route
from gtnh_solver.solver import core, solve
from gtnh_solver.solver._structure import me_cable_cells, structure_cells, structure_quality
from gtnh_solver.validator import validate
from tests._helpers import consumer, net, producer
from tests._me_fixtures import (
    MAIN,
    acceptor_comb,
    attached_line,
    coord,
    endpoint,
    gt_hatch_line,
    item_port,
    me_net,
    stub,
)
from tests._me_fixtures import at as me_at

# ------------------------------------------------------------------ end to end


def test_an_attached_line_solves_with_its_network_laid() -> None:
    problem, _ = attached_line()
    layout = solve(problem, seed=0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert [n.id for n in layout.me_networks] == [MAIN]
    built = {(d.machine_id, d.endpoint_id) for d in layout.me_networks[0].devices}
    assert built == {(m.id, e.id) for m in problem.machines for e in m.me_endpoints}
    assert validate(problem, layout).ok
    # The cable is structure like a pipe: the metrics measure it.
    cells = me_cable_cells(layout.me_networks)
    assert cells <= structure_cells(problem, layout.placements, layout.routes, cells)


@pytest.mark.parametrize("normal", [False, True], ids=["gt-me-hatch", "part-on-normal-hatch"])
def test_a_multiblocks_me_connection_is_placed_as_the_hatch_its_endpoint_names(
    normal: bool,
) -> None:
    problem, _ = gt_hatch_line(normal=normal)
    layout = solve(problem, seed=0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    served = [h for h in layout.hatches if h.port_id == "out"]
    assert [h.kind for h in served] == ["OutputBus"]
    assert validate(problem, layout).ok


def test_a_line_with_no_me_network_lays_none() -> None:
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=1, sz=3),
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    layout = solve(problem, seed=0)
    assert layout.status is LayoutStatus.VALID
    assert layout.me_networks == []
    # Nor does it report one: the layout dumps exactly as before ME power (#336).
    assert "me" not in layout.metrics.model_dump(mode="json")


def test_an_acceptor_network_solves_with_its_acceptor_on_the_lines_power() -> None:
    # The fixture's line, given room to lay it in: the acceptor is placed and cabled like any
    # machine drawing EU, and the layout reports what the network it feeds draws (#336).
    problem, _ = acceptor_comb(eut=20.0)
    problem = problem.model_copy(update={"bounding_region": CellBox(sx=8, sy=2, sz=8)})
    layout = solve(problem, seed=0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(problem, layout).ok
    (metrics,) = layout.metrics.me
    assert (metrics.id, metrics.power) == (MAIN, MEPower.ACCEPTOR)
    assert 0 < metrics.eu_per_tick <= 20.0
    (cable,) = [r for r in layout.routes if r.net_id == "power:LV"]
    assert "acc" in {t.machine_id for t in cable.terminals}


# ------------------------------------------------------------------ the two orders


def _attached() -> tuple[InputIR, tuple[Placement, ...]]:
    """The attached line over its hand-built placements, which leave every device room."""
    problem, layout = attached_line()
    return problem, tuple(layout.placements)


ME_FIRST, POWER_FIRST = True, False


def _spy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_me: frozenset[bool] = frozenset(),
    fail_power: frozenset[bool] = frozenset(),
) -> list[bool]:
    """Record each ``_lay_cable`` order, failing the ME network or stalling power in the orders
    named (``ME_FIRST``, ``POWER_FIRST``)."""
    calls: list[bool] = []
    real = core._lay_cable

    def spy(*args: Any, me_first: bool, **kwargs: Any) -> core._Laid:
        calls.append(me_first)
        laid = real(*args, me_first=me_first, **kwargs)
        stall = Infeasibility(constraint="routing", detail="stand-in", suggested_relaxation="none")
        if me_first in fail_me:
            laid = replace(laid, me=MERouteResult(failed_networks=(MAIN,), infeasibility=stall))
        if me_first in fail_power:
            laid = replace(laid, power=replace(laid.power, infeasibility=stall))
        return laid

    monkeypatch.setattr(core, "_lay_cable", spy)
    return calls


def test_an_attempt_that_lays_cleanly_tries_one_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    problem, placements = _attached()
    layout, failed = core._assemble(problem, placements, 0)
    assert calls == [ME_FIRST]
    assert layout.status is LayoutStatus.VALID
    assert failed == ()


def test_a_failed_order_is_retried_power_first_and_the_better_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(monkeypatch, fail_me=frozenset({ME_FIRST}))
    problem, placements = _attached()
    layout, failed = core._assemble(problem, placements, 0)
    assert calls == [ME_FIRST, POWER_FIRST]
    assert layout.status is LayoutStatus.VALID
    assert failed == ()
    assert [n.id for n in layout.me_networks] == [MAIN]


def test_the_order_leaving_fewer_nets_unmoved_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # ME first leaves both nets on the network unmoved; power first only stalls, naming none.
    _spy(monkeypatch, fail_me=frozenset({ME_FIRST}), fail_power=frozenset({POWER_FIRST}))
    problem, placements = _attached()
    layout, failed = core._assemble(problem, placements, 0)
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    assert failed == ()
    assert [n.id for n in layout.me_networks] == [MAIN]


def test_a_tie_keeps_the_me_first_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, fail_me=frozenset({ME_FIRST, POWER_FIRST}))
    problem, placements = _attached()
    layout, failed = core._assemble(problem, placements, 0)
    assert calls == [ME_FIRST, POWER_FIRST]
    assert layout.status is LayoutStatus.PARTIAL_INVALID
    # Every net riding the network it could not lay is named unmoved.
    assert sorted(failed) == ["mid", "stone"]


def test_a_line_with_no_me_network_never_pays_for_the_second_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(monkeypatch, fail_power=frozenset({ME_FIRST, POWER_FIRST}))
    problem = InputIR(
        bounding_region=CellBox(sx=4, sy=1, sz=3),
        machines=[producer("a"), consumer("b")],
        nets=[net("n", "a", "b")],
    )
    core._assemble(problem, tuple(place(problem).placements), 0)
    assert calls == [ME_FIRST]


def test_a_stall_naming_no_net_still_counts_as_a_failure() -> None:
    problem, placements = _attached()
    laid = core._lay_cable(
        problem,
        placements,
        route(problem, placements),
        {},
        "footprint",
        repair=False,
        me_first=False,
    )
    assert laid.failures(problem) == 0
    stall = Infeasibility(constraint="routing", detail="stand-in", suggested_relaxation="none")
    assert replace(laid, me=MERouteResult(infeasibility=stall)).failures(problem) == 1


# ------------------------------------------------------------------ the pieces


def _split_bus() -> tuple[InputIR, list[Placement]]:
    """A 2x1x1 multiblock whose one item output is too fast for one GT ME output bus, so two
    endpoints each carry half of it, each on an output bus slot of its own."""
    halves = [
        endpoint(
            f"out{i}",
            ("out",),
            MEDeviceKind.GT_OUTPUT_BUS_ME,
            gt_mid=2710,
            hatch_kind="OutputBus",
            share=0.5,
        )
        for i in range(2)
    ]
    mb = gt_hatch_line()[0].machines[0]
    mb = Machine.model_validate(
        {
            **mb.model_dump(),
            "hatch_slots": [
                {"offset": {"x": x, "y": 0, "z": 0}, "kinds": ["OutputBus"]} for x in range(2)
            ],
            "me_endpoints": [h.model_dump() for h in halves],
        }
    )
    problem = InputIR(
        bounding_region=CellBox(sx=5, sy=1, sz=4),
        machines=[mb, stub()],
        nets=[me_net("prod", ("mb", "out"))],
        me=MEConfig(networks=[MENetworkSpec(id=MAIN, mode=MEMode.ATTACHED)]),
    )
    return problem, [me_at("mb", 1, 0, 1, Facing.NORTH), me_at("stub", 0, 0, 2, Facing.WEST)]


def _terminal(x: int) -> Terminal:
    return Terminal(machine_id="mb", port_id="out", face=Facing.SOUTH, cell=coord(x, 0, 2))


def test_a_port_split_across_two_me_devices_gets_two_hatches() -> None:
    # A route's terminals are kept apart by port; two ME devices on one port need a hatch each.
    problem, placements = _split_bus()
    plan = place_hatches(problem, placements, [], [], me_terminals=[_terminal(1), _terminal(2)])
    served = sorted((h.cell.as_tuple(), h.kind, h.port_id) for h in plan.hatches if h.port_id)
    assert served == [((1, 0, 1), "OutputBus", "out"), ((2, 0, 1), "OutputBus", "out")]


def test_two_me_devices_never_share_a_casing_cell() -> None:
    problem, placements = _split_bus()
    plan = place_hatches(problem, placements, [], [], me_terminals=[_terminal(1), _terminal(1)])
    assert [h.cell.as_tuple() for h in plan.hatches if h.port_id] == [(1, 0, 1)]


def test_an_me_terminal_on_a_port_no_endpoint_kinds_falls_back_to_the_ports_own_kind() -> None:
    problem, placements = _split_bus()
    mb = problem.machines[0]
    ports = [*mb.faces.ports, item_port("extra", IODirection.OUTPUT)]
    bare = mb.model_copy(update={"faces": mb.faces.model_copy(update={"ports": ports})})
    stray = Terminal(machine_id="mb", port_id="extra", face=Facing.SOUTH, cell=coord(1, 0, 2))
    plan = place_hatches(
        problem.model_copy(update={"machines": [bare, problem.machines[1]]}),
        placements,
        [],
        [],
        me_terminals=[stray],
    )
    assert [h.kind for h in plan.hatches if h.port_id == "extra"] == ["OutputBus"]


def test_an_me_terminal_claims_its_casing_cell_like_a_routes() -> None:
    problem, _ = _split_bus()
    machines = {m.id: m for m in problem.machines}
    assert claims_by_machine((), machines, [_terminal(2)]) == {"mb": {(2, 0, 1)}}


def test_me_cable_counts_as_route_cells_in_the_ranking() -> None:
    problem, layout = attached_line()
    cells = me_cable_cells(layout.me_networks)
    bare = structure_quality(problem, layout.placements, [], "footprint")
    cabled = structure_quality(problem, layout.placements, [], "footprint", cells)
    # The blend adds every cable block; the floor it spans grows too where it sprawls.
    assert cabled[0] - bare[0] >= len(cells)


def test_a_multiblock_with_no_recorded_slots_takes_parts_facing_it_and_solves() -> None:
    # The plan says multiblock but the dataset has no structure for it, so no slot is recorded and
    # no hatch is placed on it, as for a pipe's terminal. Its devices are AE2 parts facing the
    # machine, never a GT ME hatch (which would need a slot), and the line solves: before, it
    # waited on a hatch nobody places and could never pass the validator.
    made = Machine(
        id="m0",
        type="t",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(ports=[item_port("item:out", IODirection.OUTPUT, rate=0.0)]),
    )
    chest = Machine(
        id="s0",
        type="Super Chest",
        voltage_tier="LV",
        orientation_options=list(Facing)[:4],
        faces=FaceSpec(ports=[item_port("input:r0", IODirection.INPUT)]),
    )
    product = Net(
        id="e0",
        commodity=Commodity.ITEM,
        fluid_or_item="r0",
        throughput=0.0,
        me_network="me0",
        endpoints=[
            MachineFaceRef(machine_id="m0", port_id="item:out"),
            MachineFaceRef(machine_id="s0", port_id="input:r0"),
        ],
    )
    me = MEConfig(networks=[MENetworkSpec(id="me0", mode=MEMode.SUBNET)])
    machines, nets = build_me(
        [made, chest],
        [product],
        me,
        storage_ids={"s0"},
        multiblock_ids={"m0"},
        line_tier="LV",
        recipe_ticks={},
    )
    (built,) = next(m for m in machines if m.id == "m0").me_endpoints
    assert built.device.gt_mid is None
    problem = InputIR(
        bounding_region=CellBox(sx=2, sy=1, sz=2), machines=machines, nets=nets, me=me
    )
    layout = solve(problem, seed=0)
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(problem, layout).ok
    assert layout.hatches == []
