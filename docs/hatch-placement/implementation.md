# Implementation: placing real hatches and buses

The executable form of [`plan.md`](plan.md). That document is the *why* (what GT actually does, what
the solver models today, what was measured); this one is the *how*, lane by lane. The decisions this
plan implements are `plan.md` section 10, and they are not re-argued here.

**Read before starting any lane:** section 0 below. Two of its rules exist to prevent a failure mode
that validates clean and cannot be built.

Status: lanes 0 to 3 have landed, and lane 4 all but its repair step (4c). Next is lane 5
(section 7), with 4c reassessed in 6.1.

---

## 0. Rules that apply to every lane

### 0.1 The validator must not share the solver's geometry

```python
# validator/_geometry.py:13   <- this line is the hazard
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, in_region, occupied_cells
```

The validator re-exports the solver's own primitive. While `occupied_cells` cannot rotate that is
merely untidy. The moment casing cells become meaningful it is dangerous: a rotation bug would be
mis-modelled identically in the solver and in the only automated gate that exists to catch solver
bugs, and it fails **silently** (a plausible layout, hatches on the wrong faces, validating clean).
This is what `docs/ARCHITECTURE.md` decision 4 exists to prevent.

Lane 1 must give the validator its own expansion, derived independently. "Independently" means
written from the dataset's stated convention, not copied from `ir/geometry.py`.

**Decided 2026-09-01: the rule forbids sharing the derivation, not the data.** `FACE_DELTAS` and
`OPPOSITE_FACE` are rule data (six unit vectors, six pairs) and stay shared; the precedent is
already set and documented at `validator/core.py:613-619`, where the amperage check deliberately
shares `tier_voltage` and `CABLE_LOSS_PER_BLOCK` with the router so the rounding *policy* cannot
drift. `in_region` is a derivation and gets duplicated. The validator's own expansion is one
function, roughly 10 to 15 lines (`body_cells(cell, footprint, orientation) -> set[Cell]`), plus six
one-line call-site edits: all six of its current uses reduce to "the set of world cells this placed
machine's body occupies", and none needs an iterator, an order or a count.

Note `_check_auto_connections` already re-derives adjacency inline rather than importing
`auto_output_faces` (`validator/core.py:518-522`). That is the pattern to follow.

**The convention to write it from**, byte-identical across all 208 dumps:

> `controller front = NORTH (-Z), ExtendedFacing NORTH_NORMAL_NONE; offsets d = [dx,dy,dz]`
> `world-space deltas from the controller block`

