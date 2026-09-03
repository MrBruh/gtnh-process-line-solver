# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Hatches render as real GT hatch blocks, at their own facing, vertical ones included
  (`previewer/`, `tools/`).** A hatch was previously invisible: the previewer drew the casing block
  it displaced. It now resolves to the actual `(block, meta)` GT would place - an `Input Bus (HV)`,
  an `LV Energy Hatch`, a `Muffler Hatch` - **replacing** that casing cube rather than adding one,
  which is what makes a hatch cost a casing cell. The join is on the MTE's `source_class`, exactly
  what `HatchElement.mteClasses()` names, because the display names come in two shapes
  ("Input Bus (LV)" against "LV Energy Hatch") and a subclass keeps its parent's kind the way GT's
  own adders do. Nitrobenzene places 45 of them across 14 distinct blocks, 7 of them facing up or
  down.

  **A vertical facing could not previously be expressed at all.** `_rotate_side` permutes the four
  horizontal sides and returns UP and DOWN unchanged, and the extractor pins `aFacing` to NORTH for
  every MTE it walks, so the front overlays exist on that one side only. A hatch face is therefore
  **spliced**: the target side's own background, plus NORTH's overlays where the side is the one
  the hatch faces. That is exact rather than approximate - `MTEHatch.getTexture` computes its
  background without consulting either `side` or `aFacing`, so a six-facing re-dump would write
  byte-identical stacks. Taking the background from the target side is essential: UP and DOWN carry
  `MACHINE_<TIER>_TOP`/`_BOTTOM` against the horizontals' `_SIDE` in every hatch in the pack.

  A hatch is not turned by its machine's yaw, only by its own facing, and the texture pool key
  carries that facing - without it an UP-facing and a NORTH-facing bus of one type collide and one
  silently gets the other's bake.

- **The maintenance hatch is no longer drawn duct-taped.** Its dumped states are inverted: GT flips
  it to `active` the moment it joins a formed multiblock, so the `inactive` stack is
  `OVERLAY_MAINTENANCE + OVERLAY_DUCTTAPE`, the *broken* look. Read straight, every machine in a
  line would show as needing repair - the one skin a builder is meant to react to.

- **The committed manifest carries hatches.** `tools/derive_small_manifest.py` kept an MTE only if
  its display name contained an example machine type, which no hatch can match, so a preview run
  without a local dump skinned none of them. It now also keeps every hatch kind at every tier the
  examples use, resolved through the previewer's *own* `TextureManifest.hatch_block` so the two
  cannot drift. 30 blocks to 52, 204 KB to 310 KB.

- **The scene carries `port` on every terminal and the hatch list on every machine**, so a viewer
  can tie a terminal to the hatch it docks against instead of inferring it from geometry.

- **A layout now says which casing cell every hatch is, and which way it faces
  (`router/hatches.py`).** `LayoutResult.hatches` has existed since the output contract went to
  v1 and has been empty ever since; it is filled now, from three sources. A routed terminal
  becomes a hatch on the casing cell behind it, facing the way it docked. A **free auto-output**
  connection places two - GT still ejects through an output bus's own front face into an input
  bus, so two touching casing blocks are spent even though no pipe is laid, and nothing recorded
  them before. And every dumped machine gets the **upkeep hatches its structure records**: a
  maintenance hatch, plus a muffler wherever GT offered the muffler element, which it only does
  for a controller that pollutes.

  A machine with no dumped structure gets none at all: a single-block machine *is* its own I/O,
  and emitting a bus at its cell would describe replacing the machine with a bus. So sand, which
  is single-block machines and boundary storage throughout, still places zero hatches.

- **The muffler's vent is a routing keep-out, a constraint class the solver had no instance of.**
  `MTEHatchMuffler.polluteEnvironment` calls `getAirAtSide` on its own front facing, so a cable, a
  pipe, a casing or a neighbouring machine in that cell makes it return false and the controller
  stops with `POLLUTION_FAIL`. Hatch placement picks a facing whose outward cell is empty, and the
  validator proves it (`muffler_blocked`), along with a polluting structure that was given no
  muffler at all (`muffler_missing`).

- **Running out of casing cells is an explicit infeasibility, never a retry** (`hatch_budget`).
  The budget is a per-machine total, so no nearby cell and no re-placement can create one; it
  names the machine and what it ran out of room for.

### Changed
- **Placement asks its geometry questions of the boxes, not of every cell (`ir/`, `placement/`).**
  Two predicates in the hot loop walked cell sets whose size is machine *volume*, so a solve got
  slower as the physical dataset gave machines their real footprints - exactly backwards, since
  that dataset is what makes a layout buildable.

  `auto_output_faces` materialized both machines' complete cell sets and scanned for a touching
  pair on each candidate face. The placement loop asks it about 1.5M times per solve, and the
  largest machine in nitrobenzene is 7x7x7: issue #110 profiled it at **70% of a solve**, driving
  88% of every `occupied_cells` step in the run. Two solid axis-aligned boxes touch across a face
  iff the source box stepped one cell that way overlaps the target on all three axes - six integer
  comparisons, independent of volume.

  The fit test beside it (`_best_insertion`, `_relocate`, `_swap`, `_turn_fits`) walked a
  candidate's cells to ask whether the body lies inside the bounding region: 21.7M `in_region`
  calls, about a quarter of what was left. A solid box is in-region iff its two corners are, so the
  new `ir.geometry.box_in_region` answers in six comparisons and the cells are expanded only for a
  candidate that has already cleared it - the overlap tests against `reserved`/`occupied` still
  need them, the region test never did.

  Nitrobenzene (seed 0, physical dataset): **86.5 s -> 22.3 s, 3.9x**; sand 1.0 s -> 0.8 s. Both
  shipped examples produce byte-identical VALID layouts - nitrobenzene over two seeds x all three
  objectives, sand over four - so this is a pure speed change: the search makes the same decisions
  in the same order, and no layout moves. Property tests pin both box formulations against the
  cell-set ones they replace, alongside the existing rotation-equivalence test, plus an exhaustive
  sweep of one body's whole neighbourhood at every facing pair.

- **Two touching machines are no longer enough for a free auto-output.** A multiblock ejects
  through an output hatch's own front face and receives through an input bus's, so the connection
  needs a touching pair of casing cells that can *host* those two hatches - not merely two bodies
  in contact. `ir.geometry.auto_output_faces` still models the loose rule and is still right for
  the placement cost that rewards adjacency and for a single-block machine, whose one cell is its
  own hatch; the router and the validator both apply the tighter one now.

  The **one-auto-output-per-machine** limit is scoped to match. It is a real GT limit on a
  single-block machine (one auto-output face, items XOR fluids) and simply wrong for a multiblock,
  where each output hatch ejects on its own front face independently. A multiblock now spends
  casing *cells* instead, and those claims reach the pipe router, so a pipe cannot dock onto a
  block an auto-output hatch is already standing on.

  Nitrobenzene: 45 hatches placed, floor area 154 against 144, but under the `volume` and
  `balanced` objectives the line comes out at 882 against a 1360 baseline, with 34 power cable
  cells against 60 and 79 route segments against 114. Sand is unchanged.

