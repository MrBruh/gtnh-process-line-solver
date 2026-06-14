# Roadmap

## v1 — the optimizer core

A working solver: one re-runnable annealed solution, 2.5D **buildable-compact** objective,
all three commodities single-channel + per-commodity ME toggle, single-block **and**
bounding-box multiblocks, the previewer and the build guide, the validator + tests. Free-form
routing with the realizability invariant; shared-amperage power. Target ~30–50 machines with
an anytime time budget.

**Upstream note.** We work against a **maintained fork** of gtnh-factory-flow: fix only the
export/throughput/dataset path we consume, pin a fork commit + dataset version, and **snapshot a
known-good dataset + sample exports as fixtures** in `examples/`. The solver's progress must
never depend on the fork's health. Offer fixes upstream as PRs; don't adopt the whole app.

### Canonical build order

0. **The Assignment (do before solver code):** hand-build a trivial 3-machine line in GT:NH
   with real I/O-face config, export with Schematica-Plus, clear, paste back, confirm it
   rebuilds with config intact AND runs. One evening; retires the export-fidelity risk. While
   in-game, spot-check the starter dataset's tiers/face-rules/throughputs against reality.
1. `git init` ✓, package skeleton ✓, lint/test CI ✓.
2. **IR** (`ir/`) — typed, versioned input IR + output-layout schema. Unblocks everything.
3. **Integration spike** — a real gtnh-factory-flow **exported plan JSON** → adapter → IR →
   trivial placement + router → previewer stub. Typed throughput flows through naturally (it's
   part of the documented, self-contained export), so the boundary is lower-risk than the old
   gtnh-flow-internals plan.
4. **Dataset** (`dataset/`) — the **physical** rules (footprints/faces/physical tiers/
   cell→block), keyed to gtnh-factory-flow's machine IDs. Recipes/throughput/identity come from
   its dataset, so this is smaller than before but still the biggest hand-authored piece.
   (Recommended but *declined this round*: pin one concrete demo line first to bound it.)
5. **Placement** (`placement/`) — SA/LNS + cheap routing-aware cost; orientation as a variable.
6. **Router** (`router/`) — free-form per-commodity A* with channels-per-edge cap + cell→block
   realizability + rip-up-and-reroute + ME toggle; then the shared-amperage power primitive.
7. **Solver** (`solver/`) — place↔route feedback loop + anytime budget.
8. **Validator** (`validator/`) — independent checks; **build tests alongside every step.**
9. **Previewer + build guide + CLI.**

### Parallel lanes (after IR lands)

| Lane | Tasks | Depends on |
|------|-------|------------|
| A | adapter (parse gtnh-factory-flow export) → schema/dataset version pinning | IR |
| B | dataset | IR |
| C | placement | IR |
| D | router → power | IR |
| E | validator | IR (+ shared rule data) |
| F | previewer, build guide | output schema |

Launch A–F in parallel worktrees. **Merge C + D before the solver loop** (both feed it —
coordinate the placement↔router interface). Then the CLI.

## v1.1 and beyond (deferred)

- **Theoretical-min-volume mode** — full-3D router, drop the buildability constraint; explore
  the packing lower bound (may be unbuildable, still rule-valid).
- **Formal Pareto / multi-candidate output** — compactness vs routing-slack vs buildability,
  with a congestion heatmap. (v1 covers seed comparison by re-run + preview.)
- **Round-trip `.schematic` import (Approach C)** — bootstraps the dataset from real builds,
  yields a harvested validation corpus + a compactness benchmark.
- **Paste-ready `.schematic` export** — gated on the Assignment's fidelity result.
- **GT++ quad/nonuple multi-fluid pipes** — channel-packing within a single block.
- **EnderIO conduits** — early/mid-game item transport backend.
- **CP-SAT placement backend** — optional exact solver for small sub-blocks.
- **PyPI publish automation** — once there's a usable release.

## Known risks carried forward

- **No automated correctness ground truth in v1** beyond self-consistency — mitigated by the
  in-game spot-check + golden corpus, fully addressed by the v1.1 import corpus.
- **Dataset scope is loosely bounded** until a concrete demo line is pinned (declined this
  round).
