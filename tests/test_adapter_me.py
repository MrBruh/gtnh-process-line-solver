"""Which of a plan's nets ride ME: the net list and applying a choice (#332, ``adapter.me``).

The promise a user relies on is that the id they pick a net by in ``--list-nets`` is the id the
problem carries: a net on ME is never merged, so whatever subset they choose, every chosen net
arrives in the ``InputIR`` under its listed id, on the network they named. The property test below
holds every shipped example and a line that does merge (three Ore Washers sorting their outputs
through Item Filters) to that, for any subset. The rest pins the choice rules: the shorthand, the
plan, and each way a plan is refused.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gtnh_solver.adapter import (
    AdapterWarning,
    MEPlanError,
    Plan,
    list_nets,
    load_plan,
    plan_digest,
    to_input_ir,
)
from gtnh_solver.adapter.me import choose_me, dataset_identity, line_tier
from gtnh_solver.ir import (
    Commodity,
    MEConfig,
    MEMode,
    MENetworkSpec,
    MEPlan,
    Net,
)
from tests._helpers import machine, net, property_examples
from tests.test_adapter_item_filters import _dataset, _washer_plan

_ROOT = Path(__file__).resolve().parents[1]
_PLANS: dict[str, Plan] = {
    **{p.name: load_plan(p) for p in sorted((_ROOT / "examples").glob("*.json"))},
    "washers": _washer_plan(),
}
_NETWORKS = [
    MENetworkSpec(id="main", mode=MEMode.ATTACHED),
    MENetworkSpec(id="sub", mode=MEMode.SUBNET),
]


def _adapt(plan: Plan, **kwargs: object) -> object:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)  # pack and census notes, not the subject
        return to_input_ir(plan, **kwargs)  # type: ignore[arg-type]


@settings(
    max_examples=property_examples(40),
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(data=st.data(), name=st.sampled_from(sorted(_PLANS)))
def test_every_chosen_net_keeps_its_listed_id(data: st.DataObject, name: str) -> None:
    plan = _PLANS[name]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        listed = list_nets(plan)
    chosen = {
        entry.id: data.draw(st.sampled_from(["main", "sub"]))
        for entry in listed.nets
        if data.draw(st.booleans())
    }
    me_plan = MEPlan(plan_digest=listed.plan_digest, networks=_NETWORKS, nets=chosen)
    problem = _adapt(plan, me_plan=me_plan)
    nets = {n.id: n for n in problem.nets}  # type: ignore[attr-defined]
    for net_id, network in chosen.items():
        assert nets[net_id].me_network == network
    # And nothing else rides ME: the choice is exactly what was asked for.
    assert {n.id for n in nets.values() if n.me_network is not None} == set(chosen)


@pytest.mark.parametrize("name", sorted(_PLANS))
def test_the_net_list_is_deterministic_and_names_the_problems_nets(name: str) -> None:
    plan = _PLANS[name]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        first, second = list_nets(plan), list_nets(plan)
    assert first == second
    # Every net on one network roomy enough for the line's devices (the EV nitrobenzene lines need
    # more than the default budget's 32 channels, which the build refuses, #335).
    roomy = MENetworkSpec(id="main", mode=MEMode.ATTACHED, me_channel_budget=1024)
    every = MEPlan(
        plan_digest=first.plan_digest,
        networks=[roomy],
        nets={e.id: "main" for e in first.nets},
    )
    unmerged = _adapt(plan, me_plan=every)
    # On ME, nothing merges, so the problem's item and fluid nets are exactly the listed ones.
    assert [e.id for e in first.nets] == [
        n.id
        for n in unmerged.nets  # type: ignore[attr-defined]
        if n.commodity is not Commodity.POWER
    ]


def test_the_net_list_ends_and_suggestions() -> None:
    listed = list_nets(_washer_plan())
    assert listed.line_tier == "LV"
    by_id = {e.id: e for e in listed.nets}
    ore = by_id["e-ore"]
    # Fed from a boundary chest, which is not an end: three washers take it, each by an export bus.
    assert ore.kind.value == "boundary_input"
    assert ore.producers == ()
    assert [e.machine_id for e in ore.consumers] == ["w#1", "w#2", "w#3"]
    assert {e.suggested for e in ore.consumers} == {"ME Export Bus"}
    product = by_id["e-gt.dust.a"]
    assert product.kind.value == "boundary_output"
    assert {e.suggested for e in product.producers} == {"ME Interface"}
    water = by_id["e-water"]
    assert water.commodity is Commodity.FLUID


def test_a_list_against_a_dataset_carries_its_identity() -> None:
    dataset = _dataset()
    listed = list_nets(_washer_plan(), physical=dataset)
    assert listed.dataset_version == "2.8.4@2026-01-01T00:00:00Z"
    assert listed.dataset_version == dataset_identity(dataset)
    assert dataset_identity(None) is None


def test_line_tier_is_the_highest_machine_tier() -> None:
    machines = [
        machine("a", []).model_copy(update={"voltage_tier": "HV"}),
        machine("b", []).model_copy(update={"voltage_tier": "EV"}),
        machine("c", []).model_copy(update={"voltage_tier": "UV"}),
        machine("odd", []).model_copy(update={"voltage_tier": "???"}),
    ]
    assert line_tier(machines, storage_ids={"c"}) == "EV"  # a storage's placeholder is not a tier
    assert line_tier([], storage_ids=()) == "LV"


# ------------------------------------------------------------------ choosing


def _nets() -> list[Net]:
    return [
        net("i", "a", "b"),
        net("f", "a", "b", commodity=Commodity.FLUID),
        net("p", "s", "a", commodity=Commodity.POWER),
    ]


def test_no_choice_rides_nothing() -> None:
    config, chosen = choose_me(_nets(), digest="d", dataset_version=None)
    assert (config, chosen) == (MEConfig(), {})
    config, _ = choose_me(_nets(), digest="d", dataset_version=None, me_power=True)
    assert config.power_external


def test_the_shorthand_is_one_attached_network() -> None:
    config, chosen = choose_me(
        _nets(), digest="d", dataset_version=None, me_commodities={Commodity.FLUID}
    )
    assert [(n.id, n.mode) for n in config.networks] == [("main", MEMode.ATTACHED)]
    assert chosen == {"f": "main"}


def test_power_is_never_a_networks() -> None:
    with pytest.raises(MEPlanError, match="power never rides"):
        choose_me(_nets(), digest="d", dataset_version=None, me_commodities={Commodity.POWER})


def test_a_plan_is_applied_as_written() -> None:
    me_plan = MEPlan(plan_digest="d", networks=_NETWORKS, nets={"i": "sub"})
    config, chosen = choose_me(_nets(), digest="d", dataset_version=None, me_plan=me_plan)
    assert config.networks == _NETWORKS
    assert chosen == {"i": "sub"}


@pytest.mark.parametrize(
    ("me_plan", "kwargs", "message"),
    [
        (MEPlan(plan_digest="other"), {}, "another plan"),
        (MEPlan(plan_digest="d", dataset_version="x@y"), {}, "another dataset"),
        (
            MEPlan(plan_digest="d", networks=_NETWORKS, nets={"p": "main"}),
            {},
            "no item or fluid net for: p",
        ),
        (MEPlan(plan_digest="d"), {"me_commodities": {Commodity.ITEM}}, "give one of them"),
    ],
)
def test_a_plan_that_does_not_fit_is_refused(
    me_plan: MEPlan, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(MEPlanError, match=message):
        choose_me(_nets(), digest="d", dataset_version=None, me_plan=me_plan, **kwargs)  # type: ignore[arg-type]


def test_a_plan_for_the_digest_of_another_plan_is_refused_end_to_end() -> None:
    plan = _washer_plan()
    stale = MEPlan(plan_digest=plan_digest(_washer_plan(count=2)), networks=_NETWORKS)
    with pytest.raises(MEPlanError, match="another plan"):
        _adapt(plan, me_plan=stale)
