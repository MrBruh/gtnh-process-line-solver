# Placing real hatches and buses on multiblocks

**Status: findings from a research spike, 2026-08-27. Lanes 0, 1 and 2 have since landed.**

Sections 1 to 9 are the spike as written, and are left as a record of what was known then. Where a
finding has since been resolved or corrected, it is marked inline as **[Landed]** or
**[Corrected]** rather than rewritten. Read them for the GT behaviour, which has not changed.

- **Section 10** is what the maintainer decided.
- **Section 11 is the current state and what to do next.** Start there.
- [`implementation.md`](implementation.md) is the lane-by-lane execution, and the more accurate of
  the two documents on anything mechanical.

Today a multiblock's I/O is abstract: a `Port` says "this machine needs a fluid output somewhere on
a non-front face", and the router picks any free cell next to the bounding box. This spike asks what
it would take to place **real hatch and bus blocks at real casing cells**, with real facings, real
textures, and routes that provably start and end where GT will actually accept them.

`docs/dataset-extraction/plan.md` section 8 already predicted this hole: *"Not yet built: a
hatch-assignment stage (choose and emit a concrete input/output hatch per hint slot)."*

---

## 1. What GT actually does

Every claim below is from the pinned checkout at `C:\Users\mdnss\Dev\gtnh-reference`, read directly
rather than inferred. The GTNH wiki is silent on most of it, which is probably where the belief in
the next paragraph comes from.

### 1.1 Item ports are directed. Fluid ports are not.

The single most consequential finding, and it refutes the intuition that an input "just deposits
into the inventory, so the face does not matter".

```java
// MTEHatchInputBus.java:267-272          ITEM INPUT: front face only
return side == getBaseMetaTileEntity().getFrontFacing() && aIndex != getCircuitSlot() && ...

// MTEHatchOutputBus.java:231-234         ITEM OUTPUT: front face only
return side == aBaseMetaTileEntity.getFrontFacing();

// MTEHatchEnergy.java:60-62              POWER INPUT: front face only
return side == getBaseMetaTileEntity().getFrontFacing();

// MetaTileEntity.java:502-509            FLUID: any side, never overridden by MTEHatchInput
public boolean isLiquidInput(ForgeDirection side)  { return true; }
public boolean isLiquidOutput(ForgeDirection side) { return true; }
```

**Items and power are front-face-only in both directions; fluids are omnidirectional.** A layout
that treats the five non-front faces as interchangeable will form in game and then never move items.

Two corollaries. A fluid *input* hatch can also be **drained** from any side (`isLiquidOutput`
defaults true and is never overridden), so a routed pipe that merely touches an input hatch is a
real footgun. And the auto-maintenance hatch is the one exception that accepts from any side
(`MTEHatchMaintenance.java:433-443`).

### 1.2 Output hatches push, on their front face, by default

The controller never ejects; it only moves results *into* hatches (`MTEMultiBlockBase.java:735-745`).
The eject lives on the hatch's own tick, at its own front facing:

```java
// MTEHatchOutputBus.java:243-251   every 8 ticks
getIInventoryAtSide(aBaseMetaTileEntity.getFrontFacing())
// MTEHatchOutput.java:111-115      every tick, fluids
getITankContainerAtSide(aBaseMetaTileEntity.getFrontFacing())
```

`pushOutputInventory()` is true for the normal bus and false only for ME/void variants. So an output
bus needs one adjacent receiver on its front face, and no pipe is strictly required for a short hop.

### 1.3 Facing is a free variable that GT never checks

`IStructureElement.check` takes no facing parameter (`StructureLib/.../IStructureElement.java:33`),
and every GT hatch overrides `isFacingValid` to `true`. The structure will form with every hatch
facing inward, into the machine, dead. **Correctness is entirely ours.** GT's own survival
auto-builder uses a heuristic worth mirroring: the first face not contained in the structure piece,
preferring a horizontal one (`HatchElementBuilder.java:536-571`).

