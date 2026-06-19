# Contributing to gtnh_solver

Thanks for helping build a place-and-route solver for GT:NH. This project is in the
planning/pre-alpha stage: the design is reviewed and the skeleton is here, but most modules
are stubs.

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
  (pytest + coverage on Python 3.10-3.13). The Conventional-Commits check is **advisory** -
  it flags non-conforming messages in the logs but does not block the merge; the local
  `commit-msg` hook is the real nudge.
- Describe what changed and which doc/decision (or issue) it implements.

## License

By contributing you agree your contributions are licensed under Apache-2.0 (see
[`LICENSE`](LICENSE)).
