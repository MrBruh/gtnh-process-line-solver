# `data/multiblocks/` - extracted multiblock dataset (schema v1)

Committed JSON describing GregTech multiblock controllers: one `<registry_name>.json` file per
controller plus a `_meta.json` run summary. The solver reads only this data; it never runs the
extractor. See `DATASET_EXTRACTION_PLAN.md` (sections 3-4) for the full design.

## These files are ILLUSTRATIVE FIXTURES, not a real dump

The Java extractor that will populate this directory automatically is **lane 2 / issue #45**, and
is not built yet. The files here are **hand-authored by hand to conform to schema v1** so the
Python adapter (`gtnh_solver.dataset.multiblocks`) and its golden tests have something real-shaped
to run against. They encode true GTNH ground truth where the golden tests assert it (the Electric
Blast Furnace is a 3x3x4 shell with two coil layers; the Vacuum Freezer is 3x3x3), but the exact
block metas, hint colours, and `_meta.json` provenance are placeholders. When the real extractor
lands it **replaces every file here**; do not treat these coordinates as authoritative.

## Schema (the contract)

The canonical schema is the Pydantic model `gtnh_solver.dataset.schema.MultiblockDoc` (and
`DatasetMeta` for `_meta.json`), which validates with `extra="forbid"` so a stray field fails loud.
A language-agnostic JSON Schema for the future Java extractor's own tests is available from
`gtnh_solver.dataset.schema.multiblock_json_schema()` - it is generated from that model, so it can
never drift from what the loader accepts. Fields follow `DATASET_EXTRACTION_PLAN.md` section 4.2:

- top-level `schema` (version int), `controller`, `variants`, `substitutions`, `failures`;
- `controller`: `registry_name`, `meta`, `display_name`, `source_class`, `facing_convention`;
- each variant: `trigger_stack_size`, `channels`, `blocks[{d:[x,y,z], block, meta}]`,
  `hints[{d, hint}]`, `bbox`;
- `substitutions`: identity-only channel swaps (e.g. tiered `coil` blocks);
- `_meta.json`: `schema`, `pack_version`, `mod_versions`, `generated_at`, `extractor_sha`,
  `controller_count`, `failures`.

All interpretation of these raw facts - footprint bounding boxes, hint-derived face constraints,
coil-tier semantics - lives in Python (`gtnh_solver.dataset.multiblocks`), never in the extractor.