### 1.4 The muffler needs literal air in front of it

```java
// MTEHatchMuffler.java:186-192
if (getBaseMetaTileEntity().getAirAtSide(getBaseMetaTileEntity().getFrontFacing())) { ... }
```

`getAirAtSide` is the air *block*. A cable, a pipe, a casing or a neighbouring machine in that cell
makes `polluteEnvironment` return false, which shuts the machine down with `POLLUTION_FAIL`
(`MTEMultiBlockBase.java:731, 786-801`). This is a **routing keep-out cell**, a constraint class the
solver has no equivalent for. A muffler is required exactly when `getPollutionPerSecond > 0`
(`:3629-3631`) and is an explicit error on several machines that do not pollute.

### 1.5 Hatches spend the casing budget

`buildAndChain` tries the hatch element first and falls through to the counting casing element only
on failure (`HatchElementBuilder.java:386-394`), so **every hatch placed decrements the casing
count**, and machines assert minimums: `mCasingAmount >= 8` (LCR), `BASE_CASING_COUNT -
MAX_HATCHES_ALLOWED` (Large Fluid Extractor), `CasingInfo.maxHatches` in the newer wrapper API.
Adding one more output bus can un-form a machine. Per-machine caps are near-universal:
`mMaintenanceHatches.size() == 1` appears in at least a dozen `checkMachine` implementations.

### 1.6 Which hatch a product lands in is the machine's choice, not ours

`addOutput` walks the hatch list and takes the first that can store the stack
(`MTEMultiBlockBase.java:1532-1562, 1634-1647`). To pin fluid X to hatch 1 the player must
fluid-lock it; to pin item X to bus 1, item-lock it. **A deterministic build guide has to emit the
lock configuration**, or a routed pipe may carry the wrong product.

### 1.7 Positional semantics are real

A Distillation Tower routes output fluid `i` to layer `i` and requires an output hatch per layer
(`MTEDistillationTower.java:252-276`). An Assembly Line feeds the `n`th input bus from the `n`th
recipe input. The IR needs somewhere to say "this port must be at layer `i`".

### 1.8 Consequence for docs/DOMAIN.md

DOMAIN.md currently says the five non-front faces "can each be input OR output of items or fluids".
That is right for a single-block machine and **wrong for a multiblock's hatches**. It needs a
correction whatever else is decided here.

---

## 2. What the solver models today

- A `Port` is `(id, commodity, direction, rate, max_amps)`. **No position, no face, no hatch kind,
  no block identity** (`ir/input_ir.py:31-59`). `FaceSpec`'s docstring is explicit that face
  assignment is a solver decision.
- `_dock_faces` (`router/_grid.py:37-71`) is the single scan behind all docking. It walks
  `FACE_ORDER` over **every cell of the bounding box** and yields any free outward neighbour. It has
  no notion of a hatch slot, because slot positions never reach the IR.
- `Terminal` records `(machine_id, port_id, face, cell)` where `cell` is just *outside* the footprint
  (`ir/output.py:55-63`). The hosting body cell is therefore already implicitly encoded as
  `cell - FACE_DELTAS[face]`, and the validator already computes exactly that expression
  (`validator/core.py:467-469`). It is simply **unconstrained**. **[Landed, lane 2]** A hatch is now
  recorded explicitly (`PlacedHatch`), and the validator checks it against that same expression.
- `to_physical` reduces the whole slot list to three integers (`dataset/multiblocks.py:320-324`).
  **The offsets are dropped on the floor.** **[Landed, lane 2]** They are carried now, per built
  form, re-anchored from controller-relative to minimum-corner-relative, and reach `Machine`
  as `hatch_slots`.
- The router treats the entire bounding box as one solid obstacle (`router/_grid.py:25-34`), so the
  interior does not exist for routing.

### 2.1 Today's face choice is an arbitrary tiebreak, not a decision

