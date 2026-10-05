"""The shipped lines solved with their nets on ME, end to end through the CLI (#335).

``--list-nets`` prints the NetList, an MEPlan made from it goes back in through ``--me-plan``, and
the run lays every network it names: these pin that whole path on the two shipped plans, for each
shape a network takes (attached, a link subnet, a chests subnet) and each GT ME hatch policy, plus
the over-budget plan that has to come back as an explicit infeasibility rather than a layout.

Every solve here is the suite's ``minimal`` one at the CLI's default seed (0), and each is VALID
there (measured on this branch). That is a property of the search, not of the code, so a change
that moves the search can flip one to ``partial_invalid``: then try a few seeds, pin one that is
VALID with a comment saying why, and look at why the default stopped laying it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import gtnh_solver.cli as cli_module
from gtnh_solver.cli import main
from gtnh_solver.dataset.me import me_devices_for, needs_fuzzy_card
from gtnh_solver.ir import (
    Commodity,
    InputIR,
    LayoutResult,
    LayoutStatus,
    MEDeviceKind,
    MEHatchPolicy,
    MEMode,
    MENetworkSpec,
    MEPlan,
    MEPower,
    MERole,
    MEStorage,
    NetList,
)
from gtnh_solver.solver import solve
from gtnh_solver.validator import validate
from gtnh_solver.validator.report import ViolationCode

_ROOT = Path(__file__).resolve().parents[1]
_SAND = str(_ROOT / "examples" / "gtnh-sand.json")
_NITROBENZENE = str(_ROOT / "examples" / "gtnh-nitrobenzene.json")

#: The CLI's exit code for an explicit infeasibility (``cli._report_infeasibility``).
_INFEASIBLE_EXIT = 1

#: The note a link subnet carrying all of sand prints, verbatim: six hammer devices and the link's
#: storage bus on the subnet, one channel of the main network for the one link, and the stone it
#: must stock and the sand it stores there, as with an attached network.
_LINK_NOTE = (
    "note: ME network line (subnet): 7 device(s) on 1 channel(s) of your main network, one per "
    "link; stock Stone (minecraft:stone) 0.1 items/t; it stores Sand (minecraft:sand) 0.1 items/t"
)
#: The note a chests subnet carrying all of sand prints, verbatim: the six hammer devices and a
#: storage bus on each of its four Super Chests, none of the main network's channels, and nothing
#: to stock or collect there, since the line's own chests hold all of it.
_CHESTS_NOTE = (
    "note: ME network line (subnet): 10 device(s) on none of your main network's channels"
)


@pytest.fixture
def solves(monkeypatch: pytest.MonkeyPatch) -> list[tuple[InputIR, LayoutResult]]:
    """Let the CLI solve for real, recording each problem it is handed and the layout it got."""
    solved: list[tuple[InputIR, LayoutResult]] = []

    def recording(problem: InputIR, **kwargs: Any) -> LayoutResult:
        layout = solve(problem, **kwargs)
        solved.append((problem, layout))
        return layout

    monkeypatch.setattr(cli_module, "solve", recording)
    return solved


def _listed(export: str, capsys: pytest.CaptureFixture[str]) -> NetList:
    """The NetList ``gtnh-solve EXPORT --list-nets`` prints, which an MEPlan is made against."""
    assert main([export, "--list-nets"]) == 0
    return NetList.model_validate_json(capsys.readouterr().out)


def _plan_file(
    tmp_path: Path, listed: NetList, network: MENetworkSpec, commodities: set[Commodity]
) -> str:
    """An MEPlan file putting every listed net of ``commodities`` on ``network``."""
    me_plan = MEPlan(
        plan_digest=listed.plan_digest,
        dataset_version=listed.dataset_version,
        networks=[network],
        nets={n.id: network.id for n in listed.nets if n.commodity in commodities},
    )
    path = tmp_path / "me-plan.json"
    path.write_text(me_plan.model_dump_json(), encoding="utf-8")
    return str(path)


def _assert_laid_whole(problem: InputIR, layout: LayoutResult) -> None:
    """VALID, certified by the independent gate, and every ME endpoint built on its network."""
    assert layout.status is LayoutStatus.VALID, layout.infeasibility
    assert validate(problem, layout).ok
    built = {(n.id, d.machine_id, d.endpoint_id) for n in layout.me_networks for d in n.devices}
    assert built == {(e.network, m.id, e.id) for m in problem.machines for e in m.me_endpoints}


# ------------------------------------------------------------------ nitrobenzene, one attached network


@pytest.mark.parametrize("policy", list(MEHatchPolicy), ids=lambda p: p.value)
def test_nitrobenzene_with_items_and_fluids_on_one_attached_network(
    policy: MEHatchPolicy,
    solves: list[tuple[InputIR, LayoutResult]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # The multiblock line on the committed dataset, so each machine has its real footprint and
    # hatch slots, with every item and fluid net on the main network: 28 devices, under one stub's
    # 32 channels. `always` runs beside `tier_aware` and `never` because at HV the tier-aware
    # choice lays no GT ME hatch at all (they start at EV), so only `always` proves the hatches
    # themselves are laid. The CLI's default seed 0 is VALID under all three at minimal effort
    # (so is 1 and 3; seed 2 is not, stalling on me_channels or a power dock), so none is pinned.
    listed = _listed(_NITROBENZENE, capsys)
    network = MENetworkSpec(id="main", mode=MEMode.ATTACHED, hatches=policy)
    path = _plan_file(tmp_path, listed, network, {Commodity.ITEM, Commodity.FLUID})
    assert main([_NITROBENZENE, "--me-plan", path]) == 0
    ((problem, layout),) = solves
    _assert_laid_whole(problem, layout)
    on_me = {n.id for n in problem.nets if n.rides_me}
    assert on_me == {n.id for n in problem.nets if n.commodity is not Commodity.POWER}
    assert all(r.commodity is Commodity.POWER for r in layout.routes)  # no pipe left to lay
    assert [m.id for m in problem.machines if m.me_role is MERole.ATTACH] == ["me-attach:main:1"]

    # Each device is the one the rule data chooses for its port under this policy.
    ends = {(e.machine_id, e.port_id): e for n in listed.nets for e in (*n.producers, *n.consumers)}
    expected_gt: set[tuple[str, str]] = set()
    for machine in problem.machines:
        ports = {p.id: p for p in machine.faces.ports}
        for endpoint in machine.me_endpoints:
            (port_id,) = endpoint.ports
            port, end = ports[port_id], ends[(machine.id, port_id)]
            chosen = me_devices_for(
                port.commodity,
                port.direction,
                port.rate,
                multiblock=end.multiblock,
                machine_tier=machine.voltage_tier,
                line_tier=listed.line_tier,
                hatches=policy,
                fuzzy=needs_fuzzy_card(port),
            )
            assert isinstance(chosen, tuple), chosen
            device = endpoint.device
            assert (device.kind, device.gt_mid, endpoint.hatch_kind) == (
                chosen[0].kind,
                chosen[0].gt_mid,
                chosen[0].hatch_kind,
            )
            if chosen[0].gt_mid is not None:
                expected_gt.add((machine.id, endpoint.id))
            # A multiblock's connection takes a hatch of the kind its endpoint names, on its port.
            assert end.multiblock
            assert any(
                h.machine_id == machine.id
                and h.port_id == port_id
                and h.kind == endpoint.hatch_kind
                for h in layout.hatches
            )
    (laid,) = layout.me_networks
    gt_laid = {(d.machine_id, d.endpoint_id) for d in laid.devices if d.gt_mid is not None}
    assert gt_laid == expected_gt
    if policy is MEHatchPolicy.ALWAYS:
        # Every connection is a GT ME hatch but the Coke Oven's feed of any log: a stocking bus
        # matches only the exact stacks set in it, so a normal input bus takes it, fed by an export
        # bus with a Fuzzy Card (#353).
        assert (len(gt_laid), len(laid.devices)) == (27, 28)
        (fed,) = [d for d in laid.devices if d.gt_mid is None]
        assert (fed.kind, fed.config, fed.cards.fuzzy) == (
            MEDeviceKind.EXPORT_BUS,
            ("minecraft:log@32767",),
            1,
        )
    else:
        # `never` by definition, and `tier_aware` because nitrobenzene is an HV line.
        assert listed.line_tier == "HV"
        assert gt_laid == set()
        assert {d.kind for d in laid.devices} == {
            MEDeviceKind.EXPORT_BUS,
            MEDeviceKind.FLUID_EXPORT_BUS,
            MEDeviceKind.INTERFACE,
            MEDeviceKind.DUAL_INTERFACE,
        }
    assert any(
        line.startswith("note: ME network main (attached): 28 device(s) on 28 channel(s) ")
        for line in capsys.readouterr().err.splitlines()
    )


def test_nitrobenzene_with_items_on_me_feeds_its_coke_oven_any_log_through_a_fuzzy_card(
    solves: list[tuple[InputIR, LayoutResult]],
) -> None:
    """#353, as it was found: ``gtnh-nitrobenzene.json --me items``. The Coke Oven burns
    ``minecraft:log@32767``, any log, which AE2 matches only through a Fuzzy Card. Its export bus is
    laid with one, still set to the wildcard, and the layout is VALID; the same bus without the
    card, the build this used to lay, is refused by the gate."""
    assert main([_NITROBENZENE, "--me", "items"]) == 0
    ((problem, layout),) = solves
    _assert_laid_whole(problem, layout)
    (laid,) = layout.me_networks
    (logs,) = [d for d in laid.devices if d.config == ("minecraft:log@32767",)]
    coke_oven = next(m for m in problem.machines if m.id == logs.machine_id)
    assert (coke_oven.type, logs.kind, logs.cards.fuzzy) == (
        "Coke Oven",
        MEDeviceKind.EXPORT_BUS,
        1,
    )
    assert [d for d in laid.devices if d.cards.fuzzy] == [logs]

    # Strip the card from the endpoint and the bus alike: exactly the pre-#353 build.
    bare = logs.cards.model_copy(update={"fuzzy": 0})
    endpoints = tuple(
        e.model_copy(update={"device": e.device.model_copy(update={"cards": bare})})
        if e.id == logs.endpoint_id
        else e
        for e in coke_oven.me_endpoints
    )
    oven = coke_oven.model_copy(update={"me_endpoints": endpoints})
    old_problem = problem.model_copy(
        update={"machines": [oven if m.id == oven.id else m for m in problem.machines]}
    )
    devices = [d.model_copy(update={"cards": bare}) if d == logs else d for d in laid.devices]
    old_layout = layout.model_copy(
        update={"me_networks": [laid.model_copy(update={"devices": devices})]}
    )
    assert set(validate(old_problem, old_layout).codes()) == {ViolationCode.ME_FUZZY_CARD_MISSING}


# ------------------------------------------------------------------ sand, on a subnet


def test_sand_on_a_link_subnet_lays_its_link_and_spends_one_main_channel(
    solves: list[tuple[InputIR, LayoutResult]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    listed = _listed(_SAND, capsys)
    network = MENetworkSpec(id="line", mode=MEMode.SUBNET, storage=MEStorage.LINK)
    assert main([_SAND, "--me-plan", _plan_file(tmp_path, listed, network, {Commodity.ITEM})]) == 0
    ((problem, layout),) = solves
    _assert_laid_whole(problem, layout)
    # The main network is the storage, reached through one item link: the chests are gone, and
    # seven devices need no controller (ad hoc carries eight).
    assert not [m for m in problem.machines if m.type == "Super Chest"]
    roles = [(m.id, m.me_role) for m in problem.machines if m.me_role is not None]
    assert roles == [("me-link:line:item", MERole.LINK)]
    (laid,) = layout.me_networks
    assert ("me-link:line:item", MEDeviceKind.STORAGE_BUS) in {
        (d.machine_id, d.kind) for d in laid.devices
    }
    link = next(p for p in layout.placements if p.machine_id == "me-link:line:item")
    assert link.cell.as_tuple() in laid.cells()  # a link is a cable block of its network
    assert _LINK_NOTE in capsys.readouterr().err.splitlines()


def test_sand_on_a_chests_subnet_reads_its_chests_through_storage_buses(
    solves: list[tuple[InputIR, LayoutResult]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    listed = _listed(_SAND, capsys)
    network = MENetworkSpec(id="line", mode=MEMode.SUBNET, storage=MEStorage.CHESTS)
    assert main([_SAND, "--me-plan", _plan_file(tmp_path, listed, network, {Commodity.ITEM})]) == 0
    ((problem, layout),) = solves
    _assert_laid_whole(problem, layout)
    # The stone feed and the sand buffer stay, and each internal hop gets a buffer of its own; each
    # is read by a storage bus partitioned to what it holds. Six hammer devices and four storage
    # buses are over ad hoc's eight, so a controller.
    chests = {m.id: m for m in problem.machines if m.type == "Super Chest"}
    assert len(chests) == 4
    assert len([c for c in chests if c.startswith("me-buffer:")]) == 2
    (laid,) = layout.me_networks
    buses = {d.machine_id: d for d in laid.devices if d.kind is MEDeviceKind.STORAGE_BUS}
    assert set(buses) == set(chests)
    for chest_id, bus in buses.items():
        (port,) = chests[chest_id].faces.ports
        assert bus.config == (port.id.split(":", 1)[1],)
    roles = [(m.id, m.me_role) for m in problem.machines if m.me_role is not None]
    assert roles == [("me-controller:line", MERole.CONTROLLER)]
    assert _CHESTS_NOTE in capsys.readouterr().err.splitlines()


@pytest.mark.parametrize("flags", [[], ["--fast"]], ids=["default", "fast"])
def test_sand_on_a_link_subnet_powered_by_an_energy_acceptor(
    flags: list[str],
    solves: list[tuple[InputIR, LayoutResult]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # #336: the adapter places an Energy Acceptor on sand's LV trunk, rated for an estimate of what
    # the network draws, and the laid network stays under it; the layout printed on stdout reports
    # the laid figure for a reader of the layout alone, and the run says where the power comes from.
    # A fast run lays it too (#352), as gtnh-solver-site runs one: its constructive placement put
    # the acceptor against the link, which the ME router refuses, so it is one minimal attempt.
    listed = _listed(_SAND, capsys)
    network = MENetworkSpec(id="line", mode=MEMode.SUBNET, power=MEPower.ACCEPTOR)
    path = _plan_file(tmp_path, listed, network, {Commodity.ITEM})
    assert main([_SAND, "--me-plan", path, *flags]) == 0
    ((problem, layout),) = solves
    _assert_laid_whole(problem, layout)
    (acceptor,) = [m for m in problem.machines if m.me_role is MERole.ACCEPTOR]
    assert (acceptor.id, acceptor.voltage_tier) == ("me-acceptor:line", "LV")
    (metrics,) = layout.metrics.me
    assert metrics.power is MEPower.ACCEPTOR
    assert 0 < metrics.eu_per_tick <= acceptor.eut
    assert (metrics.acceptor_eu_per_tick, metrics.acceptor_source) == (
        acceptor.eut,
        "power-source:LV",
    )
    assert metrics.acceptor_source_amps
    printed = capsys.readouterr()
    assert LayoutResult.model_validate_json(printed.out).metrics.me == [metrics]
    assert any(
        line.startswith("note: ME network line (subnet): its Energy Acceptor draws ")
        and "feed power-source:LV no more than that" in line
        for line in printed.err.splitlines()
    )


# ------------------------------------------------------------------ over budget


def test_an_attached_network_over_its_channel_budget_is_an_explicit_infeasibility(
    solves: list[tuple[InputIR, LayoutResult]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Sand on the main network takes six channels; the player says only five are free.
    listed = _listed(_SAND, capsys)
    network = MENetworkSpec(id="main", mode=MEMode.ATTACHED, me_channel_budget=5)
    path = _plan_file(tmp_path, listed, network, {Commodity.ITEM})
    assert main([_SAND, "--me-plan", path]) == _INFEASIBLE_EXIT
    assert not solves  # refused before any solve: no layout fits a budget the devices exceed
    out, err = capsys.readouterr()
    layout = LayoutResult.model_validate(json.loads(out))
    assert layout.status is LayoutStatus.INFEASIBLE
    assert layout.infeasibility is not None
    assert layout.infeasibility.constraint == "me_channel_budget"
    assert not layout.placements
    assert not layout.me_networks
    assert "[infeasible] me_channel_budget: ME network 'main' needs 6 channels" in err
