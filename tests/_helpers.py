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
  that pin a specific machine type or resource name (previewer) exact.
- ``PLACEMENT_CODES`` is the full placement-code set; test_adapter previously used a six-code
  subset (it omitted ``POWER_FEED_NOT_ON_BOUNDARY``), so asserting that code absent there too is a
  correct strengthening, not a new failure - the sand placement never trips it. The outside-front
  code (#282) joined it with the rule it generalizes.
- ``power_source`` carries the ``Power Source (LV)`` boilerplate. Its port id and orientation set
  legitimately differ by caller (power-routing nets key off ``power:out``; the placement suite
  seats the feed face from any of the four horizontals), so both stay parameters.
"""

from __future__ import annotations

import json
import os
import struct
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gtnh_solver.dataset import DatasetMeta, MachinePhysical, PhysicalDataset
from gtnh_solver.dataset.schema import SCHEMA_VERSION
from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    FaceSpec,
    Facing,
    HatchSlot,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    METoggles,
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
        ViolationCode.OUTSIDE_FRONT_NOT_ON_BOUNDARY,
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


def hub_line(
    connections: int,
    *,
    footprint: CellBox | None = None,
    me_toggles: METoggles | None = None,
    region: CellBox | None = None,
) -> InputIR:
    """A ``hub`` whose ``connections`` fluid outputs each feed a consumer ``c<i>`` of their own.

    One net per output, so the hub carries ``connections`` connections and every consumer one. A
    single block has five faces to carry them (the front carries none), which is what the line is
    for: one output more than that, and no placement lays it.
    """
    hub = machine(
        "hub",
        [
            Port(id=f"out{i}", commodity=Commodity.FLUID, direction=IODirection.OUTPUT)
            for i in range(connections)
        ],
    )
    if footprint is not None:
        hub = hub.model_copy(update={"footprint": footprint})
    return InputIR(
        bounding_region=region if region is not None else CellBox(sx=12, sy=4, sz=12),
        machines=[hub, *(consumer(f"c{i}", commodity=Commodity.FLUID) for i in range(connections))],
        nets=[
            Net(
                id=f"n{i}",
                commodity=Commodity.FLUID,
                fluid_or_item=f"f{i}",
                throughput=1.0,
                endpoints=[
                    MachineFaceRef(machine_id="hub", port_id=f"out{i}"),
                    MachineFaceRef(machine_id=f"c{i}", port_id="in"),
                ],
            )
            for i in range(connections)
        ],
        me_toggles=me_toggles if me_toggles is not None else METoggles(),
    )


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


#: The cells of a 3x3 tower layer around its hollow core, as ``(x, z)`` from the minimum corner.
_RING = tuple((x, z) for x in range(3) for z in range(3) if (x, z) != (1, 1))


def layered_tower(
    mid: str = "tower",
    outputs: Sequence[str] = ("a",),
    *,
    layers: int = 2,
    inputs: Sequence[str] = (),
    port_layers: Sequence[int | None] | None = None,
) -> Machine:
    """A Distillation Tower in miniature, ``layers + 1`` tall, whose slots record output layers.

    Built the way the dump records GT's (#299): the base takes input hatches, energy and
    maintenance on every cell but the controller's (the front centre); ring ``i`` above it is
    output layer ``i - 1`` and takes output hatches only; the top centre takes an output hatch that
    feeds no layer. Fluid output ``k`` of ``outputs`` is filled from layer ``k`` unless
    ``port_layers`` says otherwise, which is how a test builds a machine the IR must refuse.
    """
    slots = [
        HatchSlot(offset=CellCoord(x=x, y=0, z=z), kinds=("Energy", "InputHatch", "Maintenance"))
        for x in range(3)
        for z in range(3)
        if (x, z) != (1, 0)
    ]
    slots += [
        HatchSlot(
            offset=CellCoord(x=x, y=layer + 1, z=z), kinds=("OutputHatch",), output_layer=layer
        )
        for layer in range(layers)
        for x, z in _RING
    ]
    slots.append(HatchSlot(offset=CellCoord(x=1, y=layers, z=1), kinds=("OutputHatch",)))
    named = list(port_layers) if port_layers is not None else list(range(len(outputs)))
    ports = [
        Port(id=f"input:{name}", commodity=Commodity.FLUID, direction=IODirection.INPUT)
        for name in inputs
    ]
    ports += [
        Port(
            id=f"output:{name}",
            commodity=Commodity.FLUID,
            direction=IODirection.OUTPUT,
            output_layer=layer,
        )
        for name, layer in zip(outputs, named, strict=True)
    ]
    return Machine(
        id=mid,
        type="Distillation Tower",
        voltage_tier="LV",
        orientation_options=[Facing.NORTH],
        footprint=CellBox(sx=3, sy=layers + 1, sz=3),
        faces=FaceSpec(ports=ports),
        hatch_slots=tuple(slots),
        hatch_cells=len(slots),
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
    record = MachinePhysical(
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
    return PhysicalDataset(
        meta=DatasetMeta.model_validate(
            {
                "schema": SCHEMA_VERSION,
                "pack_version": "test",
                "generated_at": "2026-01-01T00:00:00Z",
                "extractor_sha": "0" * 40,
                "controller_count": 1,
                "census": census,
            }
        ),
        machines={key: record},
        # by_block_key derives from records (#172); a dump that sets only the name index has no
        # block identities, so an exact-id lookup against it silently falls back to a name.
        records=(record,),
    )


_PROPERTY_FRACTION = 0.25
"""Share of a property test's example budget a *local* run takes. See :func:`property_examples`."""

_PROPERTY_FLOOR = 10
"""No local budget drops below this: a handful of examples proves nothing at all."""


def property_examples(full: int) -> int:
    """The ``max_examples`` a hypothesis property test should run here.

    ``full`` in CI, a fraction of it locally. The budgets in ``test_solver_properties`` are ~18s of
    a 75s suite, which is a long wait for the fast feedback a local run is for, but shrinking them
    everywhere would permanently narrow the space the never-silently-invalid invariant is proven
    over - and docs/TESTING.md is explicit that a collapsed space leaves the suite green while
    proving less. Splitting it keeps every PR held to the full budget and makes iteration cheap.

    Scaled rather than set per test, so the ratio between the three budgets (200/50/300 - they are
    not interchangeable, the biggest one fuzzes ``validate``) survives the reduction.

    ``GTNH_TEST_HYPOTHESIS_FRACTION`` overrides the share; ``1.0`` runs the full budget locally,
    which is worth doing before pushing a change to the solver or the validator. An unparseable or
    out-of-range value falls back to the default rather than silently running a token few.
    """
    if os.environ.get("CI"):
        return full
    raw = os.environ.get("GTNH_TEST_HYPOTHESIS_FRACTION")
    fraction = _PROPERTY_FRACTION
    if raw is not None:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = -1.0
        if 0.0 < parsed <= 1.0:
            fraction = parsed
    return max(_PROPERTY_FLOOR, round(full * fraction))


def solid_png(rgb: tuple[int, int, int], size: int = 4) -> bytes:
    """A ``size`` x ``size`` PNG of one colour, written by hand so no test needs Pillow."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def write_icon_index(
    folder: Path,
    *,
    items: dict[str, Any] | None = None,
    fluids: dict[str, Any] | None = None,
    images: dict[str, bytes] | None = None,
) -> Path:
    """An icon index (``dataset.icons``) under ``folder``: ``index.json`` with these entries and
    ``images.zip`` holding ``images``, member path -> bytes. Returns the index's path."""
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "index.json"
    doc = {
        "generated_at": "2026-10-02T00:00:00Z",
        "schema": 1,
        "source": {"exporter": "test", "pack_version": "test", "icon_px": 4},
        "items": items or {},
        "fluids": fluids or {},
    }
    index.write_text(json.dumps(doc), encoding="utf-8")
    with zipfile.ZipFile(folder / "images.zip", "w") as archive:
        for name, data in (images or {}).items():
            archive.writestr(name, data)
    return index