- **A pipe or cable may only attach where GT would actually let a hatch be built
  (`ir/`, `router/`, `validator/`, `solver/`).** Docking walked every cell of a machine's bounding
  box, so a route could dock against a casing block no hatch can replace, and the layout described
  a structure that will not form. It now walks the machine's recorded hatch slots
  (`Machine.hatch_slots`, turned with the placement), filtered to the cells that accept that
  port's own `HatchElement` kind, and only on faces that are actually **exposed** - which is what
  keeps a hatch off an interior slot, 29% of every slot in the dump, where it would be walled
  inside the structure and reach nothing.

  Kinds stay a **lower bound, never a whitelist**, at three levels: a machine recording no slots
  constrains nothing; a kind some slot names restricts to those slots; a kind *no* slot names is
  allowed anywhere, because a GT hatch adder built from a bare method reference records the cell
  without the kind rather than as refusing it. That last one is load-bearing, not theoretical: the
  Chemical Plant records zero `Energy`-capable cells and nitrobenzene must still power it. Power
  input accepts `Energy`, `ExoticEnergy` and `MultiAmpEnergy`, since 34 of 208 controllers record
  only the TecTech spelling.

  A machine's hatch cells are also now **one shared pool**. A casing cell is one block, so a cell
  holding an input bus cannot also hold an energy hatch - and a claim on the *dock* cell could
  never see that, because one casing cell has up to five free faces. The item/fluid router hands
  its claims to the power router, so the two compete for one budget instead of quietly stacking
  two hatches on one block, which they did before. On a machine with no recorded slots the claim
  falls back to the face, since a single-block GT machine genuinely does take input on one face
  and output on another of the same block.

  The validator proves all of it from its own rotation (`_geometry.hatch_cells`, written from the
  dump's stated convention, not from `ir.geometry.rotated_slot`): `terminal_not_on_hatch_cell` and
  `terminal_hatch_contention`. A property test holds the two derivations to the same answer, and
  the oracle checks every recorded slot of every controller at all four facings lands inside its
  own machine.

  Nitrobenzene's floor area comes back from 152 to 144 as a side effect: the shared pool is what
  the previous release gave up when route-aware docking started starving multi-hatch machines.

- **Which face a pipe docks on is now decided by the route, not by a tuple ordering
  (`router/`).** `dock()` walked `FACE_ORDER` and committed to the first free face before it knew
  where the route had to go, which is why nitrobenzene's item and fluid terminals piled onto
  SOUTH 16 times out of 18 while the route-aware power router spread over four faces. Docking now
  takes *every* free cell outside a usable face and lets multi-goal A* pick: the first leg runs
  multi-source and multi-goal, so the opening pair of faces is chosen together rather than the
  first endpoint guessing before it knows the second, and each later leg starts from the cell
  already chosen. Terminals stay fixed for the whole negotiation exactly as before, so the
  docks-are-not-tradeable invariant is untouched, and an endpoint the chain cannot reach falls
  back to the old first-fit so the failure taxonomy is unchanged. `dock()` itself is gone; its
  only caller was this one.

  Nitrobenzene's terminals now read `west 6, east 6, south 4, down 3, up 3`, its build lays 86
  route segments against 114, and its power cable drops from 60 cells to 43. Its floor area rises
  from 136 to 152: shorter pipes hug machine surfaces, and one machine with three HV energy
  hatches is left with two free dock cells, so the power net cannot dock and the attempt that used
  to win is lost. Per-machine cell claiming is the next lane's job (`docs/hatch-placement/`), and
  the regression is recorded there rather than absorbed into a loosened assertion. Sand is
  unchanged.

### Fixed
- **A machine starved of power is now something the feedback loop can fix, not a lost attempt
  (`solver/`, `validator/`).** A machine takes packets through hatches capped at `Port.max_amps`
  and cable loss shrinks every packet, so its real intake is `sum(max_amps * delivered_volts)`.
  The hatch allowance is designed against a nominal 16-block run, and the validator re-checks it
  at the distance actually routed (`POWER_SUPPLY_INSUFFICIENT`) - deliberately, since only a
  routed layout knows that distance.

  But the solver returned that rejection as `partial_invalid` with an **empty** `failed_nets`, on
  the rule that a layout which routed everything yet failed validation is a solver bug and
  re-placing cannot help. For this one violation that rule is inverted: the shortfall is driven by
  cable distance, so re-placing the machine nearer its source is precisely the fix. With no failed
  net nothing was penalized and the loop wrote the whole attempt off. It now names the starved
  machine's power net, so the placement cost gains its MST trunk-length pull and the next attempt
  pulls the machine in. The empty-`failed_nets` short circuit stays exactly as it was for every
  other violation, including a report that proves a starve *alongside* a real geometric bug.

  A layout rejected only for this reports a `power_supply` infeasibility rather than the generic
  `validation` one, so the advice names the real fix (shorten the run) instead of asking for a bug
  report. `Violation` gained an optional `machine_id` so the machine it names travels structurally
  rather than only in the message prose. Refs #106.

### Added
- **A multiblock's hatch cells now say WHERE they are, and a layout says where each hatch went
  (`ir/` LayoutResult v1, `dataset/`, `adapter/`, `validator/`).** `Machine.hatch_slots` carries
  each hatch-capable casing cell as an offset from the machine's unrotated minimum corner plus the
  kinds it accepts, taken from the same built form as the footprint and the ceiling so all three
  describe one building. The dump measures those offsets from the *controller block*, which is not
  generally the minimum corner (the Coke Oven records a slot at `[-1, 0, 0]`), so the translation
  happens once in the dataset. `InputIR` stays at v3: the field is additive and an empty tuple
  reads exactly as the old behaviour, which is also what a single-block machine, a plan adapted
  without the dataset, and the 23 of 208 controllers that record no slots all get.

  `LayoutResult` gains `hatches`, one `PlacedHatch` per hatch or bus the build needs, at the body
  cell it occupies and the way it faces. **This bumps the output contract to v1, the first bump it
  has had**, because the omission is the breakage: a maintenance hatch and a muffler belong to no
  net and had nowhere to live, so a v0 layout described a machine that will not run, and a consumer
  that ignores the new field keeps describing one. A routed hatch is deliberately recorded twice,
  here and by its `Terminal`, and the validator re-derives the agreement rather than trusting it.

  The validator gains the structural half of the hatch rules: a hatch sits on a body cell of its
  own machine, faces *out* of it, shares its cell with nothing, and agrees with its port's terminal
  (`HATCH_NOT_ON_MACHINE`, `HATCH_FACES_INWARD`, `HATCH_CELL_COLLISION`, `HATCH_TERMINAL_MISMATCH`,
  `HATCH_UNKNOWN_PORT`). Facing outward is the one GT will not catch for us: `IStructureElement`
  takes no facing and every hatch returns `isFacingValid = true`, so a multiblock forms happily
  with a hatch pointing into itself and then moves nothing. `kinds` is documented at every level as
  a **lower bound, never a whitelist** - 61 of 185 controllers record no `Energy`-capable cell, so
  treating an absent kind as a prohibition would manufacture false infeasibilities across a third
  of the dataset. Nothing emits hatches yet; the assignment stage is the next lane.
- **A machine takes power through as many energy hatches as its draw needs, not one connection
  (`ir/` v3, `dataset/`, `adapter/`, `router/`, `validator/`, `system_io`).** A standard GT energy
  hatch accepts 2 amps (`MTEHatchEnergy.maxAmperesIn()`; its tooltip says so, and
  `BaseMetaTileEntity.injectEnergyUnits` enforces it per tick), and a multiblock's intake is the
  sum over its hatches. The solver modelled one connection per machine, so it could certify a
  layout feeding 16 amps into a single MV hatch that can take 2 - unbuildable. The adapter now
  gives each machine the hatches its draw needs, each carrying its share of the EU/t, and the
  partitioner works in hatches rather than machines: a heavy machine's hatches spread over as many
  cable runs as the 16x cap requires, which is what makes it powerable at all. A machine needing
  one hatch keeps the plain `power:in` port id; several suffix it (`power:in#1`, ...).

  The structural budget comes from the dump the extractor already records: `hatch_slots` says, per
  cell, which hatch kinds it accepts, and a multiblock's casing cells take I/O of any kind - the
  Industrial Coke Oven has 17 cells and every one accepts `Energy`, `InputBus`, `InputHatch`,
  `Maintenance`, `Muffler`, `OutputBus` and `OutputHatch` alike. `MachinePhysical` now derives
  those counts and the validator rejects a layout wiring more connections onto a machine than its
  structure can host (`HATCH_CELLS_EXCEEDED`). A machine with no structural record - a single-block
  machine, or any plan adapted without the dataset - keeps exactly one connection, as before.

- **The validator checks that enough power *arrives*, not just that the cable is thick enough
  (`validator/`).** Cable loss shrinks every packet and a hatch passes a bounded number per tick,
  so a machine far from its source can sit on cables that are all correctly sized and still be
  starved. Its real intake is `sum(hatch_amps x delivered_volts)` over its hatches, accumulated
  across routes because a machine's hatches can sit on different nets at different distances; a
  shortfall against its `eut` is reported as `POWER_SUPPLY_INSUFFICIENT`. The opposite case - a
  cable offering a hatch more amps than it accepts - is deliberately not an error: the hatch takes
  its 2 and the under-supply check catches any genuine shortfall.

- **TEMPORARY: a machine needing more than 3 energy hatches is supplied at a higher voltage tier
  (`adapter/`).** Upstream gtnh-factory-flow computes some machines' EU/t against a wrong recipe
  model (#44 and #45: the Industrial Coke Oven has no heating coils, and its parallel caps are
  18/30 rather than 16/32), so a node can arrive drawing far more than its stated tier plausibly
  delivers - the nitrobenzene Coke Oven wants 2355 EU/t at MV, which is 11 hatches. Rather than
  ring it with a dozen hatches, the adapter raises the tier it is *supplied* at until 3 suffice
  (MV -> HV here, which needs 3), the same thing a player would do. This does **not** re-derive the
  recipe: a real tier change also re-overclocks, moving both `eut` and the parallel count, which
  only the exporter can do. **Remove it once those upstream fixes land.** It applies only to
  machines with a structural record, so a plan adapted without the dataset is untouched.

  With these three, **the nitrobenzene example now solves `valid`** - the first time it has. It
  supersedes the "needs parallel runs or a higher tier, still Phase 2" caveat below: parallel runs
  for one machine are what the hatch model delivers.

- **A voltage tier drawing past the 16x cable cap is now split across several power sources
  (`adapter/`, `system_io`).** A shared-amperage trunk sums every machine hanging off it, so one
  source per tier meant the segment at the source carried the whole tier: the nitrobenzene line's
  MV tier wanted 21 amps against a cap of 16 and was rejected outright, no matter how it was
  placed. The synthesis now bin-packs each tier's machines by nominal amp load (first-fit
  decreasing) and gives every group its own source and net, so a tier that does not fit one run
  gets as many runs as it needs. A tier that fits keeps its old ids (`power-source:MV`,
  `power:MV`); one that splits suffixes them (`power-source:MV#1`, `power:MV#2`, ...), so the
  common case reads unchanged in the build guide and previewer.

  Partitioning uses the *nominal* (at-source) amp load, because cable distances do not exist until
  placement has run. That deliberately reserves no headroom for voltage loss: a group that loss
  later pushes over the cap is still reported by the router, never silently accepted. A machine
  whose own draw exceeds the cap is put in a group by itself rather than folded in with a
  neighbour - no partition can help one machine's single feed, and burying it in a shared trunk
  would hide the real problem behind a misleading number. That case needs parallel runs or a
  higher tier, which is still Phase 2 (`docs/ROADMAP.md`).

- **`system_io` now reports feed amperage per source as well as per tier.** The build guide states
  a wiring spec per source block, and it was reading the per-*tier* total, which was correct only
  while a tier had exactly one source. With the split above it charged every source of a tier the
  whole tier's amps: the nitrobenzene MV source feeding a lone 120 EU/t Distillation Tower asked
  the builder for 20 A instead of 2. `SystemIO.power_amps_by_source` attributes each machine's load
  to the source on its own net, and the guide renders that; `power_amps_by_tier` stays as the
  tier-wide summary the previewer shows. A power net without exactly one source is skipped rather
  than guessed at, since the validator rejects that layout anyway.

