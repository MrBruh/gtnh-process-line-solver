"""Adapter: schema-v2 multiblock facts -> the solver's physical-rules dataset.

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

from gtnh_solver.ir import CellBox, CellCoord, Facing, HatchSlot

from .roots import resolve_dataset_path
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


#: A hatch kind that receives one of a recipe's fluid OUTPUTS (``gregtech.api.enums.HatchElement``).
_OUTPUT_HATCH = "OutputHatch"

#: The hatch kind that takes power in (``gregtech.api.enums.HatchElement.Energy``).
_ENERGY_HATCH = "Energy"

#: Hatch kinds that occupy a cell for the machine's own upkeep rather than for routed I/O. Every
#: multiblock that accepts them needs exactly one of each, so the adapter budgets them away before
#: deciding how many cells are left for energy hatches.
_UPKEEP_HATCHES = ("Maintenance", "Muffler")


@dataclass(frozen=True)
class VariantShape:
    """One built form of a machine: how big it is and how many fluid outputs it can route.

    ``output_layers`` counts the distinct y-layers carrying a cell that accepts an ``OutputHatch``.
    For a layer-indexed machine that IS its routable-output capacity: a Distillation Tower sends the
    recipe's fluid output ``i`` to layer ``i`` and nowhere else, so a tower with fewer layers than
    the recipe has fluid outputs silently voids the remainder (it is still a legal build, which is
    why nothing catches it at runtime). 0 when the dump recorded no hatch slots (a pre-v2 dump, or a
    machine whose adders expose no item filter) - which reads as "unknown", not "cannot output".
    """

    footprint: CellBox
    output_layers: int
    #: How many cells of *this* form accept a hatch of any kind. Per form, not per machine: a
    #: Distillation Tower built 3x6x3 has its own count, and charging it the 3x12x3 form's would
    #: let the solver wire more connections onto it than the shape it reserved can host.
    hatch_cells: int = 0
    #: How many of this form's cells accept an ``Energy`` hatch specifically.
    energy_hatch_cells: int = 0
    #: How many upkeep hatches (maintenance, muffler) this form accepts, and so needs one each of.
    upkeep_hatch_count: int = 0
    #: Where those cells are, as offsets from THIS form's minimum corner (the corner
    #: ``Placement.cell`` names), so a consumer can turn them with the machine. Empty when the dump
    #: recorded no slots, which reads as "unknown", not "none".
    slots: tuple[HatchSlot, ...] = ()

    def energy_hatch_budget(self, routed_ports: int = 0) -> int:
        """Cells left for energy hatches once ``routed_ports`` item/fluid connections and this
        form's upkeep hatches have taken theirs.

        The binding limit is the *shared* pool of casing cells, not the ``Energy`` count alone: an
        input bus and an energy hatch compete for the same cell. Returns 0 when the dump recorded
        no hatch slots, which callers read as "unknown, impose no ceiling" rather than "none".
        """
        if not self.hatch_cells:
            return 0
        free = self.hatch_cells - self.upkeep_hatch_count - routed_ports
        return max(0, min(self.energy_hatch_cells, free))


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
    #: Every built form, smallest first. One entry for a fixed-shape machine; a parametric one (a
    #: tower that grows a layer per trigger-stack step) has the whole family, which is what lets
    #: :meth:`footprint_for` size it to a recipe instead of always reserving the maximum.
    variants: tuple[VariantShape, ...] = ()
    #: How many distinct cells of the primary variant accept a hatch of any kind. A GT multiblock
    #: builds its casing shell out of interchangeable cells: the structure asks only that a cell
    #: hold *something* legal, so almost any casing may be swapped for an input bus, an output
    #: hatch, a maintenance hatch **or an energy hatch**. This is that count - the ceiling on the
    #: machine's total connections. 0 when the dump recorded no hatch slots (a pre-v2 dump), which
    #: reads as "unknown", not "accepts none".
    #:
    #: Describes the same form :attr:`footprint` does. A machine that reserves a *smaller* form for
    #: its recipe has fewer cells than this, so read the count off :meth:`variant_for` rather than
    #: here whenever a ``fluid_outputs`` is in hand.
    hatch_cells: int = 0
    #: How many of those cells accept an ``Energy`` hatch specifically. Usually equal to
    #: :attr:`hatch_cells` (a cell that takes any hatch generally takes a power one), but recorded
    #: separately because a machine CAN restrict where power enters and guessing would be wrong.
    energy_hatch_cells: int = 0
    #: How many upkeep hatches (maintenance, muffler) this machine accepts, and so needs one each
    #: of. Cells they occupy cannot carry routed I/O, so the budget subtracts them.
    upkeep_hatch_count: int = 0

    def energy_hatch_budget(self, routed_ports: int = 0, fluid_outputs: int = 0) -> int:
        """:meth:`VariantShape.energy_hatch_budget` for the form this recipe reserves.

        ``fluid_outputs`` selects the form, exactly as :meth:`footprint_for` does, so the budget is
        always charged against the shape the builder is actually told to raise.
        """
        return self.variant_for(fluid_outputs).energy_hatch_budget(routed_ports)

    @property
    def is_layer_indexed(self) -> bool:
        """Whether each successive form adds exactly one layer AND exactly one routable output.

        Only then does ``output_layers`` mean "fluid outputs this form can route", and only then may
        :meth:`footprint_for` pick a smaller form. A Distillation Tower qualifies (heights 3..12 carry
        2..11 output layers, one per step). A Mega Distillation Tower does NOT: its "output layer" is
        a 5-block band whose whole ring accepts an output hatch, so the layer count runs 5x ahead of
        the routable count. Selecting on that would pick a tower ~5x too short and silently void
        fluids, so an unrecognised growth pattern falls back to the largest form - which over-reserves
        but can never lose product. Erring toward the big shape is the only safe direction here.
        """
        if len(self.variants) < 2:
            return False
        # strict=False: pairing a sequence with its own tail is deliberately ragged by one.
        for smaller, larger in zip(self.variants, self.variants[1:], strict=False):
            if larger.output_layers - smaller.output_layers != 1:
                return False
            if larger.footprint.sy - smaller.footprint.sy != 1:
                return False
        return self.variants[0].output_layers > 0

    def variant_for(self, fluid_outputs: int = 0) -> VariantShape:
        """The built form this recipe reserves: the smallest that can route ``fluid_outputs``
        fluids, else the form :attr:`footprint` describes.

        **The single selection point.** Footprint and hatch counts must come from the same form or
        they describe different buildings: a Distillation Tower reserved 3x6x3 used to be charged
        the 3x12x3 form's 97 hatch cells, a ceiling twice its own, which with geometric hatch slots
        would place hatches outside the box the builder was told to raise.
        """
        if self.variants and self.is_layer_indexed:
            for shape in self.variants:  # smallest first
                if shape.output_layers >= fluid_outputs:
                    return shape
            return self.variants[-1]
        return self._primary_shape()

    def _primary_shape(self) -> VariantShape:
        """The form :attr:`footprint` describes, with that form's own hatch counts.

        Looked up by footprint rather than by index because :attr:`footprint` comes from the variant
        with the most blocks while :attr:`variants` is ordered by volume; those agree for every
        machine in the dump but nothing guarantees it. Falls back to the record's own counts, which
        is the path a hand-built record and a pre-v2 dump (no variants at all) take.
        """
        for shape in self.variants:
            if shape.footprint == self.footprint:
                return shape
        return VariantShape(
            footprint=self.footprint,
            output_layers=0,
            hatch_cells=self.hatch_cells,
            energy_hatch_cells=self.energy_hatch_cells,
            upkeep_hatch_count=self.upkeep_hatch_count,
        )  # no slots: a hand-built record has no offsets to hand out

    def footprint_for(self, fluid_outputs: int = 0) -> CellBox:
        """The smallest built form that can route ``fluid_outputs`` fluids, else the largest form.

        For a layer-indexed machine this is the difference between telling a builder to raise a
        3-tall tower and a 12-tall one. Everything else - a fixed-shape machine, a growth pattern we
        cannot read, or a pre-v2 dump with no hatch data - keeps the previous behaviour of the
        largest form.
        """
        return self.variant_for(fluid_outputs).footprint

    @property
    def block_key(self) -> str:
        """This machine's controller block as ``"<registry_name>@<meta>"``.

        The exact join key an export carries in ``recipe.source.machineBlock.id``
        (gtnh-factory-flow #25), and the one identity that survives the naming-world gap between
        the exporter's recipe-map names and this dump's controller-block names.
        """
        return f"{self.registry_name}@{self.meta}"


@dataclass(frozen=True)
class PhysicalDataset:
    """A loaded ``data/multiblocks/`` dump: the run summary plus every machine keyed by display name."""

    meta: DatasetMeta
    machines: Mapping[str, MachinePhysical]

    @property
    def by_block_key(self) -> Mapping[str, MachinePhysical]:
        """Every machine indexed by :attr:`MachinePhysical.block_key` (``"<registry>@<meta>"``).

        Derived rather than stored so it cannot drift from ``machines``; the dump is ~200 entries,
        so rebuilding it per lookup is not worth caching.
        """
        return {m.block_key: m for m in self.machines.values()}

    def get(self, key: str, block_key: str | None = None) -> MachinePhysical | None:
        """The physical record for a machine, or ``None`` if the dump lacks it.

        ``block_key`` (the export's controller-block id) is tried FIRST and is authoritative: it is
        an exact identity, whereas ``key`` is the exporter's localized recipe-map name, which for a
        GT++ machine differs from the controller block's own name this dump is keyed by
        ("Chemical Plant" vs "ExxonMobil Chemical Plant"). Falling back to ``key`` keeps every
        pre-#25 plan resolving exactly as it did. A ``block_key`` the dump does not know falls back
        too, rather than failing: the dump is a partial snapshot (controllers whose extraction
        failed are simply absent), so an unknown block is a miss, not an error.
        """
        if block_key is not None:
            record = self.by_block_key.get(block_key)
            if record is not None:
                return record
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


def _hatch_counts(variant: Variant) -> tuple[int, int, int]:
    """``(hatch_cells, energy_hatch_cells, upkeep_hatch_count)`` for one built form.

    Shared by :func:`to_physical` and :func:`_variant_shapes` so a form's counts cannot drift from
    the counts recorded against the machine as a whole.
    """
    slots = variant.hatch_slots
    return (
        len(slots),
        sum(1 for s in slots if _ENERGY_HATCH in s.kinds),
        sum(1 for kind in _UPKEEP_HATCHES if any(kind in slot.kinds for slot in slots)),
    )


def _hatch_slots(variant: Variant, min_corner: tuple[int, int, int]) -> tuple[HatchSlot, ...]:
    """One form's hatch slots, re-anchored from controller-relative to minimum-corner-relative.

    The dump states its offsets as world-space deltas **from the controller block**, which is not
    generally the structure's minimum corner (the Coke Oven has a slot at ``[-1, 0, 0]``). Every
    other consumer measures from the minimum corner, because that is what ``Placement.cell`` names
    and what ``ir.geometry.occupied_cells`` expands from, so the translation happens once, here.

    Sorted, and ``kinds`` sorted within each slot, so a layout built from this is reproducible: the
    dump's own order is whatever StructureLib's element visit produced.
    """
    return tuple(
        sorted(
            (
                HatchSlot(
                    offset=CellCoord(
                        x=slot.d[0] - min_corner[0],
                        y=slot.d[1] - min_corner[1],
                        z=slot.d[2] - min_corner[2],
                    ),
                    kinds=tuple(sorted(slot.kinds)),
                )
                for slot in variant.hatch_slots
            ),
            key=lambda s: (s.offset.y, s.offset.z, s.offset.x, s.kinds),
        )
    )


def _primary_variant(doc: MultiblockDoc) -> Variant:
    """The variant that stands for the machine's footprint: the largest built form.

    Trigger-stack sweeps produce size variants (bigger stack -> bigger structure); the fully-built
    form is the one with the most blocks, ties broken by the larger trigger stack for determinism.
    """
    return max(doc.variants, key=lambda v: (len(v.blocks), v.trigger_stack_size))


def to_physical(doc: MultiblockDoc) -> MachinePhysical:
    """Interpret one :class:`MultiblockDoc` into its :class:`MachinePhysical` record.

    Derives the footprint from the blocks the primary variant actually spans (not the extractor's
    reported ``bbox``, which is only cross-checked), the I/O faces from the hint positions, the
    coil-layer count from the coil substitution table, and the hatch-cell capacity from the
    recorded hatch slots. Raises :class:`DatasetError` if the derived box disagrees with the
    reported one.
    """
    variant = _primary_variant(doc)
    min_corner, size = _extent(b.d for b in variant.blocks)
    if size != variant.bbox:
        raise DatasetError(
            f"{doc.controller.display_name!r}: variant blocks span {size} but bbox says "
            f"{variant.bbox} (a bad scan bound or facing bug in the extractor)"
        )
    footprint = CellBox(sx=size[0], sy=size[1], sz=size[2])
    hatch_cells, energy_hatch_cells, upkeep_hatch_count = _hatch_counts(variant)

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
        variants=_variant_shapes(doc),
        hatch_cells=hatch_cells,
        energy_hatch_cells=energy_hatch_cells,
        upkeep_hatch_count=upkeep_hatch_count,
    )


def _variant_shapes(doc: MultiblockDoc) -> tuple[VariantShape, ...]:
    """Every built form as ``(footprint, output_layers)``, smallest first.

    Sized from the blocks each form actually spans (the same derivation the primary variant gets, so
    a form's footprint here always agrees with :attr:`MachinePhysical.footprint` for the largest).
    Ordered by volume so :meth:`MachinePhysical.footprint_for` can take the first form that fits.
    """
    shapes = []
    for variant in doc.variants:
        min_corner, size = _extent(b.d for b in variant.blocks)
        output_layers = {
            slot.d[1] - min_corner[1] for slot in variant.hatch_slots if _OUTPUT_HATCH in slot.kinds
        }
        hatch_cells, energy_hatch_cells, upkeep_hatch_count = _hatch_counts(variant)
        shapes.append(
            VariantShape(
                footprint=CellBox(sx=size[0], sy=size[1], sz=size[2]),
                output_layers=len(output_layers),
                hatch_cells=hatch_cells,
                energy_hatch_cells=energy_hatch_cells,
                upkeep_hatch_count=upkeep_hatch_count,
                slots=_hatch_slots(variant, min_corner),
            )
        )
    return tuple(sorted(shapes, key=lambda s: s.footprint.sx * s.footprint.sy * s.footprint.sz))


def load_physical_dataset(
    data_dir: str | Path | None = None, *, version: str | None = None
) -> PhysicalDataset:
    """Load and interpret every multiblock file under ``data_dir``.

    With no explicit ``data_dir`` the location is resolved (:func:`resolve_dataset_path`): the newest
    local ``data/<version>/multiblocks/`` that exists, else the committed fixtures, with ``version``
    to pin one. Reads ``_meta.json`` for the run summary, then every other ``*.json`` file as a
    controller, keying the resulting records by display name. Raises :class:`DatasetError` on a
    duplicate key so two files can never silently shadow one machine.
    """
    directory = (
        Path(data_dir)
        if data_dir is not None
        else resolve_dataset_path("multiblocks", version=version)
    )
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
