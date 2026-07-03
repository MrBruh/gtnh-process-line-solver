"""dataset - the GT physical-rules data and its loader.

Footprints, machine faces (front = no I/O; five usable), pipe/wire tiers + throughputs,
voltage tiers, ME behavior, and cell->block mappings. This is the single biggest piece of
real work and is GT-version-specific. Rule RULES live here as DATA; the validator re-checks
them with independent LOGIC (docs/ARCHITECTURE.md #4). See docs/DOMAIN.md for the rules.

Shipped so far: the per-tier **voltage** ladder, the **cable loss** constant, and the amperage
helper (``voltage`` submodule) that the shared-amperage power feature needs; and the **multiblock**
footprint/face dataset - a schema-v1 loader (``schema``) for the extractor's ``data/multiblocks/``
JSON plus the adapter (``multiblocks``) that interprets those raw facts into IR-shaped physical
records (footprints, hint-derived faces, coil tiers). Still TODO(dataset): per-material cable loss;
throughput/tier caps; the real extractor (issue #45) replacing the illustrative fixtures; spot-check
tiers/face-rules/throughputs in-game (docs/ROADMAP.md step 0).
"""

from __future__ import annotations

from .multiblocks import (
    DEFAULT_DATA_DIR,
    DatasetError,
    MachinePhysical,
    PhysicalDataset,
    load_physical_dataset,
    to_physical,
)
from .schema import (
    SCHEMA_VERSION,
    Block,
    Controller,
    ControllerFailure,
    DatasetMeta,
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
    VOLTAGE_BY_TIER,
    UnknownTierError,
    UnpowerableError,
    amperage,
    delivered_voltage,
    tier_voltage,
)

__all__ = [  # noqa: RUF022 - grouped by submodule, not alphabetized
    # voltage / power sizing
    "CABLE_LOSS_PER_BLOCK",
    "VOLTAGE_BY_TIER",
    "UnknownTierError",
    "UnpowerableError",
    "amperage",
    "delivered_voltage",
    "tier_voltage",
    # multiblock schema v1 (raw extractor facts)
    "SCHEMA_VERSION",
    "Controller",
    "Block",
    "Hint",
    "Variant",
    "Substitution",
    "MultiblockDoc",
    "ControllerFailure",
    "DatasetMeta",
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
]
