# Texturing pipes and cables in the previewer

**Status: research spike, 2026-09-01. Nothing here is built.** This is the long form behind issue
#105, a findings document to decide from rather than a contract. Issue #4 stays the feature issue
and should be updated from this.

Machines already render with real GT sprites (`docs/dataset-extraction/texture-resolution.md`).
Routes are the remaining untextured geometry in the previewer. This spike asks what it would take to
put the real block on every cable and every pipe, and finds that the answer splits cleanly into a
bug in our own extractor, a decision the solver has never made, and a render change that is smaller
than it looks.

---

## 1. What the previewer does with routes today

Nothing texture-related. Routes never enter the texture pass at all.

The machine path runs `build_scene` -> `texturize_scene` -> per-face keys -> baked PNG -> `data:`
URI -> one `BoxGeometry` per cube. `texturize_scene` iterates `scene["machines"]` and only that
(`previewer/textures.py:511`, `:552`), so the whole route layer is outside it.

Routes take a separate path:

- `previewer/scene.py:43-47` gives each commodity one flat colour, item `#3cb44b`, fluid `#4363d8`,
  power `#ffd000`. That is the entire material model.
- `previewer/scene.py:82-110` emits `{netId, commodity, color, segments, terminals}`; a segment is
  `{from, to, thickness}`, a terminal `{machine, face, cell, thickness}`. No block, no meta, no
  texture key.
- `previewer/html.py:358-389` draws them: `const cross = isPower ? 0.09 * Math.sqrt(e.thick) : 0.07`
  (`:380`), then an untextured `MeshStandardMaterial` node cube per cell and one stretched bar per
  connected direction.

So the texture summary line that mentions only machine types is reporting accurately. There is
nothing about routes for it to say.

**One piece of prior art is already there and worth keeping.** `html.py:360-378` builds, per route
cell, the exact set of directions that cell connects in, from the segments plus the terminal face
normals. That is precisely the connection mask GT's own renderer needs (section 2.3). It just lives
in JavaScript, downstream of the tested Python.

---

## 2. What GT actually does

Every claim below comes from the pinned checkout at `C:\Users\mdnss\Dev\gtnh-reference`, read
directly.

### 2.1 Class model, and the fact everything else follows from

