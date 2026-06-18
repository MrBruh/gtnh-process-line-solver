# Contributing to gtnh_solver

Thanks for helping build a place-and-route solver for GT:NH. This project is in the
planning/pre-alpha stage: the design is reviewed and the skeleton is here, but most modules
are stubs. That makes it a great time to claim a piece and build it.

## Setup

```bash
git clone <repo>
cd gtnh-process-line-solver
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install   # wire the git hooks (lint, format, types, commit-msg)
pytest               # run tests (with coverage)
ruff check .         # lint
ruff format .        # auto-format
mypy                 # type-check
```

`pre-commit install` is the one-time step that makes your local commits run the same
checks CI does. To run them all on demand: `pre-commit run --all-files`.

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
| Adapter | `adapter/` | Parse gtnh-factory-flow's exported plan JSON into the IR |
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
- **Input is gtnh-factory-flow's exported plan JSON** (MIT data, not vendored code). Validate
  against its schema and pin a recipe-dataset version; see `docs/ARCHITECTURE.md`.

## Coding standards

Enforced by CI and the pre-commit hooks — none of this is hand-policed:

- **Python ≥ 3.10**, `src/` layout, **fully typed** under `mypy --strict` (the Pydantic
  mypy plugin checks the IR models). No `# type: ignore` without a reason comment.
- **Formatting is `ruff format`** (line length 100). Don't hand-format; let the tool.
- **Lint is `ruff check`** with a curated rule set (pycodestyle, pyflakes, isort, naming,
  pyupgrade, bugbear, comprehensions, simplify, pytest-style, ruff). The selected rules
  live in `pyproject.toml` (`[tool.ruff.lint]`) — that list *is* the contract for "what
  lint means here". Line length (E501) is intentionally left to the formatter.
- **Tests ship with the code** and **coverage is gated** (CI: `--cov-fail-under=90`; the
  standing target is 100% path coverage). Property tests (hypothesis) + the golden corpus
  are the safety net — see [`docs/TESTING.md`](docs/TESTING.md).
- **Docstrings carry the design.** Keep the ASCII diagrams current when you change nearby
  code; explain *why*, not just *what*.

## Commit messages

This repo uses **[Conventional Commits](https://www.conventionalcommits.org/)** — checked
locally by the `commit-msg` hook and in CI on PRs. Format:

```
<type>(<scope>): <summary>
```

- **type** — one of `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`,
  `chore`, `revert`. A breaking change adds `!` (e.g. `feat(ir)!: …`) and a
  `BREAKING CHANGE:` footer.
- **scope** *(optional but encouraged)* — the module/lane it touches: `ir`, `adapter`,
  `dataset`, `placement`, `router`, `solver`, `validator`, `buildguide`, `previewer`,
  `cli`, plus `ci`/`docs` for those.
- **summary** — imperative, lower-case, no trailing period.

Examples (from this repo's history):

```
feat(validator): independent geometric + structural correctness gate
feat(ir): implement InputIR + LayoutResult contracts
chore(ci): bump actions/checkout to v5 and setup-python to v6
```

User-visible changes also get a `CHANGELOG.md` entry under `[Unreleased]`.

## Branch naming

Branch off `main`, one lane per branch where possible: `<type>/<short-desc>`, optionally
scoped — e.g. `feat/router-astar`, `fix/validator-overlap`, `docs/ir-contract`.

## Pull requests

- Keep PRs scoped to one lane; fill in the PR template.
- Green CI is required: the `static` job (ruff lint + format + `mypy`) and the `test` matrix
  (pytest + coverage on Python 3.10–3.13). The Conventional-Commits check is **advisory** —
  it flags non-conforming messages in the logs but does not block the merge; the local
  `commit-msg` hook is the real nudge.
- Describe what changed and which doc/decision it implements.

## License

By contributing you agree your contributions are licensed under Apache-2.0 (see
[`LICENSE`](LICENSE)).
