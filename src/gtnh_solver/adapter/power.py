"""Synthesize the power side of the problem the export omits.

A gtnh-factory-flow export gives each machine an ``eut`` + voltage tier but **no power source
node** - it balances materials, not power. So the adapter invents the power network
(docs/DOMAIN.md, the shared-amperage net; the `power-model` design note):

- give every powered machine (``eut > 0``) the **energy hatches its draw needs**
  (:func:`_power_ports`), each one a power INPUT port carrying its share of the machine's EU/t;
- group those *feeds* by voltage tier;
- split each tier into **groups one cable run can carry** (:func:`_partition_by_amperage`),
  because a shared-amperage trunk sums its feeds and a cable tops out at 16x;
- for each group, add a **synthetic source** machine (a power OUTPUT);
- tie them with **one shared-amperage power net** per group (source + its feeds), whose segment
  thickness the power router later sizes to the *summed* amperage.

**A machine is not one connection.** A GT energy hatch accepts 2 A
(``dataset.ENERGY_HATCH_AMPS``) and a multiblock's intake is the sum over its hatches, so a
machine drawing more than one hatch can take is fed through several - and a multiblock's casing
cells accept a hatch of any kind, power included, so there is room for them (docs/DOMAIN.md).
That is what lets a single heavy machine be powered at all: its hatches spread over as many cable
runs as the 16x cap needs, where one connection could never carry the load. A machine with no
structural record (a single-block machine, or a plan adapted without the dataset) keeps exactly
one connection, the pre-v3 behaviour.

A machine that needs one hatch keeps the single-port id (``power:in``); one that needs several
suffixes them (``power:in#1``, ``power:in#2``, ...). Likewise a tier that fits on one run keeps
the single-source ids (``power-source:MV`` / ``power:MV``) and one that has to split suffixes them
(``power-source:MV#1``, ``power:MV#2``, ...), so the common case reads unchanged in the build guide
and previewer.

**How each source is itself powered is left to the builder** - the build guide says so; the layout
marks where an external source must feed in. Source *position* optimization (packing groups by
proximity rather than by load alone) is still Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass

from gtnh_solver.dataset import (
    ENERGY_HATCH_AMPS,
    MAX_CABLE_THICKNESS,
    UnknownTierError,
    UnpowerableError,
    amp_load,
    energy_hatches_for,
    tiers_above,
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


def _port_id(index: int = 0, total: int = 1) -> str:
    return POWER_IN if total == 1 else f"{POWER_IN}#{index + 1}"


#: TEMPORARY (see :func:`_supply_tier`): the most energy hatches a machine is given before its
#: voltage tier is raised instead. Three is a build most players would actually put down; past it
#: the hatch count is a symptom of a wrong tier rather than a real design.
MAX_HATCHES_BEFORE_UPGRADE = 3


def _supply_tier(machine: Machine) -> str:
    """The tier to power ``machine`` at: its own, raised until it needs few enough hatches.

    **TEMPORARY WORKAROUND.** The upstream gtnh-factory-flow export computes some machines' EU/t
    against a wrong recipe model (gtnh-factory-flow #44 and #45 for this one: the Industrial Coke
    Oven has no heating coils, and its parallel caps are 18/30 rather than 16/32), so a node can
    arrive drawing far more than its stated tier can plausibly deliver. Rather than emit a machine
    ringed with a dozen energy hatches, raise the tier until the draw fits
    :data:`MAX_HATCHES_BEFORE_UPGRADE` of them - the same thing a player would do.

    **What this is not.** It does NOT re-derive the recipe: a real tier change also re-overclocks,
    moving both ``eut`` and the parallel count, which only the exporter can do. It only changes the
    voltage the layout supplies, leaving ``eut`` as the export stated it. So the layout is
    buildable and honestly sized, but the underlying EU/t figure is still upstream's. **Remove this
    once those upstream fixes land** and the export's tiers are trustworthy.

    Applies only to a machine with a structural record (``hatch_cells``): without one there is no
    evidence the machine even has hatches - a single-block machine is its tier, and re-tiering it
    would be inventing a change the plan never asked for. Returns the machine's own tier unchanged
    in that case too, and when it needs no upgrade, when its tier is off the ladder, or when no
    higher tier helps.
    """
    if machine.hatch_cells is None:
        return machine.voltage_tier
    try:
        needed = energy_hatches_for(machine.eut, machine.voltage_tier)
    except (UnknownTierError, UnpowerableError):
        return machine.voltage_tier
    if needed <= MAX_HATCHES_BEFORE_UPGRADE:
        return machine.voltage_tier
    for candidate in tiers_above(machine.voltage_tier):
        try:
            if energy_hatches_for(machine.eut, candidate) <= MAX_HATCHES_BEFORE_UPGRADE:
                return candidate
        except UnpowerableError:  # pragma: no cover - a higher tier survives what a lower one did
            continue
    return machine.voltage_tier


@dataclass(frozen=True)
class _Feed:
    """One energy hatch: the machine port a cable run has to reach, and what it pulls.

    The unit the partitioner works in. Before v3 that unit was the machine, which quietly assumed
    a machine draws through exactly one connection - true only while its whole load fits one
    hatch.
    """

    machine_id: str
    port_id: str
    eut: float  # EU/t through this one hatch
    load: float  # nominal amps at the at-source voltage


def _power_ports(machine: Machine) -> list[Port]:
    """The power INPUT ports (energy hatches) ``machine`` needs, sharing its draw evenly.

    One port unless the machine has a structural record (``hatch_cells``) AND its draw exceeds
    what one hatch takes: without that record - a single-block machine, or any plan adapted with
    no physical dataset - there is no evidence the machine *has* hatches, so splitting its intake
    would be inventing structure the layout cannot justify.

    The split is even because the hatches are identical and GT drains them together; nothing here
    can (or needs to) predict a skew. ``max_amps`` records each hatch's own 2 A ceiling, which the
    validator uses at the delivered voltage - not to reject a cable offering more than a hatch
    takes (it simply takes its 2), but to check the machine's hatches together can still take in
    its whole draw once loss has shrunk every packet.

    Allocation is deliberately **not** capped by the machine's free cells: a machine needing more
    hatches than its casing can host is a real infeasibility, and the validator reports it against
    ``hatch_cells``. Silently allocating fewer would under-size the feed and certify a layout that
    cannot draw its own load.
    """
    if machine.hatch_cells is None:
        return [Port(id=POWER_IN, commodity=Commodity.POWER, direction=IODirection.INPUT)]
    try:
        count = energy_hatches_for(machine.eut, machine.voltage_tier)
    except UnknownTierError:
        count = 1  # off-ladder tier: unverifiable here, reported downstream (see _partition)
    share = machine.eut / count
    return [
        Port(
            id=_port_id(i, count),
            commodity=Commodity.POWER,
            direction=IODirection.INPUT,
            rate=share,
            max_amps=float(ENERGY_HATCH_AMPS),
        )
        for i in range(count)
    ]


def _feeds_for(tier: str, machines_at_tier: list[tuple[Machine, list[Port]]]) -> list[_Feed]:
    """Every energy hatch at ``tier`` as a :class:`_Feed`, in machine-then-port order.

    A feed's ``load`` is its nominal amps at the at-source voltage. An off-ladder tier has no
    verifiable load; it comes back as 0 so the partitioner leaves the tier on one net and the
    router (which reports the unknown tier) is the one to say so.
    """
    feeds: list[_Feed] = []
    for machine, ports in machines_at_tier:
        for port in ports:
            eut = machine.eut if port.rate is None else port.rate
            try:
                load = amp_load(eut, tier)
            except UnknownTierError:
                load = 0.0
            feeds.append(_Feed(machine_id=machine.id, port_id=port.id, eut=eut, load=load))
    return feeds


def _partition_by_amperage(feeds: list[_Feed]) -> list[list[_Feed]]:
    """Split one tier's feeds into groups a single cable run can carry.

    First-fit-decreasing on each feed's *nominal* amp load (at-source voltage, no loss). Distances
    are unknown until placement, so this reserves no loss headroom: a group that cable loss later
    pushes over the cap is reported by the router, not silently accepted.

    Because the unit is a hatch and not a machine, a heavy machine no longer forces an over-cap
    net: its hatches spread across as many runs as the cap needs, and several hatches of the same
    machine may share a run (one cable, several wired faces - which is how you would build it).
    A feed still over the cap on its own would land in a group alone, but cannot occur while a
    hatch's own ceiling (2 A) is far below it.

    Feeds keep their input order within a group; groups are ordered by the heaviest feed placed
    first, so the output is deterministic.
    """
    order = {(f.machine_id, f.port_id): i for i, f in enumerate(feeds)}
    groups: list[list[_Feed]] = []
    carried: list[float] = []
    for feed in sorted(feeds, key=lambda f: (-f.load, f.machine_id, f.port_id)):
        for i, so_far in enumerate(carried):
            if whole_amps(so_far + feed.load) <= MAX_CABLE_THICKNESS:
                groups[i].append(feed)
                carried[i] = so_far + feed.load
                break
        else:
            groups.append([feed])
            carried.append(feed.load)
    return [sorted(group, key=lambda f: order[(f.machine_id, f.port_id)]) for group in groups]


def synthesize_power(machines: list[Machine], nets: list[Net]) -> tuple[list[Machine], list[Net]]:
    """Return ``(machines, nets)`` augmented with synthetic power sources + shared-amperage nets.

    Machines with ``eut > 0`` gain the power INPUT ports their draw needs (:func:`_power_ports`);
    each voltage tier in use is split into groups a single cable run can carry
    (:func:`_partition_by_amperage`), and each group gains a source machine and a power net. Tiers
    are processed in sorted order so the output is deterministic. Raises
    :class:`~gtnh_solver.adapter.core.AdapterError` only via id collision.
    """
    # Re-tier first (a TEMPORARY workaround, see _supply_tier), because both the hatch count and
    # the net a machine lands on follow from the tier it is actually supplied at.
    powered = {
        m.id: m.model_copy(update={"voltage_tier": _supply_tier(m)}) for m in machines if m.eut > 0
    }
    if not powered:
        return machines, nets  # nothing draws power (e.g. only storages, or zero-eut recipes)

    ports_by_machine = {mid: _power_ports(m) for mid, m in powered.items()}

    by_tier: dict[str, list[tuple[Machine, list[Port]]]] = {}
    for m in machines:
        supplied = powered.get(m.id)
        if supplied is not None:
            by_tier.setdefault(supplied.voltage_tier, []).append((supplied, ports_by_machine[m.id]))

    existing_ids = {m.id for m in machines}

    # Append the power INPUT ports to every powered machine (its other ports are untouched).
    out_machines = [
        _with_power_inputs(powered[m.id], ports_by_machine[m.id]) if m.id in powered else m
        for m in machines
    ]
    out_nets = list(nets)

    for tier in sorted(by_tier):
        groups = _partition_by_amperage(_feeds_for(tier, by_tier[tier]))
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
                    throughput=sum(f.eut for f in group),  # total EU/t on this group's trunk
                    endpoints=[
                        MachineFaceRef(machine_id=source_id, port_id=POWER_OUT),
                        *(
                            MachineFaceRef(machine_id=f.machine_id, port_id=f.port_id)
                            for f in group
                        ),
                    ],
                )
            )
    return out_machines, out_nets


def _with_power_inputs(machine: Machine, ports: list[Port]) -> Machine:
    return machine.model_copy(update={"faces": FaceSpec(ports=[*machine.faces.ports, *ports])})


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
