"""The ME contracts (#332): a problem's ME networks, the per-net choice, and the NetList and MEPlan.

What each refuses, because a wrong one does not fail loudly: two subnets of one colour, or a Fluix
one, merge in game wherever they touch, and an attached network can only ever be the player's one
main network. Plus the versioning rule every contract here keeps (``check_contract_version``), and
InputIR v8 refusing the v7 per-commodity toggles.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gtnh_solver.ir import (
    ME_PLAN_VERSION,
    NETLIST_VERSION,
    AEColor,
    CellBox,
    Commodity,
    InputIR,
    LayoutMetrics,
    LayoutResult,
    Machine,
    MECards,
    MEConfig,
    MEDeviceKind,
    MEDeviceSpec,
    MEFlowMetrics,
    MEMode,
    MENetworkLayout,
    MENetworkMetrics,
    MENetworkSpec,
    MEPlan,
    MEPower,
    MEStorage,
    Net,
    NetEnd,
    NetEntry,
    NetKind,
    NetList,
)
from gtnh_solver.ir.nets import connection_counts
from tests._helpers import consumer, net, on_me, power_source, producer
from tests._me_fixtures import attached_line, endpoint, stub


def _attached(network_id: str = "main") -> MENetworkSpec:
    return MENetworkSpec(id=network_id, mode=MEMode.ATTACHED)


def _subnet(network_id: str, colour: AEColor | None = None) -> MENetworkSpec:
    return MENetworkSpec(id=network_id, mode=MEMode.SUBNET, colour=colour)


# ------------------------------------------------------------------ networks


def test_an_attached_network_is_fluix_and_stores_in_the_main_network() -> None:
    assert MEConfig(networks=[_attached()]).colour("main") is AEColor.FLUIX
    MENetworkSpec(id="m", mode=MEMode.ATTACHED, colour=AEColor.FLUIX)  # saying so is fine
    with pytest.raises(ValidationError, match="cannot be red"):
        MENetworkSpec(id="m", mode=MEMode.ATTACHED, colour=AEColor.RED)
    with pytest.raises(ValidationError, match="subnet's storage"):
        MENetworkSpec(id="m", mode=MEMode.ATTACHED, storage=MEStorage.CHESTS)


def test_a_subnet_is_never_fluix() -> None:
    with pytest.raises(ValidationError, match="cannot be Fluix"):
        _subnet("s", AEColor.FLUIX)


def test_networks_keep_their_rules_together() -> None:
    with pytest.raises(ValidationError, match="duplicate ME network id"):
        MEConfig(networks=[_attached("a"), _subnet("a")])
    with pytest.raises(ValidationError, match="one main network"):
        MEConfig(networks=[_attached("a"), _attached("b")])
    with pytest.raises(ValidationError, match="share a colour"):
        MEConfig(networks=[_subnet("a", AEColor.RED), _subnet("b", AEColor.RED)])


def test_an_unset_subnet_colour_is_the_first_one_free() -> None:
    config = MEConfig(
        networks=[_subnet("a"), _subnet("b", AEColor.WHITE), _subnet("c"), _attached()]
    )
    # White is taken by b, so a gets the next in AE2's order, Orange, and c the one after.
    assert [config.colour(n) for n in ("a", "b", "c", "main")] == [
        AEColor.ORANGE,
        AEColor.WHITE,
        AEColor.MAGENTA,
        AEColor.FLUIX,
    ]
    with pytest.raises(KeyError):
        config.colour("nowhere")


# ------------------------------------------------------------------ the problem


def _problem(**kwargs: object) -> InputIR:
    fields: dict[str, object] = {
        "bounding_region": CellBox(sx=4, sy=1, sz=4),
        "machines": [producer("a"), consumer("b"), power_source("s")],
        "nets": [net("n", "a", "b")],
    }
    return InputIR(**(fields | kwargs))


def test_a_net_rides_only_a_network_the_problem_has() -> None:
    riding = net("n", "a", "b").model_copy(update={"me_network": "main"})
    _problem(nets=[riding], me=MEConfig(networks=[_attached()]))
    with pytest.raises(ValidationError, match="unknown ME network 'main'"):
        _problem(nets=[riding])


def test_a_power_net_never_rides_me() -> None:
    with pytest.raises(ValidationError, match="power net never rides ME"):
        Net(
            id="p",
            commodity=Commodity.POWER,
            throughput=1.0,
            endpoints=[net("p", "s", "a").endpoints[0]],
            me_network="main",
        )


def test_rides_me_reads_the_net_and_for_power_the_problem() -> None:
    problem = on_me(_problem(), Commodity.ITEM)
    (item,) = problem.nets
    assert item.rides_me
    assert problem.rides_me(item)
    power = net("p", "s", "b", commodity=Commodity.POWER)
    assert not problem.rides_me(power)
    assert on_me(_problem(), power=True).rides_me(power)


def test_connection_counts_skip_what_rides_me() -> None:
    problem = on_me(_problem(), Commodity.ITEM)
    assert connection_counts(problem.nets) == {}
    power = [net("p", "s", "b", commodity=Commodity.POWER)]
    assert connection_counts(power) == {"s": 1, "b": 1}
    assert connection_counts(power, on_me(_problem(), power=True).rides_me) == {}


def test_input_ir_refuses_the_v7_toggles() -> None:
    payload = _problem().model_dump()
    payload["me_toggles"] = {"items": True, "fluids": False, "power": False}
    with pytest.raises(ValidationError, match="me_toggles"):
        InputIR.model_validate(payload)
    payload = _problem().model_dump() | {"version": 7}
    with pytest.raises(ValidationError, match="contract version 7"):
        InputIR.model_validate(payload)


def test_a_problem_on_me_round_trips() -> None:
    problem = on_me(_problem(), Commodity.ITEM, power=True)
    assert InputIR.model_validate_json(problem.model_dump_json()) == problem


# ------------------------------------------------------------------ the NetList and the MEPlan


def _entry(commodity: Commodity = Commodity.ITEM) -> NetEntry:
    end = NetEnd(
        machine_id="a",
        port_id="out",
        machine_type="t",
        multiblock=False,
        rate=1.0,
        suggested="ME Interface",
    )
    return NetEntry(
        id="n",
        kind=NetKind.INTERNAL,
        commodity=commodity,
        resource="x",
        rate=1.0,
        producers=(end,),
    )


def test_a_net_list_round_trips_and_checks_its_version() -> None:
    listed = NetList(plan_digest="d", solver_version="0", line_tier="LV", nets=[_entry()])
    assert NetList.model_validate_json(listed.model_dump_json()) == listed
    assert listed.version == NETLIST_VERSION
    with pytest.raises(ValidationError, match="NetList payload declares contract version 2"):
        NetList.model_validate(listed.model_dump() | {"version": 2})


def test_a_net_list_never_lists_power() -> None:
    with pytest.raises(ValidationError, match="power net never rides ME"):
        _entry(Commodity.POWER)


def test_an_me_plan_round_trips_and_checks_what_it_names() -> None:
    me_plan = MEPlan(plan_digest="d", networks=[_attached(), _subnet("s")], nets={"n": "s"})
    assert MEPlan.model_validate_json(me_plan.model_dump_json()) == me_plan
    assert me_plan.version == ME_PLAN_VERSION
    with pytest.raises(ValidationError, match="unknown ME network"):
        MEPlan(plan_digest="d", nets={"n": "nowhere"})
    with pytest.raises(ValidationError, match="share a colour"):
        MEPlan(
            plan_digest="d",
            networks=[_subnet("a", AEColor.RED), _subnet("b", AEColor.RED)],
        )
    with pytest.raises(ValidationError, match="MEPlan payload declares contract version 0"):
        MEPlan.model_validate(me_plan.model_dump() | {"version": 0})


# ------------------------------------------------------------------ InputIR v9/v10 and LayoutResult v5/v6


def test_a_gt_me_hatch_names_its_mid_and_nothing_else_does() -> None:
    MEDeviceSpec(kind=MEDeviceKind.GT_OUTPUT_BUS_ME, gt_mid=2710)
    with pytest.raises(ValidationError, match="names its mID"):
        MEDeviceSpec(kind=MEDeviceKind.GT_OUTPUT_BUS_ME)
    with pytest.raises(ValidationError, match="names its mID"):
        MEDeviceSpec(kind=MEDeviceKind.EXPORT_BUS, gt_mid=2710)


def test_an_endpoint_serves_its_machines_ports_on_its_network() -> None:
    problem, _ = attached_line()
    a = problem.machines[0]
    with pytest.raises(ValidationError, match="unknown port"):
        Machine.model_validate(
            a.model_dump()
            | {"me_endpoints": [endpoint("x", ("nope",), MEDeviceKind.EXPORT_BUS).model_dump()]}
        )
    with pytest.raises(ValidationError, match="two ME endpoints with one id"):
        Machine.model_validate(
            a.model_dump() | {"me_endpoints": [a.me_endpoints[0].model_dump()] * 2}
        )
    with pytest.raises(ValidationError, match="only an infrastructure block"):
        Machine.model_validate(
            a.model_dump()
            | {"me_endpoints": [endpoint("x", (), MEDeviceKind.EXPORT_BUS).model_dump()]}
        )
    payload = problem.model_dump()
    payload["me"] = {"networks": [], "power_external": False}
    payload["nets"] = [n | {"me_network": None} for n in payload["nets"]]
    with pytest.raises(ValidationError, match="unknown ME network 'main'"):
        InputIR.model_validate(payload)  # the endpoints still name 'main'
    payload = problem.model_dump()
    payload["nets"] = [n | {"me_network": None} for n in payload["nets"]]
    with pytest.raises(ValidationError, match="not ME network 'main' alone"):
        InputIR.model_validate(payload)  # an endpoint on a port whose net is piped


def test_infrastructure_names_its_network_and_faces_out() -> None:
    attach = stub()
    with pytest.raises(ValidationError, match="names its network"):
        Machine.model_validate(attach.model_dump() | {"me_network": None})
    with pytest.raises(ValidationError, match="must be outside_front"):
        Machine.model_validate(attach.model_dump() | {"outside_front": False})
    with pytest.raises(ValidationError, match="names its network"):
        Machine.model_validate(attach.model_dump() | {"me_role": None})


def test_a_layout_lists_each_network_and_cable_once() -> None:
    _, layout = attached_line()
    network = layout.me_networks[0]
    with pytest.raises(ValidationError, match="lists a cable cell twice"):
        MENetworkLayout.model_validate(
            network.model_dump() | {"cables": [c.model_dump() for c in network.cables] * 2}
        )
    with pytest.raises(ValidationError, match="lists an ME network twice"):
        LayoutResult.model_validate(
            layout.model_dump() | {"me_networks": [network.model_dump()] * 2}
        )
    assert network.cells() == {c.cell.as_tuple() for c in network.cables}


def test_the_contracts_round_trip_and_refuse_older_versions() -> None:
    problem, layout = attached_line()
    assert InputIR.model_validate_json(problem.model_dump_json()) == problem
    assert LayoutResult.model_validate_json(layout.model_dump_json()) == layout
    # v9 and v5 came before the Fuzzy Card (#353): a consumer of either builds a bus set to an
    # item at any damage without it, so neither payload is read as if it agreed.
    for older in (8, 9):
        with pytest.raises(ValidationError, match=f"contract version {older}"):
            InputIR.model_validate(problem.model_dump() | {"version": older})
    for older in (4, 5):
        with pytest.raises(ValidationError, match=f"contract version {older}"):
            LayoutResult.model_validate(layout.model_dump() | {"version": older})


def test_a_bus_takes_one_fuzzy_card_and_counts_it_among_its_slots() -> None:
    """The Fuzzy Card (#353): AE2 takes one a bus (``Registration.java:644``, ``:653``, ``:762``),
    and it fills an upgrade slot like any card, so ``count`` counts it."""
    assert MECards().fuzzy == 0
    assert MECards(fuzzy=1, acceleration=3).count == 4
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        MECards(fuzzy=2)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        MECards(fuzzy=-1)
    carded = MEDeviceSpec(
        kind=MEDeviceKind.EXPORT_BUS, cards=MECards(fuzzy=1), config=("minecraft:log@32767",)
    )
    # Every field is written, the card's included, so a layout states it whatever it is.
    assert carded.model_dump(mode="json")["cards"] == {
        "acceleration": 0,
        "super_speed": 0,
        "capacity": 0,
        "fuzzy": 1,
    }
    assert MEDeviceSpec.model_validate_json(carded.model_dump_json()) == carded


def test_a_layout_reports_each_network_only_when_it_has_one() -> None:
    # LayoutMetrics.me is additive (#336): left out of the dump while empty, so a layout with no ME
    # network serializes as it did before, and carried through a round trip when it has one.
    assert "me" not in LayoutMetrics(footprint=4).model_dump(mode="json")
    stone = MEFlowMetrics(resources=("stone",), commodity=Commodity.ITEM, rate=0.1)
    network = MENetworkMetrics(
        id="main",
        mode=MEMode.ATTACHED,
        colour=AEColor.FLUIX,
        power=MEPower.ACCEPTOR,
        devices=6,
        channel_budget=32,
        main_channels=6,
        ae_per_tick=71.9,
        eu_per_tick=35.95,
        acceptor_eu_per_tick=40.0,
        acceptor_source="power-source:HV",
        acceptor_source_amps=1,
        supplies=[stone],
    )
    metrics = LayoutMetrics(footprint=4, me=[network])
    dumped = metrics.model_dump(mode="json")["me"][0]
    assert (dumped["power"], dumped["acceptor_source_amps"]) == ("acceptor", 1)
    assert LayoutMetrics.model_validate_json(metrics.model_dump_json()) == metrics


def test_a_networks_report_never_names_power_as_a_flow() -> None:
    with pytest.raises(ValidationError, match="never power"):
        MEFlowMetrics(resources=("eu",), commodity=Commodity.POWER, rate=1.0)
    with pytest.raises(ValidationError):
        MEFlowMetrics(resources=(), commodity=Commodity.ITEM, rate=1.0)
