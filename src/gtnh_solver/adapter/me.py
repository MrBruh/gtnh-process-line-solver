"""Which of a plan's nets ride ME: the net list a user chooses from, and applying their choice (#332).

ME is chosen per net, in two steps against two versioned contracts (``ir.me``)::

    plan --_map_plan--> machines + nets (pre-merge ids)
                           |
                           +--> net_list() ----> NetList   (gtnh-solve --list-nets)
                           |                         |
                           |                    the user picks
                           |                         v
                           +--> choose_me(MEPlan) --> MEConfig + {net id: network}  --> _close()

The ids are the adapter's **pre-merge** net ids: an edge group's ``"+"``-joined edge ids and an
output buffer's ``output-net:...``, which a plan and a dataset fix (the dataset decides which
machines are multiblocks, whose shared ports group edges). A net that rides ME is never merged
afterwards, so its id survives into the ``InputIR`` unchanged. Both contracts carry the plan's digest
(:func:`plan_digest`) and the dataset's identity (:func:`dataset_identity`), so a choice made
against another plan or dataset is refused with :class:`MEPlanError` rather than applied to nets it
never saw.

``--me items`` and ``--me fluids`` are shorthand for one attached network, ``main``, carrying every
net of those commodities; ``--me power`` leaves the line's EU supply to the builder
(``MEConfig.power_external``). Neither a choice nor the shorthand places anything yet: a net on ME is
still only skipped downstream, until the end-to-end build (#335).
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence

from gtnh_solver import __version__
from gtnh_solver.dataset import VOLTAGE_BY_TIER, PhysicalDataset, me_devices_for
from gtnh_solver.dataset.me import MEShortfall
from gtnh_solver.ir import (
    Commodity,
    IODirection,
    Machine,
    MEConfig,
    MEMode,
    MENetworkSpec,
    MEPlan,
    Net,
    NetEnd,
    NetEntry,
    NetKind,
    NetList,
    Port,
)

from ._errors import MEPlanError
from .plan import Plan

#: The network ``--me items`` / ``--me fluids`` put their nets on: the player's main network.
SHORTHAND_NETWORK = "main"


def plan_digest(plan: Plan) -> str:
    """The SHA-256 of ``plan`` as parsed, which a :class:`NetList` and an :class:`MEPlan` carry."""
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()


def dataset_identity(physical: PhysicalDataset | None) -> str | None:
    """The physical dataset a plan was adapted against, ``"<pack>@<generated_at>"``, or ``None``.

    The dataset decides which machines are multiblocks, and a multiblock's shared port groups the
    plan's edges into one net, so the net ids depend on it.
    """
    if physical is None:
        return None
    return f"{physical.meta.pack_version}@{physical.meta.generated_at}"


def line_tier(machines: Sequence[Machine], storage_ids: Collection[str]) -> str:
    """The highest voltage tier any machine of the line runs at, before the power synthesis
    re-tiers anything; ``"LV"`` for a line with none on the ladder (boundary storages carry a
    placeholder tier and are not machines of the line)."""
    ladder = list(VOLTAGE_BY_TIER)
    tiers = [
        m.voltage_tier
        for m in machines
        if m.id not in storage_ids and m.voltage_tier in VOLTAGE_BY_TIER
    ]
    return max(tiers, key=ladder.index) if tiers else "LV"


def net_list(
    machines: Sequence[Machine],
    nets: Sequence[Net],
    *,
    storage_ids: Collection[str],
    multiblock_ids: Collection[str],
    names: Mapping[str, str],
    digest: str,
    dataset_version: str | None,
) -> NetList:
    """Every item and fluid net of a mapped plan, as a user picking ME needs to see it.

    ``machines`` and ``nets`` are the plan mapped up to its output buffers, before the power
    synthesis and the merges. Each end names the device it would get on a default attached network
    (``dataset.me.me_devices_for``), a single block's output taken as its auto-output.
    """
    tier = line_tier(machines, storage_ids)
    by_id = {m.id: m for m in machines}
    ports = {(m.id, p.id): p for m in machines for p in m.faces.ports}
    entries: list[NetEntry] = []
    for net in nets:
        if net.commodity is Commodity.POWER:
            continue
        producers: list[NetEnd] = []
        consumers: list[NetEnd] = []
        fed = drained = False
        for ref in net.endpoints:
            port = ports[(ref.machine_id, ref.port_id)]
            output = port.direction is IODirection.OUTPUT
            if ref.machine_id in storage_ids:
                fed, drained = fed or output, drained or not output
                continue
            end = _end(by_id[ref.machine_id], port, ref.machine_id in multiblock_ids, tier)
            (producers if output else consumers).append(end)
        kind = (
            NetKind.BOUNDARY_INPUT
            if fed
            else NetKind.BOUNDARY_OUTPUT
            if drained
            else NetKind.INTERNAL
        )
        resource = net.fluid_or_item or ",".join(net.items)
        entries.append(
            NetEntry(
                id=net.id,
                kind=kind,
                commodity=net.commodity,
                resource=resource,
                resource_name=names.get(resource),
                rate=net.throughput,
                producers=tuple(producers),
                consumers=tuple(consumers),
            )
        )
    return NetList(
        plan_digest=digest,
        dataset_version=dataset_version,
        solver_version=__version__,
        line_tier=tier,
        nets=entries,
    )


def _end(machine: Machine, port: Port, multiblock: bool, tier: str) -> NetEnd:
    """One machine end of a listed net, with the device a default attached network gives it."""
    chosen = me_devices_for(
        port.commodity,
        port.direction,
        port.rate,
        multiblock=multiblock,
        machine_tier=machine.voltage_tier,
        line_tier=tier,
        auto_output=not multiblock and port.direction is IODirection.OUTPUT,
    )
    if isinstance(chosen, MEShortfall):
        suggested = f"none keeps up: {chosen.detail}"
    else:
        suggested = chosen[0].label if len(chosen) == 1 else f"{len(chosen)} x {chosen[0].label}"
    return NetEnd(
        machine_id=machine.id,
        port_id=port.id,
        machine_type=machine.type,
        multiblock=multiblock,
        rate=port.rate,
        suggested=suggested,
    )


def choose_me(
    nets: Sequence[Net],
    *,
    digest: str,
    dataset_version: str | None,
    me_plan: MEPlan | None = None,
    me_commodities: Collection[Commodity] = (),
    me_power: bool = False,
) -> tuple[MEConfig, dict[str, str]]:
    """The problem's ME networks, and which pre-merge net rides which.

    From ``me_plan`` when given, else the ``me_commodities`` shorthand (one attached network,
    :data:`SHORTHAND_NETWORK`, carrying every net of those commodities), else none. ``me_power``
    leaves the EU supply to the builder either way. Raises :class:`MEPlanError` for both a plan and
    the shorthand, a plan made against another plan or dataset, or one naming a net that is not an
    item or fluid net of this plan.
    """
    listable = {net.id: net for net in nets if net.commodity is not Commodity.POWER}
    if me_plan is not None and me_commodities:
        raise MEPlanError(
            "--me items/fluids and --me-plan both choose which nets ride ME; give one of them"
        )
    if Commodity.POWER in me_commodities:
        raise MEPlanError("power never rides an ME network; leave it external instead")
    if me_plan is None:
        if not me_commodities:
            return MEConfig(power_external=me_power), {}
        config = MEConfig(
            networks=[MENetworkSpec(id=SHORTHAND_NETWORK, mode=MEMode.ATTACHED)],
            power_external=me_power,
        )
        chosen = {
            net_id: SHORTHAND_NETWORK
            for net_id, net in listable.items()
            if net.commodity in me_commodities
        }
        return config, chosen
    if me_plan.plan_digest != digest or me_plan.dataset_version != dataset_version:
        what = "plan" if me_plan.plan_digest != digest else "dataset"
        raise MEPlanError(
            f"this ME plan was made against another {what} (re-run --list-nets on this plan and "
            f"choose again): its net ids may not name the same nets"
        )
    unknown = sorted(net_id for net_id in me_plan.nets if net_id not in listable)
    if unknown:
        raise MEPlanError(
            f"the ME plan names {len(unknown)} net(s) this plan has no item or fluid net for: "
            f"{', '.join(unknown)} (re-run --list-nets)"
        )
    return MEConfig(networks=me_plan.networks, power_external=me_power), dict(me_plan.nets)
