"""Synthesize the power side of the problem the export omits.

A gtnh-factory-flow export gives each machine an ``eut`` + voltage tier but **no power source
node** - it balances materials, not power. So the adapter invents the power network
(docs/DOMAIN.md, the shared-amperage net; the `power-model` design note):

- group the powered machines (``eut > 0``) by voltage tier;
- for each tier, add **one synthetic source** machine (a power OUTPUT) and give every powered
  machine of that tier a power INPUT port;
- tie them with **one shared-amperage power net** per tier (source + its machines), whose
  segment thickness the power router later sizes to the *summed* amperage.

Correctness-first, single-source-per-tier (the handoff sequencing): multi-source count/position
optimization and voltage-loss are Phase 2. **How the source is itself powered is left to the
builder** - the build guide says so; the layout marks where an external source must feed in.
"""

from __future__ import annotations

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


def _source_id(tier: str) -> str:
    return f"power-source:{tier}"


def _net_id(tier: str) -> str:
    return f"power:{tier}"


def synthesize_power(machines: list[Machine], nets: list[Net]) -> tuple[list[Machine], list[Net]]:
    """Return ``(machines, nets)`` augmented with a per-tier power source + shared-amperage net.

    Machines with ``eut > 0`` gain a power INPUT port; each voltage tier in use gains a source
    machine and a power net. Tiers are processed in sorted order so the output is deterministic.
    Raises :class:`~gtnh_solver.adapter.core.AdapterError` indirectly only via id collision.
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
        source_id = _source_id(tier)
        if source_id in existing_ids:
            raise AdapterError(
                f"synthetic power source id {source_id!r} collides with an export machine id"
            )
        tier_machines = by_tier[tier]
        out_machines.append(_power_source(source_id, tier))
        out_nets.append(
            Net(
                id=_net_id(tier),
                commodity=Commodity.POWER,
                throughput=sum(m.eut for m in tier_machines),  # total EU/t on this tier's trunk
                endpoints=[
                    MachineFaceRef(machine_id=source_id, port_id=POWER_OUT),
                    *(MachineFaceRef(machine_id=m.id, port_id=POWER_IN) for m in tier_machines),
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