Offsets are deltas from the **controller block**, not from the bounding box's minimum corner, so
they can be negative (the Coke Oven's `[-1,0,0]`). **Decided 2026-09-01: rotation preserves the
min-corner anchor.** Rotate the cells, then re-anchor so the minimum corner lands on `origin`,
exactly as `previewer/textures.py:_place_blocks` already does. Anything else moves every pinned cell
in the suite, including for square-base machines. Whatever the validator gets must re-anchor the
same way, or solver and validator disagree by a translation for every machine whose controller is
not at its own minimum corner.

### 0.2 Recorded hatch kinds are a lower bound, never a hard constraint

The dump's per-cell `kinds` come from asking the structure element what it would accept. An adder
built from a bare method reference exposes no filter, so the cell is recorded **without** that kind.
Measured: 23 of 208 controllers record no slots at all; of the 185 that do, 61 record no
Energy-capable cell and 35 no Maintenance-capable cell.

`validator/core.py:139-150` already refuses to enforce per-kind counts for exactly this reason and
documents it at length. Any lane that filters candidate slots by kind must carry the same fallback:
**unrecorded means permissive**. Making kinds a hard search constraint converts every extraction gap
into a false infeasibility across roughly a third of the dataset.

### 0.3 Two lanes will move every shipped layout

Lane 1 unpins 81 of 208 machines from a single orientation; lane 4 tightens the auto-output
predicate, which currently resolves 13 of nitrobenzene's 25 nets. Both change the layouts the
acceptance tests pin.

Re-derive the expected numbers and update the assertions. Do **not** loosen an exact-cell assertion
into a range to make a lane green. If a pinned metric gets worse, that is a finding to report, not a
test to relax.

**Decided 2026-09-01: stop and show the diff before changing any pinned value.** Report the before,
the after, and which of the two causes below it was. Do not re-baseline silently, and note that
`cables <= 3` (`test_solver.py:87, 99, 111`) has **zero** headroom, so one extra cable cell fails
three tests at once.

**The two causes are not the same thing, and lane 1's are not what this section originally implied.**
Measured: neither shipped example contains a non-square-base machine, so removing the
`_orientations_for` pin is a behavioural no-op for sand and nitrobenzene. The 81-of-208 figure is
real for the dataset but unreachable from either fixture. What actually moves sand is that making
`occupied_cells` orientation-aware forces the orientation to be chosen *before* the cells are tested
in `_relocate`, `_swap` and `_best_insertion` (all three test cells first today), which reorders
`random.Random` draws and therefore the accepted moves.

So: a layout change traceable to RNG reordering is an expected re-baseline; a layout change on a
square-base-only example that is *not* traceable to it is a regression. If the orientation-first
refactor can keep draw order identical for square-base machines, sand need not move at all, and any
movement then becomes a regression signal for free.

### 0.4 The usual gates

`ruff format . && ruff check . && mypy && pytest`, under the 3.12 `.venv`. Tests ship in the same
commit as the code. CI gates coverage at 90%. Never bypass the hooks.

---

## 1. Where this sits in the pipeline

```
adapter  --> InputIR (machines carry hatch SLOT OFFSETS, lane 2) -------------.
                                                                             |
placement ----> Placement (origin + orientation per machine)                 |
                                                                             v
router                                                                  slot geometry
  |  dock_candidates over SLOT-DERIVED faces  (lanes 3, 4a)            (rotated, lane 1)
  |  A* per commodity, then power                                            |
  |                                                                         |
  '--> legalize (lane 4c): is this (cell, face) a legal hatch?              |
          |  yes -> keep                                                     |
          |  no  -> nearest legal slot, re-route that net (bounded)          |
          '- casing budget exceeded -> INFEASIBLE, do not retry (4e)         |
                                                                             |
upkeep placement (lane 4f): maintenance anywhere legal; muffler needs an air  |
  cell in front, which becomes a routing KEEP-OUT                            |
                                                                             v
LayoutResult v1  (routes + terminals + placed hatches, incl. upkeep)  <-------'
   |
   |--> validator: independent expansion, own rules (0.1)
   '--> previewer: per-cube facing, real hatch sprites (lane 5)
```

---

## 2. Lane 0: variant and footprint must agree

**Independent of everything else. Do it first; it is a live bug today.**

`to_physical` derives the hatch counts from one variant (`dataset/multiblocks.py:320-323`) while
`footprint_for(fluid_outputs)` (`:174`) may reserve a *different, smaller* one. Verified: a
Distillation Tower reserved 3x6x3 carries `hatch_cells=97`, which is the 3x12x3 form's count,
against 49 for its own shape.

Today that is only a loose validator ceiling. Once slots are geometric it would place hatches
outside the reserved box.

- **Change**: derive `hatch_cells`, `energy_hatch_cells` and `upkeep_hatch_count` from the same
  variant `footprint_for` selects, not from the largest.
- **Acceptance**: for every controller in the local dump and every plausible `fluid_outputs`, the
  counts belong to the variant whose shape is returned.
- **Test**: property test over `data/<version>/multiblocks/`, plus a regression pinning the
  Distillation Tower's 3x6x3 form at its own count.
- **Size**: small. One module, one test file.

---

## 3. Lane 1: rotation-aware geometry

**The prerequisite. Nothing hatch-related starts before this is green.**

`occupied_cells(origin, footprint)` (`ir/geometry.py:55`) does not rotate; its own docstring records
the TODO at `:61`, and `adapter/core._orientations_for` pins non-square-base machines to one facing
because of it (`adapter/core.py:129`), affecting 81 of 208 machines today.

The reframing that matters: the existing TODO is about the bounding **box**, and a square-base
machine's box is rotation-invariant, so rotation genuinely does not matter for it now. The moment
individual casing cells become meaningful the **contents** must rotate too, which hits 100% of
multiblocks. The Coke Oven is 3x3x3, gets all four facings today, and its slot at `[-1,0,0]` lands
in a different world cell per facing.

- **Change**:
  - `occupied_cells(origin, footprint, orientation)`, and rotation for slot offsets and faces.
  - Lift the working primitives from `previewer/textures.py:323-374`, which already rotates and
    translates exactly the dataset's convention. This is prior art, not new work.
  - A genuinely independent expansion in `validator/_geometry.py` (see 0.1).
  - Remove the `_orientations_for` pin and the previewer's unrotated fallback.
- **Surface**: 62 `occupied_cells` references across `src` and `tests`, plus 22 direct
  `footprint.sx/.sy/.sz` reads to audit.
- **The test that makes this safe**: an oracle property test that re-derives every controller's
  rotated cell set from the raw dump blocks, per facing (208 controllers x 4). If the solver and the
  oracle disagree, the solver is wrong.
- **Acceptance**: suite green, `mypy --strict` clean, sand and nitrobenzene still solve VALID, the
  oracle passes for every controller and facing, and no non-square machine is pinned any more.
- **Expect**: acceptance-test churn per 0.3. The sand line's 5x1x2 and the per-objective pins are
  the ones to watch.
- **Size**: the largest mechanical lane. Comparable to `3cfc31c` (24 files, +1601/-113).

### 3.1 Sites a survey found that the reference counts miss

The "62 references" is 51 in code plus 11 in prose, and the 22 footprint reads miss 8 more that go
through an `fp = ...footprint` alias. More importantly, four sites need work and appear in **neither**
count. Treat these as first-class checklist items:

- **`_reorient` (`placement/search.py:542-564`) performs no geometry check at all.** It picks a
  machine, picks a facing, and returns. Correct today because rotation is a no-op; after this lane a
  reorient of a non-cubic machine can overlap a neighbour, leave the region or land on a reserved
  cell, breaking the annealing loop's stated invariant that every accepted state is
  overlap/bounds/reserved-clean (`placement/search.py:39-41`). **This is the most likely place for
  the lane to introduce a silently-invalid accepted state.**
- **`_apply_occupied_delta` (`:245-269`)** keys `before_cell` by `machine_id -> cell` and guards on
  `old_cell != new_p.cell`. A reorient changes the occupied set without changing the cell, so the key
  must become `(cell, orientation)` and the guard must fire on an orientation change. Miss it and the
  incremental occupied set drifts out of sync for the rest of the anneal.
- **`_rand_origin` (`:779-787`)** bounds the origin with `fp.sx/sy/sz` against the region. Under
  rotation the extents swap, so it both hands out origins a machine cannot fit at and consumes RNG
  against the wrong bound. Callers (`_relocate:486`, `_candidate_origins:773`) have no orientation
  chosen at that point, so this is real plumbing: pass one in, or return `(origin, orientation)`.
- **`_free_origins` (`placement/constructive.py:126`)** decides which origins are free with no
  orientation in scope, and `_fit` applies `orientation_options[0]` afterwards. For a non-cubic
  machine "is this origin free" is unanswerable without an orientation. `_fit` has a second caller
  (`search._best_insertion:683`), so a signature change ripples.

**The 17 lines that go silently wrong under rotation** (bounding boxes, floor areas, volumes,
centroids and fit tests, none of which raise): `ir/geometry.py:129,133`;
`placement/search.py:365,367,369,424-426,701-703,781,784-786`; `previewer/scene.py:66-68,226`.
`ir/geometry.py:129` and `:133` are the sharpest: `front_on_boundary` already takes the facing and
simply does not use it when measuring depth.

Genuinely orientation-invariant, and worth a comment so nobody "simplifies" them into bugs:
`adapter/core.py:462-464` (`_bounding_region` runs before any orientation exists and is written
symmetrically), `dataset/multiblocks.py:170,347` (y-only, and a volume product).

The dataset's convention, for the independent implementation: *controller front = NORTH (-Z),
ExtendedFacing NORTH_NORMAL_NONE; offsets `d = [dx,dy,dz]` are world-space deltas from the
controller block*. It is one identical string on all 208 dumps.

---

## 4. Lane 2: slot geometry through the contracts

Less new work than `plan.md` implies. `HatchSlot` **already exists** in `dataset/schema.py:79`
carrying the offset and the accepted kinds, and `MachinePhysical` already derives `hatch_cells`,
`energy_hatch_cells` and `upkeep_hatch_count` from it. What is missing is that `to_physical`
(`dataset/multiblocks.py:285`) reduces the slot list to those three integers at `:320-323` and
**drops the offsets on the floor**.

- **Change**:
  - Carry slot offsets per variant into `MachinePhysical`, then onto `Machine`.
    `INPUT_IR_VERSION` 3 -> 4, recorded in `ir/__init__.py` and `docs/IR.md`.
  - `LAYOUT_RESULT_VERSION` 0 -> 1 (`ir/output.py:20`), for placed upkeep hatches. This is the
    **first output-contract bump this project has done**; they belong to no net, have no route, and
    cannot be represented today.
  - A placed hatch needs `(machine_id, kind, cell, facing, port_id | None)`. `Terminal` already
    implies the body cell as `cell - FACE_DELTAS[face]` and the validator already computes exactly
    that (`validator/core.py:467-469`); the new type should not duplicate that relationship.
- **Acceptance**: a machine with a structural record carries its slots; a single-block machine or a
  plan adapted without the dataset is byte-identical to before; fixtures and the golden corpus carry
  the new versions.
- **Size**: 1 to 2 sessions. Mechanical, but it ripples through adapter, validator, previewer,
  buildguide, `docs/IR.md` and every fixture.

---

## 5. Lane 3: route-aware docking for items and fluids

**[Landed, 2026-09-02.] Small, standalone, shippable on its own, and a hard precondition for
lane 4.** The three acceptance criteria below all hold; the two findings it turned up are in 5.1
and 5.2, and 5.1 is work lane 4 already owns.

`dock()` (`router/_grid.py:74`) commits to the first free face in `FACE_ORDER` (`:20`), blind to
where the route has to go. That is why fluid docking on nitrobenzene measures south 15 / west 1
against power's route-aware south 5 / up 3 / west 3 / down 1. `dock_candidates` (`:86`) already
exists and the power router already uses it.

Legalizing an arbitrary first-fit would promote a tuple ordering into a physical build instruction,
which is the whole reason this lane comes first.

- **Change**: move the item and fluid routers onto `dock_candidates` + `astar_multi`, mirroring
  `router/power.py`.
- **Acceptance**: the nitrobenzene face histogram stops being `FACE_ORDER`-shaped; total route cells
  do not regress on either shipped example; the solve stays deterministic for a fixed seed.
- **Size**: under a session.

**Measured, all three met.** The histogram went `south 16, up 1, west 1` -> `west 6, east 6,
south 4, down 3, up 3`. Route segments went 114 -> 86 and power cable cells 60 -> 43. Both
examples still solve twice to the same layout. Sand did not move at all, on any objective. The
whole suite stayed green with no assertion touched, which was not expected: 0.3 anticipated
churn here and there was none, because nothing pins a face or a cell count on nitrobenzene.

`dock()` is gone. Its only caller was `core._negotiate`, and leaving a first-fit helper in
`_grid` next to `dock_candidates` would only invite a future caller to reintroduce the bug.

### 5.1 What it cost, and why lane 4 is the fix rather than a lane 3 mitigation

Nitrobenzene's floor area went **136 -> 152** under the default `footprint` objective, which is
that objective's *leading* metric, so by the project's own ranking the shipped example got worse
even as its build got 25% shorter. Under `volume` and `balanced` the ranked metric also worsens
slightly (1360 -> 1400, 1496 -> 1575). Recorded rather than absorbed, per 0.3.

The mechanism is exact, and it is a capacity problem, not a docking one. Shorter pipes hug machine
surfaces. Machine `node-c85c58bc` has **three HV energy hatches** and 9 dockable cells; after
item/fluid routing takes 7 of them it has 2 left, and three hatches need three distinct cells
(`power._route_trunk`'s `claimed` set), so `power:HV` fails `face_reachability`. That kills grid
attempt 0 - which was the baseline's *winning* layout - and the multi-start grid falls back to a
worse-ranked survivor.

Nothing in lane 3 can fix that: the item/fluid router has no way to know a machine owes three
cells to a net it does not route. **4d (claimed body cells) and 4a (slot-driven candidates) are
exactly that bookkeeping**, and 4e turns the leftover case into an explicit infeasibility instead
of a lost attempt. Pulling a reservation step forward into lane 3 would move every layout twice
for the same end state. Decided 2026-09-02 with the maintainer: land lane 3, recover the metric in
lane 4, and treat 136/1360/1496 as the numbers lane 4 has to beat.

### 5.2 A pre-existing power defect this made reachable

The power router sizes cables but never checks that enough power **arrives**: a machine takes
packets through hatches capped at `Port.max_amps`, and cable loss shrinks each packet, so
`sum(max_amps * delivered_volts)` can fall below `eut` on a run whose every segment is thick
enough. `validator/core` checks it (`POWER_SUPPLY_INSUFFICIENT`); `router/power` has no equivalent,
and greps clean for `max_amps`.

The result is a fully-routed layout the validator kills, which `solver/core._assemble` returns as
`partial_invalid` with an **empty** `failed_nets` - so no net is penalized and the feedback loop
treats it as unfixable by re-placing, when it is precisely distance-driven and therefore exactly
what re-placing fixes. Two of nitrobenzene's eight grid attempts now hit it.

Lane 3 did not cause this and does not touch power sizing; longer trunks simply made it reachable.
Filed as issue #106 rather than fixed here.

---

## 6. Lane 4: assignment, legalize, upkeep, keep-out

**[Landed 2026-09-02, except 4c.] The substance.** Sub-steps in dependency order. What actually
happened to each is in 6.1; read that before starting 4c or lane 5.

**4a. Slot-driven, kind-filtered candidates.** `_dock_faces` (`router/_grid.py:37`) currently walks
every cell of the bounding box. It becomes: rotated slot offsets only, filtered by kind, with the
permissive fallback of 0.2. Interior slots (29% of all slots, touching no bbox face) are excluded
from routed I/O candidates; they remain legal for maintenance. For the Coke Oven this takes 8 of its
26 currently dockable cells out of play.

**4b. The facing rule.** A hatch's facing must point out of the structure, at an exposed face.
Stronger for items and power, which are front-face-only in both directions: the receiver must be on
that face. Fluids are omnidirectional, but a routed pipe merely touching a fluid *input* hatch can
drain it (`isLiquidOutput` defaults true), so adjacency is not innocent. GT never validates any of
this; mirror its own auto-builder heuristic (first face not contained in the structure piece,
preferring a horizontal one).

**4c. Legalize and repair.** After a net routes, check that its chosen `(cell, face)` hosts a legal
hatch. If not, take the nearest legal slot and re-route that net. Bounded retries, deterministic
order, and every abandonment recorded so the failure is explicable.

**4d. Claimed body cells.** Keep a per-machine set of claimed body cells. This closes a hole that
exists today: nothing stops two hatches sharing one body cell via two different faces.

**4e. The casing budget is an infeasibility, not a retry.** Every hatch placed decrements the casing
count and machines assert minimums, so one extra output bus can un-form a machine. This is a global
per-machine constraint; no nearby slot and no re-placement can fix it. `energy_hatch_budget`
(`dataset/multiblocks.py:139-150`) already does the arithmetic. Report it, stop.

**4f. Upkeep hatches.** Maintenance accepts from any side, so it is the easy one and may sit on an
interior slot. The muffler needs literal **air** in front of its facing, or the machine shuts down
with `POLLUTION_FAIL`; that cell becomes a routing keep-out, a constraint class the router has no
equivalent for today. A muffler is required exactly when `getPollutionPerSecond > 0`.

> **[Corrected, 2026-09-02.]** Two of those three sentences are wrong, and the code follows the
> corrections, not this paragraph.
>
> **A maintenance hatch may NOT sit on an interior slot.** Every hatch needs a facing that points
> out of its own structure, and an interior cell has six body neighbours, so any facing is inward -
> `HATCH_FACES_INWARD`. It also has to stay reachable: only the maintenance hatch's *item
> automation* is omnidirectional, while `onRightclick` requires the front face for tools, duct tape
> and the GUI. Interior slots therefore host nothing at all.
>
> **A muffler is not required "exactly when `getPollutionPerSecond > 0`".** That predicate exists
> (`MTEMultiBlockBase.java:3629-3631`) but is dead code in gregtech - only gtPlusPlus calls it, and
> gregtech's own `validateStructure` is empty. The requirement is per controller, and some
> controllers accept a muffler without ever asserting one. The proxy that works, and that landed, is
> **"the dump records a `Muffler`-capable cell"**: GT only offers the element to a controller that
> pollutes. It over-places on a handful, which is the safe direction.
>
> `plan.md` sections 1.1, 1.4 and 8.4 carry the full source citations.

**4g. Auto-output tightening.** `ir.geometry.auto_output_faces` (`:139`) models auto-output as any
touching body cell. For a multiblock it is a specific output hatch's own facing, and
`router/auto.py`'s one-auto-output-per-machine rule (`:46`) is wrong for a machine with several
output buses. This moves 13 of nitrobenzene's 25 nets; see 0.3.

**Optional: the LAP warm start.** `plan.md` section 6.1 works out that the assignment is a linear
assignment problem, exactly solvable in microseconds with about 80 lines of deterministic Hungarian
(no LAP solver is available; runtime deps are pydantic only). Under the chosen repair-based approach
this is **not** required for routed I/O. It is worth having only for the upkeep hatches, which are
never routed and therefore have nothing to repair against.

- **Acceptance**: every terminal's body cell is a recorded slot of an accepting kind (or the machine
  records none); no hatch faces into its own structure; every muffler has an empty cell in front;
  nitrobenzene and sand still solve VALID; the validator enforces all of it from its own expansion.
- **Size**: 4 to 7 sessions, and the widest uncertainty band in the plan.

### 6.1 What landed, and the one sub-step that turned out not to be needed

Two commits, both green on the full gates.

| Sub-step | State |
|---|---|
| 4a slot-driven, kind-filtered candidates | landed |
| 4b the facing rule | landed (structural half was lane 2; the usable-face half is the kind filter plus the exposure test) |
| **4c legalize and repair** | **not built, and not needed as specified** - see below |
| 4d claimed body cells | landed, and wider than planned: the pool is shared across commodities |
| 4e casing budget as an infeasibility | landed (`hatch_budget`) |
| 4f upkeep hatches + the muffler keep-out | landed |
| 4g auto-output tightening | landed |
| the LAP warm start | not built, and not needed: see below |

**4c dissolved once 4a and 4d were in place.** The plan's shape was "route, then check whether a
hatch can legally go where it docked, and try a nearby slot if not". But 4a makes the candidate
set *legal by construction* - docking only ever offers a cell that accepts the kind and has an
exposed face - and 4d makes it unique, so a routed terminal cannot be illegal by the time it is
checked. What was left for a repair loop is the case where a machine simply runs out of cells,
and the plan already says that one must be reported rather than retried (4e). Section 10.2's
decision was between repair-after-routing and negotiation-during-routing; constrain-before-routing
turned out to be a third option that costs neither.

The one thing this loses is the ability to *trade* a contested cell, which is what option C was
for. If a future line proves infeasible only because docking order spent a scarce cell badly, C is
still the answer, and nothing here forecloses it.

**The LAP warm start was not needed for the upkeep hatches either.** `plan.md` 6.1 kept it in
reserve for them, since they are never routed and so have nothing to repair against. In practice
they take the first free legal cell in the same total order docking uses, and both shipped
examples place every one of them first try. A machine tight enough to need an optimal assignment
would be one hatch cell from infeasible anyway, and reports that.

**Two deviations from the plan's text, both forced by the geometry.**

- **Upkeep hatches may not sit on an interior slot**, which 4f says they may. An interior cell has
  no outward face, and every hatch needs one or `HATCH_FACES_INWARD` rejects it - correctly: a
  maintenance hatch buried in the structure cannot be right-clicked, and GT's own maintenance GUI
  is front-face-only (`MTEHatchMaintenance.onRightclick`).
- **The claim pool is not just "body cells"**, as 4d frames it. For a machine with no dumped
  structure the claim has to be the *face*: a single-block GT machine genuinely takes input on one
  face of its one block and output on another, so claiming its body cell would cap it at one
  connection. `_grid.claim_key` is that split.

### 6.2 Where the metrics went

Recorded per 0.3. Nitrobenzene, default `footprint` objective, floor area / volume / power cable
cells / route segments:

| | floor | volume | power | segments |
|---|---|---|---|---|
| before lane 3 | **136** | 1360 | 60 | 114 |
| lane 3 (route-aware docking) | 152 | 1216 | **43** | **86** |
| lane 4a/4d (slot-driven + shared pool) | 144 | 1440 | 73 | 111 |
| lane 4 complete | 154 | 1540 | 60 | 90 |

Under `volume` and `balanced`, though, the finished lane beats the original baseline outright:
**882** volume against 1360, 126 floor against 136, 34 power cells against 60, 79 segments against
114. Sand is byte-identical throughout, on all three objectives.

The honest reading: the floor-area number under the default objective is 13% worse than a layout
that **could not be built** - hatches on cells that host none, two hatches on one block,
auto-output through casing with no bus in it, and no maintenance hatch or muffler anywhere. The
constraints are physical, so the earlier figure was never available. What is worth chasing
separately is why the `footprint` objective now finds a *worse* floor area (154) than the `volume`
objective happens to find on the way past (126); that is a multi-start grid artifact, not a lane 4
regression, and it is unscheduled.

### 6.3 Left open

- **Nothing checks that every port HAS a hatch.** Emission gives every port on a dumped machine
  one, and a test asserts it end to end on both shipped lines, but the validator does not, so a
  producer that dropped one would not be caught. It needs the ME-toggled and unrouted-port cases
  thought through first.
- **`ir.geometry.auto_output_faces` is now only the placement cost's rule.** It models the loose
  "any touching body cell" adjacency, which is right as a soft reward but is no longer what the
  router does. Worth a comment at minimum, and possibly worth teaching the cost the tighter rule
  so placement stops being rewarded for adjacencies routing will refuse.

---

## 7. Lane 5: hatch textures

**7.1 was this plan's largest unknown. It is now settled: splice, do not re-dump (2026-09-01).**

`_rotate_side` (`previewer/textures.py:330-331`) permutes only the four horizontal sides and returns
UP and DOWN unchanged, and the extractor pins `aFacing` to NORTH when dumping
(`TextureDumper.java:474-475`). Vertical facings are not an edge case: 75% of sand's terminals and
17% of nitrobenzene's are vertical.

**Why the splice is exact rather than approximate.** `MTEHatch.getTexture`
(`MTEHatch.java:55-80`) computes its background without referencing either `side` or `aFacing`; the
only use of facing in the entire method is the equality test `side != aFacing`. So

```
getTexture(side, facing) = bg(side)                      if side != facing
                         = [bg(side)] ++ overlays(state) if side == facing
```

with both halves facing-invariant. The per-side variation lives one level down, inside a single
sided `ITexture` whose six-icon array the extractor already unwraps
(`TextureDumper.java:508-517`), so the manifest's layer 0 for side S **is** `bg(S)`, correctly
resolved. A six-facing re-dump would write byte-identical stacks.

Verified rather than assumed. Across the whole monorepo exactly two classes in the 92-class
`MTEHatch` hierarchy override `getTexture`, and one is a no-op `super()` delegation
(`MTEHatchTFFT.java:76-79`). ME stocking buses, GT++ hatches and TecTech multi-amp hatches override
only `getTexturesActive`/`getTexturesInactive`, making them the simplest case rather than the
exception the spike feared. Empirically, across all 500 hatch entries x 2 states: zero non-front
sides with more than one layer, zero front stacks whose layer 0 differs from the horizontal
background, zero missing side/state keys.

**Two corrections to what section 5 of `plan.md` says.**

- The "roughly 6x the manifest volume" figure is overstated by about 3.5x. Hatches are 1.97 MB of an
  11.70 MB `blocks` payload, so a naive re-dump is 1.69x the whole file and a front-only re-dump
  1.21x. Size was never the deciding argument anyway; redundancy is.
- **Not all five non-front sides share a background.** UP and DOWN differ from the four horizontals
  in **500 of 500** hatch entries (`MACHINE_LV_TOP`/`_BOTTOM` vs `_SIDE`). An implementation that
  took the background from SOUTH would be wrong on every hatch in the pack. This is precisely why a
  vertical facing needs the target side's own layer 0.

**The rule.** Applies to hatches only; gate on the hatch set, because `MTEBasicMachine` descendants
have genuine per-side overlays and are dumped through a different code path
(`TextureDumper.java:444-467`).

```
FRONT_IN_DUMP = "NORTH"                     # TextureDumper.java:474 pins aFacing

def hatch_layers(manifest, block, meta, render_side, facing, state):
    own = manifest.layers(block, meta, render_side, state)   # target side's OWN stack
    if not own:
        return []
    background = own[0]                     # whole layer dict: icon + rgba + glow
    if render_side != facing:
        return [background]                 # MTEHatch.java:68-69
    front = manifest.layers(block, meta, FRONT_IN_DUMP, state)
    return [background] + list(front[1:])   # MTEHatch.java:70-75, same state on both reads
```

Implementation notes:

- **Do not re-rotate.** For a hatch, replace the `_rotate_side(side, -cube.steps)` mechanism in
  `_face_icons` (`previewer/textures.py:461-480`) entirely: pass world-space `render_side` and the
  hatch's own world-space `facing`. The yaw applies to the hatch's facing, not to the side lookup.
- **The pool key must gain the facing.** It is `f"{block}|{meta}|{side}|{state}"` today. Without the
  facing, an UP-facing and a NORTH-facing hatch of the same type collide in the texture pool and one
  silently gets the other's bake. **This is the one place the change can go quietly wrong.**
- `front[1:]` is safe: the front stack's layer 0 equals the horizontal background in 500/500 entries
  at both states, so the slice never eats an overlay.
- No schema change. The texture manifest is read untyped in `textures.py`; `dataset/schema.py` does
  not model it.
- Free win: the same rule fixes horizontal facings, which are wrong today whenever a hatch faces
  differently from its machine.

**Three fidelity limits that cost the same either way**, so they are separate issues and not
arguments for re-dumping: `.extFacing()` overlay *rotation* is not captured by the manifest's layer
record at all (it selects a rotation, never a different icon, so the splice picks the right sprite
and may draw it turned); the extractor dumps *unattached* hatches, so a bus on a Coke Oven renders
with its tier casing rather than the controller's skin (`updateTexture` only runs when a hatch joins
a formed multiblock); and colorization is dumped unpainted, which is correct for an unpainted build.

