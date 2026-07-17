"""Adapter: schema-v1 multiblock facts -> the solver's physical-rules dataset.

Design principle 3 of ``docs/dataset-extraction/plan.md``: the extractor emits raw facts
(blocks, hints, variants, substitutions); **all interpretation lives here**, in Python, where
the IR contracts and the tests are. Footprint bounding boxes, hint-derived face constraints, and
coil-tier semantics are computed from those facts and re-expressed as IR-shaped types
(``CellBox`` footprints, ``Facing`` faces). The moment this logic moved into the Java extractor
it would become a second, untested codebase (the plan's explicit non-goal), so it does not.

::

    data/multiblocks/*.json --load--> MultiblockDoc      (schema.py: raw extractor facts)
                                           |
                               to_physical | derive footprint + I/O faces + coil tiers
                                           v
                                    MachinePhysical       (IR-shaped physical rules for one machine)
                                           |
                          load_physical_dataset --> PhysicalDataset   (display_name -> record + meta)

The result is consumed by the gtnh-factory-flow adapter (``adapter/core.py``): given a plan whose
nodes name a machine by display name, it looks the physical record up and stamps the machine's real
footprint on the ``InputIR`` instead of the crude 1x1x1 default. The lookup is **opt-in**: passing
no dataset keeps the existing single-block behaviour, so the solver stays runnable with or without
a committed ``data/multiblocks/`` dump.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from gtnh_solver.ir import CellBox, Facing

from .schema import (
    DatasetMeta,
    MultiblockDoc,
    Variant,
    load_meta,
    load_multiblock_doc,
)

#: Default location of the committed dataset, relative to the source tree (this file is
#: ``src/gtnh_solver/dataset/multiblocks.py``, so the repo root is three parents up). Resolves in
#: an editable/dev install, which is how the repo is used; ``load_physical_dataset`` takes an
#: explicit directory for any other layout (and every test passes one).
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "multiblocks"

#: The substitutions channel that carries tiered heating coils (plan section 4.2 example). A coil
#: layer is a y-level whose blocks include one of this channel's alternatives.
_COIL_CHANNEL = "coil"


class DatasetError(ValueError):
    """A multiblock file was well-formed against the schema but not physically coherent.

    Raised when interpretation fails a sanity check the raw schema cannot express - notably a
    variant whose extractor-reported ``bbox`` disagrees with the box its own blocks span (a bad
    scan bound or facing bug in the extractor), or two files claiming the same machine key. This
    is the "unknown machine / bad footprint raises clearly" gate of docs/TESTING.md.
    """


@dataclass(frozen=True)
class MachinePhysical:
    """The physical-rules record for one machine, in IR-shaped terms.

    Everything here is *derived* from a :class:`~gtnh_solver.dataset.schema.MultiblockDoc`; nothing
    is taken on faith from the extractor. ``key`` is the display name the gtnh-factory-flow plan
    references a machine by (``recipe.machineType``), so a plan node resolves straight to its record.
    """

    key: str  # display_name; how a gtnh-factory-flow plan node names this machine
    registry_name: str
    meta: int
    source_class: str
    #: Cell-rounded bounding box of the primary variant (a single block => 1x1x1). The footprint
    #: the placer reserves and the router treats as an obstacle.
    footprint: CellBox
    #: The faces that can carry I/O, derived from where the hint dots sit on the bounding box. A
    #: hint on a bbox face means a hatch may face that way; the front (no-I/O) face is a placement
    #: choice the solver still excludes at route time, so this is the *physical* upper bound.
    io_faces: frozenset[Facing]
    #: Distinct y-layers (local, 0 at the bottom) that carry a hint - the hatch layers.
    hint_layers: frozenset[int]
    #: How many y-layers hold a heating coil (0 for a machine with no coil channel). Ground truth
    #: the golden tests pin (e.g. the EBF has exactly two).
    coil_layer_count: int
    #: How many distinct built forms the controller has (trigger-stack / shape variants).
    variant_count: int


@dataclass(frozen=True)
class PhysicalDataset:
    """A loaded ``data/multiblocks/`` dump: the run summary plus every machine keyed by display name."""

    meta: DatasetMeta
    machines: Mapping[str, MachinePhysical]

    def get(self, key: str) -> MachinePhysical | None:
        """The physical record for a machine by display name, or ``None`` if the dump lacks it."""
        return self.machines.get(key)


def _extent(
    offsets: Iterable[tuple[int, int, int]],
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The ``(min_corner, size)`` of the smallest box covering ``offsets`` (size = max-min+1 per axis)."""
    points = list(offsets)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    min_corner = (min(xs), min(ys), min(zs))
    size = (max(xs) - min_corner[0] + 1, max(ys) - min_corner[1] + 1, max(zs) - min_corner[2] + 1)
    return min_corner, size


