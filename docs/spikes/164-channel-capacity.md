# Spike: channels-per-edge capacity (issue #164)

**Status:** design only, nothing implemented. Read this before touching
`src/gtnh_solver/validator/core.py`.

**Why a spike.** #164 changes the capacity rule in the validator, which `CLAUDE.md` calls the only
*automated* correctness gate. A wrong capacity check does not fail loudly: it silently certifies
layouts that cannot be built. So the rule gets reviewed before code.

Every claim below names the file and line it came from. Where the code did not tell me, I say so
rather than guess; section 6 is the list of things I could not settle.

GT line numbers are against `../gtnh-reference/GT5-Unofficial`, pinned at tag **5.09.51.482**
(see `CLAUDE.local.md`, "GT source reference"). Re-check them after a pack bump; the class and
method names are the stable part.

---

## 0. The headline, first, because it changes what should be built

I reproduced the evidence in #164 with the repo's own reader and then decoded the reference's
actual topology. **The reference build does not share a pipe block between two nets.** Its 12 item
pipes are four disjoint runs of three blocks, one run per net. What it *does* do, and what we
cannot express, is put **several terminals of one net on one pipe block**.

That is a different constraint from the one #164 names, it lives in a different file, and it is
already legal under today's validator.

| | reference | ours |
|---|---|---|
| item pipe cells | 12 | 40 |
| item **terminals** on those cells | 20 | 20 |
| terminals per cell | 1.67 | 1.00 |
| cables / power terminals | 3 cells, 10 terminals | 23 cells, 10 terminals |

The power router already does this (a "tap", `router/power.py:398-405`) and gets 3 cable cells for
10 terminals on the reference placement, *exactly* matching the hand build. The item router forbids
it (`router/core.py:233`, `:275`, `:600`, `router/_grid.py:167`) and pays 40 cells for 20 terminals.

So: **power is the precedent, not a special case, and the gap is a docking rule rather than a
capacity rule.** The capacity model #164 asks for is still worth designing - it is the next
constraint behind this one - but it is not what closes 40 against 12, and shipping it first would
change the validator's soundness argument for no measured gain (section 5 has the number: relaxing
capacity alone moves parallel-sand from 40 to 40).

---

## 1. Evidence, reproduced

Decoded with the repo's reader, never by hand-parsing NBT:

```
gtnh-solve --inspect-schematic tests/golden/schematic/sand-parallel-reference.schematic
gtnh-solve examples/gtnh-parallel-sand.json --schematic out/ours.schematic
gtnh-solve --inspect-schematic out/ours.schematic
```

```
reference   3x3x4 = 36 cells,  27 solid:  9 mID 611, 6 mID 5592, 6 mID 5593,
                                          2 mID 1249, 1 mID 1250, 2 mID 135, 1 mID 15498
ours       11x4x5 = 220 cells, 75 solid: 40 mID 5591, 18 mID 1247, 9 mID 611,
                                          3 mID 1246, 2 mID 1248, 2 mID 135, 1 mID 15498
```

`5589` is the first Tin item pipe id and the sizes run tiny, small, medium, large, huge
(`GT5-Unofficial/src/main/java/gregtech/loaders/preload/LoaderMetaPipeEntities.java:777-781`,
`:1369-1430`). So the reference is 6 **large** (5592) plus 6 **huge** (5593) and ours is 40
**medium** (5591), which is the gauge half and belongs to #165, not here.

### The reference's real topology

Dumped per cell with `schematic.read.read_schematic`. `mFacing` is the auto-output face for a basic
machine (`MTEBasicMachine.java:597-619` ejects through `getFrontFacing()`), and `mMainFacing` is the
no-I/O face that our `Placement.orientation` models.

```
y=0   pipeB  HAMMER1 pipeA        y=1   .    cable  HAMMER2      y=2   pipeB  HAMMER3 pipeA
      pipeB  HAMMER1 pipeA              .    cable  HAMMER2            pipeB  HAMMER3 pipeA
      pipeB  HAMMER1 pipeA              .    cable  HAMMER2            pipeB  HAMMER3 pipeA
      CHEST    .       .                .    GEN      .                CHEST    .       .
```

