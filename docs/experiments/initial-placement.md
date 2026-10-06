# Experiment: how the annealer's start shapes the layout (2026-10-05)

**Result: no-go.** No start shape tried beats `main` under a gate set before the results were
read, so the placer is unchanged. What came out of it is a measuring tool, `gtnh-solve --trace`
(`solver/trace.py`, `placement/trace.py`), and the findings below, so the next experiment on the
search does not repeat these runs.

## The question

Every attempt of a solve anneals from the same start, `place(problem, lattice=True)`
(`placement/constructive.py`). It is deterministic, so a full solve's 8 attempts differ only in the
annealer's random draws. Its shape depends on the line:

| line | start |
|---|---|
| single blocks only | a spaced lattice: rows of ceil(sqrt(n)) blocks, a 1-cell gap between blocks in a row, a 2-cell aisle between rows |
| single blocks with parallel groups | the same spacing as a "shelf" of units, each group one back-to-front column (#321) |
| any multiblock | the plain first-fit strip in flow order |

The question was how much that start decides the final score, and whether a different one does
better. Two experiments: a **diagnostic** of what each anneal does to its start, then a **sweep**
of start shapes judged against `main`.

## Headline

1. **The anneal keeps the start's size, not its arrangement.** About 95% of machines move and up
   to two-thirds turn; the machines' left-to-right order is all but reshuffled (Kendall tau of x
   order 0.05 to 0.10). Yet a line of single blocks ends up roughly the start's size times a fixed
   ratio: iron's start floor 169 anneals to a median 71, a start of 100 to 48.
2. **The walk does most of its work early.** About half the cost improvement lands in the first
   10% of the iterations, 90% by half way.
3. **No spacing or multiblock-shelf variant passes its gate.** Iron looked better from a tighter
   start (gap 0, aisle 1: score -11.3%, 13 of 16 seeds won), but on 16 fresh seeds that shrank to
   -1.7% and 9 of 16. It was the luck of trying 9 variants on one seed set.
4. **The multiblock shelf start is no better than the strip** for nitrobenzene (+4.1% at `main`'s
   spacing, worse as spacing grows).
5. **bio-diesel-ethylene's problem is crowding, not its start.** On `main`, none of its 128
   attempts passes the crowding gate (`placement/feasibility.py`), whatever the spacing (1 of 128
   in one variant). Every solve is the gated fallback, partial on congestion. That is the open
   problem this round turned up.

## Setup

- **Lines:** `examples/gtnh-sand.json`, `examples/gtnh-nitrobenzene.json`, and two community plans
  kept outside the repo: `iron.json` (28 single blocks, 2.8.4) and `bio-diesel-ethylene.json` (35
  single blocks). `examples/gtnh-parallel-sand.json` joined the confirmation run.
- **Solves:** full effort, `--rounds 1`, `--jobs 1`, seeds `0:1600:100` (16, spaced 100 apart so
  no two solves share an attempt seed; see `tools/bench_layouts.py`), run on a separate machine so
  the results do not depend on what else this PC was doing.
- **Score:** floor area plus route cells, the solver's own ranking for the default `footprint`
  objective (`solver._structure.structure_quality`, the key `tools/bench_layouts.py` reports).
  Lower is better. Variants are paired with `main` seed by seed: a win is a strictly lower score.
- **Instrumentation:** an instrumented build of `main` that wrote one record per attempt. It read
  no random draws, and matched `main` on every run compared: status, size, placement and route
  counts on 128 lab runs, and byte for byte on local solves of sand and nitrobenzene. It is what
  `--trace` now writes.

## 1. Diagnostic: what the anneal does to its start

`main`, 16 solves x 8 attempts per line. Medians over the 128 anneals.