```
CommonMetaTileEntity
|- MetaTileEntity      MetaTileEntity.java:67    machines, hatches, hulls
`- MetaPipeEntity      MetaPipeEntity.java:56    cables, pipes, frames
```

`MetaPipeEntity` is a **sibling** of `MetaTileEntity`, not a subclass. There are two base tile
entities to match, also siblings: `BaseMetaTileEntity` (`:91`) and `BaseMetaPipeEntity` (`:60`).

| Thing | Class |
|---|---|
| cable and bare wire | `MTECable` (`MTECable.java:59`), the two distinguished only by the `mInsulated` field (`:64`) |
| fluid pipe | `MTEFluidPipe` (`MTEFluidPipe.java:73`) |
| item pipe, restrictive included | `MTEItemPipe` (`MTEItemPipe.java:35`) |
| frame box | `MTEFrame` (`MTEFrame.java:20`), a full cube, not a noodle |

All of them live on `gt.blockmachines` with the item damage as the MTE id
(`MetaPipeEntity.java:508-510`). The **block metadata** is not the id: it is the tile-entity base
type, and metas 4 through 11 are the ones that construct a `BaseMetaPipeEntity`
(`GTMod.java:227-239`, `HarvestTool.java:10-15`). A cable takes 8 or 9, a pipe 4 through 7.

Ids are hand-assigned in `gregtech/loaders/preload/LoaderMetaPipeEntities.java`. A cable material
reserves 12 consecutive ids: six bare wires at `wireGt01/02/04/08/12/16`, then six insulated cables
at `cableGt...`, unless `.disableCable()` makes it wire-only (superconductors). Fluid pipes take 5
consecutive plus 2 multi-fluid; item pipes take 10, five normal and five restrictive.

**A trap for any extractor.** `GTPPMTEFluidPipe` passes `mMaterial = null` upward
(`GTPPMTEFluidPipe.java:26, 34`) and keeps its colour in a separate `PipeStats` enum. Reading
`mMaterial.mIconSet` directly rather than calling `getTexture` will NPE on every GT++ pipe.

### 2.2 Texture layers, and the wrong overload

`MetaPipeEntity` declares two `getTexture` methods (`MetaPipeEntity.java:477-486`). The
`IMetaTileEntity` one takes `ForgeDirection facing` and returns `ERROR_RENDERING`. The one pipes
actually override takes `int connections`. Every pipe class overrides only the `int` overload.

The renderer reaches it through `BaseMetaPipeEntity.getTextureUncovered` (`:715-735`), which passes
`mColor - 1` as `colorIndex` (so unpainted is -1) and, as the fifth argument, **not** an active flag
but "is this face an open end", true when the connection bit for the face is set or the pipe is
completely unconnected.

Cable layers (`MTECable.java:111-159`):

| variant | face | layers, bottom to top |
|---|---|---|
| bare wire | any | `materialicons/<SET>/wire` x `Dyes.getModulation(colorIndex, material.mRGBa)` |
| insulated | connected or open end | the same wire sprite x `material.mRGBa`, dye ignored, then `iconsets/INSULATION_<size>` x `getModulation(colorIndex, CABLE_INSULATION)` |
| insulated | not connected | `INSULATION_FULL` alone |

Painting a cable therefore recolours only its insulation, while painting a bare wire recolours the
conductor. Thickness picks the insulation sprite: 0.25 TINY, 0.375 SMALL, 0.5 MEDIUM, 0.625
MEDIUM_PLUS, 0.75 LARGE, 0.875 HUGE.

Fluid pipes (`MTEFluidPipe.java:168-217`) use `pipeSide` along the barrel and a size sprite on the
bore, with a 15-entry restrictor overlay keyed by a 4-bit per-face border mask (`:87-105`). Item
pipes (`MTEItemPipe.java:93-154`) use the same ladder and stack an untinted `PIPE_RESTRICTOR` on
every face of a restrictive pipe.

**The sprites are greyscale.** Measured on the shipped PNGs rather than assumed: `wire.png` has a
max channel spread of 2, `pipeSide.png` and the size sprites 0. The colour is entirely in the
multiplier, so a dumper that drops the RGBA renders the whole pack as grey noodles. The multiply is
per-vertex, RGB only, with a per-side lightness of `{0.5, 1.0, 0.8, 0.8, 0.6, 0.6}` for
down/up/N/S/W/E (`SBRContextBase.java:43, 75-77`). `Dyes.CABLE_INSULATION` defaults to RGB 64/64/64
(`Dyes.java:41, 46-52`), a heavy darkening that cannot be skipped.

Icon names resolve through `TextureSet` (`:95-104`, `INDEX_wire = 69`, pipe prefixes 77 to 85) and
`CustomIcon` domain-qualifies them (`Textures.java:2402-2406`), giving
`assets/gregtech/textures/blocks/materialicons/<SET>/pipeMedium.png`. That is exactly the family
`TextureDumper.iconRef` already handles, so no new naming logic is needed. There are no `_OVERLAY`
siblings for any pipe or wire sprite, so a single icon plus a tint is the whole layer.

### 2.3 Geometry

All of it is in `MetaPipeEntity.renderInWorld` (`:137-290`), which is `@SideOnly(Side.CLIENT)` and
therefore has to be reimplemented rather than called.

For thickness `t`: `pipeMin = (1 - t) / 2`, `pipeMax = (1 + t) / 2`. When `t >= 1` the block renders
as six full faces. Otherwise it is a uniform axis-aligned cross: a core cube, plus one arm per
connected side running out to the block edge, with **the same cross-section as the core**. There is
no fatter node. A straight run collapses to a single full-length box (`:179, 192, 205`), an
unconnected pipe is a floating stub cube (`:168`), and an unconnected face of a connected pipe is
capped flush.

Thicknesses, from the constructors:

| family | thickness |
|---|---|
| bare wire 1x to 16x | 0.125 / 0.25 / 0.375 / 0.5 / 0.625 / 0.75 |
| insulated cable 1x to 16x | 0.25 / 0.375 / 0.5 / 0.625 / 0.75 / 0.875 |
| fluid pipe tiny to huge | 0.25 / 0.375 / 0.5 / 0.75 / 0.875 |
| fluid pipe quadruple, nonuple | 1.0, a full cube |
| item pipe tiny to large | 0.25 / 0.375 / 0.5 / 0.75 |
| item pipe huge | 1.0, a full cube; the restrictive huge is 0.875 |

Today's previewer draws `0.09 * sqrt(thickness_multiplier)`, so a 1x cable renders at roughly a
third of its real size and the ladder has the wrong shape.

### 2.4 What is player state, and therefore not dumpable

The connection mask `mConnections` (`MetaPipeEntity.java:61`, bits in `IConnectable.java:10-17`) is
set either automatically by `checkConnections()` (`:700-709`) or by the player with a wire cutter,
soldering iron or wrench. The fluid pipe's `mDisableInput` border mask is likewise player state and
defaults to 0. Neither can be observed outside a world, and neither should be: an extractor should
**enumerate** masks, not observe one. This is the same point `pipe-connections-player-controlled`
already records for routing.

---

## 3. Why the dataset has none of it

`data/2.8.4/textures/manifest.json` (9932 blocks, 2186 icons, 6769 gaps) contains zero cables and
zero pipes. They are all in the gaps list under one reason, `place threw NullPointerException`,
1185 entries, every one on `gregtech:gt.blockmachines`. Counted by meta band:

| band | count | what |
|---|---|---|
| 1200-2999 | 546 | bare wires and insulated cables |
| 5000-5799 | 292 | fluid and item pipes |
| 4096-4999 | 223 | frame boxes |
| 30000-30999 | 93 | GT++ conduits |
| 32737+ | 24 | goodgenerator wires and cables |
| other | 7 | tectech and gtnhlanth pipes |

Excluding frames, **962 cable and pipe blocks are being dropped**.

### 3.1 Root cause, two lines

```java
// tools/gtnh-extractor/.../TextureDumper.java:950
world.setBlock(OX, OY, OZ, block, 0, 3);   // block metadata hardcoded to 0
// :957-958
IMetaTileEntity mte = imte.newMetaEntity(bmte);
bmte.setMetaTileEntity(mte);
```

Meta 0 builds a `BaseMetaTileEntity`, but a pipe needs 4 through 11 for a `BaseMetaPipeEntity`.
`BaseMetaTileEntity.setMetaTileEntity` then takes its error branch, because a `MetaPipeEntity` is
not a `MetaTileEntity`, and the branch logs `getInventoryName()`, which dereferences a base that is
still null (`BaseMetaTileEntity.java:1685-1694`, `CommonMetaTileEntity.java:378-383`). The NPE
escapes `place()` and the gap is filed.

### 3.2 The second bug, hiding behind the first

```java
// TextureDumper.java:474-475
ITexture[] layers = mte.getTexture(base, ForgeDirection.getOrientation(side), ForgeDirection.NORTH,
                                   -1, active, false);