- `(0,0,3)` holds `minecraft:stone` and a cover on side 2 (NORTH): it is the **input** chest, and it
  pushes into the `x=0, y=0` run. That run is the **stone** net, 3 cells, 4 terminals.
- `x=2, y=0` is the **cobblestone** net: stage-1 hammers eject EAST into it, stage-2 hammers take it
  from DOWN. 3 cells, 6 terminals.
- `x=2, y=2` is the **gravel** net: stage-2 ejects UP, stage-3 takes it from EAST. 3 cells, 6
  terminals.
- `x=0, y=2` is the **sand** net: stage-3 ejects WEST, the output chest at `(0,2,3)` takes it from
  NORTH. `(1,2,0)` holds 39 `minecraft:sand` in an output slot, which is how stage 3 is identified.
  3 cells, 4 terminals.

Four nets, four disjoint 3-cell runs, no cell carried by two nets. The two "columns" of #164's
prose are two *x* positions used at two *y* levels by two different nets, separated by a machine.

### The current gate already certifies that build

I hand-built the reference as a `LayoutResult` (placements from the schematic, the four 3-cell
routes above, power from `route_power` on that placement) and ran `validator.validate`:

```
ITEM PIPE CELLS: 12   POWER CELLS: 3
VALIDATOR ok? True   violations: 0
```

`route_power` on the reference placement independently produced `(1,1,0) (1,1,1) (1,1,2)`, the exact
3 cables of the hand build, because taps are already allowed.

So `_check_route_capacity` (`validator/core.py:1385`) is **not** what stands between us and the
reference. What stands between us and it is:

1. `router/core.py:233` `docked: set[Cell] = set()` accumulated at `:275` and consulted in
   `_grid._dock_faces:167` (`cand in docked`), which makes every item/fluid terminal cell unique
   **across all nets**; and
2. `router/core.py:600` `taken`, filtered in `_free` (`:624-637`), which makes them unique **within**
   a net too.

Routing the reference placement with today's router fails outright
(`net 'edge-3ba1a1b2...' has no free cell path between its terminals`). Relaxing only (2) - a
monkeypatched probe, not committed - routes the stone net in exactly the reference's 3 cells and
routes all four item nets.

3. And then `placement/search.py`. `_face_shortfall` (`:557`) charges one free adjacent cell per
   port (`:615`) and treats a cell two machines could both reach as hostable by only one (`:625`).
   On the maintainer's own hand build it reads **10.00**, weighted `_W_FACES = 8.0` (`:104`) to
   **80.00**; on our sprawled 220-cell answer it reads **0.00**. The placement cost actively
   forbids the reference geometry, and that weight was tuned by measurement (`:96-103`), so it is
   not an accident to be quietly lowered.

---

## 2. The capacity model

### What a capacity IS

**A per-cell integer count of transport slots, defaulting to 1, with a per-commodity-family
partition.** Not per edge, not derived from pipe gauge.

Concretely, for a cell `c`:

```
capacity(c) = margin_channels                     # how many parallel channels the routing
                                                  # margin fits in this cell; 1 today
usage(c)    = |{ net : net occupies c }|          # cross-net occupancy
family(c)   = the one PipeFamily built at c       # item_pipe | fluid_pipe | cable
```

and the invariant is `usage(c) <= capacity(c)` **and** every net at `c` maps to `family(c)`.

Three reasons for this shape rather than the alternatives.

- **A count, not a boolean, is the whole ask of #164**, and `docs/ARCHITECTURE.md:226-234` already
  frames it as "margin -> channels-per-edge": the coarse cell is a block plus routing margin, and
  the margin is what fits *n* parallel runs. A cell is the right home because
  `route_blocks.route_cells` already resolves a route to exactly one block per cell
  (`route_blocks.py:125`), and `schematic/core.py:232-237` lowers one block per cell. Edge capacity
  would have no consumer.

