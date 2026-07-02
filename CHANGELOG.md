# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

### Changed
- **Power cables dock route-aware, on whichever face is nearest the trunk.** The power router
  (`router/power.py`) used to commit each terminal to the first free non-front face in a fixed
  order (south first), blind to where the cable then had to run, so a source behind a machine row
  made the trunk snake around it. It now considers every usable (non-front) face and docks via a
  multi-goal A* leg on the one that gives the shortest cable (new `_grid.dock_candidates` +
  `astar_multi`), the source docking toward its first sink. On the sand demo this drops the
  optimized power run from nine cables to five (matching the constructive baseline); every terminal
  is still validated (non-front, adjacent, on-route) and the trunk stays a single tree.
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
