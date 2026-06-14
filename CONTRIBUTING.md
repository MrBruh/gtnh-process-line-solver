# Contributing to gtnh_solver

Thanks for helping build a place-and-route solver for GT:NH. This project is in the
planning/pre-alpha stage: the design is reviewed and the skeleton is here, but most modules
are stubs. That makes it a great time to claim a piece and build it.

## Setup

```bash
git clone <repo>
cd gtnh-process-line-solver
git submodule update --init   # pulls the vendored gtnh-flow fork (once it's wired up)
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest          # run tests
ruff check .    # lint
mypy            # type-check
```

## Before you start

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (how it fits together and why) and
[`docs/DOMAIN.md`](docs/DOMAIN.md) (the GT:NH rules you'll need). The [`CLAUDE.md`](CLAUDE.md)
file is a fast orientation for both agents and people.

## Pick a lane

The architecture is built so most work is independent once the **IR contract** (`ir/`)
lands. Lanes that can proceed in parallel:

| Lane | Module(s) | Good first work |
|------|-----------|-----------------|
| **IR** (do first — unblocks all) | `ir/` | Typed input IR + output-layout schemas |
| Adapter | `adapter/`, `vendor/gtnh-flow/` | Fork gtnh-flow; emit IR JSON at one point |
| Dataset | `dataset/` | Author the starter machine set + loader |
| Placement | `placement/` | SA/LNS + routing-aware cost |
| Router | `router/` | Per-commodity A*; then the power primitive |
| Validator | `validator/` | Independent geometric + rule checks |
| Previewer | `previewer/` | three.js render of the output-layout JSON |

The solver loop (`solver/`) merges placement + router, so coordinate the
placement↔router interface before wiring it. Full sequencing and the v1/v1.1 split are in
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## Rules of the road

- **Tests ship with the code.** Every branch and user flow gets a test in the same PR.
  Property tests (hypothesis) and the golden corpus are the safety net — see
  [`docs/TESTING.md`](docs/TESTING.md).
- **Respect the IR contract.** It's versioned; don't break consumers silently.
- **Explicit over clever, DRY, engineered-enough.** Small, clean diffs.
- **Keep ASCII diagrams in docstrings current** when you change nearby code.
- **The vendored `gtnh-flow` is a fork.** Keep its MIT notice; mark any files you change.

## Pull requests

- Branch off `main`, keep PRs scoped to one lane where possible.
- Green CI (ruff + mypy + pytest) is required.
- Describe what changed and which doc/decision it implements.

## License

By contributing you agree your contributions are licensed under Apache-2.0 (see
[`LICENSE`](LICENSE)).
