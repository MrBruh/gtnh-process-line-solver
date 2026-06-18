# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project scaffold: docs, package skeleton, CI, license.
- Design and architecture documentation ported from the office-hours design doc
  and the engineering review (see `docs/`).
- **IR contracts (`ir/`)** — the two versioned Pydantic v2 schemas everything couples
  to: `InputIR` (the problem) and `LayoutResult` (the solution), with shared cell-grid
  geometry and enums. The input IR enforces referential integrity; geometric/rule checks
  are left to the validator. Full test suite (example + hypothesis). `docs/IR.md` updated
  to match the implemented shape.
- **Validator (`validator/`)** — the automated correctness gate: `validate(problem, layout)`
  independently checks a layout's geometry + structure (machines in-bounds / non-overlapping /
  off reserved cells / legally oriented / fully placed; nets routed once, contiguous,
  in-bounds, ME-toggles honored; pinned I/O on-route; power thickness well-formed) and
  returns a `ValidationReport` of every proven violation — never raises, never passes a
  silently-invalid layout. Rule-data checks (tier caps, summed amperage, face reachability)
  are stubbed for the dataset lane. In-code golden corpus (one known-bad case per violation).

- **Contributor standards & tooling** — documented coding + Conventional-Commits
  conventions in `CONTRIBUTING.md`; added a `.pre-commit-config.yaml` (ruff lint + format,
  `mypy --strict`, file hygiene, commit-msg lint), a PR template, and bug/feature issue
  templates.

### Changed
- **CI expanded** to a single static-checks job (via pre-commit), a Python 3.10–3.13 test
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

[Unreleased]: https://github.com/MrBruh/gtnh-process-line-solver/commits/main
