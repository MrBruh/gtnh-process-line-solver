# Testing

Goal: **100% path coverage, tests shipped with the code.** Framework: `pytest` + `hypothesis`.

## The core constraint: no headless GT simulator

True correctness of a layout (does the line actually run?) is only verifiable in-game. So
automated tests can only prove **self-consistency with the encoded rules**, not real-world
correctness. The strategy works around this with three layers:

1. **Independent validator.** The validator shares rule *data* with the router but has
   separately-written checking *logic*, so it can catch router bugs (a shared code path
   couldn't). Present today: geometric + structural validity (no overlaps, within bounds,
   pinned I/O honored, unit-step contiguous routes, single-channel capacity, terminal /
   required-I/O-face reachability, ME-toggled commodities excluded from routing) and the
   shared-amperage **power** rules (summed amperage <= cable thickness, single-source cable
   tree, voltage-drop over distance). Deferred to the dataset lane: throughput/tier caps,
   one-fluid-per-line, the dataset-specific half of face rules, and ME-endpoint *placement*
   (a toggled commodity is only skipped today, not yet endpoint-placed).

2. **Property tests (hypothesis).** The safety net against the worst failure class. For any
   generated input graph, the solver must return **a valid layout OR an explicit
   infeasibility report - never a silently-invalid layout.** This is the one invariant that
   must always hold. It lives in `tests/test_solver_properties.py`, over generated `InputIR`s
   on both the annealed and the `--fast` path, alongside the fuzz that holds `validate` to its
   never-raises contract (the downgrade that makes the invariant true calls it). **Read the
   outcome mix, not just the exit code:** the tests `event()` their status, so
   `--hypothesis-show-statistics` says whether the generated space still reaches valid,
   partial-invalid *and* infeasible layouts. A change that quietly made everything infeasible
   would leave the suite green and the promise unproven.

3. **Golden corpus** (`tests/golden/`). A small set of **known-good** layouts the validator
   must accept and **known-bad** ones it must reject - the only real-world ground-truth proxy
   in v1. Start hand-authored (3-5 good + a few bad); the v1.1 round-trip importer grows this
   from real community builds.

Plus a **manual in-game spot-check** of the starter dataset (tiers, face rules, throughputs)
during the Assignment - v1's only contact with actual GT behavior.

## What to test per module

- **adapter** - correct parsing of gtnh-factory-flow's exported plan JSON; missing/changed
  fields and plan-schema/dataset version mismatch handled, not silently dropped.
- **dataset** - entries load + validate; unknown machine / bad footprint raises clearly.
- **placement** - move operators (translate + orientation), each cost term, per-seed
  determinism, won't-fit infeasibility.
- **router** - A* per net, throughput/tier caps, one-fluid-per-line, EU-loss cost + amperage
  cap, channels-per-edge invariant, cell→block realizability, rip-up-and-reroute, ME-toggle
  skip + endpoint placement, unroutable → infeasibility.
- **solver** - the place→route→retry loop converges or gives up with a report; anytime budget
  returns best-valid-so-far.
- **validator** - geometric + rule checks; partial-invalid layouts reported, never passed.
- **cli** - parse an export, solve, print the build guide (and, with `--preview`, write the 3D
  preview HTML), honor `--fast` / `--seed` / `--objective`, surface infeasibility via exit code.

## Edge cases that must have tests

- Region too small to fit machines → infeasibility names the shortfall.
- A net that can't route within its tier → tightest violated constraint + suggested
  relaxation.
- A machine whose distinct I/O commodities exceed its five usable faces → flagged.
- Empty / single-machine line; the largest line the solver is expected to handle.

## CI sees a smaller dataset than you do

**Assert on the dataset the run actually resolved, never on the one your machine has.** Generated
dumps are local and version-namespaced (`data/<version>/`, gitignored); a fresh checkout and every
CI job carry only the committed fixtures, which are two multiblocks (Electric Blast Furnace, Vacuum
Freezer) and an example-scoped texture manifest. `resolve_dataset_path` silently falls back to
those, so a test written against a full local dump passes for its author and fails in CI.

This is not hypothetical: `test_cli_solves_nitrobenzene` asserted `exit 0` for weeks. With real
footprints the line solves valid; with fixtures alone every machine falls back to 1x1x1, and its HV
Distillation Tower needs 7 connections against the 5 usable faces a single block has, so the honest
answer is exit 1 with a `face_reachability` infeasibility. Nothing caught it because the branch was
not pushed until long after it was written.

Three ways out, in order of preference:

- **Pass the fixture directory explicitly** (`load_physical_dataset(_DATA_DIR)`), so the test asserts
  one known configuration and means the same thing everywhere. Most dataset tests do this.
