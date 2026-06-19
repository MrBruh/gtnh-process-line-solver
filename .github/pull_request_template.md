## What & why

<!-- What does this change do, and what problem does it solve? -->

## Which doc / decision does it implement?

<!-- Link the intent: a docs/ARCHITECTURE.md decision (#N), docs/IR.md, a docs/ROADMAP.md
     lane, or the issue this closes. "Code disagrees with the doc" PRs should say which side
     is being corrected. -->

## Lane

<!-- The module(s) this touches: ir / adapter / dataset / placement / router / solver /
     validator / buildguide / previewer / cli / docs / ci -->

## Checklist

- [ ] Tests ship with the code (every new branch + user flow) and `pytest` is green
- [ ] `pre-commit run --all-files` passes (ruff lint + format, `mypy --strict`)
- [ ] Coverage holds (CI gate: `--cov-fail-under=90`; the project target is 100% path coverage)
- [ ] The IR contract is respected - no silent break; if a schema changed, the version is
      bumped and the change is recorded in `ir/__init__.py` + `CHANGELOG.md`
- [ ] ASCII diagrams in any touched module docstrings are still accurate
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] Commits + PR title follow Conventional Commits (e.g. `feat(router): …`)