`FACE_ORDER = (SOUTH, NORTH, EAST, WEST, UP, DOWN)` with the comment "south first". The item/fluid
router calls `dock(...)`, which takes the **first** yield, committing before it knows where the
route has to go. The power router already got the upgrade and calls `dock_candidates`. Measured on
the nitrobenzene solve:

```
fluid:  south 15, west 1                  <- dock(), first-fit
power:  south 5, up 3, west 3, down 1     <- route-aware
```

That is why deriving hatch cells from where routing happened to dock would be wrong: it would turn a
tuple ordering into a physical build instruction.

### 2.2 Vertical facings are not an edge case

```
sand         (4 terminals):  up 3, west 1                   -> 75% vertical
nitrobenzene (30 terminals): south 21, up 4, west 4, down 1 -> 17% vertical
```

This matters for textures (section 5), because the previewer's overlay orientation is a *yaw*.

---

## 3. What the dataset already knows, measured

Surveyed across all 208 local controller dumps (`data/2.8.4/multiblocks/`):

| | |
|---|---|
| controllers with hatch-slot data | 185 / 208 (23 record none) |
| `facing_convention` | **one identical string on all 208** |
| externally reachable hatch cells | median **25** per machine |
| hatch slots strictly interior to the bbox | **29%** (5450 / 18995), 49 machines |
| machines with zero externally reachable slots | 3 (all exotic late-game) |
| non-square base (`sx != sz`) | 81 / 208 |
| machines accepting `ExoticEnergy` (TecTech multi-amp) | 33 / 208 |

The facing convention is *"controller front = NORTH (-Z), ExtendedFacing NORTH_NORMAL_NONE; offsets
d = [dx,dy,dz] world-space deltas from the controller block"*. Uniform, explicit, and already
implemented once: `previewer/textures.py:323-374` rotates and translates exactly this way. Prior
art, not new work.

The Industrial Coke Oven is the worked example: 3x3x3, 17 hatch slots, all seven kinds on every
slot, **40 exposed (cell, face) pairs**, and a middle ring (y=1) that accepts no hatch at all, so 8
of its 26 cells are dockable today and must stop being.

---

## 4. The prerequisite: rotation-aware geometry

**[Landed, lane 1.]** `occupied_cells(origin, footprint, orientation)` rotates, the
`_orientations_for` pin is gone, and every machine keeps all four horizontal facings. The section
below is why it had to come first, and is still the right framing for anyone touching that code.

`ir.geometry.occupied_cells` does not rotate; its docstring records the TODO, and
`adapter/core._orientations_for` pins non-square-base machines to one facing because of it. That
affects 81 of 208 machines *today*.

The reframing: the existing TODO is about the **bounding box**, and a square-base machine's box is
rotation-invariant, so rotation genuinely does not matter for it now. The moment individual casing
cells become meaningful, the **contents** must rotate too, which hits **100% of multiblocks**. The
Coke Oven is 3x3x3, gets all four facings today, and its slot at `[-1,0,0]` lands in a different
world cell per facing.

### 4.1 The biggest risk in this document

```python
# validator/_geometry.py:13
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, in_region, occupied_cells
```

The validator **re-exports the solver's own primitive verbatim**. `ir/geometry.py:61-69` already
warns about this in the abstract; hatch placement makes it concrete and universal. A rotation bug
would be mis-modelled identically in the solver and in the only automated correctness gate that
exists to catch solver bugs, and unlike a dataset gap (which fails loud as an infeasibility) it
fails **silently**: a plausible layout, hatches on the wrong faces, validates clean, cannot be
built. This is what ARCHITECTURE.md decision 4 exists to prevent.

Mitigation is cheap but not optional: an independent expansion on the validator side, plus an oracle
property test that re-derives every controller's rotated cell set from the raw dump blocks, per
facing.

**[Landed, lane 1.]** Both exist. `validator/_geometry.body_cells` is written from the dump's stated
convention rather than from the solver's code, a property test holds the two expansions to the same
answer, and the oracle passes for all 208 controllers at all four facings. `FACE_DELTAS` and
`OPPOSITE_FACE` stay shared as *data*, by the same reasoning `validator/core` already gives for
`tier_voltage` and `CABLE_LOSS_PER_BLOCK`; only the derivation is duplicated.

