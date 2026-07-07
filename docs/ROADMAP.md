# Roadmap

## v1 - phased: thin slice first, then the optimizer core

A working solver: one re-runnable annealed solution, 2.5D **buildable-compact** objective,
all three commodities single-channel + per-commodity ME toggle, single-block **and**
bounding-box multiblocks, the previewer and the build guide, the validator + tests. Free-form
routing with the realizability invariant; shared-amperage power. Target ~30-50 machines with
an anytime time budget.

**Why phased (cross-model design review, 2026-06).** The biggest failure mode here is a
polished IR + validator + optimizer that has never turned a real gtnh-factory-flow export into
a layout a player can build. A mediocre *valid, visible* layout is worth more than an elegant
optimizer that has consumed zero real input. So v1 proves the whole path on one small line
first (**Phase 1**), then makes it good (**Phase 2**). The optimizer work is **resequenced,
not cut** - it is the bulk of v1, queued right after the basics hold.

**Upstream note.** We work against a **maintained fork** of gtnh-factory-flow: fix only the
export/throughput/dataset path we consume, pin a fork commit + dataset version, and **snapshot a
known-good dataset + sample exports as fixtures** in `examples/`. The solver's progress must
never depend on the fork's health. Offer fixes upstream as PRs; don't adopt the whole app.

**Already landed:** the **entire Phase 1 pipeline**, end to end - package skeleton + lint/type/
test CI; the **IR** contracts (`ir/`); the **adapter** (real gtnh-factory-flow exports ->
`InputIR`, with two committed fixtures); **constructive placement**; the **crude router**
(per-commodity A* + single-channel capacity); the **previewer** and **build guide**; the
**validator**; and the **`gtnh-solve` CLI**. On top of that, several **Phase 2 slices** have
shipped: SA + LNS placement over a routing-aware cost with a selectable footprint/volume/balanced
objective (lane C); **negotiated-congestion routing** for item/fluid nets plus the shared-amperage
power model (source synthesis, tree trunks, cable voltage-loss sizing, power trunks keeping
failed-first rip-up/reroute) (lane D); the first slice of the **physical multiblock dataset** (a
schema-v1 loader and the Electric Blast Furnace / Vacuum Freezer footprints, wired into the solve
path) plus the **Java extractor** (`tools/gtnh-extractor/`) that will populate it and a weekly
**dataset-update CI** (lane B); the **place<->route feedback loop** as a multi-start grid
(`solver/core.py`); the summed-amperage + voltage-drop half of the validator's power checks
(lane E); and real GT **textures in the previewer** alongside the `system_io` boundary summary
feeding both render surfaces (lane F). The full `Added`/`Changed` list is in
[`../CHANGELOG.md`](../CHANGELOG.md).

### Phase 1 - thin end-to-end slice (prove the path)

Goal: one pinned small demo line goes **real export JSON -> buildable layout** you can see in
the previewer and verify in-game. Crude is fine - correctness and visibility beat quality. Each
step stays deliberately dumb; the validator (already built) certifies the output is not
silently invalid.

1. **Get a real gtnh-factory-flow exported plan JSON** into `examples/` - one small line
   (~3-5 machines). *(Owner action; everything below depends on it.)*
2. **Pin that as THE demo line.** Author only the physical-dataset entries it needs - keep it
   tiny. (This reverses the earlier "don't pin a demo line" call - see Risks.)
3. **adapter** - that export -> `InputIR`.
4. **placement (crude)** - deterministic constructive placement: topological / row order, a
   simple legal-orientation pick, in-bounds, no overlap. No SA/LNS yet.
5. **router (crude)** - A* per-commodity with obstacle avoidance + single-channel capacity.
   Power: sum the load on the produced path/tree, size at each machine's delivered voltage (cable
   loss over distance, flat 1 EU/block), and size-or-reject; no optimization yet.
