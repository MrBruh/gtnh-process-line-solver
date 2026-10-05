"""What a plan's ME side needs built (#335, ``adapter.me_build``).

From the per-net choice to InputIR v9: the boundary storages each mode drops or keeps, each port's
device, the single block's auto-output rule, and each network's stubs, links and controller. These
pin the adapter's half alone, against the shipped plans and a small Ore Washer line; the router and
the validator take it from there (``test_router_me``, ``test_validator_me``).
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from gtnh_solver.adapter import (
    AdapterWarning,
    InfeasiblePlanError,
    MEPlanError,
    Plan,
    list_nets,
    load_plan,
    plan_digest,
    to_input_ir,
)
from gtnh_solver.adapter.me_build import build_me
from gtnh_solver.dataset import load_physical_dataset
from gtnh_solver.ir import (
    AEColor,
    Commodity,
    FaceSpec,
    Facing,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    MEConfig,
    MEDeviceKind,
    MEHatchPolicy,
    MEMode,
    MENetworkSpec,
    MEPlan,
    MERole,
    MEStorage,
    Net,
    Port,
)
from tests.test_adapter_item_filters import _washer_plan

_ROOT = Path(__file__).resolve().parents[1]
_SAND = load_plan(_ROOT / "examples" / "gtnh-sand.json")
_NITROBENZENE = load_plan(_ROOT / "examples" / "gtnh-nitrobenzene.json")


def _adapt(plan: Plan, **kwargs: object) -> InputIR:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        return to_input_ir(plan, **kwargs)  # type: ignore[arg-type]


def _plan(plan: Plan, networks: list[MENetworkSpec], nets: dict[str, str], **kw: object) -> MEPlan:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AdapterWarning)
        listed = list_nets(plan, **kw)  # type: ignore[arg-type]
    return MEPlan(
        plan_digest=plan_digest(plan),
        dataset_version=listed.dataset_version,
        networks=networks,
        nets=nets,
    )


def _roles(ir: InputIR) -> list[tuple[str, MERole]]:
    return [(m.id, m.me_role) for m in ir.machines if m.me_role is not None]


def _devices(machine: Machine) -> list[tuple[str, MEDeviceKind]]:
    return [(e.id, e.device.kind) for e in machine.me_endpoints]


# ------------------------------------------------------------------ attached: sand


def test_sand_on_the_main_network_drops_its_chests_and_gets_one_stub() -> None:
    ir = _adapt(_SAND, me_commodities={Commodity.ITEM})
    assert _roles(ir) == [("me-attach:main:1", MERole.ATTACH)]
    stub = next(m for m in ir.machines if m.me_role is MERole.ATTACH)
    assert stub.outside_front
    assert not [m for m in ir.machines if m.type in ("Super Chest", "Super Tank")]
    hammers = [m for m in ir.machines if m.type == "Forge Hammer"]
    assert len(hammers) == 3
    for hammer in hammers:
        kinds = sorted(k.value for _, k in _devices(hammer))
        assert kinds == ["export_bus", "interface"]
        bus = next(e for e in hammer.me_endpoints if e.device.kind is MEDeviceKind.EXPORT_BUS)
        # Configured to its item, or an export bus moves nothing (spike 4.2).
        assert bus.device.config == (bus.ports[0].split(":", 1)[1],)
    # Every net on ME keeps its machine ends only.
    for net in ir.nets:
        if net.rides_me:
            assert all(not e.machine_id.startswith("storage") for e in net.endpoints)


def test_nothing_is_built_for_a_line_without_me() -> None:
    plain = _adapt(_SAND)
    assert not _roles(plain)
    assert not any(m.me_endpoints for m in plain.machines)


def test_an_attached_network_over_its_budget_is_infeasible() -> None:
    nets = {n.id: "main" for n in list_nets(_SAND).nets}
    me_plan = _plan(
        _SAND, [MENetworkSpec(id="main", mode=MEMode.ATTACHED, me_channel_budget=5)], nets
    )
    with pytest.raises(InfeasiblePlanError) as caught:
        _adapt(_SAND, me_plan=me_plan)
    assert caught.value.infeasibility.constraint == "me_channel_budget"
    assert "subnet" in (caught.value.infeasibility.suggested_relaxation or "")


# ------------------------------------------------------------------ subnets: link and chests


def test_a_link_subnet_gets_a_link_per_commodity_and_drops_its_chests() -> None:
    nets = {n.id: "line" for n in list_nets(_SAND).nets}
    me_plan = _plan(_SAND, [MENetworkSpec(id="line", mode=MEMode.SUBNET)], nets)
    ir = _adapt(_SAND, me_plan=me_plan)
    assert _roles(ir) == [("me-link:line:item", MERole.LINK)]
    link = next(m for m in ir.machines if m.me_role is MERole.LINK)
    assert link.outside_front
    assert [(e.ports, e.device.kind) for e in link.me_endpoints] == [((), MEDeviceKind.STORAGE_BUS)]
    # Six devices and the link's storage bus: ad hoc, no controller.
    assert ir.me.colour("line") is AEColor.WHITE


def test_a_chests_subnet_keeps_its_chests_and_buffers_its_internal_nets() -> None:
    nets = {n.id: "line" for n in list_nets(_SAND).nets}
    me_plan = _plan(
        _SAND, [MENetworkSpec(id="line", mode=MEMode.SUBNET, storage=MEStorage.CHESTS)], nets
    )
    ir = _adapt(_SAND, me_plan=me_plan)
    chests = [m for m in ir.machines if m.type == "Super Chest"]
    # The stone feed and the sand buffer, plus one buffer for each internal hop.
    assert len(chests) == 4
    assert all(_devices(c) and _devices(c)[0][1] is MEDeviceKind.STORAGE_BUS for c in chests)
    buffers = [c for c in chests if c.id.startswith("me-buffer:")]
    assert len(buffers) == 2
    assert all(e.device.config for c in chests for e in c.me_endpoints)  # partitioned
    # 6 hammer devices + 4 storage buses: over 8, so a controller.
    assert _roles(ir) == [("me-controller:line", MERole.CONTROLLER)]


# ------------------------------------------------------------------ a single block's outputs


def test_a_piped_output_keeps_the_face_and_an_me_output_is_pulled() -> None:
    plan = _washer_plan(count=1)
    me_plan = _plan(plan, [MENetworkSpec(id="main", mode=MEMode.ATTACHED)], {"e-gt.dust.c": "main"})
    ir = _adapt(plan, me_plan=me_plan)
    washer = next(m for m in ir.machines if m.id == "w")
    assert _devices(washer) == [("me:output:gt.dust.c", MEDeviceKind.IMPORT_BUS)]
    (bus,) = washer.me_endpoints
    assert bus.device.config == ("gt.dust.c",)  # pulls its own item, not every output


def test_outputs_all_on_one_network_share_one_interface() -> None:
    plan = _washer_plan(count=1, fluids=("waste",))
    listed = list_nets(plan)
    outputs = {n.id: "main" for n in listed.nets if n.kind.value == "boundary_output"}
    me_plan = _plan(plan, [MENetworkSpec(id="main", mode=MEMode.ATTACHED)], outputs)
    ir = _adapt(plan, me_plan=me_plan)
    washer = next(m for m in ir.machines if m.id == "w")
    (receiver,) = [e for e in washer.me_endpoints if e.id == "me:auto-output"]
    # Items and a fluid on one network: one Dual Interface takes both (spike 4.6).
    assert receiver.device.kind is MEDeviceKind.DUAL_INTERFACE
    assert len(receiver.ports) == 4


def test_a_port_on_me_and_a_pipe_at_once_is_refused() -> None:
    plan = _washer_plan(count=1, second_drain="gt.dust.a")
    me_plan = _plan(plan, [MENetworkSpec(id="main", mode=MEMode.ATTACHED)], {"e-gt.dust.a": "main"})
    with pytest.raises(MEPlanError, match="choose the same for every net on it"):
        _adapt(plan, me_plan=me_plan)


# ------------------------------------------------------------------ multiblocks


def test_a_multiblock_gets_gt_me_hatches_once_the_line_reaches_their_tier() -> None:
    physical = load_physical_dataset()
    listed = list_nets(_NITROBENZENE, physical=physical)
    fluids = {n.id: "main" for n in listed.nets if n.commodity is Commodity.FLUID}
    for policy, expect_gt in ((MEHatchPolicy.ALWAYS, True), (MEHatchPolicy.NEVER, False)):
        me_plan = _plan(
            _NITROBENZENE,
            [MENetworkSpec(id="main", mode=MEMode.ATTACHED, hatches=policy)],
            fluids,
            physical=physical,
        )
        ir = _adapt(_NITROBENZENE, physical=physical, me_plan=me_plan)
        on_multiblocks = [e for m in ir.machines if m.hatch_slots for e in m.me_endpoints]
        assert on_multiblocks
        assert all(e.hatch_kind is not None for e in on_multiblocks)
        assert all((e.device.gt_mid is not None) is expect_gt for e in on_multiblocks)


def test_a_tank_feeding_one_machine_over_me_and_another_by_pipe_stays_for_the_pipe() -> None:
    # The main network stands in for the tank only on the net that rides it; the piped net still
    # draws from the tank, so the tank and its port stay (dropping them left that net naming a
    # machine the problem no longer had).
    tank = Machine(
        id="s0",
        type="Super Tank",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id="output:water", commodity=Commodity.FLUID, direction=IODirection.OUTPUT)]
        ),
    )
    users = [
        Machine(
            id=f"m{i}",
            type="t",
            voltage_tier="LV",
            orientation_options=[Facing.NORTH],
            faces=FaceSpec(
                ports=[
                    Port(id="input:water", commodity=Commodity.FLUID, direction=IODirection.INPUT)
                ]
            ),
        )
        for i in (1, 2)
    ]

    def water(net_id: str, user: str, network: str | None) -> Net:
        return Net(
            id=net_id,
            commodity=Commodity.FLUID,
            fluid_or_item="water",
            throughput=1.0,
            me_network=network,
            endpoints=[
                MachineFaceRef(machine_id="s0", port_id="output:water"),
                MachineFaceRef(machine_id=user, port_id="input:water"),
            ],
        )

    me = MEConfig(networks=[MENetworkSpec(id="main", mode=MEMode.ATTACHED)])
    machines, nets = build_me(
        [tank, *users],
        [water("on-me", "m1", "main"), water("piped", "m2", None)],
        me,
        storage_ids={"s0"},
        multiblock_ids=set(),
        line_tier="LV",
        recipe_ticks={},
    )
    kept = next(m for m in machines if m.id == "s0")
    assert [p.id for p in kept.faces.ports] == ["output:water"]
    by_id = {n.id: n for n in nets}
    assert [e.machine_id for e in by_id["on-me"].endpoints] == ["m1"]
    assert [e.machine_id for e in by_id["piped"].endpoints] == ["s0", "m2"]
