"""Input IR - the *problem* the solver consumes.

Produced by the adapter from a gtnh-factory-flow exported plan JSON (recipes embedded)
plus the physical-rules dataset. Spec: docs/IR.md. This is one of two versioned
contracts everything couples to, so it is kept minimal and grown with explicit version
bumps (see ``INPUT_IR_VERSION`` and the changelog in ``__init__.py``).

What this contract guarantees (checked here) vs. what it does NOT:
- Guaranteed: structural well-formedness + *referential integrity* - unique ids, every
  net/pinned reference resolves to an existing machine+port, a net's commodity matches
  the ports it touches. Downstream code may assume these hold.
- NOT checked here: geometric/rule validity (cells in-bounds, no machine overlaps,
  throughput within tier caps, required-face reachability). That is the validator's job,
  on purpose - it has independent logic so it can catch solver bugs (docs/TESTING.md).
"""

from __future__ import annotations

import math

from pydantic import Field, model_validator

from ._base import FrozenModel, StrictModel
from .enums import HORIZONTAL_FACINGS, Commodity, Facing, IODirection
from .geometry import CellBox, CellCoord

#: Bump on any breaking change to the input contract; record it in ``ir/__init__.py``.
INPUT_IR_VERSION = 3


class Port(StrictModel):
    """One required I/O point the solver must expose on a usable (non-front) machine face.

    The *physical* face is chosen by the solver (placement + orientation); this only states
    the requirement. Whether a port is satisfied by auto-output is a **solver decision**, not a
    problem input - it is recorded in the output's ``AutoConnection`` (and the validator enforces
    one auto-output per machine there), so it is deliberately not a field here.
    """

    id: str = Field(min_length=1)
    commodity: Commodity
    direction: IODirection
    #: Reserved: a per-port cover override (conveyor for items, pump/regulator for fluids). Not
    #: yet produced or consumed - covers are currently derived from commodity at render time
    #: (buildguide ``_COVER``); this stays ``None`` until a dataset sets the specific cover a port
    #: needs (e.g. a regulator vs a plain pump).
    cover: str | None = None
    #: Throughput through this port - items/t, mB/t, or (since IR v3) **EU/t for a power port**.
    #: ``None`` when unknown. The adapter fills it from the recipe; it surfaces boundary I/O rates
    #: (``system_io``, previewer). For power it is the share of the machine's ``eut`` that arrives
    #: through *this* connection: a multiblock spreads its draw over several energy hatches, so the
    #: router and validator size a cable from the port's rate, never the whole machine's.
    rate: float | None = Field(default=None, ge=0.0)
    #: Most amps this single connection can accept, or ``None`` for no per-connection ceiling.
    #: A GT energy hatch takes 2 A (``dataset.ENERGY_HATCH_AMPS``). It is what a connection can
    #: take **in**, so with the delivered voltage it says how much power actually reaches the
    #: machine; a cable offering more is not an error (the hatch just takes its 2), but hatches
    #: that together take in less than the machine's ``eut`` mean it cannot run its recipe.
    max_amps: float | None = Field(default=None, gt=0.0)


class FaceSpec(StrictModel):
    """The catalog of I/O ports a machine needs across its five usable faces.

    Not a fixed face->port map: face assignment is a solver decision. The front face
    (set by orientation) carries no I/O and is never listed here.
    """

    ports: list[Port] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> FaceSpec:
        ids = [p.id for p in self.ports]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate port id within a machine's FaceSpec")
        return self


class HatchSlot(FrozenModel):
    """One casing cell of a multiblock that can host a hatch or bus, and the kinds it accepts.

    ``offset`` is measured from the machine's **unrotated** minimum corner, the same corner
    ``Placement.cell`` names, so a placed slot's world cell is
    ``placement.cell + ir.geometry.rotated_slot(offset, footprint, placement.orientation)``. Kept
    unrotated here because orientation is a placement decision the IR must not pre-empt.

    ``kinds`` holds ``gregtech.api.enums.HatchElement`` names (``OutputHatch``, ``InputBus``,
    ``Energy``, ``Maintenance``, ``Muffler``, ...), sorted so a layout is reproducible. **It is a
    lower bound, never a whitelist**: a GT hatch adder built from a bare method reference exposes no
    filter, so its cell is recorded without that kind rather than wrongly. 23 of 208 dumped
    controllers record no slots at all, 61 of the remaining 185 record no ``Energy``-capable cell,
    and 35 no ``Maintenance``-capable one. A consumer that treats an absent kind as a prohibition
    manufactures a false infeasibility across roughly a third of the dataset; treat "unrecorded" as
    permissive (``validator/core`` already refuses to enforce per-kind counts for this reason).
    """

    offset: CellCoord
    kinds: tuple[str, ...] = Field(min_length=1)