6. **previewer + build guide (minimal)** - emit the `LayoutResult` so it is visible/buildable.
7. **validator** - certify it (done; it gates Phase 1's output).
8. **the Assignment (in-game)** - can a human build this line from the output, and does it run?
   This also spot-checks the starter dataset's tiers/face-rules/throughputs against reality.

**Phase 1 success criterion:** the pinned demo line is buildable from the output and runs
in-game. This was written as a hard gate (do **not** start Phase 2 until it holds), but in
practice several Phase 2 quality slices have since shipped ahead of it (see "Already landed"
above). The in-game Assignment's outcome is **not recorded in this repo**, so read the gate as
the *intended* discipline - keep the demo line buildable as slices land - rather than a
precondition that has been formally cleared.

### Phase 2 - the optimizer core (queued right after Phase 1, NOT cut)

Layer the designed solver onto the working baseline, adding sophistication only where Phase 1
is demonstrably valid-but-bad (too large, unroutable, ugly). This is the recorded design intent
- the 9 engineering decisions in [`ARCHITECTURE.md`](ARCHITECTURE.md) still stand:

- **placement** - SA + LNS ruin-and-recreate + cheap routing-aware cost + orientation as a search
  variable (replaces the crude constructive placer). *SA and LNS are in; the routing-aware cost is
  still the HPWL + auto-output proxy, to grow into the incremental congestion estimate.*
- **router** - **negotiated-congestion routing is in** for item/fluid nets (PathFinder-style:
  contested cells are priced up round by round until every net owns its cells - order-robust, no
  ordering-induced false infeasibility; GitHub #7); power trunks keep failed-first rip-up/reroute.
  Ahead: the **channels-per-edge realizability invariant**, cell->block realizability fed back
  into search, ME-toggle endpoint placement, pluggable multi-channel backends.
- **solver** - the **place<->route feedback loop** (built: a multi-start grid in
  `solver/core.py` that routes + validates every attempt and keeps the best VALID by quality)
  plus the anytime wall-clock budget (still queued: today's grid is deterministic + bounded,
  not a wall-clock timeout).
- **power** - shared-amperage optimization (Steiner-like summing, thickness sizing, the 16x
  split/upgrade) beyond Phase 1's size-or-reject. Voltage loss is now modelled as a flat
  1 EU/block; Phase 2 adds **per-material cable loss** and, driven by it, **multi-source
  count/placement** (a nearer source instead of thickening a too-lossy run) and voltage upgrades.
- **validator (rule half)** - throughput/tier caps, one-fluid-per-line, and the
  dataset-specific half of face rules, once the physical dataset is real. (The summed-amperage
  and voltage-drop power checks already shipped, independent of the dataset.)
- **tests** - the on-disk golden corpus + broader hypothesis property tests.

#### Parallel lanes (Phase 2 - after the thin slice proves the path)

Once Phase 1 holds, fan the *quality* work out in parallel worktrees. (Phase 1 already built a
crude end-to-end version of A/C/D/F against the pinned line, so these are upgrades, not
greenfield.)

| Lane | Phase 2 work | Depends on |
|------|--------------|------------|
| A | adapter hardening -> plan-schema/dataset version pinning | Phase 1 adapter |
| B | full dataset (footprints/faces/tiers/cell->block) beyond the demo line | IR |
| C | placement -> SA/LNS + routing-aware cost | Phase 1 placement |
| D | router -> rip-up/reroute + shared-amperage power optimization | Phase 1 router |
| E | validator rule-half (tier caps, amperage, face reachability) | dataset + router |
| F | previewer / build guide polish | Phase 1 previewer |

**Merge C + D before the solver loop** (both feed it - coordinate the placement<->router
interface). Then the CLI ties it together.

## v1.1 and beyond (deferred)

- **Theoretical-min-volume mode** - full-3D router, drop the buildability constraint; explore
  the packing lower bound (may be unbuildable, still rule-valid).
- **Formal Pareto / multi-candidate output** - compactness vs routing-slack vs buildability,
  with a congestion heatmap. (v1 covers seed comparison by re-run + preview.)
- **Round-trip `.schematic` import (Approach C)** - bootstraps the dataset from real builds,
  yields a harvested validation corpus + a compactness benchmark.
- **Paste-ready `.schematic` export** - gated on the Assignment's fidelity result.
- **GT++ quad/nonuple multi-fluid pipes** - channel-packing within a single block.
- **EnderIO conduits** - early/mid-game item transport backend.
- **CP-SAT placement backend** - optional exact solver for small sub-blocks.
- **PyPI publish automation** - once there's a usable release.

## Known risks carried forward

- **No automated correctness ground truth in v1** beyond self-consistency - mitigated by the
  in-game spot-check + golden corpus, fully addressed by the v1.1 import corpus.
- **Dataset scope** - bounded now by pinning **one concrete demo line** in Phase 1 (this
  reverses the earlier decision to leave it loosely bounded). Risk: the pinned line is too
  unrepresentative to generalize from; mitigated by choosing a line that exercises all three
  commodities (item + fluid + power) and at least one multiblock if cheap.
