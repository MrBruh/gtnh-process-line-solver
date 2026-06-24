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

### Changed
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

[Unreleased]: https://github.com/MrBruh/gtnh-process-line-solver/commits/main
