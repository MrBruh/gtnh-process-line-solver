# Testing

Goal: **100% path coverage, tests shipped with the code.** Framework: `pytest` + `hypothesis`.

## The core constraint: no headless GT simulator

True correctness of a layout (does the line actually run?) is only verifiable in-game. So
automated tests can only prove **self-consistency with the encoded rules**, not real-world
correctness. The strategy works around this with three layers:

1. **Independent validator.** The validator shares rule *data* with the router but has
   separately-written checking *logic*, so it can catch router bugs (a shared code path
   couldn't). It checks geometric validity (no overlaps, within bounds, pinned I/O honored)
   and rule validity (throughput/tier caps, one-fluid-per-line, summed amperage ≤ thickness,
   required-face reachability, ME-toggled commodities excluded + endpoint-placed).

2. **Property tests (hypothesis).** The safety net against the worst failure class. For any
   generated input graph, the solver must return **a valid layout OR an explicit
   infeasibility report — never a silently-invalid layout.** This is the one invariant that
   must always hold.

3. **Golden corpus** (`tests/golden/`). A small set of **known-good** layouts the validator
   must accept and **known-bad** ones it must reject — the only real-world ground-truth proxy
   in v1. Start hand-authored (3–5 good + a few bad); the v1.1 round-trip importer grows this
   from real community builds.

Plus a **manual in-game spot-check** of the starter dataset (tiers, face rules, throughputs)
during the Assignment — v1's only contact with actual GT behavior.

## What to test per module

- **adapter** — correct parsing of gtnh-factory-flow's exported plan JSON; missing/changed
  fields and plan-schema/dataset version mismatch handled, not silently dropped.
- **dataset** — entries load + validate; unknown machine / bad footprint raises clearly.
- **placement** — move operators (translate + orientation), each cost term, per-seed
  determinism, won't-fit infeasibility.
- **router** — A* per net, throughput/tier caps, one-fluid-per-line, EU-loss cost + amperage
  cap, channels-per-edge invariant, cell→block realizability, rip-up-and-reroute, ME-toggle
  skip + endpoint placement, unroutable → infeasibility.
- **solver** — the place→route→retry loop converges or gives up with a report; anytime budget
  returns best-valid-so-far.
- **validator** — geometric + rule checks; partial-invalid layouts reported, never passed.
- **cli** — parse a project, solve, emit previewer JSON + build guide, honor ME flags, surface
  infeasibility.

## Edge cases that must have tests

- Region too small to fit machines → infeasibility names the shortfall.
- A net that can't route within its tier → tightest violated constraint + suggested
  relaxation.
- A machine whose distinct I/O commodities exceed its five usable faces → flagged.
- Empty / single-machine line; the largest line the solver is expected to handle.

## Not auto-testable (manual / in-game)

- Whether a layout actually runs in GT:NH — covered by the in-game Assignment, not CI.
- Previewer visual correctness — smoke-test the render path; eyeball the rest.

## Commands

```bash
pytest            # all tests
pytest -q tests/golden    # the corpus
ruff check .      # lint
mypy              # types
```
