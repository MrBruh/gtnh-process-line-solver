"""dataset - the GT physical-rules data and its loader.

Footprints, machine faces (front = no I/O; five usable), pipe/wire tiers + throughputs,
voltage tiers, ME behavior, and cell->block mappings. This is the single biggest piece of
real work and is GT-version-specific. Rule RULES live here as DATA; the validator re-checks
them with independent LOGIC (docs/ARCHITECTURE.md #4). See docs/DOMAIN.md for the rules.

Shipped so far: the per-tier **voltage** ladder, the **cable loss** constant, and the amp-load
helpers (``voltage`` submodule) that the shared-amperage power feature needs - machines average
a *fractional* amp load and only aggregates round up to whole amps; and the **multiblock**
footprint/face dataset - a schema-v2 loader (``schema``) for the extractor's ``data/multiblocks/``
JSON plus the adapter (``multiblocks``) that interprets those raw facts into IR-shaped physical
records (footprints, hint-derived faces, coil tiers); and the **item pipe capacity** per size
(``pipe_capacity``) a route's gauge is chosen from (#165). Still TODO(dataset): per-material cable
loss; fluid pipe throughput; the real extractor (issue #45) replacing the illustrative fixtures;
spot-check tiers/face-rules/throughputs in-game (docs/ROADMAP.md step 0).
"""

from __future__ import annotations

# The cable ladder is rule data, but its canonical home is the output contract that enforces
# membership (ir/output.py): dataset imports ir, never the reverse, so the contract cannot end up
# in an import cycle with the dataset loader (which needs ir types for footprints/facings).
from gtnh_solver.ir.output import CABLE_THICKNESSES, MAX_CABLE_THICKNESS

from .multiblocks import (
    DEFAULT_DATA_DIR,
    DatasetError,
    MachinePhysical,
    PhysicalDataset,
    load_physical_dataset,
    to_physical,
)
from .pipe_capacity import (
    ITEM_PIPE_CAPACITY,
    ITEMS_PER_INSERTION,
    STREAM_SERVICE_TICKS,
    endpoint_insertions,
    item_pipe_insertions,
    item_pipe_size_for,
)
from .pipes import (
    CABLE_DISPLAY_MATERIAL,
    CABLE_MATERIAL_BY_TIER,
    CABLE_THICKNESS_BLOCKS,
    DEFAULT_PIPE_SIZE,
    DEFAULT_PIPE_THICKNESS_BLOCKS,
    PIPE_DISPLAY_STEM,
    PIPE_MATERIAL,
    PIPE_THICKNESS_BLOCKS,
    ROUTED_PIPE_SIZES,
    cable_display_name,
    manifest_names,
    pipe_display_name,
    route_material,
)
from .roots import (
    DEFAULT_DATA,
    DatasetWarning,
    extractor_hint,
    generated_at,
    list_versions,
    resolve_dataset_path,
)
from .schema import (
    SCHEMA_VERSION,
    Block,
    Controller,
    ControllerFailure,
    DatasetMeta,
    DatasetSchemaError,
    Hint,
    MultiblockDoc,
    Substitution,
    Variant,
    load_meta,
    load_multiblock_doc,
    multiblock_json_schema,
)
from .voltage import (
    CABLE_LOSS_PER_BLOCK,
    DESIGN_RUN_BLOCKS,
    ENERGY_HATCH_AMPS,
    VOLTAGE_BY_TIER,
    UnknownTierError,
    UnpowerableError,
    amp_load,
    delivered_voltage,
    energy_hatches_for,
    machine_amps_in,
    tier_voltage,
    tiers_above,
    whole_amps,
)

__all__ = [  # noqa: RUF022 - grouped by submodule, not alphabetized
    # cable / pipe stand-in policy
    "CABLE_DISPLAY_MATERIAL",
    "CABLE_MATERIAL_BY_TIER",
    "CABLE_THICKNESS_BLOCKS",
    "DEFAULT_PIPE_SIZE",
    "DEFAULT_PIPE_THICKNESS_BLOCKS",
    "PIPE_DISPLAY_STEM",
    "PIPE_MATERIAL",
    "PIPE_THICKNESS_BLOCKS",
    "ROUTED_PIPE_SIZES",
    "cable_display_name",
    "manifest_names",
    "pipe_display_name",
    "route_material",
    # item pipe capacity (sizing)
    "ITEM_PIPE_CAPACITY",
    "ITEMS_PER_INSERTION",
    "STREAM_SERVICE_TICKS",
    "endpoint_insertions",
    "item_pipe_insertions",
    "item_pipe_size_for",
    # voltage / power sizing
    "CABLE_LOSS_PER_BLOCK",
    "CABLE_THICKNESSES",
    "DESIGN_RUN_BLOCKS",
    "MAX_CABLE_THICKNESS",
    "VOLTAGE_BY_TIER",
    "ENERGY_HATCH_AMPS",
    "UnknownTierError",
    "UnpowerableError",
    "amp_load",
    "delivered_voltage",
    "energy_hatches_for",
    "machine_amps_in",
    "tier_voltage",
    "tiers_above",
    "whole_amps",
    # multiblock schema v2 (raw extractor facts)
    "SCHEMA_VERSION",
    "Controller",
    "Block",
    "Hint",
    "Variant",
    "Substitution",
    "MultiblockDoc",
    "ControllerFailure",
    "DatasetMeta",
    "DatasetSchemaError",
    "load_multiblock_doc",
    "load_meta",
    "multiblock_json_schema",
    # multiblock adapter (interpreted physical rules)
    "DEFAULT_DATA_DIR",
    "DatasetError",
    "MachinePhysical",
    "PhysicalDataset",
    "to_physical",
    "load_physical_dataset",
    # dataset location (version-namespaced local folders + committed fixtures)
    "DEFAULT_DATA",
    "DatasetWarning",
    "extractor_hint",
    "generated_at",
    "list_versions",
    "resolve_dataset_path",
]
