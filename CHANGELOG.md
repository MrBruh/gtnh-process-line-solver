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

### Changed
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

[Unreleased]: https://github.com/MrBruh/gtnh-process-line-solver/commits/main