| | sand | nitrobenzene | iron | bio-diesel |
|---|---|---|---|---|
| machines / iterations | 6 / 360 | 23 / 1380 | 28 / 1680 | 35 / 2100 |
| start box (x, y, z), floor | 5x1x4, 20 | 41x7x7, 287 | 13x1x13, 169 | 13x1x13, 169 |
| returned floor, machines only (min to max) | 2 (2 to 8) | 130.5 (70 to 266) | 71 (33 to 132) | 99.5 (30 to 169) |
| mean move per machine, cells | 4.3 | 17.0 | 6.4 | 7.6 |
| share of machines that never moved | 0% | 4% | 4% | 3% |
| share turned | 33% | 39% | 63% | 51% |
| Kendall tau, x order / z order | 0.00 / 0.13 | 0.05 / 0.08 | 0.10 / 0.58 | 0.06 / 0.33 |
| improvement done by 10% / 50% of the walk | 100% / 100% | 47% / 91% | 48% / 90% | 56% / 92% |
| attempts the gate turned away | 0 | 0 | 4 | **128** |
| attempts routed VALID | 128 | 113 | 91 | 0 |
| the start itself, routed | VALID, score 46 | partial (2 nets) | gated (2 machines) | gated (12 machines) |

Reading it:

- The arrangement does not survive. Only iron's front-to-back rows partly do (z order tau 0.58):
  its rows are what the lattice laid, and the walk tends to compact along them.
- The size does survive, for single-block lines. The docstring of `constructive.py` already said
  the floor term barely moves a lattice, because each of its edges is held by a whole row of
  blocks. The sweep below confirms it: iron's returned floor follows the start's.
- A multiblock line's strip is folded properly (nitrobenzene's 41-cell strip comes back 18 wide),
  as #254 intended.
- Sand converges within the first tenth of a walk; it is too small to tell starts apart.

## 2. Sweep: nine start spacings

**Gate, written down before any result was read (the maintainer chose the strict option).** A
variant is a go only if all hold: no line loses VALID runs; the median score is at least 5% better
on both iron and nitrobenzene, with at least 11 of 16 seeds won on each; no line's median is more
than 2% worse; and bio-diesel's VALID count is not lower and its median unrouted nets not higher.

**Variants:** the gap between blocks in a row {0, 1, 2} x the aisle between rows {1, 2, 3}
(`_LATTICE_STRIDE_X` / `_LATTICE_STRIDE_Z` minus one), and in every variant a line with a multiblock
also starts on the shelf, each machine a unit at its footprint, instead of the strip. `main` is gap
1, aisle 2, strip.

| gap, aisle | iron score (floor / route) | iron w/t/l | nitrobenzene score (floor / route) | nitro w/t/l |
|---|---|---|---|---|
| `main` | 150 (71 / 79) | | 158.5 (108.5 / 46) | |
| 0, 1 | 133 (58 / 75.5), -11.3% | 13/0/3 | 169 (118.5 / 50.5), +6.6% | 8/0/8 |
| 0, 2 | 142 (63 / 76.5), -5.3% | 10/0/6 | 159.5, +0.6% | 9/0/7 |
| 0, 3 | 149.5, -0.3% | 6/1/9 | 157, -0.9% | 5/0/11 |
| 1, 1 | 137 (56 / 82.5), -8.7% | 8/0/8 | 158.5, 0% | 7/0/9 |
| 1, 2 | identical to `main` | 0/16/0 | 165 (120 / 43.5), +4.1% | 8/0/8 |
| 1, 3 | 150.5, +0.3% | 8/0/8 | 162, +2.2% | 7/0/9 |
| 2, 1 | 154.5, +3.0% | 8/0/8 | 164, +3.5% | 6/0/10 |
| 2, 2 | 158, +5.3% | 5/0/11 | 167, +5.4% | 6/0/10 |
| 2, 3 | 153.5, +2.3% | 7/0/9 | 175, +10.4% | 4/0/12 |

Sand, nitrobenzene and iron were VALID on 16 of 16 seeds in every variant, and sand's median score
was 7 throughout. bio-diesel stayed 0 of 16 VALID everywhere; its routed-net counts moved by noise.

