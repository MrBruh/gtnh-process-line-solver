# CLAUDE.md - agent guide for gtnh_solver

This file orients AI agents (and people) working in this repo. Read it first, then the
doc it points to for whatever you're touching.

## What this is

A **physical place-and-route solver for GregTech: New Horizons** process lines. It turns a
balanced logical graph (from a gtnh-factory-flow exported plan) plus a GT physical-rules dataset into a concrete,
buildable 3D layout (machine positions + pipe/wire routes), with a previewer and build
guide. It is NOT a recipe/ratio calculator - that problem is already solved by gtnh-factory-flow,
which we consume. See [`docs/DESIGN.md`](docs/DESIGN.md).

**Source of truth for HOW it's built:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
It records the data flow and the engineering-review decisions. If code and that doc
disagree, the doc is the intent - fix one of them and say which.

## Architecture map (where things live)

| Module | Responsibility |
|--------|----------------|
| `src/gtnh_solver/ir/` | Typed, versioned input IR + output-layout schemas (the contracts) |
| `src/gtnh_solver/adapter/` | gtnh-factory-flow exported plan JSON → IR |
| `src/gtnh_solver/dataset/` | Physical-rules data + loader (footprints, faces, tiers, ME) |
| `src/gtnh_solver/placement/` | SA/LNS placement with routing-aware cost |
| `src/gtnh_solver/router/` | Free-form per-commodity A* routing + shared-amperage power |
| `src/gtnh_solver/solver/` | place↔route feedback loop, anytime budget |
| `src/gtnh_solver/validator/` | Independent geometric + rule checks (the safety net) |
| `src/gtnh_solver/buildguide/` | Bill of materials, per-layer build instructions |
| `src/gtnh_solver/previewer/` | three.js previewer + output-layout JSON emit |
| `src/gtnh_solver/cli.py` | `gtnh-solve` entry point |
| `examples/` | Sample gtnh-factory-flow exported plans for adapter/solver tests |

## Conventions

- **Python ≥ 3.10**, `src/` layout, typed throughout (`mypy --strict`). Format/lint with
  `ruff`.
- **The IR is a contract.** Both `ir/` schemas are versioned; never break a consumer
  silently. See [`docs/IR.md`](docs/IR.md).
- **Explicit over clever; DRY; engineered-enough.** Match the surrounding code.
- **ASCII diagrams** in module docstrings for non-obvious pipelines (placement loop,
  router phases). Keep them current when you change the code near them.

## Testing

- Framework: **pytest**, plus **hypothesis** for property tests.
- Run: `pytest`  ·  lint: `ruff check .`  ·  types: `mypy`.
- **Tests ship with the code**, not as a follow-up. 100% path coverage is the target.
- The validator is the only *automated* correctness gate (there is no headless GT
  simulator). Property tests must prove: any input → a valid layout OR an explicit
  infeasibility report, never a silently-invalid one. A small golden corpus of known-good /
  known-bad layouts lives in `tests/golden/`. See [`docs/TESTING.md`](docs/TESTING.md).

## Domain gotchas (easy to get wrong)

- **Litematica does NOT run on 1.7.10.** GT:NH is Minecraft 1.7.10. The in-game schematic
  consumer is **Schematica-Plus** (classic `.schematic`, numeric block IDs). Don't target
  Litematica/`litemapy`. (Export is a post-v1 milestone anyway.)
- **Power is a shared-amperage net**, not a per-pipe flow: voltage tier follows the machine
  voltage; amperage *sums* on shared cable segments and sets thickness (1x/2x/4x/8x/12x/16x,
  16x max → parallel runs or higher voltage). See [`docs/DOMAIN.md`](docs/DOMAIN.md).
- **Machine faces:** the front face carries no I/O; the other five can input or output
  (covers make a specific pull/push explicit); a single auto-output face carries items OR
  fluids, not both. Required-I/O-face reachability is a HARD constraint.
- **Coarse-cell abstraction can lie.** Routing models channels-per-edge AND cell→block
  realizability, or it will certify layouts that don't physically fit.

## Skill routing (gstack)

When a request matches a skill, invoke it via the Skill tool. When in doubt, invoke it.
- Product/brainstorm → `/office-hours` · Architecture/plan → `/plan-eng-review`
- Full review pipeline → `/autoplan` · Bugs → `/investigate` · QA → `/qa`
- Code review → `/review` · Ship/PR → `/ship` · Backlog-ready spec → `/spec`
