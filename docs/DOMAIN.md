# Domain - GT:NH rules the solver encodes

The knowledge a contributor (or the dataset author) needs but won't necessarily know. These
rules live as *data* in `dataset/` and as *checking logic* in `validator/` (shared data,
independent logic - see [`ARCHITECTURE.md`](ARCHITECTURE.md)).

> If you play GT:NH and spot an error here, fix it - this doc is the reference both the
> router and the validator are built against, so a wrong rule here propagates everywhere.

## Platform

- GT:NH is **Minecraft 1.7.10 / Forge**. Block identity is numeric ID + metadata
  (pre-flattening), which matters for the eventual export.
- **Litematica does NOT support 1.7.10.** The in-game schematic consumer is
  **Schematica-Plus**, which can paste tile-entity NBT including GregTech machine
  configurations. Target classic `.schematic`, not `.litematic`.
- There is **no headless GT simulator**, so true correctness is only verifiable in-game.

## Machine faces

- A machine has six faces. The **front face** (set by orientation) is the working face and
  carries **no item/fluid I/O**. The solver chooses orientation so required I/O faces stay
  routable.
- The **other five faces** can each be input OR output of items or fluids. Routing a specific
  commodity onto a face may require a **cover** (conveyor for items, pump/regulator for
  fluids); a cover occupies that face and is recorded for the build guide/export.
- A machine **auto-outputs to a single face**, carrying **either items or fluids, not both**.
  A machine emitting both an item and a fluid output uses auto-output for one and a
  cover-driven output on another non-front face (or ME) for the other.
- **Required-I/O-face reachability is a HARD constraint** - a blocked required output face
  means the line doesn't run. "Convenient access" is a soft preference.

## Fluids and items (pipes)

- A fluid pipe line carries **one fluid type**; a pipe has a per-tick throughput cap by tier
  (hard constraint). Items routed physically have an analogous per-tick cap by tier.
- v1 ships **single-channel** GT pipes/cables. Transport is **pluggable per commodity**;
  planned backends: GT++ quadruple (4-channel) and nonuple (9-channel) fluid pipes (turn
  per-cell fluid routing into channel-packing), and EnderIO conduits for early/mid-game.

## Power (shared-amperage net)

Power is **not** a disjoint per-pipe flow. Multiple machines pull amperage down a shared
conductor; the cable burns if total amperage exceeds its rating. Model it as a shared net
where load **sums** along shared segments (Steiner-tree-like):

- **Voltage tier** of a cable segment follows the **machine voltage tier** it serves (cable
  rated ≥ that voltage).
- **Machines draw fractional amps on average.** A machine pulls whole packets (1 amp = one packet
  of up to tier voltage) into an internal buffer only when it has room, so a 16 EU/t LV machine
  takes a 32-EU packet every other tick - an **average of 0.5 amps**, not a whole amp. Its load on
  the net is `eut / delivered_voltage`, un-rounded; loads **sum** along shared segments and only
  the aggregate rounds up to whole amps (per segment for cable thickness, per tier for the source
  feed). Rounding per machine would overstate the draw - three 16 EU/t hammers run on a 2 A LV
  feed, not 3 A (confirmed in game).
- **Voltage loss over distance.** A cable loses voltage per block travelled, so a machine `d`
  blocks from the source receives `tier_voltage - loss·d`, not the full tier voltage. The solver
  keeps the source at the machine's tier and *thickens the cable to compensate*: a machine's
  load is sized at its **delivered** voltage (`eut / (tier_voltage - loss·d)`), so a machine
  farther from the source loads the net **more** for the same `eut`. A run so long that the
  delivered voltage reaches 0 cannot be powered at that tier and is reported infeasible. Loss is a
  flat **1 EU/block for every tier** for now (a simplifying assumption; per-material cable loss is
  Phase 2 dataset work).
- **Thickness** (1x / 2x / 4x / 8x / 16x, **16x max**) is sized to the **summed load** through
  that segment, rounded up to whole amps.
- A segment needing **> 16x** must split into **parallel runs** or move to a **higher voltage
  tier** (more power per amp).
- **The synthesized source is fed from outside.** A plan export has no power node, so the
  adapter invents one source per voltage tier; *how* it is powered is left to the builder. The
  layout reserves **the source's front face as the external feed entry**: placement pins that
  face flush on the region boundary (validator-enforced), internal cables use the other five
  faces, and the builder runs power in through the wall the front touches.

## Boundary storages (the adapter closes the line)

A plan export names recipes and flows but not the containers at the line's edge, so the adapter
synthesizes them - the same line-closing move as the per-tier power source above:

- **Inputs** map to a boundary **Super Chest** (items) / **Super Tank** (fluids) the builder
  fills; nothing in the line feeds it, so it only *sources*.
- **Outputs** are closed the same way: for every machine OUTPUT port that no net consumes, the
  adapter adds a **Super Chest/Tank plus a net** wired to it at the port's recorded rate, so the
  finished product is collected instead of exiting into thin air (the sand line auto-outputs its
  sand straight into this collection chest, still zero pipes). Such a storage only *sinks*.

These boundary storages are unpowered blocks that accept I/O covers on their faces (so covers ride
storage faces, never pipes); `system_io` surfaces the only-*source* ones as the line's inputs and
the only-*sink* ones as its outputs.

## ME networks (AE2)

Each commodity (items, fluids, power) can be **toggled to ME** individually. A toggled
commodity is removed from physical routing: today it is simply **skipped everywhere** (no route,
no terminal, no placement/cost term). Placing the appropriate ME endpoint (interface / bus / P2P)
on a machine face in its stead is **planned** (Phase 2); v1 does not model ME channel limits.
Default is to route all three physically.

## Multiblocks

Represented as a **bounding box + controller-face and hatch/bus-face metadata** (multiblocks
do I/O through hatches/buses on casing faces). Full internal StructureLib materialization is
deferred to the export milestone. Fallback if metadata is too costly to author early:
single-block-machines-only for the first solver, multiblocks added via round-trip import.

## Cell↔block realizability (don't let the abstraction lie)

Placement/routing run on a coarse cell grid; block-accuracy is materialized only at export. A
cell boundary has finite physical room, so the router enforces a **margin → max-channels-per-
edge** cap and a **cell→block realizability** check fed back into search. Without it, the
solver can certify layouts that don't physically fit.