**7.2 Per-cube facing.** `BlockCube` carries one `steps` for the whole machine's yaw. A hatch needs
its own.

**7.3 The committed manifest has zero hatch entries.** `tools/derive_small_manifest.py` keeps an MTE
only when its display name contains an example machine-type name, which no hatch can match. All
tiers is about +327 KB on a 196 KB base; LV/MV/HV only is about a third of that. This is issue #98's
open (a)/(b) fork.

**7.4 The maintenance hatch's states are inverted.** Inactive is `MAINTENANCE + DUCTTAPE`, meaning
broken; GT sets it active on placement. Rendered with the previewer's `"inactive"` default every
machine would show duct-taped.

**7.5 `previewer/scene.py:93-101` drops `port_id`** when emitting terminals, so the previewer cannot
tell which port a terminal belongs to. Add it.

**7.6 Coordinate with issue #105.** The hardcoded `ForgeDirection.NORTH` in 7.1 is
`TextureDumper.getTextureLayers`, lines 474-475. That is the *same call site* as the wrong-overload
bug that drops all 1185 pipes and cables in #105. One function, two fixes. Whoever opens that file
should do both, and the two lanes should be sequenced together rather than treated as unrelated.

The good news, unchanged from `plan.md` section 5: the full local manifest already carries all 465
hatch and bus MTEs at every real tier, all six sides, both states, with per-kind per-tier overlay
icons already resolved. No extractor work is needed to *find* them.

