"""Input IR — the *problem* the solver consumes.

Produced by the adapter from a gtnh-factory-flow exported plan JSON (recipes embedded)
plus the physical-rules dataset. Spec: docs/IR.md. This is one of two versioned
contracts everything couples to, so it is kept minimal and grown with explicit version
bumps (see ``INPUT_IR_VERSION`` and the changelog in ``__init__.py``).

What this contract guarantees (checked here) vs. what it does NOT:
- Guaranteed: structural well-formedness + *referential integrity* — unique ids, every
  net/pinned reference resolves to an existing machine+port, a net's commodity matches
  the ports it touches. Downstream code may assume these hold.
- NOT checked here: geometric/rule validity (cells in-bounds, no machine overlaps,
  throughput within tier caps, required-face reachability). That is the validator's job,
  on purpose — it has independent logic so it can catch solver bugs (docs/TESTING.md).
"""

from __future__ import annotations

from pydantic import Field, model_validator

from ._base import FrozenModel, StrictModel
from .enums import Commodity, IODirection
from .enums import Facing
from .geometry import CellBox, CellCoord

#: Bump on any breaking change to the input contract; record it in ``ir/__init__.py``.
INPUT_IR_VERSION = 0


class Port(StrictModel):
    """One required I/O point the solver must expose on a usable (non-front) machine face.

    The *physical* face is chosen by the solver (placement + orientation); this only
    states the requirement. A machine auto-outputs to a single face carrying items OR
    fluids (not both), modelled with ``is_auto_output`` (see docs/DOMAIN.md).
    """

    id: str = Field(min_length=1)
    commodity: Commodity
    direction: IODirection
    is_auto_output: bool = False
    #: Cover required to drive this port (conveyor for items, pump/regulator for fluids),
    #: ``None`` if the bare face suffices. Recorded for the build guide / export.
    cover: str | None = None


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
        auto = [p for p in self.ports if p.is_auto_output]
        if len(auto) > 1:
            raise ValueError("a machine has at most one auto-output face")
        for p in auto:
            if p.commodity is Commodity.POWER:
                raise ValueError("auto-output carries items or fluids, not power")
            if p.direction is not IODirection.OUTPUT:
                raise ValueError("an auto-output port must have direction=output")
        return self


class Machine(StrictModel):
    """A machine instance group to place. ``count`` identical copies (from the
    gtnh-factory-flow balance) share this spec; placement expands them."""

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)  # GT machine id; keys into the physical-rules dataset
    footprint: CellBox = Field(default_factory=CellBox)
    faces: FaceSpec = Field(default_factory=FaceSpec)
    voltage_tier: str = Field(min_length=1)  # LV/MV/HV/... — sets cable voltage rating
    orientation_options: list[Facing] = Field(min_length=1)
    count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _check(self) -> Machine:
        if len(self.orientation_options) != len(set(self.orientation_options)):
            raise ValueError("duplicate orientation in orientation_options")
        return self


class MachineFaceRef(FrozenModel):
    """A net endpoint: a port on a machine. Frozen/hashable so endpoints dedupe cleanly.
    The solver resolves ``port_id`` to a concrete physical face during placement."""

    machine_id: str = Field(min_length=1)
    port_id: str = Field(min_length=1)


class Net(StrictModel):
    """One logical connection to route: a commodity from/to a set of machine ports.

    ``throughput`` is **typed** — the router needs the real rate, not just connectivity:
    mB/t (fluid), items/t (item), or EU/t (power). Power is a shared-amperage net, so its
    physical thickness is computed downstream, not stored here.
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
        ports_by_machine = {
            m.id: {p.id: p.commodity for p in m.faces.ports} for m in self.machines
        }
        for net in self.nets:
            for ep in net.endpoints:
                machine_ports = ports_by_machine.get(ep.machine_id)
                if machine_ports is None:
                    raise ValueError(
                        f"net {net.id!r} references unknown machine {ep.machine_id!r}"
                    )
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