---

## 5. Textures

Better news than expected.

**Already have.** The full local manifest (`data/2.8.4/textures/manifest.json`, 14 MB) carries all
465 hatch/bus MTEs at every real tier, all six sides, both states, with **distinct per-kind per-tier
overlay icons already resolved**: `OVERLAY_PIPE_IN`/`OUT`, `ITEM_IN_SIGN`, `FLUID_OUT_SIGN`,
`OVERLAY_ENERGY_IN_MULTI_2A_<tier>`, `OVERLAY_MAINTENANCE`, `OVERLAY_DUCTTAPE`, `OVERLAY_MUFFLER`.
No extractor work is needed: `TextureDumper` already walks every MTE unfiltered.

**Need to build.**

1. **The committed manifest has zero hatch entries** (30 entries, none a hatch).
   `tools/derive_small_manifest.py` keeps an MTE only if its display name contains an example
   machine-type name, which no hatch can match. Committing ULV-UHV hatches is roughly +327 KB on a
   196 KB base; LV/MV/HV only is about a third of that. This is issue #98's open (a)/(b) fork.
2. **Vertical facing cannot be expressed.** `_rotate_side` (`previewer/textures.py:330`) permutes
   the four horizontal sides and returns UP/DOWN unchanged, and `TextureDumper.java:474` pins
   `aFacing` to NORTH when dumping. Either splice (background from the target side's own layer 0,
   overlay from NORTH's layers 1..n, sound for `MTEHatch` per its `getTexture`) or re-dump across
   six facings at ~6x the volume. **The splice needs checking against ME stocking buses and GT++
   hatches before it is generalised.**

   **[Corrected, 2026-09-01.]** Checked, and the splice is exact rather than approximate:
   `MTEHatch.getTexture` computes its background without referencing `side` or `aFacing` at all, so
   a six-facing re-dump would write byte-identical stacks. Two overriders in the 92-class hierarchy,
   one of them a no-op `super()` call; ME stocking buses and GT++ hatches turn out to be the
   simplest case, not the exception. Two figures here are also wrong: a re-dump is **1.7x** the
   manifest, not 6x, and the five non-front sides do **not** share a background (UP and DOWN differ
   from the horizontals in 500 of 500 hatch entries), so taking the background from SOUTH would be
   wrong on every hatch in the pack. The rule and the pool-key gotcha are in `implementation.md`
   section 7.1.
3. **Per-cube facing.** `BlockCube` carries one `steps` for the whole machine's yaw; a hatch needs
   its own.
4. **`previewer/scene.py` drops `port_id`** when emitting terminals, so the previewer cannot
   currently tell which port a terminal belongs to.
5. **The maintenance hatch's states are inverted** (inactive = `MAINTENANCE + DUCTTAPE` = broken; GT
   sets it active on placement). Rendered with the previewer's `"inactive"` default, every machine
   would show duct-taped.

---

## 6. Design options

Measured compute budget on the nitrobenzene line, which decides this:

```
optimize_placement:  10563.2 ms
route (item/fluid):      3.4 ms
route_power:             9.7 ms
placement : routing  =  809 : 1
```

### 6.1 The assignment problem is small and exactly solvable

For the Coke Oven: 4 routed I/O + 3 energy + 2 upkeep = **9 demands into 17 cells / 40 (cell, face)
pairs**. Raw size is `P(17,9)` times the facing choice, about 10^13 to 10^14: not enumerable.

But it is a **linear assignment problem**. The per-slot cost is separable (hard feasibility: kind
accepted, face exposed, outward cell free, muffler air rule; plus a scalar routing estimate), and the
only pairwise coupling is "two hatches cannot share a cell", which is exactly the one-to-one
constraint a LAP enforces natively. Rectangular Hungarian at `O(n^2 m)` = `9^2 x 40` = **3240
elementary steps**, microseconds. The largest dumped machine (2913 slots, ~12 demands) is still
milliseconds. No LAP solver is available (runtime deps are pydantic only), so this means ~80 lines
of deterministic Hungarian.

