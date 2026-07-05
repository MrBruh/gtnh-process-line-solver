# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Previewer real GT textures via per-block cubes and a Pillow bake (`previewer/`, lane 7 v2,
  GitHub #50).** Supersedes the v1 that skinned one stretched box per machine with a single
  representative casing - a defect that erased the coils, glass, and hatch faces that make a layout
  readable. The previewer now materialises each placed machine into ONE textured cube per constituent
  block (principle 6): it looks up the machine's extracted multiblock doc, selects the representative
  variant, expands its `blocks` list at each block's `[dx, dy, dz]` offset (yaw-oriented to the placed
  front, so the controller's front overlay points the way the solver oriented it), and textures every
  cube face independently. Cubes are clamped to the machine's reserved footprint (and a yaw that
  would spill a non-cubic machine past it falls back to native orientation), so one machine's blocks
  can never overlap a neighbour - wall-sharing is a GTNH feature left to a later change. Only a
  genuine 1x1x1 machine takes the single-block path (via the manifest's display-name index); a
  doc-less MULTIblock, such as the dynamic-height Distillation Tower whose extraction overflowed the
  variant cap, keeps its placeholder box rather than collapsing to a lone controller cube. A new Pillow
  pre-bake (`previewer/bake.py`) composites each face's layer stack - base times its RGBA multiply,
  then alpha-composited overlays, animated sprites reduced to frame 0 - into one flat 16x16 PNG per
  `(block, meta, side, state)`, so the three.js viewer only ever loads flat images and never
  composites at runtime; a face with no baked texture falls back to a neutral casing grey, an
  un-baked machine keeps its placeholder box. Pillow is the optional `preview` extra and its absence
  degrades the whole pass to placeholders. Golden tests pin the tint multiply (a machine base is never
  neutral grey), the per-block expansion (a multiblock is many distinct cubes, not one box; an
  interior coil textures distinct from the casing), and icon-name stability. PNGs stay LGPL and
  uncommitted, fetched from the pinned jar into an out-of-repo cache (`previewer/jar.py`, injected so
  the test suite never fetches).
- **`LayoutMetrics` footprint/layers are now populated (`solver`, GitHub #13).** `solve()` fills
  `LayoutResult.metrics.footprint` (floor-area bounding box of machines plus routes) and `.layers`
  (vertical extent) on every assembled layout, computed from the same occupied-cell basis the
  feedback loop ranks on. These are consumed as data (the seed-compare workflow, and the previewer
  embeds them in its scene JSON); previously they were always `null`. `buildability` and
  `congestion` stay `None` until a scoring model is defined; an infeasible (nothing-placed) result
  leaves all metrics `None`.
- **Adapter consumes the plan-schema-v2 `resolved` block (`adapter/`, GitHub #2).** A
  gtnh-factory-flow v2 export (`schemaVersion: 2`) additively carries `app`,
  `datasetVersionId`, and a `resolved` throughput block (per-machine EU/t, per-edge rates,
  external I/O, a power total); `adapter/plan.py` now parses all of it typed (unknown
  subfields stay tolerated). When `resolved` covers a node, its `totalEut` is trusted for
  `Machine.eut` - the exporter's balancer models overclocking, which `recipe.eut * parallel`
  cannot - and cross-checked against that synthesis: a divergence beyond float tolerance
  emits an `AdapterWarning` (new, exported) but the resolved figure wins, so power amperage
  is sized for the real draw. `resolved.power.totalEut` is likewise cross-checked against the
  synthesized per-tier power nets. v1 plans (no `resolved`) adapt exactly as before.
  `examples/gtnh-sand.json` is refreshed to the v2 export (adapter output unchanged - its
  resolved figures match the synthesis); the v2 nitrobenzene export ships as
  `tests/fixtures/gtnh-nitrobenzene-v2.json` instead of replacing the example, because its
  resolved EU/t legitimately diverges (overclocked LCR: 2880 vs 480 EU/t) and would shift the
  example-pinned power numbers.
- **Extractor channel handling and identity-substitution tables (`tools/gtnh-extractor/`, lane 3,
  GitHub #46).** `StructureDumper` now fills the per-controller `substitutions` object. After the
  trigger-stack sweep it probes each GT channel (`GTStructureChannels.values()`, skipping the
  always-applied `gt_no_hatch`) against the default build: holding the stack size at 1 it sets one
  channel at a time and diffs the placed blocks. Because an unset StructureLib channel reads the
  trigger's stack size, the existing stack sweep already varies every channel, so shape-changing
  channels (a distillation tower's `height`, a structure's `length`) are already recorded as size
  variants and the probe skips them; a channel that only swaps a tiered block (coil, glass, pipe
  casing) keeps the same shape and is recorded once as `substitutions[channel]` = the default tier
  plus every distinct higher tier `{channel_value, block, meta}`, rather than exploding into one
  variant per tier. The default-placed block is always included, which is what lets the Python
  adapter match the tiered blocks in the primary variant. Heating coils are a special case: the
  classic furnaces (Electric Blast Furnace, Multi Smelter, ...) place a bare `ofCoil` whose tier is
  read from the trigger's stack size rather than the `coil` channel, so the coil table is built by a
  separate stack-size sweep that identifies coil blocks by the GT `IHeatingCoil` interface (which
  also covers the channel-bound mega furnaces). New hard caps bound the per-channel value sweep and
  the total substitution entries; a controller that overflows them lands on the `_meta.json` failure
  list instead of emitting a runaway table. The Electric Blast Furnace stays one 3x3x4 shape variant
  and now carries a populated `coil` substitution table (14 tiers), so the adapter counts 2 coil
  layers.
- **Layered server-side `ITexture` texture manifest (`tools/gtnh-extractor/`, lane 6 v2, GitHub #79;
  spike #78).** Supersedes v1's flat single-icon Option A, which could only name casing shells and
  gapped every single-block machine and controller hull. A one-day spike (#78) first proved, against a
  booted GT5-Unofficial server, that the 6-arg `getTexture(...)` is not `@SideOnly(CLIENT)` and the
  `ITexture` layer objects store plain server-safe fields (`mIconContainer`, `mRGBa` via `getRGBA()`,
  `glow`, wrapper `mTextures`). The rewritten `TextureDumper` then emits schema-2 layered manifest
  entries: for every MetaTileEntity - via the `getXxxFacing{Inactive,Active}(byte)` accessors for
  basic single-block machines (reliable with no tile entity; `getTexture` NPEs on a bare placement for
  some) and via `getTexture(base, side, facing, colour, active, redstone)` for hulls and hatches
  (placed like `StructureDumper` does) - it walks the layer stack per side and active state, resolving
  each `GTRenderedTexture` to `{icon, rgba, glow}`, recursing multi/sided wrappers via `mTextures`, and
  resolving a hull's copied casing base through the block-icon path. The plain structure blocks a
  multiblock places (casings, coils) keep the v1 block-icon mechanism as un-tinted single layers.
  Icon names come from the `Textures.BlockIcons` enum `name()` (the client-only `getTextureFile()`
  throws server-side, as the spike confirmed), mapping 1:1 to the PNGs under
  `assets/<modid>/textures/blocks/`. Each MTE entry carries its `display_name` so the previewer can
  render single-block machines. PNGs are never committed; unresolved units (exotic ISBRH renderers,
  a tail of newer casing families) land on the manifest `gaps` for a follow-up. Wired additively via
  `-PtextureOut`: set alone the run is texture-only and skips the structure dump.
- **Extractor core dump loop (`tools/gtnh-extractor/`, lane 2, GitHub #45).** The Java tool now
  fills its `DumperMod.dump()` seam with `StructureDumper` + `JsonWriter` + `ErrorCollector` and
  emits the schema-v1 dataset. It iterates `GregTechAPI.METATILEENTITIES`, keeps the
  `IConstructable` controllers, and for each places it at a fixed origin in the server overworld,
  sweeps the trigger stack (size 1..N, stopping when the placed cell set stops changing so
  identity-only tier swaps collapse into one variant), and per size runs a hint pass
  (`construct(_, hintsOnly=true)` with a `RecordingProxy` swapped into `StructureLib.proxy`, and the
  world's `isRemote` flag briefly flipped since the hint walk is client-only) plus a block pass
  (`construct(_, hintsOnly=false)` with the `gt_no_hatch` channel, then scan). It writes one
  `<datasetOut>/multiblocks/<name>.json` per controller plus a `_meta.json` run summary (schema,
  pack version, mod versions, timestamp, extractor SHA, controller count, failures), with stable
  key + variant ordering. Every controller is wrapped so an exception, a non-terminating/explosive
  sweep, or an empty scan lands in `_meta.json.failures` rather than aborting the run; hint capture
  is best-effort so a controller with client-only icon hints still dumps its geometry. The output
  directory and run metadata come from `-PdatasetOut`/`-PpackVersion`/`-PextractorSha`. A verified
  headless `runServer` boot dumps 191 of 209 constructable controllers (Electric Blast Furnace
  3x3x4, Vacuum Freezer 3x3x3), all validating against `dataset/schema.py`. Channel handling /
  identity-substitution tables (`substitutions` stays empty) are lane 3; textures are lane 6.
- **Multiblock physical dataset - schema v1 + Python adapter (`dataset/`, GitHub #48).** The
  first slice of the automated dataset-extraction pipeline (`DATASET_EXTRACTION_PLAN.md`): the
  path from an extractor's raw JSON to the solver's physical rules. `dataset/schema.py` is a typed,
  `extra="forbid"` Pydantic loader for schema v1 (`MultiblockDoc` + `_meta.json` `DatasetMeta`,
  per plan section 4.2: `schema`, `controller`, `variants[blocks/hints/bbox]`, `substitutions`,
  `failures`), the cross-language contract for the future Java extractor (issue #45), with a
  derived JSON Schema (`multiblock_json_schema()`) for non-Python consumers so it cannot drift.
  `dataset/multiblocks.py` is the adapter that does **all interpretation in Python** (plan design
  principle 3): it derives each machine's footprint bounding box, hint-derived I/O faces, and
  coil-tier count from the raw facts into an IR-shaped `MachinePhysical`, and `load_physical_dataset`
  keys a whole dump by display name. Because the real extractor is not built yet, illustrative
  hand-authored fixtures ship under `data/multiblocks/` (Electric Blast Furnace, Vacuum Freezer)
  marked as such in a README, so the adapter and golden tests run today. Golden tests pin the
  ground truths (EBF is 3x3x4 with two coil layers and hatch-layer hints; Vacuum Freezer is 3x3x3)
  plus schema validation (every file validates, `_meta.json` failure list under a lenient
  threshold). Wired **opt-in** into the gtnh-factory-flow adapter: `to_input_ir(plan, physical=...)`
  stamps a known machine's real footprint on the `InputIR`, while the default path stays single-block
  so the solver runs with or without a dump. No IR contract change (additive keyword-only argument).
- **Automated dataset-update CI (`.github/workflows/update-dataset.yml`, lane 4)** - a
  weekly + manual workflow that tracks the latest *stable* GTNH pack: it resolves the pack
  version from the DreamAssemblerXXL manifests, diffs the pinned mod versions against
  `gtnh.lock.json` (exiting green with no PR when unchanged), and on a change bumps the
  extractor pins, runs the headless Forge dump, installs the dataset, re-locks, runs the
  full test suite, and opens a reviewable PR whose summary surfaces the controller-count
  delta, added/removed/changed multiblocks, and the extractor failure list. Never
  auto-merges. Backed by a typed, tested CI helper (`tools/dataset_ci/`) and a dataset-diff
  review checklist (`.github/PULL_REQUEST_TEMPLATE/dataset-update.md`).
- **Dataset extractor scaffold (`tools/gtnh-extractor/`, the repo's only Java)** - lane 1 of
  the automated multiblock-dataset pipeline (GitHub #44). A standalone GTNH
  `ExampleMod1.7.10`-based Gradle tool whose `DumperMod` (`@Mod` entrypoint) hooks
  `FMLServerStartedEvent`, runs an empty dump body, and calls `FMLCommonHandler.exitJava`
  (0 on success, nonzero on failure) so `./gradlew runServer` boots a headless dedicated
  1.7.10 server with GT5-Unofficial + StructureLib and exits as a pass/fail gate. GT5U and
  StructureLib are pinned in `dependencies.gradle` from the current stable pack manifest
  (2.8.4) and mirrored in the new repo-root `gtnh.lock.json`; the rest of GT5U's hard deps
  resolve transitively from its Nexus POM. The Python solver gains no dependency on the tool
  (it will read only the JSON the tool emits; the dump loop itself is lane 2). `NOTICE` now
  credits the two LGPL mods.
- Project scaffold: docs, package skeleton, CI, license.
- Design and architecture documentation ported from the office-hours design doc
  and the engineering review (see `docs/`).
- **IR contracts (`ir/`)** - the two versioned Pydantic v2 schemas everything couples
  to: `InputIR` (the problem) and `LayoutResult` (the solution), with shared cell-grid
  geometry and enums. The input IR enforces referential integrity; geometric/rule checks
  are left to the validator. Full test suite (example + hypothesis). `docs/IR.md` updated
  to match the implemented shape.
- **Validator (`validator/`)** - the automated correctness gate: `validate(problem, layout)`
  independently checks a layout's geometry + structure (machines in-bounds / non-overlapping /
  off reserved cells / legally oriented / fully placed; nets routed once, contiguous,
  in-bounds, ME-toggles honored; pinned I/O on-route; power thickness well-formed) and
  returns a `ValidationReport` of every proven violation - never raises, never passes a
  silently-invalid layout. Rule-data checks (tier caps, summed amperage, face reachability)
  are stubbed for the dataset lane. In-code golden corpus (one known-bad case per violation).
- **Placement (`placement/`) - Phase 1 crude placer** - `place(problem)` does deterministic
  first-fit constructive placement on the cell grid (floor layer first, honoring reserved
  cells and never overlapping; orientation = first legal option), returning a
  `PlacementResult` that is either every machine placed or an explicit `Infeasibility` naming
  what did not fit (never raises). The validator independently certifies the output. Shared
  cell-grid helpers (`occupied_cells`, `in_region`) lifted into `ir/geometry.py`. Property
  test proves the core promise: any input yields a valid placement or an explicit
  infeasibility. SA/LNS placement is Phase 2 (see `docs/ROADMAP.md`).
- **Adapter (`adapter/`)** - `adapt_file(path)` / `to_input_ir(plan)` map a gtnh-factory-flow
  exported plan JSON to `InputIR`: nodes -> machines (recipe I/O -> item/fluid ports, computed
  typed throughput), storages -> boundary **Super Chest** (items) / **Super Tank** (fluids)
  machines (blocks that take I/O covers, so covers ride machine/storage faces, never pipes),
  edges -> nets. Typed view of the consumed export shape (`plan.py`, tolerant of extra fields).
  Two real exports committed as fixtures in `examples/` (sand, nitrobenzene). Crude for Phase 1:
  single-block footprints, default orientations, power nets not synthesized yet.
- **Router (`router/`) - Phase 1 crude router** - `route(problem, placements)` resolves a
  `Terminal` per net endpoint on a usable (non-front) machine face, then A* between terminals
  over the free cell grid, returning routes or an explicit `Infeasibility`. The sand demo line
  now goes **export -> place -> route -> validator.ok**, the whole Phase 1 slice end to end.
  Crude: one channel, no capacity, item/fluid only. Added `Route.terminals: list[Terminal]` to
  the output schema (additive); the validator gained the route<->endpoint **reachability check**
  (terminal on a non-front face adjacent to its machine and on the route). Machine `orientation`
  is now constrained to horizontal facings (GT machines never face up/down).
- **Build guide (`buildguide/`)** - `build_guide(problem, layout)` renders a `LayoutResult` as
  a human-readable text guide: header, bill of materials (machines by type, pipe/cable cells
  per commodity, I/O cover count), per-net connections (resource + machine faces), and a
  per-layer ASCII map with a key. The cheap, visible Phase 1 payoff - a player can read and
  build the sand line from it - ahead of the three.js previewer.
- **Solver (`solver/`) + auto-output** - `solve(problem)` composes the pipeline: place (now in
  **flow order** - a topological sort so producers land next to consumers) -> assign
  **auto-output connections** (a source machine ejecting straight into an adjacent target's
  input face: no pipe, no cover, GT's free connection - one auto-output per machine) -> route
  pipes only for what auto-output can't cover -> assemble. The sand line now solves to a flat
  row of 4 machines auto-feeding each other: **zero pipes, zero covers**. Added
  `LayoutResult.auto_connections: list[AutoConnection]` (additive); a net is satisfied by a
  `Route` XOR an `AutoConnection`, and the validator checks auto-connection adjacency / faces /
  single-auto-output-per-machine. Power nets are still not synthesized (the export has no power
  source) - a shared-amperage power model with optimized source count/placement is next.

- **Contributor standards & tooling** - documented coding + Conventional-Commits
  conventions in `CONTRIBUTING.md`; added a `.pre-commit-config.yaml` (ruff lint + format,
  `mypy --strict`, file hygiene, commit-msg lint), a PR template, and bug/feature issue
  templates.

- **Power (shared-amperage net) - synthesis + routing.** The export carries each machine's
  `eut` + voltage tier but no power source, so the adapter now synthesizes the power network:
  one synthetic source machine + one shared-amperage power net per voltage tier feeding the
  powered machines (`adapter/power.py`). The new power router (`router/power.py`) routes each
  per-tier net as a cable trunk and sizes every segment to the **summed amperage of the machines
  downstream of it** (1x/2x/4x/8x/16x), rejecting a load over the 16x cap as an explicit
  infeasibility - correctness-first single-source-per-tier (multi-source / voltage-loss
  optimization is Phase 2). `solve()` runs it alongside the item/fluid router, and placement no
  longer lets a power source split an auto-feeding material chain. The build guide gains a
  **Power** section telling the builder where to feed external power (synthetic sources are not
  self-powered). Backing it: a new `dataset` voltage ladder + `amperage` helper, `Machine.eut`
  (additive, InputIR v1), and shared router grid/dock/A* primitives lifted into `router/_grid.py`
  (the generic router no longer touches power). See `docs/DOMAIN.md`, `docs/ARCHITECTURE.md` #8.

- **CLI (`gtnh-solve`)** - the first real Phase 1 entry point: `gtnh-solve <export.json>` loads +
  adapts the export, solves (place -> auto-output -> item/fluid + power route -> self-validate),
  and prints the build guide (`-o FILE` to write it, `--seed` to pick the seed). Exit code 0 when
  the layout is fully VALID, 1 when the solver returns an explicit infeasibility (printed to
  stderr), 2 when the export can't be loaded. Replaces the planning-stub entry point.

- **Placement optimizer (`placement/search.py`) - Phase 2 simulated annealing.**
  `optimize_placement(problem, *, seed)` seeds from the constructive first-fit placer and
  improves a **routing-aware cost** (per-net half-perimeter wirelength + compactness + flat-build
  bias) with relocate / swap / **reorient** moves (orientation is a search variable), Metropolis
  acceptance, geometric cooling, best-valid-so-far. `solve()` now uses it (the crude placer stays
  as the SA seed + a fallback). Every accepted state stays validator-clean; deterministic per
  seed. Connected machines cluster - a hub+4-spoke star drops from HPWL 10 (first-fit row) to 5
  (annealed cluster), and sand stays all-auto-output. LNS + the place<->route feedback loop are
  next (docs/ROADMAP.md lane C).

- **Placement LNS (`placement/search.py`) - large-neighbourhood ruin-and-recreate.** The optimizer
  gains a large move alongside relocate / swap / reorient: rip out a *related* (net-connected)
  cluster of machines and greedily re-insert each at the position + orientation that minimises the
  cost, biased toward cells beside its already-placed net-neighbours. One step reshapes a whole
  cluster, escaping local optima the single-cell moves plateau in. It is probability-gated inside
  the same annealing loop, so Metropolis acceptance / cooling / best-so-far and per-seed
  determinism are unchanged, and every candidate stays validator-clean (recreate validity-checks
  each insertion and abandons the move if a machine cannot be re-placed). Insertions are ranked by a
  cheap marginal cost (the machine's own nets + auto pairs + a flat-build bias), not a full recompute,
  so LNS fits the same budget as the small moves. Finishes the SA + LNS half of ROADMAP lane C.
  Because the cost is still HPWL-driven, tighter clustering can push a route onto a second layer (the
  sand demo's power cable now rises one layer, still valid) - the future congestion-aware cost
  (lane C) is what removes that. (`placement/`.)

- **Solver "optimize or not" toggle (`solve(..., optimize=...)`, `gtnh-solve --fast`).** `solve`
  gains an `optimize` flag. The default (`True`) runs the annealed placer (SA + LNS) inside the
  place<->route feedback loop; `False` takes a near-instant single constructive placement, with no
  annealing and no re-placement. Both validate their output (VALID / explicit partial /
  infeasibility, never silently invalid) - fast just trades the optimizer's clustering and
  unrouted-net recovery for speed. `--fast` exposes it on the CLI. This is the user-facing control
  the planned unified site is built around, and the home for LNS (opt-in behind the optimized
  path). (`solver/`, `cli/`.)

- **Previewer (`previewer/`) + `gtnh-solve --preview`** - a self-contained, double-clickable 3D
  view of a solved layout. `build_scene(problem, layout)` flattens the layout into a render-ready
  scene (machine boxes coloured by type with the machine name on the front face, rectangular
  cables/pipes sized by cable thickness with a lead to each machine face, auto-output arrows,
  legend, and a tight `bounds` of the built extent) - a pure, fully-tested mapping; `render_html`
  inlines it into a static three.js viewer (CDN, no npm build) with an **orbit + pan camera**
  (right-drag / arrow keys) and a **layer-by-layer slider**, framed on the built extent rather
  than the solver's oversized search region. `gtnh-solve plan.json --preview view.html` writes it.
  Build-assist scope; the congestion heatmap, multi-seed compare, real block textures, and offline
  (vendored three.js) are follow-ups.

- **Routing capacity invariant (lane D, first slice).** Routes are now laid **capacity-aware**:
  each laid route's cells become obstacles for the routes after it - across item/fluid (`route`)
  and power (`route_power` gains an `extra_obstacles` arg the solver feeds the item cells into) -
  so no cell ever carries two routes (the crude single-channel cap: one route per cell). The
  validator independently enforces it (`route_cell_collision`), closing the gap where item pipes
  and power cables could share a cell and the abstraction would certify an unbuildable layout
  (docs/ARCHITECTURE.md #7). Crude for now: one channel per cell; the per-edge multi-channel cap
  (a routing margin hosting several parallel channels) is a later lane-D slice.

- **Rip-up/reroute (lane D, second slice).** Capacity makes routing order-dependent - a net that
  grabs a scarce cell can wedge a later net out (a *false* infeasibility, not a real one). The
  item/fluid router now routes a pass, and if any net failed, rips everything up and retries with
  the failed nets moved to the front (most-constrained-first), stopping when a pass is clean or a
  failed-net set repeats (a genuine infeasibility, not an ordering accident). So a bad net order is
  no longer mistaken for unroutable. Crude failed-first reordering; negotiated-congestion routing
  (the gold-standard, order-independent approach) is tracked as a follow-up (GitHub #7).

- **Build guide is buildable from alone.** The text guide was a sketch; it now carries the detail a
  player needs to build the line without guessing: a **Placement** table (each machine's exact
  `(x, y, z)` cell, front face, and footprint), per-pipe-terminal **covers** (conveyor for items,
  pump for fluids, in input/output mode - docs/DOMAIN.md), the exact **cells** each pipe/cable runs
  along, and **per-segment cable thickness** for power. The Power note now states the amperage to
  feed each source (its trunk-root thickness) instead of pointing at the ASCII map that never
  showed it.

- **Place↔route feedback loop in `solve()`** (docs/ARCHITECTURE.md #1, #6). `solve()` no longer
  takes a single placement on faith: it assembles an attempt (place → auto-output → route →
  validate), and if the router leaves nets unrouted it **penalizes exactly those nets** so the next
  placement pulls their machines tighter (shorter routes, or adjacency that auto-outputs) and
  re-places with the next seed. It keeps the best layout seen and returns the first fully-VALID one
  (anytime: best-so-far), stopping early when re-placing cannot help - a non-routing defect, or the
  same nets failing again. A layout a single attempt leaves `partial_invalid` (one net it could not
  pipe in a congested placement) now solves VALID. Deterministic (bounded attempts keyed off `seed`
  + the accumulated penalties, no wall-clock). The routers gained `failed_nets` (which nets stalled)
  and `optimize_placement` a `net_penalties` weight to carry the signal. Crude feedback (penalize +
  re-seed); a richer incremental routing estimate inside the SA move is future work.

- **Build guide states the boundary + a real power-feed spec (GitHub #15).** Two gaps that made
  the "buildable from alone" guide actually need guesswork are closed, both from data already in the
  IR. A new **System inputs / outputs** section names what to load each boundary input storage with
  (resource + typed rate, e.g. `load Super Chest at (0, 0, 0) with minecraft:stone (~0.1 items/t)`)
  and where each finished product exits with nothing collecting it (`minecraft:sand exits Forge
  Hammer at (3, 0, 0) - place a Super Chest/Tank to collect it`) - boundary storages that only
  source, and machine output ports no net consumes. And the **Power** note now reads as a wiring
  spec - `feed LV (32 V), >=4 A -> up to 128 EU/t` (tier voltage from the `dataset` ladder × the
  trunk-root amperage) - instead of the bare cable thickness (`4x amperage`) it printed before.

- **Previewer shows the system's inputs, outputs, and power (GitHub #5).** The 3D preview now
  surfaces the same boundary the text guide does: a **System I/O** panel in the HUD lists the
  inputs to load (resource + rate), the products to collect, the total EU/t draw, and the summed
  **amperage per voltage tier** (the tier already implies the volts, so amps is the useful number,
  e.g. `LV 3A`). A toggle switches every rate between **per tick and per second**. Both surfaces
  read from one new shared, fully-tested helper - `system_io(problem, layout)`
  (`gtnh_solver/system_io.py`) - so the guide and previewer can never disagree on what crosses the
  line's edge; `build_scene` emits it as `scene.io` and the build guide was refactored onto the
  same helper (its text output is unchanged).

- **Boundary output rates in the previewer (GitHub #16).** A finished product exits a machine
  output port that no net consumes, so its rate lived nowhere - the previewer showed the product
  with no throughput. The adapter now records each port's rate from the recipe on a new additive
  `Port.rate` (items/t or mB/t, InputIR v2, no version bump), and `system_io` reads it, so the HUD
  shows e.g. `out: minecraft:sand (0.1 items/t)`. Input rates are unchanged (still the net's typed
  throughput).

- **The adapter closes the line: output buffers (GitHub #16).** A system output used to exit a
  machine into thin air (only inputs got a boundary storage), so a line was never fully collectible
  without hand-editing. The adapter now synthesizes a **Super Chest/Tank + net per unconsumed
  output** (a machine OUTPUT port no net sources), placed and wired at the port's recorded rate, so
  the product is gathered automatically - the sand line now auto-outputs its sand into a collection
  chest (still zero pipes). `system_io` reports a boundary storage that only *sinks* as a system
  output (mirroring the only-*sources* input), and the build guide reads `minecraft:sand collected
  by Super Chest at (x, y, z) (~0.1 items/t)`. Sand grows from 5 machines to 6, nitrobenzene from
  21 to 23.

- **Power sources reserve a boundary feed face.** A synthesized power source is fed by the builder
  from outside the structure, but nothing said *where* - it was placed like any machine, so the
  optimizer could bury it mid-region with no face left for the external feed. Its **front face is
  now the reserved feed entry**: constructive and SA/LNS placement pin that face flush on the
  region boundary (every move preserves it; a problem with no such slot is an explicit
  `power_feed` infeasibility), and the validator enforces the same rule independently (new
  `POWER_FEED_NOT_ON_BOUNDARY`). Internal cables keep using the other five faces - the existing
  front-face rule already keeps them off the feed face. New shared helpers:
  `Machine.is_power_source` (the buildguide's private predicate, promoted) and
  `ir.geometry.front_on_boundary`.

- **The optimizer now finds compact, low-wire layouts (the hand-built sand target).** Two
  coordinated changes (docs/ROADMAP.md lane C). The placement cost is **footprint-first**: the
  compactness driver is now the floor area (x-span times z-span, weight 1.0), so stacking a layer
  is free while sprawling costs, with the bounding-box volume kept as a mild tiebreak; power nets
  lost their base wirelength term entirely (center-distance proxies cannot see dock faces or
  shared cable taps and measurably steered AWAY from low-cable layouts) and instead gain an MST
  trunk-length pull only when feedback-penalized, to rescue a power net the router failed. The
  real cable cost is judged where it is knowable: the solver's **feedback loop is now
  quality-driven** - every bounded attempt is fully routed + validated and the best VALID layout
  by (structure footprint, power cable cells, structure volume) wins, instead of returning the
  first valid one. Optimized sand now solves to a 5x1x2 stack - the machine row with the source
  on top and a **3-cell cable trunk** tapped through the hammers' top faces - matching the
  maintainer's hand-built 3-cable solution with a smaller footprint (5 vs 6) and volume (10 vs
  12). Acceptance is pinned by a solver test.

- **Selectable compactness objective** - `solve(..., objective="footprint" | "volume" |
  "balanced")` and `gtnh-solve --objective`. "Compact" is ambiguous and the two metrics pull
  opposite ways (stacking a layer shrinks the floor but can grow the enclosing box), so the
  builder picks: `footprint` (default, the maintainer's target) minimizes floor area and stacks
  tall, `volume` minimizes the enclosing box and stays flat/cubic, `balanced` weighs both. The
  objective drives both the placement cost's compactness weights and the feedback loop's quality
  ranking of routed layouts; the fast path ignores it (constructive placement is floor-first by
  construction). This is the future unified site's second user control, next to optimize-or-not.
  Sand passes the hand-built compactness + <= 3-cable budget under every objective.

### Changed
- **Previewer wire->machine leads take the connecting cable's thickness (GitHub #6).** Each route
  terminal in the scene now carries the thickness of the fattest route segment incident to its
  cell (a mid-trunk tap touches several; the fattest is what visually meets the block), and the
  viewer sizes the short lead from the cable into the docked machine face with the same
  thickness->cross-section ramp as the trunk segments - so a 4x run meets its machine visibly fat
  and a 1x tap thin. Item/fluid terminals carry `null` and keep their fixed-size pipe leads.
  Previewer-internal (scene + viewer template): an additive scene field the template reads with a
  fallback, so no scene-version bump. (`previewer/`.)
- **The item/fluid router negotiates congestion instead of retrying orders (GitHub #7).** Laying
  nets sequentially (each net's cells hard-blocking the next) made the result hostage to net
  order; the failed-first reorder retry only reduced that. The router now runs the FPGA
  PathFinder scheme: every net routes independently with priced A* (a contested cell costs a
  present-sharing penalty per other user plus a history penalty that grows every round it stays
  contested), and all nets re-route round by round until no cell is shared - so an
  ordering-induced false infeasibility cannot happen, and what remains contested after the round
  budget is reported per net as an explicit `congestion` infeasibility (a maximal collision-free
  subset is still emitted for the feedback loop). Once the contested set stops changing, a
  geometric proof (a bottleneck cell that two nets both cannot route around) ends the negotiation
  early instead of grinding the whole round budget, so a genuine single-bottleneck congestion is
  rejected in a few rounds rather than 32; the proof only ever bails on a demonstrated collision,
  so a resolvable contention is never misreported. Power trunks keep the
  failed-first rip-up/reroute (trees grown by multi-goal A* do not decompose into per-cell
  pricing). (`router/core.py`, `router/_grid.py`.)
- **The test gate runs in a quarter of the time (GitHub #74).** Profiling showed ~3/4 of every
  CI test leg was coverage tracer overhead, not test work (the solver's hot loops execute
  millions of traced line events). The suite now runs parallel by default (`pytest-xdist`,
  `-n auto` in addopts - the local gate drops ~155s to ~60s), CI gates coverage on ONE matrix
  leg instead of every leg, and that leg uses coverage's `sys.monitoring` core
  (`COVERAGE_CORE=sysmon`, branch-capable on 3.14+). No test dropped; the 90% gate and the
  required `test` status check are unchanged.
- **CI tests Python 3.14; packaging metadata reflects real support.** The test matrix now runs
  the floor and the latest release only (`3.10` + `3.14`; a floor break or a new-release break
  is what a leg catches, and the 3.11-3.13 intermediates cannot fail while both ends pass), and
  the package gains per-version trove classifiers
  (`Programming Language :: Python :: 3.10` through `3.14`) and moves from
  `Development Status :: 1 - Planning` to `3 - Alpha`. Internal CI/build polish along with it:
  pip caching, least-privilege `permissions`, cancel-superseded-runs `concurrency`, a
  `hatchling>=1.26` build pin, and a Dependabot config (GitHub Actions + pip, weekly).
- **The router now owns the auto-output vs pipe decision.** `route()` decides itself, from the
  final placements + orientations, which nets GT's free auto-output connection covers (the logic
  moved from `solver/core.py` to `router/auto.py`, public `assign_auto_outputs`) and lays pipes
  only for the rest; `RouteResult` gains `auto_connections` so the decision rides the router's
  output, and the solver's assemble step just composes it (its `skip_nets` plumbing is gone).
  Behavior is unchanged - same greedy net order, one auto-output per source machine, only
  1-source-1-sink item/fluid nets are eligible, power/ME never auto-feed - and the validator's
  independent auto-output checks stay the gate. This advances lane D (docs/ROADMAP.md): the
  router is the geometry authority, so the optimizer's job shrinks to moving blocks and choosing
  front faces. (`router/`, `solver/`.)
- **Power trunks grow as trees with shared taps.** The power router chained every net
  source -> m0 -> m1 -> ... as a path and docked each terminal on its own distinct cell, so a
  source + N sinks always cost at least N+1 cable cells - geometrically unable to reach the
  hand-built 3-cable sand trunk. In GT one cable block feeds every adjacent wired machine face,
  so the trunk is now a tree: a sink whose dock candidate is already a trunk cell of its net
  taps it (terminal on that cell, no new cable; the cell nearest the source wins), and any other
  sink extends the tree with a multi-goal A* leg from every trunk cell laid so far. Sizing
  follows the tree - each machine's cable distance is its terminal's depth, and every segment
  carries the summed amperage of the sink terminals on its far-from-root side (replacing the
  per-leg suffix sum, which overcharged one side of a branch) - and the validator already
  re-derives branched trees and shared terminal cells independently. A source + three clustered
  sinks now trunk with two cable cells, within the sand target's three. (`router/power.py`.)
- **Power cables dock route-aware, on whichever face is nearest the trunk.** The power router
  (`router/power.py`) used to commit each terminal to the first free non-front face in a fixed
  order (south first), blind to where the cable then had to run, so a source behind a machine row
  made the trunk snake around it. It now considers every usable (non-front) face and docks via a
  multi-goal A* leg on the one that gives the shortest cable (new `_grid.dock_candidates` +
  `astar_multi`), the source docking toward its first sink. On the sand demo this drops the
  optimized power run from nine cables to five (matching the constructive baseline); every terminal
  is still validated (non-front, adjacent, on-route) and the trunk stays a single tree.
- **Optimized placement minimises total volume, with no separate per-layer penalty.** The
  routing-aware cost (`placement/search.py`) dropped its `layer count` term (and the matching
  flat-build bias in the LNS recreate ranking): the bounding-box **volume** term already accounts
  for height, so the optimizer now trades layers against footprint purely by which yields the
  smaller box. Only the optimized (SA/LNS) path uses this cost; the fast constructive path is
  unaffected.
- **Power sizing now models cable voltage loss over distance.** GT cables lose voltage per block,
  so a machine `d` blocks from the source receives `tier_voltage - loss·d`, not the full tier. The
  source stays at the machine's tier and the cable is thickened to compensate: each machine's
  amperage is sized at its *delivered* voltage (`ceil(eut / (tier_voltage - loss·d))`), so a
  machine farther out draws more amps, and a run whose voltage drops to 0 is reported infeasible
  (`voltage_drop`). Loss is a flat 1 EU/block for every tier for now (per-material loss is Phase 2).
  The power router (`router/power.py`) accumulates each machine's cable distance while building the
  trunk and sizes from it; the validator independently re-derives the distance from the cable tree
  and re-checks (new `power_voltage_drop_excessive` violation); the boundary summary
  (`system_io.py`, feeding the previewer and build guide) reports the loss-inclusive amperage the
  builder must supply. Backing it: `dataset` gains `CABLE_LOSS_PER_BLOCK`, `delivered_voltage`, an
  `UnpowerableError`, and a `distance=` argument on `amperage`. This makes the emitted line
  actually buildable: a too-long low-voltage cable is no longer certified as valid. See
  `docs/DOMAIN.md`, `docs/ARCHITECTURE.md` #8.
- **Previewer power HUD shows the feed spec with correct values.** The system-i/o panel showed
  power as `48 EU/t (LV 3A)`, where the 48 is the machines' sub-tier draw (16 x 3) and the tier
  breakdown omitted the voltage - easy to mis-supply in game. It now shows the input the way a GT
  source is fed: a total EU/t supplied plus the per-tier **full tier voltage x amps**
  (`power: 96 EU/t (LV 32V x 3A)`, where 96 = 32 V x 3 A, so the total matches the breakdown). The
  scene's `io.power.byTier` entries gain a per-tier `volts` and `total` is the summed feed (scene
  version 1). (`previewer/`.)
- **InputIR bumped to v2 (breaking): dropped `Port.is_auto_output`.** It was a dead, contradictory
  field - the adapter never set it and the solver auto-connects any adjacent output regardless of
  it. Whether a port is satisfied by auto-output is a **solver decision**, not a problem input: it
  is recorded in the output's `AutoConnection`, and the "one auto-output per machine, items-xor-
  fluids, never power" rule is enforced there by the validator (`duplicate_auto_output` /
  `auto_output_illegal_commodity`), not on the input contract. `FaceSpec`'s now-moot auto-output
  validation is removed with it. (`ir/`.)
- **InputIR bumped to v1 (breaking): dropped `Machine.count`.** Multi-instance machine groups
  are not modelled until routing is instance-aware (Phase 2): the placer expanded `count` into
  N placements sharing one machine id, but a `MachineFaceRef` cannot address a specific
  instance, so the router/solver/validator collapsed the copies via `setdefault` and left the
  extras silently unwired. Each `Machine` is now exactly one instance; the adapter rejects an
  export `machineCount > 1` with an explicit `AdapterError` instead of emitting an under-wired
  layout. (`ir/`, `adapter/`, `placement/`, `validator/`.)
- **CI expanded** to a single static-checks job (via pre-commit), a Python 3.10-3.13 test
  matrix with a coverage gate (`--cov-fail-under=90`), and an advisory (non-blocking)
  Conventional-Commits check on PRs. Ruff now runs a curated lint rule set plus
  `ruff format`; the Pydantic mypy plugin is enabled. (`pyproject.toml`,
  `.github/workflows/ci.yml`.)
- Input foundation switched from a forked gtnh-flow (Python) to consuming
  gtnh-factory-flow's MIT, Zod-validated exported plan JSON. The adapter now parses
  that documented export (no vendoring); recipes/throughput/machine-IDs come from its
  dataset, so the hand-authored physical dataset shrinks. Removed the `vendor/`
  placeholder in favor of `examples/` for sample exported plans.
- Depend on a maintained fork of gtnh-factory-flow (fix only the consumed
  export/throughput/dataset path) and snapshot a known-good dataset + sample exports
  as fixtures so the solver is decoupled from the fork's health.

### Removed
- Dropped the unused `networkx` and `numpy` core runtime dependencies - neither was
  imported anywhere in the implementation. They will be re-added if and when the Phase 2
  optimizer/graph work actually needs them (see `docs/ROADMAP.md`).

### Fixed
- **Dark casing tints no longer bake to near-black in the previewer (`previewer/bake.py`).** The
  Pillow bake turned a GT layer tint into per-channel multipliers with a raw `value / 255`, so a
  dark-neutral casing tint like bronze's `[32, 32, 32]` collapsed to `~0.125` and multiplied the
  already-full-colour tier sprite down to mean RGB around 20 (a Basic Forge Hammer baked
  effectively black). The tint is now normalised by its brightest channel instead: identical to
  `/ 255` for any tint whose peak channel is 255 (the electric `[210, 220, 255]` majority and plain
  whites are byte-unchanged), but a dark-neutral tint becomes identity, so the sprite shows through
  at full brightness with its hue shift preserved. A regression test pins that a `[32, 32, 32]` tint
  keeps a bright sprite bright, and the existing golden tint guards move to the new hue-shifted
  values. This is a readability-first approximation; GT-pixel-accurate casing colour stays a
  deferred cosmetic item.
- **The cable-thickness ladder gains GT's 12x rung** (maintainer-reported). GT ships six cable
  sizes (1x/2x/4x/8x/12x/16x) but the dataset only knew five, so any segment or feed summing to
  9 through 12 amps was sized a whole rung thick (16x). The router now picks 12x for that band,
  the output contract and validator accept it, and the docs spell the full ladder.
- **Power router does failed-first rip-up/reroute, like the item router (GitHub #40).** The power
  router laid each tier's trunk in problem order and stopped at the first net that could not route,
  reporting only that one - but capacity accretes obstacles, so a trunk laid for one tier can wedge
  a later tier's trunk out of a chokepoint: a *false* infeasibility from net order alone, and a
  weaker feedback-loop signal than the item router already gave for pipes. It now routes a pass
  and, if any net failed, rips every trunk up and retries with the failed nets first (most-
  constrained-first), stopping only when a pass is clean or a failed-net set repeats (a genuine
  infeasibility, not an ordering accident). When routing does stall it reports ALL still-failing
  nets, not just the first, so the place↔route feedback loop can penalize them all. The bounded-
  retry loop is now shared with the item router (`core._rip_up_reroute`). (`router/power.py`,
  `router/core.py`.)
- **Validator derives power amperage independently of the router (GitHub #36).** The validator is
  meant to be a second, differently-written implementation so a bug in the router's power math is
  caught, not certified (docs/ARCHITECTURE.md #4) - but its amperage re-check still called the same
  `dataset.amp_load` / `whole_amps` helpers the router sizes cables with, so a bug in the loss
  formula or the ceil-with-epsilon rounding would have been blessed by both sides. It now inlines
  its own arithmetic (`eut / (tier_voltage - loss * distance)` per machine, summed per segment,
  `ceil` with the shared epsilon), importing only the rule DATA (the voltage ladder,
  `CABLE_LOSS_PER_BLOCK`, `_AMP_EPSILON`) so the rounding policy stays identical and the two still
  agree on every valid layout, while a sizing bug is now caught on a separate code path. Separately,
  an unknown/off-ladder voltage tier was reported as `power_thickness_insufficient` (whose meaning
  is "cable thinner than the summed amps") - a wrong signal for a route that is merely unverifiable;
  it now gets its own additive `power_tier_unknown` violation code. (`validator/`.)
- **User-facing output surfaces are hardened against bad input and bad paths (GitHub #39).** The
  previewer inlined the scene JSON into its `<script>` block unescaped, so a machine type or
  resource id containing `</script>` (plan JSON is external input) could close the tag and break or
  inject into the page; the inline JSON now escapes `</` to `<\/` (JSON-transparent, the scene still
  round-trips). The CLI's `-o`/`--preview` writes raised an uncaught `OSError` on an unwritable path,
  dumping a raw traceback instead of honoring the documented 0/1/2 exit-code contract; both writes
  now report `error: could not write <path>: <reason>` to stderr and exit 2. (`previewer/`, `cli`.)
- **Amperage is sized from fractional machine loads, rounded up per aggregate - not per machine**
  (maintainer-verified in game). GT machines pull whole packets (1 amp = one packet of up to tier
  voltage) into an internal buffer only when it has room, so a 16 EU/t LV machine *averages* 0.5
  amps - but `dataset.amperage` ceiled every machine to whole amps and the callers summed the
  ceilings, overstating every aggregate: the optimized sand line's feed spec read 3 A / 96 EU/t
  when 2 A / 64 EU/t runs it in game, and cables could come out a tier thicker than needed.
  `amperage` is replaced by `amp_load` (the un-rounded `eut / delivered_voltage`, same
  unknown-tier / unpowerable errors) plus `whole_amps` (the ceil, with epsilon slack for float
  dust), and the rounding moves to where packets are actually quantized: per cable segment in the
  router and validator, per tier in `system_io` (so the guide and previewer both now say
  2 A / 64 EU/t for sand; this supersedes the interim 3 A number from the drift fix below). Cable
  loss still raises far machines' loads; the 16x cap and unpowerable checks are unchanged.
  (`dataset/`, `router/power.py`, `validator/`, `system_io`.)
- **Build guide power note agrees with the previewer (and reality).** The note read the feed
  amperage off the trunk's thickest cable segment, which both understates a trunk whose sink taps
  the source's own dock cell (its amps flow through no segment - on the optimized sand stack the
  guide said `>=2 A -> up to 64 EU/t` while the previewer said 3 A / 96 EU/t) and overstates when
  amps round up to a cable tier (the fast sand row printed 4 A for a 3 A draw). Both surfaces now
  read the same shared `system_io` numbers: the tier's machine draws summed at each machine's
  delivered voltage. Per-segment cable thickness still lives under Connections. (`buildguide/`.)
- **Validator requires a consumer on routed nets (GitHub #8).** The gate enforced the
  OUTPUT->INPUT port direction on the auto-connection path but not on the routed-pipe path, so a
  routed net with no consumer (every endpoint an OUTPUT producer) passed - the golden "valid"
  fixture even normalized one. A routed net now needs at least one INPUT endpoint
  (`route_net_no_consumer`), while still allowing multiple same-commodity producers feeding one
  pipe (GT lets several machines eject into one line). The routed path also independently checks
  every endpoint carries the net's own commodity (`route_net_mixed_commodity`), so a mixed-
  commodity net is caught even if a producer bypasses the input IR's own check. The base test
  fixture now wires a real consumer.
- **Previewer floor grid aligns to cell boundaries (GitHub #19).** The grid lines landed on
  integer boundaries on one axis but cut through the middle of the blocks on the other (a
  `GridHelper` centering artifact: integer line offsets need an even division count, half-integer
  offsets an odd one, so parity decided it per axis). The grid now uses an even span snapped to an
  integer center, so every line sits on a cell edge and the blocks read as sitting in their cells.
- **Previewer draws auto-output direction on the machine faces (GitHub #20).** The cyan auto-output
  arrow ran center-to-center between the two adjacent machines, so it was buried inside their opaque
  boxes and you could not tell which machine fed which. It is now a small flat arrow on each source
  face perpendicular to the ejecting direction (the two side faces plus top and bottom), each
  pointing the way the machine ejects, so at least one stays visible from any angle however tightly
  the machines are packed.
- **Previewer renders routes GT-style (GitHub #31).** Cables and pipes were flat bars spanning
  cell-center to cell-center plus a separate fixed lead to each machine face, which did not read like
  an in-game pipe/cable. Every route (item, fluid, power) is now a small cube at each cell centre
  with a uniform cross-section arm out to the block edge for each connection (an adjacent route cell,
  or a docked machine face), power sized by cable thickness. One node per cell keeps a run readable
  however tightly the routes are packed.
- **Validator route + auto-connection soundness holes** - the only automated correctness gate
  was certifying some geometrically-impossible layouts. Routes are now checked for unit-step
  segments (a single segment can no longer "teleport" two cells across a machine - connectivity
  alone missed it), and no route cell may sit inside a machine body or on a reserved cell.
  Auto-connections are now checked against the net they claim to satisfy: the connection must
  join that net's real OUTPUT->INPUT endpoint machines (resolved by port direction), `net_id`
  must resolve, and power/ME-routed commodities cannot be auto-output. New violation codes
  (`route_segment_not_unit`, `route_through_machine`, `route_on_reserved`,
  `auto_output_wrong_endpoints`, `auto_output_illegal_commodity`) with one negative test each.
- **`solve()` now validates its own output.** It previously returned `valid` whenever
  placement and routing each reported success, without ever running the independent validator -
  so the "never returns a silently-invalid layout" promise was not enforced end to end. `solve`
  now runs `validate()` on the assembled layout and downgrades a `valid` result to
  `partial_invalid` (carrying the violation) if anything is proven wrong.
- **Validator enforces summed-amperage power sizing** (previously deferred). It independently
  re-derives each power cable's load - rooting the cable tree at its source terminal and summing
  the draw of the machines downstream of every segment - and flags a segment whose cable is
  thinner than its load (`power_thickness_insufficient`), which also catches a load over the 16x
  cap. So a power-sizing bug in the router is caught, not certified.
- **Validator no longer blesses an uncertifiable power route.** The amperage check used to
  *skip* (certify by silence) a power route it could not verify - one with zero or multiple
  source terminals, or whose cables form a cycle/tangle instead of a single tree - the exact
  silently-invalid case the independent gate exists to catch. It now rejects both with explicit
  violations (`power_net_no_single_source`, `power_route_not_a_tree`), each with a negative test.
- **Power router always builds a tree.** `router/power` A*'d each leg of a trunk against
  obstacles that excluded the cable already laid, so legs could overlap into a non-tree whose
  per-segment amperage is undefined. Each laid leg's cells are now obstacles for the legs that
  follow, so the trunk is always a single non-overlapping path the validator can verify.
- **Placement optimizer keeps auto-output (orientation-aware cost).** The SA cost was
  orientation-independent, so `reorient` moves were a free random walk that could finalize an
  orientation putting a machine's front (no-I/O) face on a connecting side and **blocking**
  auto-output. The cost now rewards orientations that enable auto-output (a shared
  `ir.geometry.auto_output_faces` helper, reused by the solver), so the optimizer preserves -
  and recovers - the free connections instead of degrading them.
- **Adapter sizes power for `parallel`.** A node's `eut` is now `recipe.eut * parallel`: a node
  running N recipes in parallel draws N times the power, matching how throughput already scales,
  so the synthesized power cable is sized correctly for `parallel > 1` (was under-sized).
- **Validator checks terminals belong to their net.** `_check_terminals` only verified that every
  net endpoint *had* a terminal; a route could still carry a **foreign** terminal (a machine/port
  that is not one of the net's endpoints) or two terminals for one endpoint and pass. It now flags
  both (`terminal_not_an_endpoint`, `duplicate_terminal`), closing the structural half of
  required-I/O-face reachability so the gate cannot certify a route with bogus docks.

[Unreleased]: https://github.com/MrBruh/gtnh-process-line-solver/commits/main
