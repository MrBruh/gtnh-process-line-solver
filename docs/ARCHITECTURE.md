# Architecture

Source of truth for how `gtnh_solver` is built. If code disagrees with this doc, treat the
doc as intent and reconcile.

> **Build status.** This doc records the *design intent* - the nine engineering-review
> decisions. Phase 1 has shipped a crude but end-to-end version of the whole pipeline; a few
> refinements below are **Phase 2 (not yet built)** and are flagged inline. What is implemented
> today is the `Added` list in [`../CHANGELOG.md`](../CHANGELOG.md); the phased plan is in
> [`ROADMAP.md`](ROADMAP.md).

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
     │ SA/LNS,    │ ───────── placed cells ───────► │ A*, 2.5D,  │
     │ orientation│                                 │ per-commod.│
     └─────┬──────┘                                 │ + power    │
           │            place↔route↔retry           └─────┬──────┘
           └──────────────────┬─────────────────────────┘
                              ▼  (anytime: best-valid-so-far; wall-clock timeout = Phase 2)
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
  (`adapter/plan.py`). No vendoring. *(Phase 2, lane A: pin an explicit plan-schema +
  recipe-dataset version; Phase 1 just tolerates the current export shape via the committed
  `examples/` fixtures.)*
- **dataset/** - the GT **physical** rules (footprints, faces, pipe/wire physical tiers,
  multiblocks), keyed to gtnh-factory-flow's machine IDs. Recipes/throughput/identity come from
  its dataset; you author only the physical half. Still substantial, but smaller than before.
- **placement/** - simulated annealing + LNS ruin-and-recreate over a coarse cell grid;
  orientation is a placement variable; cost = HPWL + compactness + a flat-build bias + an
  auto-output reward. *(Phase 2, lane C: SA + LNS are in; the cheaper incremental
  routing/congestion estimate the cost is meant to grow into is still ahead.)*
- **router/** - free-form per-commodity A* on the cell grid; single-channel capacity;
  rip-up-and-reroute; ME-toggle skipping; the shared-amperage power primitive. *(Phase 2, lane D:
  the margin→channels-per-edge cap + cell→block realizability, and power optimization beyond
  size-or-reject.)*
- **solver/** - orchestrates the place↔route feedback loop (built: penalize the nets a pass
  leaves unrouted, re-place, keep the best-valid layout - `solver/core.py`). *(Phase 2: the
  anytime **wall-clock** budget; today it runs a deterministic bounded set of feedback passes
  keyed off the seed, not a timeout.)*
- **validator/** - independent geometric + rule checks (shares rule *data* with the router,
  not its *logic*). The only automated correctness gate.
- **buildguide/**, **previewer/**, **cli.py** - outputs and entry point.

## Engineering decisions (from the review)

1. **Placement↔routing - routing-aware + feedback loop.** Placement scores with a cheap
   incremental routing estimate; a full route runs on the feedback pass; unroutable nets feed
   a penalty back to perturb placement. The estimate must be ~O(1) per SA move.
   *Built: the feedback loop itself - a pass's unrouted nets are penalized, placement re-runs,
   the best-valid layout is kept (`solver/core.py`). Phase 2: the O(1) incremental routing
   estimate - today the SA cost is HPWL + compactness + flat-build bias + an auto-output reward
   (no per-move routing/congestion term), and the feedback is a coarse penalize-and-re-place.*
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
   *(Phase 2: the wall-clock/timeout budget. `solve()` is already anytime best-valid-so-far, but
   over a **deterministic bounded** set of feedback passes keyed off the seed, not wall-clock -
   see `solver/core.py`.)*
7. **Routing topology - free-form + realizability invariant.** Free-form capacitated routing
   plus a margin→max-channels-per-edge cap and cell→block realizability fed back into the
   loop, so the coarse-cell abstraction can't certify unbuildable layouts. *(Built: single-channel
   capacity - no cell carries two routes, validator-enforced. Phase 2, lane D: the per-edge
   multi-channel cap and cell→block realizability.)*
8. **Power - shared-amperage net.** Amperage *sums* along shared segments (Steiner-tree-like).
   Voltage tier follows the machine voltage; thickness (1x/2x/4x/8x/16x, 16x max) sizes to the
   summed amperage; past 16x split into parallel runs or a higher voltage. **Cable loss over
   distance** is modelled: a machine `d` blocks out receives `tier_voltage - loss·d`, so amperage
   is sized at that delivered voltage (thicker cable the farther out), and a run whose voltage
   drops to 0 is infeasible; loss is a flat 1 EU/block for now. The synthesized per-tier source
   is fed **from outside the structure**: its front face is the reserved feed entry, pinned flush
   on the region boundary by placement and enforced independently by the validator (the front-face
   rule already keeps internal cables off it). See [`DOMAIN.md`](DOMAIN.md).
9. **Spec corrections.** Required-I/O-face reachability is a HARD constraint; the output-layout
   schema is a versioned contract.

## Spatial model

Placement runs on a **coarse cell grid** (cell = largest common single-block footprint +
routing margin). A machine occupies an integer box of cells (single-block = 1 cell;
multiblock = cell-rounded bounding box). Reserved cells = pinned/off-limits. Block-accuracy
is materialized only at export via a per-machine cell→block mapping, **never during search**.
The router is meant to enforce a **channels-per-edge** cap derived from the margin so cell routes
lower to non-conflicting blocks. *(Phase 2, lane D: today the router enforces only single-channel
capacity - one route per cell - not the per-edge multi-channel cap.)*

## Optimization objectives

- **Buildable-compact (v1 default)** - minimize footprint subject to 2.5D routing + the
  buildability metric (layer count, vertical runs, route crossings, required-face reachability
  as a HARD constraint vs convenient access as soft).
- **Theoretical-min-volume (v1.1)** - minimize volume only, full-3D routing, may be
  unbuildable; still geometric/rule valid.