- **Every block the shipped example lines place now renders with its real GT sprite
  (`tools/gtnh-extractor/`, `previewer/`, GitHub #98).** Four mechanisms, because the single symptom
  ("the block is grey") had four unrelated causes, each needing its own fix:
  - *Icon domain.* `iconName()` hardcoded the `gregtech` domain, which put 46 unfetchable paths in
    the shipped manifest: 17 GT++ icons pointed at `assets/gregtech/.../TileEntities/` (a directory
    GT5U does not have - they live under `assets/miscutils/`), and 29 came out double-prefixed as
    `gregtech:gregtech:icons/...`, a path with a literal colon in it. The domain now comes from the
    container's own `mModID`, or from an already-qualified `mIconName`. One jar still serves all of
    them: GT5-Unofficial is a monorepo and ships all 20 asset domains.
  - *Custom icon containers.* GT's client-only icon-load hook is what populates every custom
    `IIconContainer`'s `mIcon`, so on a server they answer `getIcon` with null. Since each one
    self-registers into `GregTechAPI.sGTBlockIconload`, that public list is a complete server-side
    registry of them, and injecting a named icon into all 11,766 fixes GT++, kekztech and the rest
    generically - no per-mod table, and it reaches instances held in private statics that walking any
    one holder class would miss.
  - *ITexture accessors.* Some blocks expose a `getTextures(int)` / `getTexture(int)` that carries no
    `@SideOnly` and never dereferences the icon. Preferred over `getIcon` wherever present: it cannot
    hit the side-stripping cliff, and it carries the per-layer tint and glow that the single-icon path
    discards. This is what makes coils render, with their real active/inactive pair, and frames carry
    their per-material tint.
  - *Casing table.* The rest declare `getIcon` `@SideOnly(CLIENT)`, so the method is deleted outright
    and no reflection can reach the mapping. Ten families are transcribed from GT source, verified at
    startup against the live constants (a GT bump that moves one is now a loud log line rather than a
    silently wrong sprite), and kept an explicit allowlist: a generic "any block with a stripped
    getIcon" rule would have skinned every GT machine hull as an LV casing side, since
    `BlockMachines.getIcon` is a vestigial stub returning one constant for every meta.

  Frames additionally needed the meta scan widened: they are keyed by GT material id (up to 1000),
  not by world block metadata, so `gt.blockframes|316` was never probed at all - which is why the
  single most-referenced family in the dump stayed grey. That widening is allowlisted too, after a
  blanket version emitted 876 metas for the coil block, whose accessor answers any meta through its
  `default` arm.

  A fifth mechanism covers the third-party tail: many blocks put the `@SideOnly` on the *resolved*
  `IIcon[]` while the strings that NAME those icons are un-annotated and survive untouched
  (bartworks' and GoodGenerator's `textureNames`, vanilla's `textureName` behind
  `setBlockTextureName`). Reading those recovers bartworks glass, the whole GoodGenerator casing
  family, gtnhlanth and kekztech without any table at all, carrying the per-meta glass tints with
  them. Two traps worth naming, since both look callable and are not: `Block.getTextureName()` *is*
  `@SideOnly` while the `textureName` field behind it is not, and bartworks' un-annotated
  `getColor(int)` dereferences the stripped `IIcon[]` and dies with `NoSuchFieldError` - so both must
  be read as fields, never through their accessors.

  A sixth route covers bartworks' werkstoff material casings, which store neither an icon nor a name:
  their sprite is recomputed from the werkstoff registry plus its texture set. Their metas are
  werkstoff ids running to five digits, so they are enumerated from that registry rather than scanned.
  Deliberately not bug-compatible with GT here: upstream derives the texture-set name in a way that
  yields a nonexistent directory for the nine custom sets, so GT itself renders those as a missing
  texture; reading `mSetName` gives a path that exists.

  Net on the local 208-multiblock dump: unresolved `(block, meta)` pairs 330 to 91, multiblocks
  carrying at least one grey block 177 to 40. Both shipped example lines (sand, nitrobenzene) now
  resolve every constituent block, so the unresolved-block warning is silent on each.
- **Texture gaps name the block that has one (`tools/gtnh-extractor/`, GitHub #98).** A multiblock
  controller whose layer stack resolved empty was dropped from the manifest with *nothing recorded
  under its name*: the flattener files its complaint under the offending `ITexture` class instead, so
  29 controller hulls (Eye of Harmony, Forge of the Gods, the Space Modules) went missing with no
  discoverable reason. They now record a gap keyed by the block. The unresolved-shape gap also
  carries the runtime field state that produced it and is deduped per class, which turned a
  212-entry dead end into two lines that named the root cause directly: `mIconContainer=null`,
  because those icon holders are assigned only inside client-only `registerIcons` methods.
- **An unresolved block face draws Minecraft's missing-texture checkerboard (`previewer/html.py`,
  GitHub #98).** It used to fall back to a neutral casing grey, which was actively misleading: a
  great many GT casings genuinely are plain grey, so a missing sprite was indistinguishable from a
  correctly rendered one and the gap stayed invisible in the very view meant to reveal it. Magenta
  and black, matching the convention every Minecraft player already reads as "no texture here".
- **Multiblock casings: tiered machine casings render, and missing sprites are reported
  (`tools/gtnh-extractor/`, `previewer/`, GitHub #98).** `IIconContainer.getIcon()` is
  `@SideOnly(CLIENT)`, so FML strips it from the interface on a dedicated server: a casing reaching
  its icon via `invokevirtual BlockIcons.getIcon()` resolves, but one going through
  `invokeinterface IIconContainer.getIcon()` dies with `NoSuchMethodError`. That is the whole reason
  `gt.blockcasings` metas 10-15 always rendered while metas 0-9 - the tiered machine casings that
  are most of an ExxonMobil Chemical Plant - were always grey. Those metas are now read straight
  from the `MACHINECASINGS_BOTTOM/TOP/SIDE` arrays that hold them, per side rather than flattened to
  one face. The block scan also no longer pre-filters on `IHasIndexedTexture`, which silently
  dropped 75 registry names a dumped multiblock actually uses. (The families still unreachable at
  that point - `gt.blockcasings8`, the coils, the GT++ casings - are closed by the entry below,
  without needing the client-side route of #78.)
- **Untextured blocks are now loud (`previewer/textures.py`, GitHub #98).** A block with no manifest
  entry renders neutral grey inside an otherwise-expanded multiblock, where it is indistinguishable
  from a deliberately plain casing - nothing surfaced it, since the machine keeps no placeholder
  label. `TextureSummary` gains `unskinned_blocks` and the pass warns with the exact
  `<block>|<meta>` list. The extractor likewise records the two skips that previously `continue`d in
  silence, taking the manifest's own recorded gaps from 1846 to 5370: the shortfall was never
  measured before because most of it was invisible.
- **Distillation Towers are sized to their recipe, not to the maximum
  (`dataset/multiblocks.py`, `adapter/`, `previewer/`, GitHub #98).** A GT Distillation Tower routes
  the recipe's fluid output `i` to structure layer `i` and nowhere else, so a tower shorter than the
  recipe's fluid-output count is a *legal* build that silently voids the remainder. `MachinePhysical`
  now carries every built form with its routable-output capacity and picks the smallest that fits:
  on the nitrobenzene line the Distilled Water tower (1 fluid out) reserves 3x3x3 and the Creosote
  Oil tower (5 out) reserves 3x6x3, where both previously reserved 3x12x3. Selection applies only to
  a family that adds exactly one layer and one routable output per step; anything else (a Mega
  Distillation Tower, whose output layer is a 5-block band, or a pre-v2 dump) falls back to the
  largest form, which over-reserves but can never lose product.
- **Multiblocks resolve by controller block, not just by name (`adapter/`, `dataset/`, `previewer/`,
  `ir/`, GitHub #98).** gtnh-factory-flow names a machine by its localized `RecipeMap`, which for a
  GT++ machine is not the controller block's own name the structure dump is keyed by (`Chemical
  Plant` vs `ExxonMobil Chemical Plant`). Those machines therefore missed the structure lookup
  entirely and silently fell back to a 1x1x1 footprint and a lone cube, even though the dump had
  them. A plan exported after gtnh-factory-flow #25 now carries `recipe.source.machineBlock.id`
  (`"<registry_name>@<meta>"`), which `InputIR.Machine.block_key` plumbs to both consumers:
  `PhysicalDataset.get` and the previewer's doc lookup try the block key first and fall back to the
  name, so the join is exact where the export supports it and every pre-#25 plan behaves exactly as
  before. The Chemical Plant now resolves its real 7x7x7 footprint instead of 1x1x1.
- **Dataset schema v2: `variants[].hatch_slots` (`dataset/schema.py`, `tools/gtnh-extractor/`,
  GitHub #98).** Geometry alone cannot say which cells are I/O slots or what they accept, and for a
  layer-indexed machine that is load-bearing. The extractor now records, per cell, the
  `gregtech.api.enums.HatchElement` kinds it accepts, using StructureLib's element-visit
  instrumentation (`StructureLibAPI.enableInstrument` + `StructureElementVisitedEvent`) during the
  block pass. Two things this replaces: hint metadata is NOT hatch data (it is `dot - 1`, with
  13/14/15 reserved as StructureLib's `AIR`/`NOT_AIR`/`ERROR` - a meta-13 cell is a hollow interior,
  which is why the EBF's coil layers looked like I/O slots), and re-running the block pass without
  `gt_no_hatch` recovers nothing, since GT's hatch element returns an unconditional `false` from
  `placeBlock` and the channel is read only by the survival autobuild path.
- **Previewer: hover a block to see its machine name (`previewer/html.py`).** A textured cube carries
  no readable label (the front-face name plate is drawn only on the flat placeholder box), so once
  every machine is skinned it was hard to tell which is which. Moving the pointer over any block now
  floats that block's machine name above it (a raycast pick, reprojected each frame so it stays glued
  to the block while the camera orbits) and clears when the pointer leaves. Hovering any sub-block of
  an expanded multiblock shows its parent machine's name.
- **Previewer: toggle the auto-output arrows (`previewer/`).** The controls bar gains an
  `arrows: on/off` button that shows or hides the cyan auto-output direction arrows, so a builder can
  declutter the view. When on, the arrows still follow the layer slider; the button disables itself
  for a layout with no auto-output connections.
- **Previewer resolves more single-block machines by name (`previewer/`, GitHub #3).** Building on
  the voltage-tier prefix match, `TextureManifest.mte_block` now also resolves two naming shapes the
  plan's generic names previously missed: tiered-storage families keyed by numeral in the manifest
  (`Super Tank` -> the lowest `Super Tank I`, likewise `Super Chest`), and flavor-prefixed in-game
  names where the generic name is a whole-word suffix (`Chemical Plant` -> `ExxonMobil Chemical
  Plant`, `Coke Oven` -> `Industrial Coke Oven`). On the two example lines this takes single-block
  texture resolution from 8 to 25 machines; only the solver-synthesized Power Sources stay
  placeholders. The new strategies run after the exact/normalized/tier-prefix ones, so nothing that
  resolved before changes, and a genuinely unknown machine still resolves to nothing (kept on its
  placeholder box, never mis-mapped).
- **Real multiblock footprints wired into the solve path (`cli`, `adapter`, GAP A / the overlap
  fix).** `gtnh-solve` now loads the committed physical dataset (`data/multiblocks/`) and passes it
  to the adapter, so a machine whose type the dataset knows (Electric Blast Furnace, Vacuum Freezer)
  reserves its real multi-cell footprint instead of the crude 1x1x1 default that made multiblock
  structures overlap and the previewer render them sparse. The lookup stays a graceful enhancement:
  a missing, unreadable, or empty dataset warns to stderr and falls back to 1x1x1 footprints, so the
  documented 0/1/2 exit-code contract is untouched (a type the dataset lacks, e.g. Forge Hammer or
  the nitrobenzene multiblocks, still gets the single-block default, so those examples are
  unchanged). `_bounding_region` is now footprint-aware: the region height clears the tallest machine
  (a hardcoded 4 made a 10-tall Distillation Tower infeasible before placement even ran) and the
  floor holds the summed footprint areas with routing slack, reproducing the old `side x 4 x side`
  sizing exactly for an all-1x1x1 line. A non-square-base multiblock is pinned to a single
  orientation until `occupied_cells` becomes rotation-aware (the placer, router, and validator share
  that primitive, so a rotated non-cubic footprint would be a shared blind spot); every current
  dataset machine is square-base and keeps all four orientations.
- **Previewer active/idle machine skin toggle (`previewer/`).** The viewer now carries a `state`
  control that swaps every machine between its idle (at-rest) and running skin, so a builder can see
  which faces light up when the line runs (e.g. the Distillation Tower and Large Chemical Reactor
  front overlays in the nitrobenzene preview). The texture pass bakes both states but emits a second
  `data:` URI (`scene.texturesActive`) only for faces whose running bake actually differs from idle
  (an `_ACTIVE` overlay); a plain casing, identical in both states, carries one texture, so the
  embedded page never bloats for faces that look the same. State selection and the byte-level dedup
  live in Python (`texturize_scene`); the viewer only swaps between the two maps the scene hands it,
  and disables the control for a layout where no machine has a distinct running skin. Default display
  stays idle (no behavior change until toggled).
- **Previewer textures generically named single-block machines by voltage tier (`previewer/`,
  GitHub #3).** A plan export names a single-block machine generically ("Forge Hammer"), but the
  schema-2 texture manifest keys every single-block machine by its in-game tier-prefixed name
  ("Basic Forge Hammer" at LV, "Advanced Forge Hammer" at MV), so a generic name never matched and
  the machine (e.g. the sand line's Forge Hammer) rendered as a flat placeholder box even though its
  texture was in the manifest. `TextureManifest.mte_block` now resolves a generic name plus the
  machine's voltage tier: it tries the exact name, then a case/punctuation/whitespace-normalized
  match, then the tier's GT prefix (LV "Basic", MV "Advanced") with a "Basic" fallback for the higher
  tiers whose naming diverges per family ("Advanced X II/III/IV", "Universal", "Elite"). Single-block
  skins are near identical across tiers, so the Basic texture is an honest preview stand-in when the
  exact tier key is absent; a genuinely unknown machine resolves to nothing and keeps its placeholder
  box (never mis-mapped). The scene dict now carries each machine's `voltage_tier` for the texture
  pass to read. The sand preview's three Forge Hammers now render with their real GT texture.
- **Commit the schema-2 layered texture manifest (`data/textures/manifest.json`, ~5.8 MB).** The
  lane 7 v2 previewer reads this manifest to skin machines with real GT textures, but the repo still
  shipped only the old schema-1 (icon-only, 9 casing blocks) manifest, so a fresh clone rendered every
  machine as a placeholder. This is the extractor's `server-itexture-reflection` output (pack 2.8.4,
  GT5-Unofficial 5.09.51.482): 1470 blocks (1395 machine-tile-entities carrying their `display_name`,
  75 plain blocks), 816 icons. The pre-commit large-file guard is scoped to exclude `data/` so the
  dataset can live in the repo (rather than a repo-wide `maxkb` bump). PNGs are still never committed
  (LGPL); the previewer fetches them from the pinned GT5U jar at render time.
- **Previewer real GT textures via per-block cubes and a Pillow bake (`previewer/`, lane 7 v2,
  GitHub #50).** Supersedes the v1 that skinned one stretched box per machine with a single
  representative casing - a defect that erased the coils, glass, and hatch faces that make a layout
  readable. The previewer now materialises each placed machine into ONE textured cube per constituent
  block (principle 6): it looks up the machine's extracted multiblock doc, selects the representative
  variant, expands its `blocks` list at each block's `[dx, dy, dz]` offset (yaw-oriented to the placed
  front, so the controller's front overlay points the way the solver oriented it), and textures every
  cube face independently. Cubes are clamped to the machine's reserved footprint (and a yaw that
  would spill a non-cubic machine past it falls back to native orientation), so one machine's blocks
  can never overlap a neighbour - wall-sharing is a GTNH feature left to a later change. Only a
  genuine 1x1x1 machine takes the single-block path (via the manifest's display-name index); a
  doc-less MULTIblock, such as the dynamic-height Distillation Tower whose extraction overflowed the
  variant cap, keeps its placeholder box rather than collapsing to a lone controller cube. A new Pillow
  pre-bake (`previewer/bake.py`) composites each face's layer stack - base times its RGBA multiply,
  then alpha-composited overlays, animated sprites reduced to frame 0 - into one flat 16x16 PNG per
  `(block, meta, side, state)`, so the three.js viewer only ever loads flat images and never
  composites at runtime; a face with no baked texture falls back to a neutral casing grey, an
  un-baked machine keeps its placeholder box. Pillow is the optional `preview` extra and its absence
  degrades the whole pass to placeholders. Golden tests pin the tint multiply (a machine base is never
  neutral grey), the per-block expansion (a multiblock is many distinct cubes, not one box; an
  interior coil textures distinct from the casing), and icon-name stability. PNGs stay LGPL and
  uncommitted, fetched from the pinned jar into an out-of-repo cache (`previewer/jar.py`, injected so
  the test suite never fetches).
- **`LayoutMetrics` footprint/layers are now populated (`solver`, GitHub #13).** `solve()` fills
  `LayoutResult.metrics.footprint` (floor-area bounding box of machines plus routes) and `.layers`
  (vertical extent) on every assembled layout, computed from the same occupied-cell basis the
  feedback loop ranks on. These are consumed as data (the seed-compare workflow, and the previewer
  embeds them in its scene JSON); previously they were always `null`. `buildability` and
  `congestion` stay `None` until a scoring model is defined; an infeasible (nothing-placed) result
  leaves all metrics `None`.
- **Adapter consumes the plan-schema-v2 `resolved` block (`adapter/`, GitHub #2).** A
  gtnh-factory-flow v2 export (`schemaVersion: 2`) additively carries `app`,
  `datasetVersionId`, and a `resolved` throughput block (per-machine EU/t, per-edge rates,
  external I/O, a power total); `adapter/plan.py` now parses all of it typed (unknown
  subfields stay tolerated). When `resolved` covers a node, its `totalEut` is trusted for
  `Machine.eut` - the exporter's balancer models overclocking, which `recipe.eut * parallel`
  cannot - and cross-checked against that synthesis: a divergence beyond float tolerance
  emits an `AdapterWarning` (new, exported) but the resolved figure wins, so power amperage
  is sized for the real draw. `resolved.power.totalEut` is likewise cross-checked against the
  synthesized per-tier power nets. v1 plans (no `resolved`) adapt exactly as before.
  `examples/gtnh-sand.json` is refreshed to the v2 export (adapter output unchanged - its
  resolved figures match the synthesis); the v2 nitrobenzene export ships as
  `tests/fixtures/gtnh-nitrobenzene-v2.json` instead of replacing the example, because its
  resolved EU/t legitimately diverges (overclocked LCR: 2880 vs 480 EU/t) and would shift the
  example-pinned power numbers.
- **Extractor channel handling and identity-substitution tables (`tools/gtnh-extractor/`, lane 3,
  GitHub #46).** `StructureDumper` now fills the per-controller `substitutions` object. After the
  trigger-stack sweep it probes each GT channel (`GTStructureChannels.values()`, skipping the
  always-applied `gt_no_hatch`) against the default build: holding the stack size at 1 it sets one
  channel at a time and diffs the placed blocks. Because an unset StructureLib channel reads the
  trigger's stack size, the existing stack sweep already varies every channel, so shape-changing
  channels (a distillation tower's `height`, a structure's `length`) are already recorded as size
  variants and the probe skips them; a channel that only swaps a tiered block (coil, glass, pipe
  casing) keeps the same shape and is recorded once as `substitutions[channel]` = the default tier
  plus every distinct higher tier `{channel_value, block, meta}`, rather than exploding into one
  variant per tier. The default-placed block is always included, which is what lets the Python
  adapter match the tiered blocks in the primary variant. Heating coils are a special case: the
  classic furnaces (Electric Blast Furnace, Multi Smelter, ...) place a bare `ofCoil` whose tier is
  read from the trigger's stack size rather than the `coil` channel, so the coil table is built by a
  separate stack-size sweep that identifies coil blocks by the GT `IHeatingCoil` interface (which
  also covers the channel-bound mega furnaces). New hard caps bound the per-channel value sweep and
  the total substitution entries; a controller that overflows them lands on the `_meta.json` failure
  list instead of emitting a runaway table. The Electric Blast Furnace stays one 3x3x4 shape variant
  and now carries a populated `coil` substitution table (14 tiers), so the adapter counts 2 coil
  layers.
- **Layered server-side `ITexture` texture manifest (`tools/gtnh-extractor/`, lane 6 v2, GitHub #79;
  spike #78).** Supersedes v1's flat single-icon Option A, which could only name casing shells and
  gapped every single-block machine and controller hull. A one-day spike (#78) first proved, against a
  booted GT5-Unofficial server, that the 6-arg `getTexture(...)` is not `@SideOnly(CLIENT)` and the
  `ITexture` layer objects store plain server-safe fields (`mIconContainer`, `mRGBa` via `getRGBA()`,
  `glow`, wrapper `mTextures`). The rewritten `TextureDumper` then emits schema-2 layered manifest
  entries: for every MetaTileEntity - via the `getXxxFacing{Inactive,Active}(byte)` accessors for
  basic single-block machines (reliable with no tile entity; `getTexture` NPEs on a bare placement for
  some) and via `getTexture(base, side, facing, colour, active, redstone)` for hulls and hatches
  (placed like `StructureDumper` does) - it walks the layer stack per side and active state, resolving
  each `GTRenderedTexture` to `{icon, rgba, glow}`, recursing multi/sided wrappers via `mTextures`, and
  resolving a hull's copied casing base through the block-icon path. The plain structure blocks a
  multiblock places (casings, coils) keep the v1 block-icon mechanism as un-tinted single layers.
  Icon names come from the `Textures.BlockIcons` enum `name()` (the client-only `getTextureFile()`
  throws server-side, as the spike confirmed), mapping 1:1 to the PNGs under
  `assets/<modid>/textures/blocks/`. Each MTE entry carries its `display_name` so the previewer can
  render single-block machines. PNGs are never committed; unresolved units (exotic ISBRH renderers,
  a tail of newer casing families) land on the manifest `gaps` for a follow-up. Wired additively via
  `-PtextureOut`: set alone the run is texture-only and skips the structure dump.
- **Extractor core dump loop (`tools/gtnh-extractor/`, lane 2, GitHub #45).** The Java tool now
  fills its `DumperMod.dump()` seam with `StructureDumper` + `JsonWriter` + `ErrorCollector` and
  emits the schema-v1 dataset. It iterates `GregTechAPI.METATILEENTITIES`, keeps the
  `IConstructable` controllers, and for each places it at a fixed origin in the server overworld,
  sweeps the trigger stack (size 1..N, stopping when the placed cell set stops changing so
  identity-only tier swaps collapse into one variant), and per size runs a hint pass
  (`construct(_, hintsOnly=true)` with a `RecordingProxy` swapped into `StructureLib.proxy`, and the
  world's `isRemote` flag briefly flipped since the hint walk is client-only) plus a block pass
  (`construct(_, hintsOnly=false)` with the `gt_no_hatch` channel, then scan). It writes one
  `<datasetOut>/multiblocks/<name>.json` per controller plus a `_meta.json` run summary (schema,
  pack version, mod versions, timestamp, extractor SHA, controller count, failures), with stable
  key + variant ordering. Every controller is wrapped so an exception, a non-terminating/explosive
  sweep, or an empty scan lands in `_meta.json.failures` rather than aborting the run; hint capture
  is best-effort so a controller with client-only icon hints still dumps its geometry. The output
  directory and run metadata come from `-PdatasetOut`/`-PpackVersion`/`-PextractorSha`. A verified
  headless `runServer` boot dumps 191 of 209 constructable controllers (Electric Blast Furnace
  3x3x4, Vacuum Freezer 3x3x3), all validating against `dataset/schema.py`. Channel handling /
  identity-substitution tables (`substitutions` stays empty) are lane 3; textures are lane 6.
- **Multiblock physical dataset - schema v1 + Python adapter (`dataset/`, GitHub #48).** The
  first slice of the automated dataset-extraction pipeline (`DATASET_EXTRACTION_PLAN.md`): the
  path from an extractor's raw JSON to the solver's physical rules. `dataset/schema.py` is a typed,
  `extra="forbid"` Pydantic loader for schema v1 (`MultiblockDoc` + `_meta.json` `DatasetMeta`,
  per plan section 4.2: `schema`, `controller`, `variants[blocks/hints/bbox]`, `substitutions`,
  `failures`), the cross-language contract for the future Java extractor (issue #45), with a
  derived JSON Schema (`multiblock_json_schema()`) for non-Python consumers so it cannot drift.
  `dataset/multiblocks.py` is the adapter that does **all interpretation in Python** (plan design
  principle 3): it derives each machine's footprint bounding box, hint-derived I/O faces, and
  coil-tier count from the raw facts into an IR-shaped `MachinePhysical`, and `load_physical_dataset`
  keys a whole dump by display name. Because the real extractor is not built yet, illustrative
  hand-authored fixtures ship under `data/multiblocks/` (Electric Blast Furnace, Vacuum Freezer)
  marked as such in a README, so the adapter and golden tests run today. Golden tests pin the
  ground truths (EBF is 3x3x4 with two coil layers and hatch-layer hints; Vacuum Freezer is 3x3x3)
  plus schema validation (every file validates, `_meta.json` failure list under a lenient
  threshold). Wired **opt-in** into the gtnh-factory-flow adapter: `to_input_ir(plan, physical=...)`
  stamps a known machine's real footprint on the `InputIR`, while the default path stays single-block
  so the solver runs with or without a dump. No IR contract change (additive keyword-only argument).
- **Automated dataset-update CI (`.github/workflows/update-dataset.yml`, lane 4)** - a
  weekly + manual workflow that tracks the latest *stable* GTNH pack: it resolves the pack
  version from the DreamAssemblerXXL manifests, diffs the pinned mod versions against
  `gtnh.lock.json` (exiting green with no PR when unchanged), and on a change bumps the
  extractor pins, runs the headless Forge dump, installs the dataset, re-locks, runs the
  full test suite, and opens a reviewable PR whose summary surfaces the controller-count
  delta, added/removed/changed multiblocks, and the extractor failure list. Never
  auto-merges. Backed by a typed, tested CI helper (`tools/dataset_ci/`) and a dataset-diff
  review checklist (`.github/PULL_REQUEST_TEMPLATE/dataset-update.md`).
- **Dataset extractor scaffold (`tools/gtnh-extractor/`, the repo's only Java)** - lane 1 of
  the automated multiblock-dataset pipeline (GitHub #44). A standalone GTNH
  `ExampleMod1.7.10`-based Gradle tool whose `DumperMod` (`@Mod` entrypoint) hooks
  `FMLServerStartedEvent`, runs an empty dump body, and calls `FMLCommonHandler.exitJava`
  (0 on success, nonzero on failure) so `./gradlew runServer` boots a headless dedicated
  1.7.10 server with GT5-Unofficial + StructureLib and exits as a pass/fail gate. GT5U and
  StructureLib are pinned in `dependencies.gradle` from the current stable pack manifest
  (2.8.4) and mirrored in the new repo-root `gtnh.lock.json`; the rest of GT5U's hard deps
  resolve transitively from its Nexus POM. The Python solver gains no dependency on the tool
  (it will read only the JSON the tool emits; the dump loop itself is lane 2). `NOTICE` now
  credits the two LGPL mods.
- Project scaffold: docs, package skeleton, CI, license.
- Design and architecture documentation ported from the office-hours design doc
  and the engineering review (see `docs/`).
- **IR contracts (`ir/`)** - the two versioned Pydantic v2 schemas everything couples
  to: `InputIR` (the problem) and `LayoutResult` (the solution), with shared cell-grid
  geometry and enums. The input IR enforces referential integrity; geometric/rule checks
  are left to the validator. Full test suite (example + hypothesis). `docs/IR.md` updated
  to match the implemented shape.
- **Validator (`validator/`)** - the automated correctness gate: `validate(problem, layout)`
  independently checks a layout's geometry + structure (machines in-bounds / non-overlapping /
  off reserved cells / legally oriented / fully placed; nets routed once, contiguous,
  in-bounds, ME-toggles honored; pinned I/O on-route; power thickness well-formed) and
  returns a `ValidationReport` of every proven violation - never raises, never passes a
  silently-invalid layout. Rule-data checks (tier caps, summed amperage, face reachability)
  are stubbed for the dataset lane. In-code golden corpus (one known-bad case per violation).
- **Placement (`placement/`) - Phase 1 crude placer** - `place(problem)` does deterministic
  first-fit constructive placement on the cell grid (floor layer first, honoring reserved
  cells and never overlapping; orientation = first legal option), returning a
  `PlacementResult` that is either every machine placed or an explicit `Infeasibility` naming
  what did not fit (never raises). The validator independently certifies the output. Shared
  cell-grid helpers (`occupied_cells`, `in_region`) lifted into `ir/geometry.py`. Property
  test proves the core promise: any input yields a valid placement or an explicit
  infeasibility. SA/LNS placement is Phase 2 (see `docs/ROADMAP.md`).
- **Adapter (`adapter/`)** - `adapt_file(path)` / `to_input_ir(plan)` map a gtnh-factory-flow
  exported plan JSON to `InputIR`: nodes -> machines (recipe I/O -> item/fluid ports, computed
  typed throughput), storages -> boundary **Super Chest** (items) / **Super Tank** (fluids)
  machines (blocks that take I/O covers, so covers ride machine/storage faces, never pipes),
  edges -> nets. Typed view of the consumed export shape (`plan.py`, tolerant of extra fields).
  Two real exports committed as fixtures in `examples/` (sand, nitrobenzene). Crude for Phase 1:
  single-block footprints, default orientations, power nets not synthesized yet.
- **Router (`router/`) - Phase 1 crude router** - `route(problem, placements)` resolves a
  `Terminal` per net endpoint on a usable (non-front) machine face, then A* between terminals
  over the free cell grid, returning routes or an explicit `Infeasibility`. The sand demo line
  now goes **export -> place -> route -> validator.ok**, the whole Phase 1 slice end to end.
  Crude: one channel, no capacity, item/fluid only. Added `Route.terminals: list[Terminal]` to
  the output schema (additive); the validator gained the route<->endpoint **reachability check**
  (terminal on a non-front face adjacent to its machine and on the route). Machine `orientation`
  is now constrained to horizontal facings (GT machines never face up/down).
- **Build guide (`buildguide/`)** - `build_guide(problem, layout)` renders a `LayoutResult` as
  a human-readable text guide: header, bill of materials (machines by type, pipe/cable cells
  per commodity, I/O cover count), per-net connections (resource + machine faces), and a
  per-layer ASCII map with a key. The cheap, visible Phase 1 payoff - a player can read and
  build the sand line from it - ahead of the three.js previewer.
- **Solver (`solver/`) + auto-output** - `solve(problem)` composes the pipeline: place (now in
  **flow order** - a topological sort so producers land next to consumers) -> assign
  **auto-output connections** (a source machine ejecting straight into an adjacent target's
  input face: no pipe, no cover, GT's free connection - one auto-output per machine) -> route
  pipes only for what auto-output can't cover -> assemble. The sand line now solves to a flat
  row of 4 machines auto-feeding each other: **zero pipes, zero covers**. Added
  `LayoutResult.auto_connections: list[AutoConnection]` (additive); a net is satisfied by a
  `Route` XOR an `AutoConnection`, and the validator checks auto-connection adjacency / faces /
  single-auto-output-per-machine. Power nets are still not synthesized (the export has no power
  source) - a shared-amperage power model with optimized source count/placement is next.

- **Contributor standards & tooling** - documented coding + Conventional-Commits
  conventions in `CONTRIBUTING.md`; added a `.pre-commit-config.yaml` (ruff lint + format,
  `mypy --strict`, file hygiene, commit-msg lint), a PR template, and bug/feature issue
  templates.

- **Power (shared-amperage net) - synthesis + routing.** The export carries each machine's
  `eut` + voltage tier but no power source, so the adapter now synthesizes the power network:
  one synthetic source machine + one shared-amperage power net per voltage tier feeding the
  powered machines (`adapter/power.py`). The new power router (`router/power.py`) routes each
  per-tier net as a cable trunk and sizes every segment to the **summed amperage of the machines
  downstream of it** (1x/2x/4x/8x/16x), rejecting a load over the 16x cap as an explicit
  infeasibility - correctness-first single-source-per-tier (multi-source / voltage-loss
  optimization is Phase 2). `solve()` runs it alongside the item/fluid router, and placement no
  longer lets a power source split an auto-feeding material chain. The build guide gains a
  **Power** section telling the builder where to feed external power (synthetic sources are not
  self-powered). Backing it: a new `dataset` voltage ladder + `amperage` helper, `Machine.eut`
  (additive, InputIR v1), and shared router grid/dock/A* primitives lifted into `router/_grid.py`
  (the generic router no longer touches power). See `docs/DOMAIN.md`, `docs/ARCHITECTURE.md` #8.

- **CLI (`gtnh-solve`)** - the first real Phase 1 entry point: `gtnh-solve <export.json>` loads +
  adapts the export, solves (place -> auto-output -> item/fluid + power route -> self-validate),
  and prints the build guide (`-o FILE` to write it, `--seed` to pick the seed). Exit code 0 when
  the layout is fully VALID, 1 when the solver returns an explicit infeasibility (printed to
  stderr), 2 when the export can't be loaded. Replaces the planning-stub entry point.

- **Placement optimizer (`placement/search.py`) - Phase 2 simulated annealing.**
  `optimize_placement(problem, *, seed)` seeds from the constructive first-fit placer and
  improves a **routing-aware cost** (per-net half-perimeter wirelength + compactness + flat-build
  bias) with relocate / swap / **reorient** moves (orientation is a search variable), Metropolis
  acceptance, geometric cooling, best-valid-so-far. `solve()` now uses it (the crude placer stays
  as the SA seed + a fallback). Every accepted state stays validator-clean; deterministic per
  seed. Connected machines cluster - a hub+4-spoke star drops from HPWL 10 (first-fit row) to 5
  (annealed cluster), and sand stays all-auto-output. LNS + the place<->route feedback loop are
  next (docs/ROADMAP.md lane C).

- **Placement LNS (`placement/search.py`) - large-neighbourhood ruin-and-recreate.** The optimizer
  gains a large move alongside relocate / swap / reorient: rip out a *related* (net-connected)
  cluster of machines and greedily re-insert each at the position + orientation that minimises the
  cost, biased toward cells beside its already-placed net-neighbours. One step reshapes a whole
  cluster, escaping local optima the single-cell moves plateau in. It is probability-gated inside
  the same annealing loop, so Metropolis acceptance / cooling / best-so-far and per-seed
  determinism are unchanged, and every candidate stays validator-clean (recreate validity-checks
  each insertion and abandons the move if a machine cannot be re-placed). Insertions are ranked by a
  cheap marginal cost (the machine's own nets + auto pairs + a flat-build bias), not a full recompute,
  so LNS fits the same budget as the small moves. Finishes the SA + LNS half of ROADMAP lane C.
  Because the cost is still HPWL-driven, tighter clustering can push a route onto a second layer (the
  sand demo's power cable now rises one layer, still valid) - the future congestion-aware cost
  (lane C) is what removes that. (`placement/`.)

- **Solver "optimize or not" toggle (`solve(..., optimize=...)`, `gtnh-solve --fast`).** `solve`
  gains an `optimize` flag. The default (`True`) runs the annealed placer (SA + LNS) inside the
  place<->route feedback loop; `False` takes a near-instant single constructive placement, with no
  annealing and no re-placement. Both validate their output (VALID / explicit partial /
  infeasibility, never silently invalid) - fast just trades the optimizer's clustering and
  unrouted-net recovery for speed. `--fast` exposes it on the CLI. This is the user-facing control
  the planned unified site is built around, and the home for LNS (opt-in behind the optimized
  path). (`solver/`, `cli/`.)

- **Previewer (`previewer/`) + `gtnh-solve --preview`** - a self-contained, double-clickable 3D
  view of a solved layout. `build_scene(problem, layout)` flattens the layout into a render-ready
  scene (machine boxes coloured by type with the machine name on the front face, rectangular
  cables/pipes sized by cable thickness with a lead to each machine face, auto-output arrows,
  legend, and a tight `bounds` of the built extent) - a pure, fully-tested mapping; `render_html`
  inlines it into a static three.js viewer (CDN, no npm build) with an **orbit + pan camera**
  (right-drag / arrow keys) and a **layer-by-layer slider**, framed on the built extent rather
  than the solver's oversized search region. `gtnh-solve plan.json --preview view.html` writes it.
  Build-assist scope; the congestion heatmap, multi-seed compare, real block textures, and offline
  (vendored three.js) are follow-ups.

- **Routing capacity invariant (lane D, first slice).** Routes are now laid **capacity-aware**:
  each laid route's cells become obstacles for the routes after it - across item/fluid (`route`)
  and power (`route_power` gains an `extra_obstacles` arg the solver feeds the item cells into) -
  so no cell ever carries two routes (the crude single-channel cap: one route per cell). The
  validator independently enforces it (`route_cell_collision`), closing the gap where item pipes
  and power cables could share a cell and the abstraction would certify an unbuildable layout
  (docs/ARCHITECTURE.md #7). Crude for now: one channel per cell; the per-edge multi-channel cap
  (a routing margin hosting several parallel channels) is a later lane-D slice.

- **Rip-up/reroute (lane D, second slice).** Capacity makes routing order-dependent - a net that
  grabs a scarce cell can wedge a later net out (a *false* infeasibility, not a real one). The
  item/fluid router now routes a pass, and if any net failed, rips everything up and retries with
  the failed nets moved to the front (most-constrained-first), stopping when a pass is clean or a
  failed-net set repeats (a genuine infeasibility, not an ordering accident). So a bad net order is
  no longer mistaken for unroutable. Crude failed-first reordering; negotiated-congestion routing
  (the gold-standard, order-independent approach) is tracked as a follow-up (GitHub #7).

- **Build guide is buildable from alone.** The text guide was a sketch; it now carries the detail a
  player needs to build the line without guessing: a **Placement** table (each machine's exact
  `(x, y, z)` cell, front face, and footprint), per-pipe-terminal **covers** (conveyor for items,
  pump for fluids, in input/output mode - docs/DOMAIN.md), the exact **cells** each pipe/cable runs
  along, and **per-segment cable thickness** for power. The Power note now states the amperage to
  feed each source (its trunk-root thickness) instead of pointing at the ASCII map that never
  showed it.

- **Place↔route feedback loop in `solve()`** (docs/ARCHITECTURE.md #1, #6). `solve()` no longer
  takes a single placement on faith: it assembles an attempt (place → auto-output → route →
  validate), and if the router leaves nets unrouted it **penalizes exactly those nets** so the next
  placement pulls their machines tighter (shorter routes, or adjacency that auto-outputs) and
  re-places with the next seed. It keeps the best layout seen and returns the first fully-VALID one
  (anytime: best-so-far), stopping early when re-placing cannot help - a non-routing defect, or the
  same nets failing again. A layout a single attempt leaves `partial_invalid` (one net it could not
  pipe in a congested placement) now solves VALID. Deterministic (bounded attempts keyed off `seed`
  + the accumulated penalties, no wall-clock). The routers gained `failed_nets` (which nets stalled)
  and `optimize_placement` a `net_penalties` weight to carry the signal. Crude feedback (penalize +
  re-seed); a richer incremental routing estimate inside the SA move is future work.

- **Build guide states the boundary + a real power-feed spec (GitHub #15).** Two gaps that made
  the "buildable from alone" guide actually need guesswork are closed, both from data already in the
  IR. A new **System inputs / outputs** section names what to load each boundary input storage with
  (resource + typed rate, e.g. `load Super Chest at (0, 0, 0) with minecraft:stone (~0.1 items/t)`)
  and where each finished product exits with nothing collecting it (`minecraft:sand exits Forge
  Hammer at (3, 0, 0) - place a Super Chest/Tank to collect it`) - boundary storages that only
  source, and machine output ports no net consumes. And the **Power** note now reads as a wiring
  spec - `feed LV (32 V), >=4 A -> up to 128 EU/t` (tier voltage from the `dataset` ladder × the
  trunk-root amperage) - instead of the bare cable thickness (`4x amperage`) it printed before.

- **Previewer shows the system's inputs, outputs, and power (GitHub #5).** The 3D preview now
  surfaces the same boundary the text guide does: a **System I/O** panel in the HUD lists the
  inputs to load (resource + rate), the products to collect, the total EU/t draw, and the summed
  **amperage per voltage tier** (the tier already implies the volts, so amps is the useful number,
  e.g. `LV 3A`). A toggle switches every rate between **per tick and per second**. Both surfaces
  read from one new shared, fully-tested helper - `system_io(problem, layout)`
  (`gtnh_solver/system_io.py`) - so the guide and previewer can never disagree on what crosses the
  line's edge; `build_scene` emits it as `scene.io` and the build guide was refactored onto the
  same helper (its text output is unchanged).

- **Boundary output rates in the previewer (GitHub #16).** A finished product exits a machine
  output port that no net consumes, so its rate lived nowhere - the previewer showed the product
  with no throughput. The adapter now records each port's rate from the recipe on a new additive
  `Port.rate` (items/t or mB/t, InputIR v2, no version bump), and `system_io` reads it, so the HUD
  shows e.g. `out: minecraft:sand (0.1 items/t)`. Input rates are unchanged (still the net's typed
  throughput).

- **The adapter closes the line: output buffers (GitHub #16).** A system output used to exit a
  machine into thin air (only inputs got a boundary storage), so a line was never fully collectible
  without hand-editing. The adapter now synthesizes a **Super Chest/Tank + net per unconsumed
  output** (a machine OUTPUT port no net sources), placed and wired at the port's recorded rate, so
  the product is gathered automatically - the sand line now auto-outputs its sand into a collection
  chest (still zero pipes). `system_io` reports a boundary storage that only *sinks* as a system
  output (mirroring the only-*sources* input), and the build guide reads `minecraft:sand collected
  by Super Chest at (x, y, z) (~0.1 items/t)`. Sand grows from 5 machines to 6, nitrobenzene from
  21 to 23.

- **Power sources reserve a boundary feed face.** A synthesized power source is fed by the builder
  from outside the structure, but nothing said *where* - it was placed like any machine, so the
  optimizer could bury it mid-region with no face left for the external feed. Its **front face is
  now the reserved feed entry**: constructive and SA/LNS placement pin that face flush on the
  region boundary (every move preserves it; a problem with no such slot is an explicit
  `power_feed` infeasibility), and the validator enforces the same rule independently (new
  `POWER_FEED_NOT_ON_BOUNDARY`). Internal cables keep using the other five faces - the existing
  front-face rule already keeps them off the feed face. New shared helpers:
  `Machine.is_power_source` (the buildguide's private predicate, promoted) and
  `ir.geometry.front_on_boundary`.

- **The optimizer now finds compact, low-wire layouts (the hand-built sand target).** Two
  coordinated changes (docs/ROADMAP.md lane C). The placement cost is **footprint-first**: the
  compactness driver is now the floor area (x-span times z-span, weight 1.0), so stacking a layer
  is free while sprawling costs, with the bounding-box volume kept as a mild tiebreak; power nets
  lost their base wirelength term entirely (center-distance proxies cannot see dock faces or
  shared cable taps and measurably steered AWAY from low-cable layouts) and instead gain an MST
  trunk-length pull only when feedback-penalized, to rescue a power net the router failed. The
  real cable cost is judged where it is knowable: the solver's **feedback loop is now
  quality-driven** - every bounded attempt is fully routed + validated and the best VALID layout
  by (structure footprint, power cable cells, structure volume) wins, instead of returning the
  first valid one. Optimized sand now solves to a 5x1x2 stack - the machine row with the source
  on top and a **3-cell cable trunk** tapped through the hammers' top faces - matching the
  maintainer's hand-built 3-cable solution with a smaller footprint (5 vs 6) and volume (10 vs
  12). Acceptance is pinned by a solver test.

- **Selectable compactness objective** - `solve(..., objective="footprint" | "volume" |
  "balanced")` and `gtnh-solve --objective`. "Compact" is ambiguous and the two metrics pull
  opposite ways (stacking a layer shrinks the floor but can grow the enclosing box), so the
  builder picks: `footprint` (default, the maintainer's target) minimizes floor area and stacks
  tall, `volume` minimizes the enclosing box and stays flat/cubic, `balanced` weighs both. The
  objective drives both the placement cost's compactness weights and the feedback loop's quality
  ranking of routed layouts; the fast path ignores it (constructive placement is floor-first by
  construction). This is the future unified site's second user control, next to optimize-or-not.
  Sand passes the hand-built compactness + <= 3-cable budget under every objective.

### Changed
- **Cell geometry is rotation-aware, and the validator no longer shares it (`ir/`, `validator/`,
  `placement/`, `router/`, `previewer/`, `adapter/`).** `occupied_cells` turns a footprint about the
  vertical axis, so a non-square-base multiblock finally reserves the cells it actually covers, and
  the adapter's pin holding those machines to a single orientation is gone (81 of the 208 dumped
  controllers were affected). `orientation` is a **required** argument rather than a defaulted one:
  the primitive is shared by placement, the router and the validator, so a caller that forgot to
  rotate would be wrong identically on both sides, and requiring it makes every such caller a type
  error instead of a silent one.

  The validator now expands cells **independently** (`validator/_geometry.body_cells`), written from
  the dump's stated facing convention rather than derived from the solver's code; it used to
  re-export `ir.geometry.occupied_cells`, which was harmless only while that function could not
  rotate (docs/ARCHITECTURE.md #4). What stays shared is data, not derivation: `FACE_DELTAS` and
  `OPPOSITE_FACE` are six unit vectors and six pairs, on the same reasoning the amperage check
  already shares `tier_voltage` and `CABLE_LOSS_PER_BLOCK` with the router. A property test asserts
  the two expansions agree everywhere, and an oracle test re-derives every controller's cell set
  from the raw dump offsets at all four facings (208 of 208 pass locally; the committed fixtures
  cover two).

  Four sites needed fixing that no type error would have found. `_reorient` performed no geometry
  check at all, so a turn could overlap a neighbour or leave the region and still be accepted;
  `_apply_occupied_delta` keyed its diff on the cell, which a reorient does not change even though
  it moves every cell a non-cubic machine covers; `_rand_origin` bounded the random origin with the
  unrotated extents, both rejecting origins that fit and offering origins that do not; and
  `_free_origins` decided whether an origin was free without knowing the orientation. The reserved
  box the previewer clamps against is now rotated too, which retires the yaw-spill fall-back that
  used to draw a turned machine unturned.

  Neither shipped example moved: both are entirely square-base, and the orientation-before-cells
  reordering was done so the RNG draw sequence is unchanged, so every pinned layout, metric and
  cable count is exactly as it was. A sand solve is 0.77s against 0.87s before the lane.
- **The committed dataset is now just small fixtures; full datasets are local and version-namespaced
  (`dataset`, `previewer`).** The extractor's outputs are regenerated on demand into gitignored
  per-version folders (`data/<version>/{multiblocks,textures}/`), so several pack versions coexist
  without overwriting. The repo ships only the two multiblock fixtures and a ~120 KB texture manifest
  scoped to the example lines' machines (down from ~6 MB), so `gtnh-solve --preview examples/*.json`
  still skins out of the box; the full manifest is now local. The loader resolves the newest local
  `data/<version>/` that provides each of multiblocks/textures, else the committed fixtures, with
  `gtnh-solve --dataset-version <v>` to pin one and `--list-dataset-versions` to list them; the jar
  for texture PNGs is fetched at the GT5-Unofficial version the resolved manifest records, so its
  icons match. Reverses the earlier "texture manifest is committed" policy.
- **Previewer wire->machine leads take the connecting cable's thickness (GitHub #6).** Each route
  terminal in the scene now carries the thickness of the fattest route segment incident to its
  cell (a mid-trunk tap touches several; the fattest is what visually meets the block), and the
  viewer sizes the short lead from the cable into the docked machine face with the same
  thickness->cross-section ramp as the trunk segments - so a 4x run meets its machine visibly fat
  and a 1x tap thin. Item/fluid terminals carry `null` and keep their fixed-size pipe leads.
  Previewer-internal (scene + viewer template): an additive scene field the template reads with a
  fallback, so no scene-version bump. (`previewer/`.)
- **The item/fluid router negotiates congestion instead of retrying orders (GitHub #7).** Laying
  nets sequentially (each net's cells hard-blocking the next) made the result hostage to net
  order; the failed-first reorder retry only reduced that. The router now runs the FPGA
  PathFinder scheme: every net routes independently with priced A* (a contested cell costs a
  present-sharing penalty per other user plus a history penalty that grows every round it stays
  contested), and all nets re-route round by round until no cell is shared - so an
  ordering-induced false infeasibility cannot happen, and what remains contested after the round
  budget is reported per net as an explicit `congestion` infeasibility (a maximal collision-free
  subset is still emitted for the feedback loop). Once the contested set stops changing, a
  geometric proof (a bottleneck cell that two nets both cannot route around) ends the negotiation
  early instead of grinding the whole round budget, so a genuine single-bottleneck congestion is
  rejected in a few rounds rather than 32; the proof only ever bails on a demonstrated collision,
  so a resolvable contention is never misreported. Power trunks keep the
  failed-first rip-up/reroute (trees grown by multi-goal A* do not decompose into per-cell
  pricing). (`router/core.py`, `router/_grid.py`.)
- **The test gate runs in a quarter of the time (GitHub #74).** Profiling showed ~3/4 of every
  CI test leg was coverage tracer overhead, not test work (the solver's hot loops execute
  millions of traced line events). The suite now runs parallel by default (`pytest-xdist`,
  `-n auto` in addopts - the local gate drops ~155s to ~60s), CI gates coverage on ONE matrix
  leg instead of every leg, and that leg uses coverage's `sys.monitoring` core
  (`COVERAGE_CORE=sysmon`, branch-capable on 3.14+). No test dropped; the 90% gate and the
  required `test` status check are unchanged.
- **CI tests Python 3.14; packaging metadata reflects real support.** The test matrix now runs
  the floor and the latest release only (`3.10` + `3.14`; a floor break or a new-release break
  is what a leg catches, and the 3.11-3.13 intermediates cannot fail while both ends pass), and
  the package gains per-version trove classifiers
  (`Programming Language :: Python :: 3.10` through `3.14`) and moves from
  `Development Status :: 1 - Planning` to `3 - Alpha`. Internal CI/build polish along with it:
  pip caching, least-privilege `permissions`, cancel-superseded-runs `concurrency`, a
  `hatchling>=1.26` build pin, and a Dependabot config (GitHub Actions + pip, weekly).
- **The router now owns the auto-output vs pipe decision.** `route()` decides itself, from the
  final placements + orientations, which nets GT's free auto-output connection covers (the logic
  moved from `solver/core.py` to `router/auto.py`, public `assign_auto_outputs`) and lays pipes
  only for the rest; `RouteResult` gains `auto_connections` so the decision rides the router's
  output, and the solver's assemble step just composes it (its `skip_nets` plumbing is gone).
  Behavior is unchanged - same greedy net order, one auto-output per source machine, only
  1-source-1-sink item/fluid nets are eligible, power/ME never auto-feed - and the validator's
  independent auto-output checks stay the gate. This advances lane D (docs/ROADMAP.md): the
  router is the geometry authority, so the optimizer's job shrinks to moving blocks and choosing
  front faces. (`router/`, `solver/`.)
- **Power trunks grow as trees with shared taps.** The power router chained every net
  source -> m0 -> m1 -> ... as a path and docked each terminal on its own distinct cell, so a
  source + N sinks always cost at least N+1 cable cells - geometrically unable to reach the
  hand-built 3-cable sand trunk. In GT one cable block feeds every adjacent wired machine face,
  so the trunk is now a tree: a sink whose dock candidate is already a trunk cell of its net
  taps it (terminal on that cell, no new cable; the cell nearest the source wins), and any other
  sink extends the tree with a multi-goal A* leg from every trunk cell laid so far. Sizing
  follows the tree - each machine's cable distance is its terminal's depth, and every segment
  carries the summed amperage of the sink terminals on its far-from-root side (replacing the
  per-leg suffix sum, which overcharged one side of a branch) - and the validator already
  re-derives branched trees and shared terminal cells independently. A source + three clustered
  sinks now trunk with two cable cells, within the sand target's three. (`router/power.py`.)
- **Power cables dock route-aware, on whichever face is nearest the trunk.** The power router
  (`router/power.py`) used to commit each terminal to the first free non-front face in a fixed
  order (south first), blind to where the cable then had to run, so a source behind a machine row
  made the trunk snake around it. It now considers every usable (non-front) face and docks via a
  multi-goal A* leg on the one that gives the shortest cable (new `_grid.dock_candidates` +
  `astar_multi`), the source docking toward its first sink. On the sand demo this drops the
  optimized power run from nine cables to five (matching the constructive baseline); every terminal
  is still validated (non-front, adjacent, on-route) and the trunk stays a single tree.
- **Optimized placement minimises total volume, with no separate per-layer penalty.** The
  routing-aware cost (`placement/search.py`) dropped its `layer count` term (and the matching
  flat-build bias in the LNS recreate ranking): the bounding-box **volume** term already accounts
  for height, so the optimizer now trades layers against footprint purely by which yields the
  smaller box. Only the optimized (SA/LNS) path uses this cost; the fast constructive path is
  unaffected.
- **Power sizing now models cable voltage loss over distance.** GT cables lose voltage per block,
  so a machine `d` blocks from the source receives `tier_voltage - loss·d`, not the full tier. The
  source stays at the machine's tier and the cable is thickened to compensate: each machine's
  amperage is sized at its *delivered* voltage (`ceil(eut / (tier_voltage - loss·d))`), so a
  machine farther out draws more amps, and a run whose voltage drops to 0 is reported infeasible
  (`voltage_drop`). Loss is a flat 1 EU/block for every tier for now (per-material loss is Phase 2).
  The power router (`router/power.py`) accumulates each machine's cable distance while building the
  trunk and sizes from it; the validator independently re-derives the distance from the cable tree
  and re-checks (new `power_voltage_drop_excessive` violation); the boundary summary
  (`system_io.py`, feeding the previewer and build guide) reports the loss-inclusive amperage the
  builder must supply. Backing it: `dataset` gains `CABLE_LOSS_PER_BLOCK`, `delivered_voltage`, an
  `UnpowerableError`, and a `distance=` argument on `amperage`. This makes the emitted line
  actually buildable: a too-long low-voltage cable is no longer certified as valid. See
  `docs/DOMAIN.md`, `docs/ARCHITECTURE.md` #8.
- **Previewer power HUD shows the feed spec with correct values.** The system-i/o panel showed
  power as `48 EU/t (LV 3A)`, where the 48 is the machines' sub-tier draw (16 x 3) and the tier
  breakdown omitted the voltage - easy to mis-supply in game. It now shows the input the way a GT
  source is fed: a total EU/t supplied plus the per-tier **full tier voltage x amps**
  (`power: 96 EU/t (LV 32V x 3A)`, where 96 = 32 V x 3 A, so the total matches the breakdown). The
  scene's `io.power.byTier` entries gain a per-tier `volts` and `total` is the summed feed (scene
  version 1). (`previewer/`.)
- **InputIR bumped to v2 (breaking): dropped `Port.is_auto_output`.** It was a dead, contradictory
  field - the adapter never set it and the solver auto-connects any adjacent output regardless of
  it. Whether a port is satisfied by auto-output is a **solver decision**, not a problem input: it
  is recorded in the output's `AutoConnection`, and the "one auto-output per machine, items-xor-
  fluids, never power" rule is enforced there by the validator (`duplicate_auto_output` /
  `auto_output_illegal_commodity`), not on the input contract. `FaceSpec`'s now-moot auto-output
  validation is removed with it. (`ir/`.)
- **InputIR bumped to v1 (breaking): dropped `Machine.count`.** Multi-instance machine groups
  are not modelled until routing is instance-aware (Phase 2): the placer expanded `count` into
  N placements sharing one machine id, but a `MachineFaceRef` cannot address a specific
  instance, so the router/solver/validator collapsed the copies via `setdefault` and left the
  extras silently unwired. Each `Machine` is now exactly one instance; the adapter rejects an
  export `machineCount > 1` with an explicit `AdapterError` instead of emitting an under-wired
  layout. (`ir/`, `adapter/`, `placement/`, `validator/`.)
- **CI expanded** to a single static-checks job (via pre-commit), a Python 3.10-3.13 test
  matrix with a coverage gate (`--cov-fail-under=90`), and an advisory (non-blocking)
  Conventional-Commits check on PRs. Ruff now runs a curated lint rule set plus
  `ruff format`; the Pydantic mypy plugin is enabled. (`pyproject.toml`,
  `.github/workflows/ci.yml`.)
- Input foundation switched from a forked gtnh-flow (Python) to consuming
  gtnh-factory-flow's MIT, Zod-validated exported plan JSON. The adapter now parses
  that documented export (no vendoring); recipes/throughput/machine-IDs come from its
  dataset, so the hand-authored physical dataset shrinks. Removed the `vendor/`
  placeholder in favor of `examples/` for sample exported plans.
- Depend on a maintained fork of gtnh-factory-flow (fix only the consumed
  export/throughput/dataset path) and snapshot a known-good dataset + sample exports
  as fixtures so the solver is decoupled from the fork's health.

### Removed
- **All generated datasets are now local-only: retired the `update-textures.yml` CI workflow and
  removed the `tools/dataset_ci` helper package.** With the full texture manifest no longer committed
  (only the small example-scoped one is, see Changed), the workflow that regenerated and committed the
  ~6 MB manifest has no job; the manifest is regenerated locally like the structure dump.
  `tools/dataset_ci` (`resolve_versions`, `dataset_summary`) existed only to drive the already-removed
  `update-dataset.yml`, so it and its tests are gone, along with the `mypy`/`pytest` `tools/` path
  config that only served it.
- **The multiblock structure dump is now local-only: retired the `update-dataset.yml` CI workflow**
  (and its `dataset-update` PR template). The full ~190-controller dump (~17 MB of generated JSON)
  is not worth its repo weight or a weekly Forge run, so it is no longer built or committed by CI; a
  developer regenerates it on demand with the extractor. `data/multiblocks/` is gitignored apart from
  the two curated fixtures (Electric Blast Furnace, Vacuum Freezer) the adapter/footprint tests pin.
  Consequence: a fresh clone places and renders only the two fixtures plus single-block machines
  until the extractor is run locally.
- Dropped the unused `networkx` and `numpy` core runtime dependencies - neither was
  imported anywhere in the implementation. They will be re-added if and when the Phase 2
  optimizer/graph work actually needs them (see `docs/ROADMAP.md`).

### Fixed
- **A machine's hatch ceiling now describes the form it actually reserves (`dataset/`, `adapter/`).**
  `footprint_for` sizes a parametric machine to its recipe (a Distillation Tower with one fluid
  output reserves 3x3x3, not 3x12x3), but the hatch counts were always taken from the largest
  variant, so the two described different buildings. The Creosote Oil tower on the nitrobenzene line
  reserves 3x6x3 and was charged the 3x12x3 form's **97** hatch cells against its own **49**, a
  ceiling twice the shape the builder is told to raise. Today that only loosens a validator bound;
  once hatch slots carry geometry it would place hatches outside the reserved box, so it is fixed
  ahead of that work. `MachinePhysical.variant_for(fluid_outputs)` is now the single selection
  point, `footprint_for` and `energy_hatch_budget` both read through it, and the counts ride on
  `VariantShape` per built form. The record-level counts stay as the form `footprint` describes, for
  a fixed-shape machine and a pre-v2 dump with no variants. No shipped example loses headroom: the
  nitrobenzene tower needs 7 connections against its own 49.
- **The extractor no longer discards legitimately parametric multiblocks
  (`tools/gtnh-extractor/`, GitHub #98).** `MAX_VARIANTS = 6` rejected 16 of 191 controllers
  outright, including the Distillation Tower, Assembly Line, Cleanroom and Lapotronic
  Supercapacitor. It was the wrong instrument: the sweep cannot produce more forms than
  `MAX_STACK_SWEEP`, and per-variant blowup is already bounded by `MAX_CELLS`/`MAX_SCAN_DIM`, so a
  low variant cap only discarded real machines. Pinned to `MAX_STACK_SWEEP`, taking a local dump from
  191 controllers / 18 failures to 208 / 1 with no change to any previously extracted machine. A
  family still growing at the sweep ceiling (the Lapotronic Supercapacitor spans heights 4..50) now
  records that truncation in its own `failures` list rather than presenting a prefix as complete.
- **Super Tank / Super Chest output glyph faced the wrong way (`previewer/`).** A boundary-storage
  block auto-outputs from its front face, but the previewer oriented its output glyph (OVERLAY_STANK /
  OVERLAY_SCHEST) to the placer's `front`, which defaults every machine to north and does not track
  the eject face, so the glyph pointed away from where the block actually outputs (glyph north while
  the auto-output ejected east). A storage block with a horizontal auto-output now orients its glyph
  to that direction, so the glyph and the cyan auto-output arrow agree. Machines whose front overlay
  is a GUI/identity face rather than an output, and a storage block with a vertical eject (which a
  side glyph cannot point at), keep their placed front.
- **Basic single-block machines rendered without their front-face overlay (`tools/gtnh-extractor`,
  `previewer`, GitHub #3).** A basic machine (Forge Hammer, Macerator, Alloy Smelter, ...) drew as a
  plain steel box because its per-machine glyph never reached the manifest: the textured `mTextures`
  stack that carries the overlay is built `@SideOnly(CLIENT)` and is null on the dedicated server the
  extractor runs, and the `getXxxFacing…(byte)` accessors return the base casing layer only. The
  extractor now reconstructs the glyph from its deterministic asset path,
  `basicmachines/<folder>/OVERLAY_<FACE>[_ACTIVE][_GLOW]`, deriving `<folder>` from the machine's
  server-side `mName` (`basicmachine.<token>.tier.NN`) matched against the real folder set it
  enumerates from the GT5U jar, and appends it (plus a separate `_GLOW` emissive layer where a sibling
  PNG exists) above the casing, only for faces whose PNG actually exists so nothing is invented. On the
  committed example manifest this drops casing-only single-block fronts from 677 to 197, and the Forge
  Hammers in the sand preview now show their hammer glyph. The steel casing tint (`[210,220,255]`) and
  the multiblock hull overlays are unchanged.
- **Basic single-block machines were extracted painted black (`tools/gtnh-extractor`, `previewer`).**
  `TextureDumper.basicMachineLayers` read each machine's texture through the `getXxxFacing…(byte)`
  accessors with colour index `0`, which is the black dye, so the base casing came out tinted
  `[32,32,32]` (near-black gray) instead of the default `MACHINE_METAL` steel. Passing `-1`
  (unpainted) fixes it, and the committed example manifest was regenerated from a fresh extractor
  run, so the Forge Hammers (and every other basic machine) now render steel-blue rather than gray.
  (Their front-face overlay icon is captured separately; see the entry above.)
- **Auto-output arrow draws on top of the machine in the previewer (`previewer/html.py`, GitHub
  #30).** The per-face auto-output arrows (#20) render on every source face perpendicular to the
  ejecting direction, but the arrow sat a hair off the 0.92-scaled placeholder box, so it was buried
  under whatever drew in front of it: the opaque front-face name plate, and, on a machine that bakes
  real textures, the full-size (1.0) block cubes of its expanded render (the sand line's Forge
  Hammers hid the arrow entirely). The arrow is now lifted just outside the machine's actual rendered
  surface, expansion-aware (1.0 for the textured cubes, 0.92 for the placeholder box, plus a hair to
  clear the name plate), so it draws on top of both the casing texture and the label while normal
  depth testing still hides it behind any machine genuinely in front of it. The name plate keeps its
  opaque, high-contrast backing, so label readability is unchanged. Rendering-only, no scene or
  contract change.
- **Dark casing tints no longer bake to near-black in the previewer (`previewer/bake.py`).** The
  Pillow bake turned a GT layer tint into per-channel multipliers with a raw `value / 255`, so a
  dark-neutral casing tint like bronze's `[32, 32, 32]` collapsed to `~0.125` and multiplied the
  already-full-colour tier sprite down to mean RGB around 20 (a Basic Forge Hammer baked
  effectively black). The tint is now normalised by its brightest channel instead: identical to
  `/ 255` for any tint whose peak channel is 255 (the electric `[210, 220, 255]` majority and plain
  whites are byte-unchanged), but a dark-neutral tint becomes identity, so the sprite shows through
  at full brightness with its hue shift preserved. A regression test pins that a `[32, 32, 32]` tint
  keeps a bright sprite bright, and the existing golden tint guards move to the new hue-shifted
  values. This is a readability-first approximation; GT-pixel-accurate casing colour stays a
  deferred cosmetic item.
- **The cable-thickness ladder gains GT's 12x rung** (maintainer-reported). GT ships six cable
  sizes (1x/2x/4x/8x/12x/16x) but the dataset only knew five, so any segment or feed summing to
  9 through 12 amps was sized a whole rung thick (16x). The router now picks 12x for that band,
  the output contract and validator accept it, and the docs spell the full ladder.
- **Power router does failed-first rip-up/reroute, like the item router (GitHub #40).** The power
  router laid each tier's trunk in problem order and stopped at the first net that could not route,
  reporting only that one - but capacity accretes obstacles, so a trunk laid for one tier can wedge
  a later tier's trunk out of a chokepoint: a *false* infeasibility from net order alone, and a
  weaker feedback-loop signal than the item router already gave for pipes. It now routes a pass
  and, if any net failed, rips every trunk up and retries with the failed nets first (most-
  constrained-first), stopping only when a pass is clean or a failed-net set repeats (a genuine
  infeasibility, not an ordering accident). When routing does stall it reports ALL still-failing
  nets, not just the first, so the place↔route feedback loop can penalize them all. The bounded-
  retry loop is now shared with the item router (`core._rip_up_reroute`). (`router/power.py`,
  `router/core.py`.)
- **Validator derives power amperage independently of the router (GitHub #36).** The validator is
  meant to be a second, differently-written implementation so a bug in the router's power math is
  caught, not certified (docs/ARCHITECTURE.md #4) - but its amperage re-check still called the same
  `dataset.amp_load` / `whole_amps` helpers the router sizes cables with, so a bug in the loss
  formula or the ceil-with-epsilon rounding would have been blessed by both sides. It now inlines
  its own arithmetic (`eut / (tier_voltage - loss * distance)` per machine, summed per segment,
  `ceil` with the shared epsilon), importing only the rule DATA (the voltage ladder,
  `CABLE_LOSS_PER_BLOCK`, `_AMP_EPSILON`) so the rounding policy stays identical and the two still
  agree on every valid layout, while a sizing bug is now caught on a separate code path. Separately,
  an unknown/off-ladder voltage tier was reported as `power_thickness_insufficient` (whose meaning
  is "cable thinner than the summed amps") - a wrong signal for a route that is merely unverifiable;
  it now gets its own additive `power_tier_unknown` violation code. (`validator/`.)
- **User-facing output surfaces are hardened against bad input and bad paths (GitHub #39).** The
  previewer inlined the scene JSON into its `<script>` block unescaped, so a machine type or
  resource id containing `</script>` (plan JSON is external input) could close the tag and break or
  inject into the page; the inline JSON now escapes `</` to `<\/` (JSON-transparent, the scene still
  round-trips). The CLI's `-o`/`--preview` writes raised an uncaught `OSError` on an unwritable path,
  dumping a raw traceback instead of honoring the documented 0/1/2 exit-code contract; both writes
  now report `error: could not write <path>: <reason>` to stderr and exit 2. (`previewer/`, `cli`.)
- **Amperage is sized from fractional machine loads, rounded up per aggregate - not per machine**
  (maintainer-verified in game). GT machines pull whole packets (1 amp = one packet of up to tier
  voltage) into an internal buffer only when it has room, so a 16 EU/t LV machine *averages* 0.5
  amps - but `dataset.amperage` ceiled every machine to whole amps and the callers summed the
  ceilings, overstating every aggregate: the optimized sand line's feed spec read 3 A / 96 EU/t
  when 2 A / 64 EU/t runs it in game, and cables could come out a tier thicker than needed.
  `amperage` is replaced by `amp_load` (the un-rounded `eut / delivered_voltage`, same
  unknown-tier / unpowerable errors) plus `whole_amps` (the ceil, with epsilon slack for float
  dust), and the rounding moves to where packets are actually quantized: per cable segment in the
  router and validator, per tier in `system_io` (so the guide and previewer both now say
  2 A / 64 EU/t for sand; this supersedes the interim 3 A number from the drift fix below). Cable
  loss still raises far machines' loads; the 16x cap and unpowerable checks are unchanged.
  (`dataset/`, `router/power.py`, `validator/`, `system_io`.)
- **Build guide power note agrees with the previewer (and reality).** The note read the feed
  amperage off the trunk's thickest cable segment, which both understates a trunk whose sink taps
  the source's own dock cell (its amps flow through no segment - on the optimized sand stack the
  guide said `>=2 A -> up to 64 EU/t` while the previewer said 3 A / 96 EU/t) and overstates when
  amps round up to a cable tier (the fast sand row printed 4 A for a 3 A draw). Both surfaces now
  read the same shared `system_io` numbers: the tier's machine draws summed at each machine's
  delivered voltage. Per-segment cable thickness still lives under Connections. (`buildguide/`.)
- **Validator requires a consumer on routed nets (GitHub #8).** The gate enforced the
  OUTPUT->INPUT port direction on the auto-connection path but not on the routed-pipe path, so a
  routed net with no consumer (every endpoint an OUTPUT producer) passed - the golden "valid"
  fixture even normalized one. A routed net now needs at least one INPUT endpoint
  (`route_net_no_consumer`), while still allowing multiple same-commodity producers feeding one
  pipe (GT lets several machines eject into one line). The routed path also independently checks
  every endpoint carries the net's own commodity (`route_net_mixed_commodity`), so a mixed-
  commodity net is caught even if a producer bypasses the input IR's own check. The base test
  fixture now wires a real consumer.
- **Previewer floor grid aligns to cell boundaries (GitHub #19).** The grid lines landed on
  integer boundaries on one axis but cut through the middle of the blocks on the other (a
  `GridHelper` centering artifact: integer line offsets need an even division count, half-integer
  offsets an odd one, so parity decided it per axis). The grid now uses an even span snapped to an
  integer center, so every line sits on a cell edge and the blocks read as sitting in their cells.
- **Previewer draws auto-output direction on the machine faces (GitHub #20).** The cyan auto-output
  arrow ran center-to-center between the two adjacent machines, so it was buried inside their opaque
  boxes and you could not tell which machine fed which. It is now a small flat arrow on each source
  face perpendicular to the ejecting direction (the two side faces plus top and bottom), each
  pointing the way the machine ejects, so at least one stays visible from any angle however tightly
  the machines are packed.
- **Previewer renders routes GT-style (GitHub #31).** Cables and pipes were flat bars spanning
  cell-center to cell-center plus a separate fixed lead to each machine face, which did not read like
  an in-game pipe/cable. Every route (item, fluid, power) is now a small cube at each cell centre
  with a uniform cross-section arm out to the block edge for each connection (an adjacent route cell,
  or a docked machine face), power sized by cable thickness. One node per cell keeps a run readable
  however tightly the routes are packed.
- **Validator route + auto-connection soundness holes** - the only automated correctness gate
  was certifying some geometrically-impossible layouts. Routes are now checked for unit-step
  segments (a single segment can no longer "teleport" two cells across a machine - connectivity
  alone missed it), and no route cell may sit inside a machine body or on a reserved cell.
  Auto-connections are now checked against the net they claim to satisfy: the connection must
  join that net's real OUTPUT->INPUT endpoint machines (resolved by port direction), `net_id`
  must resolve, and power/ME-routed commodities cannot be auto-output. New violation codes
  (`route_segment_not_unit`, `route_through_machine`, `route_on_reserved`,
  `auto_output_wrong_endpoints`, `auto_output_illegal_commodity`) with one negative test each.
- **`solve()` now validates its own output.** It previously returned `valid` whenever
  placement and routing each reported success, without ever running the independent validator -
  so the "never returns a silently-invalid layout" promise was not enforced end to end. `solve`
  now runs `validate()` on the assembled layout and downgrades a `valid` result to
  `partial_invalid` (carrying the violation) if anything is proven wrong.
- **Validator enforces summed-amperage power sizing** (previously deferred). It independently
  re-derives each power cable's load - rooting the cable tree at its source terminal and summing
  the draw of the machines downstream of every segment - and flags a segment whose cable is
  thinner than its load (`power_thickness_insufficient`), which also catches a load over the 16x
  cap. So a power-sizing bug in the router is caught, not certified.
- **Validator no longer blesses an uncertifiable power route.** The amperage check used to
  *skip* (certify by silence) a power route it could not verify - one with zero or multiple
  source terminals, or whose cables form a cycle/tangle instead of a single tree - the exact
  silently-invalid case the independent gate exists to catch. It now rejects both with explicit
  violations (`power_net_no_single_source`, `power_route_not_a_tree`), each with a negative test.
- **Power router always builds a tree.** `router/power` A*'d each leg of a trunk against
  obstacles that excluded the cable already laid, so legs could overlap into a non-tree whose
  per-segment amperage is undefined. Each laid leg's cells are now obstacles for the legs that
  follow, so the trunk is always a single non-overlapping path the validator can verify.
- **Placement optimizer keeps auto-output (orientation-aware cost).** The SA cost was
  orientation-independent, so `reorient` moves were a free random walk that could finalize an
  orientation putting a machine's front (no-I/O) face on a connecting side and **blocking**
  auto-output. The cost now rewards orientations that enable auto-output (a shared
  `ir.geometry.auto_output_faces` helper, reused by the solver), so the optimizer preserves -
  and recovers - the free connections instead of degrading them.
- **Adapter sizes power for `parallel`.** A node's `eut` is now `recipe.eut * parallel`: a node
  running N recipes in parallel draws N times the power, matching how throughput already scales,
  so the synthesized power cable is sized correctly for `parallel > 1` (was under-sized).
- **Validator checks terminals belong to their net.** `_check_terminals` only verified that every
  net endpoint *had* a terminal; a route could still carry a **foreign** terminal (a machine/port
  that is not one of the net's endpoints) or two terminals for one endpoint and pass. It now flags
  both (`terminal_not_an_endpoint`, `duplicate_terminal`), closing the structural half of
  required-I/O-face reachability so the gate cannot certify a route with bogus docks.

[Unreleased]: https://github.com/MrBruh/gtnh-process-line-solver/commits/main