- **Not derived from pipe gauge.** A fatter GT pipe carries more items per tick through *one*
  block; it does not give the block a second independent lane. `docs/DOMAIN.md:249-259` has one
  thickness per family per size and `docs/DOMAIN.md:100` says v1 ships single-channel pipes, with
  GT++ quadruple/nonuple fluid pipes (real multi-channel blocks) deferred to v1.1
  (`docs/ROADMAP.md:132`). So gauge is throughput (#165) and capacity is geometry (#164). They are
  independent axes and must not be joined, or a throughput upgrade silently becomes a
  realizability claim.

- **Per-family partition, not per-commodity count.** One cell is one block
  (`route_blocks.py:259-263`, "Counted per **cell**, not per route: one cell is one block"). An
  item pipe and a cable cannot be the same block. So capacity is not "3 nets of any kind"; it is
  "up to *n* nets, all of one family". `PipeFamily` (`ir/enums.py:20-31`) is the right axis and is
  already separate from `Commodity` for exactly this reason.

### Where it is stored

**Nowhere new in the output contract.** Capacity is an input/derived property, not a solution
property:

- the per-cell channel count is a function of the **routing margin**, which is a dataset/adapter
  concept (`adapter/core.py:941` already sizes the region by a margin factor). It belongs beside
  the cell-grid definition, as a single `CHANNELS_PER_CELL` rule datum exported from `dataset/`
  the way `CABLE_THICKNESSES` is (`ir/output.py:25-37` explains why the ladder lives in `ir` and
  is re-exported by `dataset`; do the same here, or put it in `dataset` outright since nothing in
  `ir` needs to validate against it);
- the *usage* is already derivable from `LayoutResult.routes` and both the router and the
  validator compute it independently today (`router/core.py:286` `usage`,
  `validator/core.py:1397-1401` `owners`). Publishing it in the contract would let the validator
  read the router's own tally instead of re-deriving it, which is exactly the shared-logic gate
  `ARCHITECTURE.md` #4 rules out.

### Is `Segment.channel` the right home?

**It is a false friend. Do not use it as the capacity model, and consider deleting it.**

`Segment.channel` (`ir/output.py:75-82`) is written as the literal `0` in exactly two places
(`router/core.py:676`, `router/power.py:519`) and **read nowhere** in `src/` or `tests/`. Its
docstring says "on a given channel (< the per-edge channel cap)".

The problem is that a channel *index* is a much stronger claim than capacity needs, and it is the
wrong claim:

- a channel index asserts a **stable lane assignment along a run**. Nothing in GT gives a
  single-channel pipe lanes; "channel 1 of 3" would name a second pipe block in the margin, which
  is a different *cell* under the coarse grid, not a different channel of the same cell. The
  abstraction would be inventing sub-cell geometry that `docs/ARCHITECTURE.md:226-234` deliberately
  defers to export;
- a per-segment field cannot express the constraint anyway. The constraint is per **cell**, and a
  cell is the shared endpoint of up to six segments belonging to different routes. Checking
  `channel` collisions per segment would miss the cell where two routes merely *touch* endpoints,
  which is precisely the case `_check_route_capacity` catches today by unioning `seg.start` and
  `seg.end` (`validator/core.py:1400-1401`);
- and a producer-supplied index is unverifiable. The validator would be checking that the router's
  own labels do not collide, not that the geometry fits. That is the shared-logic failure mode
  `ARCHITECTURE.md` #4 exists to prevent.

The honest capacity check counts occupancy per cell and compares it to a rule datum. It needs no
field on `Segment`. **Recommendation:** leave `channel` alone in this change (removing it is a
`LAYOUT_RESULT_VERSION` bump for no benefit), but correct its docstring to say it is unused and
reserved, and fold its removal into whatever contract bump comes next. Do not build on it.

### IR contract impact

**None, for the capacity model itself.** No new field, no `LAYOUT_RESULT_VERSION` bump
(`ir/output.py:23`). A layout that today has `usage(c) == 1` everywhere is still a legal layout
under a capacity of *n*; the contract does not change, only what the gate accepts.

The one contract question the *terminal-sharing* half raises is discussed in section 3: today
nothing in the schema says two terminals may share a cell, and nothing says they may not. It is
already ambiguous, and power routes already rely on the permissive reading
(`router/power.py:398-405`, certified by `_check_terminals` at `validator/core.py:880` which
checks one-terminal-per-*endpoint*, never one-terminal-per-*cell*). Make it explicit in the
`Terminal` docstring and in `docs/IR.md`; that is documentation of existing behaviour, not a bump.

---

## 3. What the validator asserts instead, and why it is still sound

This is the section to read hardest.

### Today

`_check_route_capacity` (`validator/core.py:1385-1412`): build `owners: Cell -> set[net_id]` over
every segment endpoint of every route; flag `ROUTE_CELL_COLLISION` when `len(owners[c]) > 1`.

Note what it already permits: **one net may own as many cells as it likes, and may have several
terminals on one cell.** It is per-net, so a power trunk's taps pass. Note also what it does *not*
check: nothing anywhere asserts that two terminals belong to different machine faces, for a machine
with no `hatch_slots` (`validator/core.py:545` skips exactly those, and `:502` is the same
abstention in `_hatch_hosts`).

### Proposed

Replace one assertion with three. All three are computable from `LayoutResult` + `InputIR` alone,
which keeps the gate independent (`ARCHITECTURE.md` #4).

**A. Cell occupancy is under capacity.**

```
for each cell c: |{ route.net_id : c in route.cells() }| <= CHANNELS_PER_CELL
```

Sound because the coarse cell is one block plus margin and the margin is what a channel count is
*defined* as. It degenerates to today's rule at `CHANNELS_PER_CELL == 1`, so landing the code at 1
is a provable no-op, which is how it should ship (section 5).

**B. A cell is built from one transport family.**

```
for each cell c: |{ _FAMILY_FOR[route.commodity] : c in route.cells() }| == 1
```

New code, `ROUTE_CELL_FAMILY_CONFLICT`. Sound because `route_blocks.route_block_counts`
(`route_blocks.py:259`) and `schematic/core.py:232-237` each lower a cell to exactly one block; an
item pipe and a cable in one cell is not a tight fit, it is two blocks in one block. Today this is
implied by A at capacity 1; at capacity > 1 it must be stated, or the gate certifies a cable and a
pipe in the same block.

**C. Two connections of one machine may not contend for one block.**

```
for a machine WITHOUT hatch_slots:
    for each (machine_id, terminal.cell): at most one terminal, across ALL routes
for a machine WITH hatch_slots: already checked, casing cell, validator/core.py:540-568
```

New code, `TERMINAL_FACE_CONTENTION`, and it is deliberately the **same split** `_grid.claim_key`
makes (`_grid.py:68-82`: the casing cell for a multiblock, "For a machine with no recorded slots it
is the dock cell instead"). Re-derive that split in the validator rather than importing
`claim_key`: it is checking *logic*, and `ARCHITECTURE.md` #4 shares only rule data.

The slot-less half is **missing from the validator today** - `_check_terminal_hatch_cells` skips
those machines outright (`validator/core.py:545`), on the reasoning at `:531-534` that a single
block genuinely takes input on one face and output on another. That reasoning is right about
*different* faces and says nothing about the *same* one. It does not matter today because the
router's global `docked` set (`router/core.py:233`, `:275`) makes the case unreachable. The moment
terminal sharing is allowed it becomes reachable, so this has to be written before the router is
relaxed, not after.

Sound because for a slot-less machine one dock cell is one face of one block (that is exactly why
`claim_key` uses it), and one face does one thing: GT accepts input on any side but `mMainFacing`
and, unless `mAllowInputFromOutputSide`, the auto-output side (`MTEBasicMachine.java:959-961`;
`mAllowInputFromOutputSide` is `0` on every hammer in the reference build's NBT). Two connections
on one face of one machine is not a tight fit, it is a build that does not run.

Note the shape of what C permits, because it is the whole point: two terminals on one cell
belonging to **different machines** stay legal. That is the reference build - the input chest's
terminal and a hammer's terminal both on `(0,0,2)`, one pipe block wired to two neighbours - and
`_check_terminals` already certifies it (`validator/core.py:880`).

### What could now be certified that should not be

This is the cost side, stated plainly.

1. **Two item nets merged into one physical pipe network (rule A at capacity > 1).**
   This is the real hazard and it is not geometric. A GT item pipe delivers to *any* connected
   inventory that accepts the stack (`MTEItemPipe.java:303-340`, `sendItemStack` iterates the
   connected sides and `insertItemStackIntoTileEntity` calls `GTUtility.moveMultipleItemStacks`
   into whatever is there). The receiver's `allowPutStack` decides, and on a basic machine that is
   `mDisableFilter || allowPutStackValidated(...)` (`MTEBasicMachine.java:965`) with
   `mDisableFilter = true` by default (`:121`, overridden only by a player preference at `:474`).
   Every hammer in `sand-parallel-reference.schematic` carries `mDisableFilter = 1`.

   So two item nets sharing a run **will** cross-feed: gravel can land in a machine the plan wired
   for cobblestone. Rule A cannot see this, because `Port` carries `id`, `commodity`, `direction`,
   `cover`, `rate`, `max_amps` and **no resource identity at all**
   (`ir/input_ir.py:40-66`). The IR literally cannot tell whether two item nets carry the same item.

   The mitigation must be a rule, not a hope. Pick one and write it down:
   - **(preferred, and what this spike recommends)** capacity above 1 applies only to nets of the
     same commodity that are **provably the same flow**, which the IR cannot express today, so
     **cross-net capacity stays at 1 for items and fluids** until the IR carries a resource key.
     Fluids are worse, not better: `docs/DOMAIN.md:98` is one fluid type per line, and
     `docs/DOMAIN.md:47-50` notes a pipe merely touching a fluid input hatch can drain it;
   - or model the filter cover a merged run would need, which means the build guide and the
     export emit it and the hatch/face budget pays for it. That is a much larger change and it is
     not what the reference build does.

2. **A cell holding a pipe and a cable (without rule B).** Covered above.

3. **Two hatches of one machine on one block (without rule C).** Covered above, and note that the
   existing `TERMINAL_HATCH_CONTENTION` check (`validator/core.py:540-568`) covers only machines
   *with* `hatch_slots`, so C is not a duplicate of it, it is the missing half.

4. **Muffler keep-out.** `_check_upkeep_hatches` builds its occupancy with
   `occupied |= route.cells()` (`validator/core.py:833`). Sharing does not weaken it - a shared
   cell is still occupied - so nothing to do, but re-read it when the code changes, because a
   refactor that switched it to per-net sets would silently break the air-in-front rule.

---

## 4. Call sites that change, by file and function

### Must change for capacity (rule A and B)

| File / function | Line | What changes |
|---|---|---|
| `validator/core.py::_check_route_capacity` | 1385 | count owners per cell against `CHANNELS_PER_CELL` instead of `> 1`; add the family check |
| `validator/report.py::ViolationCode` | 40 | add `ROUTE_CELL_FAMILY_CONFLICT` (and `TERMINAL_FACE_CONTENTION` for rule C) |
| `dataset/` (new rule datum) | - | `CHANNELS_PER_CELL`, beside `CABLE_THICKNESSES`; one source for router and validator, as `ir/output.py:25-37` prescribes |
| `router/core.py::_negotiate` | 191-378 | the convergence test is `users > 1` (`:320`); it becomes `users > capacity`. The price term `_PRESENT_PENALTY * users` (`:302`) becomes a function of `max(0, users - capacity)` rather than `users` |
| `router/core.py::_congestion_is_irreducible` | 681-721 | its proof is "two nets are *forced* onto this cell, so it stays over-used". At capacity *n* it must be "*n+1* nets are forced" (`forced >= 2` at `:718` and `forced < 2` at `:720` become comparisons against `capacity`) |
| `router/power.py::_route_pass` | 266-313 | `obstacles.update(built.cells())` (`:311`) makes a finished trunk hard for every later net, as does `obstacles = ... | set(extra_obstacles)` (`:288`). Under capacity both must be counts, not walls |
| `router/core.py::route` | 134-186 | `reserved` (`:138`) is a hard cell set; under capacity a reserved cell is a cell with one slot already spoken for, not a wall |
| `solver/core.py` | 307 | `extra_obstacles={cell for r in routing.routes for cell in r.cells()}` hands the power router the pipe cells as a flat set. It needs to hand over *how much* of each cell is spent |
| `route_blocks.py::route_block_counts` | 259-274 | today it keeps **one** `RouteCell` per cell (the thicker) and silently drops the other route's `dirs`. Under sharing the mask must be the **union** of the routes' connection directions, and the family must be reconciled (rule B makes that well-defined) |
| `schematic/core.py` (grid lowering) | 232-237 | `grid[key] = cell` - last route wins, silently. Must reconcile, or refuse |
| `previewer/scene.py` | 187 | emits `route_cells(route)` per route, so a shared cell is emitted twice and draws two overlapping crosses |

### Must change for terminal sharing (the slice that actually moves the number)

| File / function | Line | What changes |
|---|---|---|
| `router/core.py::_dock_net` | 544-620 | `taken` (`:600`) stops two ports of **one net** co-locating. Relax it to permit co-location while keeping `mine` (the per-machine `claim_key` set), which is what preserves rule C |
| `router/core.py::_free` | 624-637 | the `t.cell.as_tuple() not in taken` filter (`:634`) is the exact line |
| `router/core.py::_negotiate` | 233, 249, 275 | `docked` is global across nets. For the minimal slice it **stays** global (cross-net dock sharing is not needed for the reference) |
| `router/core.py::_lay_legs` | 659-679 | two consecutive terminals on one cell make `astar(a, a, ...)` a zero-length leg contributing no segment. Verified safe by probe, but it must be a test: a route whose *every* leg is zero-length has no segments and fails `ROUTE_DISCONTINUOUS` (`validator/core.py:384`) |
| `validator/core.py` (new `_check_terminal_faces`) | - | rule C above, and it must land in the **same commit** as the `_free` relaxation, not after it |
| `placement/search.py::_face_shortfall` | 557-630 | demand is one cell per port (`:615`) and a contested cell hosts one machine (`:625`). Both are false once terminals share. Without this the router relaxation is measurably worthless (section 5) |
| `placement/search.py::_dockable_cells` | 522 | unchanged, but it is the input `_face_shortfall` divides |

### Read, confirmed unaffected

- `validator/core.py::_check_terminals` (`:880`) already permits terminals to share a cell; it keys
  on `(machine_id, port_id)` (`:908-931`).
- `validator/core.py::_check_upkeep_hatches` (`:833`) - see section 3 item 4.
- `router/_grid.py::claim_key` (`:68-82`) - already the right per-machine contention unit for both
  the multiblock and the single-block case. Do not touch it.
- `buildguide/core.py:332-336` builds an ASCII slice with `setdefault`, so a shared cell shows the
  first net's character. Cosmetic, and the text guide is on hold.

---

## 5. Minimal vertical slice

### What it is

**Let two terminals of one net share a dock cell, with the validator gaining rule C in the same
commit, and the placement cost's face-shortfall term learning that a cell can host several
same-net connections.**

That is three edits:

1. `router/core.py:634` - drop the `taken` filter in `_free`, keep `mine`.
2. `validator/core.py` - new `_check_terminal_faces`: no two terminals of one machine on one dock
   cell, across all routes. Plus `TERMINAL_FACE_CONTENTION` in `validator/report.py`.
3. `placement/search.py:615,625` - `_face_shortfall`'s demand becomes "one cell per **net** a
   machine touches" rather than "one cell per **port**", and a contested cell is shared only among
   machines that need it for *different* nets.

It ships with the validator, not behind it (`docs/TESTING.md:3`, tests ship with the code), and it
changes **no** capacity rule, so `_check_route_capacity` and the whole cross-net soundness argument
in section 3 stay exactly as they are. That is the point: it buys the measured win without spending
the gate.

Capacity itself (rules A and B, `CHANNELS_PER_CELL` wired in at **1**) is a good second commit: it
is a provable no-op at 1, it puts the rule datum and the family check in place, and it leaves the
value to be raised by a later change that can also answer the cross-feed question.

### How to measure it

Three numbers, in increasing order of what they prove.

1. **Fixed-placement, the honest ceiling.** Route the reference's own placement and count item
   cells. Today: the item router fails outright. With edit 1 alone: all four nets route, the stone
   net lands on the reference's exact 3 cells. Target: 12. Build this as a **golden test**: pin the
   12 placements read off `sand-parallel-reference.schematic` (machine cell, and `mMainFacing` as
   `Placement.orientation`), narrow `bounding_region` to `CellBox(sx=3, sy=3, sz=4)`, call
   `router.route` then `router.route_power`, and assert the item cell count. It is the only
   measurement that isolates the router from the annealer.
2. **Full solve, `examples/gtnh-parallel-sand.json`.** Item route cells, currently **40**,
   reference **12**. Measured baseline and with edit 1 alone: **40 and 40**. The relaxation alone
   changes nothing, because the annealer never chooses a geometry that could use it. Edit 3 is what
   makes the number move, and until it does the slice is not done.
3. **No regression on `examples/gtnh-sand.json`**: 0 item cells, 4 auto-connections, 3 power cells
   today. That line is fully auto-fed and must stay so; `_W_FACES` was tuned partly on it
   (`placement/search.py:96-103`).

Run the seed grid, not one seed - `placement/search.py:99-103` says the weight was mis-tuned once
exactly that way - and run `GTNH_TEST_HYPOTHESIS_FRACTION=1.0 pytest --hypothesis-show-statistics`
before pushing, per `docs/TESTING.md:148-158`, reading the outcome mix and not just the exit code.

---

## 6. Risks, and what I could not determine

**R1. The soundness risk, restated as the top one.** Raising `CHANNELS_PER_CELL` above 1 for items
or fluids certifies cross-feeding, and the IR cannot see it (`ir/input_ir.py:40-66`, no resource on
`Port`). Everything in section 3 item 1. This is the single thing a reviewer should refuse if the
implementation smuggles it in.

**R2. `_face_shortfall` is a heuristic and its own docstring says so**
(`placement/search.py:600-606`: "the real question is whether a system of distinct representatives
exists ... and settling that means a matching, which is far too slow per annealing step"). Edit 3
makes the demand side *smaller*, which relaxes a gate that exists to stop #76's crowding. It could
reintroduce false-feasible placements that the router then fails on. Mitigation: the exact gate
`placement.feasibility` still rules, and the solver loop only keeps VALID attempts
(`solver/core.py`), so the failure mode is wasted search rather than a bad certificate. Measure
solve time as well as pipe count.

**R3. I could not determine what the routing margin actually is, in cells.**
`docs/ARCHITECTURE.md:228` says "cell = largest common single-block footprint + routing margin" and
`adapter/core.py:941` sizes the *region* by a 4x factor, but I found nothing that states how many
parallel channels one cell's margin fits. `CHANNELS_PER_CELL` therefore has no principled value
above 1 yet. That is a dataset/DOMAIN question for the maintainer, and until it is answered the
correct value is 1.

**R4. I could not determine whether the reference build actually runs.** It is a saved snapshot;
every pipe in it has `mConnections = 0`, which is player state rather than a block property
(`docs/DOMAIN.md:261-265`), so the snapshot carries no wiring to read. The topology I reconstructed is from geometry, facings, covers and inventory
contents, and it is self-consistent (stone in the input chest with a NORTH cover, 39 sand in a
stage-3 output slot), but I have not seen it run and there is no headless simulator
(`docs/TESTING.md:5-9`).

**R5. The measurements were taken on the committed fixtures**, so every machine is a slot-less
1x1x1 (`docs/TESTING.md:64-76`). The Forge Hammer is a single block either way, so parallel-sand is
unaffected, but the `hatch_slots` branches in `_check_terminal_hatch_cells` were not exercised. A
line that resolves real multiblocks may behave differently and should be measured before the
capacity commit lands.

**R6. `router/power.py::reserve_power_docks` (`:113-171`) grabs `candidates[0]` in `FACE_ORDER`.**
On the reference placement that reserves `(2,0,0)` for a stage-1 hammer's energy hatch, which is
the cell the cobblestone net needs, and the item route then fails to dock. This is a third,
independent blocker on the reference geometry and it is not in #164's scope. It probably wants its
own issue: the reservation should prefer a face the item nets are not going to want, or be chosen
route-aware the way `_dock_net` and `_route_trunk` already are.

**R7. #164's stated mechanism is wrong and the issue should be amended.** "one pipe block carries
several material flows at once" is not what the reference does (section 1). Leaving it as written
will send the implementer at the validator's capacity rule, which is the one change here with a
real soundness cost and the least measured benefit.

---

## 7. Can #165 (pipe gauge selection) proceed in parallel?

**Yes, and it should, because it is independent of everything above.**

The seam is clean and already exists:

- gauge is carried by `Route.material` (`ir/output.py:96-118`, a `RouteMaterial`) and, for power
  only, `Route.thickness_per_segment` (`:141`). #165 gives item/fluid routes a real gauge the way
  power already has one;
- `router/core.py` touches gauge in exactly **one line**: `material=route_material(net.commodity)`
  at `:372`, inside the `Route(...)` construction at `:364-376`, at the end of `_negotiate`.
  Nothing in the negotiation, the pricing, the docking or the A* reads or writes gauge;
- #164's changes are in `_negotiate`'s **usage and price** bookkeeping (`:286`, `:298-303`,
  `:320-345`) and in `_dock_net`/`_free` (`:544-637`). Disjoint line ranges, disjoint concepts.

The two do meet in three files, and the merge order matters there:

- `route_blocks.py::route_cells` (`:125-172`) - #165 gives a pipe a real `thickness`; #164 gives a
  cell possibly two routes. `route_block_counts` (`:259`) is the one function both edit, because
  #165 makes "the thicker wins" meaningful for pipes and #164 makes the union-of-masks necessary.
  Whoever lands second rewrites that function; it is ~15 lines;
- `schematic/core.py:232-237` - #165 lowers 5591/5592/5593 correctly, #164 makes one cell possibly
  two routes. Same story, same function;
- `ir/output.py` - #165 may need a thickness field for pipes. #164 needs none. If #165 bumps
  `LAYOUT_RESULT_VERSION` (`:23`), #164 must not also bump it in a parallel branch.

**Recommended sequencing:** land #165 first. It is contract-additive, it has no validator
soundness cost, it independently improves the bill of materials and the export, and #165's own
"Depends on" note overstates the dependency: its *sizing* decision is correct regardless of how
sharing lands. Then land the terminal-sharing slice (section 5). Capacity itself, wired in at 1,
can go any time; raising it above 1 waits on R1 and R3.

---

## Appendix: commands used

```sh
gtnh-solve --inspect-schematic tests/golden/schematic/sand-parallel-reference.schematic
gtnh-solve examples/gtnh-parallel-sand.json --schematic out/ours.schematic
gtnh-solve --inspect-schematic out/ours.schematic
```

Everything else was throwaway probe scripts over `schematic.read.read_schematic`,
`adapter.adapt_file`, `router.route` / `router.route_power`, `validator.validate` and
`placement.search._face_shortfall`, run from a scratch directory and not committed. They are
reconstructible from the numbers quoted above; the two worth turning into real tests are named in
section 5.