```

That is the `ForgeDirection` overload, which for a pipe returns `ERROR_RENDERING`. Fixing placement
alone would replace 1185 honest gaps with 1185 blocks of confident garbage, which is worse than the
gap. Both have to move together.

### 3.3 The cheap fix is to stop placing

Every pipe `getTexture` body is a pure function of its own fields and arguments; none of them
dereferences the `IGregTechTileEntity` parameter. Checked across `MTECable`, `MTEFluidPipe`,
`MTEItemPipe`, `MTEFrame`, `GTPPMTECable`, `GTPPMTEFluidPipe`, `MTEPipeData`, `MTEPipeLaser` and
`MTEBeamlinePipe`. So the extractor can call

```java
((MetaPipeEntity) imte).getTexture(null, side, connectionMask, -1, isOpenEnd, false)
```

on the registered prototype in `GregTechAPI.METATILEENTITIES[id]` and never place a block.
`getThickness()` is server-safe (`MetaPipeEntity.java:459-464` short-circuits on `isClientSide()`),
`TextureFactory` carries no `@SideOnly`, and the icon containers are the same two families
`populateIconNames` already resolves.

---

## 4. What the solver would have to decide

The gap on our side is **identity**, not geometry. A cell's axis, its neighbour mask and its
thickness are all recoverable from `segments` plus `terminals` today.

| Fact | Where | Status |
|---|---|---|
| cell path and unit hops | `Segment.start/.end` (`ir/output.py:45-53`) | present |
| per-cell neighbour set | derived in JS (`html.py:368-378`) | derivable, not stored |
| commodity | `Route.commodity` (`ir/output.py:72`) | present |
| cable thickness 1x to 16x | `Route.thickness_per_segment` (`ir/output.py:75`) | present, power only |
| voltage tier of the cable | router-internal (`router/power.py:343-346`) | dropped before the previewer; survives only as a convention inside the net id |
| cable material, insulated or bare | nowhere | never computed |
| pipe material, size, restrictive | nowhere | never computed; the model is `_PIPE_LABEL` in `buildguide/core.py:42` |

`docs/DOMAIN.md:60-63` defers per-material cable loss to Phase 2, and nothing in `data/` names a
cable material. So choosing the block is a **domain decision the solver has never made**, and unlike
hatch placement it cannot be read out of the dataset.

Three ways to close it, in increasing order of honesty:

- **(a) Render-only, no contract change.** The previewer picks the block from
  `(commodity, thickness, tier)` through a small table of its own. Tier is available only by parsing
  the net id, which is fragile. This is a preview stand-in of exactly the same class as the
  `Basic` tier-prefix fallback for generically named single-block machines
  (`previewer/textures.py:83-87`), and has to be labelled that way.
- **(b) An additive `LayoutResult` field.** `Route.block_key`, or a per-segment list aligned with
  `thickness_per_segment`. Additive, so no `LAYOUT_RESULT_VERSION` bump under the rule in
  `ir/__init__.py:19-21`; `Route.terminals` and `auto_connections` are the precedent. It obliges the
  router to choose a material, which needs new dataset rule data, and the validator would then be
  expected to check it independently (`docs/ARCHITECTURE.md` decision 4).
- **(c) Full lowering to blocks.** `ir/output.py:7` already says routes are cell paths lowered to
  concrete blocks only at export. That is the `.schematic` lane (issue #96), not a preview fix.

---

## 5. Textures and what they cost

**Already in the jar we fetch.** `iconsets/INSULATION_*` (7), `materialicons/<SET>/wire` and
`pipeSide`, `pipeTiny` through `pipeNonuple`, `iconsets/PIPE_RESTRICTOR*` (16). Resolving the
texture set of every material that `LoaderMetaPipeEntities` uses gives **12 distinct sets**, and all
12 ship all 9 sprites. So roughly **131 PNGs cover the entire cable and pipe surface of the pack**,
against 962 blocks: the material identity lives in the RGBA multiplier, not in separate art. Neither
`jar.py` nor `bake.py` needs changing, and `bake_layers` already composites sprite times tint the
way GT does.

**The committed manifest cannot carry them yet.** `tools/derive_small_manifest.py:89-95` keeps an
MTE only when its display name contains an example machine-type name, which no cable can match, and
a block entry only when one of the two committed multiblock fixtures places it. Pipes and cables
need a third, explicit rule.

Cost, against a 196 KB committed manifest of 30 blocks and 97 icons:

| scope | entries | approximate bytes |
|---|---|---|
| one cable entry, two layers, one state | 1 | 616 |
| every wire and cable meta | 546 | ~240 KB, doubling the manifest |
| cables plus pipes | 838 | ~336 KB |
| only what the shipped examples use | 12 to 18 | ~10 KB |

Example-scoped is an order of magnitude cheaper than either alternative and is clearly the right
first cut. (The per-entry figure assumes a cable resolves as one `sides: all` group with two layers;
a connection-keyed dump would multiply it. See section 8.)

---

## 6. Recommended phasing

1. **Fix the extractor, both bugs together.** Prototype-based dump through the `int` overload, no
   placement. Enumerate connection masks rather than observing one; the minimum useful set is
   isolated plus straight-run, which is what GT's own inventory render uses
   (`MetaPipeEntity.java:117-133`). Record the fifth boolean as "open end", not as "active".
2. **Decide the manifest schema for a connection-dependent block**, then teach
   `derive_small_manifest` its third rule and commit the example-scoped subset.
3. **Author the material policy** in `docs/DOMAIN.md` and `dataset/`: tier to cable material,
   throughput to pipe material and size. This is the real work and the real risk.
4. **Answer the contract question** in section 4 before the previewer hard-codes an answer to it.
5. **Previewer.** Lift the connection-mask derivation from `html.py` into Python where it can be
   tested, emit route cubes into the same `blocks` and `textures` pools, use GT's real thickness
   table, fall back to today's coloured bars for any cell that did not resolve, and extend the
   summary line so route coverage is reported.

Steps 1 and 2 are worth doing even if the previewer work never happens: they turn 962 silent gaps
into real data, and `.schematic` export needs the same blocks.

**Fixture note.** The sand line cannot exercise the pipe half at all: it solves to one power route,
four segments, five cells, thicknesses 1x and 2x, and zero pipes, because all four item nets resolve
by auto-output. Nitrobenzene is the fixture for pipes.

---

## 7. Why this is easier than hatch placement

`docs/hatch-placement/plan.md` is blocked on rotation-aware geometry, because a hatch's sprite has a
yaw and `_rotate_side` cannot express a vertical facing (`previewer/textures.py:330-335`). Cables
and pipes are **materially isotropic**: the same sprite on all six faces, with the shape coming from
geometry rather than from art. None of the rotation work is a prerequisite here. That, plus the fact
that the sprites are already local, is the argument for doing routes first.

---

## 8. Open questions and risks

1. **Inventing a material is the one unrecoverable failure.** A cable rendered in Tin when the build
   needs Aluminium is plausible, confident and wrong, which
   `docs/dataset-extraction/texture-resolution.md:89-90` already names as the failure that cannot be
   recovered downstream. A stand-in must be labelled as one, or restricted to cases where the
   material is genuinely determined. A mismatched cable is worse than today's honest yellow bar.
2. **One cell, two thicknesses.** The sand line already produces a cell incident to both a 1x and a
   2x segment, and the viewer silently takes the maximum (`html.py:365`). As a coloured bar that is
   harmless smoothing; as a block it is a build instruction. The rule is probably right, but it
   should be written down and tested rather than left implicit in a template.
3. **The manifest's state axis does not fit.** The schema keys `(block, meta, side, state)` with
   `state` in `{inactive, active}`. For a pipe the real axes are side, connection mask, colour index
   and the restrictor border mask. Either the previewer synthesises the geometry from the mask it
   already computes, which is exact and is the recommendation, or the schema grows a dimension.
4. **Manifest key namespace.** Issue #102 already flags that MTE ids and block metas share one
   `<registry>|<meta>` namespace with no live collisions. Adding several hundred ids in the 1200 to
   5800 band is exactly what turns a latent collision live. Re-check rather than assume.
5. **`Dyes.CABLE_INSULATION` on a dedicated server.** The 64/64/64 default is read from the
   `@Config.DefaultInt` annotations and `ConfigurationManager.registerConfig(Client.class)` runs in
   `GTMod`'s unconditional static block (`GTMod.java:181-184`), but this was not verified in a live
   server run. If it were 0 the dumper would silently record black insulation. Worth a one-line
   assertion in the extractor rather than trust.
6. **Addon coverage is partly indicative.** The band totals above are measured from a real run and
   are authoritative. The per-builder arithmetic behind them is not: the GT++ band came out at 93
   against a static count of 104, almost certainly because Thaumcraft and EnderIO gates were unmet
   in that run. Several addon `getTexture` bodies were not read.
7. **`Terminal.port_id` is dropped by `scene.py:93-101`**, the same gap `docs/hatch-placement/plan.md`
   records, so the previewer cannot say which port a lead serves. Relevant only if covers are ever
   drawn on terminals.
