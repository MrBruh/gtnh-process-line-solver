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
    MEConfig,
    MEMode,
    MENetworkSpec,
    MEPlan,
    MEStorage,
    Net,
    NetEnd,
    NetEntry,
    NetKind,
    NetList,
)
from gtnh_solver.ir.nets import connection_counts
from tests._helpers import consumer, net, on_me, power_source, producer


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