- **Branch on what resolved**, when both configurations are real properties worth pinning - see
  `tests/test_cli.py::_line_resolves_multiblocks`. Prefer this to a `0 or 1` disjunction, which
  passes in every configuration and therefore asserts nothing.
- **Skip** when the full dump is absent, if the property genuinely cannot be expressed on fixtures.

The same applies to the texture manifest: the committed one is scoped to the example lines' machines
and has no hatch, cable or pipe entries at all.

## Not auto-testable (manual / in-game)

- Whether a layout actually runs in GT:NH - covered by the in-game Assignment, not CI.
- Previewer visual correctness - smoke-test the render path; eyeball the rest.

## How much of your machine a run takes

`addopts` runs the suite under `-n auto` (it is CPU-bound and every test is independent), but
`auto` means *every* core, which pins the box for the whole run. `tests/conftest.py` bounds that
with two dials, both disabled when `CI` is set so the GitHub runner still gets all of itself:

| Env var | Default | Effect |
|---|---|---|
| `GTNH_TEST_CPU_FRACTION` | `0.8` | `-n auto` uses `floor(fraction * cores)` workers, floor 1 |
| `GTNH_TEST_NICE` | on | every process drops to a below-normal scheduler priority |
| `GTNH_TEST_HYPOTHESIS_FRACTION` | `0.25` | share of each property test's `max_examples` a local run takes |

An explicit `-n 4` overrides the first (the hook only fires for `auto`/`logical`), as does xdist's
own `PYTEST_XDIST_AUTO_NUM_WORKERS`.

**The cap is not a speed tradeoff.** On a 4-core box at `--no-cov` the suite runs 56s on `-n 4`,
54s on `-n 3` and 58s on `-n 2`: the last worker oversubscribes the cores the controller needs, so
it buys nothing. Reach for `GTNH_TEST_CPU_FRACTION=1.0` only on a machine you are not using.

**`solve()` is the suite.** A probe over a serial run puts 53.0s of 73.5s (72%) inside `solve()`
across 592 calls, against 0.10s in `adapt_file` - parsing an export is free, annealing a layout is
not. Two things dominate, and each has a lever below: the hypothesis property tests (500 generated
solves) and the shipped example lines. A nitrobenzene solve is ~5.6s against sand's ~0.6s.

### Reuse a shipped-line solve; do not re-run one

`solved_sand` and `solved_nitrobenzene` (in `tests/conftest.py`) adapt and solve each shipped line
**once per session** and hand every test a private deep copy. Take them whenever a test needs *a*
real layout to render, measure or validate:

```python
def test_something(solved_nitrobenzene: tuple[InputIR, LayoutResult]) -> None:
    problem, layout = solved_nitrobenzene
```

The copy is what makes the sharing safe - `InputIR` and `LayoutResult` are `StrictModel`, so they
are mutable, and a session-scoped object one test edits is a failure the *next* test reports. It
costs ~0.4ms against a ~570ms solve, so it is free.

Two cases must still call `solve` themselves, and both are about the act of solving rather than
its output:

- **Determinism tests**, which need two independent solves to compare.
- **Anything passing a different configuration.** The fixtures are `adapt_file(path)` with *no*
  physical dataset; `adapt_file(path, physical=...)`, `optimize=False`, a non-default `objective`
  or a pinned `seed` are all different problems, and the dataset one especially (see the section
  above - the resolved dataset differs between your machine and CI).

### Property-test budgets are reduced locally, full in CI

`property_examples(full)` in `tests/_helpers.py` scales each `max_examples`: the full budget
whenever `CI` is set, a quarter of it locally (200/50/300 becomes 50/12/75). Every PR is still
held to the full space; only local iteration is cheaper. The ratio between the three budgets is
preserved, because they are not interchangeable - the largest one fuzzes `validate`, not `solve`.

Run `GTNH_TEST_HYPOTHESIS_FRACTION=1.0 pytest` before pushing a change to the solver or the
validator, and read the outcome mix with `--hypothesis-show-statistics` as the section above says:
a reduced budget reaches a smaller slice of the generated space, so a local green is weaker
evidence than a CI green.

**Coverage is a 3x multiplier**, and `addopts` enables it: serial, the suite is 73s at `--no-cov`
and 282s with `--cov`. Pass `--no-cov` while iterating; `COVERAGE_CORE=sysmon` does not help,
because `sys.monitoring` cannot measure branches before Python 3.14 and coverage silently falls
back to its tracer.

## Commands

```bash
pytest                    # all tests
pytest --no-cov           # ~3x faster; coverage is on by default via addopts
pytest -q tests/golden    # the corpus
ruff check .              # lint
mypy                      # types

# the full property-test space, as CI runs it - before pushing solver/validator work
GTNH_TEST_HYPOTHESIS_FRACTION=1.0 pytest --hypothesis-show-statistics
```
