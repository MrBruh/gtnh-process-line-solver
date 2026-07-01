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

**Already landed:** package skeleton + lint/type/test CI; the **IR** (`ir/`, T1); the
**validator** geometric/structural half (`validator/`). These are the contracts + the safety
gate that Phase 1 builds against.

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
in-game. Do **not** start Phase 2 until this holds.

### Phase 2 - the optimizer core (queued right after Phase 1, NOT cut)

Layer the designed solver onto the working baseline, adding sophistication only where Phase 1
is demonstrably valid-but-bad (too large, unroutable, ugly). This is the recorded design intent
- the 9 engineering decisions in [`ARCHITECTURE.md`](ARCHITECTURE.md) still stand:

- **placement** - SA/LNS + cheap routing-aware cost + orientation as a search variable
  (replaces the crude constructive placer).
- **router** - rip-up-and-reroute, the **channels-per-edge realizability invariant**,
  cell->block realizability fed back into search, ME-toggle endpoint placement, pluggable
  multi-channel backends.
- **solver** - the **place<->route feedback loop** + anytime wall-clock budget
  (best-valid-so-far on timeout).
- **power** - shared-amperage optimization (Steiner-like summing, thickness sizing, the 16x
  split/upgrade) beyond Phase 1's size-or-reject. Voltage loss is now modelled as a flat
  1 EU/block; Phase 2 adds **per-material cable loss** and, driven by it, **multi-source
  count/placement** (a nearer source instead of thickening a too-lossy run) and voltage upgrades.
- **validator (rule half)** - throughput/tier caps, summed amperage <= cable rating,
  one-fluid-per-line, required-I/O-face reachability, once the physical dataset is real.
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
