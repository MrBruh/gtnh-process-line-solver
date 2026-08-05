"""Synthesize the power side of the problem the export omits.

A gtnh-factory-flow export gives each machine an ``eut`` + voltage tier but **no power source
node** - it balances materials, not power. So the adapter invents the power network
(docs/DOMAIN.md, the shared-amperage net; the `power-model` design note):

- group the powered machines (``eut > 0``) by voltage tier;
- split each tier into **groups one cable run can carry** (:func:`_partition_by_amperage`),
  because a shared-amperage trunk sums its machines and a cable tops out at 16x;
- for each group, add a **synthetic source** machine (a power OUTPUT) and give every powered
  machine in it a power INPUT port;
- tie them with **one shared-amperage power net** per group (source + its machines), whose
  segment thickness the power router later sizes to the *summed* amperage.

A tier that fits on one run keeps the single-source ids (``power-source:MV`` / ``power:MV``); a
tier that has to split suffixes them (``power-source:MV#1``, ``power:MV#2``, ...), so the common
case reads unchanged in the build guide and previewer.

**How each source is itself powered is left to the builder** - the build guide says so; the layout
marks where an external source must feed in. Source *position* optimization (packing groups by
proximity rather than by load alone) is still Phase 2.
"""

from __future__ import annotations

from gtnh_solver.dataset import (
    MAX_CABLE_THICKNESS,
    UnknownTierError,
    amp_load,
    whole_amps,
)
from gtnh_solver.ir import (
    Commodity,
    FaceSpec,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Port,
)
from gtnh_solver.ir.enums import HORIZONTAL_FACINGS_ORDERED

from ._errors import AdapterError

#: Port ids the synthesis adds (kept distinct from the adapter's ``direction:resource`` ids).
POWER_IN = "power:in"
POWER_OUT = "power:out"
_DEFAULT_ORIENTATIONS = list(HORIZONTAL_FACINGS_ORDERED)  # front defaults to the first (NORTH)


def _source_id(tier: str, index: int = 0, total: int = 1) -> str:
    return f"power-source:{tier}" if total == 1 else f"power-source:{tier}#{index + 1}"


def _net_id(tier: str, index: int = 0, total: int = 1) -> str:
    return f"power:{tier}" if total == 1 else f"power:{tier}#{index + 1}"


def _partition_by_amperage(tier: str, tier_machines: list[Machine]) -> list[list[Machine]]:
    """Split one tier's machines into groups a single cable run can carry.

    First-fit-decreasing on each machine's *nominal* amp load (at-source voltage, no loss).
    Distances are unknown until placement, so this reserves no loss headroom: a group that cable
    loss later pushes over the cap is reported by the router, not silently accepted.

    A machine whose own load already exceeds the cap lands in a group of its own. No partition can
    help it (one machine, one feed), so the router's over-cap infeasibility is the honest outcome -
    it needs parallel runs or a higher tier, which is Phase 2 (docs/ROADMAP.md).

    Machines keep their export order within a group; groups are ordered by their heaviest member,
    so the output is deterministic.
    """
    try:
        loads = {m.id: amp_load(m.eut, tier) for m in tier_machines}
    except UnknownTierError:
        # Off-ladder tier: amperage is unverifiable here. Keep the single-net shape so the router
        # and validator report the unknown tier, rather than the adapter raising earlier and
        # turning a reported violation into a hard failure.
        return [tier_machines]

    order = {m.id: i for i, m in enumerate(tier_machines)}
    groups: list[list[Machine]] = []
    carried: list[float] = []
    for machine in sorted(tier_machines, key=lambda m: (-loads[m.id], m.id)):
        load = loads[machine.id]
        for i, so_far in enumerate(carried):
            if whole_amps(so_far + load) <= MAX_CABLE_THICKNESS:
                groups[i].append(machine)
                carried[i] = so_far + load
                break
        else:
            groups.append([machine])
            carried.append(load)
    return [sorted(group, key=lambda m: order[m.id]) for group in groups]


def synthesize_power(machines: list[Machine], nets: list[Net]) -> tuple[list[Machine], list[Net]]:
    """Return ``(machines, nets)`` augmented with synthetic power sources + shared-amperage nets.

    Machines with ``eut > 0`` gain a power INPUT port; each voltage tier in use is split into
    groups a single cable run can carry (:func:`_partition_by_amperage`), and each group gains a
    source machine and a power net. Tiers are processed in sorted order so the output is
    deterministic. Raises :class:`~gtnh_solver.adapter.core.AdapterError` only via id collision.
    """
    by_tier: dict[str, list[Machine]] = {}
    for m in machines:
        if m.eut > 0:
            by_tier.setdefault(m.voltage_tier, []).append(m)
    if not by_tier:
        return machines, nets  # nothing draws power (e.g. only storages, or zero-eut recipes)

    existing_ids = {m.id for m in machines}
    powered_ids = {m.id for tier_machines in by_tier.values() for m in tier_machines}

    # Append a power INPUT port to every powered machine (its other ports are untouched).
    out_machines = [_with_power_input(m) if m.id in powered_ids else m for m in machines]
    out_nets = list(nets)

    for tier in sorted(by_tier):
        groups = _partition_by_amperage(tier, by_tier[tier])
        for index, group in enumerate(groups):
            source_id = _source_id(tier, index, len(groups))
            if source_id in existing_ids:
                raise AdapterError(
                    f"synthetic power source id {source_id!r} collides with an export machine id"
                )
            out_machines.append(_power_source(source_id, tier))
            out_nets.append(
                Net(
                    id=_net_id(tier, index, len(groups)),
                    commodity=Commodity.POWER,
                    throughput=sum(m.eut for m in group),  # total EU/t on this group's trunk
                    endpoints=[
                        MachineFaceRef(machine_id=source_id, port_id=POWER_OUT),
                        *(MachineFaceRef(machine_id=m.id, port_id=POWER_IN) for m in group),
                    ],
                )
            )
    return out_machines, out_nets


def _with_power_input(machine: Machine) -> Machine:
    port = Port(id=POWER_IN, commodity=Commodity.POWER, direction=IODirection.INPUT)
    return machine.model_copy(update={"faces": FaceSpec(ports=[*machine.faces.ports, port])})


def _power_source(source_id: str, tier: str) -> Machine:
    return Machine(
        id=source_id,
        type=f"Power Source ({tier})",
        voltage_tier=tier,
        eut=0.0,  # a source supplies power, it does not draw it
        orientation_options=_DEFAULT_ORIENTATIONS,
        faces=FaceSpec(
            ports=[Port(id=POWER_OUT, commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )
