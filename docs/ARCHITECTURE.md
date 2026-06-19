# Architecture

Source of truth for how `gtnh_solver` is built. If code disagrees with this doc, treat the
doc as intent and reconcile.

## Data flow

```
                    ┌────────────────────────┐
                    │    gtnh-factory-flow    │  (MIT web planner; export a plan)
                    └───────────┬─────────────┘
                     exported plan JSON (recipes embedded) + recipe dataset
                                │ adapter: parse + Zod-validate → IR
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
                              ▼  (anytime: best-valid-so-far on timeout)
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
- **adapter/** - parses gtnh-factory-flow's exported plan JSON (Zod-validated, recipes
  embedded) into the IR, against a pinned plan-schema and recipe-dataset version. No vendoring.
- **dataset/** - the GT **physical** rules (footprints, faces, pipe/wire physical tiers,
  multiblocks), keyed to gtnh-factory-flow's machine IDs. Recipes/throughput/identity come from
  its dataset; you author only the physical half. Still substantial, but smaller than before.
- **placement/** - SA/LNS over a coarse cell grid; orientation is a placement variable; cost
  = compactness + a *cheap incremental* routing estimate (HPWL + congestion proxy) +
  buildability.
- **router/** - free-form per-commodity A* on the cell grid with a channels-per-edge cap and
  cell→block realizability; rip-up-and-reroute; ME-toggle handling; the shared-amperage power
  primitive.
- **solver/** - orchestrates the place↔route feedback loop and the anytime budget.
- **validator/** - independent geometric + rule checks (shares rule *data* with the router,
  not its *logic*). The only automated correctness gate.
- **buildguide/**, **previewer/**, **cli.py** - outputs and entry point.

## Engineering decisions (from the review)

1. **Placement↔routing - routing-aware + feedback loop.** Placement scores with a cheap
   incremental routing estimate; a full route runs on the feedback pass; unroutable nets feed
   a penalty back to perturb placement. The estimate must be ~O(1) per SA move.
2. **IR - minimal, versioned, up front.** Defined before the integration spike; grows with
   explicit versioning.
3. **Input - consume a maintained fork of gtnh-factory-flow's exported plan JSON** (MIT;
   Zod-validated, recipes embedded). We depend on a **maintained fork** and fix only bugs in the
   export/throughput/dataset path we consume (fixes offered upstream as PRs). The adapter validates against a **pinned plan-schema version** and a **pinned
   recipe-dataset version**, and we **snapshot a known-good dataset + sample exports as fixtures**
   (`examples/`) so the solver is decoupled from the fork's health. No upstream code is vendored.
   (Supersedes the old fork/patch-gtnh-flow plan.)
4. **Validator - shared rule data, independent checking logic** so it can catch router bugs.
5. **Ground truth - golden corpus + property tests now**; harvested corpus via round-trip
   import is v1.1. Plus an in-game spot-check of the starter dataset during the Assignment.
6. **Performance - target ~30-50 machines, anytime wall-clock budget** (best-valid-so-far on
   timeout). Router uses **A\*** (not Lee BFS) with a Manhattan heuristic on the bounded grid.
7. **Routing topology - free-form + realizability invariant.** Free-form capacitated routing
   plus a margin→max-channels-per-edge cap and cell→block realizability fed back into the
   loop, so the coarse-cell abstraction can't certify unbuildable layouts.
8. **Power - shared-amperage net.** Amperage *sums* along shared segments (Steiner-tree-like).
   Voltage tier follows the machine voltage; thickness (1x/2x/4x/8x/16x, 16x max) sizes to the
   summed amperage; past 16x split into parallel runs or a higher voltage. See [`DOMAIN.md`](DOMAIN.md).
9. **Spec corrections.** Required-I/O-face reachability is a HARD constraint; the output-layout
   schema is a versioned contract.

## Spatial model

Placement runs on a **coarse cell grid** (cell = largest common single-block footprint +
routing margin). A machine occupies an integer box of cells (single-block = 1 cell;
multiblock = cell-rounded bounding box). Reserved cells = pinned/off-limits. Block-accuracy
is materialized only at export via a per-machine cell→block mapping, **never during search**.
The router enforces a **channels-per-edge** cap derived from the margin so cell routes lower
to non-conflicting blocks.

## Optimization objectives

- **Buildable-compact (v1 default)** - minimize footprint subject to 2.5D routing + the
  buildability metric (layer count, vertical runs, route crossings, required-face reachability
  as a HARD constraint vs convenient access as soft).
- **Theoretical-min-volume (v1.1)** - minimize volume only, full-3D routing, may be
  unbuildable; still geometric/rule valid.