### 6.2 The three options

| | Where it lives | Per attempt | Verdict |
|---|---|---|---|
| **A** LAP stage between placement and routing | new module, called from `_assemble` | microseconds | viable, but pins terminals before routing knows better |
| **B** hatch choice as an SA move | `placement/search.py` | ~unchanged | **reject** |
| **C** router negotiates slots | `router/_grid.py` + the negotiation loop | small constant on 13 ms | **recommended** |

**Reject B.** It multiplies the SA state space by ~10^13 per multiblock against an unchanged
1380-iteration budget, and `_cost` computes nothing that hatch choice changes (HPWL is over machine
*centres*), so the hatch dimension would be a free random walk that also dilutes the moves that
matter. `placement/search.py:11-13` already documents exactly this pathology for reorient.

**A is the deterministic warm start, not the whole answer.** Its weakness is that it pins terminals
up front, which is precisely what the power router moved *away* from: `router/power.py` uses
`dock_candidates` + `astar_multi` because pinning one face made the trunk snake around the machine.
Since `solver/core._quality` ranks on power cable cells, that regression would be directly visible.

**C is where the cycles are and where the responsibility already sits.** The router already owns
every other "how does this net physically attach" decision, including auto-output vs pipe and, for
power, route-aware face choice. `astar_multi` already exists and is already used for exactly this.
And PathFinder negotiation *is* a mechanism for allocating a scarce shared resource among competing
nets, which is what a multiblock's casing cells are: price a contested casing cell like a contested
route cell, and a slot is abandoned exactly when the detour is cheaper.

The cost of C is real: terminals stop being fixed for the whole negotiation, which is currently a
documented invariant ("docks are not tradeable and foreign docks are hard", `router/core.py:32-34`).
The salvage subset and the irreducibility proof (`_congestion_is_irreducible`) both have to be
re-derived, along with the property tests pinning order-robustness.

---

## 7. Recommended phasing

Three changes, in order. Nothing hatch-related starts before step 1 is green.

1. **Rotation-aware geometry.** `occupied_cells(origin, footprint, orientation)`, rotated slot
   offsets and faces (lift the working primitives from `previewer/textures.py:323-335`), a genuinely
   independent expansion in `validator/_geometry.py`, and an oracle property test against the raw
   dump per facing. Remove the `_orientations_for` pin and the previewer's unrotated fallback. This
   touches ~28 `occupied_cells` call sites plus ~20 direct `footprint.sx/.sz` reads.
2. **Slot geometry through the contracts.** `HatchSlot` offsets carried **per variant** into
   `MachinePhysical`, then onto `Machine` (INPUT_IR_VERSION 3 -> 4). `LayoutResult` gains
   upkeep-hatch placements (LAYOUT_RESULT_VERSION 0 -> 1), since maintenance and muffler have no net
   and no route and cannot be represented today.
3. **Slot-driven docking plus negotiation.** `_dock_faces` becomes slot-driven and kind-filtered;
   endpoints get candidate sets routed with `astar_multi`; contested casing cells get priced by the
   existing history mechanism; a per-machine claimed-**body**-cell set closes a hole that exists
   today (nothing currently stops two hatches sharing one body cell via two faces). The LAP is round
   zero's warm start and places the never-routed upkeep hatches once the routes are known.

---

## 8. Open questions and risks

1. **`kinds` is a documented lower bound, not a fact.** A hatch adder built from a bare method
   reference exposes no filter, so the cell is recorded without that kind. Measured: 23 of 208
   controllers record no slots at all; of the 185 that do, **61 record no Energy-capable cell** and
   **35 record no Maintenance-capable cell**. `validator/core.py:139-150` already refuses to enforce
   per-kind counts for this reason. Making slot kinds a hard search constraint converts every
   extraction gap into a false infeasibility across roughly a third of the dataset. Needs an explicit
   "unrecorded means permissive" fallback, and ideally an extractor change that distinguishes
   *restricted* from *not recorded*.