class Machine(StrictModel):
    """A single machine to place at one position.

    Multi-instance machine groups (the gtnh-factory-flow balance can call for N identical
    copies of a recipe) are **not modelled yet**: a net endpoint (``MachineFaceRef``) cannot
    address one instance of a group, so the placer/router/validator could only drop the copies
    and leave the extras silently unwired. Until instance-aware routing exists (Phase 2,
    docs/ROADMAP.md) each ``Machine`` is exactly one instance, and the adapter rejects an export
    ``machineCount > 1`` rather than emit an under-wired layout. (``count`` was dropped in
    InputIR v1; see ``ir/__init__.py``.)
    """

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)  # GT machine id; keys into the physical-rules dataset
    #: The GT controller block this machine is, as ``"<registry_name>@<meta>"``
    #: ("gregtech:gt.blockmachines@998"). ``type`` is the exporter's localized recipe-map name,
    #: which for GT++ machines differs from the controller block's name that the structure dataset
    #: is keyed by - this is the exact join key when the export supplies it, and the dataset falls
    #: back to matching on ``type`` when it is None (a pre-#25 plan).
    block_key: str | None = None
    footprint: CellBox = Field(default_factory=CellBox)
    faces: FaceSpec = Field(default_factory=FaceSpec)
    voltage_tier: str = Field(min_length=1)  # LV/MV/HV/... - sets cable voltage rating
    orientation_options: list[Facing] = Field(min_length=1)
    #: EU/t this machine draws; with ``voltage_tier`` it sets the amperage it pulls on a
    #: shared-amperage cable (dataset.amperage). 0 for an unpowered block or a power source.
    eut: float = Field(default=0.0, ge=0.0)
    #: How many cells of this machine's structure can hold a hatch/bus, or ``None`` when unknown
    #: (a single-block machine, or a plan adapted without the physical dataset). A multiblock's
    #: casing cells accept I/O of any kind - items, fluids, **or power** - so this is the ceiling
    #: on its total connections, energy hatches included. The validator uses it to reject a layout
    #: that wires more connections onto a machine than its structure has cells to host.
    hatch_cells: int | None = Field(default=None, ge=0)
    #: Where those cells actually are, when the structure dump recorded them. Empty for a
    #: single-block machine, for a plan adapted without the physical dataset, and for the 23 of 208
    #: controllers whose adders expose no filter - all of which read as "unknown", not "none", so a
    #: consumer falls back to treating any body face as dockable rather than refusing to place.
    #: ``len(hatch_slots)`` agrees with :attr:`hatch_cells` whenever both are present; the count
    #: exists separately because it survived a dump that recorded no offsets.
    hatch_slots: tuple[HatchSlot, ...] = ()

    @property
    def is_power_source(self) -> bool:
        """Whether this machine *supplies* power (it has a power OUTPUT port).

        Today only the adapter's synthesized per-tier source matches (a plan export has no power
        nodes). Such a machine is fed externally by the builder: its front face is the reserved
        feed face and placement pins that face on the region boundary (validator-enforced), so
        power enters from outside the structure. When real in-plan generators arrive with the
        dataset lane, this structural predicate needs a dataset-driven refinement.
        """
        return any(
            p.commodity is Commodity.POWER and p.direction is IODirection.OUTPUT
            for p in self.faces.ports
        )

    @property
    def power_input_ports(self) -> list[Port]:
        """This machine's power INPUT ports - its energy hatches, in declaration order."""
        return [
            p
            for p in self.faces.ports
            if p.commodity is Commodity.POWER and p.direction is IODirection.INPUT
        ]

    def port_eut(self, port_id: str) -> float:
        """EU/t arriving through ``port_id``: its own ``rate``, else the machine's whole ``eut``.

        The fallback keeps a single-port machine (and every pre-v3 problem, where power ports
        carried no rate) sizing exactly as it did, while a machine whose draw is spread over
        several energy hatches charges each cable only its own hatch's share.
        """
        for port in self.faces.ports:
            if port.id == port_id:
                return self.eut if port.rate is None else port.rate
        return 0.0

    @model_validator(mode="after")
    def _check(self) -> Machine:
        if len(self.orientation_options) != len(set(self.orientation_options)):
            raise ValueError("duplicate orientation in orientation_options")
        non_horizontal = [f for f in self.orientation_options if f not in HORIZONTAL_FACINGS]
        if non_horizontal:
            raise ValueError(
                "machine front must face a horizontal direction (N/S/E/W); "
                f"got {[f.value for f in non_horizontal]}"
            )
        rated = [p.rate for p in self.power_input_ports if p.rate is not None]
        if rated:
            # Split draws must account for the whole machine: a hatch left off the books would be
            # a cable nothing sizes, and an extra one would double-charge the net. Rates only ever
            # come from a division of ``eut``, so exact-ish equality is the right check.
            if len(rated) != len(self.power_input_ports):
                raise ValueError(
                    "power input ports must all carry a rate or none of them (a partially rated "
                    "machine would leave part of its draw unsized)"
                )
            total = math.fsum(rated)
            if not math.isclose(total, self.eut, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(
                    f"power input port rates sum to {total} EU/t but the machine draws {self.eut}"
                )
        return self


class MachineFaceRef(FrozenModel):
    """A net endpoint: a port on a machine. Frozen/hashable so endpoints dedupe cleanly.
    The solver resolves ``port_id`` to a concrete physical face during placement."""

    machine_id: str = Field(min_length=1)
    port_id: str = Field(min_length=1)


class Net(StrictModel):
    """One logical connection to route: a commodity from/to a set of machine ports.

    ``throughput`` is **typed** - mB/t (fluid), items/t (item), or EU/t (power). Its consumer is
    ``system_io``, which reads it to report boundary feed/product rates on the previewer and build
    guide; the Phase 1 router needs only connectivity, not the rate (per-net tier-cap checks are a
    Phase 2 upgrade). Power is a shared-amperage net, so its physical thickness is computed
    downstream, not stored here.
    """

    id: str = Field(min_length=1)
    commodity: Commodity
    fluid_or_item: str | None = None  # which fluid/item; None for power
    throughput: float = Field(ge=0.0)
    endpoints: list[MachineFaceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Net:
        if self.commodity is Commodity.POWER:
            if self.fluid_or_item is not None:
                raise ValueError("power nets must not name a fluid_or_item")
        elif not self.fluid_or_item:
            raise ValueError(f"{self.commodity.value} net must name a fluid_or_item")
        return self


class METoggles(StrictModel):
    """Per-commodity ME (AE2) routing toggles. A toggled commodity is removed from
    physical routing; the solver places the ME endpoint instead. Default: route all
    three physically (docs/DOMAIN.md)."""

    items: bool = False
    fluids: bool = False
    power: bool = False

    def toggled(self, commodity: Commodity) -> bool:
        """Whether ``commodity`` is routed via ME (and so removed from physical routing)."""
        return {
            Commodity.ITEM: self.items,
            Commodity.FLUID: self.fluids,
            Commodity.POWER: self.power,
        }[commodity]


class PinnedIO(StrictModel):
    """A fixed external input/output point (e.g. a feed/drain chest) at a cell, tied to
    a net. Honoring it is a hard geometric constraint, checked by the validator."""

    net_id: str = Field(min_length=1)
    cell: CellCoord
    kind: IODirection


class InputIR(StrictModel):
    """The whole problem: machines, nets, fixed/blocked cells, ME toggles, and the
    bounding region the layout must fit. Referential integrity is enforced on build."""

    version: int = INPUT_IR_VERSION
    bounding_region: CellBox
    machines: list[Machine] = Field(default_factory=list)
    nets: list[Net] = Field(default_factory=list)
    pinned: list[PinnedIO] = Field(default_factory=list)
    reserved_cells: list[CellCoord] = Field(default_factory=list)
    me_toggles: METoggles = Field(default_factory=METoggles)

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> InputIR:
        machine_ids = [m.id for m in self.machines]
        if len(machine_ids) != len(set(machine_ids)):
            raise ValueError("duplicate machine id")
        net_ids = [n.id for n in self.nets]
        if len(net_ids) != len(set(net_ids)):
            raise ValueError("duplicate net id")

        # port_id -> commodity, per machine, for endpoint resolution + commodity match.
        ports_by_machine = {m.id: {p.id: p.commodity for p in m.faces.ports} for m in self.machines}
        for net in self.nets:
            for ep in net.endpoints:
                machine_ports = ports_by_machine.get(ep.machine_id)
                if machine_ports is None:
                    raise ValueError(f"net {net.id!r} references unknown machine {ep.machine_id!r}")
                if ep.port_id not in machine_ports:
                    raise ValueError(
                        f"net {net.id!r} references unknown port {ep.port_id!r} "
                        f"on machine {ep.machine_id!r}"
                    )
                if machine_ports[ep.port_id] is not net.commodity:
                    raise ValueError(
                        f"net {net.id!r} ({net.commodity.value}) connects to port "
                        f"{ep.port_id!r} of a different commodity"
                    )

        net_id_set = set(net_ids)
        for pin in self.pinned:
            if pin.net_id not in net_id_set:
                raise ValueError(f"pinned I/O references unknown net {pin.net_id!r}")
        return self
