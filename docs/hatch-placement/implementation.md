# Implementation: placing real hatches and buses

The executable form of [`plan.md`](plan.md). That document is the *why* (what GT actually does, what
the solver models today, what was measured); this one is the *how*, lane by lane. The decisions this
plan implements are `plan.md` section 10, and they are not re-argued here.

**Read before starting any lane:** section 0 below. Two of its rules exist to prevent a failure mode
that validates clean and cannot be built.

Status: no lane started as of 2026-09-01.

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

**Small, standalone, shippable on its own, and a hard precondition for lane 4.**

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

---

## 6. Lane 4: assignment, legalize, upkeep, keep-out

The substance. Sub-steps in dependency order.

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

---

## 7. Lane 5: hatch textures

**De-risk 7.1 before committing to a schedule for this lane.** It is the largest unknown in the
whole body of work, and its fallback changes the cost of the dataset rather than the previewer.

**7.1 Vertical facing cannot be expressed today.** `_rotate_side` (`previewer/textures.py:330-331`)
permutes the four horizontal sides and returns UP and DOWN unchanged, and the extractor pins
`aFacing` to NORTH when dumping (`TextureDumper.java:474-475`). Vertical facings are not an edge
case: 75% of sand's terminals and 17% of nitrobenzene's are vertical. Two routes:

- **Splice**: background from the target side's own layer 0, overlay from NORTH's layers 1..n. Cheap,
  but it must be checked against ME stocking buses and GT++ hatches before it is generalised.
- **Re-dump across six facings**: correct by construction, roughly 6x the manifest volume.

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
