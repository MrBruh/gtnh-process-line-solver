# Requirements: physical dataset extraction

What the extraction pipeline must achieve. This doc is the *what*; the *how* is in
[implementation.md](implementation.md), and the mechanics are not restated here. The living
roadmap is [plan.md](plan.md) (temporary).

## Purpose

The solver needs GT:NH **physical** facts it cannot get from gtnh-factory-flow: the exact block
layout of every multiblock, where its I/O hatches may go, and the textures every machine and
casing wears. This pipeline produces those facts as JSON the Python solver reads. Two outputs,
one tool:

- a **multiblock structure dump** (`data/multiblocks/`): per controller, the exact placed blocks,
  hatch-slot positions, size variants, and tiered-block substitutions; and
- a **texture manifest** (`data/textures/manifest.json`): per block a preview draws, the layered
  texture stack (icon + tint + glow) needed to render it.

It is **not** a recipe, ratio, or throughput extractor. That is gtnh-factory-flow's domain (see
[../DESIGN.md](../DESIGN.md)).

## Why runtime extraction (a constraint on the whole approach)

Multiblock structures are defined in **Java code, not data**: characters in a shape array map to
*elements* that are frequently lambdas (tiered coils, hatch-or-casing adders, dynamic shapes).
Static source parsing covers roughly a third of multiblocks and silently mis-describes the rest,
so it is a non-goal as the pipeline. The output must **match in-game behaviour by construction**,
and the only way to guarantee that is to run the same `construct()` the in-game hologram projector
runs, headlessly, and read back what it places. Every requirement below assumes that runtime
strategy.

## What the structure dump must produce

Per multiblock controller, one JSON file carrying **raw facts only** (no solver interpretation):

- **Identity**: registry name, meta, in-game display name, source class, and the facing
  convention the offsets are expressed in.
- **Placed blocks**: for every block the built structure occupies, its `[dx, dy, dz]` offset from
  the controller and its `(registry_name, meta)`. The controller sits at the origin.
- **Hatch slots (hints)**: the positions where the player may place an I/O hatch, captured as the
  structure's hint dots. Hatches themselves are not placed; their legal positions are.
- **Size variants**: when a controller builds different *shapes* at different sizes (a taller
  tower, a longer structure), each distinct shape is one variant.
- **Tiered substitutions**: when a channel only swaps a *tiered block* without changing the shape
  (coil, glass, pipe-casing tiers), the alternatives are recorded once as a substitution table
  rather than exploded into one variant per tier.
- A derived **bounding box**, for the consumer to cross-check.

Plus a run summary (`_meta.json`): schema version, pack and mod versions, generation timestamp,
extractor git SHA, controller count, and the per-controller **failure list**.

**Hatches are a face capability, not placeable blocks.** The dump records where a hatch is
*allowed* (hint slots), not a specific chosen hatch, because which hatch (input vs output, item
vs fluid, tier) is a solver decision, not a structural fact. Auto-placed hatches are suppressed
so the dump is the casing shell plus its degrees of freedom.

## What the texture manifest must produce

Per block a preview can draw (casings, single-block machines, hatches and buses, multiblock
controller hulls, and material-tinted cables for the router's pipes/wires), the **ordered,
bottom-to-top layer stack** GT's own renderer composites, per side (6) and per active state
(idle and running):

- each layer as `{ icon name, RGBA multiply, glow flag }`;
- an `icon -> path-inside-the-jar` map so a consumer can fetch the PNG; and
- a `gaps` list of anything that did not resolve, so coverage regressions are visible in the diff.

**No PNG is ever produced or committed here**, only names and jar paths. GT textures are LGPL; the
consumer fetches them from the pinned mod jar at render time (see Licensing).

## Constraints (non-functional requirements)

1. **Quarantine the Java.** The extractor is one standalone Gradle tool in `tools/`. The Python
   solver never imports or invokes it; it only reads the emitted JSON. If the tool vanished, the
   solver still runs on whatever dataset is committed.
2. **Never fork or patch GT5-Unofficial.** Depend on it (and StructureLib) as pinned libraries
   from the GTNH Nexus. A pack update is a version bump, not a merge.
3. **Extractor emits facts; Python interprets.** Footprints, face constraints, tier semantics,
   anything solver-shaped, are derived on the Python side where the contracts and tests live. The
   moment the Java makes a solver decision it becomes a second, untested codebase.
4. **Fail loud, per-controller.** One broken multiblock (an exception, a runaway sweep, an empty
   scan) lands on the failure list; it never aborts the run or vanishes silently.
5. **Reviewable, deterministic output.** Stable key and list ordering, so a regenerated dataset
   diffs minimally instead of reshuffling.
6. **Licensing.** Extracted structure *facts* are fine. LGPL PNGs stay out of this Apache-2.0
   repo; `NOTICE` credits GT5-Unofficial and StructureLib.

## Commit and delivery policy

- The **structure dump is local-only** (decided 2026-07-08): regenerated on demand, **never
  committed**, and with **no CI**. Only two curated fixtures ship (`gregtech_machine_1000.json` =
  Electric Blast Furnace, `gregtech_machine_1001.json` = Vacuum Freezer) for the tests;
  `data/multiblocks/` is otherwise gitignored. A fresh clone can place and render those two plus
  single-block machines; every other multiblock is a placeholder until a developer runs the
  extractor. Rationale: the shipped example lines barely use multiblock docs, so ~190 churny
  generated files are not worth their repo weight or a weekly Forge CI run.
- The **texture manifest is committed** and refreshed by its own workflow
  (`.github/workflows/update-textures.yml`), because it is the keystone that skins every machine
  in the previewer; without it every machine renders as a placeholder.

## Output contracts

- Structure dump: **schema v1**, the cross-language contract defined by the Pydantic models in
  `src/gtnh_solver/dataset/schema.py` (a JSON Schema is derived from them for non-Python
  consumers). A breaking change bumps `SCHEMA_VERSION` there and in the extractor together.
- Texture manifest: **schema v2**, the layered stack described above.

Field-level shapes live with the code (schema.py and the manifest writer); this doc states only
*what must appear*, not the exact keys.

## Acceptance criteria

- **Golden ground truths** hold (they change only when GTNH does): the EBF main piece is 3x3x4
  with exactly 2 coil layers and hint dots on its hatch layer; the Vacuum Freezer is 3x3x3.
- Every `data/multiblocks/*.json` validates against schema v1; the `_meta.json` failure count
  stays under an agreed threshold (start lenient, ratchet down).
- The texture manifest resolves a non-zero count of blocks, MTEs, and icons; its `gaps` list is
  reviewed for regressions, not required to be empty.

## Non-goals

- Static Java source parsing as the pipeline (kept only as a possible one-off bootstrap).
- Non-StructureLib multiblocks (e.g. Thaumcraft altars).
- Recipes, ratios, throughput, or any non-physical data (gtnh-factory-flow's job).
- Tracking daily or experimental pack builds.
- Committing the structure dump or any PNG.
