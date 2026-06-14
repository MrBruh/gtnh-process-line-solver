# Design

Problem framing and the chosen approach. (Originated from a `/office-hours` session and an
engineering review; this is the canonical, repo-resident copy.)

## Problem statement

GT:NH has mature tooling for the *logical* side of factory design (`gtnh-flow` and forks
balance ratios/power into a flow graph) but nothing for the *physical* side. This project
takes a balanced process line and decides **where every machine physically goes** and **how
pipes and wires route between them** in the world, so the line works — optimized for
compactness under fixed constraints (pinned I/O chest locations, reserved cells, a bounding
region). It is **place-and-route** (VLSI/PCB problem family) + the **facility layout
problem**, retargeted to GregTech with full physical fidelity.

The primary v1 deliverable is an **interactive 3D previewer + a layer-by-layer build
guide**. A paste-ready schematic export is a later, fidelity-gated milestone.

## What makes it worth building

- The logical-balancing space is crowded; the **physical-layout space is empty** — no
  existing tool places machines / routes pipes for GT:NH. This is the wedge.
- It's a meaty optimization + algorithms project (constraint solving, metaheuristics,
  capacitated multi-commodity routing) on a domain the author knows — a strong learning
  vehicle that produces a tool people will use.
- The previewer turns a non-deterministic solver into something you can see, compare, and
  trust.

## Premises (agreed)

1. **Real gap, not a reinvention** — this is the layer past `gtnh-flow`, not a rebuild.
2. **gtnh-flow gives the logical graph, not physical data** — the solver supplies a separate
   GT-version-specific dataset of footprints + pipe/wire tiers + placement rules. This data
   layer is the bulk of the real work.
3. **"Works" needs rule-aware routing, not just connectivity** — throughput caps,
   one-fluid-per-pipe, wire limits/loss, accessible I/O faces. "Connected" ≠ "correct."
4. **The optimizer is assembly of proven techniques, not novel research** — place-then-route;
   SA/LNS placement; A* maze routing with rip-up-and-reroute.
5. **It needs a distribution path** — a pip-installable CLI/package producing the previewer +
   build guide (and later the export).

## Chosen approach

**Optimizer core first** (a deliberate, eyes-open choice: the rigorous solver is the point
and the learning payoff, accepting a slower path to a usable tool). A small integration
spike de-risks the gtnh-flow boundary before the solver is built.

The full set of engineering decisions (placement↔routing feedback loop, IR contract,
fork boundary, validator independence, free-form routing + realizability invariant,
shared-amperage power model, etc.) lives in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Success criteria

- For a real small-to-medium balanced line, the solver produces a layout that is
  geometrically valid (no overlaps, within bounds, pinned I/O honored) **and** rule-valid
  (every routed commodity within throughput/tier limits; ME-toggled commodities excluded and
  endpoint-placed).
- When no valid layout exists, the solver reports the tightest violated constraint and a
  suggested relaxation — never a silent failure or a silently-invalid layout.
- The previewer renders any candidate in 3D and supports comparing multiple seeds.
- The build guide is precise enough to reproduce the layout by hand.
- (Later) An exported schematic pastes into GT:NH via Schematica-Plus and runs.

## Platform reality

GT:NH is **Minecraft 1.7.10 / Forge**. Litematica does not support 1.7.10; the in-game
schematic consumer is **Schematica-Plus** (classic `.schematic`, numeric block IDs). See
[`DOMAIN.md`](DOMAIN.md). There is **no headless GT simulator**, which shapes the test
strategy ([`TESTING.md`](TESTING.md)).