def _hint_faces(
    variant: Variant, min_corner: tuple[int, int, int], size: tuple[int, int, int]
) -> frozenset[Facing]:
    """The bounding-box faces the variant's hint positions touch (its I/O-capable faces).

    Each hint is translated to box-local coordinates; a hint on a min/max plane of an axis lies on
    that face of the box, so a hatch there can face outward that way. A corner hint touches up to
    three faces; an interior hint (should not occur in a shell) touches none.
    """
    faces: set[Facing] = set()
    for hint in variant.hints:
        lx, ly, lz = (hint.d[i] - min_corner[i] for i in range(3))
        if lx == 0:
            faces.add(Facing.WEST)
        if lx == size[0] - 1:
            faces.add(Facing.EAST)
        if ly == 0:
            faces.add(Facing.DOWN)
        if ly == size[1] - 1:
            faces.add(Facing.UP)
        if lz == 0:
            faces.add(Facing.NORTH)
        if lz == size[2] - 1:
            faces.add(Facing.SOUTH)
    return frozenset(faces)


def _primary_variant(doc: MultiblockDoc) -> Variant:
    """The variant that stands for the machine's footprint: the largest built form.

    Trigger-stack sweeps produce size variants (bigger stack -> bigger structure); the fully-built
    form is the one with the most blocks, ties broken by the larger trigger stack for determinism.
    """
    return max(doc.variants, key=lambda v: (len(v.blocks), v.trigger_stack_size))


def to_physical(doc: MultiblockDoc) -> MachinePhysical:
    """Interpret one :class:`MultiblockDoc` into its :class:`MachinePhysical` record.

    Derives the footprint from the blocks the primary variant actually spans (not the extractor's
    reported ``bbox``, which is only cross-checked), the I/O faces from the hint positions, and the
    coil-layer count from the coil substitution table. Raises :class:`DatasetError` if the derived
    box disagrees with the reported one.
    """
    variant = _primary_variant(doc)
    min_corner, size = _extent(b.d for b in variant.blocks)
    if size != variant.bbox:
        raise DatasetError(
            f"{doc.controller.display_name!r}: variant blocks span {size} but bbox says "
            f"{variant.bbox} (a bad scan bound or facing bug in the extractor)"
        )
    footprint = CellBox(sx=size[0], sy=size[1], sz=size[2])

    coil_blocks = {(s.block, s.meta) for s in doc.substitutions.get(_COIL_CHANNEL, [])}
    coil_layers = {
        b.d[1] - min_corner[1] for b in variant.blocks if (b.block, b.meta) in coil_blocks
    }
    hint_layers = {h.d[1] - min_corner[1] for h in variant.hints}

    return MachinePhysical(
        key=doc.controller.display_name,
        registry_name=doc.controller.registry_name,
        meta=doc.controller.meta,
        source_class=doc.controller.source_class,
        footprint=footprint,
        io_faces=_hint_faces(variant, min_corner, size),
        hint_layers=frozenset(hint_layers),
        coil_layer_count=len(coil_layers),
        variant_count=len(doc.variants),
    )


def load_physical_dataset(data_dir: str | Path | None = None) -> PhysicalDataset:
    """Load and interpret every multiblock file under ``data_dir`` (defaults to the committed dump).

    Reads ``_meta.json`` for the run summary, then every other ``*.json`` file as a controller,
    keying the resulting records by display name. Raises :class:`DatasetError` on a duplicate key so
    two files can never silently shadow one machine.
    """
    directory = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    meta = load_meta(directory / "_meta.json")
    machines: dict[str, MachinePhysical] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "_meta.json":
            continue
        physical = to_physical(load_multiblock_doc(path))
        if physical.key in machines:
            raise DatasetError(
                f"two files claim machine {physical.key!r} ({path.name} and an earlier file)"
            )
        machines[physical.key] = physical
    return PhysicalDataset(meta=meta, machines=machines)