2. **Variant/footprint mismatch (a live bug today).** **[Landed, lane 0.]** `hatch_cells` came from
   the *largest* variant while `footprint_for` may reserve a *smaller* one: a Distillation Tower
   reserved 3x6x3 carried `hatch_cells=97`, the 3x12x3 form's count, against 49 for its own.
   `MachinePhysical.variant_for(fluid_outputs)` is now the single selection point and footprint,
   ceiling and slot offsets all come from it.
3. **Auto-output semantics change, and every shipped layout moves with them.** Auto-output for a
   multiblock goes through a specific output hatch's facing, not "any touching body cell" as
   `ir.geometry.auto_output_faces` models. The predicate gets strictly tighter, and `router/auto.py`'s
   "one auto-output per machine" rule is wrong for a multiblock with several output hatches. 13 of
   the 25 nitrobenzene nets currently resolve by auto-output.
4. **Interior slots.** 29% of slots touch no bbox face. They must be excluded from routed I/O
   candidates (they may still legally host a maintenance hatch), or the router must be allowed inside
   the box, which is a much larger change.
5. **Feedback channel.** `failed_nets` is the only signal and it only steers placement. A
   hatch-caused failure (demand exceeds legal slots, muffler with nowhere to vent) is not fixable by
   re-placing that net, so the loop would cycle-detect and return a partial.
6. **Cell == block is currently accidental.** Footprints are derived directly from block extents, so
   one cell is one block today, but ARCHITECTURE.md reserves the right to make a cell larger.
   Block-granular hatch offsets only map 1:1 while that holds. Either ratify it explicitly, or hatch
   assignment acquires a sub-cell placement problem.
7. **Hatch tier is a decision with structural consequences.** `getSlots(tier)` gives 1/4/9/16 slots,
   fluid capacity is `8000 << tier`, and several machines gate hatch tier on glass or coil tier.
8. **Lock configuration must reach the build output** (section 1.6), or routed pipes carry the wrong
   product.

---

## 9. What this unlocks

