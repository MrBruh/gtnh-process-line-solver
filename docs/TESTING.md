# Testing

Goal: **100% path coverage, tests shipped with the code.** Framework: `pytest` + `hypothesis`, on
**Python 3.14 only**: it is the one version CI runs, so run the suite from a `.venv` built with a
3.14 interpreter (`py -3.14 -m venv .venv` on Windows) and no other.

## The suite tests correctness, not layout quality

Every solve in the suite is a **minimal** one (`solve(effort="minimal")`, `gtnh-solve --effort
minimal`): one attempt instead of eight, an anneal capped at 250 iterations, and the router's
negotiation capped at 8 rounds instead of 32. Every stage still runs (anneal, route, negotiate,
salvage, power, hatches, validate), so a minimal solve proves the code works end to end and that
its answer is valid or explicitly infeasible. What it does not do is look hard for a *good* layout,
which is what a full solve spends its time on: a full nitrobenzene solve is ~18 s of CPU, a minimal
one ~1.7 s. `tests/conftest.py::_minimal_solves` sets this for the whole session by pointing
`solver.core.DEFAULT_EFFORT` at `minimal`, the same way the dataset pin works below, so a test (or
a CLI run inside one) that names no effort gets the minimal one.

Layout **quality** is a separate tier, `full_solve`. Its tests hold the search to a bar (the
hand-built sand targets, the parallel line reaching VALID, the repair pass finding the 3-cable
trunk), solve at full effort, and are **skipped unless the run passes `--full-solve`**:

```bash
pytest --full-solve -m full_solve   # the quality tier alone
```

Run it when benchmarking, or when a change seems to have cost layout quality it should not have.
**A small drop is expected, not a failure**: as more of GT's rules land and more components are
placed so that a line runs as it does in game, the search has less room, and a layout can get a
little bigger. Nothing in CI runs this tier.

A tier of tests answers "is it still good enough"; whether a change made layouts **better** takes
many seeds of real lines, paired. `tools/bench_layouts.py` runs `gtnh-solve` on plans x spaced
seeds under any checkout's code, records each layout's floor, route cells and the solver's own
ranking key, and `--compare A B` pairs two such runs seed by seed into wins, ties, losses and VALID
flips. That table is the go/no-go for a placer, router or solver change:

```bash
python tools/bench_layouts.py examples/gtnh-parallel-sand.json --root ../main --out bench/main
python tools/bench_layouts.py examples/gtnh-parallel-sand.json --out bench/branch
python tools/bench_layouts.py --compare bench/main bench/branch
```

Two kinds of test sit between the tiers, and the rule for each:

- **A test of the multi-start itself** (the ranking of attempts, the process pool, determinism
  across ties) passes `effort="full"` explicitly and stays in the everyday run, since one attempt
  has nothing to rank and a pool is never started for it. They run on small lines, so they are
  cheap.
- **A test that uses a session solve fixture** (`solved_sand`, `solved_nitrobenzene`) gets a
  minimal layout. A `full_solve` test must solve for itself, because those fixtures were built
  before its marker could apply.

## The core constraint: no headless GT simulator

True correctness of a layout (does the line actually run?) is only verifiable in-game. So
automated tests can only prove **self-consistency with the encoded rules**, not real-world
correctness. The strategy works around this with three layers:

1. **Independent validator.** The validator shares rule *data* with the router but has
   separately-written checking *logic*, so it can catch router bugs (a shared code path
   couldn't). Present today: geometric + structural validity (no overlaps, within bounds,
   pinned I/O honored, unit-step contiguous routes, single-channel capacity, terminal /
   required-I/O-face reachability, nets on ME excluded from routing), the
   shared-amperage **power** rules (summed amperage <= cable thickness, single-source cable
   tree, voltage-drop over distance), and **item pipe throughput** (each pipe block makes the
   insertions the streams through it need, #190; its golden cases are the maintainer's
   parallel-sand export, refused, and his working build, accepted). Deferred to the dataset lane:
   fluid throughput, tier caps, one-fluid-per-line, the dataset-specific half of face rules, and
   ME-endpoint *placement* (a toggled commodity is only skipped today, not yet endpoint-placed).