**Verdict: no-go on all nine.** Nitrobenzene never improves by 5%, so clause 2 fails everywhere.

The two knobs never touch the same line: spacing only reaches single-block lines (iron, sand,
bio-diesel), the shelf only reaches nitrobenzene. Each row of the table is therefore two separate
results, iron's spacing effect and nitrobenzene's shelf effect, and a gate asking both lines to
improve cannot be passed by a gain on one. That is a flaw in how this sweep was laid out, kept
visible here rather than reinterpreted afterwards.

- **Iron** favoured tight starts and lost on wide ones.
- **Nitrobenzene** lost on the shelf at every spacing: at `main`'s spacing (1, 2) its floor grew
  (108.5 to 120) more than its route shrank (46 to 43.5).

## 3. Confirmation: iron's tight start on fresh seeds

The maintainer chose to test the best iron result again before believing it.

**Gate, written down before the run:** gap 0, aisle 1 for single-block lines, multiblock lines left
on the strip (so nitrobenzene is identical to `main` and was not run), seeds `1600:3200:100` (none
used before). A go only if iron's median score is at least 5% better with at least 11 of 16 seeds
won, no line loses VALID runs, and no line's median is more than 2% worse.

| line | VALID | score (median) | w/t/l | floor / route | attempts routed VALID |
|---|---|---|---|---|---|
| iron | 16 to 16 | 143 to 140.5 (-1.7%) | 9/0/7 | 72 / 77 to 63 / 75 | 94 to 50 of 128 |
| sand | 16 to 16 | 7 to 7 | 0/14/2 | unchanged | 128 to 128 |
| parallel-sand | 16 to 16 | 27 to 27 | 0/16/0 | unchanged (its bank-column layout wins either way) | 127 to 127 |
| bio-diesel | 0 to 0 | none VALID | | | 0 to 0 (all gated) |

**Verdict: no-go.** The sweep's -11.3% and 13 of 16 did not replicate.

Two observations outside the gate, as leads rather than results:

- The tight start trimmed iron's worst cases: the highest score fell from 181 to 158 and the mean
  from 148.6 to 138.9. It also cost some of the best layouts (seed 3100: 129 to 153), so the
  median hardly moved.
- It halved the attempts that route VALID (94 to 50 of 128). Iron has the slack to absorb that; a
  tighter line would start losing VALID solves.

## What not to retry, and what is open

- **Don't retry start spacing or the multiblock shelf** without a new reason. Both are measured
  here, and #272 (the lattice) and #291 (a spaced multiblock seed, parked) measured neighbours of
  them.
- **Open: bio-diesel-ethylene never passes the crowding gate.** No anneal over 128 attempts and nine
  start shapes produces a placement every machine can dock in. Its fallback layout is partial on
  congestion. The gate's verdict, the face-shortfall term that stands in for it during the walk,
  and what that line's machines need are the place to look.
- **Open: a start that trims the worst case.** If the tail matters more than the median for some
  use, gap 0 / aisle 1 is a lead, with its VALID-attempt cost measured on lines tighter than iron.

## Reproducing

`gtnh-solve PLAN --seed S --rounds 1 --trace FILE` writes the per-attempt records this experiment
used (the module docstrings of `solver/trace.py` and `placement/trace.py` describe each field).
The spike branches stay on GitHub and are never meant to merge:

| branch | commit | what |
|---|---|---|
| `spike/initial-placement` | `f8244ad` | the instrumented `main` (records on stderr) plus an off-by-default `_SHELF_MULTIBLOCKS` switch |
| `spike/ip-sweep` | `668da22` to `dc4395b` | one commit per sweep variant, in the table's order (gap 0 to 2, aisle 1 to 3 within each) |
| `spike/ip-confirm` | `216dc04` | gap 0, aisle 1, shelf off |

The base for every comparison is `main` at `d7a398c`.