Beyond correctness: `.schematic` export (issue #96) needs real hatch blocks at real positions with
real facings. Everything in phase 2 above is a prerequisite for it, so this work is not only a
routing-correctness fix.

---

## 10. Decisions (2026-09-01)

Taken by the maintainer after reading sections 1 to 9. Recorded here because the sections above are
deliberately written as findings and options; this is what was chosen. The lane-by-lane execution
of it is [`implementation.md`](implementation.md).

### 10.1 Scope: correctness **and** real hatch textures

Both halves are in. A hatch gets a real cell, a real facing, and a real rendered block. Section 5's
work is therefore in scope, including the two items it flags as unsolved: per-cube facing, and the
fact that `_rotate_side` (`previewer/textures.py:330-331`) cannot express a vertical facing at all
while 75% of sand's terminals and 17% of nitrobenzene's are vertical (section 2.2).

The consequence to plan around: **the vertical-facing texture splice is the single largest unknown
in the whole body of work.** It should be de-risked early, out of order if necessary, because the
fallback (re-dumping across six facings at roughly 6x the manifest volume) changes the cost of the
dataset lane rather than the previewer lane.

### 10.2 Approach: route-aware docking, then legalize

Not option C (router negotiates casing cells), and not option A (LAP pins terminals up front).
Instead a fourth option, arrived at from the maintainer's reading:

> after a route is made, check whether a hatch can legally be placed there, and try a nearby slot
> if it cannot.

Adopted **with one precondition**: item and fluid docking must move to `dock_candidates` before the
legalize step exists. Section 2.1 is the reason. `dock()` commits to the first free face in
`FACE_ORDER` (`router/_grid.py:20, 74`), which is why fluid docking measures south 15 / west 1,
and legalizing that choice after the fact would promote a tuple ordering into a physical build
instruction. The power router already made this move (`dock_candidates`, `:86`); the item and fluid
routers have not.

Why this over option C: it keeps the documented invariant that docks are fixed for the duration of
a negotiation (`router/core.py:32-34`), so neither the salvage subset nor
`_congestion_is_irreducible` has to be re-derived. Option C stays the better final answer and is
not foreclosed; this is the cheaper path to a correct v1, and lane 3 of the implementation is a
prerequisite for either.

Known limitation, to be surfaced rather than worked around: **a repair loop cannot fix a global
constraint.** The casing budget (section 1.5) is per machine, not per cell, so a machine demanding
more hatches than its structure can host must be reported as an explicit infeasibility. Retrying a
nearby slot will never resolve it, and neither will re-placing the net, since `failed_nets` only
steers placement (section 8.5).

### 10.3 Upkeep hatches are in v1

Maintenance and muffler are placed, not deferred. This forces the `LAYOUT_RESULT_VERSION` 0 to 1
bump (they belong to no net and cannot be represented today) and brings the muffler's air rule
(section 1.4) in as a genuine routing keep-out cell.

The argument for including them rather than deferring: deferring leaves the casing budget
optimistic by up to two slots per machine, and the casing budget is precisely the constraint that
silently un-forms a multiblock. `MachinePhysical.upkeep_hatch_count`
(`dataset/multiblocks.py:137`) and `energy_hatch_budget` (`:139-150`) already reserve for them
arithmetically, so the data is there; what is missing is a place in `LayoutResult` to put them.

### 10.4 The three constraints from the maintainer's read

Recorded because they are the acceptance criteria a reviewer will check against.

1. **A hatch must not face into the multiblock.** Section 1.3 is why this is entirely ours: GT never
   validates facing. Stronger than stated, though: for items and power the face must also be
   *exposed* and have the receiver on it, because those are front-face-only in both directions
   (section 1.1). Fluids are the only omnidirectional case, and even they carry the drain footgun
   from the same section.
2. **A hatch will not auto-output unless the receiver is on its face.** Correct, with two
   refinements: it is the *hatch's own* front facing that matters, not the controller's
   (section 1.2), and no pipe is strictly required for a short hop, since the output bus pushes
   directly into an adjacent inventory every 8 ticks. Combined with GT pipes not self-connecting
   (the `pipe-connections-player-controlled` memory), the build guide has to state which side to
   wire.
3. **Legalize the hatch after routing, retry nearby.** Adopted as 10.2.

### 10.5 Estimate

Roughly **9 to 16 focused sessions across 5 or 6 PRs**, sized against comparable landed lanes
(`3cfc31c` at 24 files, +1601/-113; `6504801` at 24 files, +2759/-1157). About half the cost is in
lanes 1 and 2, which are prerequisites under any approach and have nothing to do with hatch choice.

### 10.6 One correction to section 8

Open question 2 (the variant/footprint mismatch) is genuinely independent and should be fixed
first, as it says. Open question 3 (auto-output semantics) is **not** independent, despite reading
that way: the tighter predicate is "the output hatch's own facing", which cannot be written until
hatches have facings. It belongs in the assignment lane, not before it.

---

## 11. Where this stands (2026-09-02)

### 11.1 Landed

Three lanes are on `main`, each merged after a full green run of the gates. The suite is at 462
tests and 98% coverage.

| Lane | Commit | What it did |
|---|---|---|
| 0 | `306e3f6` | A machine's hatch ceiling is charged to the form it reserves, not the largest one (section 8, question 2) |
| 1 | `aa2c1b8` | Rotation-aware geometry, and the validator stops sharing the solver's expansion (section 4) |
| 2 | `5e436f5` | Hatch slots reach the IR; a layout records every placed hatch (`LayoutResult` v1) |

What that leaves in place for the assignment lane:

- `Machine.hatch_slots` carries every hatch-capable casing cell, as an offset from the machine's
  unrotated minimum corner plus the kinds it accepts. Measured on nitrobenzene: Chemical Plant 23,
  Distillation Tower 25 and 49, Coke Oven 17, Large Chemical Reactor 25.
- `ir.geometry.rotated_slot(offset, footprint, orientation)` turns a slot with its machine and
  re-anchors it, property-tested to map a machine's own cells exactly onto `occupied_cells` and to
  stay injective.
- `LayoutResult.hatches` holds a `PlacedHatch` per hatch, routed and upkeep alike, and the validator
  already rejects one that is off its machine, faces inward, shares a cell, or disagrees with its
  port's terminal.
- Nothing emits a hatch yet. Every `hatches` list is empty.

### 11.2 What to do next

Lanes 3 to 5 of [`implementation.md`](implementation.md), in that order. Lane 3 is small,
standalone and a hard precondition for lane 4's legalize step: move item and fluid docking onto
`dock_candidates`, so the legalize step repairs a route-aware choice rather than promoting the
`FACE_ORDER` tiebreak (section 2.1) into a build instruction.

Only the *structural* half of the hatch rules is enforced. The policy half is lane 4: a face that is
outward must also be **usable** - for items and power the receiver has to be on it, because those
are front-face-only in both directions (section 1.1) - plus the muffler keep-out, the casing budget
as an explicit infeasibility, and the auto-output tightening (section 8, question 3).

### 11.3 Things learned since the spike that are not in sections 1 to 9

- **Lane 2 was smaller than section 7 assumed, and lane 1 larger.** `HatchSlot` already existed in
  the dataset schema and the counts were already derived from it; only the offsets were dropped. Lane
  1, by contrast, had four sites needing work that no type error would have found, the worst being
  `_reorient`, which performed no geometry check at all and would have started accepting overlapping
  states in silence.
- **Only `LayoutResult` bumped.** Section 7 said both contracts would. The rule in `ir/__init__.py`
  is that additive fields do not bump, so `Machine.hatch_slots` did not; `LayoutResult` did, because
  a consumer that ignores `hatches` renders a build with no maintenance hatch and no muffler, which
  is breaking by omission even though nothing raises.
- **`_cost` is hot enough to notice a constructor.** Rotating inside it built a pydantic model 407k
  times per sand solve, and testing the fit once per orientation in `_best_insertion` quadrupled
  that work: together 2.5x on a solve, caught only because the suite went from ~540s to 862s. It
  needed a square-base short-circuit and a per-rotated-box memo to come back under baseline.
  **Lane 4 adds per-slot work on that same path** - measure it.
- **Making `orientation` a required argument is what made lane 1 tractable.** `mypy --strict`
  enumerated all 19 call sites instead of a human hunting them. Worth repeating for any change to a
  primitive this widely shared.
- **The RNG trajectory can be preserved deliberately.** Picking the orientation before testing cells
  reorders nothing random, so both shipped examples came through lane 1 byte-identical. That is why
  any future movement in those pins is a regression signal rather than an expected re-baseline.
- **CI carries only the two committed fixtures**, so every nitrobenzene machine falls back to 1x1x1
  there and the line cannot solve valid. A test that asserts otherwise passes locally and fails in
  CI; `test_cli_solves_nitrobenzene` did exactly that for weeks. See the new section in
  [`../TESTING.md`](../TESTING.md). Lanes 4 and 5 will want dataset-dependent tests, so read it
  first.

### 11.4 Still open, unchanged

Section 8's questions 1 and 3 to 8 all stand. Question 1 (kinds are a lower bound) is now documented
at every level the data passes through, but nothing enforces the permissive fallback yet, because
nothing filters by kind yet - that arrives with lane 4 and is the single easiest way to manufacture
false infeasibilities across a third of the dataset.

Also unrelated to this work but unscheduled, from the `factory-flow-upstream` spike: the adapter
does not model `nodes[].recipeInputOverrides` (both shipped fixtures carry it), and `schema_version`
has no producer guard. Both are live defects today.
