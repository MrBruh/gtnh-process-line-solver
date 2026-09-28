# `data/multiblocks/` - extracted multiblock dataset (schema v2)

Committed JSON describing GregTech multiblock controllers: one `<registry_name>.json` file per
controller plus a `_meta.json` run summary. The solver reads only this data; it never runs the
extractor. See `docs/dataset-extraction/` (requirements.md for the what, implementation.md for the
how) for the full design.

## These files are FIXTURES, not a real dump

The Java extractor (`DumperMod`, `StructureDumper`, `JsonWriter`, `ErrorCollector`, `TextureDumper`
under `tools/gtnh-extractor/`) is complete, but its full multiblock dump is **local-only**:
regenerated on demand and **never committed** (see the commit and delivery policy in
`docs/dataset-extraction/requirements.md`, and the `.gitignore` allow-list). Two kinds of file ship
instead.

**Two hand-authored fixtures**, `gregtech_machine_1000.json` (Electric Blast Furnace) and
`gregtech_machine_1001.json` (Vacuum Freezer), so the Python adapter
(`gtnh_solver.dataset.multiblocks`) and its golden tests have something real-shaped to run against.
They are **hand-authored to conform to schema v2** and encode true GTNH ground truth where the
golden tests assert it (the Electric Blast Furnace is a 3x3x4 shell with two coil layers; the
Vacuum Freezer is 3x3x3), but the exact block metas, hint colours, `hatch_slots` kinds, and
`_meta.json` provenance are placeholders. In particular their controller `meta` ids are
illustrative and do NOT match any one GT5U build - a real local dump is the authority on those.

**The controllers the shipped example lines resolve to**, one `gregtech_gt_blockmachines_<meta>.json`
each, so the nitrobenzene lines place real multiblocks and solve on a fresh clone and in CI instead
of falling back to 1x1x1 boxes (a Distillation Tower with seven connections does not fit on one).
These are **trimmed from a real dump**: the extractor's own documents, cut down to the built forms
the examples reserve (a tower keeps its heights up to the tallest one used, not all ten).
`tools/derive_example_multiblocks.py` writes them and checks that every example adapts exactly as
it does against the full dump; rerun it when the examples change or the pack moves, and list any
new file in the `.gitignore` allow-list. They were cut from the **2.9.0-beta-2** dump
(GT5-Unofficial 5.09.54.20, StructureLib 1.4.42), which `_meta.json` does not record: it describes
the directory, and apart from `controller_count` its provenance is a placeholder. The 2.8.4
`gtnh-nitrobenzene.json` resolves against these 2.9 forms too, unless a local 2.8.4 dump exists.
Trimmed files for the shipped examples are allowed; a verbatim dump is not (decided 2026-09-28).

A contributor who wants the solver to place any other multiblock runs the extractor locally.

## Schema (the contract)

The canonical schema is the Pydantic model `gtnh_solver.dataset.schema.MultiblockDoc` (and
`DatasetMeta` for `_meta.json`), which validates with `extra="forbid"` so a stray field fails loud.
A language-agnostic JSON Schema for the Java extractor's own tests is available from
`gtnh_solver.dataset.schema.multiblock_json_schema()` - it is generated from that model, so it can
never drift from what the loader accepts. The fields, restated here for a reader of these files:

- top-level `schema` (version int), `controller`, `variants`, `substitutions`, `failures`;
- `controller`: `registry_name`, `meta`, `display_name`, `source_class`, `facing_convention`;
- each variant: `trigger_stack_size`, `channels`, `blocks[{d:[x,y,z], block, meta}]`,
  `hints[{d, hint}]`, `hatch_slots[{d, kinds}]`, `bbox`;
- `substitutions`: identity-only channel swaps (e.g. tiered `coil` blocks);
- `_meta.json`: `schema`, `pack_version`, `mod_versions`, `generated_at`, `extractor_sha`,
  `controller_count`, `failures`.

All interpretation of these raw facts - footprint bounding boxes, hint-derived face constraints,
coil-tier semantics - lives in Python (`gtnh_solver.dataset.multiblocks`), never in the extractor.