2. **Property tests (hypothesis).** The safety net against the worst failure class. For any
   generated input graph, the solver must return **a valid layout OR an explicit
   infeasibility report - never a silently-invalid layout.** This is the one invariant that
   must always hold. It lives in `tests/test_solver_properties.py`, over generated `InputIR`s
   on both the annealed and the `--fast` path, alongside the fuzz that holds `validate` to its
   never-raises contract (the downgrade that makes the invariant true calls it), and the promise
   that the annealed path is VALID wherever `--fast` is (#132). **Read the
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
- **validator** - geometric + rule checks; partial-invalid layouts reported, never passed. The
  ME gate is held to hand-built builds (`tests/_me_fixtures.py`: an attached line, a comb of
  machines along one cable row, a multiblock with a GT ME hatch or a normal hatch), each valid as
  built and broken one rule at a time per violation code (`tests/test_validator_me.py`); its
  channel pathing is tested on its own against the hand trace in `docs/spikes/329-me-ae2.md`
  section 2.7 and on graphs with cycles, tied and not.
- **cli** - parse an export, solve, print the layout as JSON that round-trips through
  `LayoutResult` (valid and infeasible runs alike, and nothing on stdout but that JSON, whatever
  the run warns about), or with `--preview` / `--schematic` write the artifact and keep stdout
  empty; honor `--fast` / `--seed` / `--objective` / `--effort`, surface infeasibility via exit
  code.

## Edge cases that must have tests

- Region too small to fit machines → infeasibility names the shortfall.
- A net that can't route within its tier → tightest violated constraint + suggested
  relaxation.
- A machine whose distinct I/O commodities exceed its five usable faces → flagged.
- Empty / single-machine line; the largest line the solver is expected to handle.

## The suite sees the same dataset CI does, because it is pinned

**A test asserts against a stated dataset, never against the one your machine happens to hold.**
Generated dumps are local and version-namespaced (`data/<version>/`, gitignored); a fresh checkout
and every CI job carry only the committed data, which is two multiblocks (Electric Blast Furnace,
Vacuum Freezer) and an example-scoped texture manifest. In the CLI `resolve_dataset_path` prefers
the newest local dump and falls back to those, which is the documented convenience there
(`--dataset-version` is its escape hatch) - but under `pytest` it made the suite ask a different
question on every machine.

`tests/conftest.py::_pinned_dataset_root` removes that variable (#182): a session fixture copies the
committed sub-paths into a temp root and points `dataset.roots.DEFAULT_DATA` at it, so no version
folder exists to prefer and every unpinned resolution - CLI, previewer, schematic exporter,
`load_physical_dataset()` - lands on the committed data. Measured before the pin, same tree, one
variable: 954 passed / 7 skipped with no local dump, 955 passed / 6 skipped and twice the wall clock
with a 2.9 dump staged. CI, always a clean clone, only ever saw the first, which is how #176 (a
break that only appears at the newer pack) stayed invisible.

That the committed data is *small* still matters, and this is not hypothetical:
`test_cli_solves_nitrobenzene` asserted `exit 0` for weeks. With real footprints the line solves
valid; with the fixtures of the time (two unrelated controllers) every machine fell back to 1x1x1,
and its HV Distillation Tower needs 7 connections against the 5 usable faces a single block has, so
the honest answer was exit 1 with a `face_reachability` infeasibility. Nothing caught it because the
branch was not pushed until long after it was written. The committed data now carries that line's
controllers (`tools/derive_example_multiblocks.py`), so it solves in CI as well.

So, to test against anything other than the committed data, **state it**, in order of preference:

- **Pass the directory explicitly** (`load_physical_dataset(_DATA_DIR)`), so the test asserts one
  known configuration and means the same thing everywhere. Most dataset tests do this.
- **Stage the dataset the test is about** and point at it: write files under `tmp_path` and
  monkeypatch `DEFAULT_DATA` there (`tests/test_schematic.py`), or pass `data_dir=` directly
  (`tests/test_dataset_roots.py`, which is where `resolve_dataset_path`'s own newest-first mtime
  behaviour keeps its coverage).
- **Generate a local fixture from a dump, and skip without it,** when the property is about a pack
  only a real dump can speak for. Nothing a dumper produced is committed, not even a filtered slice
  of it, so such a fixture lives in `tests/fixtures/local/`, which is gitignored as a whole
  directory. The one there today is the 2.9 cable/pipe name list behind the rename guard in
  `tests/test_dataset_pipes.py` (#176). With a 2.9 dump staged under `data/<version>/`:

  ```sh
  python tools/derive_pipe_names.py data/<version>/textures/manifest.json
  ```

  That filters the manifest's `blocks` on `kind == "pipe"` and takes each `display_name`, with no
  solver code involved, and writes `tests/fixtures/local/manifest_pipe_names_2.9.json`. It refuses
  a pruned manifest, since a list taken from one would agree with the stand-in policy by
  construction.

  **The cost is real: CI has no dump, so CI never runs this check.** It runs only on a machine where
  someone has generated the list; everywhere else it is reported as skipped, with the command above
  as the reason. That is the same CI coverage the check had before the pin, when it read a staged
  dump directly. What the pin changes is only that the input is now stated (a named file with a
  named skip) rather than whatever `data/` holds, so this test and the acceptance module below are
  the only places the suite's result still depends on the machine: whether those files exist.

- **Read a whole local dump by explicit path, and skip without it,** when the property is "a real
  plan draws as itself", which no filtered slice can answer. `tests/test_acceptance_2_9.py` (#208)
  is the one such module: it adapts `examples/ev-nitrobenzene.json` against
  `data/2.9.0-beta-2/multiblocks` and `.../textures/manifest.json`, named directly rather than
  resolved, and checks that every machine expands to, and exports as, the controller its
  `block_key` names. Each missing half skips with the extractor run that makes it. The same cost
  applies: CI never runs it, so run the suite on a machine with the 2.9 dump staged before calling
  2.9 preview or export work done.

What no test may do is inherit whatever sits in `data/` on the machine running it: a green that
depends on local disk state is not evidence about the code.

The same applies to the texture manifest: the committed one is scoped to the example lines' machines
and has no hatch entries at all.

## Not auto-testable (manual / in-game)

- Whether a layout actually runs in GT:NH - covered by the in-game Assignment, not CI.
- Previewer visual correctness - smoke-test the render path; eyeball the rest.

## How much of your machine a run takes

`addopts` runs the suite under `-n auto` (it is CPU-bound and every test is independent), but
`auto` means *every* core, which pins the box for the whole run. `tests/conftest.py` bounds that
with two dials, both disabled when `CI` is set so the GitHub runner still gets all of itself:

| Env var | Default | Effect |
|---|---|---|
| `GTNH_TEST_CPU_FRACTION` | `1.0` | `-n auto` uses `floor(fraction * cores)` workers, floor 1 |
| `GTNH_TEST_NICE` | on | every process drops to a below-normal scheduler priority |
| `GTNH_TEST_HYPOTHESIS_FRACTION` | `0.25` | share of each property test's `max_examples` a local run takes |

An explicit `-n 4` overrides the first (the hook only fires for `auto`/`logical`), as does xdist's
own `PYTEST_XDIST_AUTO_NUM_WORKERS`.

A run takes every core by default, as `-n auto` always did; the fraction is there to hand cores
back when you want the machine while it runs. **It is close to free when you do.** On a 4-core box
at `--no-cov` the suite runs 56s on `-n 4`, 54s on `-n 3` and 58s on `-n 2`: the last worker
oversubscribes the cores the controller needs, so `GTNH_TEST_CPU_FRACTION=0.75` there costs
nothing at all.

The priority drop is the dial doing most of the work, which is why it is the one left on by
default: it costs no wall clock on an idle machine and still lets the foreground preempt the run.

**`solve()` is the suite.** A probe over a serial run once put 53.0s of 73.5s (72%) inside
`solve()` across 592 calls, against 0.10s in `adapt_file` - parsing an export is free, annealing
and routing a layout is not. That is why every solve is now minimal (above), and most of what a
minimal solve still costs is the router: on a placement it cannot route, each negotiation round
re-searches the contested nets, which is what the 8-round cap bounds. Two things dominate what is
left, and each has a lever below: the hypothesis property tests (500 generated solves) and the
shipped example lines. A minimal nitrobenzene solve is ~1.7s of CPU against sand's ~0.06s.

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

Before opening a PR on a change to the solver or the validator, run the property tests at the full
budget, `GTNH_TEST_HYPOTHESIS_FRACTION=1.0 pytest tests/test_solver_properties.py`, and read the
outcome mix with `--hypothesis-show-statistics` as the section above says: a reduced budget reaches
a smaller slice of the generated space, so a local green is weaker evidence than a CI green.

**Coverage is cheap on 3.14**, and `addopts` enables it. Coverage.py measures through
`sys.monitoring` there, branches included, which costs about 7% of a run (it cost 3x under the old
tracer, before the project required 3.14). Pass `--no-cov` anyway on a partial run, so that the
report does not print every module the run never imported as uncovered.

### Property tests have no per-example deadline

`tests/conftest.py` loads a Hypothesis profile, `gtnh`, that sets `deadline=None` and nothing else,
so a new `@given` test needs no `deadline=` of its own. Hypothesis's default is 200 ms of wall clock
per example, and wall clock is what `-n auto` on a busy machine takes away: a test that is only
waiting for a core, not slow, fails with `DeadlineExceeded`, or `Flaky` when the replay is quicker
(#216). The profile is built on whichever built-in profile Hypothesis picked, so under `CI` its `ci`
profile (already deadline-free, and derandomized) still applies in full. A test's own `@settings`
wins for what it names, so the `property_examples()` budgets are unaffected.

## Commands

```bash
pytest                    # all tests (every solve minimal; the full_solve tier skipped)
pytest --no-cov           # no coverage report; coverage is on by default via addopts
pytest -q tests/golden    # the corpus
ruff check .              # lint
mypy                      # types

# the full property-test space, as CI runs it - before opening a PR on solver/validator work
GTNH_TEST_HYPOTHESIS_FRACTION=1.0 pytest tests/test_solver_properties.py --hypothesis-show-statistics

# layout quality at full effort - when benchmarking, or chasing an unexpected quality drop
pytest --full-solve -m full_solve
```
