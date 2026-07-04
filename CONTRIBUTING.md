# Contributing to gtnh_solver

Thanks for helping build a place-and-route solver for GT:NH. Phase 1 has shipped a crude but
end-to-end pipeline (adapter through solver, validator, previewer, and build guide); the work
now is Phase 2 quality, organized as the **Build lanes** below.

## Setup

Requires **Python 3.10+** (`pyproject.toml` pins `requires-python = ">=3.10"`). If your default
`python` is older (3.8 is a common system default), `pip install` fails opaquely, so create the
venv with an explicit 3.10+ interpreter - e.g. `py -3.12` on Windows.

```bash
git clone <repo>
cd gtnh-process-line-solver
python -m venv .venv && . .venv/bin/activate   # Windows: py -3.12 -m venv .venv; .venv\Scripts\activate
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

## Build lanes

Phase 1 shipped a crude end-to-end slice; Phase 2 is the quality work, fanned out into parallel
lanes (from [`docs/ROADMAP.md`](docs/ROADMAP.md)). Each lane is an *upgrade* on a piece Phase 1
already built, so most have already landed a first slice. The lanes are formally gated behind
Phase 1's in-game Assignment (does the pinned demo line build and run in GT:NH), so read status
as "safe to start on" rather than "unblocked".

| Lane | Phase 2 work | Status |
|------|--------------|--------|
| A | adapter hardening: pin the plan-schema + recipe-dataset version | Open - Phase 1 adapter done; pinning not started |
| B | full physical dataset (footprints / faces / tiers / cell->block) | Open - no per-machine entries exist yet; `dataset/` holds only the voltage ladder + amp helpers, and footprints/faces are hardcoded adapter defaults |
| C | placement: SA/LNS + routing-aware cost | Largely landed - SA + LNS + the routing-aware (HPWL / compactness / auto-output) cost are in; the incremental congestion-aware cost is the remaining refinement |
| D | router: negotiated-congestion, multi-channel cap, shared-amperage power optimization | In progress - rip-up/reroute, single-channel capacity, and size-or-reject power landed |
| E | validator rule-half: tier caps, summed amperage, face reachability | Partly landed - summed-amperage + voltage-drop validation shipped independently; only the tier caps + dataset-specific face rules stay blocked on lane B |
| F | previewer / build-guide polish | Previewer polish in progress; build-guide polish deferred |

Pick a lane, comment on (or open) an issue to claim it, and ship one logical change per PR.

## Issues, branches, and PRs

Bug reports and feature requests live as **GitHub issues** (use the templates).

- Picking up an existing issue? Comment to claim it so two people don't build the same thing,
  then reference it from your PR with a closing keyword (`Closes #123`) so it closes on merge.
- No issue behind the change? That's fine - branch, change, and open a PR describing what and why.

One logical change per branch/PR, whether or not there's an issue behind it.

## Rules of the road

- **Tests ship with the code.** Every branch and user flow gets a test in the same PR.
  Property tests (hypothesis) and the golden corpus are the safety net - see
  [`docs/TESTING.md`](docs/TESTING.md).
- **Respect the IR contract.** It's versioned; don't break consumers silently.
- **Explicit over clever, DRY, engineered-enough.** Small, clean diffs.
- **Keep ASCII diagrams in docstrings current** when you change nearby code.
- **Input is gtnh-factory-flow's exported plan JSON** (MIT data, not vendored code). Validate
  against its schema and pin a recipe-dataset version; see `docs/ARCHITECTURE.md`.

## Coding standards

Enforced by CI and the pre-commit hooks - none of this is hand-policed:

- **Python ≥ 3.10**, `src/` layout, **fully typed** under `mypy --strict` (the Pydantic
  mypy plugin checks the IR models). No `# type: ignore` without a reason comment.
- **Formatting is `ruff format`** (line length 100). Let the tool fix formatting automatically.
- **Lint is `ruff check`** with a curated rule set (pycodestyle, pyflakes, isort, naming,
  pyupgrade, bugbear, comprehensions, simplify, pytest-style, ruff). The selected rules
  live in `pyproject.toml` (`[tool.ruff.lint]`) - that list *is* the contract for "what
  lint means here". Line length (E501) is intentionally left to the formatter.
- **Tests ship with the code** and **coverage is gated** (CI: `--cov-fail-under=90`; the
  standing target is 100% path coverage). Property tests (hypothesis) + the golden corpus
  are the safety net - see [`docs/TESTING.md`](docs/TESTING.md).
- **Docstrings carry the design.** Keep the ASCII diagrams current when you change nearby
  code; explain *why*, not just *what*.

## Commit messages

This repo uses **[Conventional Commits](https://www.conventionalcommits.org/)** - checked
locally by the `commit-msg` hook and in CI on PRs. Format:

```
<type>(<scope>): <summary>
```

- **type** - one of `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`,
  `chore`, `revert`. A breaking change adds `!` (e.g. `feat(ir)!: …`) and a
  `BREAKING CHANGE:` footer.
- **scope** *(optional but encouraged)* - the module/lane it touches: `ir`, `adapter`,
  `dataset`, `placement`, `router`, `solver`, `validator`, `buildguide`, `previewer`,
  `cli`, plus `ci`/`docs` for those.
- **summary** - imperative, lower-case, no trailing period.

Examples (from this repo's history):

```
feat(validator): independent geometric + structural correctness gate
feat(ir): implement InputIR + LayoutResult contracts
chore(ci): bump actions/checkout to v5 and setup-python to v6
```

User-visible changes also get a `CHANGELOG.md` entry under `[Unreleased]`.

## Branch naming

Branch off `main`, one logical change per branch: `<type>/<short-desc>` (the type matches the
commit type) - e.g. `feat/router-astar`, `docs/tidy-readme`. If the branch resolves an issue,
prefix the description with its number: `fix/17-validator-overlap`.

## Pull requests

- One logical change per PR; fill in the PR template.
- If the PR resolves an issue, link it in the description with a closing keyword (`Closes #123`,
  `Fixes #123`) so the issue closes automatically on merge. No issue is fine too.
- Green CI is required: the `static` job (ruff lint + format + `mypy`) and the `test` matrix
  (pytest on the floor and latest Python; coverage is gated once, on the latest leg). The Conventional-Commits check is **advisory** -
  it flags non-conforming messages in the logs but does not block the merge; the local
  `commit-msg` hook is the real nudge.
- Describe what changed and which doc/decision (or issue) it implements.

## License

By contributing you agree your contributions are licensed under Apache-2.0 (see
[`LICENSE`](LICENSE)).