---

## 8. What not to do

- **Do not make hatch choice an SA move.** Rejected in `plan.md` section 6.2 with measurements: it
  multiplies the state space by roughly 10^13 per multiblock against an unchanged 1380-iteration
  budget, and `_cost` computes nothing that hatch choice changes, so it would be a free random walk
  that dilutes the moves that matter. `placement/search.py:11-13` already documents this pathology
  for reorient.
- **Do not enforce per-kind counts as a hard constraint** without the permissive fallback (0.2).
- **Do not let the validator import the solver's rotation** (0.1).
- **Do not loosen a pinned assertion** to absorb re-baselining (0.3).
- **Do not retry a casing-budget failure** (4e).
- **Do not derive hatch cells from wherever routing happened to dock** without lane 3 first (5).

---

## 9. Definition of done

1. Every routed terminal lands on a cell the dump records as accepting that hatch kind, or on a
   machine that records no slots at all.
2. No hatch faces into its own structure; items and power have their receiver on the hatch's own
   front face.
3. Every machine that pollutes has a muffler with an empty cell in front of it, and routing respects
   that keep-out.
4. A machine demanding more hatches than its casing budget allows is reported as an explicit
   infeasibility naming the machine and the shortfall.
5. The validator proves 1 to 4 from its own geometry, not the solver's.
6. Both shipped examples still solve VALID, with their metrics re-derived rather than relaxed.
7. Hatches render with their real GT sprite at their real facing, vertical facings included.
8. `docs/DOMAIN.md` is corrected: the "five non-front faces are interchangeable" claim is right for a
   single-block machine and wrong for a multiblock's hatches (`plan.md` section 1.8).
9. The build guide emits the fluid-lock and item-lock configuration, or a routed pipe may carry the
   wrong product (`plan.md` section 1.6).
