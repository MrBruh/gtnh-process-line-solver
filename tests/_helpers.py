"""Shared test factories, previously copy-pasted with slight drift across the per-module suites.

These are plain factory functions - they take arguments - so they live in an importable helper
module rather than as pytest fixtures in ``conftest.py`` (fixtures are injected, not called with
positional/keyword args). Each per-module test file imports the ones it needs.

Reconciled drift (chose the form that keeps every caller green):

- ``at`` carries an ``orientation`` keyword (test_router_auto's form); the router/power callers
  that only ever hand-place NORTH-facing machines take the default.
- ``producer`` / ``consumer`` / ``net`` carry a ``commodity`` keyword (test_router_auto's variadic
  form); the single-sink ITEM callers (test_solver) use the defaults. ``net`` is variadic in its
  sinks, so a 1->1 net is just the no-extra-sink case. ``type_`` / ``fluid`` keep the few callers
  that pin a specific machine type or resource name (buildguide, previewer) exact.
- ``PLACEMENT_CODES`` is the full seven-code set; test_adapter previously used a six-code subset
  (it omitted ``POWER_FEED_NOT_ON_BOUNDARY``), so asserting that code absent there too is a
  correct strengthening, not a new failure - the sand placement never trips it.
- ``power_source`` carries the ``Power Source (LV)`` boilerplate. Its port id and orientation set
  legitimately differ by caller (power-routing nets key off ``power:out``; the placement suite
  seats the feed face from any of the four horizontals), so both stay parameters.
"""

from __future__ import annotations

from gtnh_solver.dataset import DatasetMeta, MachinePhysical, PhysicalDataset
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    IODirection,
    Machine,
    MachineFaceRef,
    Net,
    Placement,
    Port,
)
from gtnh_solver.validator.report import ViolationCode

#: The placement/geometry violation codes a clean placement must be free of. The full set; a
#: caller asserting a routed/valid placement is disjoint from it certifies the placer's geometry.
PLACEMENT_CODES: frozenset[ViolationCode] = frozenset(
    {
        ViolationCode.MACHINE_OVERLAP,
        ViolationCode.MACHINE_OUT_OF_BOUNDS,
        ViolationCode.MACHINE_ON_RESERVED,
        ViolationCode.BAD_ORIENTATION,
        ViolationCode.PLACEMENT_COUNT_MISMATCH,
        ViolationCode.UNKNOWN_MACHINE,
        ViolationCode.POWER_FEED_NOT_ON_BOUNDARY,
    }
)


def machine(
    mid: str, ports: list[Port], *, orientation: Facing = Facing.NORTH, type_: str = "t"
) -> Machine:
    """An LV machine with the given I/O ports, front facing ``orientation``."""
    return Machine(
        id=mid,
        type=type_,
        voltage_tier="LV",
        orientation_options=[orientation],
        faces=FaceSpec(ports=ports),
    )


def producer(mid: str, *, commodity: Commodity = Commodity.ITEM, type_: str = "t") -> Machine:
    """A machine with a single OUTPUT port ``out`` of ``commodity``."""
    return machine(
        mid, [Port(id="out", commodity=commodity, direction=IODirection.OUTPUT)], type_=type_
    )


def consumer(mid: str, *, commodity: Commodity = Commodity.ITEM, type_: str = "t") -> Machine:
    """A machine with a single INPUT port ``in`` of ``commodity``."""
    return machine(
        mid, [Port(id="in", commodity=commodity, direction=IODirection.INPUT)], type_=type_
    )


def net(
    nid: str,
    src: str,
    *dsts: str,
    commodity: Commodity = Commodity.ITEM,
    fluid: str = "x",
) -> Net:
    """A net from ``src``'s ``out`` port to each of ``dsts``' ``in`` ports."""
    return Net(
        id=nid,
        commodity=commodity,
        fluid_or_item=None if commodity is Commodity.POWER else fluid,
        throughput=1.0,
        endpoints=[
            MachineFaceRef(machine_id=src, port_id="out"),
            *(MachineFaceRef(machine_id=dst, port_id="in") for dst in dsts),
        ],
    )


def at(mid: str, x: int, y: int, z: int, *, orientation: Facing = Facing.NORTH) -> Placement:
    """Place ``mid`` at ``(x, y, z)`` with front facing ``orientation``."""
    return Placement(machine_id=mid, cell=CellCoord(x=x, y=y, z=z), orientation=orientation)


def power_source(
    mid: str = "src", *, orientations: list[Facing] | None = None, port_id: str = "power:out"
) -> Machine:
    """A synthesized LV power source: one power OUTPUT port (its front is the external-feed face)."""
    return Machine(
        id=mid,
        type="Power Source (LV)",
        voltage_tier="LV",
        orientation_options=orientations if orientations is not None else [Facing.NORTH],
        faces=FaceSpec(
            ports=[Port(id=port_id, commodity=Commodity.POWER, direction=IODirection.OUTPUT)]
        ),
    )


def hatched_dataset(
    hatch_cells: int = 20, key: str = "M", *, census: bool = True
) -> PhysicalDataset:
    """A dataset whose one machine is a multiblock with ``hatch_cells`` interchangeable cells.

    A GT casing cell accepts a hatch of any kind, so ``energy_hatch_cells`` matches; one
    maintenance hatch is reserved. A record is what tells the power synthesis the machine HAS
    hatches; with no record it keeps a single connection.

    ``census`` says whether the dump enumerates every multiblock controller in the pack, which is
    what decides the meaning of a MISS: in a census the machine is then known to be a single block
    and GT's ``maxAmperesIn`` ceiling applies to it, while in a sample (the committed fixtures) a
    miss is no evidence at all and the adapter must state no ceiling. Pass ``key`` to make the
    lookup miss on purpose.
    """
    return PhysicalDataset(
        meta=DatasetMeta.model_validate(
            {
                "schema": 2,
                "pack_version": "test",
                "generated_at": "2026-01-01T00:00:00Z",
                "extractor_sha": "0" * 40,
                "controller_count": 1,
                "census": census,
            }
        ),
        machines={
            key: MachinePhysical(
                key=key,
                registry_name="test:block",
                meta=0,
                source_class="test.Controller",
                footprint=CellBox(sx=3, sy=3, sz=3),
                io_faces=frozenset({Facing.NORTH}),
                hint_layers=frozenset({0}),
                coil_layer_count=0,
                variant_count=1,
                hatch_cells=hatch_cells,
                energy_hatch_cells=hatch_cells,
                upkeep_hatch_count=1,
            )
        },
    )
