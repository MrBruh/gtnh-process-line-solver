# Architecture

Source of truth for how `gtnh_solver` is built. If code disagrees with this doc, treat the
doc as intent and reconcile.

> **Build status.** This doc records the *design intent* - the nine engineering-review decisions
> (#1 to #9) plus #10, decided since. Phase 1 has shipped a crude but end-to-end version of the
> whole pipeline; a few refinements below are **Phase 2 (not yet built)** and are flagged inline.
> What is implemented today is the `Added` list in [`../CHANGELOG.md`](../CHANGELOG.md); the
> phased plan is in [`ROADMAP.md`](ROADMAP.md).

## Data flow

```
                    ┌────────────────────────┐
                    │    gtnh-factory-flow    │  (MIT web planner; export a plan)
                    └───────────┬─────────────┘
                     exported plan JSON (recipes embedded) + recipe dataset
                                │ adapter: parse + validate (Pydantic) → IR
                                ▼
   ┌─────────────────────┐  ┌──────────────┐
   │ physical-rules data │─►│   INPUT IR   │  machines (footprint, faces, orientation
   │ footprints, faces,  │  │  (versioned) │  options), nets (commodity + typed
   │ pipe/wire tiers,    │  └──────┬───────┘  throughput), pinned I/O, bounding region,
   │ ME toggles, cell→blk│         │          ME toggles, cell→block mapping
   └─────────────────────┘         ▼
            ┌──────────────────────┴───────────────────┐
            ▼          routing-aware cost (cheap)        ▼
     ┌────────────┐  ◄──── feedback (penalty) ───── ┌────────────┐
     │ Placement  │                                 │  Router    │
     │ SA/LNS,    │ ───────── placed cells ───────► │ A*, 3D,    │
     │ orientation│                                 │ per-commod.│
     └─────┬──────┘                                 │ + power    │
           │            place↔route↔retry           └─────┬──────┘
           └──────────────────┬─────────────────────────┘
                              ▼  (multi-start grid: best VALID by quality; wall-clock timeout = Phase 2)
                       ┌────────────┐
                       │  Validator │  independent logic, shared rule DATA
                       └─────┬──────┘
                 ┌───────────┴──────────┐
                 ▼                      ▼
          ┌────────────┐         ┌────────────┐
          │ OUTPUT IR  │────────►│ Previewer  │  three.js
          │ (layout    │         └────────────┘
          │  schema,   │────────►┌────────────┐
          │  versioned)│         │ Build guide│  BoM, per-layer coords
          └────────────┘         └────────────┘
   (v1.1+: .schematic export, round-trip import, theoretical-min-volume mode, Pareto)
```

## Components

- **ir/** - two versioned contracts: the **input IR** (problem) and the **output layout
  schema** (solution, consumed by previewer + build guide + later export). See [`IR.md`](IR.md).
- **adapter/** - parses gtnh-factory-flow's exported plan JSON (the *upstream* exporter
  Zod-validates it; recipes are embedded) into the IR with **Pydantic** models
  (`adapter/plan.py`). No vendoring. On a schema-v2 export it trusts the `resolved` throughput
  block for each machine's EU/t draw (the exporter models overclocking) and cross-checks it
  against the recipe-derived power synthesis - a mismatch warns (`AdapterWarning`) but resolved
  wins; v1 plans keep the pure synthesis (#2). *(Phase 2, lane A: pin an explicit plan-schema +
  recipe-dataset version; Phase 1 just tolerates the current export shape via the committed
  `examples/` fixtures.)*
- **dataset/** - the GT **physical** rules (footprints, faces, pipe/wire physical tiers,
  multiblocks) plus the extracted **texture manifest** the previewer skins blocks with, keyed to
  gtnh-factory-flow's machine IDs. Recipes/throughput/identity come from its dataset; you author
  only the physical half. Still substantial, but smaller than before.
  Generated data is local and version-namespaced (`data/<version>/`) with committed fixtures as the
  fallback (`dataset/roots.py`); how the extractor resolves a block's sprite is in
  [`dataset-extraction/texture-resolution.md`](dataset-extraction/texture-resolution.md).
- **placement/** - simulated annealing + LNS ruin-and-recreate over a coarse cell grid;
  orientation is a placement variable; cost = per-net wirelength (HPWL) + an auto-output reward
  + an **objective-weighted compactness** term (floor footprint and/or bounding-box volume, the
  weights set by the selected objective). There is deliberately **no** per-layer / flat-build
  bias: height is paid only through the volume term. Power nets carry no base wirelength term; a
  failed *or starved* power net enters the cost as an MST trunk-length pull only once the
  feedback penalizes it. *(Phase 2, lane C: SA + LNS are in; the cheaper incremental
  routing/congestion estimate the cost is meant to grow into is still ahead.)*
- **router/** - free-form per-commodity A* on the **full-3D** cell grid (all six faces are
  neighbours); single-channel capacity; **negotiated-congestion routing** for item/fluid nets
  (priced A*, PathFinder-style; power trunks keep failed-first rip-up/reroute); ME-toggle
  skipping; the shared-amperage power primitive. It owns the **auto-output vs pipe** decision (`router/auto.py`,
  `assign_auto_outputs`): adjacent 1-source-1-sink item/fluid nets take GT's free auto-output,
  only the rest are piped. *(Phase 2, lane D: the margin→channels-per-edge cap + cell→block
  realizability, and power optimization beyond size-or-reject.)*
- **solver/** - orchestrates the place↔route feedback loop (built: a bounded **multi-start grid**
  - SA weight modes x seeds - that fully routes + validates every attempt and keeps the best
  VALID layout by a quality ranking, penalizing the nets a pass leaves unrouted - and the power
  net of any machine validation proves starved of power, which is a placement defect, not a bug -
  so the next placement pulls them tighter - `solver/core.py`). *(Phase 2: the anytime
  **wall-clock** budget; today it runs a deterministic bounded grid keyed off the seed, not a
  timeout.)*
- **system_io.py** - the single source of truth for the line's **boundary I/O** (what to feed in,
  what to collect) and the **power-feed spec** (EU/t plus amperage per voltage tier). Pure over
  the `InputIR` + `LayoutResult`; both the build guide and the previewer read it, so the two
  render surfaces cannot drift on what crosses the line's edge.
- **validator/** - independent geometric + rule checks (shares rule *data* with the router,
  not its *logic*). The only automated correctness gate.
- **buildguide/**, **previewer/**, **cli.py** - outputs and entry point. The previewer skins each
  block with its extracted GT sprite; a face that resolves none renders Minecraft's magenta/black
  missing-texture checkerboard, **not** a neutral grey, because so many GT casings are plain grey
  that a gap was indistinguishable from a correct render (`previewer/html.py`, `_MISSING`).

## Engineering decisions (from the review)

1. **Placement↔routing - routing-aware + feedback loop.** Placement scores with a cheap
   incremental routing estimate; a full route runs on the feedback pass; unroutable nets feed
   a penalty back to perturb placement. The estimate must be ~O(1) per SA move.
   *Built: the feedback loop - `solve()` runs a bounded **multi-start grid** (SA weight modes x
   seeds), fully routes + validates every attempt, and keeps the best VALID layout by a quality
   ranking (the objective's compactness metric, then real power-cable cells, then the other
   compactness metric); a pass's unrouted nets - plus the power net of any machine the validator
   proves **starved**, whose shortfall is distance-driven and so is exactly what re-placing fixes -
   are penalized so the next placement pulls them tighter (`solver/core.py`). This replaced the
   earlier first-valid-wins, coarse penalize-and-re-place behaviour: cheap placement-time proxies
   cannot see dock faces or shared cable taps, so a layout's real quality is only knowable once it
   is routed, hence ranking fully routed attempts rather than stopping at the first valid one.
   Phase 2: the O(1) incremental routing estimate - today the SA cost is per-net HPWL + an
   objective-weighted compactness term + an auto-output reward (no per-move routing/congestion
   term, and deliberately no per-layer bias - height is paid only through the volume term).*
2. **IR - minimal, versioned, up front.** Defined before the integration spike; grows with
   explicit versioning.
3. **Input - consume a maintained fork of gtnh-factory-flow's exported plan JSON** (MIT; the
   upstream exporter validates it with Zod, recipes embedded). We depend on a **maintained fork**
   and fix only bugs in the export/throughput/dataset path we consume (fixes offered upstream as
   PRs). The adapter re-parses that export with **Pydantic** (`adapter/plan.py`), and we
   **snapshot a known-good dataset + sample exports as fixtures** (`examples/`) so the solver is
   decoupled from the fork's health. *(Phase 2, lane A: validate against an explicit **pinned
   plan-schema version** and **pinned recipe-dataset version**; Phase 1 only tolerates the current
   export shape.)* No upstream code is vendored. (Supersedes the old fork/patch-gtnh-flow plan.)
4. **Validator - shared rule data, independent checking logic** so it can catch router bugs.
5. **Ground truth - golden corpus + property tests now**; harvested corpus via round-trip
   import is v1.1. Plus an in-game spot-check of the starter dataset during the Assignment.
6. **Performance - target ~30-50 machines, anytime wall-clock budget** (best-valid-so-far on
   timeout). Router uses **A\*** (not Lee BFS) with a Manhattan heuristic on the bounded grid.
   *(Phase 2: the wall-clock/timeout budget. `solve()` already returns the best VALID layout by
   its quality ranking, but over a **deterministic bounded** multi-start grid keyed off the seed,
   not a wall-clock timeout - see `solver/core.py`.)*
7. **Routing topology - free-form + realizability invariant.** Free-form capacitated routing
   plus a margin→max-channels-per-edge cap and cell→block realizability fed back into the
   loop, so the coarse-cell abstraction can't certify unbuildable layouts. *(Built: single-channel
   capacity - no cell carries two routes, validator-enforced. Phase 2, lane D: the per-edge
   multi-channel cap and cell→block realizability.)*
8. **Power - shared-amperage net.** Amperage *sums* along shared segments (Steiner-tree-like).
   Voltage tier follows the machine voltage; thickness (1x/2x/4x/8x/12x/16x, 16x max) sizes to the
   summed amperage; past 16x split into parallel runs or a higher voltage. **Cable loss over
   distance** is modelled: a machine `d` blocks out receives `tier_voltage - loss·d`, so amperage
   is sized at that delivered voltage (thicker cable the farther out), and a run whose voltage
   drops to 0 is infeasible; loss is a flat 1 EU/block for now. A tier gets **one synthesized
   source per cable run it needs**: the adapter bin-packs the tier's *feeds* by nominal amp load,
   so a tier drawing past 16x is split across several sources rather than rejected outright, and
   a machine drawing past 16x on its own is split too, because a feed is one energy hatch and not
   a whole machine (#10). Each source is fed **from outside the structure**: its front face is the
   reserved feed entry, pinned flush on the region boundary by placement and enforced
   independently by the validator (the front-face rule already keeps internal cables off it). See
   [`DOMAIN.md`](DOMAIN.md).
9. **Spec corrections.** Required-I/O-face reachability is a HARD constraint; the output-layout
   schema is a versioned contract.
10. **Power intake is per *connection*, not per machine** (added since the review, once the
    physical dataset made a machine's structure knowable). A machine's draw is modelled as one
    port per GT **energy hatch**: each carries its share of the `eut` (`Port.rate`) and its own
    2 A ceiling (`Port.max_amps`), and the machine's intake is their sum, as in game
    (`MTEHatchEnergy` accepts 2 amps; `MTEMultiBlockBase.getMaxInputPower()` sums its hatches).
    The old one-port-per-machine model charged every cable the machine's whole draw, right only
    while a machine has a single hatch, and it made a heavy machine unpowerable **by
    construction** (one feed, capped at 16x) where the in-game answer is simply to add hatches.
    So the adapter allocates the hatches a draw needs and the partitioner bin-packs *hatches*,
    letting one machine's intake spread over several cable runs. The ceiling is structural, from
    the extractor dump's hatch slots (`Machine.hatch_cells`): a multiblock's casing cells are
    interchangeable, so item, fluid and power connections compete for one pool, and a machine
    wired more connections than it has cells is reported (`HATCH_CELLS_EXCEEDED`) rather than
    silently given fewer hatches than its draw needs, which would certify a layout that cannot
    draw its own load. **Only the shortfall direction is checked.** After cable loss a machine's
    real intake is `sum(max_amps · delivered_volts)` over its hatches, so it can sit on cables
    that are all thick enough and still not take in its `eut`; nothing else catches that, and a
    machine that cannot take in its draw does not run the recipe the plan balanced, so the
    validator flags it (`POWER_SUPPLY_INSUFFICIENT`). That verdict is the one the solver acts on
    rather than merely reports: the shortfall is set by how far the cable ran, so the loop
    penalizes the machine's power net and re-places it nearer its source before giving up
    (decision 1). The reverse, a cable offering a hatch more amps than it accepts, is deliberately
    **not** an error: the hatch just takes its 2 A and nothing burns (only a cable over its *own*
    rating does, which the thickness check already covers), so flagging it would reject layouts
    that work in game. *Temporary:* a machine that would need more than three hatches is supplied
    at a **higher voltage tier** instead, because some upstream gtnh-factory-flow exports compute
    EU/t against a wrong recipe model (gtnh-factory-flow #44, #45); it changes only the voltage
    the layout supplies, never the recipe (a real tier change re-overclocks, which only the
    exporter can do), and it comes out once those upstream fixes land. See
    [`DOMAIN.md`](DOMAIN.md).

## Spatial model

Placement runs on a **coarse cell grid** (cell = largest common single-block footprint +
routing margin). A machine occupies an integer box of cells (single-block = 1 cell;
multiblock = cell-rounded bounding box). Reserved cells = pinned/off-limits. Block-accuracy
is materialized only at export via a per-machine cell→block mapping, **never during search**.
The router is meant to enforce a **channels-per-edge** cap derived from the margin so cell routes
lower to non-conflicting blocks. *(Phase 2, lane D: today the router enforces only single-channel
capacity - one route per cell - not the per-edge multi-channel cap.)*

## Optimization objectives

"Compact" is ambiguous - stacking a layer shrinks the floor but can grow the enclosing box - so
what the optimizer targets is **user-selectable** (`gtnh-solve --objective`, `solve(...,
objective=...)`). The choice sets both the placement cost's compactness weights and the feedback
loop's quality ranking of routed layouts (`placement/search.py`, `solver/core.py`):

- **`footprint` (v1 default)** - minimize the floor area (x-span by z-span) and stack tall; the
  maintainer's target.
- **`volume`** - minimize the enclosing bounding box and stay flat/cubic.
- **`balanced`** - weigh footprint and volume together.

All three stay inside the **buildable** family: required-I/O-face reachability is a HARD
constraint (convenient access is soft), routing is single-channel realizable, and the fast
constructive path (`--fast`) ignores the objective (it is floor-first by construction). Note that
`--objective volume` is **not** the v1.1 *theoretical-min-volume* mode below: it still produces a
buildable, single-channel-realizable layout - it just weights the enclosing box instead of the
floor.

- **Theoretical-min-volume (v1.1)** - minimize volume only, dropping the buildability constraint,
  to explore the packing lower bound; may be unbuildable, still geometric/rule valid. See
  [`ROADMAP.md`](ROADMAP.md).
